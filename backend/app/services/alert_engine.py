"""预警判断的纯函数：比较运算符、阈值、静默时段与周期序号。

这些规则不读写数据仓库，指标预警服务和指标口径服务共用，避免两边各写一套判断。
"""
from __future__ import annotations

from datetime import datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any

# 历史数据里的运算符有多种写法，比较前统一成规范名；原始值始终保留在列表字段里。
_OPERATOR_ALIASES = {
    ">": "greater_than",
    "greater_than": "greater_than",
    "gt": "greater_than",
    "大于": "greater_than",
    "超过": "greater_than",
    ">=": "greater_than_or_equal",
    "greater_than_or_equal": "greater_than_or_equal",
    "gte": "greater_than_or_equal",
    "大于等于": "greater_than_or_equal",
    "不低于": "greater_than_or_equal",
    "≥": "greater_than_or_equal",
    "<": "less_than",
    "less_than": "less_than",
    "lt": "less_than",
    "小于": "less_than",
    "低于": "less_than",
    "<=": "less_than_or_equal",
    "less_than_or_equal": "less_than_or_equal",
    "lte": "less_than_or_equal",
    "小于等于": "less_than_or_equal",
    "不高于": "less_than_or_equal",
    "≤": "less_than_or_equal",
    "==": "equal",
    "=": "equal",
    "equal": "equal",
    "eq": "equal",
    "等于": "equal",
    "!=": "not_equal",
    "not_equal": "not_equal",
    "ne": "not_equal",
    "不等于": "not_equal",
}

OPERATOR_LABELS = {
    "greater_than": "大于",
    "greater_than_or_equal": "大于等于",
    "less_than": "小于",
    "less_than_or_equal": "小于等于",
    "equal": "等于",
    "not_equal": "不等于",
}

ACTIVE_STATUS = "已生效"
CLOSED_STATUS = "已关闭"
FROZEN_STATUS = "已冻结"
DRAFT_STATUS = "草稿"
REVIEW_STATUS = "规则评审中"
ALERT_STATUSES = [DRAFT_STATUS, REVIEW_STATUS, ACTIVE_STATUS, FROZEN_STATUS, CLOSED_STATUS]
BINDABLE_ALERT_STATUSES = [DRAFT_STATUS, REVIEW_STATUS, ACTIVE_STATUS]


def normalize_operator(raw: Any) -> str | None:
    """把 greater_than / > / 大于等写法统一成规范名，无法识别时返回 None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    return _OPERATOR_ALIASES.get(text.lower(), _OPERATOR_ALIASES.get(text))


def operator_display(raw: Any) -> str:
    normalized = normalize_operator(raw)
    if normalized is None:
        return str(raw or "—")
    label = OPERATOR_LABELS[normalized]
    raw_text = str(raw or "").strip()
    return raw_text if raw_text == normalized else f"{raw_text}（{label}）"


def parse_number(raw: Any) -> Decimal | None:
    """严格解析数值：拒绝布尔、空值和 nan/inf，避免非法阈值混进比较。"""
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return value


def parse_positive_int(raw: Any) -> int | None:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if value != value.to_integral_value() or value < 1:
        return None
    return int(value)


def parse_silence_window(raw: Any) -> tuple[time, time] | None:
    """解析 00:00-06:00 这类静默时段；支持跨零点时段（如 22:00-02:00）。"""
    text = str(raw or "").strip()
    if not text or "-" not in text:
        return None
    start_text, end_text = (part.strip() for part in text.split("-", 1))
    try:
        start = datetime.strptime(start_text, "%H:%M").time()
        end = datetime.strptime(end_text, "%H:%M").time()
    except ValueError:
        return None
    return start, end


def in_silence_window(raw: Any, occurred_at: Any) -> bool:
    window = parse_silence_window(raw)
    if window is None:
        return False
    moment: time | None = None
    if isinstance(occurred_at, datetime):
        moment = occurred_at.time()
    elif occurred_at:
        text = str(occurred_at).strip()
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M"):
            try:
                moment = datetime.strptime(text, fmt).time()
                break
            except ValueError:
                continue
        if moment is None and "T" in text:
            try:
                moment = datetime.fromisoformat(text).time()
            except ValueError:
                moment = None
    if moment is None:
        return False
    start, end = window
    if start <= end:
        return start <= moment <= end
    return moment >= start or moment <= end


def evaluate(value: Any, operator: Any, threshold: Any) -> bool | None:
    """返回本次取值是否命中规则；运算符或数值非法时返回 None。"""
    normalized = normalize_operator(operator)
    current = parse_number(value)
    limit = parse_number(threshold)
    if normalized is None or current is None or limit is None:
        return None
    if normalized == "greater_than":
        return current > limit
    if normalized == "greater_than_or_equal":
        return current >= limit
    if normalized == "less_than":
        return current < limit
    if normalized == "less_than_or_equal":
        return current <= limit
    if normalized == "equal":
        return current == limit
    return current != limit


def validate_threshold(values: dict[str, Any]) -> tuple[Decimal | None, str]:
    """生效前校验阈值；返回 (阈值, 错误说明)。"""
    normalized = normalize_operator(values.get("比较运算符"))
    if normalized is None:
        return None, f"比较运算符「{values.get('比较运算符') or '空'}」无法识别"
    threshold = parse_number(values.get("阈值数值"))
    if threshold is None:
        return None, f"阈值数值「{values.get('阈值数值') if values.get('阈值数值') is not None else '空'}」不是合法数值"
    return threshold, ""


def cycle_sort_key(cycle: Any) -> tuple[int, str]:
    """周期既可能是日期，也可能是 2026-W41 这类标签，统一给出可比较的 key。"""
    text = str(cycle or "").strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m", "%Y%m%d"):
        try:
            return 1, datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return 0, text


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()
