"""基于 SQLite 的工单存储。

接口与 agents.ticket_handler.TicketStore（内存实现）保持一致，两者可直接互换：
内存实现用于测试与无数据库场景，本实现用于真实运行，重启后数据仍在。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from domain.ticket import check_transition, new_ticket_id
from persistence.database import Database
from persistence.models import TicketRecord


class SqlTicketStore:
    """工单持久化存储"""

    def __init__(self, database: Database):
        self._db = database

    def create(self, ticket_type: str, priority: str, summary: str,
               details: str, user_id: str) -> dict:
        with self._db.session() as session:
            now = datetime.now()
            record = TicketRecord(
                ticket_id=new_ticket_id(now),
                type=ticket_type,
                priority=priority,
                status="created",
                summary=summary,
                details=details,
                user_id=user_id,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            session.flush()
            return record.to_dict()

    def query(self, ticket_id: str) -> dict | None:
        with self._db.session() as session:
            record = session.get(TicketRecord, ticket_id)
            return record.to_dict() if record is not None else None

    def query_by_user(self, user_id: str) -> list[dict]:
        with self._db.session() as session:
            rows = session.scalars(
                select(TicketRecord)
                .where(TicketRecord.user_id == user_id)
                .order_by(TicketRecord.created_at)
            ).all()
            return [record.to_dict() for record in rows]

    def update_status(self, ticket_id: str, status: str) -> tuple[dict | None, str | None]:
        with self._db.session() as session:
            record = session.get(TicketRecord, ticket_id)
            if record is None:
                return None, f"未找到工单号 {ticket_id}"

            error = check_transition(record.status, status)
            if error:
                return None, error

            if record.status != status:
                record.status = status
                record.updated_at = datetime.now()
            session.flush()
            return record.to_dict(), None
