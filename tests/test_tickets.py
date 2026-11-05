"""工单领域规则、工具层与 Agent 的工具调用路径。"""

import pytest

from agents.ticket_handler import TicketHandlerAgent
from domain.ticket import ALLOWED_TRANSITIONS, TicketStatus, check_transition, new_ticket_id
from helpers import make_state  # noqa: F401  （保持 helpers 可导入）
from langchain_core.messages import HumanMessage
from tool_server.mcp_server import validate_arguments


# ── 领域规则 ──

def test_ticket_id_format():
    ticket_id = new_ticket_id()
    assert ticket_id.startswith("TK-")
    assert len(ticket_id.split("-")) == 3
    assert len(ticket_id.split("-")[2]) == 6


def test_legal_transitions():
    assert check_transition("created", "processing") is None
    assert check_transition("processing", "resolved") is None
    assert check_transition("resolved", "closed") is None
    assert check_transition("created", "closed") is None


def test_illegal_transitions_are_rejected_with_reason():
    reason = check_transition("processing", "created")
    assert reason and "不允许" in reason
    assert check_transition("closed", "processing")
    assert check_transition("created", "resolved")


def test_transition_to_same_status_is_idempotent():
    assert check_transition("created", "created") is None


def test_unknown_status_rejected():
    assert check_transition("created", "pending_review")
    assert check_transition("nonsense", "closed")


def test_closed_is_terminal():
    assert ALLOWED_TRANSITIONS[TicketStatus.CLOSED] == set()


# ── 存储实现 ──

def test_memory_store_crud(ticket_store):
    ticket = ticket_store.create("refund", "high", "买错了", "", "u1")
    assert ticket_store.query(ticket["ticket_id"])["status"] == "created"
    assert len(ticket_store.query_by_user("u1")) == 1

    updated, error = ticket_store.update_status(ticket["ticket_id"], "processing")
    assert error is None and updated["status"] == "processing"

    _, error = ticket_store.update_status(ticket["ticket_id"], "created")
    assert error is not None
    assert ticket_store.query("TK-NOPE") is None


# ── 工具层 ──

