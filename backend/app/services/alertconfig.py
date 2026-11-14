"""指标预警业务规则：状态流转、阈值校验、连续周期累计与指标版本联动。

状态闭环：

    草稿 --提交规则评审--> 规则评审中 --生效规则--> 已生效 --关闭规则--> 已关闭（终点）
                              ^                      |
                              |                      +--发起修订--> 规则评审中
    已冻结 --重新评审--> 规则评审中

- 只有「已生效」的规则参与累计，关闭后不再接收任何触发，连续周期停在关闭时的原始值。
- 生效前必须绑定当前已发布的指标版本，阈值与比较运算符必须合法；任何修改都要回到评审。
- 指标换版 / 下线会冻结仍绑在旧版本上的规则，并留下可追溯的历史。
"""
from __future__ import annotations

from typing import Any

from app.services import metric_catalog
from app.services.alert_engine import (
    ACTIVE_STATUS,
    ALERT_STATUSES,
    CLOSED_STATUS,
    DRAFT_STATUS,
    FROZEN_STATUS,
    REVIEW_STATUS,
    cycle_sort_key,
    evaluate,
    in_silence_window,
    normalize_operator,
    now_iso,
    operator_display,
    parse_positive_int,
    validate_threshold,
)
from app.store import store

MODULE = "alertconfig"
HISTORY_MODULE = "alert_history"
NOTIFICATION_MODULE = "alert_notification"

REQUIRED_FIELDS = ["预警编号", "关联指标", "比较运算符", "阈值数值", "持续周期数"]
EDITABLE_FIELDS = [
    "关联指标",
    "比较运算符",
    "阈值数值",
    "持续周期数",
    "预警级别",
    "通知接收人",
    "静默时段",
]
ACTION_RULES = {
    "提交规则评审": REVIEW_STATUS,
    "撤回评审": DRAFT_STATUS,
    "生效规则": ACTIVE_STATUS,
    "关闭规则": CLOSED_STATUS,
    "重新评审": DRAFT_STATUS,
    "发起修订": DRAFT_STATUS,
}
SORT_FIELDS = {
    "id": lambda row: int(row.get("id", 0)),
    "预警编号": lambda row: str(row.get("预警编号") or ""),
    "关联指标": lambda row: str(row.get("关联指标") or ""),
    "关联指标版本": lambda row: str(row.get("关联指标版本") or ""),
    "状态": lambda row: ALERT_STATUSES.index(row.get("status")) if row.get("status") in ALERT_STATUSES else 99,
    "连续周期数": lambda row: int(row.get("连续周期数") or 0),
    "阈值数值": lambda row: float(row.get("阈值") or 0),
    "关闭时间": lambda row: str(row.get("关闭时间") or ""),
}
DEFAULT_SORT = "id"


class AlertError(Exception):
    """带可读说明的业务失败；事务里抛出会整体回滚。"""


def _row_or_raise(entry_id: int) -> dict[str, Any]:
    entry = store.find(MODULE, entry_id)
    if entry is None:
        raise AlertError(f"预警规则 {entry_id} 不存在或已归档")
    return entry


def _metric_of(entry: dict[str, Any]) -> dict[str, Any] | None:
    return metric_catalog.find_metric(entry.get("关联指标ID") or entry.get("关联指标"))


def _is_metric_live_for(entry: dict[str, Any], metric: dict[str, Any] | None) -> bool:
    if metric is None:
        return False
    live = metric_catalog.live_version(metric)
    if live is None:
        return False
    return str(entry.get("关联指标版本") or "") == live


