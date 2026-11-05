"""
MCP 工具协议服务端 — Model Context Protocol 的「工具子集」

实现了 MCP 中与 tools 相关的部分：工具注册、工具发现（tools/list）、工具调用（tools/call），
消息格式遵循 JSON-RPC 2.0，工具入参用 JSON Schema 声明，错误码使用协议标准值。

与完整 MCP 实现的差距（当前边界）：
- 是进程内直接函数调用，没有 stdio / HTTP 传输层
- 没有 initialize 握手与 capabilities 协商
- 未实现 resources、prompts 两类能力
- `requires_auth` 字段已声明，但调用链上尚未校验

另外两点现实情况：`order_query` 返回的是演示数据；工单类工具已接入真实存储，
知识检索工具已接入知识库（由调用方注入，未注入时退化为演示数据）。
业务层（agents/ticket_handler.py）现在**经由本层调用工具**，不再直接操作存储。
"""

from __future__ import annotations

import json
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable
from datetime import datetime


def validate_arguments(schema: dict[str, Any], arguments: dict[str, Any]) -> str | None:
    """
    按 input_schema 做最小必要校验：required、type、enum。

    只实现本项目实际用到的 JSON Schema 子集，不做完整规范实现。
    返回 None 表示通过，否则返回拒绝原因。
    """
    if not isinstance(arguments, dict):
        return "arguments 必须是对象"

    properties = schema.get("properties", {}) or {}
    for name in schema.get("required", []) or []:
        if name not in arguments or arguments[name] in (None, ""):
            return f"缺少必填参数: {name}"

    type_map = {
        "string": str, "integer": int, "number": (int, float),
        "boolean": bool, "array": list, "object": dict,
    }
    for name, value in arguments.items():
        spec = properties.get(name)
        if spec is None:
            continue  # 未声明的参数交给 handler 的默认值处理
        expected = spec.get("type")
        python_type = type_map.get(expected)
        if python_type is not None:
            # bool 是 int 的子类，需要单独拦掉，否则 True 会被当成合法的整数
            if isinstance(value, bool) and expected in ("integer", "number"):
                return f"参数 {name} 期望 {expected}，实际是 boolean"
            if not isinstance(value, python_type):
                return f"参数 {name} 期望 {expected}，实际是 {type(value).__name__}"
        allowed = spec.get("enum")
        if allowed and value not in allowed:
            return f"参数 {name} 取值必须是 {allowed} 之一"
    return None


@dataclass
class ToolDefinition:
    """MCP工具定义"""
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]
    category: str = "general"
    requires_auth: bool = False


@dataclass
class ToolCallResult:
    """工具调用结果"""
    tool_name: str
    success: bool
    result: Any = None
    error: str | None = None
    duration_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())


