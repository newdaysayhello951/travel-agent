"""工单领域模型与状态流转规则。

放在独立模块而不是塞进 Agent 或存储实现里，是为了让**内存实现**与**数据库实现**
共用同一套规则，避免两处各写一份校验逻辑导致行为漂移。
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum


class TicketStatus(str, Enum):
    """工单生命周期状态"""
    CREATED = "created"
    PROCESSING = "processing"
    RESOLVED = "resolved"
    CLOSED = "closed"


class TicketPriority(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    URGENT = "urgent"


# 允许的状态流转图；不在表里的组合一律拒绝，避免非法状态跳转。
ALLOWED_TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    TicketStatus.CREATED: {TicketStatus.PROCESSING, TicketStatus.CLOSED},
    TicketStatus.PROCESSING: {TicketStatus.RESOLVED, TicketStatus.CLOSED},
    TicketStatus.RESOLVED: {TicketStatus.CLOSED},
    TicketStatus.CLOSED: set(),
}

STATUS_LABELS: dict[str, str] = {
    TicketStatus.CREATED.value: "已创建",
    TicketStatus.PROCESSING.value: "处理中",
    TicketStatus.RESOLVED.value: "已解决",
    TicketStatus.CLOSED.value: "已关闭",
}

PRIORITY_LABELS: dict[str, str] = {
    TicketPriority.LOW.value: "普通",
    TicketPriority.MEDIUM.value: "中等",
    TicketPriority.HIGH.value: "高",
    TicketPriority.URGENT.value: "紧急",
}


def new_ticket_id(now: datetime | None = None) -> str:
    """生成工单号：日期 + 6 位随机十六进制，便于人工识别且不暴露业务量。"""
    now = now or datetime.now()
    return f"TK-{now.strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"


def check_transition(current: str, target: str) -> str | None:
    """
    校验状态流转是否合法。

    Returns:
        None 表示允许（含"重复设置为同一状态"的幂等情形）；否则返回拒绝原因。
    """
    try:
        current_status = TicketStatus(current)
    except ValueError:
        return f"工单当前状态「{current}」不在支持范围内"

    try:
        target_status = TicketStatus(target)
    except ValueError:
        allowed = "、".join(STATUS_LABELS.values())
        return f"不支持的状态「{target}」，可用状态：{allowed}"

    if target_status is current_status:
        return None  # 幂等

    if target_status not in ALLOWED_TRANSITIONS[current_status]:
        return (
            f"不允许从「{STATUS_LABELS[current_status.value]}」流转到"
            f"「{STATUS_LABELS[target_status.value]}」"
        )
    return None