def _history(entry: dict[str, Any], *, event: str, before: str, after: str, note: str,
             operator: str = "", extra: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {
        "规则ID": int(entry["id"]),
        "预警编号": entry.get("预警编号"),
        "指标版本": entry.get("关联指标版本"),
        "事件": event,
        "动作": event,
        "来源": operator or "系统",
        "操作人": operator or "系统",
        "说明": note,
        "时间": now_iso(),
        "连续周期数": entry.get("连续周期数", entry.get("连续周期", 0)),
        "连续周期": entry.get("连续周期数", 0),
        "触发周期": None,
        "取值": None,
        "阈值": entry.get("阈值"),
        "比较运算符": entry.get("比较运算符"),
        "是否命中": None,
        "通知状态": None,
        "静默时间": None,
        "前状态": before,
        "后状态": after,
    }
    if extra:
        row.update(extra)
    return store.insert(HISTORY_MODULE, row)


def _set_status(entry: dict[str, Any], status: str) -> None:
    entry["status"] = status
    entry["pending"] = status in (DRAFT_STATUS, REVIEW_STATUS)
    entry["abnormal"] = status == FROZEN_STATUS


def _refresh_display(entry: dict[str, Any]) -> None:
    """每次读取都回填指标实时状态，列表与指标详情不会各说各话。"""
    metric = _metric_of(entry)
    entry["指标状态"] = metric_catalog.metric_state(metric)
    entry["指标可用"] = metric_catalog.live_version(metric) is not None
    entry["当前已发布版本"] = metric_catalog.live_version(metric)
    if entry["指标可用"] and entry.get("关联指标版本"):
        entry["绑定版本状态"] = (
            metric_catalog.PUBLISHED_STATE
            if str(entry.get("关联指标版本")) == str(metric_catalog.live_version(metric))
            else "待重新绑定"
        )
    entry["比较运算符展示"] = operator_display(entry.get("比较运算符"))
    entry["连续周期数"] = int(entry.get("连续周期") or 0)
    entry["周期序号"] = int(entry.get("持续周期数") or 0)
    entry.setdefault("规则版本", 1)
    if entry.get("status") == CLOSED_STATUS:
        # 老数据可能没写关闭快照，按当前计数补齐，保证「关闭后停在原值」可核对。
        entry.setdefault("关闭前连续周期", int(entry.get("连续周期数") or 0))
        if not entry.get("关闭时间"):
            closed = [
                row.get("时间")
                for row in store.rows(HISTORY_MODULE)
                if int(row.get("规则ID", -1)) == int(entry.get("id", 0))
                and row.get("后状态") == CLOSED_STATUS
            ]
            entry["关闭时间"] = closed[-1] if closed else None


def _reconcile(entry: dict[str, Any]) -> bool:
    """修复历史遗留的单侧状态：规则显示已生效，指标却已下线或已换版。"""
    if entry.get("status") != ACTIVE_STATUS:
        return False
    metric = _metric_of(entry)
    if _is_metric_live_for(entry, metric):
        return False
    live = metric_catalog.live_version(metric)
    reason = "指标已下线" if live is None else f"指标已发布新版本 {live}"
    _set_status(entry, FROZEN_STATUS)
    entry["冻结时间"] = now_iso()
    entry["冻结原因"] = reason
    entry["状态说明"] = f"{reason}，规则已冻结，需重新评审后才能生效"
    entry["绑定版本状态"] = "待重新绑定"
    _history(
        entry,
        event="冻结规则",
        before=ACTIVE_STATUS,
        after=FROZEN_STATUS,
        note=reason,
        operator="系统对账",
    )
    return True


def freeze_alerts_for_metric(
    metric: dict[str, Any],
    *,
    reason: str,
    operator: str = "系统",
) -> int:
    """指标换版 / 下线时冻结仍挂在旧版本上的生效规则。

    只冻结「已生效」的规则：已关闭是终点，保持关闭；评审中的规则在生效时自然会
    重新绑定新的已发布版本。连续周期、阈值、比较运算符全部原样保留。
    """
    frozen = 0
    live = metric_catalog.live_version(metric)
    with store.transaction():
        for entry in store.rows(MODULE):
            if str(entry.get("关联指标ID") or "") != str(metric.get("id")) and str(
                entry.get("关联指标") or ""
            ) != str(metric.get("指标编号") or ""):
                continue
            if entry.get("status") != ACTIVE_STATUS:
                continue
            if live is not None and str(entry.get("关联指标版本") or "") == str(live):
                # 已经绑定新版本的规则不受换版影响。
                continue
            _set_status(entry, FROZEN_STATUS)
            entry["冻结时间"] = now_iso()
            entry["冻结原因"] = reason
            entry["绑定版本状态"] = "待重新绑定"
            entry["状态说明"] = f"{reason}，规则已冻结，需重新评审后绑定新版本"
            _history(
                entry,
                event="冻结规则",
                before=ACTIVE_STATUS,
                after=FROZEN_STATUS,
                note=reason,
                operator=operator,
                extra={"指标版本": metric_catalog.live_version(metric)},
            )
            frozen += 1
    return frozen


def reconcile_all() -> int:
    changed = 0
    with store.transaction():
        for entry in store.rows(MODULE):
            _refresh_display(entry)
            if _reconcile(entry):
                changed += 1
    return changed


class AlertconfigService:
    # ------------------------------------------------------------------ 读取
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        metric_version: str | None = None,
        sort_by: str = DEFAULT_SORT,
        order: str = "asc",
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        if sort_by not in SORT_FIELDS:
            raise AlertError(f"不支持的排序字段「{sort_by}」，可选：{'、'.join(SORT_FIELDS)}")
        if order not in ("asc", "desc"):
            raise AlertError("排序方向只能是 asc 或 desc")
        with store.transaction():
            reconcile_all()
            rows = list(store.rows(MODULE))
            for row in rows:
                _refresh_display(row)
            if keyword:
                needle = keyword.strip()
                rows = [
                    row
                    for row in rows
                    if needle in str(row.get("预警编号") or "")
                    or needle in str(row.get("关联指标") or "")
                    or needle in str(row.get("关联指标版本") or "")
                    or needle in str(row.get("通知接收人") or "")
                ]
            if status:
                rows = [row for row in rows if row.get("status") == status]
            if metric_version:
                rows = [
                    row for row in rows if str(row.get("关联指标版本") or "") == metric_version
                ]
            key = SORT_FIELDS[sort_by]
            # id 兜底保证同一份数据每次排序结果一致，翻页不会串行。
            rows.sort(key=lambda row: (key(row), int(row.get("id", 0))), reverse=(order == "desc"))
            total = len(rows)
            start = max(page - 1, 0) * size
            # 返回副本：序号与分页信息只属于本次查询，不写回仓库。
            page_rows = [dict(row) for row in rows[start:start + size]]
            for offset, row in enumerate(page_rows):
                row["序号"] = start + offset + 1
                row["序列位置"] = f"第 {start + offset + 1} / 共 {total} 条"
                row["页码"] = page
                row["页大小"] = size
                row["排序字段"] = sort_by
                row["排序方向"] = order
            return page_rows, total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        with store.transaction():
            entry = store.find(MODULE, entry_id)
            if entry is None:
                return None
            _refresh_display(entry)
            _reconcile(entry)
            _refresh_display(entry)
            return dict(entry)

    def get_trace(self, entry_id: int) -> dict[str, Any]:
        """预警状态与口径版本的互相追溯：规则、指标、版本、历史、通知放在一起。"""
        entry = self.get_entry(entry_id)
        if entry is None:
            raise AlertError(f"预警规则 {entry_id} 不存在或已归档")
        metric = _metric_of(entry)
        history = [
            dict(row)
            for row in sorted(
                (row for row in store.rows(HISTORY_MODULE) if int(row.get("规则ID", -1)) == entry_id),
                key=lambda row: (str(row.get("时间") or ""), int(row.get("id", 0))),
            )
        ]
        notifications = [
            dict(row)
            for row in sorted(
                (row for row in store.rows(NOTIFICATION_MODULE) if int(row.get("规则ID", -1)) == entry_id),
                key=lambda row: (cycle_sort_key(row.get("触发周期")), int(row.get("id", 0))),
            )
        ]
        return {
            "rule": entry,
            "metric": metric_catalog.metric_view(metric) if metric else None,
            "metric_brief": metric_catalog.metric_brief(metric),
            "binding": {
                "关联指标ID": entry.get("关联指标ID"),
                "关联指标版本": entry.get("关联指标版本"),
                "绑定版本状态": entry.get("绑定版本状态"),
                "当前已发布版本": metric_catalog.live_version(metric),
                "版本一致": str(entry.get("关联指标版本") or "")
                == str(metric_catalog.live_version(metric) or ""),
            },
            "history": history,
            "notifications": notifications,
        }

    def get_history(self, entry_id: int) -> list[dict[str, Any]]:
        _row_or_raise(entry_id)
        return [
            dict(row)
            for row in sorted(
                (row for row in store.rows(HISTORY_MODULE) if int(row.get("规则ID", -1)) == entry_id),
                key=lambda row: (str(row.get("时间") or ""), int(row.get("id", 0))),
            )
        ]

    def get_notifications(self, entry_id: int) -> list[dict[str, Any]]:
        _row_or_raise(entry_id)
        return [
            dict(row)
            for row in sorted(
                (row for row in store.rows(NOTIFICATION_MODULE) if int(row.get("规则ID", -1)) == entry_id),
                key=lambda row: (cycle_sort_key(row.get("触发周期")), int(row.get("id", 0))),
            )
        ]

    # ------------------------------------------------------------------ 写入
    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        code = str(values.get("预警编号") or "").strip()
        if any(str(row.get("预警编号") or "") == code for row in store.rows(MODULE)):
            return None, [f"预警编号 {code} 已存在"]
        metric = metric_catalog.find_metric(values.get("关联指标"))
        if metric is None:
            return None, [f"关联指标「{values.get('关联指标')}」不存在"]
        operator = normalize_operator(values.get("比较运算符"))
        if operator is None:
            return None, [f"比较运算符「{values.get('比较运算符')}」无法识别"]
        threshold, error = validate_threshold(values)
        if error:
            return None, [error]
        cycles = parse_positive_int(values.get("持续周期数"))
        if cycles is None:
            return None, ["持续周期数必须是大于 0 的整数"]
        with store.transaction():
            entry = {
                "id": store.next_id(MODULE),
                "status": DRAFT_STATUS,
                "pending": True,
                "abnormal": False,
                "预警编号": code,
                "关联指标": metric.get("指标编号"),
                "比较运算符": operator,
                "阈值数值": str(threshold),
                "持续周期数": str(cycles),
                "预警级别": values.get("预警级别") or "P2",
                "通知接收人": values.get("通知接收人") or "数据值班组",
                "静默时段": values.get("静默时段") or "",
                "关联指标ID": metric.get("id"),
                "关联指标版本": None,
                "绑定版本号": None,
                "绑定版本状态": "未绑定",
                "阈值": str(threshold),
                "阈值边界": "含等于" if operator in ("greater_than_or_equal", "less_than_or_equal") else "不含等于",
                "周期序号": cycles,
                "连续周期数": 0,
                "当前连续周期": 0,
                "连续周期": 0,
                "上一命中周期": None,
                "最近触发周期": None,
                "关闭时间": None,
                "关闭前连续周期": None,
                "冻结时间": None,
                "冻结原因": None,
                "最近评审时间": None,
                "评审意见": None,
                "规则版本": 1,
                "草稿": True,
                "状态说明": "规则草稿，提交规则评审后才能生效",
            }
            store.rows(MODULE).append(entry)
            _history(
                entry,
                event="登记规则",
                before="无",
                after=DRAFT_STATUS,
                note=f"登记预警规则 {code}，关联指标 {metric.get('指标编号')}",
                operator=str(values.get("操作人") or "数据值班员"),
            )
            _refresh_display(entry)
            return dict(entry), []

    def update_entry(
        self,
        entry_id: int,
        values: dict[str, Any],
        *,
        operator: str = "",
    ) -> dict[str, Any]:
        """只有草稿/评审中的规则可以改阈值和比较运算符，并发修改按规则版本号拦下。"""
        with store.transaction():
            entry = _row_or_raise(entry_id)
            _refresh_display(entry)
            if entry.get("status") != DRAFT_STATUS:
                raise AlertError(
                    f"规则当前为「{entry.get('status')}」，阈值和比较运算符只能在草稿中修改，"
                    "请先撤回评审或发起修订"
                )
            expected = values.get("expected_rule_version")
            if expected is not None and int(expected) != int(entry.get("规则版本") or 1):
                raise AlertError(
                    f"规则已被其他人修改（当前规则版本 {entry.get('规则版本')}），请刷新后重试"
                )
            updates: dict[str, Any] = {}
            for field in EDITABLE_FIELDS:
                if field in values and values[field] is not None:
                    updates[field] = values[field]
            if not updates:
                raise AlertError("没有可修改的字段")
            if "关联指标" in updates:
                metric = metric_catalog.find_metric(updates["关联指标"])
                if metric is None:
                    raise AlertError(f"关联指标「{updates['关联指标']}」不存在")
                updates["关联指标ID"] = metric.get("id")
                updates["关联指标"] = metric.get("指标编号")
                if str(entry.get("关联指标ID")) != str(metric.get("id")):
                    updates["关联指标版本"] = None
                    updates["绑定版本状态"] = "未绑定"
            candidate = dict(entry)
            candidate.update(updates)
            operator_name = normalize_operator(candidate.get("比较运算符"))
            if operator_name is None:
                raise AlertError(f"比较运算符「{candidate.get('比较运算符')}」无法识别")
            threshold, error = validate_threshold(candidate)
            if error:
                raise AlertError(error)
            cycles = parse_positive_int(candidate.get("持续周期数"))
            if cycles is None:
                raise AlertError("持续周期数必须是大于 0 的整数")
            if "静默时段" in updates and str(updates.get("静默时段") or "").strip():
                from app.services.alert_engine import parse_silence_window

                if parse_silence_window(updates["静默时段"]) is None:
                    raise AlertError("静默时段格式应为 HH:MM-HH:MM，例如 00:00-06:00")
            before = {
                "比较运算符": entry.get("比较运算符"),
                "阈值数值": entry.get("阈值数值"),
                "持续周期数": entry.get("持续周期数"),
                "静默时段": entry.get("静默时段"),
            }
            entry.update(updates)
            entry["比较运算符"] = operator_name
            entry["阈值数值"] = str(threshold)
            entry["阈值"] = str(threshold)
            entry["持续周期数"] = str(cycles)
            entry["规则版本"] = int(entry.get("规则版本") or 1) + 1
            _history(
                entry,
                event="修改规则",
                before=str(entry.get("status")),
                after=str(entry.get("status")),
                note=f"阈值/比较符变更：{before} -> "
                f"{ {key: entry.get(key) for key in before} }",
                operator=operator or "数据值班员",
                extra={"变更前": before, "变更后": {key: entry.get(key) for key in before}},
            )
            _refresh_display(entry)
            return dict(entry)

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
            return None, f"动作「{action}」不属于指标预警可执行范围"
        try:
            with store.transaction():
                entry = _row_or_raise(entry_id)
                _refresh_display(entry)
                _reconcile(entry)
                message = self._apply_action(entry, action, values, operator)
                _refresh_display(entry)
                return dict(entry), message
        except AlertError as error:
            return None, str(error)

    def _apply_action(
        self,
        entry: dict[str, Any],
        action: str,
        values: dict[str, Any],
        operator: str,
    ) -> str:
        current = str(entry.get("status") or DRAFT_STATUS)
        expected_status = values.get("expected_status")
        if expected_status is not None and str(expected_status) != current:
            raise AlertError(
                f"规则已被其他人处理，当前状态为「{current}」，请刷新后重试"
            )
        if current == CLOSED_STATUS:
            raise AlertError("规则已关闭，关闭是终点，不能再次操作；如需监控请登记新规则")
        if action == "关闭规则":
            return self._close(entry, operator)
        if action == "提交规则评审":
            if current != DRAFT_STATUS:
                raise AlertError(f"只有草稿可以提交评审，当前状态为「{current}」")
            self._ensure_valid(entry)
            _set_status(entry, REVIEW_STATUS)
            entry["最近评审时间"] = None
            entry["评审意见"] = None
            entry["状态说明"] = "已提交规则评审，评审通过后才能生效"
            _history(
                entry,
                event="提交规则评审",
                before=current,
                after=REVIEW_STATUS,
                note="规则进入评审",
                operator=operator,
            )
            return "预警规则已提交评审"
        if action == "生效规则":
            if current != REVIEW_STATUS:
                raise AlertError(f"只有规则评审中才能生效，当前状态为「{current}」")
            return self._activate(entry, values, operator)
        if action == "撤回评审":
            if current != REVIEW_STATUS:
                raise AlertError(f"只有规则评审中可以撤回到草稿，当前状态为「{current}」")
            _set_status(entry, DRAFT_STATUS)
            entry["状态说明"] = "已撤回评审，可修改阈值和比较运算符后重新提交"
            _history(
                entry,
                event="撤回评审",
                before=current,
                after=DRAFT_STATUS,
                note="评审撤回，规则回到草稿",
                operator=operator,
            )
            return "预警规则已撤回到草稿"
        if action == "重新评审":
            if current != FROZEN_STATUS:
                raise AlertError(f"只有已冻结的规则需要重新评审，当前状态为「{current}」")
            metric = _metric_of(entry)
            if metric_catalog.live_version(metric) is None:
                raise AlertError("关联指标尚无可用的已发布版本，需先发布新版本再重新评审")
            self._ensure_valid(entry)
            _set_status(entry, DRAFT_STATUS)
            entry["规则版本"] = int(entry.get("规则版本") or 1) + 1
            entry["绑定版本状态"] = "待重新绑定"
            entry["冻结原因"] = None
            entry["冻结时间"] = None
            entry["状态说明"] = "已从冻结恢复为草稿，修改后需提交评审并绑定新版本"
            _history(
                entry,
                event="重新评审",
                before=current,
                after=DRAFT_STATUS,
                note="冻结规则恢复为草稿，待重新评审绑定新版本",
                operator=operator,
            )
            return "预警规则已恢复为草稿，请提交评审后生效"
        if action == "发起修订":
            if current != ACTIVE_STATUS:
                raise AlertError(f"只有已生效的规则可以发起修订，当前状态为「{current}」")
            _set_status(entry, DRAFT_STATUS)
            entry["规则版本"] = int(entry.get("规则版本") or 1) + 1
            entry["状态说明"] = "已发起修订，修订期间不再累计周期，修改后需重新评审"
            _history(
                entry,
                event="发起修订",
                before=current,
                after=DRAFT_STATUS,
                note="已生效规则发起修订，回到草稿等待重新评审",
                operator=operator,
            )
            return "预警规则已发起修订，回到草稿"
        raise AlertError(f"动作「{action}」不属于指标预警可执行范围")

    def _ensure_valid(self, entry: dict[str, Any]) -> None:
        if not str(entry.get("预警编号") or "").strip():
            raise AlertError("预警编号不能为空")
        metric = _metric_of(entry)
        if metric is None:
            raise AlertError(f"关联指标「{entry.get('关联指标')}」不存在")
        if normalize_operator(entry.get("比较运算符")) is None:
            raise AlertError(f"比较运算符「{entry.get('比较运算符')}」无法识别")
        _, error = validate_threshold(entry)
        if error:
            raise AlertError(error)
        if parse_positive_int(entry.get("持续周期数")) is None:
            raise AlertError("持续周期数必须是大于 0 的整数")

    def _activate(
        self,
        entry: dict[str, Any],
        values: dict[str, Any],
        operator: str,
    ) -> str:
        self._ensure_valid(entry)
        metric = _metric_of(entry)
        live = metric_catalog.live_version(metric)
        if metric is None or live is None:
            raise AlertError("关联指标没有已发布的版本，规则不能生效")
        requested = values.get("指标版本")
        if requested is not None and str(requested).strip():
            requested = str(requested).strip()
            if requested != live:
                raise AlertError(
                    f"指标版本「{requested}」不是当前已发布版本（当前为 {live}），"
                    "请重新评审后再生效"
                )
        metric_catalog.ensure_versions(metric)
        state = metric_catalog.version_state(metric, live)
        if state != metric_catalog.PUBLISHED_STATE:
            raise AlertError(f"指标版本 {live} 状态为「{state}」，不能作为生效绑定版本")
        previous = str(entry.get("关联指标版本") or "")
        entry["关联指标"] = metric.get("指标编号")
        entry["关联指标ID"] = metric.get("id")
        entry["关联指标版本"] = live
        entry["绑定版本号"] = int(str(live).rsplit("-v", 1)[-1])
        entry["绑定版本状态"] = metric_catalog.PUBLISHED_STATE
        # 生效后从「从未命中」开始累计；关闭/冻结前的原始计数留在历史里。
        preserved = int(entry.get("连续周期数") or 0)
        entry["当前连续周期"] = 0
        entry["连续周期"] = 0
        entry["连续周期数"] = 0
        entry["上一命中周期"] = None
        entry["最近触发周期"] = None
        entry["关闭时间"] = None
        entry["关闭前连续周期"] = None
        entry["冻结原因"] = None
        entry["冻结时间"] = None
        _set_status(entry, ACTIVE_STATUS)
        entry["状态说明"] = f"已生效，绑定指标版本 {live}，连续周期从 0 开始累计"
        _history(
            entry,
            event="生效规则",
            before=REVIEW_STATUS,
            after=ACTIVE_STATUS,
            note=(
                f"绑定指标版本 {live}"
                + (f"，原绑定版本 {previous}" if previous and previous != live else "")
                + f"；生效前连续周期 {preserved} 保留在历史中，生效后重新累计"
            ),
            operator=operator,
        )
        return f"预警规则已生效，绑定指标版本 {live}"

    def _close(self, entry: dict[str, Any], operator: str) -> str:
        current = str(entry.get("status") or "")
        if current == CLOSED_STATUS:
            raise AlertError("规则已关闭，关闭是终点")
        entry["关闭时间"] = now_iso()
        entry["关闭前连续周期"] = int(entry.get("连续周期数") or 0)
        entry["状态说明"] = (
            f"已关闭，连续周期停在 {entry['关闭前连续周期']}，不再累计也不再通知"
        )
        _set_status(entry, CLOSED_STATUS)
        _history(
            entry,
            event="关闭规则",
            before=current,
            after=CLOSED_STATUS,
            note=f"关闭时连续周期为 {entry['关闭前连续周期']}，关闭后停止累计",
            operator=operator,
        )
        return f"预警规则已关闭，连续周期停在 {entry['关闭前连续周期']}"

    # ------------------------------------------------------------------ 触发
    def trigger(
        self,
        entry_id: int,
        *,
        value: Any,
        cycle: Any,
        occurred_at: Any = None,
        operator: str = "",
    ) -> dict[str, Any]:
        """接收一个周期的指标取值并累计；关闭后的规则不会产生任何计数或通知。"""
        with store.transaction():
            entry = _row_or_raise(entry_id)
            _refresh_display(entry)
            _reconcile(entry)
            _refresh_display(entry)
            status = entry.get("status")
            if status == CLOSED_STATUS:
                raise AlertError(
                    f"规则已关闭，连续周期停在 {entry.get('连续周期数')}，不再接收触发"
                )
            if status != ACTIVE_STATUS:
                raise AlertError(f"规则当前为「{status}」，只有已生效的规则参与累计")
            metric = _metric_of(entry)
            live = metric_catalog.live_version(metric)
            if not _is_metric_live_for(entry, metric):
                _set_status(entry, FROZEN_STATUS)
                entry["冻结时间"] = now_iso()
                entry["冻结原因"] = (
                    "指标已下线" if live is None else f"指标版本已切换为 {live}"
                )
                _history(
                    entry,
                    event="冻结规则",
                    before=ACTIVE_STATUS,
                    after=FROZEN_STATUS,
                    note=str(entry["冻结原因"]),
                    operator="系统",
                )
                raise AlertError(f"{entry['冻结原因']}，规则已冻结，本周期不累计")
            hit = evaluate(value, entry.get("比较运算符"), entry.get("阈值"))
            if hit is None:
                raise AlertError("取值或阈值不是合法数值，本周期不累计")
            cycle_label = str(cycle or "").strip() or now_iso()[:10]
            already = next(
                (
                    row
                    for row in store.rows(HISTORY_MODULE)
                    if int(row.get("规则ID", -1)) == entry_id
                    and str(row.get("触发周期") or "") == cycle_label
                    and row.get("事件") == "周期触发"
                ),
                None,
            )
            if already is not None:
                raise AlertError(
                    f"周期 {cycle_label} 已处理，未重复累计（当前连续周期 "
                    f"{int(entry.get('连续周期数') or 0)}）"
                )
            if in_silence_window(entry.get("静默时段"), occurred_at or cycle_label):
                silent = True
            else:
                silent = False
            before_streak = int(entry.get("连续周期数") or 0)
            if hit:
                streak = before_streak + 1
                entry["上一命中周期"] = cycle_label
                entry["最近触发周期"] = cycle_label
            else:
                streak = 0
                entry["上一命中周期"] = None
            entry["连续周期"] = streak
            entry["连续周期数"] = streak
            required = parse_positive_int(entry.get("持续周期数")) or 1
            notification = None
            reached = hit and streak >= required
            if reached:
                existing = next(
                    (
                        row
                        for row in store.rows(NOTIFICATION_MODULE)
                        if int(row.get("规则ID", -1)) == entry_id
                        and str(row.get("触发周期") or "") == cycle_label
                    ),
                    None,
                )
                if existing is None:
                    notification = None if silent else store.insert(
                        NOTIFICATION_MODULE,
                        {
                            "规则ID": int(entry["id"]),
                            "预警编号": entry.get("预警编号"),
                            "指标版本": entry.get("关联指标版本"),
                            "触发周期": cycle_label,
                            "取值": str(value),
                            "阈值": entry.get("阈值"),
                            "比较运算符": entry.get("比较运算符"),
                            "连续周期数": streak,
                            "通知状态": "已发送",
                            "静默时间": False,
                            "时间": now_iso(),
                            "说明": f"连续第 {streak} 个周期命中，发出预警通知",
                        },
                    )
            _history(
                entry,
                event="周期触发",
                before=ACTIVE_STATUS,
                after=ACTIVE_STATUS,
                note=(
                    f"周期 {cycle_label} 取值 {value}，"
                    f"{'命中' if hit else '未命中'}，连续周期 {before_streak} -> {streak}"
                ),
                operator=operator or "调度触发",
                extra={
                    "触发周期": cycle_label,
                    "取值": str(value),
                    "是否命中": hit,
                    "通知状态": (notification or {}).get("通知状态", "未达阈值"),
                    "静默时间": silent,
                    "前连续周期": before_streak,
                },
            )
            _refresh_display(entry)
            return {
                "rule": dict(entry),
                "evaluation": {
                    "周期": cycle_label,
                    "取值": str(value),
                    "比较运算符": entry.get("比较运算符"),
                    "阈值": entry.get("阈值"),
                    "是否命中": hit,
                    "连续周期数": streak,
                    "要求持续周期数": required,
                    "达到阈值": reached,
                    "静默": silent,
                    "通知状态": (notification or {}).get("通知状态"),
                },
                "notification": dict(notification) if notification else None,
            }
