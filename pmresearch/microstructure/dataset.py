"""Phase 20 — Microstructure + Lifecycle Dataset builder.

Joins each eligible fill-context row (Phase 18 all-fill context by default, or
legacy maker/enriched context when requested) with:
  - book-before-derived microstructure features (depth/imbalance/distance),
  - per-fill inventory/exposure before & after (a single ledger replay pass,
    reusing `exposure/engine.py` + `exposure/negrisk.py`, the same primitives
    `projections/exposures.py` uses for its daily snapshots — here evaluated
    at the exact fill timestamp instead of UTC day boundaries),
  - the lifecycle outcome of the episode (`projections/episodes.py`) that
    fill's event_id belongs to.

Every feature that can't be computed gets an entry in that row's
`null_reasons_json` (feature_name -> reason) rather than a dedicated SQL
column per feature — see the Phase 20 plan doc's discussion. Realized PnL
fields are attributed at the *episode* level (episodes don't split PnL across
individual fills), a documented simplification, not a per-fill decomposition.
`fill_size` is retained as the legacy notional-USDC copy-through from Phase 18;
new consumers should prefer `fill_shares = abs(delta_shares)` and
`fill_notional_usdc = abs(delta_usdc)` for unambiguous sizing.

Read-only over the ledger: never mutates wallet_events, holdings or episodes
(ADR 0006). It only reads fill context/episodes/book_snapshots/markets and
writes to its own `microstructure_lifecycle_dataset` table.
"""

from __future__ import annotations

import csv
import json
import time
from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Optional

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ..db.retry import retry_locked
from ..exposure import negrisk
from ..exposure.descriptors import STRUCTURE_NEG_RISK_EVENT_MEMBER
from ..exposure.engine import market_exposure
from ..ledger.replay import stream_events
from ..projections.episodes import fetch_episodes
from ..projections.exposures import _load_market_meta

DATASET_VERSION = 1
_ZERO = Decimal("0")
_DUST = Decimal("0.000001")
_DAY_SECONDS = 86400

# excellent > good > usable > weak > stale > missing (matches maker_fills.py)
_CONTEXT_ORDER = {
    "excellent": 5,
    "good": 4,
    "usable": 3,
    "weak": 2,
    "stale": 1,
    "missing": 0,
}

_MARKOUT_HORIZONS = {
    "markout_5m": 5 * 60,
    "markout_15m": 15 * 60,
    "markout_1h": 60 * 60,
    "markout_24h": 24 * 60 * 60,
}

_QUANTITY_EVENT_TYPES = ("TRADE", "SPLIT", "MERGE", "REDEEM", "RESOLUTION_SETTLEMENT")


@dataclass(frozen=True)
class MicrostructureDatasetStats:
    wallet: str
    watchlist: str
    context_source: str
    fills_seen: int
    rows_written: int
    by_close_path: dict[str, int]
    by_context_status: dict[str, int]


def _decimal(value) -> Decimal:
    if value is None:
        return _ZERO
    return Decimal(str(value))


def _opt_decimal(value) -> Optional[Decimal]:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def _str_or_none(value: Optional[Decimal]) -> Optional[str]:
    return None if value is None else str(value)


def _trade_utc(ts: int) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _abs_text_sql(column: str) -> str:
    return f"CASE WHEN {column} LIKE '-%' THEN substr({column}, 2) ELSE {column} END"


