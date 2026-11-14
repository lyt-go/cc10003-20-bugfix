"""指标口径业务规则：版本化、发布/下线联动、回滚与历史追溯。

口径版本是一次评审的最小单位：

- 提交评审：版本号 +1，基于当前字段登记一条「评审中」版本，评审期间不改变线上版本。
- 发布指标：只有评审中的版本可以发布；发布后旧版本标记「已替代」，
  仍绑定旧版本的预警规则统一冻结（保留连续周期与历史，等新版本重新评审）。
- 下线指标：停用线上版本并冻结该指标下所有未关闭的规则；关闭的规则保持关闭。
- 回滚：按目标版本内容生成一个新的评审版本，不能直接把老版本重新挂回线上，
  必须重新走评审与发布。
"""
from __future__ import annotations

from typing import Any

from app.services import metric_catalog
from app.services.alert_engine import now_iso
from app.services.alertconfig import freeze_alerts_for_metric
from app.services.metric_catalog import (
    METRIC_DRAFT,
    METRIC_OFFLINE,
    METRIC_PUBLISHED,
    METRIC_REVIEW,
    METRIC_STATUSES,
)
from app.store import store

MODULE = "metric"
REQUIRED_FIELDS = ["指标编号", "指标名称", "计算公式"]
EDITABLE_FIELDS = ["指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数"]
ACTION_RULES = {
    "提交口径评审": METRIC_REVIEW,
    "发布指标": METRIC_PUBLISHED,
    "下线指标": METRIC_OFFLINE,
    "回滚版本": METRIC_REVIEW,
}
NEGATIVE_ACTIONS = ["下线指标"]
SORT_FIELDS = {
    "id": lambda row: int(row.get("id", 0)),
    "指标编号": lambda row: str(row.get("指标编号") or ""),
    "指标名称": lambda row: str(row.get("指标名称") or ""),
    "状态": lambda row: METRIC_STATUSES.index(row.get("status")) if row.get("status") in METRIC_STATUSES else 99,
    "版本号": lambda row: int(row.get("版本号") or 0),
}


class MetricError(Exception):
    """带可读说明的业务失败；事务里抛出会整体回滚。"""


def _row_or_raise(entry_id: int) -> dict[str, Any]:
    entry = store.find(MODULE, entry_id)
    if entry is None:
        raise MetricError(f"指标定义 {entry_id} 不存在或已归档")
    return entry


def _set_status(entry: dict[str, Any], status: str) -> None:
    entry["status"] = status
    entry["pending"] = status in (METRIC_DRAFT, METRIC_REVIEW)
    entry["abnormal"] = status == METRIC_OFFLINE


