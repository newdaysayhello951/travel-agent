"""
工单处理 Agent — 创建、查询、更新与列出工单

工单当前保存在进程内存的 TicketStore 中，重启即丢（持久化在后续阶段接入）。
注意：本模块直接使用内部 TicketStore，尚未通过 tool_server 的 MCP 工具层调用。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call


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


# 允许的状态流转图；不在表里的组合一律拒绝，避免出现非法状态跳转。
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


TICKET_SYSTEM_PROMPT = """你是一个专业的工单处理Agent，负责处理客户的业务办理请求。

你的职责：
1. 分析用户需求，判断要执行哪种工单操作
2. 提取工单关键信息（类型、优先级、描述、目标状态）
3. 不负责生成最终话术，由系统按结构化结果执行

工单类型：
- refund: 退款申请
- claim: 理赔申请
- account_open: 开户申请
- account_change: 账户变更
- complaint: 投诉工单
- general: 通用工单

优先级判断规则：
- urgent: 资金安全、账户被盗
- high: 退款超时、理赔争议
- medium: 常规业务办理
- low: 信息咨询类

请以JSON格式返回工单信息：
{
    "action": "create|query|update|list",
    "ticket_type": "refund|claim|account_open|...",
    "priority": "low|medium|high|urgent",
    "summary": "工单摘要",
    "details": "详细描述",
    "ticket_id": "查询或更新工单时必须提取用户提供的工单号，没有就留空",
    "status": "更新工单时填写目标状态，取值 processing|resolved|closed"
}

