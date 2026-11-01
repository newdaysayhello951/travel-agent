"""每次运行独立计数，避免缓存 Agent 在不同请求之间共享调用预算。"""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass

from langchain.agents.middleware import wrap_tool_call
from langchain_core.messages import ToolMessage


@dataclass
class ExecutionBudget:
    attempts: int = 0


current_budget: ContextVar[ExecutionBudget | None] = ContextVar('travel_budget', default=None)


class ExecutionLimitError(RuntimeError):
    pass


async def call_with_retry(handler, request, *, timeout, retries, max_calls):
    budget = current_budget.get()
    if budget is None:
        raise ExecutionLimitError('工具调用缺少执行上下文')
    for attempt in range(retries + 1):
        if budget.attempts >= max_calls:
            raise ExecutionLimitError('已达到工具调用次数上限，请缩小规划范围')
        budget.attempts += 1
        try:
            return await asyncio.wait_for(handler(request), timeout)
        except (TimeoutError, ConnectionError, OSError):
            if attempt == retries:
                return ToolMessage(content='工具暂时不可用：超时或网络故障。不得编造结果，请说明缺失信息。', tool_call_id=request.tool_call['id'], status='error')
            await asyncio.sleep(min(0.25 * 2**attempt, 1))


def tool_reliability(timeout, retries, max_calls):
    @wrap_tool_call
    async def guarded(request, handler):
        return await call_with_retry(handler, request, timeout=timeout, retries=retries, max_calls=max_calls)
    return guarded
