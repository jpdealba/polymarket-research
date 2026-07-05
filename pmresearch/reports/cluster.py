"""Phase 19: wallet cluster comparison and candidate ranking (read-only).

Answers "which wallets are worth studying alongside a leader wallet (e.g.
RN1), and are they independent, followers, sharing a signal, or plausibly the
same operator?" per docs/plan/phase18_to_24_rn1_microstructure_research_plan.md
(Phase 19). This is prioritization, not proof of causality: nothing here
writes to the ledger or any projection table (ADR 0006).

For each candidate trade, the nearest leader trade within +/-window_s is
found under a precedence of match tiers (tightest first): same token+side,
same token any side, same question (market) + side, same question any side,
same event (different market). The classification thresholds below
operationalize the plan's descriptive definitions (lines 462-479 of the plan
doc) into concrete numeric cutoffs -- these are heuristic starting points, not
statistically validated, and are meant to be tuned as more wallets are
compared.
"""

from __future__ import annotations

import bisect
import csv
from dataclasses import dataclass
from datetime import date as date_cls, timedelta
from decimal import Decimal
from pathlib import Path
from statistics import median
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..projections.daily_equity import fetch_daily_equity, latest_daily_equity
from ..projections.holdings import fetch_holdings
from ..walletmanager.manager import list_wallets

_ZERO = Decimal("0")

_TRADES_SQL = text(
    "SELECT ts, condition_id, token_id, side, price, delta_shares "
    "FROM wallet_events WHERE wallet = :wallet AND event_type = 'TRADE' "
    "ORDER BY ts"
)

_MARKET_META_SQL = text("SELECT condition_id, question, event_id FROM markets")


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or 0))


@dataclass
class ClusterCompareResult:
    leader: str
    wallet: str
    window_s: int
    matches: list[dict]
    summary: dict
    classification: str


@dataclass
class WalletCandidateScore:
    wallet: str
    label: str
    overlap_score: Decimal
    same_token_side_match_rate: Decimal
    same_question_match_rate: Decimal
    median_delay_s: int
    rn1_first_share: Decimal
    candidate_first_share: Decimal
    pnl_1d: Decimal
    pnl_1w: Decimal
    positions_value: Decimal
    closed_cycle_score: Decimal
    research_priority: Decimal
    classification: str


def _load_trades(session: Session, wallet: str) -> list[dict]:
    rows = session.execute(_TRADES_SQL, {"wallet": wallet.lower()}).fetchall()
    return [
        {
            "ts": int(r.ts),
            "condition_id": r.condition_id,
            "token_id": r.token_id,
            "side": r.side,
            "price": _decimal(r.price),
            "shares": abs(_decimal(r.delta_shares)),
        }
        for r in rows
    ]


def _load_market_meta(session: Session) -> dict[str, tuple[str, Optional[str]]]:
    rows = session.execute(_MARKET_META_SQL).fetchall()
    return {r.condition_id: (r.question or "", r.event_id) for r in rows}


def _match_tier(
    c: dict, window_trades: list[dict], meta: dict[str, tuple[str, Optional[str]]]
) -> tuple[Optional[str], Optional[dict]]:
    c_question, c_event = meta.get(c["condition_id"] or "", ("", None))

    same_token_side = [
        t for t in window_trades if t["token_id"] == c["token_id"] and t["side"] == c["side"]
    ]
    if same_token_side:
        tier = same_token_side
        tier_name = "same_token_side"
    else:
        same_token = [t for t in window_trades if t["token_id"] == c["token_id"]]
        if same_token:
            tier = same_token
            tier_name = "same_token_any_side"
        elif c_question:
            same_q_side = [
                t
                for t in window_trades
                if meta.get(t["condition_id"] or "", ("", None))[0] == c_question
                and t["side"] == c["side"]
            ]
            if same_q_side:
                tier = same_q_side
                tier_name = "same_question_side"
            else:
                same_q = [
                    t
                    for t in window_trades
                    if meta.get(t["condition_id"] or "", ("", None))[0] == c_question
                ]
                if same_q:
                    tier = same_q
                    tier_name = "same_question_any_side"
                elif c_event:
                    same_event = [
                        t
                        for t in window_trades
                        if meta.get(t["condition_id"] or "", ("", None))[1] == c_event
                        and t["condition_id"] != c["condition_id"]
                    ]
                    if same_event:
                        tier = same_event
                        tier_name = "same_event"
                    else:
                        return None, None
                else:
                    return None, None
        else:
            return None, None

    best = min(tier, key=lambda t: abs(t["ts"] - c["ts"]))
    return tier_name, best