class MCPToolServer:
    """
    MCP 工具服务端（进程内实现）。

    能力：
    1. 工具注册 (Tool Registration) —— 直接调用或装饰器声明
    2. 工具发现 (Tool Discovery) —— 对应协议方法 tools/list
    3. 工具调用 (Tool Invocation) —— 对应协议方法 tools/call
    4. 结果与调用日志 (Result & Call Log)

    协议层面使用 JSON-RPC 2.0 消息格式、inputSchema 声明与标准错误码，
    但没有传输层，`handle_jsonrpc()` 目前也没有挂到任何 HTTP 路由上。
    """

    def __init__(self, max_call_log: int = 200):
        self._tools: dict[str, ToolDefinition] = {}
        # 用固定长度队列，调用量再大也不会无限占用内存
        self._call_log: deque[ToolCallResult] = deque(maxlen=max_call_log)

    def register_tool(self, tool: ToolDefinition) -> None:
        """注册一个MCP工具"""
        self._tools[tool.name] = tool

    def register(
        self,
        name: str,
        description: str,
        input_schema: dict[str, Any],
        category: str = "general",
        requires_auth: bool = False,
    ) -> Callable:
        """工具注册装饰器"""
        def decorator(func: Callable[..., Awaitable[Any]]) -> Callable:
            tool = ToolDefinition(
                name=name,
                description=description,
                input_schema=input_schema,
                handler=func,
                category=category,
                requires_auth=requires_auth,
            )
            self._tools[name] = tool
            return func
        return decorator

    def list_tools(self, category: str | None = None) -> list[dict]:
        """
        工具发现：列出所有可用工具。
        对应MCP的 tools/list 方法。
        """
        tools = []
        for tool in self._tools.values():
            if category and tool.category != category:
                continue
            tools.append({
                "name": tool.name,
                "description": tool.description,
                "inputSchema": tool.input_schema,
                "category": tool.category,
            })
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        *,
        authenticated: bool = False,
    ) -> ToolCallResult:
        """
        工具调用，对应 MCP 的 tools/call。

        `authenticated` 表示调用方已通过鉴权。**默认 False 是安全默认值**：
        声明了 `requires_auth` 的工具，必须由已鉴权的调用方显式声明才能执行。
        """
        tool = self._tools.get(name)
        if tool is None:
            return self._record(ToolCallResult(
                tool_name=name,
                success=False,
                error=f"Tool '{name}' not found. Available: {list(self._tools.keys())}",
            ))

        if tool.requires_auth and not authenticated:
            return self._record(ToolCallResult(
                tool_name=name,
                success=False,
                error=f"工具 {name} 需要鉴权后才能调用",
            ))

        invalid = validate_arguments(tool.input_schema, arguments)
        if invalid:
            return self._record(ToolCallResult(
                tool_name=name, success=False, error=f"入参校验失败: {invalid}",
            ))

        start = time.perf_counter()
        try:
            output = await tool.handler(**arguments)
            return self._record(ToolCallResult(
                tool_name=name,
                success=True,
                result=output,
                duration_ms=(time.perf_counter() - start) * 1000,
            ))
        except Exception as exc:
            return self._record(ToolCallResult(
                tool_name=name,
                success=False,
                error=str(exc),
                duration_ms=(time.perf_counter() - start) * 1000,
            ))

    def _record(self, result: ToolCallResult) -> ToolCallResult:
        """记录调用日志（底层是固定长度队列）并返回结果"""
        self._call_log.append(result)
        return result

    async def handle_jsonrpc(self, request: dict, *, authenticated: bool = False) -> dict:
        """
        处理JSON-RPC 2.0请求。MCP协议传输层实现，挂在 POST /mcp 上。
        """
        method = request.get("method", "")
        params = request.get("params", {}) or {}
        req_id = request.get("id", 1)

        try:
            if method == "tools/list":
                result = self.list_tools(category=params.get("category"))
            elif method == "tools/call":
                tool_name = params.get("name", "")
                arguments = params.get("arguments", {})
                call_result = await self.call_tool(
                    tool_name, arguments, authenticated=authenticated
                )
                result = {
                    "success": call_result.success,
                    "result": call_result.result,
                    "error": call_result.error,
                }
            elif method == "ping":
                result = {"status": "ok"}
            else:
                return {
                    "jsonrpc": "2.0",
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                    "id": req_id,
                }

            return {"jsonrpc": "2.0", "result": result, "id": req_id}

        except Exception as e:
            return {
                "jsonrpc": "2.0",
                "error": {"code": -32603, "message": str(e)},
                "id": req_id,
            }

    def get_call_log(self, last_n: int = 100) -> list[dict]:
        """获取最近的工具调用日志（底层是固定长度队列，最多保留 max_call_log 条）"""
        return [
            {
                "tool": r.tool_name,
                "success": r.success,
                "duration_ms": r.duration_ms,
                "timestamp": r.timestamp,
                "error": r.error,
            }
            for r in list(self._call_log)[-last_n:]
        ]


