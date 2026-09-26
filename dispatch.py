"""批量分派的业务判定层。

职责：上限校验、名额计算、跳过原因、超额整体不写入、退回后名额重算。
本层不直接操作 SQL，所有读写通过 CorpusDB 的存储方法完成。
"""

from __future__ import annotations

from database import CorpusDB, DomainError


def _unique_in_order(values: list[int]) -> list[int]:
    seen: set[int] = set()
    result = []
    for value in values:
        if value not in seen:
            seen.add(value)
            result.append(value)
    return result


class DispatchService:
    def __init__(self, db: CorpusDB) -> None:
        self.db = db

    def _require_open_batch(self, batch_id: int) -> str:
        status = self.db.get_batch_status(batch_id)
        if status is None:
            raise DomainError("批次不存在")
        if status == "frozen":
            raise DomainError("批次已冻结，不能继续分派")
        return status

    def _require_annotator(self, annotator_id: int) -> None:
        role = self.db.get_annotator_role(annotator_id)
        if role is None:
            raise DomainError("用户不存在")
        if role != "annotator":
            raise DomainError("只能分派给标注员")

    def remaining_quota(self, batch_id: int, annotator_id: int) -> int | None:
        cap = self.db.get_assignment_cap(batch_id, annotator_id)
        if cap is None:
            return None
        return max(0, cap - self.db.count_unsubmitted(batch_id, annotator_id))

    def set_cap(self, batch_id: int, annotator_id: int, cap: int) -> dict:
        """按批次设置某位标注员的“未提交”任务上限。"""
        self._require_open_batch(batch_id)
        self._require_annotator(annotator_id)
        if not isinstance(cap, int) or isinstance(cap, bool) or cap < 0:
            raise DomainError("上限必须是不小于0的整数")
        self.db.set_assignment_cap(batch_id, annotator_id, cap)
        return {
            "batch_id": batch_id,
            "annotator_id": annotator_id,
            "cap": cap,
            "remaining_quota": self.remaining_quota(batch_id, annotator_id),
        }

    def dispatch(self, batch_id: int, annotator_id: int, ordinals: list[int]) -> dict:
        """一次向某位标注员分派若干条目序号。

        - 已分给本人或他人的条目跳过并给出原因；
        - 待新增数超过剩余名额时整体不写入（全或无）；
        - 返回成功项、跳过原因和剩余名额。
        """
        self._require_open_batch(batch_id)
        self._require_annotator(annotator_id)
        if not isinstance(ordinals, list) or not ordinals:
            raise DomainError("条目序号不能为空")
        normalized: list[int] = []
        for ordinal in ordinals:
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
                raise DomainError("条目序号必须是大于0的整数")
            normalized.append(ordinal)
        ordered = _unique_in_order(normalized)

        cap = self.db.get_assignment_cap(batch_id, annotator_id)
        if cap is None:
            raise DomainError("请先为该标注员设置本批次的未提交上限")

        items = self.db.items_by_ordinal(batch_id, ordered)
        owners = self.db.assigned_annotators_for_items([row["id"] for row in items.values()])
        remaining = cap - self.db.count_unsubmitted(batch_id, annotator_id)

        skips: dict[int, str] = {}
        candidates: list[tuple[int, int]] = []
        for ordinal in ordered:
            row = items.get(ordinal)
            if row is None:
                skips[ordinal] = "条目不存在或不属于该批次"
                continue
            holders = owners.get(row["id"], [])
            if annotator_id in holders:
                skips[ordinal] = "已分给本人，跳过"
            elif holders:
                skips[ordinal] = "已分给其他标注员，跳过"
            else:
                candidates.append((ordinal, row["id"]))

        assigned: list[int] = []
        if len(candidates) > remaining:
            # 新增数超过剩余名额：一条都不写入
            for ordinal, _ in candidates:
                skips[ordinal] = f"剩余名额不足（剩余 {max(0, remaining)} 个，待新增 {len(candidates)} 条），未写入"
        elif candidates:
            try:
                self.db.insert_assignments(batch_id, annotator_id, [item_id for _, item_id in candidates])
            except DomainError as exc:
                for ordinal, _ in candidates:
                    skips[ordinal] = f"{exc}，未写入"
            else:
                assigned = [ordinal for ordinal, _ in candidates]
                remaining -= len(assigned)

        return {
            "batch_id": batch_id,
            "annotator_id": annotator_id,
            "cap": cap,
            "assigned": assigned,
            "skipped": [{"ordinal": ordinal, "reason": skips[ordinal]} for ordinal in ordered if ordinal in skips],
            "remaining_quota": max(0, remaining),
        }

    def return_items(self, batch_id: int, annotator_id: int, ordinals: list[int]) -> dict:
        """退回未提交任务：只删除 status='assigned' 的分派，已提交记录保持不变。"""
        self._require_open_batch(batch_id)
        self._require_annotator(annotator_id)
        if not isinstance(ordinals, list) or not ordinals:
            raise DomainError("条目序号不能为空")
        for ordinal in ordinals:
            if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
                raise DomainError("条目序号必须是大于0的整数")
        ordered = _unique_in_order(ordinals)

        items = self.db.items_by_ordinal(batch_id, ordered)
        states = self.db.assignment_states(
            batch_id, annotator_id, [row["id"] for row in items.values()]
        )
        skips: dict[int, str] = {}
        candidates: list[tuple[int, int]] = []
        for ordinal in ordered:
            row = items.get(ordinal)
            if row is None:
                skips[ordinal] = "条目不存在或不属于该批次"
                continue
            state = states.get(row["id"])
            if state is None:
                skips[ordinal] = "未分派给该标注员，跳过"
            elif state == "submitted":
                skips[ordinal] = "已提交的记录不能退回，保持不变"
            else:
                candidates.append((ordinal, row["id"]))

        returned: list[int] = []
        if candidates:
            self.db.delete_assignments(batch_id, annotator_id, [item_id for _, item_id in candidates])
            survivors = self.db.assignment_states(
                batch_id, annotator_id, [item_id for _, item_id in candidates]
            )
            for ordinal, item_id in candidates:
                if item_id in survivors:
                    skips[ordinal] = "退回时任务已提交或状态变化，未退回"
                else:
                    returned.append(ordinal)

        return {
            "batch_id": batch_id,
            "annotator_id": annotator_id,
            "returned": returned,
            "skipped": [{"ordinal": ordinal, "reason": skips[ordinal]} for ordinal in ordered if ordinal in skips],
            "remaining_quota": self.remaining_quota(batch_id, annotator_id),
        }
