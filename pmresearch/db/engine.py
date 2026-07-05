"""Engine/session factory. WAL + foreign_keys pragmas on every connection."""

from __future__ import annotations

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from ..config import Settings


def get_engine(settings: Settings) -> Engine:
    settings.db_dir.mkdir(parents=True, exist_ok=True)
    engine = create_engine(settings.sqlalchemy_url, future=True, connect_args={"timeout": 120})

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        # 120s: large one-off admin rebuilds (e.g. episodes/dataset replay for
        # a multi-million-event wallet) run for minutes alongside the
        # always-on collector; 30s wasn't enough headroom to avoid losing the
        # write-lock race against the collector's own periodic writes.
        cursor.execute("PRAGMA busy_timeout=120000")
        # NORMAL is crash-safe under WAL (only risks the last txn on power loss)
        # and cuts the fsync/write-lock duration that was starving the
        # high-frequency book sampler and stalling enrichment.
        cursor.execute("PRAGMA synchronous=NORMAL")
        # Memory-mapped reads and a larger page cache: the DB is multi-GB and
        # read-heavy (book/context/reconcile projections), default cache is 2MB.
        cursor.execute("PRAGMA mmap_size=536870912")  # 512 MiB
        cursor.execute("PRAGMA cache_size=-262144")  # 256 MiB (negative = KiB)
        cursor.close()

    return engine


def get_session_factory(settings: Settings) -> sessionmaker:
    return sessionmaker(bind=get_engine(settings), future=True)
