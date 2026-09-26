import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from database import CorpusDB, DomainError
from dispatch import Dispatcher


class DispatchServiceTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.dispatcher = Dispatcher(self.db)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.i1 = self.db.add_item(self.batch, 1, "文本一")
        self.i2 = self.db.add_item(self.batch, 2, "文本二")
        self.i3 = self.db.add_item(self.batch, 3, "文本三")
        self.i4 = self.db.add_item(self.batch, 4, "文本四")

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def _freeze(self):
        for idx, (item, label1, label2) in enumerate(
            [(self.i1, "正向", "正向"), (self.i2, "中性", "中性"),
             (self.i3, "负向", "负向"), (self.i4, "正向", "正向")], start=1
        ):
            self.db.assign(item, self.a1)
            self.db.assign(item, self.a2)
            self.db.submit_annotation(item, self.a1, label1)
            self.db.submit_annotation(item, self.a2, label2)
        self.db.freeze_batch(self.batch, self.mgr)

    def test_bulk_dispatch_writes_and_skips_assigned(self):
        set_result = self.dispatcher.set_cap(self.batch, self.a1, 2)
        self.assertEqual(2, set_result["remaining"])
        result = self.dispatcher.assign_ordinals(self.batch, self.a1, [1, 2, 9])
        self.assertEqual(2, result["written"])
        self.assertEqual([1, 2], [e["ordinal"] for e in result["assigned"]])
        self.assertEqual([{"ordinal": 9, "reason": "条目不在该批次中"}], result["skipped"])
        self.assertEqual(0, result["remaining"])

        # 再次提交：已分给本人、他人占用、剩余名额不足、请求内重复均跳过且不写入
        self.dispatcher.set_cap(self.batch, self.a2, 5)
        self.dispatcher.assign_ordinals(self.batch, self.a2, [3])
        again = self.dispatcher.assign_ordinals(self.batch, self.a1, [1, 3, 4, 1])
        self.assertEqual(0, again["written"])
        reasons = [(e["ordinal"], e["reason"]) for e in again["skipped"]]
        self.assertIn((1, "已分给本人"), reasons)
        self.assertIn((3, "已分给他人"), reasons)
        self.assertIn((4, "超出剩余名额，本次未写入"), reasons)
        self.assertIn((1, "请求内序号重复"), reasons)
        # 名额不足时整体回滚：条目4 未被写入
        self.assertIsNone(self.db.get_open_assignment(self.batch, 4, self.a1))

    def test_capacity_overflow_writes_nothing(self):
        self.dispatcher.set_cap(self.batch, self.a1, 2)
        result = self.dispatcher.assign_ordinals(self.batch, self.a1, [1, 2, 3])
        self.assertEqual(0, result["written"])
        self.assertEqual([], result["assigned"])
        self.assertEqual(3, len(result["skipped"]))
        self.assertEqual(2, result["remaining"])
        self.assertEqual(0, self.db.conn.execute("SELECT COUNT(*) FROM assignments").fetchone()[0])

    def test_release_recalculates_quota_submitted_kept(self):
        self.dispatcher.set_cap(self.batch, self.a1, 2)
        self.dispatcher.assign_ordinals(self.batch, self.a1, [1, 2])
        self.db.submit_annotation(self.i1, self.a1, "正向")  # 已提交不占名额但不可退回
        # 提交后名额重算：只剩条目2占用
        ctx = self.db.dispatch_context(self.batch, self.a1)
        self.assertEqual(1, ctx["open_count"])

        with self.assertRaisesRegex(DomainError, "已提交"):
            self.dispatcher.release(self.batch, self.a1, 1)
        released = self.dispatcher.release(self.batch, self.a1, 2)
        self.assertTrue(released["returned"])
        self.assertEqual(2, released["remaining"])
        # 已提交标注仍在
        self.assertEqual(
            1,
            self.db.conn.execute(
                "SELECT COUNT(*) FROM annotations WHERE item_id=? AND annotator_id=?", (self.i1, self.a1)
            ).fetchone()[0],
        )
        # 释放后条目2可再次分派
        again = self.dispatcher.assign_ordinals(self.batch, self.a1, [2])
        self.assertEqual(1, again["written"])

    def test_frozen_batch_rejects_dispatch_and_release(self):
        self.dispatcher.set_cap(self.batch, self.a1, 4)
        self._freeze()
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.dispatcher.set_cap(self.batch, self.a1, 9)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.dispatcher.assign_ordinals(self.batch, self.a1, [3])
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.dispatcher.release(self.batch, self.a1, 1)

    def test_cap_required_and_validation(self):
        with self.assertRaisesRegex(DomainError, "上限"):
            self.dispatcher.assign_ordinals(self.batch, self.a1, [1])
        with self.assertRaisesRegex(DomainError, "上限"):
            self.dispatcher.set_cap(self.batch, self.a1, -1)
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.dispatcher.set_cap(self.batch, self.arb, 3)
        self.dispatcher.set_cap(self.batch, self.a1, 2)
        with self.assertRaisesRegex(DomainError, "序号"):
            self.dispatcher.assign_ordinals(self.batch, self.a1, [])
        result = self.dispatcher.assign_ordinals(self.batch, self.a1, [0, "x", 2.5, True])
        self.assertEqual(0, result["written"])
        self.assertEqual(4, len(result["skipped"]))
        # cap=0 取消上限
        cleared = self.dispatcher.set_cap(self.batch, self.a1, 0)
        self.assertIsNone(cleared["cap"])
        self.assertIsNone(self.db.get_dispatch_cap(self.batch, self.a1))

    def test_legacy_assign_blocked_on_frozen_batch(self):
        self._freeze()
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.db.assign(self.i1, self.a1)


