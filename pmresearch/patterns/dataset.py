"""Phase 22.5: descriptive order timing and pattern mining dataset.

This module is intentionally read-only over existing Phase 18/20/22 artifacts.
It writes CSV/Markdown evidence files only; it does not create strategies or
change simulation behavior.
"""

from __future__ import annotations

import csv
import json
import shutil
import statistics
from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable, Optional

from sqlalchemy import bindparam, inspect, text
from sqlalchemy.orm import Session

_ZERO = Decimal("0")
_DUST = Decimal("0.000001")

CONTEXT_ORDER = {
    "excellent": 5,
    "good": 4,
    "usable": 3,
    "weak": 2,
    "stale": 1,
    "missing": 0,
}

OUTPUT_FILES = (
    "order_timing_dataset.csv",
    "condition_inventory_timeline.csv",
    "pair_completion_report.csv",
    "merge_timing_report.csv",
    "sibling_market_sequence_report.csv",
    "unpaired_inventory_duration_report.csv",
    "pattern_mining_summary.md",
)

ORDER_TIMING_COLUMNS = [
    "wallet", "wallet_label", "fill_event_id", "tx_hash", "event_id", "event_title",
    "condition_id", "question", "market_family", "token_id", "outcome_label", "side",
    "role", "order_hash", "fill_ts", "fill_utc", "fill_price", "fill_size",
    "fill_notional_usdc", "order_created_ts", "order_created_utc",
    "order_first_seen_book_ts", "order_first_seen_book_utc", "order_last_seen_book_ts",
    "order_last_seen_book_utc", "order_cancelled_ts", "order_lifetime_s",
    "fill_after_created_s", "fill_after_first_seen_s", "order_time_confidence",
    "yes_token_id", "no_token_id", "yes_outcome_label", "no_outcome_label",
    "qty_yes_before", "qty_no_before", "qty_yes_after", "qty_no_after",
    "cost_yes_before", "cost_no_before", "cost_yes_after", "cost_no_after",
    "wac_yes_before", "wac_no_before", "wac_yes_after", "wac_no_after",
    "market_value_yes_before", "market_value_no_before", "market_value_yes_after",
    "market_value_no_after", "paired_qty_before", "paired_qty_after",
    "unpaired_yes_before", "unpaired_no_before", "unpaired_yes_after",
    "unpaired_no_after", "bond_cost_before", "bond_cost_after", "bond_delta",
    "unpaired_delta", "directional_before", "directional_after", "directional_delta",
    "event_total_cost_before", "event_total_cost_after", "event_total_qty_before",
    "event_total_qty_after", "event_bond_qty_before", "event_bond_qty_after",
    "event_unpaired_inventory_before", "event_unpaired_inventory_after",
    "event_exposure_before", "event_exposure_after", "event_market_count_active_before",
    "event_market_count_active_after", "event_capital_used_before",
    "event_capital_used_after", "event_phase", "time_to_start_s",
    "time_since_start_s", "fill_token_side", "feature_availability", "null_reasons_json",
]

TIMELINE_COLUMNS = [
    "wallet", "fill_event_id", "event_id", "condition_id", "question", "fill_ts",
    "fill_utc", "yes_token_id", "no_token_id", "qty_yes_before", "qty_no_before",
    "qty_yes_after", "qty_no_after", "cost_yes_before", "cost_no_before",
    "cost_yes_after", "cost_no_after", "paired_qty_before", "paired_qty_after",
    "unpaired_yes_before", "unpaired_no_before", "unpaired_yes_after",
    "unpaired_no_after", "event_bond_qty_before", "event_bond_qty_after",
    "event_unpaired_inventory_before", "event_unpaired_inventory_after",
]

PAIR_COMPLETION_COLUMNS = [
    "wallet", "event_id", "condition_id", "question", "first_leg_token_id",
    "first_leg_label", "first_leg_ts", "first_leg_price", "first_leg_qty",
    "complement_token_id", "complement_label", "complement_fill_ts",
    "complement_fill_price", "complement_fill_qty", "time_to_complement_s",
    "completed_pair_qty", "complete_set_cost", "edge_per_set", "edge_bps",
    "completion_confidence", "time_bucket", "cost_bucket",
]

MERGE_TIMING_COLUMNS = [
    "wallet", "event_id", "condition_id", "question", "merge_ts", "merge_utc",
    "merge_qty", "merge_usdc_released", "time_from_start_s", "time_from_last_fill_s",
    "time_from_last_complement_fill_s", "paired_qty_before_merge",
    "unpaired_yes_before_merge", "unpaired_no_before_merge", "paired_qty_after_merge",
    "capital_released", "merge_batch_id",
]

SIBLING_SEQUENCE_COLUMNS = [
    "wallet", "event_id", "anchor_condition_id", "anchor_question",
    "anchor_market_family", "anchor_ts", "sibling_condition_id", "sibling_question",
    "sibling_market_family", "sibling_ts", "delta_s", "same_side",
    "same_outcome_label", "price_anchor", "price_sibling", "sequence_type",
]

UNPAIRED_DURATION_COLUMNS = [
    "wallet", "event_id", "condition_id", "question", "unpaired_start_ts",
    "unpaired_end_ts", "duration_s", "dominant_side", "max_unpaired_qty",
    "max_unpaired_cost", "resolved_by", "final_pnl_if_available",
]


@dataclass(frozen=True)
class PatternBuildStats:
    wallet: str
    out_dir: Path
    fills: int
    pair_completions: int
    merges: int
    sibling_sequences: int
    unpaired_periods: int


@dataclass(frozen=True)
class TokenPair:
    yes_token_id: str
    no_token_id: str
    yes_outcome_label: str | None
    no_outcome_label: str | None

    def complement(self, token_id: str) -> str | None:
        if token_id == self.yes_token_id:
            return self.no_token_id
        if token_id == self.no_token_id:
            return self.yes_token_id
        return None

    def side_name(self, token_id: str) -> str | None:
        if token_id == self.yes_token_id:
            return "YES"
        if token_id == self.no_token_id:
            return "NO"
        return None

    def label(self, token_id: str) -> str | None:
        if token_id == self.yes_token_id:
            return self.yes_outcome_label
        if token_id == self.no_token_id:
            return self.no_outcome_label
        return None


def dec(value) -> Decimal:
    if value is None:
        return _ZERO
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return _ZERO


def opt_dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def dstr(value: Decimal | int | float | None) -> str | None:
    if value is None:
        return None
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return str(value)


def utc(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(int(ts), tz=timezone.utc).isoformat()


def paired_qty(qty_yes: Decimal, qty_no: Decimal) -> Decimal:
    return min(qty_yes, qty_no)


def unpaired_yes(qty_yes: Decimal, qty_no: Decimal) -> Decimal:
    return max(qty_yes - qty_no, _ZERO)


def unpaired_no(qty_yes: Decimal, qty_no: Decimal) -> Decimal:
    return max(qty_no - qty_yes, _ZERO)


def wac(qty: Decimal, cost: Decimal) -> Decimal | None:
    if abs(qty) <= _DUST:
        return None
    return cost / qty


def complete_set_cost(first_price: Decimal, complement_price: Decimal) -> Decimal:
    return first_price + complement_price


def edge_per_set(cost: Decimal) -> Decimal:
    return Decimal("1") - cost


def time_bucket(seconds: int | None) -> str:
    if seconds is None:
        return "not_completed"
    if seconds < 10:
        return "under_10s"
    if seconds < 60:
        return "under_60s"
    if seconds < 5 * 60:
        return "1m_5m"
    if seconds < 15 * 60:
        return "5m_15m"
    if seconds < 60 * 60:
        return "15m_60m"
    return "over_60m"


def cost_bucket(cost: Decimal | None) -> str:
    if cost is None:
        return "unknown"
    if cost < Decimal("0.90"):
        return "lt_0_90"
    if cost < Decimal("0.95"):
        return "0_90_0_95"
    if cost < Decimal("0.98"):
        return "0_95_0_98"
    if cost <= Decimal("1.00"):
        return "0_98_1_00"
    return "gt_1_00"


def _table_exists(session: Session, table_name: str) -> bool:
    return inspect(session.bind).has_table(table_name)


def _columns(session: Session, table_name: str) -> set[str]:
    if not _table_exists(session, table_name):
        return set()
    return {row["name"] for row in inspect(session.bind).get_columns(table_name)}


def _token_pairs(session: Session) -> dict[str, TokenPair]:
    rows = session.execute(
        text(
            "SELECT token_id, condition_id, outcome_index, outcome_label "
            "FROM tokens ORDER BY condition_id, outcome_index"
        )
    ).fetchall()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row.condition_id, []).append(row)
    pairs: dict[str, TokenPair] = {}
    for condition_id, toks in grouped.items():
        if len(toks) != 2:
            continue
        by_index = {int(t.outcome_index): t for t in toks}
        if 0 not in by_index or 1 not in by_index:
            continue
        pairs[condition_id] = TokenPair(
            yes_token_id=str(by_index[0].token_id),
            no_token_id=str(by_index[1].token_id),
            yes_outcome_label=by_index[0].outcome_label,
            no_outcome_label=by_index[1].outcome_label,
        )
    return pairs


