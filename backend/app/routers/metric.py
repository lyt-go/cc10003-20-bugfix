"""指标口径接口：版本化口径定义，覆盖提交评审、发布、下线、回滚与追溯。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.metric import MetricError, MetricService
from app.services.metric_catalog import METRIC_STATUSES

router = APIRouter(prefix="/api/metric", tags=["指标口径"])

service = MetricService()

LIST_FIELDS = ["指标编号", "指标名称", "计算公式", "统计粒度", "时间窗口", "筛选条件", "精度位数", "口径版本"]


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按指标编号、名称或版本检索"),
    status: str | None = Query(default=None, description="草稿、口径评审中、已发布、已下线"),
    sort_by: str = Query(default="id", description="排序字段：id、指标编号、指标名称、状态、版本号"),
    order: str = Query(default="asc"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按指标编号与状态过滤指标口径列表；每条记录都带当前发布状态和版本追溯。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    if page < 1:
        raise HTTPException(status_code=400, detail="页码从 1 开始")
    if status and status not in METRIC_STATUSES:
        raise HTTPException(status_code=400, detail=f"状态「{status}」不在允许范围内")
    try:
        items, total = service.list_entries(
            keyword=keyword, status=status, sort_by=sort_by, order=order, page=page, size=size
        )
    except MetricError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries(status: str | None = Query(default=None)) -> dict[str, Any]:
    """导出指标口径清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(status=status, page=1, size=10000)
    return {"module": "metric", "total": total, "items": items}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条指标定义明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"指标定义 {entry_id} 不存在或已归档")
    return entry


@router.get("/{entry_id}/detail", response_model=dict)
def get_detail(entry_id: int) -> dict:
    """指标详情：同时返回版本追溯、关联预警规则与状态，列表和详情不再错位。"""
    try:
        return service.get_detail(entry_id)
    except MetricError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条指标定义，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="指标定义已登记（草稿）", entry=entry)


@router.post("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改草稿指标的口径字段；已提交评审的指标必须生成新版本。"""
    try:
        entry = service.update_entry(entry_id, payload.values, operator=str(payload.values.get("操作人") or ""))
    except MetricError as error:
        return ActionResult(ok=False, message=str(error))
    return ActionResult(ok=True, message="指标草稿已更新", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条指标定义执行提交口径评审、发布指标、下线指标或回滚版本。"""
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
