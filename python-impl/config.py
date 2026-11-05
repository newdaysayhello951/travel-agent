"""
统一配置入口 — 集中读取根目录 .env

此前 `os.getenv` 分散在 settings.py 与 api/main.py 中，同一个配置项没有单一来源，
检索索引用的环境变量名也沿用了与当前实现无关的历史命名。现在统一在这里解析：

- `get_settings()` 带缓存，整个进程只解析一次，返回带类型校验的配置快照。
- 区分两类配置：
  * **必需配置**（模型名与密钥）缺失时，由 `settings.create_llm()` 抛错，
    让服务在启动阶段就失败，而不是等第一个请求进来才返回 502。
  * **可选配置**一律给可用默认值，缺失不影响启动（例如 Redis 不可用时降级到内存）。
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


def _env(name: str, default: str | None = None) -> str | None:
    """读取环境变量；空字符串按“未设置”处理，避免 .env 里的 `KEY=` 覆盖默认值。"""
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_int(name: str, default: int) -> int:
    raw = _env(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"环境变量 {name} 需要是整数，当前值为 {raw!r}") from exc


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


class Settings(BaseModel):
    """进程级配置快照（带类型与取值范围校验）"""

    # ── 模型 ──
    llm_provider: str = "openai_compatible"
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_enable_thinking: bool = False
    llm_timeout_seconds: int = Field(default=45, gt=0)
    llm_max_retries: int = Field(default=0, ge=0)
    # 结构化输出的实现方式。function_calling 依赖网关支持工具调用；
    # json_schema / json_mode 走 response_format，部分网关（含本机实测的网关）会拒绝。
    llm_structured_method: str = "function_calling"

    # ── 会话与记忆 ──
    redis_url: str = "redis://localhost:6379/0"
    short_term_max_turns: int = Field(default=20, gt=0)
    short_term_ttl_seconds: int = Field(default=1800, gt=0)
    # 内存降级路径最多缓存多少个会话（LRU 淘汰），防止会话数无限增长
    short_term_max_sessions: int = Field(default=1000, gt=0)
    # 送进模型的历史消息条数上限；超出部分会从检查点里移除
    max_context_messages: int = Field(default=20, gt=0)

    # ── 持久化 ──
    # 业务数据（工单等）
    database_path: Path = ROOT / ".data" / "app.db"
    # 图检查点（多轮对话状态），与业务数据分开存放
    checkpoint_db_path: Path = ROOT / ".data" / "checkpoints.sqlite"
    # 知识库源文件目录
    knowledge_dir: Path = ROOT / "knowledge"
    # 知识库分块索引的缓存位置（<index_path>.meta.json）
    knowledge_index_path: Path = ROOT / ".data" / "knowledge" / "index"

    # ── 工具 ──
    mcp_call_log_max: int = Field(default=200, gt=0)

    # ── 服务 ──
    host: str = "127.0.0.1"
    port: int = Field(default=8001, gt=0, lt=65536)
    request_timeout_seconds: int = Field(default=180, gt=0)
    max_concurrent_requests: int = Field(default=1, gt=0)
    cors_allow_origins: list[str] = Field(default_factory=list)

    # ── 可观测 ──
    otel_service_name: str = "smart-cs-multi-agent"
    otel_otlp_endpoint: str | None = None

    def missing_llm_fields(self) -> list[str]:
        """返回缺失的必需模型配置项名称，供启动时给出明确报错。"""
        missing = []
        if not self.llm_api_key:
            missing.append("LLM_API_KEY")
        if not self.llm_model:
            missing.append("LLM_MODEL")
        return missing


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """解析并缓存配置。整个进程只执行一次。"""
    provider = _env("LLM_PROVIDER", "openai_compatible") or "openai_compatible"

    if provider == "dashscope":
        api_key = _env("DASHSCOPE_API_KEY")
        base_url = DASHSCOPE_BASE_URL
        model = _env("DASHSCOPE_MODEL", "qwen3-max")
    else:
        api_key = _env("LLM_API_KEY") or _env("OPENAI_API_KEY")
        base_url = _env("LLM_BASE_URL") or _env("OPENAI_BASE_URL")
        model = _env("LLM_MODEL") or _env("OPENAI_MODEL")

    host = _env("HOST", "127.0.0.1") or "127.0.0.1"
    port = _env_int("PORT", 8001)

    return Settings(
        llm_provider=provider,
        llm_api_key=api_key,
        llm_base_url=base_url,
        llm_model=model,
        llm_enable_thinking=_env_bool("LLM_ENABLE_THINKING", False),
        llm_timeout_seconds=_env_int("LLM_TIMEOUT_SECONDS", 45),
        llm_structured_method=_env("LLM_STRUCTURED_METHOD", "function_calling")
        or "function_calling",
        redis_url=_env("REDIS_URL", "redis://localhost:6379/0") or "redis://localhost:6379/0",
        short_term_max_turns=_env_int("SHORT_TERM_MAX_TURNS", 20),
        short_term_ttl_seconds=_env_int("SHORT_TERM_TTL_SECONDS", 1800),
        short_term_max_sessions=_env_int("SHORT_TERM_MAX_SESSIONS", 1000),
        max_context_messages=_env_int("MAX_CONTEXT_MESSAGES", 20),
        mcp_call_log_max=_env_int("MCP_CALL_LOG_MAX", 200),
        database_path=Path(_env("DATABASE_PATH", str(ROOT / ".data" / "app.db"))),
        checkpoint_db_path=Path(
            _env("CHECKPOINT_DB_PATH", str(ROOT / ".data" / "checkpoints.sqlite"))
        ),
        knowledge_dir=Path(_env("KNOWLEDGE_DIR", str(ROOT / "knowledge"))),
        knowledge_index_path=Path(
            _env("KNOWLEDGE_INDEX_PATH", str(ROOT / ".data" / "knowledge" / "index"))
        ),
        host=host,
        port=port,
        request_timeout_seconds=_env_int("REQUEST_TIMEOUT_SECONDS", 180),
        max_concurrent_requests=_env_int("MAX_CONCURRENT_REQUESTS", 1),
        cors_allow_origins=[f"http://127.0.0.1:{port}", f"http://localhost:{port}"],
        otel_service_name=_env("OTEL_SERVICE_NAME", "smart-cs-multi-agent") or "smart-cs-multi-agent",
        otel_otlp_endpoint=_env("OTEL_EXPORTER_OTLP_ENDPOINT"),
    )