说明：
- 用户只是咨询政策、问流程，没有办理意图时，action 返回 "query" 且 ticket_id 留空。
- 用户要查看自己有哪些工单时，action 返回 "list"。
"""


class TicketStore:
    """内存工单存储（重启即丢，持久化在后续阶段接入）"""

    def __init__(self):
        self._tickets: dict[str, dict] = {}

    def create(self, ticket_type: str, priority: str, summary: str,
               details: str, user_id: str) -> dict:
        ticket_id = f"TK-{datetime.now().strftime('%Y%m%d')}-{uuid.uuid4().hex[:6].upper()}"
        now = datetime.now().isoformat()
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": TicketStatus.CREATED.value,
            "summary": summary,
            "details": details,
            "user_id": user_id,
            "created_at": now,
            "updated_at": now,
        }
        self._tickets[ticket_id] = ticket
        return ticket

    def query(self, ticket_id: str) -> dict | None:
        return self._tickets.get(ticket_id)

    def query_by_user(self, user_id: str) -> list[dict]:
        return [t for t in self._tickets.values() if t["user_id"] == user_id]

    def update_status(self, ticket_id: str, status: str) -> tuple[dict | None, str | None]:
        """
        按 ALLOWED_TRANSITIONS 校验并更新状态。

        Returns:
            (更新后的工单, None) 成功；(None, 失败原因) 失败
        """
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None, f"未找到工单号 {ticket_id}"

        try:
            target = TicketStatus(status)
        except ValueError:
            allowed = "、".join(STATUS_LABELS.values())
            return None, f"不支持的状态「{status}」，可用状态：{allowed}"

        current = TicketStatus(ticket["status"])
        if target is current:
            return ticket, None  # 幂等：重复设置同一状态视为成功

        if target not in ALLOWED_TRANSITIONS[current]:
            return None, (
                f"不允许从「{STATUS_LABELS[current.value]}」流转到"
                f"「{STATUS_LABELS[target.value]}」"
            )

        ticket["status"] = target.value
        ticket["updated_at"] = datetime.now().isoformat()
        return ticket, None


class TicketHandlerAgent:
    """工单处理 Agent"""

    def __init__(self, llm: ChatOpenAI, ticket_store: TicketStore | None = None):
        self.llm = llm
        self.ticket_store = ticket_store or TicketStore()

    @trace_agent_call("ticket_analyze")
    async def analyze_request(self, user_message: str) -> dict:
        """用模型把自然语言需求抽取成结构化工单指令"""
        messages = [
            SystemMessage(content=TICKET_SYSTEM_PROMPT),
            HumanMessage(content=f"用户消息: {user_message}"),
        ]

        response = await self.llm.ainvoke(messages)

        # 模型常把 JSON 包在 Markdown 代码围栏里，这里剥掉后再解析。
        raw = response.content.strip()
        if raw.startswith("```"):
            raw = raw.removeprefix("```json").removeprefix("```")
            raw = raw.removesuffix("```").strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"action": "unknown", "raw_output": user_message[:200]}
        if not isinstance(parsed, dict):
            return {"action": "unknown", "raw_output": user_message[:200]}
        return parsed

    @trace_agent_call("ticket_create")
    async def create_ticket(self, ticket_info: dict, user_id: str) -> str:
        """创建工单"""
        ticket = self.ticket_store.create(
            ticket_type=ticket_info.get("ticket_type", "general"),
            priority=ticket_info.get("priority", "medium"),
            summary=ticket_info.get("summary", ""),
            details=ticket_info.get("details", ""),
            user_id=user_id,
        )

        priority_label = PRIORITY_LABELS.get(ticket["priority"], "中等")

        return (
            f"工单已创建成功！\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📝 类型: {ticket['type']}\n"
            f"⚡ 优先级: {priority_label}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n\n"
            f"我们将尽快处理您的请求，请保存好工单号以便后续查询。"
        )

    @trace_agent_call("ticket_query")
    async def query_ticket(self, ticket_id: str) -> str:
        """查询单个工单"""
        ticket = self.ticket_store.query(ticket_id)
        if not ticket:
            return f"未找到工单号 {ticket_id}，请确认工单号是否正确。"

        return self._format_ticket(ticket, header="工单查询结果：")

    @trace_agent_call("ticket_update")
    async def update_ticket(self, ticket_id: str, status: str) -> str:
        """更新工单状态，非法流转会给出明确原因"""
        if not ticket_id:
            return "请提供需要更新的工单号，例如 TK-20261101-ABC123。"
        if not status:
            return (
                f"请说明要把工单 {ticket_id} 更新为哪种状态，"
                f"可选：{('、'.join(STATUS_LABELS.values()))}。"
            )

        ticket, error = self.ticket_store.update_status(ticket_id, status)
        if error:
            return f"工单状态更新失败：{error}"

        return self._format_ticket(ticket, header="工单状态已更新：")

    @trace_agent_call("ticket_list")
    async def list_tickets(self, user_id: str) -> str:
        """列出某个用户的工单"""
        tickets = self.ticket_store.query_by_user(user_id)
        if not tickets:
            return "您当前没有工单记录。"

        lines = [f"您共有 {len(tickets)} 个工单："]
        for ticket in tickets:
            lines.append(
                f"- {ticket['ticket_id']}｜{ticket['type']}｜"
                f"{STATUS_LABELS.get(ticket['status'], ticket['status'])}｜{ticket['summary']}"
            )
        return "\n".join(lines)

    def _format_ticket(self, ticket: dict, header: str) -> str:
        status_label = STATUS_LABELS.get(ticket["status"], ticket["status"])
        return (
            f"{header}\n\n"
            f"📋 工单号: {ticket['ticket_id']}\n"
            f"📊 状态: {status_label}\n"
            f"📝 类型: {ticket['type']}\n"
            f"📄 摘要: {ticket['summary']}\n"
            f"🕐 创建时间: {ticket['created_at']}\n"
            f"🔄 更新时间: {ticket['updated_at']}"
        )

    @trace_agent_call("ticket_handler_process")
    async def process(self, state: dict[str, Any]) -> dict[str, Any]:
        """作为 Graph 节点处理状态"""
        messages = state.get("messages", [])
        user_id = state.get("user_id", "anonymous")

        if not messages:
            return {}

        last_message = messages[-1].content
        ticket_info = await self.analyze_request(last_message)

        action = ticket_info.get("action", "create")
        # 用真值判断而不是 `"ticket_id" in ticket_info`：模型可能返回空字符串或 null，
        # 键存在不代表值可用。
        ticket_id = str(ticket_info.get("ticket_id") or "").strip()

        if action == "create":
            result = await self.create_ticket(ticket_info, user_id)
        elif action == "query" and ticket_id:
            result = await self.query_ticket(ticket_id)
        elif action == "query":
            result = "请提供需要查询的工单号，例如 TK-20261101-ABC123。"
        elif action == "update":
            result = await self.update_ticket(ticket_id, str(ticket_info.get("status") or "").strip())
        elif action == "list":
            result = await self.list_tickets(user_id)
        else:
            # 模型没能给出可执行的指令时，明确说"没听懂"，而不是谎称功能不支持。
            result = (
                "抱歉，我没能理解您的需求。您可以试试："
                "“我要申请退款，原因是买错了产品”、"
                "“查询工单 TK-20261101-ABC123”、"
                "“把工单 TK-20261101-ABC123 标记为处理中”，或者“列出我的工单”。"
            )

        # sub_results 没有 reducer（默认覆盖语义），需要自己把已有结果合并进来，
        # 否则会覆盖掉同一次请求里其他 Agent 写入的结果。
        return {
            "sub_results": {
                **state.get("sub_results", {}),
                "ticket_handler": result,
            },
        }
