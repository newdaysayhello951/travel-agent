"""
工作记忆 — Agent当前任务的中间推理状态
存储在进程内存中，生命周期与单次请求对齐。
用于维护Supervisor的路由决策上下文和子Agent的中间结果。
"""

from __future__ import annotations

import threading
from collections import defaultdict
from datetime import datetime
from typing import Any


class WorkingMemory:
    """
    工作记忆：维护当前会话的中间状态快照。

    特点：
    - 进程内存储，读写零延迟（临界区内没有 await，所以用同步锁即可）
    - 按 session_id 隔离
    - current_context 保存"最新快照"，entries 保留有限条数的历史记录
    """

    def __init__(self, max_entries_per_session: int = 50):
        self._store: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._context: dict[str, dict[str, Any]] = defaultdict(dict)
        self._lock = threading.Lock()
        self._max_entries = max_entries_per_session

    def update(self, session_id: str, data: dict[str, Any]) -> None:
        """更新工作记忆"""
        with self._lock:
            entry = {
                "timestamp": datetime.now().isoformat(),
                "data": data,
            }
            self._store[session_id].append(entry)

            if len(self._store[session_id]) > self._max_entries:
                self._store[session_id] = self._store[session_id][-self._max_entries:]

            self._context[session_id].update(data)

    def get_context(self, session_id: str) -> dict[str, Any]:
        """获取当前session的完整上下文"""
        return dict(self._context.get(session_id, {}))

    def clear(self, session_id: str) -> None:
        """清除指定session的工作记忆"""
        with self._lock:
            self._store.pop(session_id, None)
            self._context.pop(session_id, None)