class MetricService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
        sort_by: str = "id",
        order: str = "asc",
    ) -> tuple[list[dict[str, Any]], int]:
        if sort_by not in SORT_FIELDS:
            raise MetricError(f"不支持的排序字段「{sort_by}」")
        if order not in ("asc", "desc"):
            raise MetricError("排序方向只能是 asc 或 desc")
        with store.transaction():
            rows = list(store.rows(MODULE))
            for row in rows:
                metric_catalog.ensure_versions(row)
            if keyword:
                needle = keyword.strip()
                rows = [
                    row
                    for row in rows
                    if needle in str(row.get("指标编号") or "")
                    or needle in str(row.get("指标名称") or "")
                    or needle in str(row.get("口径版本") or "")
                ]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            key = SORT_FIELDS[sort_by]
            rows.sort(key=lambda row: (key(row), int(row.get("id", 0))), reverse=(order == "desc"))
            total = len(rows)
            start = max(page - 1, 0) * size
            page_rows = [metric_catalog.metric_view(row) for row in rows[start:start + size]]
            for offset, row in enumerate(page_rows):
                row["序号"] = start + offset + 1
                row["序列位置"] = f"第 {start + offset + 1} / 共 {total} 条"
            return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            return metric_catalog.metric_view(entry)

    def get_detail(self, entry_id: int) -> dict[str, Any]:
        """指标详情：口径版本、预警规则与各自状态放在同一份视图里，避免列表与详情错位。"""
        from app.services.alertconfig import AlertconfigService

        entry = self.get_entry(entry_id)
        if entry is None:
            raise MetricError(f"指标定义 {entry_id} 不存在或已归档")
        alerts = []
        for row in store.rows("alertconfig"):
            if str(row.get("关联指标ID") or "") == str(entry_id) or str(
                row.get("关联指标") or ""
            ) == str(entry.get("指标编号")):
                snapshot = AlertconfigService().get_entry(int(row["id"]))
                if snapshot is not None:
                    alerts.append(snapshot)
        history = [
            dict(row)
            for row in sorted(
                (row for row in store.rows(metric_catalog.HISTORY_MODULE) if int(row.get("指标ID", -1)) == entry_id),
                key=lambda row: (str(row.get("时间") or ""), int(row.get("id", 0))),
            )
        ]
        return {
            "metric": entry,
            "alerts": alerts,
            "history": history,
            "summary": {
                "规则总数": len(alerts),
                "已生效": sum(1 for row in alerts if row.get("status") == "已生效"),
                "已冻结": sum(1 for row in alerts if row.get("status") == "已冻结"),
                "已关闭": sum(1 for row in alerts if row.get("status") == "已关闭"),
                "指标可用": entry.get("可用"),
            },
        }

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values.get("指标编号") or "").strip()
        if any(str(row.get("指标编号") or "") == code for row in store.rows(MODULE)):
            return None, [f"指标编号 {code} 已存在"]
        with store.transaction():
            entry = {
                "id": store.next_id(MODULE),
                "status": METRIC_DRAFT,
                "pending": True,
                "abnormal": False,
                "指标编号": code,
                "指标名称": values.get("指标名称"),
                "计算公式": values.get("计算公式"),
                "统计粒度": values.get("统计粒度") or "自然日",
                "时间窗口": values.get("时间窗口") or "最近 1 天",
                "筛选条件": values.get("筛选条件") or "全部业务线",
                "精度位数": values.get("精度位数") or 2,
                "口径版本": f"{code}-v1",
                "版本号": 1,
                "已发布版本": None,
                "发布状态": "未发布",
                "已发布时间": None,
                "下线时间": None,
                "下线原因": None,
                "提交时间": now_iso(),
                "评审人": values.get("评审人"),
            }
            store.rows(MODULE).append(entry)
            metric_catalog.ensure_versions(entry)
            metric_catalog.write_metric_history(
                entry,
                event="登记指标",
                before="无",
                after=METRIC_DRAFT,
                version=entry["口径版本"],
                operator=str(values.get("操作人") or "数据值班员"),
                note="登记指标定义",
                at=now_iso(),
            )
            return metric_catalog.metric_view(entry), []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        operator: str = "",
    ) -> dict[str, Any]:
        """只有草稿可以改口径内容；已提交评审或已发布的指标必须走回滚/新版本。"""
        with store.transaction():
            entry = _row_or_raise(entry_id)
            if entry.get("status") != METRIC_DRAFT:
                raise MetricError(
                    f"指标当前为「{entry.get('status')}」，口径内容不能直接修改，请提交评审或新建版本"
                )
            updates = {
                field: values[field]
                for field in EDITABLE_FIELDS
                if field in values and values[field] is not None
            }
            if not updates:
                raise MetricError("没有可修改的字段")
            entry.update(updates)
            metric_catalog.write_metric_history(
                entry,
                event="修改指标",
                before=METRIC_DRAFT,
                after=METRIC_DRAFT,
                version=entry.get("口径版本"),
                operator=operator or "数据值班员",
                note=f"草稿字段变更：{'、'.join(updates)}",
                at=now_iso(),
            )
            return metric_catalog.metric_view(entry)

    def run_action(
        self,
        entry_id: int,
        action: str,
        *,
        values: dict[str, Any] | None = None,
        operator: str = "",
    ) -> tuple[dict[str, Any] | None, str]:
        values = values or {}
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于指标口径可执行范围"
        try:
            with store.transaction():
                entry = _row_or_raise(entry_id)
                expected = values.get("expected_status")
                if expected is not None and str(expected) != str(entry.get("status")):
                    raise MetricError(
                        f"指标已被其他人处理，当前状态为「{entry.get('status')}」，请刷新后重试"
                    )
                message = self._apply_action(entry, action, values, operator)
                return metric_catalog.metric_view(entry), message
        except MetricError as error:
            return None, str(error)
        except Exception as error:  # 事务已回滚，按接口契约返回可读失败
            return None, f"指标动作失败，数据已回滚：{error}"

    def _apply_action(
        self,
        entry: dict[str, Any],
        action: str,
        values: dict[str, Any],
        operator: str,
    ) -> str:
        if action == "提交口径评审":
            return self._submit_review(entry, values, operator)
        if action == "发布指标":
            return self._publish(entry, values, operator)
        if action == "下线指标":
            return self._offline(entry, values, operator)
        if action == "回滚版本":
            return self._rollback(entry, values, operator)
        raise MetricError(f"动作「{action}」不属于指标口径可执行范围")

    def _submit_review(self, entry: dict[str, Any], values: dict[str, Any], operator: str) -> str:
        current = str(entry.get("status") or METRIC_DRAFT)
        if current == METRIC_OFFLINE:
            raise MetricError("指标已下线，恢复必须使用回滚版本生成新的评审版本")
        if current == METRIC_REVIEW:
            raise MetricError("指标已有版本在评审中，不能重复提交")
        if current not in (METRIC_DRAFT, METRIC_PUBLISHED):
            raise MetricError(f"指标当前为「{current}」，不能提交评审")
        for field in EDITABLE_FIELDS:
            if field in values and values[field] is not None:
                entry[field] = values[field]
        version_no = int(entry.get("版本号") or 1) + 1
        entry["版本号"] = version_no
        entry["口径版本"] = f"{entry.get('指标编号')}-v{version_no}"
        entry["提交时间"] = now_iso()
        entry["评审人"] = operator or entry.get("评审人")
        _set_status(entry, METRIC_REVIEW)
        metric_catalog.record_review_version(
            entry,
            version_no,
            note=str(values.get("备注") or "提交口径评审"),
            operator=operator,
            submitted_at=entry["提交时间"],
        )
        metric_catalog.write_metric_history(
            entry,
            event="提交口径评审",
            before=current,
            after=METRIC_REVIEW,
            version=entry["口径版本"],
            operator=operator,
            note="口径内容进入评审，评审期间线上版本不变",
            at=now_iso(),
        )
        return f"指标已提交口径评审，待发布版本 {entry['口径版本']}"

    def _publish(self, entry: dict[str, Any], values: dict[str, Any], operator: str) -> str:
        current = str(entry.get("status") or "")
        if current != METRIC_REVIEW:
            raise MetricError(f"只有口径评审中的指标可以发布，当前状态为「{current}」")
        version = str(entry.get("口径版本") or "")
        metric_catalog.ensure_versions(entry)
        state = metric_catalog.version_state(entry, version)
        if state != metric_catalog.REVIEW_STATE:
            raise MetricError(f"版本 {version} 状态为「{state}」，不能发布")
        reviewer = str(values.get("评审人") or operator or "").strip()
        if not reviewer:
            raise MetricError("发布指标需要填写评审人，评审通过后才能发布")
        previous = metric_catalog.live_version(entry)
        at = now_iso()
        entry["已发布版本"] = version
        entry["发布状态"] = metric_catalog.PUBLISHED_STATE
        entry["已发布时间"] = at
        entry["评审人"] = reviewer
        entry["下线时间"] = None
        entry["下线原因"] = None
        _set_status(entry, METRIC_PUBLISHED)
        metric_catalog.mark_version_state(entry, version, metric_catalog.PUBLISHED_STATE, at=at)
        if previous and previous != version:
            metric_catalog.mark_version_state(entry, previous, metric_catalog.SUPERSEDED_STATE, at=at)
        metric_catalog.write_metric_history(
            entry,
            event="发布指标",
            before=current,
            after=METRIC_PUBLISHED,
            version=version,
            operator=reviewer,
            note=f"口径版本 {version} 发布" + (f"，替代 {previous}" if previous else ""),
            at=at,
            extra={"上一版本": previous},
        )
        frozen = 0
        if previous and previous != version:
            frozen = freeze_alerts_for_metric(
                entry,
                reason=f"指标已发布新版本 {version}，原绑定版本 {previous} 已替代",
                operator=operator or reviewer,
            )
        return (
            f"指标已发布版本 {version}"
            + (f"，冻结 {frozen} 条绑定旧版本的预警规则" if frozen else "")
        )

    def _offline(self, entry: dict[str, Any], values: dict[str, Any], operator: str) -> str:
        current = str(entry.get("status") or "")
        if current == METRIC_OFFLINE:
            raise MetricError("指标已下线，不能重复下线")
        live = metric_catalog.live_version(entry)
        if live is None:
            raise MetricError("指标没有已发布的版本，无需下线")
        reason = str(values.get("下线原因") or "指标口径下线").strip()
        at = now_iso()
        entry["发布状态"] = metric_catalog.OFFLINE_STATE
        entry["下线时间"] = at
        entry["下线原因"] = reason
        _set_status(entry, METRIC_OFFLINE)
        metric_catalog.mark_version_state(entry, live, metric_catalog.OFFLINE_STATE, at=at)
        # 评审中的版本一并中止：恢复必须重新生成评审版本。
        for row in metric_catalog.version_rows(int(entry["id"])):
            if row.get("状态") == metric_catalog.REVIEW_STATE:
                row["状态"] = metric_catalog.ABORTED_STATE
                row["备注"] = (row.get("备注") or "") + " · 指标下线，评审中止"
        metric_catalog.write_metric_history(
            entry,
            event="下线指标",
            before=current,
            after=METRIC_OFFLINE,
            version=live,
            operator=operator,
            note=reason,
            at=at,
        )
        frozen = freeze_alerts_for_metric(
            entry,
            reason=f"指标已下线（{reason}），规则冻结并保留关闭前历史",
            operator=operator,
        )
        return f"指标已下线，冻结 {frozen} 条预警规则"

    def _rollback(self, entry: dict[str, Any], values: dict[str, Any], operator: str) -> str:
        target = str(values.get("目标版本") or values.get("指标版本") or "").strip()
        if not target:
            raise MetricError("回滚版本需要指定目标版本")
        snapshot = metric_catalog.version_snapshot(entry, target)
        if snapshot is None:
            raise MetricError(f"指标版本 {target} 不存在，不能回滚")
        current = str(entry.get("status") or "")
        if current == METRIC_REVIEW:
            raise MetricError("指标已有版本在评审中，不能同时发起回滚")
        if current not in (METRIC_PUBLISHED, METRIC_OFFLINE):
            raise MetricError(f"指标当前为「{current}」，只有已发布或已下线的指标可以回滚")
        version_no = int(entry.get("版本号") or 1) + 1
        at = now_iso()
        for field in ("计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数"):
            if snapshot.get(field) is not None:
                entry[field] = snapshot[field]
        entry["版本号"] = version_no
        entry["口径版本"] = f"{entry.get('指标编号')}-v{version_no}"
        entry["提交时间"] = at
        entry["评审人"] = operator or entry.get("评审人")
        entry["下线原因"] = None if current == METRIC_OFFLINE else entry.get("下线原因")
        if current == METRIC_OFFLINE:
            entry["发布状态"] = "未发布"
        _set_status(entry, METRIC_REVIEW)
        metric_catalog.record_review_version(
            entry,
            version_no,
            note=f"按版本 {target} 回滚生成新的评审版本",
            rollback_from=target,
            operator=operator,
            submitted_at=at,
        )
        metric_catalog.write_metric_history(
            entry,
            event="回滚版本",
            before=current,
            after=METRIC_REVIEW,
            version=entry["口径版本"],
            operator=operator,
            note=f"按 {target} 回滚生成评审版本 {entry['口径版本']}，需重新评审发布",
            at=at,
            extra={"回滚自版本": target},
        )
        return (
            f"已按版本 {target} 生成新的评审版本 {entry['口径版本']}，"
            "评审发布后预警规则需重新评审绑定"
        )
