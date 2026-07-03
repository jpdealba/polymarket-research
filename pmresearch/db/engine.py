"""Engine/session factory. WAL + foreign_keys pragmas on every connection."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ..config import Settings


def get_engine(settings: Settings) -> Engine:
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.sqlalchemy_url, future=True, connect_args={"timeout": 30})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def get_session_factory(settings: Settings) -> sessionmaker:
    return sessionmaker(bind=get_engine(settings), future=True)