def analyze_cluster_compare(
    session: Session, leader: str, wallet: str, *, window_s: int = 300
) -> ClusterCompareResult:
    leader = leader.lower()
    wallet = wallet.lower()
    meta = _load_market_meta(session)
    leader_trades = _load_trades(session, leader)
    candidate_trades = _load_trades(session, wallet)
    leader_ts_list = [t["ts"] for t in leader_trades]

    matches: list[dict] = []
    match_type_counts: dict[str, int] = {}
    delays: list[int] = []
    price_diffs: list[Decimal] = []
    size_ratios: list[Decimal] = []
    same_side_count = 0
    leader_first = 0
    candidate_first = 0
    simultaneous = 0
    same_event_count = 0

    for c in candidate_trades:
        lo = bisect.bisect_left(leader_ts_list, c["ts"] - window_s)
        hi = bisect.bisect_right(leader_ts_list, c["ts"] + window_s)
        window_trades = leader_trades[lo:hi]
        if not window_trades:
            continue
        tier_name, best = _match_tier(c, window_trades, meta)
        if tier_name is None or best is None:
            continue

        match_type_counts[tier_name] = match_type_counts.get(tier_name, 0) + 1
        delay = c["ts"] - best["ts"]
        delays.append(delay)
        side_match = best["side"] == c["side"]
        if side_match:
            same_side_count += 1
        price_diff = abs(c["price"] - best["price"])
        price_diffs.append(price_diff)
        size_ratio = (c["shares"] / best["shares"]) if best["shares"] > _ZERO else _ZERO
        size_ratios.append(size_ratio)
        if delay > 0:
            leader_first += 1
        elif delay < 0:
            candidate_first += 1
        else:
            simultaneous += 1

        c_meta = meta.get(c["condition_id"] or "", ("", None))
        best_meta = meta.get(best["condition_id"] or "", ("", None))
        if c_meta[1] is not None and c_meta[1] == best_meta[1]:
            same_event_count += 1

        matches.append(
            {
                "candidate_trade_ts": c["ts"],
                "leader_trade_ts": best["ts"],
                "delay_s": delay,
                "leader": leader,
                "candidate": wallet,
                "token_id": c["token_id"],
                "condition_id": c["condition_id"],
                "question": c_meta[0],
                "side_candidate": c["side"],
                "side_leader": best["side"],
                "side_match": int(side_match),
                "price_candidate": str(c["price"]),
                "price_leader": str(best["price"]),
                "price_diff": str(price_diff),
                "size_candidate": str(c["shares"]),
                "size_leader": str(best["shares"]),
                "size_ratio": str(size_ratio),
                "match_type": tier_name,
            }
        )

    total_candidate_trades = len(candidate_trades)
    matched_count = len(matches)
    same_token_matches = match_type_counts.get("same_token_side", 0) + match_type_counts.get(
        "same_token_any_side", 0
    )
    same_question_matches = (
        same_token_matches
        + match_type_counts.get("same_question_side", 0)
        + match_type_counts.get("same_question_any_side", 0)
    )

    def _rate(n: int, d: int) -> float:
        return (n / d) if d else 0.0

    abs_delays = sorted(abs(d) for d in delays)
    sorted_price_diffs = sorted(price_diffs)

    def _pctile(sorted_vals: list, p: float):
        if not sorted_vals:
            return 0
        return sorted_vals[int(p * (len(sorted_vals) - 1))]

    summary = {
        "matched_trade_count": matched_count,
        "total_candidate_trades": total_candidate_trades,
        "match_rate_same_token": _rate(same_token_matches, total_candidate_trades),
        "match_rate_same_question": _rate(same_question_matches, total_candidate_trades),
        "same_side_match_rate": _rate(same_side_count, matched_count),
        "median_delay_seconds": median(abs_delays) if abs_delays else 0,
        "p90_delay_seconds": _pctile(abs_delays, 0.9),
        "price_diff_median": str(median(price_diffs)) if price_diffs else "0",
        "price_diff_p90": str(_pctile(sorted_price_diffs, 0.9)),
        "size_ratio_median": str(median(size_ratios)) if size_ratios else "0",
        "rn1_first_share": _rate(leader_first, matched_count),
        "candidate_first_share": _rate(candidate_first, matched_count),
        "simultaneous_share": _rate(simultaneous, matched_count),
        "same_event_share": _rate(same_event_count, matched_count),
    }

    size_ratio_median = Decimal(summary["size_ratio_median"])
    classification = _classify(summary, size_ratio_median)
    return ClusterCompareResult(
        leader=leader,
        wallet=wallet,
        window_s=window_s,
        matches=matches,
        summary=summary,
        classification=classification,
    )


