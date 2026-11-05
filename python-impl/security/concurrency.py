"""按调用方的并发闸门。

替代原先的全局 `asyncio.Semaphore(1)`：全局单闸门会把整个服务的吞吐锁成串行，
高并发下排队的请求必然超时。改成**按调用方分桶**后，单个调用方仍然受限（保护上游
模型网关的额度），不同调用方之间不再互相拖累。

信号量数量有上限，避免大量不同 key 造成对象堆积。
"""

from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
from typing import AsyncIterator


class KeyedConcurrencyGate:
    """每个调用方一个信号量"""

    def __init__(self, per_key_limit: int, max_keys: int = 1000):
        if per_key_limit <= 0:
            raise ValueError("per_key_limit 必须为正数")
        self.per_key_limit = per_key_limit
        self.max_keys = max_keys
        self._gates: OrderedDict[str, asyncio.Semaphore] = OrderedDict()

    def _gate_for(self, key: str) -> asyncio.Semaphore:
        gate = self._gates.get(key)
        if gate is None:
            gate = asyncio.Semaphore(self.per_key_limit)
            self._gates[key] = gate
        self._gates.move_to_end(key)
        while len(self._gates) > self.max_keys:
            self._gates.popitem(last=False)
        return gate

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncIterator[None]:
        """占用一个并发位；超出上限时在此排队等待。"""
        gate = self._gate_for(key)
        await gate.acquire()
        try:
            yield
        finally:
            gate.release()

    def in_flight(self, key: str) -> int:
        """当前占用中的并发位数量（用于观测与测试断言）。"""
        gate = self._gates.get(key)
        if gate is None:
            return 0
        return self.per_key_limit - gate._value  # noqa: SLF001  只读查询