def _fill_rows(
    session: Session,
    *,
    wallet: str,
    watchlist: str,
    min_context: str,
    context_source: str,
) -> list:
    threshold = _CONTEXT_ORDER[min_context]
    statuses = [s for s, order in _CONTEXT_ORDER.items() if order >= threshold]
    if context_source == "all_fills":
        table = "all_fill_context"
        alias = "afc"
        role_expr = "COALESCE(afc.role, 'UNKNOWN')"
    elif context_source == "maker_only":
        table = "maker_fill_context"
        alias = "mfc"
        role_expr = "mfc.role"
    else:
        raise ValueError("context_source must be 'all_fills' or 'maker_only'")

    stmt = text(
        f"SELECT {alias}.event_id, {alias}.wallet, {alias}.token_id, {alias}.condition_id, "
        f"{alias}.trade_ts, {alias}.trade_utc, {alias}.side, {alias}.fill_price, "
        f"{alias}.fill_size, "
        f"COALESCE({alias}.fill_shares, {_abs_text_sql('we.delta_shares')}) AS fill_shares, "
        f"COALESCE({alias}.fill_notional_usdc, {_abs_text_sql('we.delta_usdc')}) AS fill_notional_usdc, "
        f"{alias}.delta_usdc, {role_expr} AS role, "
        f"{alias}.context_status, {alias}.book_before_age_s, {alias}.book_after_age_s, "
        f"{alias}.best_bid_before, {alias}.best_ask_before, {alias}.mid_before, {alias}.spread_before, "
        f"{alias}.depth_top_before_json, :context_source AS context_source "
        f"FROM {table} {alias} "
        f"JOIN wallet_events we ON we.id = {alias}.event_id "
        f"JOIN watchlist_tokens wt ON wt.token_id = {alias}.token_id "
        "JOIN watchlists wl ON wl.id = wt.watchlist_id AND wl.name = :watchlist "
        f"WHERE {alias}.wallet = :wallet AND wt.is_active = 1 "
        f"AND {alias}.context_status IN :statuses "
        f"ORDER BY {alias}.trade_ts, {alias}.event_id"
    ).bindparams(bindparam("statuses", expanding=True))
    return session.execute(
        stmt,
        {
            "wallet": wallet.lower(),
            "watchlist": watchlist,
            "statuses": statuses,
            "context_source": context_source,
        },
    ).fetchall()


def _book_features(fill, null_reasons: dict) -> dict:
    mid = _opt_decimal(fill.mid_before)
    bid = _opt_decimal(fill.best_bid_before)
    ask = _opt_decimal(fill.best_ask_before)
    spread = _opt_decimal(fill.spread_before)
    fill_price = _opt_decimal(fill.fill_price)

    spread_bps = None
    if spread is not None and mid is not None and mid != _ZERO:
        spread_bps = spread / mid * Decimal(10000)

    bid_top1 = ask_top1 = bid_top5 = ask_top5 = None
    imbalance_top1 = imbalance_top5 = None
    if fill.depth_top_before_json:
        try:
            depth = json.loads(fill.depth_top_before_json)
        except (TypeError, ValueError):
            depth = None
        if depth:
            bids = depth.get("bids") or []
            asks = depth.get("asks") or []
            bid_top1 = _decimal(bids[0].get("size")) if bids else _ZERO
            ask_top1 = _decimal(asks[0].get("size")) if asks else _ZERO
            bid_top5 = sum((_decimal(b.get("size")) for b in bids[:5]), _ZERO)
            ask_top5 = sum((_decimal(a.get("size")) for a in asks[:5]), _ZERO)
            if bid_top1 + ask_top1 != _ZERO:
                imbalance_top1 = (bid_top1 - ask_top1) / (bid_top1 + ask_top1)
            if bid_top5 + ask_top5 != _ZERO:
                imbalance_top5 = (bid_top5 - ask_top5) / (bid_top5 + ask_top5)
    if bid_top1 is None:
        for key in (
            "bid_depth_top1", "ask_depth_top1", "bid_depth_top5", "ask_depth_top5",
            "book_imbalance_top1", "book_imbalance_top5",
        ):
            null_reasons[key] = "no_book_depth"

    distance_to_mid = fill_price - mid if fill_price is not None and mid is not None else None
    distance_to_bid = fill_price - bid if fill_price is not None and bid is not None else None
    distance_to_ask = fill_price - ask if fill_price is not None and ask is not None else None

    fill_inside_spread = fill_at_best_bid = fill_at_best_ask = None
    if fill_price is not None and bid is not None and ask is not None:
        fill_inside_spread = int(bid < fill_price < ask)
        fill_at_best_bid = int(fill_price == bid)
        fill_at_best_ask = int(fill_price == ask)

    return {
        "spread_bps": _str_or_none(spread_bps),
        "bid_depth_top1": _str_or_none(bid_top1),
        "ask_depth_top1": _str_or_none(ask_top1),
        "bid_depth_top5": _str_or_none(bid_top5),
        "ask_depth_top5": _str_or_none(ask_top5),
        "book_imbalance_top1": _str_or_none(imbalance_top1),
        "book_imbalance_top5": _str_or_none(imbalance_top5),
        "distance_fill_to_mid": _str_or_none(distance_to_mid),
        "distance_fill_to_bid": _str_or_none(distance_to_bid),
        "distance_fill_to_ask": _str_or_none(distance_to_ask),
        "fill_inside_spread": fill_inside_spread,
        "fill_at_best_bid": fill_at_best_bid,
        "fill_at_best_ask": fill_at_best_ask,
    }