def _classify(summary: dict, size_ratio_median: Decimal) -> str:
    """Operationalizes the plan's classification definitions (plan doc lines
    462-479) into concrete numeric cutoffs. Heuristic starting point, not
    statistically validated -- tune as more wallets get compared."""
    total = summary["total_candidate_trades"]
    matched = summary["matched_trade_count"]
    if total == 0 or matched == 0:
        return "independent"

    median_delay = summary["median_delay_seconds"]
    same_token_side_rate = summary["match_rate_same_token"]
    candidate_first_share = summary["candidate_first_share"]
    same_event_share = summary["same_event_share"]
    match_rate = matched / total
    size_ok = size_ratio_median > _ZERO and Decimal("0.5") <= size_ratio_median <= Decimal("2.0")

    if median_delay <= 5 and same_token_side_rate >= 0.5 and size_ok:
        return "same_system_candidate"
    if 5 < median_delay <= 300 and same_token_side_rate >= 0.3 and candidate_first_share <= 0.2:
        return "follower"
    if same_event_share >= 0.3 and same_token_side_rate < 0.3:
        return "shared_signal"
    if match_rate >= 0.1:
        return "research_candidate"
    return "independent"


def _pnl_window(session: Session, wallet: str, days: int) -> Decimal:
    """PnL over the trailing `days` window, diffed from daily_equity.marked_pnl
    against the nearest available snapshot at or before that many days back.
    Returns 0 if there isn't enough equity history to compute a diff."""
    rows = fetch_daily_equity(session, wallet)
    if not rows:
        return _ZERO
    latest = rows[-1]
    target = date_cls.fromisoformat(latest.date) - timedelta(days=days)
    baseline = None
    for row in rows:
        if date_cls.fromisoformat(row.date) <= target:
            baseline = row
        else:
            break
    if baseline is None:
        return _ZERO
    return latest.marked_pnl - baseline.marked_pnl


def _closed_cycle_score(session: Session, wallet: str) -> Decimal:
    """Heuristic proxy: share of the wallet's traded conditions with no open
    (nonzero) holding left. Not defined anywhere in the plan -- a stand-in
    until Phase 20/21 lifecycle data gives a precise close_path metric."""
    wallet = wallet.lower()
    traded = session.execute(
        text(
            "SELECT COUNT(DISTINCT condition_id) AS n FROM wallet_events "
            "WHERE wallet = :w AND condition_id IS NOT NULL"
        ),
        {"w": wallet},
    ).scalar_one()
    if not traded:
        return _ZERO
    holdings = fetch_holdings(session, wallet, nonzero=True)
    open_conditions = {h.condition_id for h in holdings if h.condition_id}
    closed = traded - len(open_conditions)
    return Decimal(closed) / Decimal(traded)


def _research_priority(
    overlap_score: Decimal, closed_cycle_score: Decimal, pnl_1w: Decimal
) -> Decimal:
    """Simple weighted composite -- the plan doesn't define this formula
    either, so kept explicit and easy to re-tune: 40% overlap with the
    leader, 30% closed-cycle share, 30% a soft-capped positive 1-week PnL."""
    pnl_component = pnl_1w if pnl_1w > _ZERO else _ZERO
    pnl_component = min(pnl_component, Decimal("10000")) / Decimal("10000")
    return (
        overlap_score * Decimal("0.4")
        + closed_cycle_score * Decimal("0.3")
        + pnl_component * Decimal("0.3")
    )


def _wallet_label(session: Session, wallet: str) -> str:
    row = session.execute(
        text("SELECT display_name FROM wallets WHERE address = :w"), {"w": wallet.lower()}
    ).fetchone()
    if row is not None and row.display_name:
        return row.display_name
    return wallet


