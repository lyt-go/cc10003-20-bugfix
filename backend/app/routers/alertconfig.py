"""指标预警接口：规则评审、生效、关闭、触发累计与口径版本追溯。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.alert_engine import ALERT_STATUSES
from app.services.alertconfig import AlertError, AlertconfigService

router = APIRouter(prefix="/api/alertconfig", tags=["指标预警"])

service = AlertconfigService()

LIST_FIELDS = ["预警编号", "关联指标", "比较运算符", "阈值数值", "持续周期数", "预警级别", "通知接收人", "静默时段"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按预警编号、关联指标、指标版本或接收人检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已冻结、已关闭"),
    metric_version: str | None = Query(default=None, description="只看绑定某个指标版本的规则"),
    sort_by: str = Query(default="id", description="排序字段：id、预警编号、关联指标、关联指标版本、状态、连续周期数、阈值数值、关闭时间"),
    order: str = Query(default="asc", description="排序方向：asc 或 desc"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按编号、状态与指标版本过滤预警列表；每条记录都带稳定序号，翻页后仍能定位同一版本。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if page < 1:
        raise HTTPException(status_code=400, detail="页码从 1 开始")
    if status and status not in ALERT_STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在允许范围内")
    try:
        items, total = service.list_entries(
            keyword=keyword,
            status=status,
            metric_version=metric_version,
            sort_by=sort_by,
            order=order,
            page=page,
            size=size,
        )
    except AlertError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(
    status: str | None = Query(default=None),
    metric_version: str | None = Query(default=None),
) -> dict[str, Any]:
    """导出指标预警清单：返回当前过滤条件下的全量数据。"""
    try:
        items, total = service.list_entries(status=status, metric_version=metric_version, page=1, size=10000)
    except AlertError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"module": "alertconfig", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预警规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预警规则 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/trace", response_model=dict)
def get_trace(entry_id: int) -> dict:
    """预警追溯：规则状态、绑定的指标版本、版本状态、历史与通知记录互相对齐。"""
    try:
        return service.get_trace(entry_id)
    except AlertError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/{entry_id}/history", response_model=list[dict])
def get_history(entry_id: int) -> list[dict]:
    """规则历史：登记、评审、生效、冻结、关闭与逐周期累计明细。"""
    try:
        return service.get_history(entry_id)
    except AlertError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.get("/{entry_id}/notifications", response_model=list[dict])
def get_notifications(entry_id: int) -> list[dict]:
    """规则通知记录：关闭之后不会再有新的通知。"""
    try:
        return service.get_notifications(entry_id)
    except AlertError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预警规则，缺字段或阈值非法时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if entry is None:
        return ActionResult(ok=False, message=f"预警规则未登记：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预警规则已登记（草稿），需提交评审后才能生效", entry=entry)


@router.post("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改草稿/评审中规则的阈值、比较运算符等字段；并发修改会按规则版本号拦下。"""
    try:
        entry = service.update_entry(entry_id, payload.values, operator=str(payload.values.get("操作人") or ""))
    except AlertError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message="预警规则已更新，需重新评审后生效", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """执行提交规则评审、生效规则、关闭规则、重新评审或发起修订。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(
        entry_id,
        action,
        values=payload.values,
        operator=str(payload.values.get("操作人") or ""),
    )
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.post("/{entry_id}/trigger", response_model=ActionResult)
def trigger_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """模拟一个周期的指标取值：命中则累计连续周期，关闭后的规则不再接收触发。"""
    values = payload.values
    value = values.get("取值", values.get("value"))
    cycle = values.get("周期", values.get("cycle"))
    if value is None:
        return ActionResult(ok=False, message="触发需要提供取值")
    if cycle is None:
        return ActionResult(ok=False, message="触发需要提供周期")
    try:
        outcome = service.trigger(
            entry_id,
            value=value,
            cycle=cycle,
            occurred_at=values.get("发生时间") or values.get("occurred_at"),
            operator=str(values.get("操作人") or "调度触发"),
        )
    except AlertError as error:
        return ActionResult(ok=False, message=str(error))
    evaluation = outcome["evaluation"]
    state = "命中" if evaluation["是否命中"] else "未命中"
    message = (
        f"周期 {evaluation['周期']} {state}，连续周期 {evaluation['连续周期数']}/"
        f"{evaluation['要求持续周期数']}"
    )
    if evaluation["达到阈值"]:
        message += f"，已产生预警通知（{evaluation['通知状态']}）"
    if evaluation["静默"]:
        message += "，处于静默时段"
    return ActionResult(ok=True, message=message, entry=outcome["rule"])
