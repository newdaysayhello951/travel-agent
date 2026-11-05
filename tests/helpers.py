"""测试用公共构件（假模型、状态构造函数与固定响应）。"""

from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage

from agents.supervisor import RouteDecision

COMPLIANCE_PASS = '{"passed": true, "risk_level": "low", "violations": [], "suggestions": []}'
COMPLIANCE_FAIL = (
    '{"passed": false, "risk_level": "high", '
    '"violations": ["包含违规金融用语: 保证收益"], "suggestions": []}'
)
TICKET_CREATE = (
    '{"action": "create", "ticket_type": "refund", "priority": "high", '
    '"summary": "用户申请退款", "details": "买错了产品"}'
)


class _Structured:
    """模拟 `with_structured_output()` 返回的对象"""

    def __init__(self, outer: "FakeLLM"):
        self._outer = outer

    async def ainvoke(self, messages, **kwargs):
        self._outer.structured_calls += 1
        if self._outer.raise_on_structured:
            raise RuntimeError("structured output unavailable")
        return RouteDecision(intent=self._outer.intent)


class FakeLLM:
    """假模型：按提示词内容分发，覆盖图中所有需要模型调用的节点。"""

    def __init__(self, intent: str = "knowledge_rag"):
        self.intent = intent
        self.text_calls = 0
        self.structured_calls = 0
        self.revision_calls = 0
        self.raise_on_structured = False
        self.compliance_json = COMPLIANCE_PASS
        self.ticket_json = TICKET_CREATE
        self.answer = "ANSWER: 购买后 7 天内可申请退款。来源: refund_policy.md"
        self.revised_answer = "REVISED: 购买后 7 天内可申请退款（仅供参考，以合同条款为准）。"
        # 前 N 次合规审查返回不通过，用于测试整改循环
        self.compliance_fail_times = 0

    def with_structured_output(self, schema, **kwargs):
        return _Structured(self)

    async def ainvoke(self, messages, **kwargs):
        text = "\n".join(getattr(m, "content", "") or "" for m in messages)
        if "合规整改助手" in text:
            self.revision_calls += 1
            return AIMessage(content=self.revised_answer)
        if "相关性排序专家" in text:
            return AIMessage(content="0")
        if "改写为更适合关键词检索" in text:
            return AIMessage(content="refund policy window")
        if "金融合规审查Agent" in text:
            if self.compliance_fail_times > 0:
                self.compliance_fail_times -= 1
                return AIMessage(content=COMPLIANCE_FAIL)
            return AIMessage(content=self.compliance_json)
        if "工单处理Agent" in text:
            return AIMessage(content=self.ticket_json)
        if "知识库问答Agent" in text:
            return AIMessage(content=self.answer)
        if "Supervisor" in text:
            self.text_calls += 1
            return AIMessage(content=self.intent)
        return AIMessage(content="unexpected")


def make_state(text: str, user_id: str = "u1", session_id: str = "s1") -> dict:
    """构造一次图调用的初始状态"""
    return {
        "messages": [HumanMessage(content=text)],
        "user_id": user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }


def thread(session_id: str) -> dict:
    """构造 LangGraph 的 config（按 thread_id 存取检查点）"""
    return {"configurable": {"thread_id": session_id}}
