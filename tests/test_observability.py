"""可观测性：日志配置、per-agent 指标、token 计量、采样配置。"""

import logging

from langchain_core.messages import AIMessage
from logging_setup import configure_logging
from tracing.llm_metering import MeteredLLM, TokenUsage, extract_usage
from tracing.otel_config import AGENT_METRICS, AgentMetrics, trace_agent_call


# ── 日志 ──

def test_configure_logging_is_idempotent():
    configure_logging("INFO")
    before = len(logging.getLogger().handlers)
    configure_logging("INFO")
    assert len(logging.getLogger().handlers) == before


def test_configure_logging_makes_info_visible():
    configure_logging("INFO")
    assert logging.getLogger().level == logging.INFO
    assert logging.getLogger().handlers


# ── per-agent 指标 ──

async def test_decorator_records_metrics():
    metrics = AgentMetrics()
    AGENT_METRICS._call_counts.clear()
    AGENT_METRICS._total_duration.clear()
    AGENT_METRICS._error_counts.clear()

    @trace_agent_call("probe_ok")
    async def ok():
        return {"a": 1}

    @trace_agent_call("probe_fail")
    async def fail():
        raise ValueError("boom")

    await ok()
    try:
        await fail()
    except ValueError:
        pass

    summary = AGENT_METRICS.get_summary()
    assert summary["probe_ok"]["total_calls"] == 1
    assert summary["probe_ok"]["error_rate"] == 0
    assert summary["probe_fail"]["total_calls"] == 1
    assert summary["probe_fail"]["error_rate"] == 1
    assert metrics.get_summary() == {}  # 独立实例不受影响


async def test_decorator_preserves_return_value_and_exception():
    @trace_agent_call("probe_value")
    async def returns(value):
        return value

    assert await returns(42) == 42

    @trace_agent_call("probe_raise")
    async def raises():
        raise KeyError("x")

    try:
        await raises()
    except KeyError:
        pass
    else:
        raise AssertionError("异常被吞掉了")


# ── token 计量 ──

class _FakeMessage(AIMessage):
    usage_metadata: dict


def _message(prompt: int, completion: int) -> AIMessage:
    message = AIMessage(content="hi")
    message.usage_metadata = {
        "input_tokens": prompt, "output_tokens": completion,
        "total_tokens": prompt + completion,
    }
    return message


def test_extract_usage_from_usage_metadata():
    usage = extract_usage(_message(10, 5))
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 5


def test_extract_usage_returns_none_when_absent():
    """对象没有任何用量信息时返回 None，而不是抛出异常"""
    assert extract_usage(object()) is None


def test_token_usage_aggregates_and_totals():
    usage = TokenUsage()
    usage.record("m", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})
    usage.record("m", {"input_tokens": 20, "output_tokens": 1, "total_tokens": 21})
    summary = usage.get_summary()
    assert summary["m"]["calls"] == 2
    assert summary["m"]["prompt_tokens"] == 30
    assert summary["m"]["completion_tokens"] == 6
    assert summary["__total__"]["total_tokens"] == 36


def test_token_usage_ignores_missing_usage():
    """没有用量信息时不产生计数条目，避免出现 calls=0 的假记录"""
    usage = TokenUsage()
    usage.record("m", None)
    usage.record("m", {})
    assert "m" not in usage.get_summary()
    assert usage.get_summary()["__total__"]["calls"] == 0


async def test_metered_llm_records_and_passes_through():
    class _Inner:
        def __init__(self):
            self.calls = 0

        async def ainvoke(self, messages, **kwargs):
            self.calls += 1
            return _message(7, 3)

        def with_structured_output(self, schema, **kwargs):
            return self

    inner = _Inner()
    usage = TokenUsage()
    metered = MeteredLLM(inner, label="demo", usage=usage)

    message = await metered.ainvoke(["x"])
    assert message.content == "hi"
    assert inner.calls == 1
    assert usage.get_summary()["demo"]["total_tokens"] == 10

    await metered.with_structured_output(dict).ainvoke(["x"])
    assert usage.get_summary()["demo"]["calls"] == 2


def test_metered_llm_forwards_attributes():
    class _Inner:
        model_name = "demo-model"

    assert MeteredLLM(_Inner()).model_name == "demo-model"


# ── 配置 ──

def test_observability_settings_defaults():
    from config import Settings

    settings = Settings()
    assert settings.log_level == "INFO"
    assert settings.otel_sample_ratio == 1.0


def test_sample_ratio_is_validated():
    import pytest
    from pydantic import ValidationError

    from config import Settings

    with pytest.raises(ValidationError):
        Settings(otel_sample_ratio=1.5)
