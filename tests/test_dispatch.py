import json
import os
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))

import app as app_module
from database import CorpusDB, DomainError
from dispatch import DispatchService


class DispatchTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = CorpusDB(self.path)
        self.svc = DispatchService(self.db)
        self.a1 = self.db.add_user("甲", "annotator")
        self.a2 = self.db.add_user("乙", "annotator")
        self.arb = self.db.add_user("仲裁", "arbitrator")
        self.mgr = self.db.add_user("管理", "manager")
        self.g = self.db.add_guideline("v1", "独立标注")
        self.batch = self.db.create_batch("测试批次", self.g)
        self.ids = [self.db.add_item(self.batch, i, f"文本{i}") for i in range(1, 6)]
        self.db.set_assignment_cap(self.batch, self.a1, 3)

    def tearDown(self):
        self.db.close()
        os.unlink(self.path)

    def test_set_cap_requires_annotator_and_open_batch(self):
        with self.assertRaisesRegex(DomainError, "标注员"):
            self.svc.set_cap(self.batch, self.arb, 3)
        with self.assertRaisesRegex(DomainError, "上限"):
            self.svc.set_cap(self.batch, self.a1, -1)
        with self.assertRaisesRegex(DomainError, "批次不存在"):
            self.svc.set_cap(999, self.a1, 3)

    def test_bulk_dispatch_success_skip_and_quota(self):
        result = self.svc.dispatch(self.batch, self.a1, [1, 2, 3])
        self.assertEqual([1, 2, 3], result["assigned"])
        self.assertEqual([], result["skipped"])
        self.assertEqual(0, result["remaining_quota"])

        # 已分给本人 / 他人跳过；序号去重；不存在的序号跳过
        self.db.assign(self.ids[3], self.a2)
        again = self.svc.dispatch(self.batch, self.a1, [1, 4, 5, 5, 9])
        self.assertEqual([], again["assigned"])
        reasons = {s["ordinal"]: s["reason"] for s in again["skipped"]}
        self.assertIn("本人", reasons[1])
        self.assertIn("其他标注员", reasons[4])
        self.assertIn("不存在", reasons[9])
        self.assertEqual(0, again["remaining_quota"])

    def test_over_quota_writes_nothing(self):
        result = self.svc.dispatch(self.batch, self.a1, [1, 2])
        self.assertEqual(2, len(result["assigned"]))
        # 只剩 1 个名额，试图再领 3 条：整体不写入
        over = self.svc.dispatch(self.batch, self.a1, [3, 4, 5])
        self.assertEqual([], over["assigned"])
        self.assertEqual(3, len(over["skipped"]))
        self.assertTrue(all("剩余名额不足" in s["reason"] for s in over["skipped"]))
        self.assertEqual(1, over["remaining_quota"])
        # 4、5 没有被写入，仍可分给其他标注员
        self.db.set_assignment_cap(self.batch, self.a2, 5)
        other = self.svc.dispatch(self.batch, self.a2, [4, 5])
        self.assertEqual([4, 5], other["assigned"])

    def test_dispatch_without_cap_rejected(self):
        with self.assertRaisesRegex(DomainError, "上限"):
            self.svc.dispatch(self.batch, self.a2, [1])

    def test_submit_frees_no_quota_and_return_recalculates(self):
        result = self.svc.dispatch(self.batch, self.a1, [1, 2, 3])
        self.assertEqual(0, result["remaining_quota"])
        # 提交后占用名额释放（提交记录不再计入未提交上限）
        self.db.submit_annotation(self.ids[0], self.a1, "正向")
        self.assertEqual(1, self.svc.remaining_quota(self.batch, self.a1))
        # 已提交的不能再分派给本人（跳过），但名额允许再领新条目
        more = self.svc.dispatch(self.batch, self.a1, [1, 4])
        self.assertEqual([4], more["assigned"])
        self.assertEqual(0, more["remaining_quota"])

        # 退回未提交任务：名额重算；已提交记录保持不变
        back = self.svc.return_items(self.batch, self.a1, [1, 4])
        self.assertEqual([4], back["returned"])
        self.assertIn("已提交", back["skipped"][0]["reason"])
        self.assertEqual(1, back["remaining_quota"])
        # 已提交的标注仍然存在
        own = self.db.get_item_for_user(self.ids[0], self.a1)["own_annotation"]
        self.assertEqual("正向", own["label"])
        # 退回后可以重新分派
        reassign = self.svc.dispatch(self.batch, self.a1, [4])
        self.assertEqual([4], reassign["assigned"])

    def test_frozen_batch_blocks_cap_dispatch_and_return(self):
        frozen_batch = self.db.create_batch("待冻结批次", self.g)
        f1 = self.db.add_item(frozen_batch, 1, "文本")
        f2 = self.db.add_item(frozen_batch, 2, "文本")
        self.svc.set_cap(frozen_batch, self.a1, 2)
        self.svc.set_cap(frozen_batch, self.a2, 2)
        self.svc.dispatch(frozen_batch, self.a1, [1, 2])
        self.db.assign(f1, self.a2)
        self.db.assign(f2, self.a2)
        self.db.submit_annotation(f1, self.a1, "中性")
        self.db.submit_annotation(f1, self.a2, "中性")
        self.db.submit_annotation(f2, self.a1, "中性")
        self.db.submit_annotation(f2, self.a2, "中性")
        self.db.freeze_batch(frozen_batch, self.mgr)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.svc.set_cap(frozen_batch, self.a1, 9)
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.svc.dispatch(frozen_batch, self.a1, [1])
        with self.assertRaisesRegex(DomainError, "冻结"):
            self.svc.return_items(frozen_batch, self.a1, [1])

    def test_snapshot_exposes_caps_and_assignments(self):
        self.svc.dispatch(self.batch, self.a1, [1])
        snap = self.db.snapshot()
        self.assertEqual(
            [{"batch_id": self.batch, "annotator_id": self.a1, "cap": 3}],
            [{k: c[k] for k in ("batch_id", "annotator_id", "cap")} for c in snap["assignment_caps"]],
        )
        self.assertEqual(1, len(snap["assignments"]))


class DispatchHttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fd, cls.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        cls.db = app_module.CorpusDB(cls.path)

        class TestHandler(app_module.Handler):
            db = cls.db
            dispatch_service = DispatchService(cls.db)

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)
        cls.db.close()
        os.unlink(cls.path)

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

    def test_cap_and_bulk_assign_endpoints(self):
        a = self.db.add_user("接口标注员", "annotator")
        g = self.db.add_guideline("v9", "规则")
        b = self.db.create_batch("接口批次", g)
        self.db.add_item(b, 1, "x")
        self.db.add_item(b, 2, "y")

        status, body = self._post(f"/api/batches/{b}/cap", {"annotator_id": a, "cap": 1})
        self.assertEqual(200, status)
        self.assertEqual(1, body["remaining_quota"])

        status, body = self._post(f"/api/batches/{b}/bulk-assign", {"annotator_id": a, "ordinals": [1, 2]})
        self.assertEqual(200, status)
        self.assertEqual([], body["assigned"])
        self.assertTrue(all("剩余名额不足" in s["reason"] for s in body["skipped"]))

        status, body = self._post(f"/api/batches/{b}/bulk-assign", {"annotator_id": a, "ordinals": [1]})
        self.assertEqual([1], body["assigned"])

        status, body = self._post(f"/api/batches/{b}/return", {"annotator_id": a, "ordinals": [1]})
        self.assertEqual(200, status)
        self.assertEqual([1], body["returned"])

        status, body = self._post(f"/api/batches/999/bulk-assign", {"annotator_id": a, "ordinals": [1]})
        self.assertEqual(400, status)


if __name__ == "__main__":
    unittest.main()
