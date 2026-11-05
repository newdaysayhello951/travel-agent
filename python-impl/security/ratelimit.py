"""按调用方限流：进程内令牌桶。

单机有效。多实例部署时应换成 Redis 实现（与本项目短期记忆的降级思路一致），
接口保持不变即可替换。
"""

from __future__ import annotations

import time
from collections import OrderedDict
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    updated_at: float


class TokenBucketLimiter:
    """令牌桶限流器：按调用方标识分桶，桶数量有上限以防被海量 key 撑爆内存。"""

    def __init__(self, per_minute: int, burst: int | None = None, max_buckets: int = 5000):
        if per_minute <= 0:
            raise ValueError("per_minute 必须为正数")
        self.rate_per_second = per_minute / 60.0
        self.capacity = float(burst if burst and burst > 0 else per_minute)
        self.max_buckets = max_buckets
        self._buckets: OrderedDict[str, _Bucket] = OrderedDict()

    def _bucket_for(self, key: str) -> _Bucket:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(tokens=self.capacity, updated_at=time.monotonic())
            self._buckets[key] = bucket
        self._buckets.move_to_end(key)
        while len(self._buckets) > self.max_buckets:
            self._buckets.popitem(last=False)
        return bucket

    def check(self, key: str, cost: float = 1.0) -> tuple[bool, float]:
        """
        尝试取走 `cost` 个令牌。

        Returns:
            (是否放行, 建议等待秒数)；不放行时等待秒数为 0 表示配置异常。
        """
        now = time.monotonic()
        bucket = self._bucket_for(key)
        elapsed = max(0.0, now - bucket.updated_at)
        bucket.tokens = min(self.capacity, bucket.tokens + elapsed * self.rate_per_second)
        bucket.updated_at = now

        if bucket.tokens >= cost:
            bucket.tokens -= cost
            return True, 0.0

        deficit = cost - bucket.tokens
        return False, deficit / self.rate_per_second if self.rate_per_second else 0.0

    def remaining(self, key: str) -> int:
        """当前剩余令牌数（取整），用于响应头。"""
        bucket = self._buckets.get(key)
        if bucket is None:
            return int(self.capacity)
        now = time.monotonic()
        elapsed = max(0.0, now - bucket.updated_at)
        return int(min(self.capacity, bucket.tokens + elapsed * self.rate_per_second))
