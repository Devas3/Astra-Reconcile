"""SQLAlchemy 2.0 engine/session plumbing (SQLite for dev/test, PostgreSQL for production)."""
from __future__ import annotations

from typing import Iterator, Optional

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings


class Base(DeclarativeBase):
    pass


def make_engine(url: Optional[str] = None) -> Engine:
    url = url or get_settings().database_url
    kwargs: dict = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if ":memory:" in url:
            kwargs["poolclass"] = StaticPool
    engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _pragmas(dbapi_conn, _):  # WAL + FK enforcement
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    return engine


_engine: Optional[Engine] = None
_factory: Optional[sessionmaker] = None


def get_engine() -> Engine:
    global _engine, _factory
    if _engine is None:
        _engine = make_engine()
        _factory = sessionmaker(_engine, expire_on_commit=False)
    return _engine


def configure(engine: Engine) -> None:
    """Override the process-wide engine (used by tests)."""
    global _engine, _factory
    _engine = engine
    _factory = sessionmaker(engine, expire_on_commit=False)


def get_session() -> Iterator[Session]:
    get_engine()
    assert _factory is not None
    with _factory() as session:
        yield session
