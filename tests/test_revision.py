"""合规修正循环（evaluator-optimizer）：图里第一次出现「环」。"""

from agents.supervisor import create_supervisor_graph
from helpers import COMPLIANCE_FAIL, make_state, thread
from memory.long_term import LongTermMemory
from memory.working_memory import WorkingMemory


def build_graph(fake_llm, tool_server, tmp_dir, retries: int = 1):
    memory = LongTermMemory(index_path=tmp_dir / "index")
    memory.add_document("退款政策：购买后 7 天内可申请无理由退款。", source="refund_policy.md")
    return create_supervisor_graph(
        llm=fake_llm,
        working_memory=WorkingMemory(),
        long_term_memory=memory,
        tool_server=tool_server,
        max_context_messages=6,
        max_compliance_retries=retries,
    )


def test_graph_contains_a_cycle(graph):
    """合规审查后有一条回到整改节点的边，整改后再回到合规审查——构成环"""
    edges = {(e.source, e.target) for e in graph.get_graph().edges}
    assert ("compliance_check", "revise") in edges
    assert ("revise", "compliance_check") in edges


async def test_failed_compliance_is_revised_then_passes(fake_llm, tool_server, tmp_dir):
    """首次审查不通过 → 整改 → 复审通过 → 输出整改后的内容（而不是直接拒答）"""
    fake_llm.compliance_fail_times = 1
    graph = build_graph(fake_llm, tool_server, tmp_dir, retries=1)

    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-revise-ok"))

    assert fake_llm.revision_calls == 1
    assert result["retry_count"] == 1
    assert result["compliance_passed"] is True
    assert "REVISED" in result["final_response"]
    assert "未通过自动审查" not in result["final_response"]


async def test_gives_up_after_max_retries(fake_llm, tool_server, tmp_dir):
    """整改后仍不通过时，达到重试上限就拒答，不会无限循环"""
    fake_llm.compliance_fail_times = 99
    graph = build_graph(fake_llm, tool_server, tmp_dir, retries=2)

    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-revise-giveup"))

    assert fake_llm.revision_calls == 2
    assert result["retry_count"] == 2
    assert result["compliance_passed"] is False
    assert "未通过自动审查" in result["final_response"]


async def test_no_revision_when_retries_disabled(fake_llm, tool_server, tmp_dir):
    """重试额度为 0 时行为与改造前一致：直接拒答"""
    fake_llm.compliance_fail_times = 99
    graph = build_graph(fake_llm, tool_server, tmp_dir, retries=0)

    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-no-revise"))

    assert fake_llm.revision_calls == 0
    assert result["compliance_passed"] is False
    assert "未通过自动审查" in result["final_response"]


async def test_revision_reports_violations_to_the_model(fake_llm, tool_server, tmp_dir):
    """整改请求里必须带上具体违规项，否则模型不知道要改什么"""
    captured: list[str] = []

    original = fake_llm.ainvoke

    async def spy(messages, **kwargs):
        captured.append("\n".join(getattr(m, "content", "") or "" for m in messages))
        return await original(messages, **kwargs)

    fake_llm.ainvoke = spy
    fake_llm.compliance_fail_times = 1
    graph = build_graph(fake_llm, tool_server, tmp_dir, retries=1)
    await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-revise-prompt"))

    revision_prompts = [text for text in captured if "合规整改助手" in text]
    assert revision_prompts
    assert "违规项" in revision_prompts[0]
    assert "保证收益" in revision_prompts[0]


async def test_compliance_pass_skips_revision(fake_llm, tool_server, tmp_dir):
    graph = build_graph(fake_llm, tool_server, tmp_dir, retries=2)
    result = await graph.ainvoke(make_state("退款政策是什么"), config=thread("t-no-fail"))
    assert fake_llm.revision_calls == 0
    assert result["retry_count"] == 0
    assert result["compliance_passed"] is True


async def test_revision_without_text_results_still_terminates(tool_server, tmp_dir):
    """没有可整改文本时不能死循环：仍然消耗重试额度并收尾"""
    from helpers import COMPLIANCE_FAIL, FakeLLM

    class NoTextLLM(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            text = "\n".join(getattr(m, "content", "") or "" for m in messages)
            if "金融合规审查Agent" in text:
                return __import__("langchain_core.messages", fromlist=["AIMessage"]).AIMessage(
                    content=COMPLIANCE_FAIL
                )
            return await super().ainvoke(messages, **kwargs)

    llm = NoTextLLM(intent="ticket_handler")
    llm.ticket_json = '{"action": "unknown"}'
    graph = build_graph(llm, tool_server, tmp_dir, retries=1)
    result = await graph.ainvoke(make_state("???"), config=thread("t-revise-notext"))
    assert result["compliance_passed"] is False
    assert result["final_response"]
