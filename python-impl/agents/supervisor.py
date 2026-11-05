"""
Supervisor 编排节点 — 入口调度与出口汇总

图结构（串行流水线）：
    supervisor_route --条件边--> knowledge_rag | ticket_handler
                                              |
                                    两者都 --> compliance_check --> synthesize --> END

入口节点判断意图（优先用结构化输出，网关不支持时回退到文本解析），
出口节点汇总子结果、写入回复，并把超出上下文窗口的旧消息从检查点里移除。
检查点默认使用 MemorySaver，只存在于当前进程内存中。
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Annotated, Any, Literal, TypedDict

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    RemoveMessage,
    SystemMessage,
)
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from pydantic import BaseModel, Field

from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent
from agents.compliance_checker import ComplianceCheckerAgent
from memory.working_memory import WorkingMemory
from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph

logger = logging.getLogger(__name__)


# ─── 状态定义 ───

class AgentState(TypedDict):
    """
    图内共享状态。

    只有 messages 带 reducer（add_messages，追加 + 按 ID 去重）。其余字段是默认的覆盖语义，
    所以每次调用都要在 initial_state 里给出初值，避免上一轮的中间结果串到本轮。
    sub_results 故意不加 reducer：加了会让跨轮的中间结果累积成脏数据。
    等引入并行节点时，再配合显式重置或 RemoveMessage 一起改。
    """
    messages: Annotated[list[BaseMessage], add_messages]
    user_id: str
    session_id: str
    intent: str
    sub_results: dict[str, Any]
    compliance_passed: bool
    final_response: str
    current_agent: str
    retry_count: int


# ─── Supervisor 节点 ───

SUPERVISOR_SYSTEM_PROMPT = """你是一个智能客服系统的 Supervisor（主管编排 Agent）。

你的职责是判断用户当前这句话应该交给哪个子 Agent 处理。

可用的子 Agent：
- knowledge_rag: 知识库检索与问答（产品、政策、流程等咨询）
- ticket_handler: 工单处理（创建、查询、更新、列出工单等办理类请求）

