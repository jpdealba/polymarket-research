"""Book sampler: polls CLOB /book for Relevant Tokens, persists snapshots.

The sampler is designed to be:
- Isolated from sync jobs (own session, own timeouts, own exception handling)
- Bounded: per-tick cap on tokens to poll, per-token delay to respect rate limits
- Observable: logs token count, snapshot count, errors
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.engine import get_session_factory
from ..rawstore.store import RawStore
from ..sources.clob import BookSnapshot, ClobSource
from .relevant import relevant_token_ids

logger = logging.getLogger(__name__)

# Maximum tokens to poll per tick.  If the relevant set is larger, we rotate
# through it across ticks.
MAX_TOKENS_PER_TICK = 50

# Per-token delay between CLOB requests to avoid hammering the API.
PER_TOKEN_DELAY_S = 0.1


@dataclass(frozen=True)
class SampleStats:
    tokens_queried: int
    snapshots_written: int
    books_found: int
    empty_books: int
    errors: int
    total_relevant: int


def _persist_snapshot(
    session,
    raw_store: RawStore,
    snap: BookSnapshot,
    *,
    sample_run_id: int | None = None,
    watchlist_id: int | None = None,
    selector_reason: str | None = None,
) -> bool:
    """Insert a book_snapshots row.  Returns True if a new row was written."""
    if snap.raw_fetch.raw_fetch_id == 0:
        # Synthetic empty from a fetch error — skip DB write.
        return False

    # Use wall-clock time, not the raw-fetch file's mtime: RawStore dedupes
    # identical payloads by content hash across all history, so an unchanging
    # (or empty) book returns the *original* file with its *original* mtime on
    # every poll. Keying ts off that would collide with the first-ever snapshot
    # for that content and silently stop writing new rows for stale/thin books.
    ts = int(datetime.now(timezone.utc).timestamp())

    exists = session.execute(
        text(
            "SELECT 1 FROM book_snapshots "
            "WHERE token_id = :token_id AND ts = :ts"
        ),
        {"token_id": snap.token_id, "ts": ts},
    ).fetchone()
    if exists is not None:
        return False

    depth_json = json.dumps(snap.depth_top, sort_keys=True) if snap.depth_top else None
    session.execute(
        text(
            "INSERT INTO book_snapshots "
            "(token_id, ts, best_bid, best_ask, spread, mid, depth_top_json, raw_ref, "
            "sample_run_id, watchlist_id, selector_reason) "
            "VALUES (:token_id, :ts, :best_bid, :best_ask, :spread, :mid, :depth_json, "
            ":raw_ref, :sample_run_id, :watchlist_id, :selector_reason)"
        ),
        {
            "token_id": snap.token_id,
            "ts": ts,
            "best_bid": str(snap.best_bid) if snap.best_bid is not None else None,
            "best_ask": str(snap.best_ask) if snap.best_ask is not None else None,
            "spread": str(snap.spread) if snap.spread is not None else None,
            "mid": str(snap.mid) if snap.mid is not None else None,
            "depth_json": depth_json,
            "raw_ref": snap.raw_fetch.raw_fetch_id if snap.raw_fetch.raw_fetch_id else None,
            "sample_run_id": sample_run_id,
            "watchlist_id": watchlist_id,
            "selector_reason": selector_reason,
        },
    )
    session.commit()
    return True


def sample_once(
    settings: Settings,
    *,
    max_tokens: int = MAX_TOKENS_PER_TICK,
    per_token_delay_s: float = PER_TOKEN_DELAY_S,
) -> SampleStats:
    """Run one sampling tick: query relevant tokens, fetch books, persist.

    This is the entry point for both the CLI `pmr books sample-once` and the
    scheduled job.  It creates its own session and source adapter — fully
    isolated from sync jobs.
    """
    session = get_session_factory(settings)()
    source = ClobSource()
    raw_store = RawStore(settings, session)
    try:
        token_ids = relevant_token_ids(session)
        if not token_ids:
            return SampleStats(
                tokens_queried=0,
                snapshots_written=0,
                books_found=0,
                empty_books=0,
                errors=0,
                total_relevant=0,
            )

        # Rotate through the relevant set: track the last offset we sampled.
        offset = _get_sample_offset(session)
        if offset >= len(token_ids):
            offset = 0
        # Take up to max_tokens from the rotated list.
        rotated = token_ids[offset:] + token_ids[:offset]
        to_poll = rotated[:max_tokens]
        # Update offset for next tick.
        new_offset = max_tokens % len(token_ids) if len(token_ids) > max_tokens else 0
        _set_sample_offset(session, new_offset)

        snapshots = source.fetch_book_batch(
            raw_store, to_poll, per_token_delay_s=per_token_delay_s
        )

        written = 0
        found = 0
        empty = 0
        errors = 0
        for snap in snapshots:
            if snap.raw_fetch.raw_fetch_id == 0:
                errors += 1
                continue
            if snap.has_book:
                found += 1
            else:
                empty += 1
            if _persist_snapshot(session, raw_store, snap):
                written += 1

        stats = SampleStats(
            tokens_queried=len(to_poll),
            snapshots_written=written,
            books_found=found,
            empty_books=empty,
            errors=errors,
            total_relevant=len(token_ids),
        )
        logger.info(
            "Book sample: queried=%d written=%d found=%d empty=%d errors=%d relevant=%d",
            stats.tokens_queried,
            stats.snapshots_written,
            stats.books_found,
            stats.empty_books,
            stats.errors,
            stats.total_relevant,
        )
        return stats
    finally:
        source.close()
        session.close()


def _get_sample_offset(session) -> int:
    """Read the persistent rotation offset for the book sampler."""
    row = session.execute(
        text("SELECT value FROM _book_sampler_state WHERE key = 'sample_offset'")
    ).fetchone()
    return int(row.value) if row else 0


def _set_sample_offset(session, offset: int) -> None:
    """Write the persistent rotation offset."""
    session.execute(
        text(
            "INSERT INTO _book_sampler_state (key, value) "
            "VALUES ('sample_offset', :val) "
            "ON CONFLICT(key) DO UPDATE SET value = :val"
        ),
        {"val": str(offset)},
    )
    session.commit()
