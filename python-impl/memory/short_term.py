"""
短期记忆 — 会话级对话记录

优先使用 Redis（支持多进程/多实例共享，带 TTL 与滑动窗口）；未安装 redis 包或连接失败时，
自动降级到进程内存。

注意两条路径的行为并不一致：Redis 路径有 TTL 过期，内存降级路径只有条数上限、没有过期时间，
也不跨进程共享。降级过程目前是静默的（没有日志与指标），因此 Redis 真的不可用时外部无法察觉。
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

try:
    import redis.asyncio as aioredis
except ImportError:
    aioredis = None


class ShortTermMemory:
    """
    短期记忆：会话级对话记录缓存。

    两条实现路径：
    - Redis 可用：rpush 追加 → ltrim 只保留最近 max_turns 条 → expire 设置过期
    - Redis 不可用：退化为进程内存字典，只保留条数上限，没有过期时间

    连接是懒加载的：只有真正读写时才尝试连接，这样 Redis 不可用也不会拖垮服务启动。
    """

    def __init__(
        self,
        redis_url: str = "redis://localhost:6379/0",
        max_turns: int = 20,
        ttl_seconds: int = 1800,
    ):
        self.max_turns = max_turns
        self.ttl_seconds = ttl_seconds
        self._redis_url = redis_url
        self._redis: Any = None
        self._fallback_store: dict[str, list] = {}

    async def _get_redis(self):
        """懒加载Redis连接"""
        if self._redis is None:
            if aioredis is None:
                return None
            try:
                self._redis = aioredis.from_url(self._redis_url, decode_responses=True)
                await self._redis.ping()
            except Exception:
                self._redis = None
        return self._redis

    def _session_key(self, session_id: str) -> str:
        return f"smartcs:short_term:{session_id}"

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
        else:
            if session_id not in self._fallback_store:
                self._fallback_store[session_id] = []
            self._fallback_store[session_id].append(message)
            if len(self._fallback_store[session_id]) > self.max_turns:
                self._fallback_store[session_id] = self._fallback_store[session_id][-self.max_turns:]

    async def get_history(self, session_id: str, last_n: int | None = None) -> list[dict]:
        """获取对话历史"""
        r = await self._get_redis()

        if r is not None:
            key = self._session_key(session_id)
            n = last_n or self.max_turns
            raw = await r.lrange(key, -n, -1)
            return [json.loads(item) for item in raw]
        else:
            history = self._fallback_store.get(session_id, [])
            if last_n:
                return history[-last_n:]
            return list(history)

    async def clear(self, session_id: str) -> None:
        """清除指定session的短期记忆"""
        r = await self._get_redis()
        if r is not None:
            await r.delete(self._session_key(session_id))
        else:
            self._fallback_store.pop(session_id, None)
