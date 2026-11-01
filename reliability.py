"""每次运行独立计数，避免缓存 Agent 在不同请求之间共享调用预算。"""
import asyncio
import json
from uuid import uuid4
import httpx
from contextvars import ContextVar
from dataclasses import dataclass, field

from langchain.agents.middleware import wrap_tool_call, wrap_model_call
from langchain_core.messages import ToolMessage, SystemMessage


@dataclass
class ExecutionBudget:
    attempts: int = 0
    model_calls: int = 0
    cache: dict = field(default_factory=dict)
    cache_hits: int = 0
    finalization_forced: bool = False


@dataclass
class RunReport:
    run_id: str = field(default_factory=lambda: uuid4().hex)
    status: str = 'created'
    attempts: list[dict] = field(default_factory=list)
    tool_attempts: int = 0
    model_calls: int = 0
    cache_hits: int = 0
    finalization_forced: bool = False
    seconds: float = 0


current_budget: ContextVar[ExecutionBudget | None] = ContextVar('travel_budget', default=None)


class ExecutionLimitError(RuntimeError):
    pass


class PlanningServiceError(RuntimeError):
    """对外不暴露包含URL、凭据的原始SDK异常。"""


def transient_error(exc):
    if isinstance(exc, ExceptionGroup):
        return bool(exc.exceptions) and all(transient_error(e) for e in exc.exceptions)
    return isinstance(exc, (TimeoutError, ConnectionError, OSError, httpx.TimeoutException, httpx.NetworkError))


def safe_tool_error(request):
    return ToolMessage(content='工具暂时不可用，未取得有效数据。不得编造结果，请说明缺失信息。',
                       tool_call_id=request.tool_call['id'], status='error')


def reserve_model_call(max_calls):
    budget = current_budget.get()
    if budget is None or budget.model_calls >= max_calls:
        raise ExecutionLimitError('已达到模型调用上限，修正也计入本次预算')
    budget.model_calls += 1


def model_reliability(max_calls, max_tool_calls=24):
    @wrap_model_call
    async def guarded_model(request, handler):
        budget = current_budget.get()
        if budget and (budget.cache_hits >= 3 or budget.model_calls >= max(1,max_calls-2)
                       or budget.attempts >= max(1,max_tool_calls-4)):
            budget.finalization_forced = True
            original = request.system_message.content if request.system_message else ''
            if not isinstance(original,str):
                original=json.dumps(original,ensure_ascii=False)
            request=request.override(tools=[],tool_choice=None,
                system_message=SystemMessage(content=original+'\n查询阶段已经结束，不再调用工具。按用户原始任务输出最终结果：只查天气则简洁回答天气，规划任务则输出行程JSON；未知天气留空，缺失路线标为待确认。严禁编造查询事实。信息根本不足时明确说明。'))
        reserve_model_call(max_calls)
        return await handler(request)
    return guarded_model


async def call_with_retry(handler, request, *, timeout, retries, max_calls):
    budget = current_budget.get()
    if budget is None:
        raise ExecutionLimitError('工具调用缺少执行上下文')
    call = request.tool_call
    cache_key = None
    if call.get('name', '').startswith('maps_'):
        cache_key = (call['name'], json.dumps(call.get('args', {}), sort_keys=True, ensure_ascii=False))
    if cache_key in budget.cache:
        budget.cache_hits += 1
        return budget.cache[cache_key].model_copy(update={'tool_call_id':call['id'], 'id':None})
    for attempt in range(retries + 1):
        if budget.attempts >= max_calls:
            raise ExecutionLimitError('已达到工具调用次数上限，请缩小规划范围')
        budget.attempts += 1
        try:
            result = await asyncio.wait_for(handler(request), timeout)
            if isinstance(result, ToolMessage) and result.status == 'error':
                return safe_tool_error(request)
            if cache_key is not None and isinstance(result, ToolMessage):
                budget.cache[cache_key] = result.model_copy(update={'id':None})
            return result
        except Exception as exc:
            if not transient_error(exc):
                raise
            if attempt == retries:
                return safe_tool_error(request)
            await asyncio.sleep(min(0.25 * 2**attempt, 1))


def tool_reliability(timeout, retries, max_calls):
    @wrap_tool_call
    async def guarded(request, handler):
        try:
            return await call_with_retry(handler, request, timeout=timeout, retries=retries, max_calls=max_calls)
        except ExecutionLimitError:
            return ToolMessage(content='本次查询预算已用完，停止调用工具，只用已有资料生成结果；未知信息明确标记待确认。',
                               tool_call_id=request.tool_call['id'],status='error')
    return guarded
