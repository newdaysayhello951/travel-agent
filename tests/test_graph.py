"""图编排：结构、路由、合规闸门、上下文窗口、多轮对话。"""

from helpers import COMPLIANCE_FAIL, make_state, thread


def _nodes(graph) -> list[str]:
    return sorted(n for n in graph.get_graph().nodes if not n.startswith("__"))


def test_graph_structure(graph):
    """节点与条件边必须与文档一致：6 个节点，路由 2 个去向，合规后 2 个去向"""
    assert _nodes(graph) == [
        "compliance_check", "knowledge_rag", "revise", "supervisor_route",
        "synthesize", "ticket_handler",
    ]
    targets = sorted(
        e.target for e in graph.get_graph().edges if getattr(e, "conditional", False)
    )
    assert targets == ["knowledge_rag", "revise", "synthesize", "ticket_handler"]


async def test_routes_to_knowledge(graph, fake_llm):
    fake_llm.intent = "knowledge_rag"
    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-knowledge"))
    assert result["intent"] == "knowledge_rag"
    assert "knowledge_rag" in result["sub_results"]
    assert result["compliance_passed"] is True
    assert "7 天内可申请退款" in result["final_response"]


async def test_structured_routing_avoids_text_model(graph, fake_llm):
    """结构化路由可用时，不应该再调用一次文本模型来解析路由"""
    await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-structured"))
    assert fake_llm.structured_calls >= 1
    assert fake_llm.text_calls == 0


async def test_falls_back_to_text_parsing_when_structured_fails(graph, fake_llm):
    """结构化输出不可用时回退到文本解析，并去掉 Markdown 标记"""
    fake_llm.raise_on_structured = True
    fake_llm.intent = "**knowledge_rag** 根据问题判断"
    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-fallback"))
    assert result["intent"] == "knowledge_rag"
    assert fake_llm.text_calls == 1


async def test_unparsable_intent_uses_default(graph, fake_llm):
    fake_llm.raise_on_structured = True
    fake_llm.intent = "我不确定该给谁"
    result = await graph.ainvoke(make_state("???"), config=thread("t-default"))
    assert result["intent"] == "knowledge_rag"


async def test_routes_to_ticket(graph, fake_llm):
    fake_llm.intent = "ticket_handler"
    result = await graph.ainvoke(make_state("我要退款", user_id="u-t"), config=thread("t-ticket"))
    assert result["intent"] == "ticket_handler"
    assert "工单已创建成功" in result["final_response"]


async def test_compliance_failure_blocks_content(graph, fake_llm):
    """合规不通过时必须用固定话术替换正文，不能让原内容流出"""
    fake_llm.compliance_json = COMPLIANCE_FAIL
    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-compliance-fail"))
    assert result["compliance_passed"] is False
    assert "未通过自动审查" in result["final_response"]
    assert "7 天内可申请退款" not in result["final_response"]


async def test_compliance_parse_error_is_fail_closed(graph, fake_llm):
    """审查结果解析失败必须判定为不通过（fail-closed），而不是默认放行"""
    fake_llm.compliance_json = "这不是 JSON"
    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-compliance-parse"))
    assert result["compliance_passed"] is False
    assert "未通过自动审查" in result["final_response"]


async def test_context_window_keeps_history_bounded_and_paired(graph):
    """消息历史受窗口限制，且裁剪后仍是「问答成对」结构"""
    lengths = []
    result = None
    for index in range(5):
        result = await graph.ainvoke(
            make_state(f"第{index + 1}轮提问"), config=thread("t-window")
        )
        lengths.append(len(result["messages"]))

    assert lengths == [2, 4, 6, 6, 6], lengths  # 不裁剪的话最后一轮会是 10 条
    roles = [type(m).__name__ for m in result["messages"]]
    assert roles == ["HumanMessage", "AIMessage"] * 3, roles
    assert all(getattr(m, "id", None) for m in result["messages"])


async def test_multi_turn_accumulates_messages(graph):
    first = await graph.ainvoke(make_state("第一问"), config=thread("t-multi"))
    second = await graph.ainvoke(make_state("第二问"), config=thread("t-multi"))
    assert len(first["messages"]) == 2
    assert len(second["messages"]) == 4


async def test_sub_results_reset_between_turns(graph, fake_llm):
    """sub_results 没有 reducer，必须每轮重置，不能把上一轮结果带进来"""
    fake_llm.intent = "knowledge_rag"
    await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-reset"))
    fake_llm.intent = "ticket_handler"
    second = await graph.ainvoke(make_state("我要退款"), config=thread("t-reset"))
    assert "ticket_handler" in second["sub_results"]
    assert "knowledge_rag" not in second["sub_results"]


async def test_empty_message_list_is_safe(graph):
    """节点收到空消息列表时返回空增量，不应抛异常"""
    state = make_state("x")
    state["messages"] = []
    result = await graph.ainvoke(state, config=thread("t-empty"))
    assert result["final_response"]