@pytest.mark.parametrize("schema,arguments,expected_ok", [
    ({"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]}, {}, False),
    ({"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": "x"}, False),
    ({"type": "object", "properties": {"a": {"type": "integer"}}}, {"a": True}, False),
    ({"type": "object", "properties": {"a": {"type": "string", "enum": ["x"]}}}, {"a": "y"}, False),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": "ok"}, True),
    ({"type": "object", "properties": {"a": {"type": "string"}}}, {"a": "ok", "extra": 1}, True),
])
def test_schema_validation(schema, arguments, expected_ok):
    assert (validate_arguments(schema, arguments) is None) is expected_ok


async def test_tool_requires_auth(tool_server):
    denied = await tool_server.call_tool(
        "ticket_create", {"ticket_type": "refund", "summary": "s", "user_id": "u1"}
    )
    assert denied.success is False and "鉴权" in denied.error

    allowed = await tool_server.call_tool(
        "ticket_create", {"ticket_type": "refund", "summary": "s", "user_id": "u1"},
        authenticated=True,
    )
    assert allowed.success is True


async def test_tool_rejects_missing_required_argument(tool_server):
    result = await tool_server.call_tool("ticket_create", {"summary": "s"}, authenticated=True)
    assert result.success is False and "必填" in result.error


async def test_tool_rejects_enum_violation_before_business_logic(tool_server, ticket_store):
    ticket = ticket_store.create("refund", "low", "s", "", "u1")
    result = await tool_server.call_tool(
        "ticket_update", {"ticket_id": ticket["ticket_id"], "status": "created"},
        authenticated=True,
    )
    assert result.success is False
    assert ticket_store.query(ticket["ticket_id"])["status"] == "created"


async def test_unknown_tool_is_reported(tool_server):
    result = await tool_server.call_tool("nope", {})
    assert result.success is False and "not found" in result.error


async def test_jsonrpc_methods(tool_server):
    assert (await tool_server.handle_jsonrpc({"method": "ping"}))["result"] == {"status": "ok"}
    listing = await tool_server.handle_jsonrpc({"method": "tools/list"})
    assert any(t["name"] == "ticket_create" for t in listing["result"])
    unknown = await tool_server.handle_jsonrpc({"method": "nope"})
    assert unknown["error"]["code"] == -32601


async def test_jsonrpc_respects_requires_auth(tool_server):
    payload = {"method": "tools/call", "params": {
        "name": "ticket_create",
        "arguments": {"ticket_type": "general", "summary": "s", "user_id": "u"},
    }}
    assert (await tool_server.handle_jsonrpc(payload))["result"]["success"] is False
    assert (await tool_server.handle_jsonrpc(payload, authenticated=True))["result"]["success"] is True


async def test_knowledge_search_uses_real_index(tool_server):
    result = await tool_server.call_tool("knowledge_search", {"query": "退款 几天"})
    assert result.success is True
    assert any(hit["source"] == "refund_policy.md" for hit in result.result)


# ── Agent 走工具层 ──

async def _run_agent(agent, payload, user_id="u1"):
    async def fake_analyze(message):
        return payload
    agent.analyze_request = fake_analyze
    return await agent.process({
        "messages": [HumanMessage("x")], "user_id": user_id, "sub_results": {},
    })


async def test_agent_uses_tool_layer_for_all_operations(tool_server, ticket_store):
    agent = TicketHandlerAgent(llm=object(), tool_server=tool_server)
    created = await _run_agent(agent, {
        "action": "create", "ticket_type": "refund", "priority": "high",
        "summary": "退款", "details": "",
    })
    ticket_id = ticket_store.query_by_user("u1")[0]["ticket_id"]

    await _run_agent(agent, {"action": "query", "ticket_id": ticket_id})
    await _run_agent(agent, {"action": "update", "ticket_id": ticket_id, "status": "processing"})
    await _run_agent(agent, {"action": "list"})

    called = {entry["tool"] for entry in tool_server.get_call_log(last_n=20)}
    assert called == {"ticket_create", "ticket_query", "ticket_update", "ticket_list"}, called
    assert "工单已创建成功" in created["sub_results"]["ticket_handler"]
    assert ticket_store.query(ticket_id)["status"] == "processing"


async def test_agent_reports_when_not_wired_to_tool_layer():
    agent = TicketHandlerAgent(llm=object())
    result = await _run_agent(agent, {"action": "list"})
    assert "未接入工具层" in result["sub_results"]["ticket_handler"]


@pytest.mark.parametrize("payload,expected", [
    ({"action": "query", "ticket_id": ""}, "请提供需要查询的工单号"),
    ({"action": "query", "ticket_id": None}, "请提供需要查询的工单号"),
    ({"action": "unknown"}, "我没能理解您的需求"),
])
async def test_agent_edge_cases(tool_server, payload, expected):
    agent = TicketHandlerAgent(llm=object(), tool_server=tool_server)
    result = await _run_agent(agent, payload)
    assert expected in result["sub_results"]["ticket_handler"]


async def test_agent_merges_sub_results_instead_of_overwriting(tool_server):
    """sub_results 没有 reducer，节点必须自己合并，否则会覆盖其他 Agent 的结果"""
    agent = TicketHandlerAgent(llm=object(), tool_server=tool_server)
    async def fake_analyze(message):
        return {"action": "list"}
    agent.analyze_request = fake_analyze
    original = {"knowledge_rag": "prev"}
    snapshot = dict(original)
    result = await agent.process({
        "messages": [HumanMessage("x")], "user_id": "u1", "sub_results": original,
    })
    assert "knowledge_rag" in result["sub_results"]
    assert original == snapshot  # 不能原地修改入参
