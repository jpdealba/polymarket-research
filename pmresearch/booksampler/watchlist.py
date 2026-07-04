"""Book sampling for named watchlists."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import text

from ..config import Settings
from ..db.engine import get_session_factory
from ..rawstore.store import RawStore
from ..sources.clob import ClobSource
from ..watchlists.world_cup import active_token_rows_for_sampling, token_ids
from .sampler import PER_TOKEN_DELAY_S, _persist_snapshot

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WatchlistSampleStats:
    run_id: int | None
    watchlist_id: int | None
    tokens_selected: int
    tokens_sampled: int
    snapshots_written: int
    books_found: int
    empty_books: int
    errors: int
    status: str


def _utc(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _latest_wallet_event_ts(session, wallet: str | None) -> int | None:
    if not wallet:
        return None
    return session.execute(
        text("SELECT MAX(ts) FROM wallet_events WHERE wallet = :wallet"),
        {"wallet": wallet.lower()},
    ).scalar()


def _start_run(session, watchlist_id: int, *, wallet: str | None, selected: int) -> int:
    latest = _latest_wallet_event_ts(session, wallet)
    now = int(datetime.now(timezone.utc).timestamp())
    run_id = session.execute(
        text(
            "INSERT INTO book_sample_runs "
            "(watchlist_id, started_at, selector_wallet_latest_event_ts, "
            "selector_wallet_latest_event_utc, tokens_selected, status) "
            "VALUES (:watchlist_id, :started_at, :latest_ts, :latest_utc, "
            ":selected, 'running') RETURNING id"
        ),
        {
            "watchlist_id": watchlist_id,
            "started_at": now,
            "latest_ts": latest,
            "latest_utc": _utc(latest),
            "selected": selected,
        },
    ).scalar()
    session.commit()
    return int(run_id)


def _finish_run(
    session,
    *,
    run_id: int,
    tokens_sampled: int,
    books_found: int,
    empty_books: int,
    errors: int,
    status: str,
) -> None:
    session.execute(
        text(
            "UPDATE book_sample_runs SET finished_at = :finished_at, "
            "tokens_sampled = :tokens_sampled, books_found = :books_found, "
            "books_empty = :empty_books, errors = :errors, status = :status "
            "WHERE id = :run_id"
        ),
        {
            "finished_at": int(datetime.now(timezone.utc).timestamp()),
            "tokens_sampled": tokens_sampled,
            "books_found": books_found,
            "empty_books": empty_books,
            "errors": errors,
            "status": status,
            "run_id": run_id,
        },
    )
    session.commit()


def sample_watchlist_once(
    settings: Settings,
    *,
    name: str,
    limit: int = 200,
    wallet: str | None = None,
    max_priority: int | None = None,
    min_priority: int | None = None,
    per_token_delay_s: float = PER_TOKEN_DELAY_S,
) -> WatchlistSampleStats:
    session = get_session_factory(settings)()
    source = ClobSource()
    raw_store = RawStore(settings, session)
    try:
        watchlist_id, rows = active_token_rows_for_sampling(
            session,
            name=name,
            limit=limit,
            max_priority=max_priority,
            min_priority=min_priority,
        )
        if watchlist_id is None:
            return WatchlistSampleStats(None, None, 0, 0, 0, 0, 0, 0, "missing_watchlist")
        run_id = _start_run(session, watchlist_id, wallet=wallet, selected=len(rows))
        if not rows:
            _finish_run(
                session,
                run_id=run_id,
                tokens_sampled=0,
                books_found=0,
                empty_books=0,
                errors=0,
                status="no_tokens",
            )
            return WatchlistSampleStats(run_id, watchlist_id, 0, 0, 0, 0, 0, 0, "no_tokens")

        snapshots = source.fetch_book_batch(
            raw_store, token_ids(rows), per_token_delay_s=per_token_delay_s
        )
        reasons = {row.token_id: f"{row.source}:{row.reason or ''}" for row in rows}
        written = found = empty = errors = 0
        for snap in snapshots:
            if snap.raw_fetch.raw_fetch_id == 0:
                errors += 1
                continue
            if snap.has_book:
                found += 1
            else:
                empty += 1
            if _persist_snapshot(
                session,
                raw_store,
                snap,
                sample_run_id=run_id,
                watchlist_id=watchlist_id,
                selector_reason=reasons.get(snap.token_id),
            ):
                written += 1
        status = "ok" if errors == 0 else "partial_error"
        _finish_run(
            session,
            run_id=run_id,
            tokens_sampled=len(snapshots),
            books_found=found,
            empty_books=empty,
            errors=errors,
            status=status,
        )
        logger.info(
            "Watchlist sample %s: selected=%d written=%d found=%d empty=%d errors=%d",
            name,
            len(rows),
            written,
            found,
            empty,
            errors,
        )
        return WatchlistSampleStats(
            run_id=run_id,
            watchlist_id=watchlist_id,
            tokens_selected=len(rows),
            tokens_sampled=len(snapshots),
            snapshots_written=written,
            books_found=found,
            empty_books=empty,
            errors=errors,
            status=status,
        )
    except Exception:
        logger.exception("Watchlist sample failed for %s", name)
        raise
    finally:
        source.close()
        session.close()