def analyze_cluster_candidates(
    session: Session,
    leader: str,
    *,
    wallets: Optional[list[str]] = None,
    window_s: int = 300,
) -> list[WalletCandidateScore]:
    leader = leader.lower()
    if wallets is None:
        universe = [row.address for row in list_wallets(session, active_only=True)]
    else:
        universe = wallets
    candidate_wallets = [w.lower() for w in universe if w.lower() != leader]

    scores: list[WalletCandidateScore] = []
    for w in candidate_wallets:
        result = analyze_cluster_compare(session, leader, w, window_s=window_s)
        s = result.summary
        overlap_score = Decimal(str(s["match_rate_same_token"]))
        same_question_rate = Decimal(str(s["match_rate_same_question"]))
        pnl_1d = _pnl_window(session, w, 1)
        pnl_1w = _pnl_window(session, w, 7)
        latest_equity = latest_daily_equity(session, w)
        positions_value = latest_equity.portfolio_value if latest_equity else _ZERO
        closed_cycle_score = _closed_cycle_score(session, w)
        priority = _research_priority(overlap_score, closed_cycle_score, pnl_1w)

        scores.append(
            WalletCandidateScore(
                wallet=w,
                label=_wallet_label(session, w),
                overlap_score=overlap_score,
                same_token_side_match_rate=overlap_score,
                same_question_match_rate=same_question_rate,
                median_delay_s=s["median_delay_seconds"],
                rn1_first_share=Decimal(str(s["rn1_first_share"])),
                candidate_first_share=Decimal(str(s["candidate_first_share"])),
                pnl_1d=pnl_1d,
                pnl_1w=pnl_1w,
                positions_value=positions_value,
                closed_cycle_score=closed_cycle_score,
                research_priority=priority,
                classification=result.classification,
            )
        )

    scores.sort(key=lambda r: r.research_priority, reverse=True)
    return scores


# --------------------------------------------------------------------------
# File writers
# --------------------------------------------------------------------------


def write_compare_report(result: ClusterCompareResult, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    if result.matches:
        csv_path = out_dir / "cluster_match_table.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=list(result.matches[0].keys()))
            w.writeheader()
            for row in result.matches:
                w.writerow(row)
        written.append(csv_path)

    md_path = out_dir / "CLUSTER_COMPARE.md"
    md_path.write_text(_render_compare_markdown(result), encoding="utf-8")
    written.append(md_path)
    return written


def _render_compare_markdown(result: ClusterCompareResult) -> str:
    s = result.summary
    return f"""# Cluster compare: {result.wallet} vs leader {result.leader}

Window: +/-{result.window_s}s. Read-only; no ledger/projection writes (ADR 0006).

Classification: **{result.classification}**

| metric | value |
|---|---:|
| matched_trade_count | {s['matched_trade_count']} |
| total_candidate_trades | {s['total_candidate_trades']} |
| match_rate_same_token | {s['match_rate_same_token']*100:.1f}% |
| match_rate_same_question | {s['match_rate_same_question']*100:.1f}% |
| same_side_match_rate | {s['same_side_match_rate']*100:.1f}% |
| median_delay_seconds | {s['median_delay_seconds']} |
| p90_delay_seconds | {s['p90_delay_seconds']} |
| price_diff_median | {s['price_diff_median']} |
| price_diff_p90 | {s['price_diff_p90']} |
| size_ratio_median | {s['size_ratio_median']} |
| rn1_first_share | {s['rn1_first_share']*100:.1f}% |
| candidate_first_share | {s['candidate_first_share']*100:.1f}% |
| simultaneous_share | {s['simultaneous_share']*100:.1f}% |
| same_event_share | {s['same_event_share']*100:.1f}% |

Full match detail: `cluster_match_table.csv`.
"""


def write_cluster_report(results: list[ClusterCompareResult], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    leader = results[0].leader if results else ""
    sections = [_render_compare_markdown(r) for r in results]
    body = f"""# Wallet cluster report vs leader {leader}

Read-only comparison; no causal claim, no ledger/projection writes (ADR 0006).

""" + "\n---\n\n".join(sections)
    out_path.write_text(body, encoding="utf-8")
    return out_path


def write_candidates_csv(scores: list[WalletCandidateScore], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "wallet",
        "label",
        "overlap_score",
        "same_token_side_match_rate",
        "same_question_match_rate",
        "median_delay_s",
        "rn1_first_share",
        "candidate_first_share",
        "pnl_1d",
        "pnl_1w",
        "positions_value",
        "closed_cycle_score",
        "research_priority",
        "classification",
    ]
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for s in scores:
            w.writerow(
                {
                    "wallet": s.wallet,
                    "label": s.label,
                    "overlap_score": str(s.overlap_score),
                    "same_token_side_match_rate": str(s.same_token_side_match_rate),
                    "same_question_match_rate": str(s.same_question_match_rate),
                    "median_delay_s": s.median_delay_s,
                    "rn1_first_share": str(s.rn1_first_share),
                    "candidate_first_share": str(s.candidate_first_share),
                    "pnl_1d": str(s.pnl_1d),
                    "pnl_1w": str(s.pnl_1w),
                    "positions_value": str(s.positions_value),
                    "closed_cycle_score": str(s.closed_cycle_score),
                    "research_priority": str(s.research_priority),
                    "classification": s.classification,
                }
            )
    return out_path
