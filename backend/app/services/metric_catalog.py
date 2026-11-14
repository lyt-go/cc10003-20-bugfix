"""指标口径的受控词表：解析指标、读取已发布版本、登记版本履历。

指标预警服务只依赖这个模块，不反向依赖 MetricService，跨模块联动就不会形成环。
"""
from __future__ import annotations

from typing import Any

from app.store import store

METRIC_MODULE = "metric"
VERSION_MODULE = "metric_version"
HISTORY_MODULE = "metric_history"

PUBLISHED_STATE = "已发布"
OFFLINE_STATE = "已下线"
REVIEW_STATE = "评审中"
DRAFT_STATE = "草稿"
SUPERSEDED_STATE = "已替代"
ABORTED_STATE = "评审中止"

METRIC_DRAFT = "草稿"
METRIC_REVIEW = "口径评审中"
METRIC_PUBLISHED = "已发布"
METRIC_OFFLINE = "已下线"
METRIC_STATUSES = [METRIC_DRAFT, METRIC_REVIEW, METRIC_PUBLISHED, METRIC_OFFLINE]


def find_metric(ref: Any) -> dict[str, Any] | None:
    """按 id、指标编号或指标名称定位指标；找不到返回 None。"""
    if ref is None or isinstance(ref, bool):
        return None
    text = str(ref).strip()
    if not text:
        return None
    if text.isdigit():
        row = store.find(METRIC_MODULE, int(text))
        if row is not None:
            return row
    for row in store.rows(METRIC_MODULE):
        if text in (str(row.get("指标编号") or ""), str(row.get("指标名称") or "")):
            return row
    return None


def live_version(metric: dict[str, Any] | None) -> str | None:
    """当前真正在线的口径版本；未发布或已下线时返回 None。"""
    if metric is None:
        return None
    if metric.get("发布状态") != PUBLISHED_STATE:
        return None
    return metric.get("已发布版本")


def metric_state(metric: dict[str, Any] | None) -> str:
    if metric is None:
        return "指标不存在"
    if live_version(metric):
        return PUBLISHED_STATE
    if metric.get("发布状态") == OFFLINE_STATE:
        return OFFLINE_STATE
    status = str(metric.get("status") or METRIC_DRAFT)
    return status if status in METRIC_STATUSES else METRIC_DRAFT


def version_rows(metric_id: int) -> list[dict[str, Any]]:
    return [row for row in store.rows(VERSION_MODULE) if int(row.get("指标ID", -1)) == metric_id]


def version_state(metric: dict[str, Any] | None, version: str | None) -> str:
    if metric is None or not version:
        return "未绑定"
    ensure_versions(metric)
    for row in version_rows(int(metric["id"])):
        if row.get("口径版本") == version:
            return str(row.get("状态") or "未知")
    return "版本不存在"


def ensure_versions(metric: dict[str, Any]) -> list[dict[str, Any]]:
    """老数据没有版本履历表时，按指标当前字段补一条，保证追溯链不断。"""
    rows = version_rows(int(metric["id"]))
    if rows:
        return rows
    status = str(metric.get("status") or METRIC_DRAFT)
    state = {
        METRIC_DRAFT: DRAFT_STATE,
        METRIC_REVIEW: REVIEW_STATE,
        METRIC_PUBLISHED: PUBLISHED_STATE,
        METRIC_OFFLINE: OFFLINE_STATE,
    }.get(status, DRAFT_STATE)
    record = _base_record(metric, int(metric.get("版本号") or 1), state)
    record["发布时间"] = metric.get("已发布时间")
    if state == OFFLINE_STATE:
        record["下线时间"] = metric.get("下线时间")
    store.insert(VERSION_MODULE, record)
    return [record]


def _base_record(metric: dict[str, Any], version_no: int, state: str, note: str = "") -> dict[str, Any]:
    code = str(metric.get("指标编号") or "")
    return {
        "指标ID": int(metric["id"]),
        "指标编号": code,
        "指标名称": metric.get("指标名称"),
        "版本号": version_no,
        "口径版本": f"{code}-v{version_no}",
        "计算公式": metric.get("计算公式"),
        "统计粒度": metric.get("统计粒度"),
        "时间窗口": metric.get("时间窗口"),
        "筛选条件": metric.get("筛选条件"),
        "精度位数": metric.get("精度位数"),
        "状态": state,
        "提交时间": metric.get("提交时间"),
        "发布时间": None,
        "下线时间": None,
        "评审人": metric.get("评审人"),
        "备注": note,
        "回滚自版本": None,
        "归档内容": {
            "计算公式": metric.get("计算公式"),
            "统计粒度": metric.get("统计粒度"),
            "时间窗口": metric.get("时间窗口"),
            "筛选条件": metric.get("筛选条件"),
            "精度位数": metric.get("精度位数"),
        },
    }


