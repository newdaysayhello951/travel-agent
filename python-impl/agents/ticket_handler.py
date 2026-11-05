"""
工单处理 Agent — 创建、查询、更新与列出工单

存储由外部注入：默认用进程内的 TicketStore（测试替身、无数据库场景），
真实运行由 persistence.SqlTicketStore 提供数据库持久化。两者接口一致、可直接互换。

注意：本模块直接使用存储实现，尚未通过 tool_server 的 MCP 工具层调用。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from domain.ticket import (
    PRIORITY_LABELS,
    STATUS_LABELS,
    check_transition,
    new_ticket_id,
)
from tracing.otel_config import trace_agent_call


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
    """进程内工单存储。仅用于测试与无数据库场景，重启即丢。"""

    def __init__(self):
        self._tickets: dict[str, dict] = {}

    def create(self, ticket_type: str, priority: str, summary: str,
               details: str, user_id: str) -> dict:
        now = datetime.now().isoformat()
        ticket_id = new_ticket_id()
        ticket = {
            "ticket_id": ticket_id,
            "type": ticket_type,
            "priority": priority,
            "status": "created",
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
        """按领域规则校验并更新状态；返回 (工单, 失败原因)。"""
        ticket = self._tickets.get(ticket_id)
        if ticket is None:
            return None, f"未找到工单号 {ticket_id}"

        error = check_transition(ticket["status"], status)
        if error:
            return None, error

        if ticket["status"] != status:
            ticket["status"] = status
            ticket["updated_at"] = datetime.now().isoformat()
        return ticket, None


class TicketHandlerAgent:
    """
    工单处理 Agent。

    所有工单操作都**经由 MCP 工具层**（`MCPToolServer.call_tool`），不直接接触存储——
    这样把工具实现换成真实工单系统时，本类不需要改动。
    """

    def __init__(self, llm: ChatOpenAI, tool_server: Any | None = None):
        self.llm = llm
        self.tool_server = tool_server

    async def _call_tool(self, name: str, arguments: dict) -> Any:
        """
        调用工具层。

        `authenticated=True`：Agent 只在已鉴权的 HTTP 请求内运行，内部调用视为已鉴权；
        对外的 HTTP 入口（`/api/tools/call`、`/mcp`）仍按真实鉴权结果校验。
        """
        if self.tool_server is None:
            raise RuntimeError("工单 Agent 未接入工具层")
        result = await self.tool_server.call_tool(name, arguments, authenticated=True)
        if not result.success:
            raise RuntimeError(result.error or "工具调用失败")
        return result.result

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
        """通过工具层创建工单"""
        try:
            ticket = await self._call_tool("ticket_create", {
                "ticket_type": ticket_info.get("ticket_type", "general"),
                "priority": ticket_info.get("priority", "medium"),
                "summary": ticket_info.get("summary", ""),
                "details": ticket_info.get("details", ""),
                "user_id": user_id,
            })
        except RuntimeError as exc:
            return f"工单创建失败：{exc}"

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
        """通过工具层查询单个工单"""
        try:
            ticket = await self._call_tool("ticket_query", {"ticket_id": ticket_id})
        except RuntimeError as exc:
            return f"工单查询失败：{exc}"
        return self._format_ticket(ticket, header="工单查询结果：")

    @trace_agent_call("ticket_update")
    async def update_ticket(self, ticket_id: str, status: str) -> str:
        """更新工单状态，非法流转会给出明确原因"""
        if not ticket_id:
            return "请提供需要更新的工单号，例如 TK-20261101-ABC123。"
        if not status:
            return (
                f"请说明要把工单 {ticket_id} 更新为哪种状态，"
                f"可选：{'、'.join(STATUS_LABELS.values())}。"
            )

        try:
            ticket = await self._call_tool(
                "ticket_update", {"ticket_id": ticket_id, "status": status}
            )
        except RuntimeError as exc:
            return f"工单状态更新失败：{exc}"
        return self._format_ticket(ticket, header="工单状态已更新：")

    @trace_agent_call("ticket_list")
    async def list_tickets(self, user_id: str) -> str:
        """列出某个用户的工单"""
        try:
            tickets = await self._call_tool("ticket_list", {"user_id": user_id})
        except RuntimeError as exc:
            return f"工单查询失败：{exc}"
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
            result = await self.update_ticket(
                ticket_id, str(ticket_info.get("status") or "").strip()
            )
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
