"""Watchlist CRUD, sync_state tracking, and next-action/staleness decisions.

Owns the watchlist tables directly via parameterized SQL (no ORM models —
Alembic owns the schema, this module just reads/writes it; see ADR 0002).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session


class SyncAction(str, Enum):
    BACKFILL = "backfill"
    INCREMENTAL = "incremental"


@dataclass(frozen=True)
class SyncStateRow:
    wallet: str
    backfill_complete: bool
    backfill_cursor_ts: Optional[int]
    last_incremental_ts: Optional[int]
    last_success_at: Optional[str]
    last_error: Optional[str]
    consecutive_failures: int
    status: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _to_sync_state(row) -> SyncStateRow:
    return SyncStateRow(
        wallet=row.wallet,
        backfill_complete=bool(row.backfill_complete),
        backfill_cursor_ts=row.backfill_cursor_ts,
        last_incremental_ts=row.last_incremental_ts,
        last_success_at=row.last_success_at,
        last_error=row.last_error,
        consecutive_failures=row.consecutive_failures,
        status=row.status,
    )


def add_wallet(session: Session, address: str, *, display_name: str | None = None) -> bool:
    """Insert the wallet, (re)activate its watchlist row, and initialize
    sync_state on first add. Idempotent: returns False if already active."""
    address = address.lower()
    now = _now()

    session.execute(
        text(
            "INSERT INTO wallets (address, first_seen_at, display_name) VALUES (:a, :t, :n) "
            "ON CONFLICT(address) DO NOTHING"
        ),
        {"a": address, "t": now, "n": display_name},
    )

    existing = session.execute(
        text("SELECT active FROM watchlist WHERE wallet = :a"), {"a": address}
    ).fetchone()

    if existing is not None and existing.active:
        session.commit()
        return False

    if existing is not None:
        session.execute(
            text(
                "UPDATE watchlist SET active = 1, added_at = :t, removed_at = NULL WHERE wallet = :a"
            ),
            {"a": address, "t": now},
        )
    else:
        session.execute(
            text(
                "INSERT INTO watchlist (wallet, active, added_at, removed_at) "
                "VALUES (:a, 1, :t, NULL)"
            ),
            {"a": address, "t": now},
        )
        session.execute(
            text(
                "INSERT INTO sync_state "
                "(wallet, backfill_complete, backfill_cursor_ts, last_incremental_ts, "
                "last_success_at, last_error, consecutive_failures, status) "
                "VALUES (:a, 0, NULL, NULL, NULL, NULL, 0, 'new')"
            ),
            {"a": address},
        )
    session.commit()
    return True


def remove_wallet(session: Session, address: str) -> bool:
    """Deactivate the watchlist row. Idempotent: False if already inactive/absent."""
    address = address.lower()
    existing = session.execute(
        text("SELECT active FROM watchlist WHERE wallet = :a"), {"a": address}
    ).fetchone()
    if existing is None or not existing.active:
        return False
    session.execute(
        text("UPDATE watchlist SET active = 0, removed_at = :t WHERE wallet = :a"),
        {"a": address, "t": _now()},
    )
    session.commit()
    return True


def list_wallets(session: Session, *, active_only: bool = True):
    query = (
        "SELECT w.address, w.display_name, w.first_seen_at, wl.active "
        "FROM wallets w JOIN watchlist wl ON wl.wallet = w.address"
    )
    if active_only:
        query += " WHERE wl.active = 1"
    query += " ORDER BY w.address"
    return session.execute(text(query)).fetchall()


def get_sync_state(session: Session, address: str) -> Optional[SyncStateRow]:
    row = session.execute(
        text("SELECT * FROM sync_state WHERE wallet = :a"), {"a": address.lower()}
    ).fetchone()
    return _to_sync_state(row) if row is not None else None


def list_sync_states(session: Session):
    rows = session.execute(text("SELECT * FROM sync_state ORDER BY wallet")).fetchall()
    return [_to_sync_state(row) for row in rows]


def next_action(session: Session, address: str) -> SyncAction:
    state = get_sync_state(session, address)
    if state is None or not state.backfill_complete:
        return SyncAction.BACKFILL
    return SyncAction.INCREMENTAL


def is_stale(session: Session, address: str, *, cadence_s: int, stale_multiplier: float = 3.0) -> bool:
    """True if the wallet has a successful sync on record but it's older than
    `stale_multiplier` cadences. A wallet that has never synced successfully
    is 'new', not 'stale' — that distinction is what last_success_at is for."""
    state = get_sync_state(session, address)
    if state is None or state.last_success_at is None:
        return False
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(state.last_success_at)).total_seconds()
    return age > cadence_s * stale_multiplier


def start_backfill(session: Session, address: str) -> None:
    session.execute(
        text("UPDATE sync_state SET status = 'backfilling' WHERE wallet = :a"),
        {"a": address.lower()},
    )
    session.commit()


def complete_backfill(session: Session, address: str, *, cursor_ts: int, up_to_ts: int) -> None:
    session.execute(
        text(
            "UPDATE sync_state SET backfill_complete = 1, backfill_cursor_ts = :cursor, "
            "last_incremental_ts = :up_to, last_success_at = :t, last_error = NULL, "
            "consecutive_failures = 0, status = 'complete' WHERE wallet = :a"
        ),
        {"a": address.lower(), "cursor": cursor_ts, "up_to": up_to_ts, "t": _now()},
    )
    session.commit()


def record_incremental_success(session: Session, address: str, *, up_to_ts: int) -> None:
    session.execute(
        text(
            "UPDATE sync_state SET last_incremental_ts = :up_to, last_success_at = :t, "
            "last_error = NULL, consecutive_failures = 0, status = 'incremental' WHERE wallet = :a"
        ),
        {"a": address.lower(), "up_to": up_to_ts, "t": _now()},
    )
    session.commit()


def record_failure(session: Session, address: str, error: str) -> None:
    session.execute(
        text(
            "UPDATE sync_state SET last_error = :e, "
            "consecutive_failures = consecutive_failures + 1, status = 'error' WHERE wallet = :a"
        ),
        {"a": address.lower(), "e": error},
    )
    session.commit()
