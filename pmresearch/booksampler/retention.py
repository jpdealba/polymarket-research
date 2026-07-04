"""Book sampler retention: prunes raw book snapshot files beyond the
configured retention window.  Summary rows (best_bid/ask/spread/mid) in
`book_snapshots` are kept indefinitely — only the raw gzip files and their
raw_fetches references are cleaned up.

The prune job:
1. Finds book_snapshots with raw_ref pointing to raw_fetches older than N days.
2. Deletes the raw gzip files from disk.
3. NULLs the raw_ref in book_snapshots (the summary data remains).
4. Deletes the orphaned raw_fetches rows.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PruneStats:
    snapshots_checked: int
    raw_refs_nulled: int
    raw_files_deleted: int
    raw_fetches_deleted: int
    bytes_freed: int


def prune_raw_books(
    session: Session,
    settings: Settings,
    *,
    retention_days: int | None = None,
) -> PruneStats:
    """Prune raw book snapshot files older than the retention window.

    Summary rows in book_snapshots are preserved — only the raw gzip files
    and their raw_fetches entries are removed.
    """
    if retention_days is None:
        retention_days = settings.book_retention_raw_days

    cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    cutoff_iso = cutoff.isoformat()

    # Find book_snapshots with a raw_ref older than the cutoff.
    rows = session.execute(
        text(
            "SELECT bs.token_id, bs.ts, bs.raw_ref, rf.file_path "
            "FROM book_snapshots bs "
            "JOIN raw_fetches rf ON rf.id = bs.raw_ref "
            "WHERE rf.fetched_at < :cutoff "
            "AND bs.raw_ref IS NOT NULL "
            "ORDER BY bs.token_id, bs.ts"
        ),
        {"cutoff": cutoff_iso},
    ).fetchall()

    snapshots_checked = len(rows)
    raw_files_deleted = 0
    raw_fetches_deleted = 0
    bytes_freed = 0

    for row in rows:
        file_path = Path(row.file_path)
        if file_path.exists():
            try:
                size = file_path.stat().st_size
                file_path.unlink()
                bytes_freed += size
                raw_files_deleted += 1
            except OSError:
                logger.warning("Failed to delete raw book file: %s", file_path)

    # NULL out the raw_ref for pruned snapshots (summary data stays).
    result = session.execute(
        text(
            "UPDATE book_snapshots SET raw_ref = NULL "
            "WHERE raw_ref IN ("
            "  SELECT id FROM raw_fetches WHERE fetched_at < :cutoff"
            ")"
        ),
        {"cutoff": cutoff_iso},
    )
    raw_refs_nulled = result.rowcount

    # Delete the orphaned raw_fetches rows.
    result = session.execute(
        text(
            "DELETE FROM raw_fetches "
            "WHERE source = 'clob' AND endpoint = 'book' "
            "AND fetched_at < :cutoff"
        ),
        {"cutoff": cutoff_iso},
    )
    raw_fetches_deleted = result.rowcount

    session.commit()

    stats = PruneStats(
        snapshots_checked=snapshots_checked,
        raw_refs_nulled=raw_refs_nulled,
        raw_files_deleted=raw_files_deleted,
        raw_fetches_deleted=raw_fetches_deleted,
        bytes_freed=bytes_freed,
    )
    logger.info(
        "Book prune: checked=%d files_deleted=%d refs_nulled=%d fetches_deleted=%d freed=%d bytes",
        stats.snapshots_checked,
        stats.raw_files_deleted,
        stats.raw_refs_nulled,
        stats.raw_fetches_deleted,
        stats.bytes_freed,
    )
    return stats