# SQLite's default SQLITE_MAX_VARIABLE_NUMBER is small enough that a highly
# active wallet's full event/episode history can blow past it in one IN (...)
# — every list-typed IN query below is chunked defensively.
_IN_CHUNK_SIZE = 500


def _chunked(values: list, size: int = _IN_CHUNK_SIZE):
    for i in range(0, len(values), size):
        yield values[i : i + size]


def _load_markets_extra(session: Session) -> tuple[dict[str, tuple[str | None, str | None]], dict[str, bool]]:
    """condition_id -> (category, start_date); event_id -> neg_risk flag."""
    rows = session.execute(
        text("SELECT condition_id, category, start_date, event_id FROM markets")
    ).fetchall()
    condition_extra = {r.condition_id: (r.category, r.start_date) for r in rows}
    event_ids = list({r.event_id for r in rows if r.event_id})
    neg_risk: dict[str, bool] = {}
    for chunk in _chunked(event_ids):
        ev_rows = session.execute(
            text("SELECT event_id, neg_risk FROM pm_events WHERE event_id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": chunk},
        ).fetchall()
        neg_risk.update({r.event_id: bool(r.neg_risk) for r in ev_rows})
    return condition_extra, neg_risk


def _closing_event_types(session: Session, event_ids: list[int]) -> dict[int, str]:
    out: dict[int, str] = {}
    for chunk in _chunked(event_ids):
        rows = session.execute(
            text("SELECT id, event_type FROM wallet_events WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": chunk},
        ).fetchall()
        out.update({r.id: r.event_type for r in rows})
    return out


def _classify_close_path(close_reason: str, last_event_type: Optional[str]) -> tuple[str, dict[str, int]]:
    flags = {
        "closed_by_merge": 0,
        "closed_by_redeem": 0,
        "closed_by_sell": 0,
        "closed_by_resolution": 0,
        "closed_by_unresolved_open": 0,
    }
    if close_reason == "open":
        flags["closed_by_unresolved_open"] = 1
        return "OPEN", flags
    if close_reason == "flat":
        if last_event_type == "TRADE":
            flags["closed_by_sell"] = 1
            return "SELL", flags
        if last_event_type == "MERGE":
            flags["closed_by_merge"] = 1
            return "MERGE", flags
        return "MIXED", flags
    if close_reason == "resolution":
        if last_event_type in ("REDEEM", "REDEEM_PAYOUT"):
            flags["closed_by_redeem"] = 1
            return "REDEEM", flags
        if last_event_type == "RESOLUTION_SETTLEMENT":
            flags["closed_by_resolution"] = 1
            return "RESOLUTION", flags
        return "UNKNOWN", flags
    return "UNKNOWN", flags


def _build_event_episode_map(
    session: Session, wallet: str, *, token_ids: set[str]
) -> tuple[dict[int, object], list[object]]:
    """Episode lookup scoped to the fills' own tokens only.

    A highly active wallet (e.g. RN1) can have tens of thousands of episodes
    across its entire trading history; every fill's episode is always keyed
    by that fill's own token_id, so restricting to `token_ids` keeps the
    downstream event_id -> event_type lookup's IN clause proportional to the
    watchlist scope instead of the wallet's whole history.
    """
    episodes = [ep for ep in fetch_episodes(session, wallet) if ep.token_id in token_ids]
    out: dict[int, object] = {}
    for ep in episodes:
        try:
            ids = json.loads(ep.events_consumed or "[]")
        except (TypeError, ValueError):
            ids = []
        for eid in ids:
            out[int(eid)] = ep
    return out, episodes


