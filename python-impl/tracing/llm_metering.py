"""模型调用的 token 计量。

用**代理包装**模型客户端，而不是在 5 个调用点各写一遍统计：这样任何一次模型调用
都会被记账，也不会因为新增调用点而漏统计。

`MeteredLLM` 对其他行为完全透传（`with_structured_output`、属性访问等），
Agent 代码不需要任何改动。
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class TokenUsage:
    """按模型/用途累计 token 用量"""

    def __init__(self) -> None:
        self._records: dict[str, dict[str, int]] = {}

    def record(self, label: str, usage: dict[str, Any] | None) -> None:
        if not usage:
            return
        entry = self._records.setdefault(
            label, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += int(usage.get("input_tokens") or 0)
        entry["completion_tokens"] += int(usage.get("output_tokens") or 0)
        entry["total_tokens"] += int(usage.get("total_tokens") or 0)

    def get_summary(self) -> dict[str, dict[str, int]]:
        summary = {
            label: dict(entry) for label, entry in self._records.items()
        }
        summary["__total__"] = {
            key: sum(entry[key] for entry in self._records.values())
            for key in ("calls", "prompt_tokens", "completion_tokens", "total_tokens")
        }
        return summary

    def reset(self) -> None:
        self._records.clear()


# 全局计量实例，供接口层展示
TOKEN_USAGE = TokenUsage()


def extract_usage(message: Any) -> dict[str, Any] | None:
    """从模型返回消息里取 token 用量（不同版本字段名不同，做兼容）"""
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict) and usage:
        return usage
    metadata = getattr(message, "response_metadata", None)
    if isinstance(metadata, dict):
        for key in ("token_usage", "usage"):
            candidate = metadata.get(key)
            if isinstance(candidate, dict) and candidate:
                return candidate
    return None


class _MeteredStructured:
    """包装 `with_structured_output()` 的返回值，同样记账"""

    def __init__(self, runnable: Any, label: str, usage: "TokenUsage"):
        self._runnable = runnable
        self._label = label
        self._usage = usage

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        result = await self._runnable.ainvoke(messages, **kwargs)
        usage = extract_usage(result)
        if usage is None and hasattr(result, "raw"):
            usage = extract_usage(getattr(result, "raw"))
        self._usage.record(self._label, usage)
        return result

    def __getattr__(self, item: str) -> Any:
        return getattr(self._runnable, item)


class MeteredLLM:
    """模型客户端代理：记录 token 用量，其余行为透传"""

    def __init__(self, inner: Any, label: str = "chat", usage: TokenUsage | None = None):
        self._inner = inner
        self._label = label
        self._usage = usage or TOKEN_USAGE

    async def ainvoke(self, messages: Any, **kwargs: Any) -> Any:
        message = await self._inner.ainvoke(messages, **kwargs)
        self._usage.record(self._label, extract_usage(message))
        return message

    def with_structured_output(self, schema: Any, **kwargs: Any) -> _MeteredStructured:
        return _MeteredStructured(
            self._inner.with_structured_output(schema, **kwargs), self._label, self._usage
        )

    def __getattr__(self, item: str) -> Any:
        # 其余属性（model_name、temperature 等）直接透传
        return getattr(self._inner, item)