class DispatchHttpTest(unittest.TestCase):
    """页面操作层（路由）冒烟测试。"""

    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.environ["CORPUS_DB"] = self.path
        os.environ["PORT"] = "0"
        sys.modules.pop("app", None)
        import app as app_module

        self.app = app_module
        # Handler.db 在类定义时绑定；换成临时库并重绑判定层
        app_module.Handler.db = app_module.CorpusDB(self.path)
        app_module.Handler.dispatcher = app_module.Dispatcher(app_module.Handler.db)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app_module.Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

        db = app_module.Handler.db
        self.a1 = db.add_user("标注员A", "annotator")
        self.a2 = db.add_user("标注员B", "annotator")
        self.gid = db.add_guideline("http-v1", "规则")
        self.bid = db.create_batch("HTTP批次", self.gid)
        db.add_item(self.bid, 1, "甲")
        db.add_item(self.bid, 2, "乙")
        db.add_item(self.bid, 3, "丙")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.app.Handler.db.close()
        os.unlink(self.path)

    def _post(self, path, payload):
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_dispatch_routes(self):
        status, body = self._post(f"/api/batches/{self.bid}/dispatch-cap",
                                  {"annotator_id": self.a1, "cap": 2})
        self.assertEqual(201, status)
        self.assertEqual(2, body["remaining"])

        status, body = self._post(f"/api/batches/{self.bid}/dispatch",
                                  {"annotator_id": self.a1, "ordinals": [1, 2]})
        self.assertEqual(200, status)
        self.assertEqual(2, body["written"])
        self.assertEqual(0, body["remaining"])

        # 名额已满：新增超出剩余名额，整体不写入
        status, body = self._post(f"/api/batches/{self.bid}/dispatch",
                                  {"annotator_id": self.a1, "ordinals": [3]})
        self.assertEqual(200, status)
        self.assertEqual(0, body["written"])
        self.assertEqual("超出剩余名额，本次未写入", body["skipped"][0]["reason"])

        self._post(f"/api/batches/{self.bid}/dispatch-cap",
                   {"annotator_id": self.a2, "cap": 5})
        status, body = self._post(f"/api/batches/{self.bid}/dispatch",
                                  {"annotator_id": self.a2, "ordinals": [2]})
        self.assertEqual("已分给他人", body["skipped"][0]["reason"])

        status, body = self._post(f"/api/batches/{self.bid}/release",
                                  {"annotator_id": self.a1, "ordinal": 2})
        self.assertEqual(200, status)
        # 条目1仍为未提交分派占用名额，条目2退回后释放 1 个
        self.assertEqual(1, body["remaining"])

        status, body = self._post(f"/api/batches/{self.bid}/dispatch",
                                  {"annotator_id": self.a1, "ordinals": "not-a-list"})
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
