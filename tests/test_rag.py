"""RAG 检索与重排的健壮性（含一个曾经导致"检索不到资料"的静默失败）。"""

from agents.knowledge_rag import KnowledgeRAGAgent
from helpers import FakeLLM


def _docs(count: int) -> list[dict]:
    return [
        {"id": f"d{i}", "content": f"第{i}篇文档内容", "source": f"s{i}.md", "score": 0.5}
        for i in range(count)
    ]


class _RerankLLM(FakeLLM):
    """按预设文本作为重排节点的模型输出"""

    def __init__(self, rerank_output: str):
        super().__init__()
        self.rerank_output = rerank_output

    async def ainvoke(self, messages, **kwargs):
        text = "\n".join(getattr(m, "content", "") or "" for m in messages)
        if "相关性排序专家" in text:
            from langchain_core.messages import AIMessage
            return AIMessage(content=self.rerank_output)
        return await super().ainvoke(messages, **kwargs)


async def test_rerank_keeps_valid_indices():
    agent = KnowledgeRAGAgent(llm=_RerankLLM("1, 0"))
    result = await agent.rerank_documents("q", _docs(3), top_k=3)
    assert [d["id"] for d in result] == ["d1", "d0"]


async def test_rerank_deduplicates_repeated_indices():
    agent = KnowledgeRAGAgent(llm=_RerankLLM("0,0,1"))
    result = await agent.rerank_documents("q", _docs(3), top_k=3)
    assert len(result) == 2


async def test_rerank_falls_back_when_output_unparsable():
    agent = KnowledgeRAGAgent(llm=_RerankLLM("抱歉我无法判断"))
    result = await agent.rerank_documents("q", _docs(3), top_k=2)
    assert [d["id"] for d in result] == ["d0", "d1"]


async def test_rerank_falls_back_when_all_indices_out_of_range():
    """
    回归用例：模型返回的索引**全部越界**时，过滤结果是空列表且不抛异常。
    修复前这里会返回空列表，导致下游当成"没有检索到资料"而输出兜底话术。
    """
    agent = KnowledgeRAGAgent(llm=_RerankLLM("3, 4, 5"))
    result = await agent.rerank_documents("q", _docs(2), top_k=2)
    assert [d["id"] for d in result] == ["d0", "d1"], result


async def test_rerank_handles_negative_indices():
    """负数索引在 Python 里是合法下标，必须被过滤掉而不是取到末尾文档"""
    agent = KnowledgeRAGAgent(llm=_RerankLLM("-1"))
    result = await agent.rerank_documents("q", _docs(3), top_k=1)
    assert [d["id"] for d in result] == ["d0"], result


async def test_rerank_returns_empty_for_empty_input():
    agent = KnowledgeRAGAgent(llm=_RerankLLM("0"))
    assert await agent.rerank_documents("q", [], top_k=3) == []


async def test_generate_answer_without_documents_does_not_call_model():
    """没有检索结果时不应调用模型（省钱且从根本上消除编造）"""

    class _Exploding(FakeLLM):
        async def ainvoke(self, messages, **kwargs):
            if "知识库问答Agent" in "\n".join(
                getattr(m, "content", "") or "" for m in messages
            ):
                raise AssertionError("不应该在有检索结果为空时调用模型")
            return await super().ainvoke(messages, **kwargs)

    agent = KnowledgeRAGAgent(llm=_Exploding())
    answer = await agent.generate_answer("q", [])
    assert "暂未找到" in answer