def _wallet_labels(session: Session) -> dict[str, str | None]:
    if not _table_exists(session, "wallets"):
        return {}
    return {
        r.address.lower(): r.display_name
        for r in session.execute(text("SELECT address, display_name FROM wallets")).fetchall()
    }


def _wallet_scope(session: Session, wallet: str, *, include_gap_wallet: bool) -> list[str]:
    wallets = [wallet.lower()]
    if not include_gap_wallet or not _table_exists(session, "wallets"):
        return wallets
    cols = _columns(session, "wallets")
    active_clause = "AND is_active = 1" if "is_active" in cols else ""
    rows = session.execute(
        text(
            "SELECT address FROM wallets "
            f"WHERE lower(COALESCE(display_name, '')) LIKE '%gap%' {active_clause}"
        )
    ).fetchall()
    for row in rows:
        addr = row.address.lower()
        if addr not in wallets:
            wallets.append(addr)
    return wallets


def _fill_rows(
    session: Session,
    *,
    wallets: list[str],
    watchlist: str | None,
    event_id: str | None,
    min_context: str,
):
    if _table_exists(session, "all_fill_context"):
        threshold = CONTEXT_ORDER[min_context]
        statuses = [s for s, order in CONTEXT_ORDER.items() if order >= threshold]
        joins = [
            "JOIN wallet_events we ON we.id = afc.event_id",
            "LEFT JOIN fill_enrichment fe ON fe.event_id = we.id",
            "LEFT JOIN markets m ON m.condition_id = afc.condition_id",
            "LEFT JOIN pm_events pe ON pe.event_id = m.event_id",
            "LEFT JOIN tokens tok ON tok.token_id = afc.token_id",
        ]
        where = [
            "afc.wallet IN :wallets",
            "afc.context_status IN :statuses",
            "afc.token_id IS NOT NULL",
        ]
        params = {"wallets": wallets, "statuses": statuses}
        if watchlist:
            joins.extend(
                [
                    "JOIN watchlists wl ON wl.name = :watchlist",
                    "JOIN watchlist_tokens wt ON wt.watchlist_id = wl.id "
                    "AND wt.token_id = afc.token_id AND wt.is_active = 1",
                ]
            )
            params["watchlist"] = watchlist
        if event_id:
            where.append("m.event_id = :event_id")
            params["event_id"] = event_id
        stmt = text(
            "SELECT afc.event_id AS fill_event_id, afc.wallet, afc.token_id, afc.condition_id, "
            "afc.trade_ts AS fill_ts, afc.trade_utc AS fill_utc, afc.side, "
            "afc.fill_price, afc.fill_shares, afc.fill_notional_usdc, afc.fill_size, "
            "COALESCE(afc.role, fe.role, 'UNKNOWN') AS role, fe.order_hash, fe.source AS enrichment_source, "
            "we.tx_hash, we.delta_shares, we.delta_usdc, afc.context_status, "
            "afc.book_before_ts, afc.book_before_age_s, afc.best_bid_before, afc.best_ask_before, "
            "afc.mid_before, afc.depth_top_before_json, "
            "m.event_id, pe.title AS event_title, m.question, m.category AS market_family, "
            "m.start_date, m.end_date, m.closed_time, tok.outcome_index, tok.outcome_label "
            "FROM all_fill_context afc "
            + " ".join(joins)
            + " WHERE "
            + " AND ".join(where)
            + " ORDER BY afc.wallet, afc.trade_ts, afc.event_id"
        ).bindparams(bindparam("wallets", expanding=True), bindparam("statuses", expanding=True))
        return session.execute(stmt, params).fetchall()

    joins = [
        "LEFT JOIN fill_enrichment fe ON fe.event_id = we.id",
        "LEFT JOIN markets m ON m.condition_id = we.condition_id",
        "LEFT JOIN pm_events pe ON pe.event_id = m.event_id",
        "LEFT JOIN tokens tok ON tok.token_id = we.token_id",
    ]
    where = ["we.wallet IN :wallets", "we.event_type = 'TRADE'", "we.token_id IS NOT NULL"]
    params = {"wallets": wallets}
    if watchlist and _table_exists(session, "watchlists"):
        joins.extend(
            [
                "JOIN watchlists wl ON wl.name = :watchlist",
                "JOIN watchlist_tokens wt ON wt.watchlist_id = wl.id "
                "AND wt.token_id = we.token_id AND wt.is_active = 1",
            ]
        )
        params["watchlist"] = watchlist
    if event_id:
        where.append("m.event_id = :event_id")
        params["event_id"] = event_id
    stmt = text(
        "SELECT we.id AS fill_event_id, we.wallet, we.token_id, we.condition_id, "
        "we.ts AS fill_ts, :empty AS fill_utc, we.side, we.price AS fill_price, "
        "CASE WHEN we.delta_shares LIKE '-%' THEN substr(we.delta_shares, 2) ELSE we.delta_shares END AS fill_shares, "
        "CASE WHEN we.delta_usdc LIKE '-%' THEN substr(we.delta_usdc, 2) ELSE we.delta_usdc END AS fill_notional_usdc, "
        "we.usdc_size AS fill_size, COALESCE(fe.role, 'UNKNOWN') AS role, fe.order_hash, "
        "fe.source AS enrichment_source, we.tx_hash, we.delta_shares, we.delta_usdc, "
        "'missing' AS context_status, NULL AS book_before_ts, NULL AS book_before_age_s, "
        "NULL AS best_bid_before, NULL AS best_ask_before, NULL AS mid_before, NULL AS depth_top_before_json, "
        "m.event_id, pe.title AS event_title, m.question, m.category AS market_family, "
        "m.start_date, m.end_date, m.closed_time, tok.outcome_index, tok.outcome_label "
        "FROM wallet_events we "
        + " ".join(joins)
        + " WHERE "
        + " AND ".join(where)
        + " ORDER BY we.wallet, we.ts, we.id"
    ).bindparams(bindparam("wallets", expanding=True))
    return session.execute(stmt, {**params, "empty": ""}).fetchall()


def _ledger_events(
    session: Session,
    wallets: list[str],
    *,
    token_ids: set[str] | None = None,
    condition_ids: set[str] | None = None,
    max_ts: int | None = None,
):
    where = ["wallet IN :wallets"]
    params: dict = {"wallets": wallets}
    bindparams = [bindparam("wallets", expanding=True)]
    if token_ids and condition_ids:
        where.append("(token_id IN :token_ids OR condition_id IN :condition_ids)")
        params["token_ids"] = sorted(token_ids)
        params["condition_ids"] = sorted(condition_ids)
        bindparams.extend(
            [bindparam("token_ids", expanding=True), bindparam("condition_ids", expanding=True)]
        )
    elif token_ids:
        where.append("token_id IN :token_ids")
        params["token_ids"] = sorted(token_ids)
        bindparams.append(bindparam("token_ids", expanding=True))
    elif condition_ids:
        where.append("condition_id IN :condition_ids")
        params["condition_ids"] = sorted(condition_ids)
        bindparams.append(bindparam("condition_ids", expanding=True))
    if max_ts is not None:
        where.append("ts <= :max_ts")
        params["max_ts"] = max_ts
    stmt = text(
        "SELECT id, wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
        "delta_shares, delta_usdc, price, usdc_size "
        "FROM wallet_events WHERE "
        + " AND ".join(where)
        + " ORDER BY wallet, ts, id"
    ).bindparams(*bindparams)
    result = session.execute(stmt.execution_options(stream_results=True), params)
    yield from result