class _PositionReplay:
    """Single forward ledger replay: per-token qty checkpoints plus
    before/after position snapshots for a target set of fill event_ids."""

    def __init__(self, session: Session, wallet: str, target_event_ids: set[int]):
        self.condition_tokens, self.condition_meta, self.event_conditions = _load_market_meta(session)
        self.token_condition: dict[str, str] = {}
        for cond, tokens in self.condition_tokens.items():
            for token_id in tokens:
                self.token_condition[token_id] = cond
        self.positions: dict[str, Decimal] = {}
        self.checkpoints: dict[str, list[tuple[int, Decimal]]] = {}
        self.target_event_ids = target_event_ids
        # event_id -> {"before": {token_id: qty}, "after": {token_id: qty}}
        self.snapshots: dict[int, dict[str, dict[str, Decimal]]] = {}
        self._replay(session, wallet)

    def _qty(self, token_id: str) -> Decimal:
        return self.positions.get(token_id, _ZERO)

    def _checkpoint(self, token_id: str, ts: int) -> None:
        self.checkpoints.setdefault(token_id, []).append((ts, self._qty(token_id)))

    def _tokens_for_snapshot(self, event) -> list[str]:
        tokens: list[str] = []
        if event.token_id:
            tokens.append(event.token_id)
            complement = self._complement(event.token_id)
            if complement:
                tokens.append(complement)
        elif event.condition_id:
            tokens.extend(self.condition_tokens.get(event.condition_id, []))
        return tokens

    def _complement(self, token_id: str) -> Optional[str]:
        condition_id = self.token_condition.get(token_id)
        tokens = self.condition_tokens.get(condition_id or "", [])
        if len(tokens) != 2:
            return None
        return tokens[1] if tokens[0] == token_id else tokens[0]

    def _snapshot_qty(self, tokens: list[str]) -> dict[str, Decimal]:
        return {t: self._qty(t) for t in tokens}

    def _apply(self, event) -> None:
        etype = event.event_type
        condition_id = event.condition_id
        if etype == "TRADE":
            if event.token_id is None:
                return
            self.positions[event.token_id] = self._qty(event.token_id) + _decimal(event.delta_shares)
        elif etype == "SPLIT":
            tokens = self.condition_tokens.get(condition_id or "", [])
            size = _decimal(event.delta_shares)
            for token_id in tokens:
                self.positions[token_id] = self._qty(token_id) + size
        elif etype == "MERGE":
            tokens = self.condition_tokens.get(condition_id or "", [])
            size = -_decimal(event.delta_shares)
            for token_id in tokens:
                self.positions[token_id] = self._qty(token_id) - size
        elif etype == "REDEEM":
            tokens = self.condition_tokens.get(condition_id or "", [])
            for token_id in tokens:
                self.positions[token_id] = _ZERO
        elif etype == "RESOLUTION_SETTLEMENT":
            if event.token_id is not None:
                self.positions[event.token_id] = _ZERO

    def _replay(self, session: Session, wallet: str) -> None:
        for event in stream_events(session, wallet=wallet):
            is_target = event.event_type == "TRADE" and event.id in self.target_event_ids
            tokens = self._tokens_for_snapshot(event) if (
                is_target or event.event_type in _QUANTITY_EVENT_TYPES
            ) else []
            before = self._snapshot_qty(tokens) if is_target else None

            self._apply(event)

            if event.event_type in _QUANTITY_EVENT_TYPES:
                for token_id in tokens:
                    self._checkpoint(token_id, int(event.ts))
            if is_target:
                after = self._snapshot_qty(tokens)
                self.snapshots[event.id] = {"before": before, "after": after}

    def qty_at(self, token_id: str, ts: int) -> Optional[Decimal]:
        points = self.checkpoints.get(token_id)
        if not points:
            return None
        idx = bisect_right([p[0] for p in points], ts) - 1
        if idx < 0:
            return None
        return points[idx][1]


def _market_row(session: Session, condition_id: Optional[str]):
    if condition_id is None:
        return None
    return session.execute(
        text("SELECT structure_type, event_id FROM markets WHERE condition_id = :cid"),
        {"cid": condition_id},
    ).fetchone()