def record_review_version(
    metric: dict[str, Any],
    version_no: int,
    *,
    note: str = "",
    rollback_from: str | None = None,
    operator: str = "",
    submitted_at: str | None = None,
) -> dict[str, Any]:
    record = _base_record(metric, version_no, REVIEW_STATE, note)
    record["口径版本"] = f"{metric.get('指标编号')}-v{version_no}"
    record["提交时间"] = submitted_at
    record["评审人"] = operator
    record["回滚自版本"] = rollback_from
    store.insert(VERSION_MODULE, record)
    return record


def mark_version_state(
    metric: dict[str, Any],
    version: str,
    state: str,
    *,
    at: str | None = None,
) -> dict[str, Any] | None:
    ensure_versions(metric)
    for row in version_rows(int(metric["id"])):
        if row.get("口径版本") == version:
            row["状态"] = state
            if state == PUBLISHED_STATE:
                row["发布时间"] = at
            elif state == OFFLINE_STATE:
                row["下线时间"] = at
            elif state == ABORTED_STATE:
                row["备注"] = (row.get("备注") or "") + " · 评审中止"
            return row
    return None


def version_snapshot(metric: dict[str, Any], version: str | None) -> dict[str, Any] | None:
    """读取指定版本的归档内容，回滚时按它复制出新版本。"""
    for row in ensure_versions(metric):
        if row.get("口径版本") == version:
            snapshot = dict(row.get("归档内容") or {})
            snapshot.setdefault("计算公式", row.get("计算公式"))
            snapshot.setdefault("统计粒度", row.get("统计粒度"))
            snapshot.setdefault("时间窗口", row.get("时间窗口"))
            snapshot.setdefault("筛选条件", row.get("筛选条件"))
            snapshot.setdefault("精度位数", row.get("精度位数"))
            snapshot["口径版本"] = row.get("口径版本")
            snapshot["版本号"] = row.get("版本号")
            snapshot["状态"] = row.get("状态")
            return snapshot
    return None


def metric_view(metric: dict[str, Any]) -> dict[str, Any]:
    """指标对外展示字段：状态、发布状态、已发布版本与版本追溯保持一致。"""
    ensure_versions(metric)
    view = dict(metric)
    view["指标当前状态"] = metric_state(metric)
    view["发布状态"] = metric.get("发布状态") or (
        PUBLISHED_STATE if metric.get("status") == METRIC_PUBLISHED else "未发布"
    )
    view["当前版本"] = metric.get("口径版本")
    view["已发布版本"] = metric.get("已发布版本")
    view["评审中版本"] = (
        metric.get("口径版本") if metric.get("status") == METRIC_REVIEW else None
    )
    if metric.get("status") == METRIC_REVIEW and live_version(metric):
        view["指标当前状态"] = f"{PUBLISHED_STATE}（新版本评审中）"
    view["可用"] = live_version(metric) is not None
    view["版本追溯"] = [
        {
            "版本号": row.get("版本号"),
            "口径版本": row.get("口径版本"),
            "状态": row.get("状态"),
            "提交时间": row.get("提交时间"),
            "发布时间": row.get("发布时间"),
            "下线时间": row.get("下线时间"),
            "回滚自版本": row.get("回滚自版本"),
            "备注": row.get("备注"),
        }
        for row in ensure_versions(metric)
    ]
    return view


def write_metric_history(
    metric: dict[str, Any],
    *,
    event: str,
    before: str,
    after: str,
    version: str | None,
    operator: str,
    note: str = "",
    at: str,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = {
        "指标ID": int(metric["id"]),
        "指标编号": metric.get("指标编号"),
        "事件": event,
        "动作": event,
        "前状态": before,
        "后状态": after,
        "指标版本": version,
        "操作人": operator,
        "说明": note,
        "时间": at,
    }
    if extra:
        row.update(extra)
    return store.insert(HISTORY_MODULE, row)


def metric_brief(metric: dict[str, Any] | None) -> dict[str, Any]:
    if metric is None:
        return {"指标ID": None, "指标编号": None, "指标名称": None, "指标状态": "指标不存在"}
    return {
        "指标ID": metric.get("id"),
        "指标编号": metric.get("指标编号"),
        "指标名称": metric.get("指标名称"),
        "指标状态": metric_state(metric),
        "发布状态": metric.get("发布状态"),
        "当前版本": metric.get("口径版本"),
        "已发布版本": metric.get("已发布版本"),
        "可用": live_version(metric) is not None,
    }