def _relevant_scope(
    rows,
    pairs: dict[str, TokenPair],
    condition_event: dict[str, str | None],
) -> tuple[set[str], set[str]]:
    event_ids = {r.event_id for r in rows if r.event_id}
    condition_ids = {
        condition_id for condition_id, event_id in condition_event.items() if event_id in event_ids
    }
    condition_ids.update(r.condition_id for r in rows if r.condition_id)
    token_ids: set[str] = set()
    for condition_id in condition_ids:
        pair = pairs.get(condition_id)
        if pair is not None:
            token_ids.add(pair.yes_token_id)
            token_ids.add(pair.no_token_id)
    return token_ids, condition_ids


def _apply_trade(
    qty: dict[str, Decimal],
    cost: dict[str, Decimal],
    token_id: str,
    delta_shares: Decimal,
    delta_usdc: Decimal,
) -> None:
    current_qty = qty.get(token_id, _ZERO)
    current_cost = cost.get(token_id, _ZERO)
    if delta_shares > 0:
        qty[token_id] = current_qty + delta_shares
        cost[token_id] = current_cost + abs(delta_usdc)
        return
    sell_qty = abs(delta_shares)
    if current_qty > _DUST:
        reduce_cost = current_cost * min(sell_qty, current_qty) / current_qty
    else:
        reduce_cost = _ZERO
    qty[token_id] = current_qty - sell_qty
    cost[token_id] = max(current_cost - reduce_cost, _ZERO)


def _apply_condition_event(
    qty: dict[str, Decimal],
    cost: dict[str, Decimal],
    pair: TokenPair,
    event_type: str,
    delta_shares: Decimal,
    delta_usdc: Decimal,
) -> None:
    amount = abs(delta_shares)
    if amount <= _DUST:
        amount = abs(delta_usdc)
    if event_type == "SPLIT":
        add_cost_each = abs(delta_usdc) / Decimal("2") if abs(delta_usdc) > _DUST else _ZERO
        for tid in (pair.yes_token_id, pair.no_token_id):
            qty[tid] = qty.get(tid, _ZERO) + amount
            cost[tid] = cost.get(tid, _ZERO) + add_cost_each
    elif event_type == "MERGE":
        for tid in (pair.yes_token_id, pair.no_token_id):
            current_qty = qty.get(tid, _ZERO)
            current_cost = cost.get(tid, _ZERO)
            remove = min(amount, current_qty)
            remove_cost = current_cost * remove / current_qty if current_qty > _DUST else _ZERO
            qty[tid] = current_qty - remove
            cost[tid] = max(current_cost - remove_cost, _ZERO)
    elif event_type == "REDEEM":
        for tid in (pair.yes_token_id, pair.no_token_id):
            qty[tid] = _ZERO
            cost[tid] = _ZERO


def _condition_state(qty: dict[str, Decimal], cost: dict[str, Decimal], pair: TokenPair) -> dict:
    qy = qty.get(pair.yes_token_id, _ZERO)
    qn = qty.get(pair.no_token_id, _ZERO)
    cy = cost.get(pair.yes_token_id, _ZERO)
    cn = cost.get(pair.no_token_id, _ZERO)
    paired = paired_qty(qy, qn)
    uy = unpaired_yes(qy, qn)
    un = unpaired_no(qy, qn)
    directional = qy - qn
    return {
        "qty_yes": qy,
        "qty_no": qn,
        "cost_yes": cy,
        "cost_no": cn,
        "wac_yes": wac(qy, cy),
        "wac_no": wac(qn, cn),
        "paired_qty": paired,
        "unpaired_yes": uy,
        "unpaired_no": un,
        "bond_cost": min(cy, cn),
        "directional": directional,
    }


def _event_state(
    qty: dict[str, Decimal],
    cost: dict[str, Decimal],
    pairs: dict[str, TokenPair],
    condition_event: dict[str, str | None],
    event_id: str | None,
) -> dict:
    if not event_id:
        return {
            "total_cost": _ZERO,
            "total_qty": _ZERO,
            "bond_qty": _ZERO,
            "unpaired_inventory": _ZERO,
            "exposure": _ZERO,
            "market_count_active": 0,
            "capital_used": _ZERO,
        }
    total_cost = total_qty = bond_qty = unpaired = exposure = _ZERO
    active = 0
    for condition_id, ev in condition_event.items():
        if ev != event_id:
            continue
        pair = pairs.get(condition_id)
        if pair is None:
            continue
        state = _condition_state(qty, cost, pair)
        condition_qty = state["qty_yes"] + state["qty_no"]
        condition_cost = state["cost_yes"] + state["cost_no"]
        if abs(condition_qty) > _DUST:
            active += 1
        total_qty += condition_qty
        total_cost += condition_cost
        bond_qty += state["paired_qty"]
        unpaired += state["unpaired_yes"] + state["unpaired_no"]
        exposure += abs(state["directional"])
    return {
        "total_cost": total_cost,
        "total_qty": total_qty,
        "bond_qty": bond_qty,
        "unpaired_inventory": unpaired,
        "exposure": exposure,
        "market_count_active": active,
        "capital_used": total_cost,
    }


def _event_phase(fill_ts: int, start_date: str | None, end_date: str | None, closed_time: str | None) -> tuple[str, int | None, int | None]:
    start_ts = _parse_ts(start_date)
    end_ts = _parse_ts(end_date) or _parse_ts(closed_time)
    time_to_start = None if start_ts is None else start_ts - fill_ts
    time_since_start = None if start_ts is None else fill_ts - start_ts
    if start_ts is None:
        return "unknown", time_to_start, time_since_start
    if fill_ts < start_ts:
        return "pregame", time_to_start, time_since_start
    if end_ts is not None and fill_ts > end_ts:
        return "post_event", time_to_start, time_since_start
    if end_ts is not None and end_ts > start_ts:
        progress = (fill_ts - start_ts) / (end_ts - start_ts)
        if progress < 0.35:
            return "early_live", time_to_start, time_since_start
        if 0.45 <= progress <= 0.60:
            return "halftime_or_pause", time_to_start, time_since_start
        return "late_live", time_to_start, time_since_start
    return "early_live", time_to_start, time_since_start


