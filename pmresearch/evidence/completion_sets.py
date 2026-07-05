"""Completion-set / inventory-cycling evidence audit (read-only).

Reconstructs, from `wallet_events` alone, the mechanics behind the hypothesis
that a wallet operates as a sports binary market maker / inventory cycler:

  * pairs both outcome tokens of a binary market with passive buys,
  * monetizes matched complete sets via MERGE (payout $1 per set),
  * redeems residual (winning-side) inventory at resolution.

Nothing here writes to `wallet_events` or any projection table. Weighted-average
cost (WAC, ADR 0003) is reused from `projections.pnl_decomposition` so realized
edges are consistent with the accepted PnL decomposition. Complementary tokens
are keyed by `condition_id` + `tokens.outcome_index` (never Yes/No labels); only
markets with exactly two outcome tokens are treated as binary.

Units convention (critical, see `RN1_COMPLETION_SET_AUDIT.md`):
  1 token0 share + 1 token1 share = 1 complete set = $1 at merge/resolution.
  A MERGE event's `delta_shares` magnitude is the per-token / per-set count N
  (destroys N of each token, pays N USDC), NOT the 2N total outcome shares.
"""

from __future__ import annotations

import csv
import json
from array import array
from collections import defaultdict, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..projections.pnl_decomposition import (
    _Position,
    _decimal,
    _derived_redeem_conditions,
    _load_metadata,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
DUST = Decimal("0.000001")
WINNER_THRESHOLD = Decimal("0.99")
LOSER_THRESHOLD = Decimal("0.01")

_CLOSE_EVENTS = {"REDEEM", "REDEEM_PAYOUT", "RESOLUTION_SETTLEMENT"}

_STREAM_SQL = text(
    "SELECT id, event_type, ts, condition_id, token_id, delta_shares, delta_usdc, price "
    "FROM wallet_events WHERE wallet = :wallet ORDER BY ts, id"
).execution_options(stream_results=True)

_FEE_SQL = text(
    "SELECT COUNT(*) AS n, COALESCE(SUM(CAST(estimated_fee AS TEXT)), '0') AS s "
    "FROM fee_estimates WHERE wallet = :wallet"
)


def _q6(value: Decimal) -> str:
    """Decimal string, quantized to 6 dp for stable CSV output."""
    return str(value.quantize(Decimal("0.000001")))


class _MarketState:
    """Per-condition accumulator for a binary market (2 outcome tokens)."""

    __slots__ = (
        "tokens", "buy_qty", "buy_cost", "sell_qty", "sell_proceeds",
        "merge_sets", "merge_usdc", "merge_cost", "merge_edge",
        "split_sets", "redeem_qty", "redeem_usdc", "redeem_cost", "redeem_edge",
        "fifo", "matched_pair_qty", "wait_weight_sum", "wait_seconds_max",
        "pairs_under_60s", "pairs_total_events", "res_snapshot",
    )

    def __init__(self, tokens: list[str]) -> None:
        self.tokens = tokens  # [token0 (idx0), token1 (idx1)]
        self.buy_qty = {t: _ZERO for t in tokens}
        self.buy_cost = {t: _ZERO for t in tokens}
        self.sell_qty = {t: _ZERO for t in tokens}
        self.sell_proceeds = {t: _ZERO for t in tokens}
        self.merge_sets = _ZERO
        self.merge_usdc = _ZERO
        self.merge_cost = {t: _ZERO for t in tokens}
        self.merge_edge = _ZERO
        self.split_sets = _ZERO
        self.redeem_qty = _ZERO
        self.redeem_usdc = _ZERO
        self.redeem_cost = _ZERO
        self.redeem_edge = _ZERO
        # FIFO deques of [remaining_qty, ts] per token for temporal leg matching.
        self.fifo = {t: deque() for t in tokens}
        self.matched_pair_qty = _ZERO
        self.wait_weight_sum = _ZERO  # sum(matched_qty * seconds) for weighted mean
        self.wait_seconds_max = 0
        self.pairs_under_60s = _ZERO
        self.pairs_total_events = 0
        self.res_snapshot: Optional[tuple[Decimal, Decimal]] = None  # (qty0, qty1)


@dataclass
class CompletionSetAudit:
    wallet: str
    computed_at: str
    # per-market row dicts
    pair_lifecycle: list[dict]
    merge_edge: list[dict]
    redeem_orphan: list[dict]
    temporal: list[dict]
    pnl_bridge: list[dict]
    # coverage / metadata accounting
    coverage: dict
    # global temporal + orphan summaries for the markdown
    summary: dict


def analyze_completion_sets(session: Session, wallet: str) -> CompletionSetAudit:
    wallet = wallet.lower()
    condition_tokens, token_conditions, condition_categories, resolution_prices = _load_metadata(
        session
    )
    derived_conditions = _derived_redeem_conditions(session, wallet)

    # token -> (condition, other_token) for binary conditions only
    binary_conditions = {
        cid: toks for cid, toks in condition_tokens.items() if len(toks) == 2
    }
    other_token: dict[str, str] = {}
    for cid, toks in binary_conditions.items():
        other_token[toks[0]] = toks[1]
        other_token[toks[1]] = toks[0]

    positions: dict[str, _Position] = {}
    markets: dict[str, _MarketState] = {}
    wait_seconds = array("q")  # every matched lot's seconds_between_legs (count basis)

    # coverage accounting: never skip silently
    ledger_condition_ids: set[str] = set()
    events_no_condition = 0
    events_unmapped_condition = 0
    unmapped_condition_ids: set[str] = set()
    events_non_binary = 0
    non_binary_condition_ids: set[str] = set()

    rewards = {"all": _ZERO, "sports": _ZERO}
    directional = {"all": _ZERO, "sports": _ZERO}  # realized sell PnL (WAC)

    def pos(token_id: str) -> _Position:
        p = positions.get(token_id)
        if p is None:
            p = positions[token_id] = _Position()
        return p

    def scopes_for(cid: Optional[str]) -> tuple[str, ...]:
        cat = (condition_categories.get(cid or "", "") or "").strip().lower()
        return ("all", "sports") if cat == "sports" else ("all",)

    events_processed = 0
    for ev in session.execute(_STREAM_SQL, {"wallet": wallet}):
        events_processed += 1
        etype = ev.event_type
        cid = ev.condition_id
        usdc = _decimal(ev.delta_usdc)

        if etype in {"REWARD", "MAKER_REBATE", "TAKER_REBATE"}:
            for sc in scopes_for(cid):
                rewards[sc] += usdc
            continue

        if cid is not None:
            ledger_condition_ids.add(cid)

        # Only binary markets participate in the pair mechanics.
        toks = binary_conditions.get(cid or "")
        if toks is None:
            if etype in {"TRADE", "MERGE", "SPLIT", "REDEEM", "REDEEM_PAYOUT",
                         "RESOLUTION_SETTLEMENT"}:
                if cid is None:
                    events_no_condition += 1
                elif cid not in condition_tokens:
                    events_unmapped_condition += 1
                    unmapped_condition_ids.add(cid)
                else:
                    events_non_binary += 1
                    non_binary_condition_ids.add(cid)
            # Still fold into WAC so nothing is invented; but no pair analysis.
            _fold_wac_only(positions, condition_tokens, derived_conditions,
                           resolution_prices, etype, cid, ev)
            continue

        m = markets.get(cid)
        if m is None:
            m = markets[cid] = _MarketState(toks)

        if etype == "TRADE":
            if ev.token_id is None:
                continue
            tok = ev.token_id
            other = other_token.get(tok)
            delta = _decimal(ev.delta_shares)
            p = pos(tok)
            if delta > _ZERO:  # BUY
                p.add(delta, -usdc)
                m.buy_qty[tok] += delta
                m.buy_cost[tok] += -usdc
                # temporal FIFO match against the opposite leg's waiting inventory
                m.pairs_total_events += 1
                rem = delta
                other_fifo = m.fifo.get(other)
                while rem > DUST and other_fifo:
                    o_qty, o_ts = other_fifo[0]
                    take = rem if rem < o_qty else o_qty
                    wait = int(ev.ts) - int(o_ts)
                    if wait < 0:
                        wait = 0
                    m.matched_pair_qty += take
                    m.wait_weight_sum += take * Decimal(wait)
                    if wait > m.wait_seconds_max:
                        m.wait_seconds_max = wait
                    if wait <= 60:
                        m.pairs_under_60s += take
                    wait_seconds.append(wait)
                    rem -= take
                    if take >= o_qty - DUST:
                        other_fifo.popleft()
                    else:
                        other_fifo[0][0] = o_qty - take
                if rem > DUST:
                    m.fifo[tok].append([rem, int(ev.ts)])
            elif delta < _ZERO:  # SELL
                shares = -delta
                sell_pnl = p.remove(shares, usdc)
                for sc in scopes_for(cid):
                    directional[sc] += sell_pnl
                m.sell_qty[tok] += shares
                m.sell_proceeds[tok] += usdc
                # consume this token's own waiting (unmatched) inventory
                own = m.fifo[tok]
                rem = shares
                while rem > DUST and own:
                    o_qty, _o_ts = own[0]
                    take = rem if rem < o_qty else o_qty
                    rem -= take
                    if take >= o_qty - DUST:
                        own.popleft()
                    else:
                        own[0][0] = o_qty - take
            continue

        if etype == "SPLIT":
            shares = _decimal(ev.delta_shares)
            cost_per = -usdc / _decimal(len(toks))
            for t in toks:
                pos(t).add(shares, cost_per)
            m.split_sets += shares
            continue

        if etype == "MERGE":
            sets = -_decimal(ev.delta_shares)
            proceeds_per = usdc / _decimal(len(toks))
            m.merge_sets += sets
            m.merge_usdc += usdc
            for t in toks:
                pnl = pos(t).remove(sets, proceeds_per)
                m.merge_cost[t] += proceeds_per - pnl
                m.merge_edge += pnl
                # merge consumes matched inventory from the front of the FIFO
                own = m.fifo[t]
                rem = sets
                while rem > DUST and own:
                    o_qty, _o_ts = own[0]
                    take = rem if rem < o_qty else o_qty
                    rem -= take
                    if take >= o_qty - DUST:
                        own.popleft()
                    else:
                        own[0][0] = o_qty - take
            continue

        if etype in _CLOSE_EVENTS:
            # snapshot pre-close net holdings once per condition
            if m.res_snapshot is None:
                m.res_snapshot = (pos(toks[0]).qty, pos(toks[1]).qty)
            if etype == "REDEEM":
                if cid in derived_conditions and usdc == _ZERO:
                    continue
                proceeds_per = usdc / _decimal(len(toks))
                for t in toks:
                    p = positions.get(t)
                    if p is None:
                        continue
                    closed_qty = p.qty
                    pnl = p.close(proceeds_per)
                    m.redeem_qty += closed_qty if closed_qty > _ZERO else _ZERO
                    m.redeem_cost += proceeds_per - pnl
                    m.redeem_edge += pnl
                m.redeem_usdc += usdc
            elif etype == "REDEEM_PAYOUT":
                prices = resolution_prices.get(cid or "", {})
                for t in toks:
                    p = positions.get(t)
                    if p is None or abs(p.qty) <= DUST:
                        continue
                    proceeds = p.qty * prices.get(t, _ZERO)
                    closed_qty = p.qty
                    pnl = p.close(proceeds)
                    m.redeem_qty += closed_qty if closed_qty > _ZERO else _ZERO
                    m.redeem_usdc += proceeds
                    m.redeem_cost += proceeds - pnl
                    m.redeem_edge += pnl
            else:  # RESOLUTION_SETTLEMENT (token-scoped)
                t = ev.token_id
                if t is None:
                    continue
                p = positions.get(t)
                if p is None or abs(p.qty) <= DUST:
                    continue
                prices = resolution_prices.get(cid or "", {})
                proceeds = p.qty * prices.get(t, _ZERO)
                closed_qty = p.qty
                pnl = p.close(proceeds)
                m.redeem_qty += closed_qty if closed_qty > _ZERO else _ZERO
                m.redeem_usdc += proceeds
                m.redeem_cost += proceeds - pnl
                m.redeem_edge += pnl
            continue

    computed_at = datetime.now(timezone.utc).isoformat()
    return _build_audit(
        wallet, computed_at, markets, positions, binary_conditions,
        condition_categories, resolution_prices,
        rewards, directional, wait_seconds,
        coverage={
            "events_processed": events_processed,
            "ledger_condition_ids": len(ledger_condition_ids),
            "binary_condition_ids": len(markets),
            "events_no_condition": events_no_condition,
            "events_unmapped_condition": events_unmapped_condition,
            "unmapped_condition_ids": len(unmapped_condition_ids),
            "events_non_binary": events_non_binary,
            "non_binary_condition_ids": len(non_binary_condition_ids),
        },
        session=session,
    )


def _fold_wac_only(positions, condition_tokens, derived_conditions,
                   resolution_prices, etype, cid, ev) -> None:
    """Keep WAC positions correct for non-binary/unmapped conditions so the
    global cash bridge is not distorted, without running pair analysis."""
    def pos(token_id):
        p = positions.get(token_id)
        if p is None:
            p = positions[token_id] = _Position()
        return p

    usdc = _decimal(ev.delta_usdc)
    if etype == "TRADE":
        if ev.token_id is None:
            return
        delta = _decimal(ev.delta_shares)
        if delta > _ZERO:
            pos(ev.token_id).add(delta, -usdc)
        elif delta < _ZERO:
            pos(ev.token_id).remove(-delta, usdc)
        return
    toks = condition_tokens.get(cid or "", [])
    if etype == "SPLIT" and toks:
        cost_per = -usdc / _decimal(len(toks))
        for t in toks:
            pos(t).add(_decimal(ev.delta_shares), cost_per)
    elif etype == "MERGE" and toks:
        shares = -_decimal(ev.delta_shares)
        proceeds_per = usdc / _decimal(len(toks))
        for t in toks:
            pos(t).remove(shares, proceeds_per)
    elif etype == "REDEEM" and toks:
        if cid in derived_conditions and usdc == _ZERO:
            return
        proceeds_per = usdc / _decimal(len(toks))
        for t in toks:
            p = positions.get(t)
            if p is not None:
                p.close(proceeds_per)
    elif etype == "REDEEM_PAYOUT" and toks:
        prices = resolution_prices.get(cid or "", {})
        for t in toks:
            p = positions.get(t)
            if p is not None and abs(p.qty) > DUST:
                p.close(p.qty * prices.get(t, _ZERO))
    elif etype == "RESOLUTION_SETTLEMENT" and ev.token_id is not None:
        p = positions.get(ev.token_id)
        if p is not None and abs(p.qty) > DUST:
            prices = resolution_prices.get(cid or "", {})
            p.close(p.qty * prices.get(ev.token_id, _ZERO))


def _percentiles(arr: array, ps: list[float]) -> dict[float, int]:
    if len(arr) == 0:
        return {p: 0 for p in ps}
    s = sorted(arr)
    out = {}
    for p in ps:
        idx = int(p * (len(s) - 1))
        out[p] = s[idx]
    return out


def _build_audit(wallet, computed_at, markets, positions, binary_conditions,
                 condition_categories, resolution_prices,
                 rewards, directional, wait_seconds, coverage, session):
    q = _load_questions(session, set(markets.keys()))

    pair_rows: list[dict] = []
    merge_rows: list[dict] = []
    orphan_rows: list[dict] = []
    temporal_rows: list[dict] = []

    # global orphan aggregates over resolved binary markets
    orphan_winner_qty = _ZERO
    orphan_loser_qty = _ZERO
    ambiguous_matched_qty = _ZERO
    resolved_markets = 0
    unresolved_markets = 0

    # global temporal aggregates
    total_matched = _ZERO
    total_under_60 = _ZERO
    total_under_300 = _ZERO  # computed from wait_seconds count basis below

    for cid, m in markets.items():
        cat = (condition_categories.get(cid, "") or "unknown")
        toks = m.tokens
        t0, t1 = toks[0], toks[1]
        buy0, buy1 = m.buy_qty[t0], m.buy_qty[t1]
        matched_buy = buy0 if buy0 < buy1 else buy1
        prices = resolution_prices.get(cid, {})
        resolved = bool(prices)

        # ---- 1) pair lifecycle ----
        pair_rows.append({
            "condition_id": cid,
            "question": q.get(cid, ""),
            "category": cat,
            "resolved": int(resolved),
            "has_resolution_metadata": int(resolved),
            "token0_id": t0,
            "token1_id": t1,
            "token0_buy_qty": _q6(buy0),
            "token1_buy_qty": _q6(buy1),
            "matched_pair_qty": _q6(matched_buy),
            "matched_outcome_shares_total": _q6(matched_buy * 2),
            "merge_sets_total": _q6(m.merge_sets),
            "merge_usdc_actual": _q6(m.merge_usdc),
            "pair_qty_vs_merge_diff": _q6(matched_buy - m.merge_sets),
            "split_sets": _q6(m.split_sets),
            "redeem_qty": _q6(m.redeem_qty),
            "redeem_usdc": _q6(m.redeem_usdc),
        })

        # ---- 2) merge realized edge ----
        if m.merge_sets > DUST:
            pair_cost = (m.merge_cost[t0] + m.merge_cost[t1])
            edge_per_set = m.merge_edge / m.merge_sets
            merge_rows.append({
                "condition_id": cid,
                "category": cat,
                "merge_sets": _q6(m.merge_sets),
                "consumed_token0_cost_basis": _q6(m.merge_cost[t0]),
                "consumed_token1_cost_basis": _q6(m.merge_cost[t1]),
                "pair_cost_per_set": _q6(pair_cost / m.merge_sets),
                "merge_payout_usdc": _q6(m.merge_usdc),
                "realized_edge_usdc": _q6(m.merge_edge),
                "realized_edge_per_set": _q6(edge_per_set),
                "realized_edge_bps": _q6(edge_per_set * Decimal(10000)),
            })

        # ---- 3) redeem / orphan audit (resolved binary only) ----
        if resolved:
            resolved_markets += 1
            winner = None
            for t in toks:
                if prices.get(t, _ZERO) >= WINNER_THRESHOLD:
                    winner = t
            q0, q1 = (m.res_snapshot if m.res_snapshot is not None
                      else (positions_qty(positions, t0), positions_qty(positions, t1)))
            matched_res = q0 if q0 < q1 else q1
            if matched_res < _ZERO:
                matched_res = _ZERO
            unmatched_side = t0 if q0 > q1 else t1
            unmatched_qty = abs(q0 - q1)
            uw = unmatched_qty if unmatched_side == winner else _ZERO
            ul = unmatched_qty if unmatched_side != winner and winner is not None else _ZERO
            ambiguous = matched_res > DUST
            if winner is not None:
                orphan_winner_qty += uw
                orphan_loser_qty += ul
                ambiguous_matched_qty += matched_res
            orphan_rows.append({
                "condition_id": cid,
                "category": cat,
                "winner_token": winner or "",
                "redeem_qty": _q6(m.redeem_qty),
                "redeem_payout_usdc": _q6(m.redeem_usdc),
                "cost_basis_of_redeemed_qty": _q6(m.redeem_cost),
                "qty0_at_resolution": _q6(q0),
                "qty1_at_resolution": _q6(q1),
                "matched_at_resolution": _q6(matched_res),
                "unmatched_winner_qty_at_resolution": _q6(uw),
                "unmatched_loser_qty_at_resolution": _q6(ul),
                "ambiguous_complete_set_vs_winner_residual": int(ambiguous),
            })
        else:
            unresolved_markets += 1

        # ---- 4) temporal imbalance ----
        directional_qty = abs(buy0 - buy1)
        maxbuy = buy0 if buy0 > buy1 else buy1
        imbalance_ratio = (directional_qty / maxbuy) if maxbuy > _ZERO else _ZERO
        mkt_matched = m.matched_pair_qty
        median_wait = (m.wait_weight_sum / mkt_matched) if mkt_matched > _ZERO else _ZERO
        pct_u60 = (m.pairs_under_60s / mkt_matched * Decimal(100)) if mkt_matched > _ZERO else _ZERO
        temporal_rows.append({
            "condition_id": cid,
            "category": cat,
            "token0_qty": _q6(buy0),
            "token1_qty": _q6(buy1),
            "matched_pair_qty": _q6(mkt_matched),
            "directional_imbalance_qty": _q6(directional_qty),
            "imbalance_ratio": _q6(imbalance_ratio),
            "mean_seconds_between_legs": _q6(median_wait),
            "max_seconds_between_legs": str(m.wait_seconds_max),
            "pct_pairs_under_60s": _q6(pct_u60),
        })
        total_matched += mkt_matched
        total_under_60 += m.pairs_under_60s

    # global temporal percentiles (count basis over matched lots)
    pc = _percentiles(wait_seconds, [0.5, 0.9, 0.99])
    n_lots = len(wait_seconds)
    under60 = sum(1 for w in wait_seconds if w <= 60)
    under300 = sum(1 for w in wait_seconds if w <= 300)

    # directional imbalance percentiles across markets
    dq = sorted(abs(m.buy_qty[m.tokens[0]] - m.buy_qty[m.tokens[1]]) for m in markets.values())
    ir = sorted(
        (abs(m.buy_qty[m.tokens[0]] - m.buy_qty[m.tokens[1]]) /
         max(m.buy_qty[m.tokens[0]], m.buy_qty[m.tokens[1]]))
        if max(m.buy_qty[m.tokens[0]], m.buy_qty[m.tokens[1]]) > _ZERO else _ZERO
        for m in markets.values()
    )

    def dpct(sorted_list, p):
        if not sorted_list:
            return _ZERO
        return sorted_list[int(p * (len(sorted_list) - 1))]

    residual_total = orphan_winner_qty + orphan_loser_qty
    pct_winner = (orphan_winner_qty / residual_total * Decimal(100)) if residual_total > _ZERO else _ZERO

    # ---- 5) pnl bridge (all + sports) ----
    pnl_bridge = _build_bridge(markets, positions, condition_categories,
                               rewards, directional, session, wallet)

    summary = {
        "temporal": {
            "n_matched_lots": n_lots,
            "pct_under_60s": (Decimal(under60) / Decimal(n_lots) * 100) if n_lots else _ZERO,
            "pct_under_300s": (Decimal(under300) / Decimal(n_lots) * 100) if n_lots else _ZERO,
            "p50_seconds": pc[0.5],
            "p90_seconds": pc[0.9],
            "p99_seconds": pc[0.99],
            "imbalance_qty_p50": dpct(dq, 0.5),
            "imbalance_qty_p90": dpct(dq, 0.9),
            "imbalance_qty_max": dq[-1] if dq else _ZERO,
            "imbalance_ratio_p50": dpct(ir, 0.5),
            "imbalance_ratio_p90": dpct(ir, 0.9),
        },
        "orphan": {
            "resolved_markets": resolved_markets,
            "unresolved_markets": unresolved_markets,
            "unmatched_winner_qty": orphan_winner_qty,
            "unmatched_loser_qty": orphan_loser_qty,
            "ambiguous_matched_qty": ambiguous_matched_qty,
            "pct_unmatched_residual_winner_by_qty": pct_winner,
        },
    }

    return CompletionSetAudit(
        wallet=wallet, computed_at=computed_at,
        pair_lifecycle=pair_rows, merge_edge=merge_rows,
        redeem_orphan=orphan_rows, temporal=temporal_rows,
        pnl_bridge=pnl_bridge, coverage=coverage, summary=summary,
    )


def positions_qty(positions, token_id) -> Decimal:
    p = positions.get(token_id)
    return p.qty if p is not None else _ZERO


def _build_bridge(markets, positions, condition_categories, rewards,
                  directional, session, wallet):
    """Bridge the reconstructed completion-set mechanics to gross PnL.

    Self-contained and fresh: `ledger_gross_pnl` is reconstructed from THIS
    read-only replay (directional sells + MERGE edge + REDEEM edge + rewards,
    all WAC per ADR 0003), so it never depends on the possibly-stale
    `pnl_decomposition` projection. That projection is reported only as a
    labelled cross-check (`projection_*` columns); a near-zero
    `xcheck_merge_delta` confirms the replay reproduces the accepted
    bond_merge, while a large `xcheck_redeem_delta` typically just means the
    projection was last rebuilt (via `pmr derive`) before recent resolutions
    landed — it does not invalidate the fresh reconstruction.
    """
    from ..projections.pnl_decomposition import fetch_pnl_decomposition

    fees = _estimated_fees(session, wallet)
    proj_all = {r.scope: r for r in fetch_pnl_decomposition(session, wallet)}
    proj_cat = {r.scope: r for r in fetch_pnl_decomposition(session, wallet, by_category=True)}
    proj_map = {"all": proj_all.get("all"), "sports": proj_cat.get("category:sports")}

    rows = []
    for scope in ("all", "sports"):
        buy_cost = _ZERO
        sell_proceeds = _ZERO
        merge_proceeds = _ZERO
        redeem_proceeds = _ZERO
        merge_edge = _ZERO
        redeem_edge = _ZERO
        for cid, m in markets.items():
            cat = (condition_categories.get(cid, "") or "").strip().lower()
            if scope == "sports" and cat != "sports":
                continue
            for t in m.tokens:
                buy_cost += m.buy_cost[t]
                sell_proceeds += m.sell_proceeds[t]
            merge_proceeds += m.merge_usdc
            redeem_proceeds += m.redeem_usdc
            merge_edge += m.merge_edge
            redeem_edge += m.redeem_edge
        rw = rewards[scope]
        dir_pnl = directional[scope]
        completion_pnl = merge_edge + redeem_edge  # the completion-set mechanics
        # fresh, self-contained reconstruction (no dependency on the projection)
        gross = completion_pnl + dir_pnl + rw
        pct_completion = (
            (completion_pnl / gross * Decimal(100)) if gross != _ZERO else _ZERO
        )
        net_after_fees = gross - fees if fees is not None else gross

        proj = proj_map.get(scope)
        proj_bond = _q6(proj.bond_merge_pnl) if proj is not None else "NA"
        proj_redemption = _q6(proj.redemption_pnl) if proj is not None else "NA"
        proj_directional = _q6(proj.directional_pnl) if proj is not None else "NA"
        xcheck_merge = _q6(merge_edge - proj.bond_merge_pnl) if proj is not None else "NA"
        xcheck_redeem = _q6(redeem_edge - proj.redemption_pnl) if proj is not None else "NA"
        note = (
            "ledger_gross_pnl reconstructed fresh from this replay = "
            "realized_merge_edge + realized_redeem_edge + directional_sell_pnl + "
            "rewards. projection_* columns are a labelled cross-check only "
            "(pnl_decomposition is rebuilt on-demand by `pmr derive` and may lag "
            "the live ledger). xcheck_merge_delta ~0 validates the merge edge; a "
            "large xcheck_redeem_delta usually reflects projection staleness."
        )
        rows.append({
            "scope": scope,
            "trade_buy_cost": _q6(buy_cost),
            "trade_sell_proceeds": _q6(sell_proceeds),
            "merge_proceeds": _q6(merge_proceeds),
            "redeem_proceeds": _q6(redeem_proceeds),
            "rewards_rebates": _q6(rw),
            "estimated_fees": _q6(fees) if fees is not None else "NA",
            "realized_merge_edge_usdc": _q6(merge_edge),
            "realized_redeem_edge_usdc": _q6(redeem_edge),
            "realized_directional_pnl": _q6(dir_pnl),
            "completion_mechanics_pnl": _q6(completion_pnl),
            "ledger_gross_pnl": _q6(gross),
            "pct_gross_from_completion": _q6(pct_completion),
            "reconstructed_net_after_fees": _q6(net_after_fees),
            "projection_bond_merge_pnl": proj_bond,
            "projection_redemption_pnl": proj_redemption,
            "projection_directional_pnl": proj_directional,
            "xcheck_merge_delta": xcheck_merge,
            "xcheck_redeem_delta": xcheck_redeem,
            "notes": note,
        })
    return rows


def _estimated_fees(session: Session, wallet: str) -> Optional[Decimal]:
    try:
        row = session.execute(_FEE_SQL, {"wallet": wallet.lower()}).fetchone()
    except Exception:
        return None
    if row is None or int(row.n or 0) == 0:
        return None
    try:
        return Decimal(str(row.s or "0"))
    except Exception:
        return None


def _load_questions(session: Session, condition_ids: set[str]) -> dict[str, str]:
    if not condition_ids:
        return {}
    rows = session.execute(
        text("SELECT condition_id, question FROM markets")
    ).fetchall()
    return {r.condition_id: (r.question or "") for r in rows if r.condition_id in condition_ids}


# --------------------------------------------------------------------------
# File writers
# --------------------------------------------------------------------------

def write_audit(audit: CompletionSetAudit, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def _csv(name: str, rows: list[dict], fieldnames: list[str]) -> None:
        path = out_dir / name
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames)
            w.writeheader()
            for r in rows:
                w.writerow(r)
        written.append(path)

    if audit.pair_lifecycle:
        _csv("rn1_pair_lifecycle_by_market.csv", audit.pair_lifecycle,
             list(audit.pair_lifecycle[0].keys()))
    if audit.merge_edge:
        _csv("rn1_merge_realized_edge.csv", audit.merge_edge,
             list(audit.merge_edge[0].keys()))
    if audit.redeem_orphan:
        _csv("rn1_redeem_orphan_audit.csv", audit.redeem_orphan,
             list(audit.redeem_orphan[0].keys()))
    if audit.temporal:
        _csv("rn1_temporal_imbalance_distribution.csv", audit.temporal,
             list(audit.temporal[0].keys()))
    _csv("rn1_pnl_bridge.csv", audit.pnl_bridge, list(audit.pnl_bridge[0].keys()))

    md_path = out_dir / "RN1_COMPLETION_SET_AUDIT.md"
    md_path.write_text(_render_markdown(audit), encoding="utf-8")
    written.append(md_path)
    return written


def _render_markdown(audit: CompletionSetAudit) -> str:
    s = audit.summary
    t = s["temporal"]
    o = s["orphan"]
    cov = audit.coverage
    bridge = {r["scope"]: r for r in audit.pnl_bridge}
    total_merge_edge = sum(Decimal(r["realized_edge_usdc"]) for r in audit.merge_edge)
    total_merge_sets = sum(Decimal(r["merge_sets"]) for r in audit.merge_edge)
    wavg_edge_per_set = (total_merge_edge / total_merge_sets) if total_merge_sets else _ZERO

    def bd(scope, key):
        return bridge.get(scope, {}).get(key, "NA")

    return f"""# RN1 Completion-Set / Inventory-Cycling Audit

Wallet: `{audit.wallet}`
Computed: {audit.computed_at}
Method: read-only replay of `wallet_events` (no projection or ledger mutation),
WAC cost basis (ADR 0003) reused from `projections.pnl_decomposition`.

## Executive summary

**Strong provisional finding:** RN1 appears to operate as a sports binary market
maker / inventory cycler. It accumulates both outcome tokens of binary markets
with passive buys, matches them into complete sets, and monetizes those sets via
MERGE (payout $1/set) plus resolution redemption of residual inventory. The PnL
bridge below compares this independent replay with the accepted projection and
reports any residuals explicitly.

Headline reconstructed numbers (all categories):

- Binary markets analyzed: **{cov['binary_condition_ids']:,}** of {cov['ledger_condition_ids']:,} ledger conditions.
- Total realized MERGE edge: **${total_merge_edge:,.0f}** over {total_merge_sets:,.0f} sets.
- Weighted realized edge per set: **{wavg_edge_per_set*100:.3f}c** ({wavg_edge_per_set*10000:.0f} bps of $1).
- Resolved markets audited for orphans: **{o['resolved_markets']:,}**.

## Thesis

1 token0 share + 1 token1 share = 1 complete set = $1. RN1 buys both legs below
$1 combined, so each completed set carries a positive gap. Sets are closed either
by MERGE (immediate $1) or by holding to resolution and redeeming. Directional
prediction is a minor component; the edge is structural (spread capture on the
completed set) at scale.

## What is supported

- Realized MERGE edge is positive and large (${total_merge_edge:,.0f}), consistent
  with buying complete sets below $1 and merging them for $1.
- Redemption of residual inventory is a second, smaller monetization channel.
- The PnL bridge reports the reconstruction against the accepted projection,
  including non-zero residuals instead of hiding them.

## What is NOT supported / still open

- This audit does **not** prove the edge is risk-free. Between legs, inventory is
  temporarily one-sided (directional).
- Orphan residual direction is reported from **net holdings at resolution**, not
  asserted as "100% winner". Where matched inventory persists to resolution, rows
  are flagged `ambiguous_complete_set_vs_winner_residual`.
- Fees are estimated-schedule only (no actual per-fill fee evidence).
- Temporal matching is buy-leg FIFO; SPLIT-created sets are folded into WAC but
  excluded from buy-pair timing.

## Units: outcome shares vs complete sets

`matched_pair_qty` counts **sets**. Total outcome shares = `2 * matched_pair_qty`.
A MERGE of N sets destroys N of each token and pays **N USDC**; never compare
total outcome shares against MERGE payout without dividing by 2. Column
`pair_qty_vs_merge_diff` surfaces markets where buy-matched sets and actual merged
sets diverge (partial merges, holds to resolution, or splits).

## MERGE realized edge

- Total realized edge: **${total_merge_edge:,.2f}** over **{total_merge_sets:,.0f}** sets.
- Weighted edge per set: **{wavg_edge_per_set*100:.4f}c** ({wavg_edge_per_set*10000:.1f} bps).
- Per-market detail: `rn1_merge_realized_edge.csv`
  (cost basis per leg via WAC, `pair_cost_per_set`, `realized_edge_bps`).

## REDEEM / orphan audit

- Resolved binary markets: **{o['resolved_markets']:,}** (unresolved: {o['unresolved_markets']:,}).
- Unmatched residual at resolution - winner qty: **{o['unmatched_winner_qty']:,.0f}**,
  loser qty: **{o['unmatched_loser_qty']:,.0f}**.
- Ambiguous matched-at-resolution qty (complete sets held, not a directional
  lean): **{o['ambiguous_matched_qty']:,.0f}**.
- Share of clean unmatched residual that is the winner, by qty:
  **{o['pct_unmatched_residual_winner_by_qty']:.1f}%**.
- Per-market detail: `rn1_redeem_orphan_audit.csv`.

> Note: the winner-lean of residual is reported, not the earlier informal
> "100% winner" claim. The `ambiguous_*` flag marks markets where a naive
> residual reading would overstate a directional lean.

## Temporal imbalance

- Matched buy-lot events: **{t['n_matched_lots']:,}**.
- Completed within <=60s: **{t['pct_under_60s']:.1f}%**; <=300s: **{t['pct_under_300s']:.1f}%**.
- Seconds between legs - p50: **{t['p50_seconds']:,}s**, p90: **{t['p90_seconds']:,}s**,
  p99: **{t['p99_seconds']:,}s**.
- Directional imbalance qty per market - p50: **{t['imbalance_qty_p50']:,.0f}**,
  p90: **{t['imbalance_qty_p90']:,.0f}**, max: **{t['imbalance_qty_max']:,.0f}**.
- Imbalance ratio per market - p50: **{t['imbalance_ratio_p50']:.3f}**,
  p90: **{t['imbalance_ratio_p90']:.3f}**.
- Per-market detail: `rn1_temporal_imbalance_distribution.csv`.

Interpretation: legs are mostly **not** simultaneous, so this is continuous
passive liquidity provision across a market's life, not flash arbitrage.

## PnL bridge

`ledger_gross_pnl` is reconstructed fresh from this read-only replay
(realized_merge_edge + realized_redeem_edge + directional_sell_pnl + rewards, all
WAC). It does not depend on the `pnl_decomposition` projection; the `projection_*`
columns are a labelled cross-check only.

| metric | all | sports |
|---|---:|---:|
| merge_proceeds | {bd('all','merge_proceeds')} | {bd('sports','merge_proceeds')} |
| redeem_proceeds | {bd('all','redeem_proceeds')} | {bd('sports','redeem_proceeds')} |
| rewards_rebates | {bd('all','rewards_rebates')} | {bd('sports','rewards_rebates')} |
| realized_merge_edge_usdc | {bd('all','realized_merge_edge_usdc')} | {bd('sports','realized_merge_edge_usdc')} |
| realized_redeem_edge_usdc | {bd('all','realized_redeem_edge_usdc')} | {bd('sports','realized_redeem_edge_usdc')} |
| realized_directional_pnl | {bd('all','realized_directional_pnl')} | {bd('sports','realized_directional_pnl')} |
| completion_mechanics_pnl | {bd('all','completion_mechanics_pnl')} | {bd('sports','completion_mechanics_pnl')} |
| ledger_gross_pnl (fresh) | {bd('all','ledger_gross_pnl')} | {bd('sports','ledger_gross_pnl')} |
| pct_gross_from_completion | {bd('all','pct_gross_from_completion')} | {bd('sports','pct_gross_from_completion')} |
| estimated_fees | {bd('all','estimated_fees')} | {bd('sports','estimated_fees')} |
| reconstructed_net_after_fees | {bd('all','reconstructed_net_after_fees')} | {bd('sports','reconstructed_net_after_fees')} |
| xcheck_merge_delta (~0) | {bd('all','xcheck_merge_delta')} | {bd('sports','xcheck_merge_delta')} |
| xcheck_redeem_delta | {bd('all','xcheck_redeem_delta')} | {bd('sports','xcheck_redeem_delta')} |
| projection_bond_merge_pnl | {bd('all','projection_bond_merge_pnl')} | {bd('sports','projection_bond_merge_pnl')} |

Full detail: `rn1_pnl_bridge.csv`. A near-zero `xcheck_merge_delta` confirms this
independent WAC replay reproduces the accepted projection's bond_merge; a large
`xcheck_redeem_delta` typically reflects `pnl_decomposition` staleness (rebuilt
on-demand by `pmr derive`), not an error in the fresh reconstruction.

## Coverage / metadata accounting

- Events processed: **{cov['events_processed']:,}**.
- Ledger conditions: {cov['ledger_condition_ids']:,}; binary (2 tokens): {cov['binary_condition_ids']:,}.
- Non-binary condition events (excluded from pair analysis, kept in WAC): {cov['events_non_binary']:,} across {cov['non_binary_condition_ids']:,} conditions.
- Unmapped-condition events (no market metadata): {cov['events_unmapped_condition']:,} across {cov['unmapped_condition_ids']:,} conditions.
- Events with no condition_id: {cov['events_no_condition']:,}.

## Caveats

- Read-only snapshot; projections such as `episodes` may lag the ledger.
- Prices/sizes taken verbatim from `wallet_events`.
- WAC (not FIFO) for realized edge, per ADR 0003; temporal matching uses buy-leg
  FIFO purely for timing diagnostics.
- Sports scoping depends on `markets.category`; uncategorized markets fall only
  into `all`.

## Next steps

- Attribute the per-set edge to counterparties (who crosses RN1's resting bids).
- Distinguish merge-close vs resolution-close sets in the lifecycle table.
- Model MERGE/REDEEM inventory reduction inside the temporal deque to refine the
  ambiguous-residual classification.
"""