只需给出路由结果，不要把答案写在这里。
"""

VALID_INTENTS = frozenset({"knowledge_rag", "ticket_handler"})
DEFAULT_INTENT = "knowledge_rag"

# 回退路径用：从模型自由文本里抽出合法意图，容忍 Markdown 标记与整句话。
_INTENT_PATTERN = re.compile(r"knowledge_rag|ticket_handler", re.IGNORECASE)


class RouteDecision(BaseModel):
    """路由结果的结构化输出模型，枚举约束由框架保证。"""

    intent: Literal["knowledge_rag", "ticket_handler"] = Field(
        description="该消息应交给哪个 Agent：knowledge_rag=知识库问答，ticket_handler=工单处理"
    )


def _extract_intent(text: str) -> str:
    """回退解析：从自由文本中提取合法意图，提取不到则使用默认值。"""
    match = _INTENT_PATTERN.search(text or "")
    return match.group(0).lower() if match else DEFAULT_INTENT


class SupervisorNode:
    """Supervisor 的两个节点：入口路由、出口汇总"""

    def __init__(
        self,
        llm: ChatOpenAI,
        working_memory: WorkingMemory,
        max_context_messages: int = 20,
        structured_method: str = "function_calling",
    ):
        self.llm = llm
        self.working_memory = working_memory
        self.max_context_messages = max_context_messages
        # 结构化输出优先。必须显式指定 method="function_calling"：
        # with_structured_output 的默认实现走 response_format(json_schema)，而实测所在的
        # 模型网关会以 400 拒绝（要求 messages 里出现 "json" 字样），只有 function_calling 可用。
        # 构造失败不影响启动；运行时若调用失败也会回退到文本解析。
        try:
            self._structured_router: Any = llm.with_structured_output(
                RouteDecision, method=structured_method
            )
        except Exception:
            logger.warning("当前模型不支持结构化输出，路由将直接使用文本解析")
            self._structured_router = None

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> dict[str, Any]:
        """判断意图并写入状态"""
        messages = state["messages"]
        session_id = state.get("session_id", "default")

        context = self.working_memory.get_context(session_id)
        # 只把上下文窗口内的最近若干条消息送进路由判断，避免 prompt 随对话轮数无限增长。
        recent = messages[-self.max_context_messages:]

        routing_prompt = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            SystemMessage(content=f"当前工作记忆上下文: {context}"),
            *recent,
            HumanMessage(content="请判断用户最新这句话应该交给哪个 Agent 处理。"),
        ]

        intent = await self._decide_intent(routing_prompt)
        self.working_memory.update(session_id, {"last_intent": intent})

        return {"intent": intent, "current_agent": "supervisor"}

    async def _decide_intent(self, routing_prompt: list[BaseMessage]) -> str:
        """优先结构化输出；不可用时回退到文本解析 + 白名单兜底。"""
        if self._structured_router is not None:
            try:
                decision = await self._structured_router.ainvoke(routing_prompt)
                intent = getattr(decision, "intent", None)
                if intent in VALID_INTENTS:
                    return intent
                logger.warning("结构化路由返回了非预期值: %r", intent)
            except Exception as exc:
                logger.warning("结构化路由调用失败，回退到文本解析: %s", type(exc).__name__)

        response = await self.llm.ainvoke(routing_prompt)
        return _extract_intent(response.content)

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> dict[str, Any]:
        """汇总子 Agent 的文本结果、写入最终回复，并裁剪过长的消息历史"""
        sub_results = state.get("sub_results", {})
        compliance_passed = state.get("compliance_passed", True)

        if not compliance_passed:
            final_response = (
                "抱歉，此回复未通过自动审查，请联系人工客服进一步核实。"
                "当前版本没有自动转人工或创建审核工单的能力。"
            )
        else:
            result_parts = [
                result for result in sub_results.values()
                if isinstance(result, str) and result
            ]
            final_response = (
                "\n\n".join(result_parts) if result_parts
                else "抱歉，暂时无法处理您的请求，请稍后重试。"
            )

        return {
            "final_response": final_response,
            "messages": [*self._messages_to_remove(state), AIMessage(content=final_response)],
        }

    def _messages_to_remove(self, state: AgentState) -> list[RemoveMessage]:
        """
        计算需要从检查点移除的旧消息。

        本轮的问答各占一条消息，所以按**偶数条**从最前面移除，保证剩下的历史仍是
        完整的「用户提问 + 助手回答」成对结构，不会出现孤立的回答。
        """
        messages = state.get("messages", [])
        total_after_this_turn = len(messages) + 1
        overflow = total_after_this_turn - self.max_context_messages
        if overflow <= 0:
            return []

        if overflow % 2:  # 向上取偶，按整轮丢弃
            overflow += 1
        overflow = min(overflow, len(messages))

        removals = []
        for message in messages[:overflow]:
            message_id = getattr(message, "id", None)
            if message_id:
                removals.append(RemoveMessage(id=message_id))
        if len(removals) != overflow:
            logger.warning("部分历史消息没有 ID，本次只移除了 %d/%d 条", len(removals), overflow)
        return removals


# ─── 路由函数 ───

def route_to_agent(state: AgentState) -> str:
    """根据意图返回下一个节点名；非 ticket_handler 一律走知识问答"""
    return "ticket_handler" if state.get("intent") == "ticket_handler" else "knowledge_rag"


# ─── 构建 Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    ticket_store: Any | None = None,
    checkpointer: Any | None = None,
    enable_checkpointing: bool = True,
    max_context_messages: int = 20,
    structured_method: str = "function_calling",
) -> CompiledStateGraph:
    """
    构建 Supervisor 编排的 StateGraph。

    Args:
        llm: 语言模型实例；为 None 时按根目录 .env 创建
        working_memory: 工作记忆，用于把上下文注入路由判断
        long_term_memory: 长期记忆（知识库），供知识问答 Agent 检索
        ticket_store: 工单存储；不传则用进程内实现（测试场景）
        checkpointer: 图检查点实现；不传且 enable_checkpointing 为真时用 MemorySaver
        enable_checkpointing: 是否启用检查点（checkpointer 已显式传入时此参数被忽略）
        max_context_messages: 历史消息条数上限，超出部分从检查点移除
        structured_method: 结构化输出方式，默认 function_calling

    Returns:
        已编译的图对象，提供 ainvoke / astream 等接口
    """
    if llm is None:
        from settings import create_llm
        llm = create_llm()
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory, max_context_messages, structured_method)

    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory)
    ticket_agent = TicketHandlerAgent(llm, ticket_store)
    compliance_agent = ComplianceCheckerAgent(llm)

    graph = StateGraph(AgentState)

    graph.add_node("supervisor_route", supervisor.route_decision)
    graph.add_node("knowledge_rag", knowledge_agent.process)
    graph.add_node("ticket_handler", ticket_agent.process)
    graph.add_node("compliance_check", compliance_agent.process)
    graph.add_node("synthesize", supervisor.synthesize_response)

    graph.set_entry_point("supervisor_route")

    graph.add_conditional_edges(
        "supervisor_route",
        route_to_agent,
        {
            "knowledge_rag": "knowledge_rag",
            "ticket_handler": "ticket_handler",
        },
    )

    # 两条业务分支都必须经过合规审查，这个约束由图结构强制，不依赖调用方自觉。
    graph.add_edge("knowledge_rag", "compliance_check")
    graph.add_edge("ticket_handler", "compliance_check")
    graph.add_edge("compliance_check", "synthesize")
    graph.add_edge("synthesize", END)

    if checkpointer is None and enable_checkpointing:
        checkpointer = MemorySaver()
    return graph.compile(checkpointer=checkpointer)