def build_microstructure_dataset(
    session: Session,
    *,
    wallet: str,
    watchlist: str = "world_cup_2026",
    min_context: str = "usable",
    context_source: str = "all_fills",
) -> MicrostructureDatasetStats:
    wallet = wallet.lower()
    fills = _fill_rows(
        session,
        wallet=wallet,
        watchlist=watchlist,
        min_context=min_context,
        context_source=context_source,
    )
    if not fills:
        def _delete_existing() -> None:
            session.execute(
                text("DELETE FROM microstructure_lifecycle_dataset WHERE wallet = :w"), {"w": wallet}
            )
            session.commit()

        retry_locked(session, _delete_existing)
        return MicrostructureDatasetStats(wallet, watchlist, context_source, 0, 0, {}, {})

    target_event_ids = {int(f.event_id) for f in fills}
    target_tokens = {f.token_id for f in fills}
    replay = _PositionReplay(session, wallet, target_event_ids)
    condition_extra, event_neg_risk = _load_markets_extra(session)
    event_episode, all_episodes = _build_event_episode_map(session, wallet, token_ids=target_tokens)

    last_event_ids: list[int] = []
    for ep in all_episodes:
        try:
            ids = json.loads(ep.events_consumed or "[]")
        except (TypeError, ValueError):
            ids = []
        if ids:
            last_event_ids.append(int(ids[-1]))
    closing_types = _closing_event_types(session, last_event_ids)

    wallet_label = session.execute(
        text("SELECT display_name FROM wallets WHERE address = :w"), {"w": wallet}
    ).scalar()

    now = int(time.time())
    rows: list[dict] = []
    by_close_path: dict[str, int] = {}
    by_context_status: dict[str, int] = {}

    for fill in fills:
        event_id = int(fill.event_id)
        null_reasons: dict[str, str] = {}
        by_context_status[fill.context_status] = by_context_status.get(fill.context_status, 0) + 1

        book = _book_features(fill, null_reasons)

        market_row = _market_row(session, fill.condition_id)
        structure_type = market_row.structure_type if market_row else None
        event_id_for_market = market_row.event_id if market_row else None

        category, start_date = condition_extra.get(fill.condition_id or "", (None, None))
        if category is None:
            null_reasons["market_category"] = "missing_market_metadata"
        time_to_event_start_s = None
        if start_date:
            try:
                start_ts = int(datetime.fromisoformat(start_date.replace("Z", "+00:00")).timestamp())
                time_to_event_start_s = start_ts - int(fill.trade_ts)
            except ValueError:
                null_reasons["time_to_event_start_s"] = "missing_market_metadata"
        else:
            null_reasons["time_to_event_start_s"] = "missing_market_metadata"

        snap = replay.snapshots.get(event_id, {"before": {}, "after": {}})
        complement_id = replay._complement(fill.token_id)
        qty_token_before = snap["before"].get(fill.token_id, _ZERO) if snap["before"] else _ZERO
        qty_token_after = snap["after"].get(fill.token_id, _ZERO) if snap["after"] else _ZERO
        qty_complement_before = snap["before"].get(complement_id, _ZERO) if complement_id and snap["before"] else _ZERO
        qty_complement_after = snap["after"].get(complement_id, _ZERO) if complement_id and snap["after"] else _ZERO

        directional_before = directional_after = bond_before = bond_after = None
        bond_ratio_before = bond_ratio_after = None
        if complement_id is not None:
            tokens_order = replay.condition_tokens.get(fill.condition_id or "", [])
            me_before = market_exposure(
                structure_type, tokens_order,
                {fill.token_id: qty_token_before, complement_id: qty_complement_before},
            )
            me_after = market_exposure(
                structure_type, tokens_order,
                {fill.token_id: qty_token_after, complement_id: qty_complement_after},
            )
            directional_before, bond_before = me_before.directional, me_before.bond
            directional_after, bond_after = me_after.directional, me_after.bond
            if bond_before is not None and (bond_before + abs(directional_before)) != _ZERO:
                bond_ratio_before = bond_before / (bond_before + abs(directional_before))
            if bond_after is not None and (bond_after + abs(directional_after)) != _ZERO:
                bond_ratio_after = bond_after / (bond_after + abs(directional_after))
        else:
            null_reasons["directional_before"] = "no_complement_token"
            null_reasons["bond_before"] = "no_complement_token"
            null_reasons["directional_after"] = "no_complement_token"
            null_reasons["bond_after"] = "no_complement_token"

        bond_delta = (
            bond_after - bond_before if bond_before is not None and bond_after is not None else None
        )
        directional_delta = (
            directional_after - directional_before
            if directional_before is not None and directional_after is not None
            else None
        )

        event_exposure_before = event_exposure_after = event_exposure_delta = None
        if (
            structure_type == STRUCTURE_NEG_RISK_EVENT_MEMBER
            and event_id_for_market
            and event_neg_risk.get(event_id_for_market)
        ):
            siblings_before = []
            siblings_after = []
            for sib_cond in replay.event_conditions.get(event_id_for_market, []):
                sib_tokens = replay.condition_tokens.get(sib_cond, [])
                if len(sib_tokens) != 2:
                    continue
                if sib_cond == fill.condition_id:
                    q0b, q1b = qty_token_before, qty_complement_before
                    q0a, q1a = qty_token_after, qty_complement_after
                    if sib_tokens[0] != fill.token_id:
                        q0b, q1b = q1b, q0b
                        q0a, q1a = q1a, q0a
                else:
                    q0b = replay._qty(sib_tokens[0])
                    q1b = replay._qty(sib_tokens[1])
                    q0a, q1a = q0b, q1b
                if abs(q0b) > _DUST or abs(q1b) > _DUST or abs(q0a) > _DUST or abs(q1a) > _DUST:
                    siblings_before.append((sib_cond, q0b, q1b))
                    siblings_after.append((sib_cond, q0a, q1a))
            if siblings_before:
                ev_before = negrisk.event_exposure(event_id_for_market, siblings_before)
                ev_after = negrisk.event_exposure(event_id_for_market, siblings_after)
                event_exposure_before = ev_before.net_after_exclusivity
                event_exposure_after = ev_after.net_after_exclusivity
                event_exposure_delta = event_exposure_after - event_exposure_before

        # -- lifecycle / close path --
        episode = event_episode.get(event_id)
        if episode is None:
            close_path = "UNKNOWN"
            close_flags = {
                "closed_by_merge": 0, "closed_by_redeem": 0, "closed_by_sell": 0,
                "closed_by_resolution": 0, "closed_by_unresolved_open": 0,
            }
            close_ts = hold_seconds = None
            realized_pnl_wac = realized_pnl_per_share = realized_pnl_bps_on_cost = None
            pnl_episode = pnl_at_resolution = None
            null_reasons["close_path"] = "episode_not_built"
        else:
            ids = json.loads(episode.events_consumed or "[]")
            last_id = int(ids[-1]) if ids else None
            last_type = closing_types.get(last_id) if last_id is not None else None
            close_path, close_flags = _classify_close_path(episode.close_reason, last_type)
            close_ts = episode.close_ts
            hold_seconds = (
                int(close_ts) - int(episode.open_ts) if close_ts is not None else None
            )
            if close_path == "OPEN":
                realized_pnl_wac = realized_pnl_per_share = realized_pnl_bps_on_cost = None
                pnl_episode = pnl_at_resolution = None
                null_reasons["realized_pnl_wac"] = "position_still_open"
                null_reasons["pnl_episode"] = "position_still_open"
                null_reasons["pnl_at_resolution"] = "position_still_open"
            else:
                realized_pnl_wac = _decimal(episode.realized_pnl)
                peak_qty = _decimal(episode.peak_qty)
                wac_entry = _decimal(episode.wac_entry)
                realized_pnl_per_share = (
                    realized_pnl_wac / peak_qty if peak_qty != _ZERO else None
                )
                realized_pnl_bps_on_cost = (
                    realized_pnl_wac / (wac_entry * peak_qty) * Decimal(10000)
                    if wac_entry != _ZERO and peak_qty != _ZERO
                    else None
                )
                pnl_episode = realized_pnl_wac
                if close_path == "RESOLUTION":
                    pnl_at_resolution = realized_pnl_wac
                else:
                    pnl_at_resolution = None
                    null_reasons["pnl_at_resolution"] = "no_resolution_yet"

        by_close_path[close_path] = by_close_path.get(close_path, 0) + 1

        target_24h = int(fill.trade_ts) + _DAY_SECONDS
        remaining_qty_24h = replay.qty_at(fill.token_id, target_24h)
        is_open_24h = None
        if remaining_qty_24h is None:
            null_reasons["remaining_open_qty_after_24h"] = "no_price_point"
        else:
            is_open_24h = int(abs(remaining_qty_24h) > _DUST)

        side_sign = Decimal(1) if fill.side == "BUY" else Decimal(-1)
        fill_price = _opt_decimal(fill.fill_price)
        markouts: dict[str, Optional[Decimal]] = {}
        for name, horizon in _MARKOUT_HORIZONS.items():
            mid_at_horizon = _mid_at(session, fill.token_id, int(fill.trade_ts) + horizon, max_lookahead=2 * horizon)
            if mid_at_horizon is None or fill_price is None:
                markouts[name] = None
                null_reasons[name] = "no_price_point"
            else:
                markouts[name] = (mid_at_horizon - fill_price) * side_sign

        rows.append(
            {
                "event_id": event_id,
                "wallet": wallet,
                "token_id": fill.token_id,
                "condition_id": fill.condition_id,
                "trade_ts": int(fill.trade_ts),
                "trade_utc": fill.trade_utc,
                "side": fill.side,
                "fill_price": fill.fill_price,
                "fill_size": fill.fill_size,
                "fill_shares": fill.fill_shares,
                "fill_notional_usdc": fill.fill_notional_usdc,
                "delta_usdc": fill.delta_usdc,
                "role": fill.role,
                "context_status": fill.context_status,
                "book_before_age_s": fill.book_before_age_s,
                "book_after_age_s": fill.book_after_age_s,
                "best_bid_before": fill.best_bid_before,
                "best_ask_before": fill.best_ask_before,
                "mid_before": fill.mid_before,
                "spread_before": fill.spread_before,
                **book,
                "trade_hour_utc": datetime.fromtimestamp(int(fill.trade_ts), tz=timezone.utc).hour,
                "market_category": category,
                "time_to_event_start_s": time_to_event_start_s,
                "wallet_label": wallet_label,
                "qty_token_before": str(qty_token_before),
                "qty_complement_before": str(qty_complement_before) if complement_id else None,
                "directional_before": _str_or_none(directional_before),
                "bond_before": _str_or_none(bond_before),
                "bond_ratio_before": _str_or_none(bond_ratio_before),
                "qty_token_after": str(qty_token_after),
                "qty_complement_after": str(qty_complement_after) if complement_id else None,
                "directional_after": _str_or_none(directional_after),
                "bond_after": _str_or_none(bond_after),
                "bond_ratio_after": _str_or_none(bond_ratio_after),
                "bond_delta": _str_or_none(bond_delta),
                "directional_delta": _str_or_none(directional_delta),
                "event_exposure_before": _str_or_none(event_exposure_before),
                "event_exposure_after": _str_or_none(event_exposure_after),
                "event_exposure_delta": _str_or_none(event_exposure_delta),
                "close_path": close_path,
                "close_ts": close_ts,
                "hold_seconds": hold_seconds,
                "realized_pnl_wac": _str_or_none(realized_pnl_wac),
                "realized_pnl_per_share": _str_or_none(realized_pnl_per_share),
                "realized_pnl_bps_on_cost": _str_or_none(realized_pnl_bps_on_cost),
                "remaining_open_qty_after_24h": _str_or_none(remaining_qty_24h),
                "is_open_after_24h": is_open_24h,
                **close_flags,
                "markout_5m": _str_or_none(markouts["markout_5m"]),
                "markout_15m": _str_or_none(markouts["markout_15m"]),
                "markout_1h": _str_or_none(markouts["markout_1h"]),
                "markout_24h": _str_or_none(markouts["markout_24h"]),
                "pnl_episode": _str_or_none(pnl_episode),
                "pnl_at_resolution": _str_or_none(pnl_at_resolution),
                "null_reasons_json": json.dumps(null_reasons, sort_keys=True, separators=(",", ":")),
                "dataset_version": DATASET_VERSION,
                "watchlist": watchlist,
                "context_source": fill.context_source,
                "built_at": now,
            }
        )

    columns = list(rows[0].keys())
    placeholders = ", ".join(f":{c}" for c in columns)

    def _write() -> None:
        session.execute(
            text("DELETE FROM microstructure_lifecycle_dataset WHERE wallet = :w"), {"w": wallet}
        )
        session.execute(
            text(
                f"INSERT INTO microstructure_lifecycle_dataset ({', '.join(columns)}) "
                f"VALUES ({placeholders})"
            ),
            rows,
        )
        session.commit()

    retry_locked(session, _write)

    return MicrostructureDatasetStats(
        wallet=wallet,
        watchlist=watchlist,
        context_source=context_source,
        fills_seen=len(fills),
        rows_written=len(rows),
        by_close_path=by_close_path,
        by_context_status=by_context_status,
    )


