"""
FastAPI 入口 — 本机网页与 REST 接口（非流式）

职责划分：
- 接口层：请求校验（Pydantic）、HTTP 语义映射（503/504/429/502）、生命周期钩子
- 会话层：短期记忆、工作记忆、图检查点的读写与清理

本模块持有进程级共享对象，因此同一进程内所有请求复用同一份模型客户端与同一张编译好的图。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field

from agents.supervisor import create_supervisor_graph
from config import get_settings
from memory.long_term import LongTermMemory
from memory.short_term import ShortTermMemory
from memory.working_memory import WorkingMemory
from settings import create_llm
from tool_server.mcp_server import MCPToolServer, create_default_tools
from tracing.otel_config import AgentMetrics, init_tracer

logger = logging.getLogger(__name__)

settings = get_settings()

# ── 进程级共享对象：只创建一次，所有请求复用 ──
working_memory = WorkingMemory()
short_term_memory = ShortTermMemory(
    redis_url=settings.redis_url,
    max_turns=settings.short_term_max_turns,
    ttl_seconds=settings.short_term_ttl_seconds,
)
long_term_memory = LongTermMemory(index_path=str(settings.knowledge_index_path))
mcp_server = create_default_tools(MCPToolServer())
metrics = AgentMetrics()

# 图在生命周期钩子里才编译；未就绪时接口返回 503
graph = None

# 全局并发闸门：避免本机演示把上游模型网关的额度与频率打爆，
# 代价是吞吐被锁成串行，高并发下排队的请求会超时。
request_gate = asyncio.Semaphore(settings.max_concurrent_requests)

# 示例知识库：启动时装载，当前不落盘，重启后重新装载（add_document 幂等，不会重复）
SAMPLE_KNOWLEDGE: list[tuple[str, str]] = [
    (
        "product_faq.md",
        "我们的理财产品A年化收益率为3.5%-5.2%，投资期限为6个月至3年，最低投资金额10000元。"
        "注意：理财非存款，产品有风险，投资须谨慎。",
    ),
    (
        "refund_policy.md",
        "退款政策：用户在购买后7天内可申请无理由退款，超过7天需提供合理原因。"
        "退款将在3-5个工作日内原路退回。",
    ),
    (
        "account_guide.md",
        "开户流程：1.准备身份证原件 2.填写开户申请表 3.进行视频认证 4.设置交易密码 "
        "5.完成风险评估问卷。整个流程约需15-30分钟。",
    ),
]


@asynccontextmanager
async def lifespan(app: FastAPI):
    """yield 之前是启动准备，之后是关闭清理（当前无需清理的资源）"""
    global graph

    init_tracer(
        service_name=settings.otel_service_name,
        otlp_endpoint=settings.otel_otlp_endpoint,
    )

    # 模型配置缺失会在这里直接抛错，让服务启动失败，而不是等第一个请求进来才返回 502。
    graph = create_supervisor_graph(
        llm=create_llm(),
        working_memory=working_memory,
        long_term_memory=long_term_memory,
    )

    for source, content in SAMPLE_KNOWLEDGE:
        long_term_memory.add_document(content=content, source=source)

    yield


app = FastAPI(
    title="智能客服多 Agent 系统",
    description="基于 LangGraph 编排的多 Agent 智能客服后端",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_allow_origins,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=4000)
    user_id: str = Field(default="anonymous", max_length=100)
    session_id: str | None = Field(default=None, max_length=100)


class ChatResponse(BaseModel):
    response: str
    session_id: str
    intent: str
    compliance_passed: bool


@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest) -> ChatResponse:
    """主聊天接口：一次请求对应一次图执行"""
    if graph is None:
        raise HTTPException(status_code=503, detail="系统初始化中")

    session_id = request.session_id or str(uuid.uuid4())

    await short_term_memory.add_message(session_id, "user", request.message)

    initial_state = {
        "messages": [HumanMessage(content=request.message)],
        "user_id": request.user_id,
        "session_id": session_id,
        "intent": "",
        "sub_results": {},
        "compliance_passed": True,
        "final_response": "",
        "current_agent": "",
        "retry_count": 0,
    }
    config = {"configurable": {"thread_id": session_id}}

    try:
        started = time.monotonic()
        async with asyncio.timeout(settings.request_timeout_seconds):
            async with request_gate:
                result = await graph.ainvoke(initial_state, config=config)
        metrics.record_call("chat", (time.monotonic() - started) * 1000, True)
    except Exception as exc:
        metrics.record_call("chat", (time.monotonic() - started) * 1000, False)
        # 只记录异常类型：第三方异常原文可能包含网关地址与鉴权信息，既不入日志也不回传页面。
        logger.warning("Chat failed: %s", type(exc).__name__)
        if isinstance(exc, TimeoutError):
            raise HTTPException(status_code=504, detail="处理超时，请缩短问题后重试") from None
        if getattr(exc, "status_code", None) == 429:
            raise HTTPException(status_code=429, detail="模型接口额度或频率受限，请稍后再试") from None
        raise HTTPException(status_code=502, detail="模型服务暂时不可用，请稍后再试") from None

    final_response = result.get("final_response", "系统处理异常，请稍后重试")
    await short_term_memory.add_message(session_id, "assistant", final_response)

    return ChatResponse(
        response=final_response,
        session_id=session_id,
        intent=result.get("intent", "unknown"),
        compliance_passed=result.get("compliance_passed", True),
    )


@app.get("/api/history/{session_id}")
async def get_history(session_id: str) -> dict:
    """获取某会话的对话记录（来源：短期记忆）"""
    history = await short_term_memory.get_history(session_id)
    return {"session_id": session_id, "messages": history}


@app.delete("/api/history/{session_id}")
async def clear_session(session_id: str) -> dict:
    """
    清空某会话的全部痕迹：短期记忆 + 工作记忆 + 图检查点。

    只清短期记忆是不够的——模型看到的多轮上下文来自图检查点，
    检查点不清掉的话，新会话仍会被当成老会话的延续。
    """
    await short_term_memory.clear(session_id)
    working_memory.clear(session_id)

    checkpoint_cleared = False
    checkpointer = getattr(graph, "checkpointer", None) if graph is not None else None
    if checkpointer is not None:
        try:
            await checkpointer.adelete_thread(session_id)
            checkpoint_cleared = True
        except Exception:
            logger.warning("清理图检查点失败: session=%s", session_id)

    return {
        "session_id": session_id,
        "cleared": True,
        "checkpoint_cleared": checkpoint_cleared,
    }


@app.get("/api/tools")
async def list_tools() -> dict:
    """工具发现接口（对应 MCP 的 tools/list）"""
    return {"tools": mcp_server.list_tools()}


@app.post("/api/tools/call")
async def call_tool(request: dict) -> dict:
    """工具调用接口（对应 MCP 的 tools/call）"""
    result = await mcp_server.call_tool(
        name=request.get("name", ""),
        arguments=request.get("arguments", {}),
    )
    return {
        "success": result.success,
        "result": result.result,
        "error": result.error,
        "duration_ms": result.duration_ms,
    }


@app.get("/api/metrics")
async def get_metrics() -> dict:
    """系统指标与最近的工具调用日志"""
    return {
        "agent_metrics": metrics.get_summary(),
        "tool_call_log": mcp_server.get_call_log(last_n=20),
    }


@app.get("/")
async def home() -> FileResponse:
    return FileResponse(Path(__file__).resolve().parents[1] / "web/index.html")


@app.get("/health")
async def health_check() -> dict:
    return {"status": "healthy", "version": "1.0.0"}


if __name__ == "__main__":
    import uvicorn

    # 与 run.py 保持一致：默认只监听本机，需要对外暴露时通过 HOST 显式指定。
    uvicorn.run(
        "api.main:app",
        host=settings.host,
        port=settings.port,
        reload=True,
    )
