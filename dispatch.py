"""批量分派的业务判定层。

只做规则判定并编排 CorpusDB 的原子存储方法，本身不执行 SQL，
也不了解 HTTP；HTTP 路由在 app.py，落库在 database.py。
"""

from __future__ import annotations

from database import CorpusDB, DomainError


def _as_positive_int(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise DomainError("序号必须是大于 0 的整数")
    return value


class Dispatcher:
    def __init__(self, db: CorpusDB) -> None:
        self.db = db

    def set_cap(self, batch_id: int, annotator_id: int, cap: int) -> dict:
        """按批次设置每位标注员的未提交上限；cap=0 表示取消上限。"""
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise DomainError("未提交上限必须是不小于 0 的整数")
        ctx = self.db.dispatch_context(batch_id, annotator_id)
        if not ctx["batch"]:
            raise DomainError("批次不存在")
        if ctx["batch"]["status"] == "frozen":
            raise DomainError("冻结批次不能继续分派")
        if ctx["user_role"] != "annotator":
            raise DomainError("只能为标注员设置分派名额")
        self.db.set_dispatch_cap(batch_id, annotator_id, cap)
        if cap == 0:
            return {"batch_id": batch_id, "annotator_id": annotator_id, "cap": None,
                    "remaining": None, "cleared": True}
        return {"batch_id": batch_id, "annotator_id": annotator_id, "cap": cap,
                "remaining": max(0, cap - ctx["open_count"])}

    def assign_ordinals(self, batch_id: int, annotator_id: int, ordinals: list) -> dict:
        """一次提交标注员和若干条目序号，返回成功项、跳过原因和剩余名额。

        已分给本人或他人的条目跳过；新增数超过剩余名额时整体不写入。
        """
        if not isinstance(ordinals, list) or not ordinals:
            raise DomainError("至少提交一个条目序号")

        ctx = self.db.dispatch_context(batch_id, annotator_id)
        if not ctx["batch"]:
            raise DomainError("批次不存在")
        if ctx["batch"]["status"] == "frozen":
            raise DomainError("冻结批次不能继续分派")
        if ctx["user_role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        if ctx["cap"] is None:
            raise DomainError("请先为该标注员设置本批次的未提交上限")

        items, holders = ctx["items"], ctx["holders"]
        successes: list[dict] = []
        skipped: list[dict] = []
        seen: set[int] = set()
        for raw in ordinals:
            try:
                ordinal = _as_positive_int(raw)
            except DomainError:
                skipped.append({"ordinal": raw, "reason": "序号必须是大于0的整数"})
                continue
            if ordinal in seen:
                skipped.append({"ordinal": ordinal, "reason": "请求内序号重复"})
                continue
            seen.add(ordinal)
            item_id = items.get(ordinal)
            if item_id is None:
                skipped.append({"ordinal": ordinal, "reason": "条目不在该批次中"})
            elif annotator_id in holders.get(item_id, set()):
                skipped.append({"ordinal": ordinal, "reason": "已分给本人"})
            elif holders.get(item_id):
                skipped.append({"ordinal": ordinal, "reason": "已分给他人"})
            else:
                successes.append({"ordinal": ordinal, "item_id": item_id})

        remaining = max(0, ctx["cap"] - ctx["open_count"])
        if len(successes) > remaining:
            for entry in successes:
                entry["reason"] = "超出剩余名额，本次未写入"
            skipped.extend(successes)
            successes = []
        else:
            if successes:
                self.db.bulk_insert_assignments(batch_id, annotator_id, [e["item_id"] for e in successes])
            remaining = max(0, ctx["cap"] - ctx["open_count"] - len(successes))

        return {
            "batch_id": batch_id,
            "annotator_id": annotator_id,
            "cap": ctx["cap"],
            "assigned": successes,
            "skipped": skipped,
            "written": len(successes),
            "remaining": remaining,
        }

    def release(self, batch_id: int, annotator_id: int, ordinal: int) -> dict:
        """退回未提交任务：删除分派并释放名额；已提交记录不变。"""
        ordinal = _as_positive_int(ordinal)
        ctx = self.db.dispatch_context(batch_id, annotator_id)
        if not ctx["batch"]:
            raise DomainError("批次不存在")
        if ctx["batch"]["status"] == "frozen":
            raise DomainError("冻结批次不能继续分派")
        if ctx["user_role"] != "annotator":
            raise DomainError("条目不存在或用户不是标注员")
        if ctx["items"].get(ordinal) is None:
            raise DomainError("条目不在该批次中")
        assignment = self.db.get_open_assignment(batch_id, ordinal, annotator_id)
        if not assignment:
            raise DomainError("该条目未分派给此标注员")
        if assignment["status"] == "submitted":
            raise DomainError("已提交任务不能退回")
        self.db.delete_assignment(assignment["id"])
        remaining = None
        if ctx["cap"] is not None:
            remaining = max(0, ctx["cap"] - ctx["open_count"] + 1)
        return {
            "batch_id": batch_id,
            "annotator_id": annotator_id,
            "ordinal": ordinal,
            "returned": True,
            "remaining": remaining,
        }
