"""
短期记忆 — 会话级对话记录

优先使用 Redis（支持多进程/多实例共享，带 TTL 与滑动窗口）；未安装 redis 包或连接失败时，
自动降级到进程内存。

两条路径都做了容量与过期控制，但机制不同：
- Redis 路径：ltrim 保留最近 max_turns 条 + expire 设置空闲过期
- 内存降级路径：按 max_turns 截断 + 空闲超过 ttl_seconds 的会话被清理 + 最多保留
  max_sessions 个会话（LRU 淘汰），进程内有效、不跨进程共享

降级发生时只记录一次警告，便于运维发现 Redis 实际不可用。
"""

from __future__ import annotations

import json
import logging
from collections import OrderedDict
from datetime import datetime
from time import monotonic
from typing import Any

logger = logging.getLogger(__name__)

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class ShortTermMemory:
    """短期记忆：会话级对话记录缓存"""

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
        max_sessions: int = 1000,
    ):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self.max_sessions = max_sessions
        self._redis_url = redis_url
        self._redis: Any = None
        # 内存降级：OrderedDict 记录写入顺序，用于 LRU 淘汰
        self._fallback_store: OrderedDict[str, list[dict]] = OrderedDict()
        self._fallback_seen: dict[str, float] = {}
        self._degraded_reason: str | None = None

    # ── Redis 连接（懒加载 + 降级） ──

    async def _get_redis(self):
        """懒加载 Redis 连接；不可用时返回 None，由调用方走内存分支。"""
        if self._redis is None:
            if aioredis is None:
                self._note_degraded("未安装 redis 包")
                return None
            try:
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception as exc:
                self._note_degraded(f"连接失败（{type(exc).__name__}）")
                self._redis = None
        return self._redis

    def _note_degraded(self, reason: str) -> None:
        """降级只警告一次，避免每个请求都刷同一条日志。"""
        if self._degraded_reason is None:
            self._degraded_reason = reason
            logger.warning(
                "短期记忆降级为进程内存存储：%s。该路径不跨进程共享，"
                "过期与容量按 ttl_seconds / max_turns / max_sessions 在进程内控制。",
                reason,
            )

    @property
    def degraded(self) -> bool:
        """是否已降级到内存存储（供健康检查与测试使用）。"""
        return self._degraded_reason is not None

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

    # ── 内存降级的容量与过期控制 ──

    def _purge_expired(self, now: float) -> None:
        expired = [
            session_id for session_id, seen in self._fallback_seen.items()
            if now - seen > self.ttl_seconds
        ]
        for session_id in expired:
            self._fallback_store.pop(session_id, None)
            self._fallback_seen.pop(session_id, None)

    def _evict_overflow(self) -> None:
        while len(self._fallback_store) > self.max_sessions:
            session_id, _ = self._fallback_store.popitem(last=False)  # 淘汰最久未使用
            self._fallback_seen.pop(session_id, None)

    def _touch(self, session_id: str, now: float) -> None:
        self._fallback_store.move_to_end(session_id)
        self._fallback_seen[session_id] = now

    # ── 读写 ──

    async def add_message(self, session_id: str, role: str, content: str) -> None:
        """添加一条对话消息"""
        message = {
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        }

        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            await r.rpush(key, json.dumps(message, ensure_ascii=False))
            await r.ltrim(key, -self.max_turns, -1)
            await r.expire(key, self.ttl_seconds)
            return

        now = monotonic()
        self._purge_expired(now)
        bucket = self._fallback_store.setdefault(session_id, [])
        bucket.append(message)
        if len(bucket) > self.max_turns:
            del bucket[:-self.max_turns]
        self._touch(session_id, now)
        self._evict_overflow()

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史"""
        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            n = last_n or self.max_turns
            raw = await r.lrange(key, -n, -1)
            return [json.loads(item) for item in raw]

        now = monotonic()
        self._purge_expired(now)
        if session_id in self._fallback_store:
            self._touch(session_id, now)  # 读到即视为活跃，刷新空闲计时
        history = self._fallback_store.get(session_id, [])
        if last_n:
            return history[-last_n:]
        return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定 session 的短期记忆"""
        r = await self._get_redis()
        if r is not None:
            await r.delete(self._session_key(session_id))
            return
        self._fallback_store.pop(session_id, None)
        self._fallback_seen.pop(session_id, None)