def _parse_ts(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def _market_values(session: Session, token_ids: Iterable[str], ts: int) -> dict[str, Decimal | None]:
    out: dict[str, Decimal | None] = {}
    for token_id in set(token_ids):
        row = session.execute(
            text(
                "SELECT mid FROM book_snapshots WHERE token_id = :token_id AND ts <= :ts "
                "ORDER BY ts DESC LIMIT 1"
            ),
            {"token_id": token_id, "ts": ts},
        ).fetchone()
        out[token_id] = opt_dec(row.mid) if row is not None else None
    return out


def _compatible_snapshot(row, *, side: str | None, fill_price: Decimal | None, tolerance_bps: int) -> bool:
    if fill_price is None or fill_price <= _ZERO:
        return False
    book_side = "bids" if side == "BUY" else "asks"
    max_diff = fill_price * Decimal(tolerance_bps) / Decimal(10000)
    prices: list[Decimal] = []
    if row.depth_top_json:
        try:
            depth = json.loads(row.depth_top_json)
        except (TypeError, ValueError):
            depth = {}
        for level in depth.get(book_side, []) if isinstance(depth, dict) else []:
            price = opt_dec(level.get("price") if isinstance(level, dict) else None)
            if price is not None:
                prices.append(price)
    best = opt_dec(row.best_bid if side == "BUY" else row.best_ask)
    if best is not None:
        prices.append(best)
    return any(abs(price - fill_price) <= max_diff for price in prices)


def _exact_order_time(session: Session, order_hash: str | None) -> dict | None:
    if not order_hash:
        return None
    for table_name in ("order_timing", "orders", "clob_orders"):
        cols = _columns(session, table_name)
        if not cols or "order_hash" not in cols:
            continue
        created_col = next((c for c in ("order_created_ts", "created_ts", "created_at_ts") if c in cols), None)
        if created_col is None:
            continue
        cancelled_col = next((c for c in ("order_cancelled_ts", "cancelled_ts") if c in cols), None)
        source_col = "source" if "source" in cols else None
        select_cancel = cancelled_col if cancelled_col else "NULL"
        select_source = source_col if source_col else "NULL"
        row = session.execute(
            text(
                f"SELECT {created_col} AS created_ts, {select_cancel} AS cancelled_ts, "
                f"{select_source} AS source FROM {table_name} WHERE lower(order_hash) = :order_hash "
                "ORDER BY created_ts LIMIT 1"
            ),
            {"order_hash": order_hash.lower()},
        ).fetchone()
        if row is not None and row.created_ts is not None:
            source = str(row.source or "").lower()
            confidence = "exact_onchain" if source in {"rpc", "onchain", "polygonscan"} else "exact_subgraph"
            return {
                "order_created_ts": int(row.created_ts),
                "order_cancelled_ts": None if row.cancelled_ts is None else int(row.cancelled_ts),
                "order_time_confidence": confidence,
            }
    return None


def resolve_order_timing(
    session: Session,
    *,
    order_hash: str | None,
    token_id: str,
    side: str | None,
    role: str | None,
    fill_ts: int,
    fill_price: Decimal | None,
    lookback_s: int,
    tolerance_bps: int,
) -> dict:
    exact = _exact_order_time(session, order_hash)
    if exact is not None:
        created = exact["order_created_ts"]
        cancelled = exact["order_cancelled_ts"]
        return {
            "order_created_ts": created,
            "order_created_utc": utc(created),
            "order_first_seen_book_ts": None,
            "order_first_seen_book_utc": None,
            "order_last_seen_book_ts": None,
            "order_last_seen_book_utc": None,
            "order_cancelled_ts": cancelled,
            "order_lifetime_s": (cancelled or fill_ts) - created,
            "fill_after_created_s": fill_ts - created,
            "fill_after_first_seen_s": None,
            "order_time_confidence": exact["order_time_confidence"],
        }

    if (role or "").lower() == "maker":
        rows = session.execute(
            text(
                "SELECT token_id, ts, best_bid, best_ask, depth_top_json "
                "FROM book_snapshots WHERE token_id = :token_id "
                "AND ts >= :start_ts AND ts <= :fill_ts ORDER BY ts ASC"
            ),
            {"token_id": token_id, "start_ts": fill_ts - lookback_s, "fill_ts": fill_ts},
        ).fetchall()
        matches = [
            int(row.ts)
            for row in rows
            if _compatible_snapshot(row, side=side, fill_price=fill_price, tolerance_bps=tolerance_bps)
        ]
        if matches:
            first_seen = matches[0]
            last_seen = matches[-1]
            return {
                "order_created_ts": None,
                "order_created_utc": None,
                "order_first_seen_book_ts": first_seen,
                "order_first_seen_book_utc": utc(first_seen),
                "order_last_seen_book_ts": last_seen,
                "order_last_seen_book_utc": utc(last_seen),
                "order_cancelled_ts": None,
                "order_lifetime_s": fill_ts - first_seen,
                "fill_after_created_s": None,
                "fill_after_first_seen_s": fill_ts - first_seen,
                "order_time_confidence": "estimated_book_seen",
            }

    return {
        "order_created_ts": None,
        "order_created_utc": None,
        "order_first_seen_book_ts": None,
        "order_first_seen_book_utc": None,
        "order_last_seen_book_ts": None,
        "order_last_seen_book_utc": None,
        "order_cancelled_ts": None,
        "order_lifetime_s": None,
        "fill_after_created_s": None,
        "fill_after_first_seen_s": None,
        "order_time_confidence": "fill_only_unknown",
    }


def _build_dataset_rows(
    session: Session,
    fills,
    *,
    pairs: dict[str, TokenPair],
    lookback_s: int,
    tolerance_bps: int,
    labels: dict[str, str | None],
    progress_callback: Callable[[str, int], None] | None = None,
) -> tuple[list[dict], list[dict], dict[int, dict]]:
    condition_event = {
        r.condition_id: r.event_id
        for r in session.execute(text("SELECT condition_id, event_id FROM markets")).fetchall()
    }
    fill_ids = {int(f.fill_event_id) for f in fills}
    fills_by_id = {int(f.fill_event_id): f for f in fills}
    wallets = sorted({f.wallet.lower() for f in fills})
    scope_tokens, scope_conditions = _relevant_scope(fills, pairs, condition_event)
    max_fill_ts = max((int(f.fill_ts) for f in fills), default=None)
    qty: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    cost: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    before_after: dict[int, dict] = {}
    timeline_rows: list[dict] = []

    for n, ev in enumerate(
        _ledger_events(
            session,
            wallets,
            token_ids=scope_tokens,
            condition_ids=scope_conditions,
            max_ts=max_fill_ts,
        ),
        start=1,
    ):
        if progress_callback and (n == 1 or n % 100000 == 0):
            progress_callback("replay_fills", n)
        wallet = ev.wallet.lower()
        q = qty.setdefault(wallet, {})
        c = cost.setdefault(wallet, {})
        pair = pairs.get(ev.condition_id or "")
        if int(ev.id) in fill_ids and ev.event_type == "TRADE" and pair is not None:
            f = fills_by_id[int(ev.id)]
            before = _condition_state(q, c, pair)
            event_before = _event_state(q, c, pairs, condition_event, f.event_id)
            _apply_trade(q, c, ev.token_id, dec(ev.delta_shares), dec(ev.delta_usdc))
            after = _condition_state(q, c, pair)
            event_after = _event_state(q, c, pairs, condition_event, f.event_id)
            before_after[int(ev.id)] = {
                "before": before,
                "after": after,
                "event_before": event_before,
                "event_after": event_after,
            }
            timeline_rows.append(
                _timeline_row(wallet, f, pair, before, after, event_before, event_after)
            )
            continue
        if ev.event_type == "TRADE" and ev.token_id:
            _apply_trade(q, c, ev.token_id, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif pair is not None and ev.event_type in {"SPLIT", "MERGE", "REDEEM"}:
            _apply_condition_event(q, c, pair, ev.event_type, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif ev.event_type == "RESOLUTION_SETTLEMENT" and ev.token_id:
            q[ev.token_id] = _ZERO
            c[ev.token_id] = _ZERO

    rows: list[dict] = []
    for fill in fills:
        pair = pairs.get(fill.condition_id or "")
        snap = before_after.get(int(fill.fill_event_id))
        null_reasons: dict[str, str] = {}
        if pair is None:
            null_reasons["binary_pair"] = "missing_or_non_binary_token_metadata"
        if snap is None:
            before = after = {
                "qty_yes": _ZERO,
                "qty_no": _ZERO,
                "cost_yes": _ZERO,
                "cost_no": _ZERO,
                "wac_yes": None,
                "wac_no": None,
                "paired_qty": _ZERO,
                "unpaired_yes": _ZERO,
                "unpaired_no": _ZERO,
                "bond_cost": _ZERO,
                "directional": _ZERO,
            }
            event_before = event_after = _event_state({}, {}, pairs, condition_event, fill.event_id)
            null_reasons["inventory"] = "missing_binary_pair_or_replay_snapshot"
        else:
            before, after = snap["before"], snap["after"]
            event_before, event_after = snap["event_before"], snap["event_after"]
        mark = _market_values(
            session,
            [] if pair is None else [pair.yes_token_id, pair.no_token_id],
            int(fill.fill_ts),
        )
        mv_y_before = None if pair is None or mark.get(pair.yes_token_id) is None else before["qty_yes"] * mark[pair.yes_token_id]
        mv_n_before = None if pair is None or mark.get(pair.no_token_id) is None else before["qty_no"] * mark[pair.no_token_id]
        mv_y_after = None if pair is None or mark.get(pair.yes_token_id) is None else after["qty_yes"] * mark[pair.yes_token_id]
        mv_n_after = None if pair is None or mark.get(pair.no_token_id) is None else after["qty_no"] * mark[pair.no_token_id]
        if pair is not None and (mark.get(pair.yes_token_id) is None or mark.get(pair.no_token_id) is None):
            null_reasons["market_value"] = "no_mark_or_book_mid"

        timing = resolve_order_timing(
            session,
            order_hash=fill.order_hash,
            token_id=fill.token_id,
            side=fill.side,
            role=fill.role,
            fill_ts=int(fill.fill_ts),
            fill_price=opt_dec(fill.fill_price),
            lookback_s=lookback_s,
            tolerance_bps=tolerance_bps,
        )
        if timing["order_time_confidence"] == "fill_only_unknown":
            null_reasons["order_timing"] = "no_exact_order_time_or_compatible_book_seen"
        elif timing["order_time_confidence"] == "estimated_book_seen":
            null_reasons["order_timing"] = "estimated_from_compatible_book_snapshot_not_wallet_attributed"

        bond_delta = after["paired_qty"] - before["paired_qty"]
        unpaired_delta = (
            after["unpaired_yes"] + after["unpaired_no"]
            - before["unpaired_yes"] - before["unpaired_no"]
        )
        directional_delta = after["directional"] - before["directional"]
        phase, tts, tss = _event_phase(int(fill.fill_ts), fill.start_date, fill.end_date, fill.closed_time)
        side_name = pair.side_name(fill.token_id) if pair else None
        rows.append(
            {
                "wallet": fill.wallet.lower(),
                "wallet_label": labels.get(fill.wallet.lower()),
                "fill_event_id": int(fill.fill_event_id),
                "tx_hash": fill.tx_hash,
                "event_id": fill.event_id,
                "event_title": fill.event_title,
                "condition_id": fill.condition_id,
                "question": fill.question,
                "market_family": fill.market_family,
                "token_id": fill.token_id,
                "outcome_label": fill.outcome_label,
                "side": fill.side,
                "role": fill.role,
                "order_hash": fill.order_hash,
                "fill_ts": int(fill.fill_ts),
                "fill_utc": fill.fill_utc or utc(int(fill.fill_ts)),
                "fill_price": fill.fill_price,
                "fill_size": fill.fill_shares,
                "fill_notional_usdc": fill.fill_notional_usdc or fill.fill_size,
                **timing,
                "yes_token_id": None if pair is None else pair.yes_token_id,
                "no_token_id": None if pair is None else pair.no_token_id,
                "yes_outcome_label": None if pair is None else pair.yes_outcome_label,
                "no_outcome_label": None if pair is None else pair.no_outcome_label,
                "qty_yes_before": dstr(before["qty_yes"]),
                "qty_no_before": dstr(before["qty_no"]),
                "qty_yes_after": dstr(after["qty_yes"]),
                "qty_no_after": dstr(after["qty_no"]),
                "cost_yes_before": dstr(before["cost_yes"]),
                "cost_no_before": dstr(before["cost_no"]),
                "cost_yes_after": dstr(after["cost_yes"]),
                "cost_no_after": dstr(after["cost_no"]),
                "wac_yes_before": dstr(before["wac_yes"]),
                "wac_no_before": dstr(before["wac_no"]),
                "wac_yes_after": dstr(after["wac_yes"]),
                "wac_no_after": dstr(after["wac_no"]),
                "market_value_yes_before": dstr(mv_y_before),
                "market_value_no_before": dstr(mv_n_before),
                "market_value_yes_after": dstr(mv_y_after),
                "market_value_no_after": dstr(mv_n_after),
                "paired_qty_before": dstr(before["paired_qty"]),
                "paired_qty_after": dstr(after["paired_qty"]),
                "unpaired_yes_before": dstr(before["unpaired_yes"]),
                "unpaired_no_before": dstr(before["unpaired_no"]),
                "unpaired_yes_after": dstr(after["unpaired_yes"]),
                "unpaired_no_after": dstr(after["unpaired_no"]),
                "bond_cost_before": dstr(before["bond_cost"]),
                "bond_cost_after": dstr(after["bond_cost"]),
                "bond_delta": dstr(bond_delta),
                "unpaired_delta": dstr(unpaired_delta),
                "directional_before": dstr(before["directional"]),
                "directional_after": dstr(after["directional"]),
                "directional_delta": dstr(directional_delta),
                "event_total_cost_before": dstr(event_before["total_cost"]),
                "event_total_cost_after": dstr(event_after["total_cost"]),
                "event_total_qty_before": dstr(event_before["total_qty"]),
                "event_total_qty_after": dstr(event_after["total_qty"]),
                "event_bond_qty_before": dstr(event_before["bond_qty"]),
                "event_bond_qty_after": dstr(event_after["bond_qty"]),
                "event_unpaired_inventory_before": dstr(event_before["unpaired_inventory"]),
                "event_unpaired_inventory_after": dstr(event_after["unpaired_inventory"]),
                "event_exposure_before": dstr(event_before["exposure"]),
                "event_exposure_after": dstr(event_after["exposure"]),
                "event_market_count_active_before": event_before["market_count_active"],
                "event_market_count_active_after": event_after["market_count_active"],
                "event_capital_used_before": dstr(event_before["capital_used"]),
                "event_capital_used_after": dstr(event_after["capital_used"]),
                "event_phase": phase,
                "time_to_start_s": tts,
                "time_since_start_s": tss,
                "fill_token_side": side_name,
                "feature_availability": "post_fill_diagnostic",
                "null_reasons_json": json.dumps(null_reasons, sort_keys=True, separators=(",", ":")),
            }
        )
        if progress_callback and len(rows) % 5000 == 0:
            progress_callback("dataset_rows", len(rows))
    return rows, timeline_rows, before_after


def _timeline_row(wallet, fill, pair, before, after, event_before, event_after) -> dict:
    return {
        "wallet": wallet,
        "fill_event_id": int(fill.fill_event_id),
        "event_id": fill.event_id,
        "condition_id": fill.condition_id,
        "question": fill.question,
        "fill_ts": int(fill.fill_ts),
        "fill_utc": fill.fill_utc or utc(int(fill.fill_ts)),
        "yes_token_id": pair.yes_token_id,
        "no_token_id": pair.no_token_id,
        "qty_yes_before": dstr(before["qty_yes"]),
        "qty_no_before": dstr(before["qty_no"]),
        "qty_yes_after": dstr(after["qty_yes"]),
        "qty_no_after": dstr(after["qty_no"]),
        "cost_yes_before": dstr(before["cost_yes"]),
        "cost_no_before": dstr(before["cost_no"]),
        "cost_yes_after": dstr(after["cost_yes"]),
        "cost_no_after": dstr(after["cost_no"]),
        "paired_qty_before": dstr(before["paired_qty"]),
        "paired_qty_after": dstr(after["paired_qty"]),
        "unpaired_yes_before": dstr(before["unpaired_yes"]),
        "unpaired_no_before": dstr(before["unpaired_no"]),
        "unpaired_yes_after": dstr(after["unpaired_yes"]),
        "unpaired_no_after": dstr(after["unpaired_no"]),
        "event_bond_qty_before": dstr(event_before["bond_qty"]),
        "event_bond_qty_after": dstr(event_after["bond_qty"]),
        "event_unpaired_inventory_before": dstr(event_before["unpaired_inventory"]),
        "event_unpaired_inventory_after": dstr(event_after["unpaired_inventory"]),
    }


def build_pair_completion_report(rows: list[dict]) -> list[dict]:
    by_condition: dict[tuple[str, str], list[dict]] = {}
    for row in rows:
        if row["side"] == "BUY" and row["yes_token_id"] and row["no_token_id"]:
            by_condition.setdefault((row["wallet"], row["condition_id"]), []).append(row)
    out: list[dict] = []
    for (_wallet, _condition), fills in by_condition.items():
        fills.sort(key=lambda r: (int(r["fill_ts"]), int(r["fill_event_id"])))
        for i, first in enumerate(fills):
            complement_token = (
                first["no_token_id"] if first["token_id"] == first["yes_token_id"] else first["yes_token_id"]
            )
            comp = next((r for r in fills[i + 1 :] if r["token_id"] == complement_token), None)
            first_qty = dec(first["fill_size"])
            comp_qty = dec(comp["fill_size"]) if comp else None
            completed = min(first_qty, comp_qty) if comp_qty is not None else None
            cost = (
                complete_set_cost(dec(first["fill_price"]), dec(comp["fill_price"]))
                if comp is not None
                else None
            )
            edge = edge_per_set(cost) if cost is not None else None
            dt = int(comp["fill_ts"]) - int(first["fill_ts"]) if comp is not None else None
            out.append(
                {
                    "wallet": first["wallet"],
                    "event_id": first["event_id"],
                    "condition_id": first["condition_id"],
                    "question": first["question"],
                    "first_leg_token_id": first["token_id"],
                    "first_leg_label": first["outcome_label"],
                    "first_leg_ts": first["fill_ts"],
                    "first_leg_price": first["fill_price"],
                    "first_leg_qty": first["fill_size"],
                    "complement_token_id": complement_token,
                    "complement_label": None if comp is None else comp["outcome_label"],
                    "complement_fill_ts": None if comp is None else comp["fill_ts"],
                    "complement_fill_price": None if comp is None else comp["fill_price"],
                    "complement_fill_qty": None if comp is None else comp["fill_size"],
                    "time_to_complement_s": dt,
                    "completed_pair_qty": dstr(completed),
                    "complete_set_cost": dstr(cost),
                    "edge_per_set": dstr(edge),
                    "edge_bps": dstr(edge * Decimal(10000) if edge is not None else None),
                    "completion_confidence": "observed_later_complement_fill" if comp else "not_completed",
                    "time_bucket": time_bucket(dt),
                    "cost_bucket": cost_bucket(cost),
                }
            )
    return out


def _last_complement_fill_ts(rows: list[dict], wallet: str, condition_id: str, merge_ts: int) -> int | None:
    prior = [
        r for r in rows
        if r["wallet"] == wallet and r["condition_id"] == condition_id
        and int(r["fill_ts"]) <= merge_ts and dec(r["bond_delta"]) > _ZERO
    ]
    if not prior:
        return None
    return max(int(r["fill_ts"]) for r in prior)


def build_merge_timing_report(
    session: Session,
    rows: list[dict],
    *,
    pairs: dict[str, TokenPair],
    wallets: list[str],
    progress_callback: Callable[[str, int], None] | None = None,
) -> list[dict]:
    row_by_fill_id = {int(r["fill_event_id"]): r for r in rows}
    relevant_conditions = {r["condition_id"] for r in rows if r["condition_id"]}
    if not relevant_conditions:
        return []
    stmt = text(
        "SELECT we.*, m.event_id, m.question FROM wallet_events we "
        "LEFT JOIN markets m ON m.condition_id = we.condition_id "
        "WHERE we.wallet IN :wallets AND we.event_type = 'MERGE' "
        "AND we.condition_id IN :conditions ORDER BY we.wallet, we.ts, we.id"
    ).bindparams(bindparam("wallets", expanding=True), bindparam("conditions", expanding=True))
    merges = session.execute(
        stmt, {"wallets": wallets, "conditions": sorted(relevant_conditions)}
    ).fetchall()
    if not merges:
        return []
    scope_tokens: set[str] = set()
    for condition_id in relevant_conditions:
        pair = pairs.get(condition_id)
        if pair is not None:
            scope_tokens.add(pair.yes_token_id)
            scope_tokens.add(pair.no_token_id)
    max_merge_ts = max(int(m.ts) for m in merges)

    qty: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    cost: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    merge_ids = {int(m.id) for m in merges}
    merge_states: dict[int, tuple[dict, dict]] = {}
    for n, ev in enumerate(
        _ledger_events(
            session,
            wallets,
            token_ids=scope_tokens,
            condition_ids=set(relevant_conditions),
            max_ts=max_merge_ts,
        ),
        start=1,
    ):
        if progress_callback and (n == 1 or n % 100000 == 0):
            progress_callback("replay_merges", n)
        wallet = ev.wallet.lower()
        pair = pairs.get(ev.condition_id or "")
        q = qty.setdefault(wallet, {})
        c = cost.setdefault(wallet, {})
        if ev.event_type == "TRADE" and ev.token_id:
            _apply_trade(q, c, ev.token_id, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif pair is not None and ev.event_type in {"SPLIT", "REDEEM"}:
            _apply_condition_event(q, c, pair, ev.event_type, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif pair is not None and ev.event_type == "MERGE":
            before = _condition_state(q, c, pair)
            _apply_condition_event(q, c, pair, ev.event_type, dec(ev.delta_shares), dec(ev.delta_usdc))
            after = _condition_state(q, c, pair)
            if int(ev.id) in merge_ids:
                merge_states[int(ev.id)] = (before, after)

    out: list[dict] = []
    by_event_batch: dict[tuple[str, str | None], list[tuple[int, int]]] = {}
    for idx, merge in enumerate(merges):
        by_event_batch.setdefault((merge.wallet.lower(), merge.event_id), []).append((idx, int(merge.ts)))
        before, after = merge_states.get(
            int(merge.id),
            (
                {"paired_qty": _ZERO, "unpaired_yes": _ZERO, "unpaired_no": _ZERO},
                {"paired_qty": _ZERO},
            ),
        )
        last_fill = max(
            (
                int(r["fill_ts"])
                for r in rows
                if r["wallet"] == merge.wallet.lower()
                and r["condition_id"] == merge.condition_id
                and int(r["fill_ts"]) <= int(merge.ts)
            ),
            default=None,
        )
        last_comp = _last_complement_fill_ts(rows, merge.wallet.lower(), merge.condition_id, int(merge.ts))
        out.append(
            {
                "wallet": merge.wallet.lower(),
                "event_id": merge.event_id,
                "condition_id": merge.condition_id,
                "question": merge.question,
                "merge_ts": int(merge.ts),
                "merge_utc": utc(int(merge.ts)),
                "merge_qty": dstr(abs(dec(merge.delta_shares))),
                "merge_usdc_released": dstr(abs(dec(merge.delta_usdc))),
                "time_from_start_s": None,
                "time_from_last_fill_s": None if last_fill is None else int(merge.ts) - last_fill,
                "time_from_last_complement_fill_s": None if last_comp is None else int(merge.ts) - last_comp,
                "paired_qty_before_merge": dstr(before["paired_qty"]),
                "unpaired_yes_before_merge": dstr(before["unpaired_yes"]),
                "unpaired_no_before_merge": dstr(before["unpaired_no"]),
                "paired_qty_after_merge": dstr(after["paired_qty"]),
                "capital_released": dstr(abs(dec(merge.delta_usdc))),
                "merge_batch_id": None,
            }
        )
    batch_num = 0
    for (_wallet, _event_id), items in by_event_batch.items():
        items.sort(key=lambda x: x[1])
        current: list[int] = []
        last_ts: int | None = None
        for idx, ts in items:
            if last_ts is None or ts - last_ts <= 5:
                current.append(idx)
            else:
                if len(current) > 1:
                    batch_num += 1
                    for current_idx in current:
                        out[current_idx]["merge_batch_id"] = f"merge_batch_{batch_num}"
                current = [idx]
            last_ts = ts
        if len(current) > 1:
            batch_num += 1
            for current_idx in current:
                out[current_idx]["merge_batch_id"] = f"merge_batch_{batch_num}"
    return out


def build_sibling_sequence_report(rows: list[dict]) -> list[dict]:
    out: list[dict] = []
    by_wallet_event: dict[tuple[str, str | None], list[dict]] = {}
    for row in rows:
        by_wallet_event.setdefault((row["wallet"], row["event_id"]), []).append(row)
    for fills in by_wallet_event.values():
        fills.sort(key=lambda r: int(r["fill_ts"]))
        ts_list = [int(r["fill_ts"]) for r in fills]
        for idx, anchor in enumerate(fills):
            lo = bisect_left(ts_list, int(anchor["fill_ts"]) - 3600)
            hi = bisect_left(ts_list, int(anchor["fill_ts"]) + 3600 + 1)
            for sib in fills[lo:hi]:
                if sib["fill_event_id"] == anchor["fill_event_id"]:
                    continue
                delta = int(sib["fill_ts"]) - int(anchor["fill_ts"])
                if anchor["condition_id"] == sib["condition_id"]:
                    sequence = (
                        "same_condition_complement"
                        if anchor["token_id"] != sib["token_id"]
                        else "unknown"
                    )
                elif anchor["market_family"] and anchor["market_family"] == sib["market_family"]:
                    sequence = "same_market_family"
                elif anchor["market_family"] and sib["market_family"]:
                    sequence = "cross_market_family"
                else:
                    sequence = "event_basket_sequence"
                out.append(
                    {
                        "wallet": anchor["wallet"],
                        "event_id": anchor["event_id"],
                        "anchor_condition_id": anchor["condition_id"],
                        "anchor_question": anchor["question"],
                        "anchor_market_family": anchor["market_family"],
                        "anchor_ts": anchor["fill_ts"],
                        "sibling_condition_id": sib["condition_id"],
                        "sibling_question": sib["question"],
                        "sibling_market_family": sib["market_family"],
                        "sibling_ts": sib["fill_ts"],
                        "delta_s": delta,
                        "same_side": int(anchor["side"] == sib["side"]),
                        "same_outcome_label": int(anchor["outcome_label"] == sib["outcome_label"]),
                        "price_anchor": anchor["fill_price"],
                        "price_sibling": sib["fill_price"],
                        "sequence_type": sequence,
                    }
                )
    return out


def build_unpaired_duration_report(
    session: Session,
    rows: list[dict],
    *,
    pairs: dict[str, TokenPair],
    wallets: list[str],
    progress_callback: Callable[[str, int], None] | None = None,
) -> list[dict]:
    condition_meta = {
        r.condition_id: (r.event_id, r.question)
        for r in session.execute(text("SELECT condition_id, event_id, question FROM markets")).fetchall()
    }
    relevant = {r["condition_id"] for r in rows if r["condition_id"] in pairs}
    qty: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    cost: dict[str, dict[str, Decimal]] = {w: {} for w in wallets}
    periods: dict[tuple[str, str], dict | None] = {}
    out: list[dict] = []
    scope_tokens: set[str] = set()
    for condition_id in relevant:
        pair = pairs.get(condition_id)
        if pair is not None:
            scope_tokens.add(pair.yes_token_id)
            scope_tokens.add(pair.no_token_id)

    def maybe_transition(wallet: str, condition_id: str, ts: int, event_type: str) -> None:
        pair = pairs[condition_id]
        state = _condition_state(qty[wallet], cost[wallet], pair)
        key = (wallet, condition_id)
        imbalance = state["unpaired_yes"] + state["unpaired_no"]
        current = periods.get(key)
        if current is None and imbalance > _DUST:
            periods[key] = {
                "wallet": wallet,
                "event_id": condition_meta.get(condition_id, (None, None))[0],
                "condition_id": condition_id,
                "question": condition_meta.get(condition_id, (None, None))[1],
                "unpaired_start_ts": ts,
                "unpaired_end_ts": None,
                "duration_s": None,
                "dominant_side": "YES" if state["unpaired_yes"] >= state["unpaired_no"] else "NO",
                "max_unpaired_qty": imbalance,
                "max_unpaired_cost": state["cost_yes"] if state["unpaired_yes"] >= state["unpaired_no"] else state["cost_no"],
                "resolved_by": "still_open",
                "final_pnl_if_available": None,
            }
        elif current is not None:
            if imbalance > current["max_unpaired_qty"]:
                current["max_unpaired_qty"] = imbalance
                current["dominant_side"] = "YES" if state["unpaired_yes"] >= state["unpaired_no"] else "NO"
                current["max_unpaired_cost"] = (
                    state["cost_yes"] if current["dominant_side"] == "YES" else state["cost_no"]
                )
            if imbalance <= _DUST:
                current["unpaired_end_ts"] = ts
                current["duration_s"] = ts - int(current["unpaired_start_ts"])
                current["resolved_by"] = _resolved_by(event_type)
                out.append(_stringify_period(current))
                periods[key] = None

    for n, ev in enumerate(
        _ledger_events(
            session,
            wallets,
            token_ids=scope_tokens,
            condition_ids=set(relevant),
        ),
        start=1,
    ):
        if progress_callback and (n == 1 or n % 100000 == 0):
            progress_callback("replay_unpaired", n)
        wallet = ev.wallet.lower()
        if ev.condition_id not in relevant:
            continue
        pair = pairs[ev.condition_id]
        q = qty.setdefault(wallet, {})
        c = cost.setdefault(wallet, {})
        if ev.event_type == "TRADE" and ev.token_id:
            _apply_trade(q, c, ev.token_id, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif ev.event_type in {"SPLIT", "MERGE", "REDEEM"}:
            _apply_condition_event(q, c, pair, ev.event_type, dec(ev.delta_shares), dec(ev.delta_usdc))
        elif ev.event_type == "RESOLUTION_SETTLEMENT" and ev.token_id:
            q[ev.token_id] = _ZERO
            c[ev.token_id] = _ZERO
        maybe_transition(wallet, ev.condition_id, int(ev.ts), ev.event_type)

    for current in periods.values():
        if current is not None:
            out.append(_stringify_period(current))
    return out


def _resolved_by(event_type: str) -> str:
    if event_type == "TRADE":
        return "complement_fill"
    if event_type == "MERGE":
        return "merge"
    if event_type == "REDEEM":
        return "redeem"
    if event_type == "RESOLUTION_SETTLEMENT":
        return "resolution"
    return "unknown"


def _stringify_period(period: dict) -> dict:
    return {
        **period,
        "max_unpaired_qty": dstr(period["max_unpaired_qty"]),
        "max_unpaired_cost": dstr(period["max_unpaired_cost"]),
    }


def _write_csv(path: Path, rows: list[dict], columns: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if columns is None:
        columns = list(rows[0].keys()) if rows else []
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def build_patterns_dataset(
    session: Session,
    *,
    wallet: str,
    out_dir: Path,
    event_id: str | None = None,
    watchlist: str | None = None,
    min_context: str = "usable",
    lookback_s: int = 7200,
    book_match_tolerance_bps: int = 5,
    include_gap_wallet: bool = False,
    progress_callback: Callable[[str, int], None] | None = None,
) -> PatternBuildStats:
    wallets = _wallet_scope(session, wallet, include_gap_wallet=include_gap_wallet)
    pairs = _token_pairs(session)
    labels = _wallet_labels(session)
    if progress_callback:
        progress_callback("load_fills", 0)
    fills = _fill_rows(
        session,
        wallets=wallets,
        watchlist=watchlist,
        event_id=event_id,
        min_context=min_context,
    )
    if progress_callback:
        progress_callback("loaded_fills", len(fills))
    rows, timeline, _snapshots = _build_dataset_rows(
        session,
        fills,
        pairs=pairs,
        lookback_s=lookback_s,
        tolerance_bps=book_match_tolerance_bps,
        labels=labels,
        progress_callback=progress_callback,
    )
    if progress_callback:
        progress_callback("pair_completion", len(rows))
    pair_report = build_pair_completion_report(rows)
    if progress_callback:
        progress_callback("merge_timing", len(pair_report))
    merge_report = build_merge_timing_report(
        session, rows, pairs=pairs, wallets=wallets, progress_callback=progress_callback
    )
    if progress_callback:
        progress_callback("sibling_sequences", len(merge_report))
    sibling_report = build_sibling_sequence_report(rows)
    if progress_callback:
        progress_callback("unpaired_duration", len(sibling_report))
    unpaired_report = build_unpaired_duration_report(
        session, rows, pairs=pairs, wallets=wallets, progress_callback=progress_callback
    )

    if progress_callback:
        progress_callback("writing_csv", len(rows))
    _write_csv(out_dir / "order_timing_dataset.csv", rows, ORDER_TIMING_COLUMNS)
    _write_csv(out_dir / "condition_inventory_timeline.csv", timeline, TIMELINE_COLUMNS)
    _write_csv(out_dir / "pair_completion_report.csv", pair_report, PAIR_COMPLETION_COLUMNS)
    _write_csv(out_dir / "merge_timing_report.csv", merge_report, MERGE_TIMING_COLUMNS)
    _write_csv(out_dir / "sibling_market_sequence_report.csv", sibling_report, SIBLING_SEQUENCE_COLUMNS)
    _write_csv(out_dir / "unpaired_inventory_duration_report.csv", unpaired_report, UNPAIRED_DURATION_COLUMNS)
    write_pattern_summary(out_dir, rows, pair_report, merge_report, sibling_report)

    return PatternBuildStats(
        wallet=wallet.lower(),
        out_dir=out_dir,
        fills=len(rows),
        pair_completions=len(pair_report),
        merges=len(merge_report),
        sibling_sequences=len(sibling_report),
        unpaired_periods=len(unpaired_report),
    )


def write_pattern_summary(
    out_dir: Path,
    rows: list[dict] | None = None,
    pair_report: list[dict] | None = None,
    merge_report: list[dict] | None = None,
    sibling_report: list[dict] | None = None,
) -> Path:
    rows = _read_csv(out_dir / "order_timing_dataset.csv") if rows is None else rows
    pair_report = _read_csv(out_dir / "pair_completion_report.csv") if pair_report is None else pair_report
    merge_report = _read_csv(out_dir / "merge_timing_report.csv") if merge_report is None else merge_report
    sibling_report = (
        _read_csv(out_dir / "sibling_market_sequence_report.csv") if sibling_report is None else sibling_report
    )
    buy_rows = [r for r in rows if r.get("side") == "BUY"]
    paired_inc = [r for r in buy_rows if dec(r.get("bond_delta")) > _ZERO]
    unpaired_reduced = [r for r in buy_rows if dec(r.get("unpaired_delta")) < _ZERO]
    unilateral_inc = [r for r in buy_rows if dec(r.get("unpaired_delta")) > _ZERO]
    completed = [r for r in pair_report if r.get("completion_confidence") != "not_completed"]
    comp_times = [int(r["time_to_complement_s"]) for r in completed if r.get("time_to_complement_s")]
    costs = [dec(r.get("complete_set_cost")) for r in completed if r.get("complete_set_cost")]
    waits = [
        int(r.get("fill_after_created_s") or r.get("fill_after_first_seen_s"))
        for r in rows
        if (r.get("fill_after_created_s") or r.get("fill_after_first_seen_s"))
    ]
    timing_counts: dict[str, int] = {}
    for row in rows:
        c = row.get("order_time_confidence") or "unknown"
        timing_counts[c] = timing_counts.get(c, 0) + 1
    sequence_counts: dict[str, int] = {}
    for row in sibling_report:
        key = f"{row.get('anchor_market_family') or 'unknown'} -> {row.get('sibling_market_family') or 'unknown'}"
        sequence_counts[key] = sequence_counts.get(key, 0) + 1
    top_sequences = sorted(sequence_counts.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
    merge_capital = [dec(r.get("capital_released")) for r in merge_report if r.get("capital_released")]
    immediate_merges = [
        r for r in merge_report
        if r.get("time_from_last_complement_fill_s") not in (None, "")
        and int(r["time_from_last_complement_fill_s"]) <= 60
    ]
    strong_patterns = []
    if buy_rows and len(paired_inc) / len(buy_rows) >= 0.5:
        strong_patterns.append("BUY fills frequently increase paired/bond inventory.")
    if completed and _median(comp_times) is not None and _median(comp_times) <= 300:
        strong_patterns.append("Complement fills often arrive within minutes.")
    if costs and _median_decimal(costs) is not None and _median_decimal(costs) < Decimal("0.98"):
        strong_patterns.append("Observed complete-set costs often sit below 0.98.")
    if not strong_patterns:
        strong_patterns.append("No pattern clears a simple strength threshold yet; keep this as evidence.")

    lines = [
        "# Phase 22.5 - Order Timing + Pattern Mining Summary",
        "",
        f"Rows analyzed: {len(rows)} fills. BUY fills: {len(buy_rows)}.",
        "",
        "## Required Answers",
        "",
        f"1. BUY fills increasing paired/bond inventory: {_pct(len(paired_inc), len(buy_rows))}.",
        f"2. BUY fills reducing unpaired inventory: {_pct(len(unpaired_reduced), len(buy_rows))}.",
        f"3. BUY fills increasing unilateral inventory: {_pct(len(unilateral_inc), len(buy_rows))}.",
        f"4. Time to complement: median={_fmt_seconds(_median(comp_times))}, p90={_fmt_seconds(_p90(comp_times))}.",
        f"5. Complete-set cost: average={_fmt_dec(_avg_decimal(costs))}, median={_fmt_dec(_median_decimal(costs))}.",
        "6. Complete-set buckets: "
        f"<0.95={_pct(sum(1 for c in costs if c < Decimal('0.95')), len(costs))}, "
        f"<0.98={_pct(sum(1 for c in costs if c < Decimal('0.98')), len(costs))}, "
        f"<1.00={_pct(sum(1 for c in costs if c < Decimal('1.00')), len(costs))}.",
        f"7. Maker/order wait before fill: median={_fmt_seconds(_median(waits))}, p90={_fmt_seconds(_p90(waits))}.",
        "8. Order timing confidence: "
        + ", ".join(f"{k}={_pct(v, len(rows))}" for k, v in sorted(timing_counts.items())),
        "9. Market families bought together: "
        + ("; ".join(f"{k} ({v})" for k, v in top_sequences) if top_sequences else "none observed."),
        f"10. MERGE timing: {len(immediate_merges)}/{len(merge_report)} within 60s of last complement fill; "
        "phase inference is timestamp-only unless event pause data exists.",
        f"11. Capital released by MERGE: total={_fmt_dec(sum(merge_capital, _ZERO))}, "
        f"median={_fmt_dec(_median_decimal(merge_capital))}.",
        f"12. Operating level: {_operating_level(rows, sibling_report)}.",
        "13. Patterns strong enough to simulate later: " + " ".join(strong_patterns),
        "14. Uncertain patterns: exact order creation/cancellation is unavailable unless an order table exists; "
        "book-derived timing only means a compatible order was visible, not that RN1/Gap owned it. "
        "Event phase is inferred from start/end timestamps and does not know true halftime/pause state.",
        "",
        "## Leakage Notes",
        "",
        "Inventory-before, book-before, event metadata, role/order hash, and estimated pre-fill timing are decision context. "
        "Inventory-after, complement timing, MERGE, REDEEM/resolution, and final PnL diagnostics are post-fill or final outcome evidence.",
    ]
    path = out_dir / "pattern_mining_summary.md"
    out_dir.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def export_patterns_dataset(session: Session, *, wallet: str, out: Path) -> int:
    default_dir = out.parent / f"{wallet.lower()}_patterns"
    src = default_dir / "order_timing_dataset.csv"
    if not src.exists():
        build_patterns_dataset(session, wallet=wallet, out_dir=default_dir)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, out)
    return len(_read_csv(out))


def _pct(n: int, d: int) -> str:
    if d == 0:
        return "n/a"
    return f"{(n / d) * 100:.1f}% ({n}/{d})"


def _median(values: list[int]) -> int | None:
    if not values:
        return None
    return int(statistics.median(values))


def _p90(values: list[int]) -> int | None:
    if not values:
        return None
    values = sorted(values)
    idx = min(len(values) - 1, int(round((len(values) - 1) * 0.9)))
    return values[idx]


def _avg_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return sum(values, _ZERO) / Decimal(len(values))


def _median_decimal(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return Decimal(str(statistics.median(values)))


def _fmt_seconds(value: int | None) -> str:
    return "n/a" if value is None else f"{value}s"


def _fmt_dec(value: Decimal | None) -> str:
    return "n/a" if value is None else f"{value:.6f}"


def _operating_level(rows: list[dict], sibling_report: list[dict]) -> str:
    if not rows:
        return "unknown; no fills in dataset"
    paired_share = sum(1 for r in rows if dec(r.get("bond_delta")) > _ZERO) / len(rows)
    event_sequence_share = (
        sum(1 for r in sibling_report if r.get("sequence_type") in {"same_market_family", "cross_market_family", "event_basket_sequence"})
        / len(sibling_report)
        if sibling_report
        else 0
    )
    if event_sequence_share >= 0.25:
        return "event-level or basket-aware; many fills have sibling event activity nearby"
    if paired_share >= 0.35:
        return "condition-level; many fills alter complete-set inventory"
    return "fill-level or weakly condition-aware; little sibling/paired evidence in this slice"
