"""Daily exposure snapshots projection (Phase 10).

Replays a wallet's ledger day by day (UTC day boundaries, exactly like
`daily_equity`), maintaining per-token QUANTITY only — exposure needs quantity
over time, not cost/WAC. At each UTC day-end it snapshots:

  * `exposures_daily` — one row per condition the wallet holds ANY nonzero
    token position in: market-level directional + bond (binary /
    negRisk-event-member) or unclassified (no decomposition), plus the
    market's structure_type and event_id.
  * `event_exposures_daily` — one row per negRisk event the wallet holds
    member positions in: the per-condition directional exposure vector and its
    net_after_exclusivity (see `exposure/negrisk.py` for the netting rule).

Quantity semantics reuse the holdings projection's rules (Phase 4):
  TRADE   token-scoped add/remove of shares.
  SPLIT   condition-scoped: +size shares to every mapped token (bond rises).
  MERGE   condition-scoped: -size shares from every mapped token (bond drops).
  REDEEM  condition-scoped: zeroes every mapped token.
  RESOLUTION_SETTLEMENT  token-scoped: zeroes the settled token.
  REWARD / rebates / unknown types: no quantity effect.

Drop-and-rebuild per wallet, deterministic, batched commits. All arithmetic is
Decimal end-to-end; numeric fields persist as decimal strings.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Callable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..exposure import negrisk
from ..exposure.descriptors import STRUCTURE_NEG_RISK_EVENT_MEMBER
from ..exposure.engine import market_exposure
from .base import Projection

logger = logging.getLogger(__name__)

EXPOSURES_PROJECTION_VERSION = 1

_ZERO = Decimal("0")
_DAY_END = time(23, 59, 59, tzinfo=timezone.utc)


@dataclass(frozen=True)
class ExposuresStats:
    wallet: str
    condition_rows: int
    event_rows: int
    first_date: str | None
    last_date: str | None
    unknown_structure_warnings: int


@dataclass(frozen=True)
class ExposuresProgress:
    wallet: str
    stage: str
    events_processed: int
    events_total: int
    current_date: str | None
    condition_rows: int
    event_rows: int


@dataclass(frozen=True)
class ExposureRow:
    wallet: str
    condition_id: str
    date: str
    directional: Decimal | None
    bond: Decimal | None
    structure_type: str
    event_id: str | None
    projection_version: int


@dataclass(frozen=True)
class EventExposureRow:
    wallet: str
    event_id: str
    date: str
    exposure_vector: dict[str, str]
    net_after_exclusivity: Decimal
    projection_version: int


_BOUNDS_SQL = text(
    "SELECT COUNT(*) AS event_count, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
    "FROM wallet_events WHERE wallet = :wallet"
)

_EVENTS_SQL = text(
    "SELECT id, event_type, ts, condition_id, token_id, delta_shares "
    "FROM wallet_events WHERE wallet = :wallet ORDER BY ts, id"
)

_INSERT_EXPOSURE_SQL = text(
    "INSERT INTO exposures_daily "
    "(wallet, condition_id, date, directional, bond, structure_type, event_id, "
    "projection_version) "
    "VALUES (:wallet, :condition_id, :date, :directional, :bond, :structure_type, "
    ":event_id, :projection_version)"
)

_INSERT_EVENT_SQL = text(
    "INSERT INTO event_exposures_daily "
    "(wallet, event_id, date, exposure_vector_json, net_after_exclusivity, "
    "projection_version) "
    "VALUES (:wallet, :event_id, :date, :exposure_vector_json, "
    ":net_after_exclusivity, :projection_version)"
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


def _utc_date(ts: int) -> date:
    return datetime.fromtimestamp(ts, timezone.utc).date()


def _load_market_meta(
    session: Session,
) -> tuple[dict[str, list[str]], dict[str, tuple[str | None, str | None]], dict[str, list[str]]]:
    """Return (condition_tokens, condition_meta, event_conditions).

    condition_tokens: condition_id -> token_ids ordered by outcome_index.
    condition_meta:   condition_id -> (structure_type, event_id).
    event_conditions: event_id -> negRisk member condition_ids (sorted).
    """
    rows = session.execute(
        text(
            "SELECT t.condition_id, t.token_id, m.structure_type, m.event_id "
            "FROM tokens t JOIN markets m ON m.condition_id = t.condition_id "
            "ORDER BY t.condition_id, t.outcome_index"
        )
    ).fetchall()
    condition_tokens: dict[str, list[str]] = {}
    condition_meta: dict[str, tuple[str | None, str | None]] = {}
    event_conditions: dict[str, list[str]] = {}
    for row in rows:
        condition_tokens.setdefault(row.condition_id, []).append(row.token_id)
        if row.condition_id not in condition_meta:
            condition_meta[row.condition_id] = (row.structure_type, row.event_id)
            if row.structure_type == STRUCTURE_NEG_RISK_EVENT_MEMBER and row.event_id:
                event_conditions.setdefault(row.event_id, [])
                if row.condition_id not in event_conditions[row.event_id]:
                    event_conditions[row.event_id].append(row.condition_id)
    for event_id in event_conditions:
        event_conditions[event_id].sort()
    return condition_tokens, condition_meta, event_conditions


def rebuild_exposures(
    session: Session,
    wallet: str,
    *,
    dust_epsilon: Decimal = Decimal("0.000001"),
    through_date: date | None = None,
    progress_fn: Callable[[ExposuresProgress], None] | None = None,
    batch_size: int = 25,
    event_progress_interval: int = 100000,
) -> ExposuresStats:
    """Drop and rebuild both exposure projections for one wallet."""
    wallet = wallet.lower()
    bounds = session.execute(_BOUNDS_SQL, {"wallet": wallet}).fetchone()
    session.execute(
        text("DELETE FROM exposures_daily WHERE wallet = :wallet"), {"wallet": wallet}
    )
    session.execute(
        text("DELETE FROM event_exposures_daily WHERE wallet = :wallet"),
        {"wallet": wallet},
    )
    session.commit()
    if bounds is None or int(bounds.event_count or 0) == 0:
        _emit(progress_fn, wallet, "empty", 0, 0, None, 0, 0)
        return ExposuresStats(wallet, 0, 0, None, None, 0)

    condition_tokens, condition_meta, event_conditions = _load_market_meta(session)
    # token_id -> condition_id (for building per-condition token maps quickly).
    token_condition: dict[str, str] = {}
    for cond, tokens in condition_tokens.items():
        for token_id in tokens:
            token_condition[token_id] = cond

    positions: dict[str, Decimal] = {}
    # condition_id -> set of token_ids the wallet has touched (for snapshots).
    touched_conditions: set[str] = set()

    events_total = int(bounds.event_count or 0)
    events_processed = 0
    condition_rows = 0
    event_rows = 0
    unknown_warnings = 0
    exposure_buffer: list[dict] = []
    event_buffer: list[dict] = []

    first_day = _utc_date(int(bounds.min_ts))
    last_event_day = _utc_date(int(bounds.max_ts))
    end_day = through_date or max(last_event_day, datetime.now(timezone.utc).date())

    event_iter = iter(
        session.execute(
            _EVENTS_SQL.execution_options(stream_results=True), {"wallet": wallet}
        )
    )
    next_event = next(event_iter, None)
    current_day = first_day

    _emit(
        progress_fn, wallet, "start", 0, events_total,
        current_day.isoformat(), 0, 0,
    )

    def qty(token_id: str) -> Decimal:
        return positions.get(token_id, _ZERO)

    def touch(condition_id: str | None) -> None:
        if condition_id:
            touched_conditions.add(condition_id)

    def apply_event(event) -> None:
        etype = event.event_type
        condition_id = event.condition_id
        if etype == "TRADE":
            if event.token_id is None:
                return
            positions[event.token_id] = qty(event.token_id) + _decimal(event.delta_shares)
            touch(token_condition.get(event.token_id) or condition_id)
            return
        if etype == "SPLIT":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                return
            size = _decimal(event.delta_shares)
            for token_id in tokens:
                positions[token_id] = qty(token_id) + size
            touch(condition_id)
            return
        if etype == "MERGE":
            tokens = condition_tokens.get(condition_id or "", [])
            if not tokens:
                return
            size = -_decimal(event.delta_shares)
            for token_id in tokens:
                positions[token_id] = qty(token_id) - size
            touch(condition_id)
            return
        if etype == "REDEEM":
            tokens = condition_tokens.get(condition_id or "", [])
            for token_id in tokens:
                positions[token_id] = _ZERO
            touch(condition_id)
            return
        if etype == "RESOLUTION_SETTLEMENT":
            if event.token_id is not None:
                positions[event.token_id] = _ZERO
                touch(token_condition.get(event.token_id) or condition_id)
            return
        # REWARD / rebates / unknown types: no quantity effect.

    def snapshot(day: date) -> None:
        nonlocal condition_rows, event_rows, unknown_warnings
        iso = day.isoformat()
        # Market-level rows: every condition with any nonzero token position.
        active_conditions: set[str] = set()
        for condition_id in sorted(touched_conditions):
            tokens = condition_tokens.get(condition_id, [])
            token_ids = tokens or [
                tid for tid in positions if token_condition.get(tid) == condition_id
            ]
            if not any(abs(qty(tid)) > dust_epsilon for tid in token_ids):
                continue
            active_conditions.add(condition_id)
            structure_type, event_id = condition_meta.get(condition_id, (None, None))
            qty_by_token = {tid: qty(tid) for tid in token_ids}
            me = market_exposure(structure_type, token_ids, qty_by_token)
            if me.unknown_structure:
                unknown_warnings += 1
            exposure_buffer.append(
                {
                    "wallet": wallet,
                    "condition_id": condition_id,
                    "date": iso,
                    "directional": None if me.directional is None else str(me.directional),
                    "bond": None if me.bond is None else str(me.bond),
                    "structure_type": me.structure_type,
                    "event_id": event_id,
                    "projection_version": EXPOSURES_PROJECTION_VERSION,
                }
            )
        # Event-level rows: negRisk events with any member position.
        for event_id in sorted(event_conditions):
            siblings: list[tuple[str, Decimal, Decimal]] = []
            for condition_id in event_conditions[event_id]:
                tokens = condition_tokens.get(condition_id, [])
                if len(tokens) != 2:
                    continue
                q0 = qty(tokens[0])
                q1 = qty(tokens[1])
                if abs(q0) > dust_epsilon or abs(q1) > dust_epsilon:
                    siblings.append((condition_id, q0, q1))
            if not siblings:
                continue
            ev = negrisk.event_exposure(event_id, siblings)
            event_buffer.append(
                {
                    "wallet": wallet,
                    "event_id": event_id,
                    "date": iso,
                    "exposure_vector_json": negrisk.vector_json(ev.exposure_vector),
                    "net_after_exclusivity": str(ev.net_after_exclusivity),
                    "projection_version": EXPOSURES_PROJECTION_VERSION,
                }
            )

    def flush(stage: str, current: date | None) -> None:
        nonlocal condition_rows, event_rows
        if exposure_buffer:
            session.execute(_INSERT_EXPOSURE_SQL, exposure_buffer)
            condition_rows += len(exposure_buffer)
            exposure_buffer.clear()
        if event_buffer:
            session.execute(_INSERT_EVENT_SQL, event_buffer)
            event_rows += len(event_buffer)
            event_buffer.clear()
        session.commit()
        _emit(
            progress_fn, wallet, stage, events_processed, events_total,
            current.isoformat() if current else None, condition_rows, event_rows,
        )

    days_since_flush = 0
    while current_day <= end_day:
        next_day = current_day + timedelta(days=1)
        while next_event is not None and _utc_date(int(next_event.ts)) < next_day:
            apply_event(next_event)
            events_processed += 1
            if event_progress_interval > 0 and events_processed % event_progress_interval == 0:
                _emit(
                    progress_fn, wallet, "events", events_processed, events_total,
                    current_day.isoformat(), condition_rows, event_rows,
                )
            next_event = next(event_iter, None)
        snapshot(current_day)
        days_since_flush += 1
        if days_since_flush >= batch_size:
            flush("flush", current_day)
            days_since_flush = 0
        current_day = next_day

    flush("flush", end_day)

    return ExposuresStats(
        wallet=wallet,
        condition_rows=condition_rows,
        event_rows=event_rows,
        first_date=first_day.isoformat() if condition_rows or event_rows else None,
        last_date=end_day.isoformat() if condition_rows or event_rows else None,
        unknown_structure_warnings=unknown_warnings,
    )


def _emit(
    progress_fn: Callable[[ExposuresProgress], None] | None,
    wallet: str,
    stage: str,
    events_processed: int,
    events_total: int,
    current_date: str | None,
    condition_rows: int,
    event_rows: int,
) -> None:
    if progress_fn is None:
        return
    progress_fn(
        ExposuresProgress(
            wallet=wallet,
            stage=stage,
            events_processed=events_processed,
            events_total=events_total,
            current_date=current_date,
            condition_rows=condition_rows,
            event_rows=event_rows,
        )
    )


def fetch_exposures(
    session: Session, wallet: str, *, condition_id: str | None = None
) -> list[ExposureRow]:
    sql = (
        "SELECT wallet, condition_id, date, directional, bond, structure_type, "
        "event_id, projection_version FROM exposures_daily WHERE wallet = :wallet"
    )
    params: dict = {"wallet": wallet.lower()}
    if condition_id is not None:
        sql += " AND condition_id = :condition_id"
        params["condition_id"] = condition_id
    sql += " ORDER BY condition_id, date"
    rows = session.execute(text(sql), params).fetchall()
    return [
        ExposureRow(
            wallet=row.wallet,
            condition_id=row.condition_id,
            date=row.date,
            directional=None if row.directional is None else _decimal(row.directional),
            bond=None if row.bond is None else _decimal(row.bond),
            structure_type=row.structure_type,
            event_id=row.event_id,
            projection_version=int(row.projection_version),
        )
        for row in rows
    ]


def fetch_event_exposures(
    session: Session, wallet: str, *, event_id: str | None = None
) -> list[EventExposureRow]:
    import json

    sql = (
        "SELECT wallet, event_id, date, exposure_vector_json, net_after_exclusivity, "
        "projection_version FROM event_exposures_daily WHERE wallet = :wallet"
    )
    params: dict = {"wallet": wallet.lower()}
    if event_id is not None:
        sql += " AND event_id = :event_id"
        params["event_id"] = event_id
    sql += " ORDER BY event_id, date"
    rows = session.execute(text(sql), params).fetchall()
    return [
        EventExposureRow(
            wallet=row.wallet,
            event_id=row.event_id,
            date=row.date,
            exposure_vector=json.loads(row.exposure_vector_json),
            net_after_exclusivity=_decimal(row.net_after_exclusivity),
            projection_version=int(row.projection_version),
        )
        for row in rows
    ]


class ExposuresProjection(Projection):
    name = "exposures"
    version = EXPOSURES_PROJECTION_VERSION

    def __init__(self, dust_epsilon: Decimal = Decimal("0.000001")) -> None:
        self.dust_epsilon = dust_epsilon

    def rebuild(self, session: Session, wallet: str) -> ExposuresStats:
        return rebuild_exposures(session, wallet, dust_epsilon=self.dust_epsilon)
