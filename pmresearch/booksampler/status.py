"""Book sampler status: tracked tokens, snapshot counts, storage. Extracted
from `pmresearch/cli/books.py`'s `books_status` command so the CLI and
`pmresearch/api.py` share one implementation (no duplicated SQL) — see
Phase 16 (docs/plan/IMPLEMENTATION_PLAN.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings


@dataclass(frozen=True)
class BookSamplerStatus:
    token_count: int
    snapshot_count: int
    with_raw_ref: int
    raw_fetch_count: int
    oldest_ts: Optional[int]
    newest_ts: Optional[int]
    raw_storage_bytes: int


def book_sampler_status(session: Session, settings: Settings) -> BookSamplerStatus:
    token_count = session.execute(
        text("SELECT COUNT(DISTINCT token_id) FROM book_snapshots")
    ).scalar() or 0

    snapshot_count = session.execute(
        text("SELECT COUNT(*) FROM book_snapshots")
    ).scalar() or 0

    raw_count = session.execute(
        text("SELECT COUNT(*) FROM book_snapshots WHERE raw_ref IS NOT NULL")
    ).scalar() or 0

    oldest = session.execute(text("SELECT MIN(ts) FROM book_snapshots")).scalar()
    newest = session.execute(text("SELECT MAX(ts) FROM book_snapshots")).scalar()

    # Storage estimate: sum of raw gzip files for clob/book
    storage_row = session.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(content_hash)), 0) "
            "FROM raw_fetches WHERE source = 'clob' AND endpoint = 'book'"
        )
    ).fetchone()

    raw_books_dir = settings.raw_dir / "clob" / "book"
    if raw_books_dir.exists():
        total_bytes = sum(
            f.stat().st_size for f in raw_books_dir.rglob("*.json.gz") if f.is_file()
        )
    else:
        total_bytes = 0

    return BookSamplerStatus(
        token_count=token_count,
        snapshot_count=snapshot_count,
        with_raw_ref=raw_count,
        raw_fetch_count=storage_row[0] if storage_row else 0,
        oldest_ts=oldest,
        newest_ts=newest,
        raw_storage_bytes=total_bytes,
    )
