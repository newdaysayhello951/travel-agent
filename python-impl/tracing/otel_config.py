"""
全链路追踪 — OpenTelemetry 接入

用装饰器为每个 Agent 方法创建 span，记录耗时、成败、结果字段名与异常。
导出目标由配置决定：配置了 OTLP 收集器地址就走 OTLP，否则退化为打印到标准输出。

当前边界：没有采集 token 消耗，也没有配置采样率。
"""

from __future__ import annotations

import functools
import time
from typing import Any, Callable

try:
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter
    from opentelemetry.sdk.resources import Resource

    _HAS_OTEL = True
except ImportError:
    _HAS_OTEL = False


_tracer = None


def init_tracer(
    service_name: str = "smart-cs-multi-agent",
    otlp_endpoint: str | None = None,
    sample_ratio: float = 1.0,
) -> None:
    """
    初始化OpenTelemetry追踪器。

    Args:
        service_name: 服务名称
        otlp_endpoint: OTLP收集器地址，None则输出到控制台
        sample_ratio: 采样比例；小于 1 时按 TraceId 比例采样，用于控制追踪数据量
    """
    global _tracer

    if not _HAS_OTEL:
        return

    resource = Resource.create({"service.name": service_name})
    provider_kwargs: dict[str, Any] = {"resource": resource}
    if sample_ratio < 1.0:
        # 全量上报在高流量下会淹没后端，采样是控制追踪数据量的主要手段
        from opentelemetry.sdk.trace.sampling import TraceIdRatioBased
        provider_kwargs["sampler"] = TraceIdRatioBased(max(0.0, min(1.0, sample_ratio)))
    provider = TracerProvider(**provider_kwargs)

    if otlp_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            exporter = OTLPSpanExporter(endpoint=otlp_endpoint)
        except ImportError:
            exporter = ConsoleSpanExporter()
    else:
        exporter = ConsoleSpanExporter()

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer(service_name)


def get_tracer():
    """获取全局Tracer实例"""
    global _tracer
    if _tracer is None:
        if _HAS_OTEL:
            _tracer = trace.get_tracer("smart-cs-multi-agent")
        else:
            return None
    return _tracer


def trace_agent_call(agent_name: str) -> Callable:
    """
    Agent调用追踪装饰器。

    为每个被装饰的方法创建一个 span，写入以下属性：
    - agent.name: Agent 名称
    - agent.method: 方法名
    - agent.duration_ms: 调用耗时（毫秒）
    - agent.success: 是否成功
    - agent.result_keys: 返回字典的键名（仅当返回值为 dict）
    - agent.error: 异常信息（仅失败时）

    记录异常后会重新抛出，异常处理责任仍在上层，观测层不改变业务行为。

    用法：
        @trace_agent_call("knowledge_rag")
        async def process(self, state):
            ...
    """
    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            tracer = get_tracer()
            started = time.perf_counter()
            span_name = f"agent.{agent_name}.{func.__name__}"

            # 追踪不可用时也要记录指标：观测能力缺失不应该让业务指标一起消失。
            if tracer is None:
                try:
                    result = await func(*args, **kwargs)
                except Exception:
                    AGENT_METRICS.record_call(
                        agent_name, (time.perf_counter() - started) * 1000, False
                    )
                    raise
                AGENT_METRICS.record_call(
                    agent_name, (time.perf_counter() - started) * 1000, True
                )
                return result

            with tracer.start_as_current_span(span_name) as span:
                span.set_attribute("agent.name", agent_name)
                span.set_attribute("agent.method", func.__name__)

                try:
                    result = await func(*args, **kwargs)
                    duration_ms = (time.perf_counter() - started) * 1000

                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", True)

                    if isinstance(result, dict):
                        span.set_attribute("agent.result_keys", str(list(result.keys())))

                    AGENT_METRICS.record_call(agent_name, duration_ms, True)
                    return result

                except Exception as e:
                    duration_ms = (time.perf_counter() - started) * 1000
                    span.set_attribute("agent.duration_ms", duration_ms)
                    span.set_attribute("agent.success", False)
                    span.set_attribute("agent.error", str(e))
                    span.record_exception(e)
                    AGENT_METRICS.record_call(agent_name, duration_ms, False)
                    raise

        return wrapper
    return decorator


class AgentMetrics:
    """Agent调用指标收集器"""

    def __init__(self):
        self._call_counts: dict[str, int] = {}
        self._total_duration: dict[str, float] = {}
        self._error_counts: dict[str, int] = {}

    def record_call(self, agent_name: str, duration_ms: float, success: bool):
        self._call_counts[agent_name] = self._call_counts.get(agent_name, 0) + 1
        self._total_duration[agent_name] = self._total_duration.get(agent_name, 0.0) + duration_ms
        if not success:
            self._error_counts[agent_name] = self._error_counts.get(agent_name, 0) + 1

    def get_summary(self) -> dict[str, Any]:
        summary = {}
        for agent_name in self._call_counts:
            calls = self._call_counts[agent_name]
            total_ms = self._total_duration[agent_name]
            errors = self._error_counts.get(agent_name, 0)
            summary[agent_name] = {
                "total_calls": calls,
                "avg_duration_ms": total_ms / calls if calls > 0 else 0,
                "error_rate": errors / calls if calls > 0 else 0,
            }
        return summary


# 全局指标实例：装饰器与接口层共用同一份统计，避免"埋点写了两套、只接通一套"
AGENT_METRICS = AgentMetrics()
