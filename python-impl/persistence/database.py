"""数据库连接与会话管理（SQLAlchemy 2.0 + SQLite）。

只做三件事：建库建表、提供事务性会话、提供显式关闭。
上层（store / repository）只依赖这里暴露的 `Database`，不直接接触引擎。
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    """所有 ORM 模型的基类"""


class Database:
    """SQLite 数据库的薄封装"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(f"sqlite:///{self.path}", future=True)
        self._session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        """建表（幂等）。迁移需求出现前先用 create_all，避免过早引入迁移框架。"""
        from persistence import models  # noqa: F401  导入以完成模型注册
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        """事务性会话：正常结束提交，异常回滚。"""
        session = self._session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    def dispose(self) -> None:
        self.engine.dispose()
