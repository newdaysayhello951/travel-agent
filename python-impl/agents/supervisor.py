"""
Supervisor 编排节点 — 入口调度与出口汇总

图结构（串行流水线）：
    supervisor_route --条件边--> knowledge_rag | ticket_handler
                                              |
                                    两者都 --> compliance_check --> synthesize --> END

入口节点用一次 LLM 调用判断意图（结果受白名单约束），
出口节点汇总子结果并写入最终回复。
检查点默认使用 MemorySaver，只存在于当前进程内存中。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from langgraph.graph import END, StateGraph
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver

from agents.knowledge_rag import KnowledgeRAGAgent
from agents.ticket_handler import TicketHandlerAgent
from agents.compliance_checker import ComplianceCheckerAgent
from memory.working_memory import WorkingMemory
from memory.long_term import LongTermMemory
from tracing.otel_config import trace_agent_call

if TYPE_CHECKING:
    from langgraph.graph.state import CompiledStateGraph


# ─── 状态定义 ───

class AgentState(TypedDict):
    """
    图内共享状态。

    只有 messages 带 reducer（add_messages，追加）。其余字段是默认的覆盖语义，
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

VALID_INTENTS = {"knowledge_rag", "ticket_handler"}
DEFAULT_INTENT = "knowledge_rag"


class SupervisorNode:
    """Supervisor 的两个节点：入口路由、出口汇总"""

    def __init__(self, llm: ChatOpenAI, working_memory: WorkingMemory):
        self.llm = llm
        self.working_memory = working_memory

    @trace_agent_call("supervisor")
    async def route_decision(self, state: AgentState) -> dict[str, Any]:
        """判断意图并写入状态；LLM 输出经白名单兜底"""
        messages = state["messages"]
        session_id = state.get("session_id", "default")

        context = self.working_memory.get_context(session_id)

        routing_prompt = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            SystemMessage(content=f"当前工作记忆上下文: {context}"),
            *messages,
            HumanMessage(content=(
                "请分析用户的最新消息，返回应该路由到的 Agent 名称。"
                "只返回以下之一，不要有其他内容: knowledge_rag, ticket_handler"
            )),
        ]

        response = await self.llm.ainvoke(routing_prompt)
        # 模型输出是不可信输入：可能带 Markdown、整句话或大小写差异，统一归一化后白名单兜底。
        intent = response.content.strip().strip("*` \n").lower()
        if intent not in VALID_INTENTS:
            intent = DEFAULT_INTENT

        self.working_memory.update(session_id, {"last_intent": intent})

        return {"intent": intent, "current_agent": "supervisor"}

    @trace_agent_call("supervisor_synthesize")
    async def synthesize_response(self, state: AgentState) -> dict[str, Any]:
        """汇总子 Agent 的文本结果并写入最终回复"""
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
            "messages": [AIMessage(content=final_response)],
        }


# ─── 路由函数 ───

def route_to_agent(state: AgentState) -> str:
    """根据意图返回下一个节点名；非 ticket_handler 一律走知识问答"""
    return "ticket_handler" if state.get("intent") == "ticket_handler" else "knowledge_rag"


# ─── 构建 Graph ───

def create_supervisor_graph(
    llm: ChatOpenAI | None = None,
    working_memory: WorkingMemory | None = None,
    long_term_memory: LongTermMemory | None = None,
    enable_checkpointing: bool = True,
) -> CompiledStateGraph:
    """
    构建 Supervisor 编排的 StateGraph。

    Args:
        llm: 语言模型实例；为 None 时按根目录 .env 创建
        working_memory: 工作记忆，用于把上下文注入路由判断
        long_term_memory: 长期记忆（知识库），供知识问答 Agent 检索
        enable_checkpointing: 是否挂载检查点；开启后按 thread_id 支持多轮对话

    Returns:
        已编译的图对象，提供 ainvoke / astream 等接口
    """
    if llm is None:
        from settings import create_llm
        llm = create_llm()
    if working_memory is None:
        working_memory = WorkingMemory()

    supervisor = SupervisorNode(llm, working_memory)

    knowledge_agent = KnowledgeRAGAgent(llm, long_term_memory)
    ticket_agent = TicketHandlerAgent(llm)
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

    checkpointer = MemorySaver() if enable_checkpointing else None
    return graph.compile(checkpointer=checkpointer)