def create_default_tools(
    server: MCPToolServer,
    ticket_store: Any | None = None,
    long_term_memory: Any | None = None,
) -> MCPToolServer:
    """
    注册工具集。

    `ticket_store` 与 `long_term_memory` 由调用方注入：传入真实实现时，工具直接操作
    数据库与知识库；不传则退化为演示数据（便于测试与无外部依赖启动）。
    工单类工具声明了 `requires_auth`，只有已鉴权的调用方才能执行。
    """

    @server.register(
        name="order_query",
        description="查询订单信息（演示数据，未接入真实订单系统）",
        input_schema={
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "订单号"},
                "user_id": {"type": "string", "description": "用户ID"},
            },
        },
        category="order",
    )
    async def order_query(order_id: str = "", user_id: str = "") -> dict:
        return {
            "order_id": order_id or "ORD-20260401-001",
            "status": "shipped",
            "amount": 299.00,
            "product": "智能理财产品A",
            "created_at": "2026-04-01T10:00:00",
        }

    @server.register(
        name="knowledge_search",
        description="搜索企业知识库，返回相关文档片段",
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "top_k": {"type": "integer", "description": "返回数量", "default": 3},
            },
            "required": ["query"],
        },
        category="knowledge",
    )
    async def knowledge_search(query: str, top_k: int = 3) -> list[dict]:
        if long_term_memory is None:
            return [{"content": f"关于'{query}'的演示文档片段", "source": "demo.md", "score": 0.0}]
        return [
            {
                "content": doc["content"],
                "source": doc.get("source", ""),
                "score": doc.get("score", 0.0),
            }
            for doc in long_term_memory.search(query, top_k=top_k)
        ]

    @server.register(
        name="ticket_create",
        description="创建客服工单，返回工单号与当前状态",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_type": {
                    "type": "string",
                    "description": "工单类型：refund/claim/account_open/account_change/complaint/general",
                },
                "priority": {"type": "string", "enum": ["low", "medium", "high", "urgent"]},
                "summary": {"type": "string", "description": "工单摘要"},
                "details": {"type": "string", "description": "详细描述"},
                "user_id": {"type": "string", "description": "提交工单的用户"},
            },
            "required": ["ticket_type", "summary", "user_id"],
        },
        category="ticket",
        requires_auth=True,
    )
    async def ticket_create(ticket_type: str, summary: str, user_id: str,
                            priority: str = "medium", details: str = "") -> dict:
        if ticket_store is None:
            raise RuntimeError("未配置工单存储，无法创建工单")
        return ticket_store.create(ticket_type, priority, summary, details, user_id)

    @server.register(
        name="ticket_query",
        description="按工单号查询工单详情与状态",
        input_schema={
            "type": "object",
            "properties": {"ticket_id": {"type": "string", "description": "工单号"}},
            "required": ["ticket_id"],
        },
        category="ticket",
        requires_auth=True,
    )
    async def ticket_query(ticket_id: str) -> dict:
        if ticket_store is None:
            raise RuntimeError("未配置工单存储，无法查询工单")
        ticket = ticket_store.query(ticket_id)
        if ticket is None:
            raise ValueError(f"未找到工单号 {ticket_id}")
        return ticket

    @server.register(
        name="ticket_update",
        description="更新工单状态，非法状态流转会被拒绝",
        input_schema={
            "type": "object",
            "properties": {
                "ticket_id": {"type": "string"},
                "status": {"type": "string", "enum": ["processing", "resolved", "closed"]},
            },
            "required": ["ticket_id", "status"],
        },
        category="ticket",
        requires_auth=True,
    )
    async def ticket_update(ticket_id: str, status: str) -> dict:
        if ticket_store is None:
            raise RuntimeError("未配置工单存储，无法更新工单")
        ticket, error = ticket_store.update_status(ticket_id, status)
        if error:
            raise ValueError(error)
        return ticket

    @server.register(
        name="ticket_list",
        description="列出某个用户的全部工单",
        input_schema={
            "type": "object",
            "properties": {"user_id": {"type": "string"}},
            "required": ["user_id"],
        },
        category="ticket",
        requires_auth=True,
    )
    async def ticket_list(user_id: str) -> list[dict]:
        if ticket_store is None:
            raise RuntimeError("未配置工单存储，无法列出工单")
        return ticket_store.query_by_user(user_id)

    @server.register(
        name="risk_check",
        description="风控接口 — 检查交易/操作的风险等级",
        input_schema={
            "type": "object",
            "properties": {
                "user_id": {"type": "string"},
                "action": {"type": "string"},
                "amount": {"type": "number"},
            },
            "required": ["user_id", "action"],
        },
        category="compliance",
    )
    async def risk_check(user_id: str, action: str, amount: float = 0.0) -> dict:
        risk_level = "low"
        if amount > 50000:
            risk_level = "high"
        elif amount > 10000:
            risk_level = "medium"

        return {
            "user_id": user_id,
            "action": action,
            "risk_level": risk_level,
            "requires_manual_review": risk_level == "high",
        }

    return server