def _mid_at(
    session: Session, token_id: str, target_ts: int, *, max_lookahead: int
) -> Optional[Decimal]:
    row = session.execute(
        text(
            "SELECT mid FROM book_snapshots "
            "WHERE token_id = :token_id AND ts >= :target_ts AND ts <= :max_ts "
            "ORDER BY ts ASC LIMIT 1"
        ),
        {"token_id": token_id, "target_ts": target_ts, "max_ts": target_ts + max_lookahead},
    ).fetchone()
    return None if row is None or row.mid is None else Decimal(str(row.mid))


def dataset_stats(session: Session, wallet: str) -> dict:
    wallet = wallet.lower()
    total = session.execute(
        text("SELECT COUNT(*) FROM microstructure_lifecycle_dataset WHERE wallet = :w"),
        {"w": wallet},
    ).scalar_one()
    by_close_path = {
        r.close_path: r.n
        for r in session.execute(
            text(
                "SELECT close_path, COUNT(*) AS n FROM microstructure_lifecycle_dataset "
                "WHERE wallet = :w GROUP BY close_path"
            ),
            {"w": wallet},
        )
    }
    by_context_status = {
        r.context_status: r.n
        for r in session.execute(
            text(
                "SELECT context_status, COUNT(*) AS n FROM microstructure_lifecycle_dataset "
                "WHERE wallet = :w GROUP BY context_status"
            ),
            {"w": wallet},
        )
    }
    reason_counts: dict[str, int] = {}
    for row in session.execute(
        text("SELECT null_reasons_json FROM microstructure_lifecycle_dataset WHERE wallet = :w"),
        {"w": wallet},
    ):
        try:
            reasons = json.loads(row.null_reasons_json or "{}")
        except (TypeError, ValueError):
            reasons = {}
        for feature, reason in reasons.items():
            key = f"{feature}:{reason}"
            reason_counts[key] = reason_counts.get(key, 0) + 1
    return {
        "wallet": wallet,
        "total_rows": int(total or 0),
        "by_close_path": by_close_path,
        "by_context_status": by_context_status,
        "null_reason_counts": reason_counts,
    }


def export_dataset(session: Session, wallet: str, out_path: Path, *, fmt: str = "parquet") -> int:
    rows = session.execute(
        text("SELECT * FROM microstructure_lifecycle_dataset WHERE wallet = :w ORDER BY trade_ts"),
        {"w": wallet.lower()},
    ).mappings().fetchall()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        columns: list[str] = []
    else:
        columns = list(rows[0].keys())

    if fmt == "csv":
        with out_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for r in rows:
                writer.writerow([r[c] for c in columns])
    elif fmt == "parquet":
        import pyarrow as pa
        import pyarrow.parquet as pq

        table = pa.Table.from_pylist([dict(r) for r in rows])
        pq.write_table(table, out_path)
    else:
        raise ValueError(f"unsupported export format: {fmt!r}")
    return len(rows)
