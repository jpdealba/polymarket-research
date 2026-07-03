"""Phase 4 holdings data-quality report (ADR 0002/0006).

Diagnoses the warnings `pmr replay holdings` surfaces but doesn't explain:
negative post-replay balances, MERGE/REDEEM events skipped for lacking token
metadata, holdings rows with no token dimension row, and event types outside
`ledger.model.KNOWN_EVENT_TYPES` (e.g. CONVERSION). Read-only: queries
`holdings` + `wallet_events` + `tokens`/`markets`, never mutates them.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..ledger.model import KNOWN_EVENT_TYPES, normalize_condition_id
from ..ledger.replay import stream_events

_DUST = Decimal("0.000001")


def _decimal(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


@dataclass(frozen=True)
class NegativeHoldingRow:
    token_id: str
    qty: Decimal
    wac_cost: Decimal
    as_of_ts: int
    condition_id: Optional[str]
    outcome_label: Optional[str]
    question: Optional[str]
    category: Optional[str]
    closed: Optional[bool]
    cause_event_id: Optional[int]
    cause_event_type: Optional[str]
    cause_event_ts: Optional[int]


@dataclass(frozen=True)
class NegativeHoldingsSummary:
    negative_token_count: int
    negative_condition_count: int
    paired_equal_magnitude_conditions: int
    total_negative_qty: Decimal
    cause_event_type_counts: dict[str, int]


@dataclass(frozen=True)
class MissingConditionRow:
    condition_id: str
    event_count: int
    event_types: str
    total_usdc_size: Decimal
    first_ts: int
    last_ts: int
    classification: str
    normalized_match_question: Optional[str]


@dataclass(frozen=True)
class MissingTokenRow:
    token_id: str
    qty: Decimal
    wac_cost: Decimal
    as_of_ts: int


@dataclass(frozen=True)
class UndocumentedEventRow:
    id: int
    event_type: str
    ts: int
    condition_id: Optional[str]
    usdc_size: Decimal
    tx_hash: str
    raw_ref: int


def _fetch_negative_token_ids(session: Session, wallet: str, dust_epsilon: Decimal) -> dict[str, tuple[Decimal, Decimal, int]]:
    rows = session.execute(
        text("SELECT token_id, qty, wac_cost, as_of_ts FROM holdings WHERE wallet = :w"),
        {"w": wallet.lower()},
    ).fetchall()
    return {
        row.token_id: (_decimal(row.qty), _decimal(row.wac_cost), row.as_of_ts)
        for row in rows
        if _decimal(row.qty) < -dust_epsilon
    }


def _diagnose_causes(
    session: Session, wallet: str, negative_token_ids: set[str]
) -> dict[str, tuple[int, str, int]]:
    """Single replay pass recording, for each negative token, the last event
    that touched it and left it negative from that point to the end of the
    stream — the event responsible for the final negative balance. Mirrors
    `projections.holdings.rebuild_holdings`'s event semantics without
    persisting a full per-token history (only negative tokens are tracked)."""
    cond_tokens: dict[str, list[str]] = {}
    for row in session.execute(
        text("SELECT condition_id, token_id FROM tokens ORDER BY condition_id, outcome_index")
    ):
        cond_tokens.setdefault(row.condition_id, []).append(row.token_id)

    qty: dict[str, Decimal] = {}
    last_negative_run_start: dict[str, tuple[int, str, int]] = {}

    def touch(token_id: str, new_qty: Decimal, event_id: int, event_type: str, ts: int) -> None:
        if token_id not in negative_token_ids:
            return
        if new_qty < 0:
            if token_id not in last_negative_run_start:
                last_negative_run_start[token_id] = (event_id, event_type, ts)
        else:
            last_negative_run_start.pop(token_id, None)

    for event in stream_events(session, wallet=wallet):
        etype = event.event_type
        if etype == "TRADE":
            if event.token_id is None:
                continue
            delta = _decimal(event.delta_shares)
            new_qty = qty.get(event.token_id, Decimal(0)) + delta
            qty[event.token_id] = new_qty
            touch(event.token_id, new_qty, event.id, etype, event.ts)
        elif etype in ("MERGE", "SPLIT", "REDEEM"):
            tokens = cond_tokens.get(event.condition_id or "")
            if not tokens:
                continue
            if etype == "REDEEM":
                for token_id in tokens:
                    qty[token_id] = Decimal(0)
                    touch(token_id, Decimal(0), event.id, etype, event.ts)
            elif etype == "MERGE":
                size = -_decimal(event.delta_shares)
                for token_id in tokens:
                    new_qty = qty.get(token_id, Decimal(0)) - size
                    qty[token_id] = new_qty
                    touch(token_id, new_qty, event.id, etype, event.ts)
            else:  # SPLIT
                size = _decimal(event.delta_shares)
                for token_id in tokens:
                    new_qty = qty.get(token_id, Decimal(0)) + size
                    qty[token_id] = new_qty
                    touch(token_id, new_qty, event.id, etype, event.ts)

    return last_negative_run_start


def negative_holdings_report(
    session: Session, wallet: str, *, dust_epsilon: Decimal = _DUST
) -> tuple[list[NegativeHoldingRow], NegativeHoldingsSummary]:
    negatives = _fetch_negative_token_ids(session, wallet, dust_epsilon)
    if not negatives:
        return [], NegativeHoldingsSummary(0, 0, 0, Decimal(0), {})

    causes = _diagnose_causes(session, wallet, set(negatives))

    meta_rows = session.execute(
        text(
            "SELECT h.token_id, t.condition_id, t.outcome_label, m.question, m.category, m.closed "
            "FROM holdings h "
            "LEFT JOIN tokens t ON t.token_id = h.token_id "
            "LEFT JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE h.wallet = :w"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    meta_by_token = {row.token_id: row for row in meta_rows}

    rows: list[NegativeHoldingRow] = []
    by_condition: dict[Optional[str], list[Decimal]] = {}
    cause_type_counts: dict[str, int] = {}
    total_negative_qty = Decimal(0)

    for token_id, (qty, wac_cost, as_of_ts) in negatives.items():
        meta = meta_by_token.get(token_id)
        cause = causes.get(token_id)
        rows.append(
            NegativeHoldingRow(
                token_id=token_id,
                qty=qty,
                wac_cost=wac_cost,
                as_of_ts=as_of_ts,
                condition_id=meta.condition_id if meta else None,
                outcome_label=meta.outcome_label if meta else None,
                question=meta.question if meta else None,
                category=meta.category if meta else None,
                closed=bool(meta.closed) if meta and meta.closed is not None else None,
                cause_event_id=cause[0] if cause else None,
                cause_event_type=cause[1] if cause else None,
                cause_event_ts=cause[2] if cause else None,
            )
        )
        total_negative_qty += qty
        by_condition.setdefault(meta.condition_id if meta else None, []).append(qty)
        if cause:
            cause_type_counts[cause[1]] = cause_type_counts.get(cause[1], 0) + 1
        else:
            cause_type_counts["unknown"] = cause_type_counts.get("unknown", 0) + 1

    paired = 0
    for cond, qtys in by_condition.items():
        if cond is not None and len(qtys) == 2 and abs(qtys[0] - qtys[1]) <= Decimal("0.01"):
            paired += 1

    rows.sort(key=lambda r: (r.condition_id or "", r.token_id))
    summary = NegativeHoldingsSummary(
        negative_token_count=len(rows),
        negative_condition_count=len(by_condition),
        paired_equal_magnitude_conditions=paired,
        total_negative_qty=total_negative_qty,
        cause_event_type_counts=cause_type_counts,
    )
    return rows, summary


def missing_conditions_report(session: Session, wallet: str) -> list[MissingConditionRow]:
    rows = session.execute(
        text(
            "SELECT we.condition_id, COUNT(*) n_events, "
            "GROUP_CONCAT(DISTINCT we.event_type) event_types, "
            "SUM(CAST(we.usdc_size AS REAL)) total_usdc, "
            "MIN(we.ts) first_ts, MAX(we.ts) last_ts "
            "FROM wallet_events we "
            "LEFT JOIN markets m ON m.condition_id = we.condition_id "
            "WHERE lower(we.wallet) = lower(:w) AND we.condition_id IS NOT NULL AND m.condition_id IS NULL "
            "GROUP BY we.condition_id "
            "ORDER BY n_events DESC"
        ),
        {"w": wallet},
    ).fetchall()

    out: list[MissingConditionRow] = []
    for row in rows:
        normalized = normalize_condition_id(row.condition_id)
        match = None
        if normalized != row.condition_id:
            match = session.execute(
                text("SELECT question FROM markets WHERE condition_id = :c"),
                {"c": normalized},
            ).fetchone()
        classification = "encoding_bug_bytea_prefix" if match else "unavailable_upstream"
        out.append(
            MissingConditionRow(
                condition_id=row.condition_id,
                event_count=row.n_events,
                event_types=row.event_types,
                total_usdc_size=_decimal(row.total_usdc),
                first_ts=row.first_ts,
                last_ts=row.last_ts,
                classification=classification,
                normalized_match_question=match.question if match else None,
            )
        )
    return out


def missing_token_metadata_report(
    session: Session, wallet: str, *, dust_epsilon: Decimal = _DUST
) -> list[MissingTokenRow]:
    rows = session.execute(
        text(
            "SELECT h.token_id, h.qty, h.wac_cost, h.as_of_ts "
            "FROM holdings h "
            "LEFT JOIN tokens t ON t.token_id = h.token_id "
            "WHERE h.wallet = :w AND t.token_id IS NULL"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    return [
        MissingTokenRow(
            token_id=row.token_id,
            qty=_decimal(row.qty),
            wac_cost=_decimal(row.wac_cost),
            as_of_ts=row.as_of_ts,
        )
        for row in rows
        if abs(_decimal(row.qty)) > dust_epsilon
    ]


def undocumented_events_report(session: Session, wallet: str) -> list[UndocumentedEventRow]:
    placeholders = ", ".join(f":t{i}" for i in range(len(KNOWN_EVENT_TYPES)))
    params = {f"t{i}": t for i, t in enumerate(KNOWN_EVENT_TYPES)}
    params["w"] = wallet.lower()
    rows = session.execute(
        text(
            "SELECT id, event_type, ts, condition_id, usdc_size, tx_hash, raw_ref "
            "FROM wallet_events "
            f"WHERE wallet = :w AND event_type NOT IN ({placeholders}) "
            "ORDER BY ts, id"
        ),
        params,
    ).fetchall()
    return [
        UndocumentedEventRow(
            id=row.id,
            event_type=row.event_type,
            ts=row.ts,
            condition_id=row.condition_id,
            usdc_size=_decimal(row.usdc_size),
            tx_hash=row.tx_hash,
            raw_ref=row.raw_ref,
        )
        for row in rows
    ]
