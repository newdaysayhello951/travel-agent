"""合规修正节点 —— 让图里出现真正的"环"。

原先合规审查不通过就直接返回拒答话术，属于**一次性闸门**。
现在把违规原因回传给模型让它重写，重新送审，最多重试若干次——
这才构成 Anthropic 所说的 **evaluator-optimizer** 模式。

只重写 `sub_results` 里的文本结果，不重跑检索：检索结果本身不是违规来源，
重跑一遍既慢又贵。
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from tracing.otel_config import trace_agent_call

REVISION_SYSTEM_PROMPT = """你是金融客服的合规整改助手。

你会收到一段未通过合规审查的回复，以及具体的违规项。请在不改变原意、
不丢失关键信息的前提下重写这段回复，使其满足合规要求：

1. 删除或改写被标记的违规表述（如"保证收益""零风险"等承诺性用语）
2. 涉及收益、期限等内容必须加"以上信息仅供参考，具体以合同条款为准"
3. 保留原有的关键事实（金额、天数、办理步骤、来源标注等）
4. 不新增原文没有的事实，也不要编造政策

只返回整改后的回复正文，不要解释、不要加任何前后缀。
"""


class RevisionNode:
    """把违规原因回传模型做一次整改"""

    def __init__(self, llm: ChatOpenAI):
        self.llm = llm

    @trace_agent_call("revision")
    async def revise(self, state: dict[str, Any]) -> dict[str, Any]:
        """重写未通过审查的文本结果，并把重试计数加一"""
        sub_results = state.get("sub_results", {}) or {}
        compliance = sub_results.get("compliance") or {}
        violations = compliance.get("violations") or []

        targets = {key: value for key, value in sub_results.items() if isinstance(value, str)}
        retry_count = state.get("retry_count", 0) + 1

        if not targets:
            # 没有可整改的文本（例如只写了结构化结果），直接消耗一次重试避免死循环
            return {"retry_count": retry_count, "current_agent": "revision"}

        reason = "\n".join(f"- {item}" for item in violations) or "- 未通过合规审查"
        revised: dict[str, str] = {}
        for key, text in targets.items():
            messages = [
                SystemMessage(content=REVISION_SYSTEM_PROMPT),
                HumanMessage(content=f"待整改的回复：\n{text}\n\n违规项：\n{reason}"),
            ]
            response = await self.llm.ainvoke(messages)
            revised[key] = response.content

        return {
            "sub_results": {**sub_results, **revised},
            "retry_count": retry_count,
            "current_agent": "revision",
        }
