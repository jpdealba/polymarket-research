"""Episodes projection: flat-to-flat token episodes with WAC PnL.

ADR 0003 fixes the convention: token-level flat-to-flat boundaries, weighted
average cost, and no debounce. A zero crossing is therefore always a boundary,
even when the next event re-enters the same token at the same timestamp.

Phase 8 adds derived REDEEM_PAYOUT rows for source-reported zero redemption
cash legs; when those rows are present, resolution-close PnL includes the
derived proceeds.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal
from statistics import median
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..ledger.replay import stream_events
from .base import Projection

EPISODES_PROJECTION_VERSION = 2
MICRO_EPISODE_SECONDS = 60

_ZERO = Decimal("0")

_INSERT_SQL = text(
    "INSERT INTO episodes "
    "(wallet, token_id, condition_id, open_ts, close_ts, close_reason, peak_qty, "
    "num_adds, num_partial_exits, wac_entry, realized_pnl, reward_income, fees_paid, "
    "events_consumed, projection_version) "
    "VALUES (:wallet, :token_id, :condition_id, :open_ts, :close_ts, :close_reason, "
    ":peak_qty, :num_adds, :num_partial_exits, :wac_entry, :realized_pnl, "
    ":reward_income, :fees_paid, :events_consumed, :projection_version)"
)


@dataclass(frozen=True)
class EpisodesRebuildStats:
    wallet: str
    events_processed: int
    episodes_written: int
    open_episodes: int
    flat_closed_episodes: int
    resolution_closed_episodes: int
    event_applications_consumed: int
    unmapped_condition_events: int
    unmapped_condition_ids: int
    as_of_ts: int


@dataclass(frozen=True)
class EpisodeStats:
    wallet: str
    count: int
    open_count: int
    flat_closed_count: int
    resolution_closed_count: int
    duration_min: Optional[int]
    duration_p50: Optional[float]
    duration_p90: Optional[float]
    duration_max: Optional[int]
    micro_episode_count: int
    micro_episode_share: Decimal
    realized_pnl: Decimal
    reward_income: Decimal


@dataclass
class _Episode:
    wallet: str
    token_id: str
    condition_id: Optional[str]
    open_ts: int
    qty: Decimal = _ZERO
    cost: Decimal = _ZERO
    peak_qty: Decimal = _ZERO
    num_adds: int = 0
    num_partial_exits: int = 0
    wac_entry: Decimal = _ZERO
    realized_pnl: Decimal = _ZERO
    reward_income: Decimal = _ZERO
    events_consumed: list[int] = field(default_factory=list)

    def consume(self, event_id: int) -> None:
        if not self.events_consumed or self.events_consumed[-1] != event_id:
            self.events_consumed.append(event_id)

    def add(self, shares: Decimal, cost: Decimal, event_id: int, *, is_opening: bool) -> None:
        if not is_opening:
            self.num_adds += 1
        self.qty += shares
        self.cost += cost
        if self.qty > self.peak_qty:
            self.peak_qty = self.qty
        if self.qty > _ZERO:
            self.wac_entry = self.cost / self.qty
        self.consume(event_id)

    def remove(
        self,
        shares: Decimal,
        proceeds: Decimal,
        event_id: int,
        *,
        dust_epsilon: Decimal,
        force_close: bool = False,
    ) -> bool:
        qty_before = self.qty
        cost_before = self.cost

        if force_close:
            shares = qty_before

        if qty_before > _ZERO:
            closes = force_close or abs(qty_before - shares) <= dust_epsilon
            if closes:
                self.realized_pnl += proceeds - cost_before
                self.qty = _ZERO
                self.cost = _ZERO
            elif shares > qty_before:
                self.realized_pnl += proceeds - cost_before
                self.qty = qty_before - shares
                self.cost = _ZERO
                self.wac_entry = _ZERO
            else:
                basis = cost_before * shares / qty_before
                self.realized_pnl += proceeds - basis
                self.qty = qty_before - shares
                self.cost = cost_before - basis
                self.wac_entry = self.cost / self.qty
                self.num_partial_exits += 1
        else:
            self.qty = qty_before - shares
            self.cost = _ZERO

        if self.qty > self.peak_qty:
            self.peak_qty = self.qty
        self.consume(event_id)
        return force_close or abs(self.qty) <= dust_epsilon


def _load_token_maps(session: Session) -> tuple[dict[str, list[str]], dict[str, str]]:
    rows = session.execute(
        text("SELECT condition_id, token_id FROM tokens ORDER BY condition_id, outcome_index")
    )
    condition_tokens: dict[str, list[str]] = {}
    token_conditions: dict[str, str] = {}
    for row in rows:
        condition_tokens.setdefault(row.condition_id, []).append(row.token_id)
        token_conditions[row.token_id] = row.condition_id
    return condition_tokens, token_conditions


def _load_resolution_prices(session: Session) -> dict[str, dict[str, Decimal]]:
    rows = session.execute(
        text(
            "SELECT condition_id, resolution_prices_json FROM markets "
            "WHERE resolution_prices_json IS NOT NULL"
        )
    ).fetchall()
    out: dict[str, dict[str, Decimal]] = {}
    for row in rows:
        try:
            payload = json.loads(row.resolution_prices_json or "{}")
        except json.JSONDecodeError:
            payload = {}
        out[row.condition_id] = {
            str(token_id): Decimal(str(price)) for token_id, price in payload.items()
        }
    return out


def _derived_redeem_conditions(session: Session, wallet: str) -> set[str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT condition_id FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'REDEEM_PAYOUT' "
            "AND is_derived = 1 AND condition_id IS NOT NULL"
        ),
        {"wallet": wallet.lower()},
    ).fetchall()
    return {row.condition_id for row in rows}


def _row(episode: _Episode, close_ts: Optional[int], close_reason: str) -> dict:
    return {
        "wallet": episode.wallet,
        "token_id": episode.token_id,
        "condition_id": episode.condition_id,
        "open_ts": episode.open_ts,
        "close_ts": close_ts,
        "close_reason": close_reason,
        "peak_qty": str(episode.peak_qty),
        "num_adds": episode.num_adds,
        "num_partial_exits": episode.num_partial_exits,
        "wac_entry": str(episode.wac_entry),
        "realized_pnl": str(episode.realized_pnl),
        "reward_income": str(episode.reward_income),
        "fees_paid": None,
        "events_consumed": json.dumps(episode.events_consumed, separators=(",", ":")),
        "projection_version": EPISODES_PROJECTION_VERSION,
    }


def rebuild_episodes(
    session: Session, wallet: str, *, dust_epsilon: Decimal = Decimal("0.000001")
) -> EpisodesRebuildStats:
    """Drop and rebuild flat-to-flat episodes for one wallet from the ledger."""
    wallet = wallet.lower()
    condition_tokens, token_conditions = _load_token_maps(session)
    resolution_prices = _load_resolution_prices(session)
    derived_redeem_conditions = _derived_redeem_conditions(session, wallet)

    active: dict[str, _Episode] = {}
    rows: list[dict] = []
    events_processed = 0
    event_applications = 0
    unmapped_condition_events = 0
    unmapped_conditions: set[str] = set()
    max_ts = 0

    def get_episode(token_id: str, condition_id: Optional[str], ts: int) -> tuple[_Episode, bool]:
        episode = active.get(token_id)
        if episode is not None:
            return episode, False
        episode = _Episode(
            wallet=wallet,
            token_id=token_id,
            condition_id=condition_id or token_conditions.get(token_id),
            open_ts=ts,
        )
        active[token_id] = episode
        return episode, True

    def close(token_id: str, ts: int, reason: str) -> None:
        episode = active.pop(token_id)
        rows.append(_row(episode, ts, reason))

    def apply_add(token_id: str, condition_id: Optional[str], ts: int, event_id: int, shares: Decimal, cost: Decimal) -> None:
        nonlocal event_applications
        if abs(shares) <= dust_epsilon:
            return
        episode, is_opening = get_episode(token_id, condition_id, ts)
        episode.add(shares, cost, event_id, is_opening=is_opening)
        event_applications += 1

    def apply_remove(
        token_id: str,
        condition_id: Optional[str],
        ts: int,
        event_id: int,
        shares: Decimal,
        proceeds: Decimal,
        *,
        reason: str = "flat",
        force_close: bool = False,
    ) -> None:
        nonlocal event_applications
        episode = active.get(token_id)
        if episode is None:
            if abs(shares) <= dust_epsilon:
                return
            episode, _ = get_episode(token_id, condition_id, ts)
        should_close = episode.remove(
            shares,
            proceeds,
            event_id,
            dust_epsilon=dust_epsilon,
            force_close=force_close,
        )
        event_applications += 1
        if should_close:
            close(token_id, ts, reason)

    for event in stream_events(session, wallet=wallet):
        events_processed += 1
        ts = event.ts
        if ts > max_ts:
            max_ts = ts

        etype = event.event_type
        if etype == "TRADE":
            if event.token_id is None:
                continue
            delta = Decimal(event.delta_shares)
            if delta > _ZERO:
                apply_add(
                    event.token_id,
                    event.condition_id,
                    ts,
                    event.id,
                    delta,
                    -Decimal(event.delta_usdc),
                )
            elif delta < _ZERO:
                apply_remove(
                    event.token_id,
                    event.condition_id,
                    ts,
                    event.id,
                    -delta,
                    Decimal(event.delta_usdc),
                )

        elif etype in ("SPLIT", "MERGE", "REDEEM", "REDEEM_PAYOUT"):
            tokens = condition_tokens.get(event.condition_id or "")
            if not tokens:
                unmapped_condition_events += 1
                if event.condition_id:
                    unmapped_conditions.add(event.condition_id)
                continue
            if etype == "SPLIT":
                shares = Decimal(event.delta_shares)
                cost_per_token = -Decimal(event.delta_usdc) / len(tokens)
                for token_id in tokens:
                    apply_add(token_id, event.condition_id, ts, event.id, shares, cost_per_token)
            elif etype == "MERGE":
                shares = -Decimal(event.delta_shares)
                proceeds_per_token = Decimal(event.delta_usdc) / len(tokens)
                for token_id in tokens:
                    apply_remove(
                        token_id,
                        event.condition_id,
                        ts,
                        event.id,
                        shares,
                        proceeds_per_token,
                    )
            elif etype == "REDEEM":
                if (
                    event.condition_id in derived_redeem_conditions
                    and Decimal(event.delta_usdc) == _ZERO
                ):
                    continue
                proceeds_per_token = Decimal(event.delta_usdc) / len(tokens)
                for token_id in tokens:
                    episode = active.get(token_id)
                    if episode is None:
                        continue
                    apply_remove(
                        token_id,
                        event.condition_id,
                        ts,
                        event.id,
                        episode.qty,
                        proceeds_per_token,
                        reason="resolution",
                        force_close=True,
                    )
            else:
                prices = resolution_prices.get(event.condition_id or "", {})
                for token_id in tokens:
                    episode = active.get(token_id)
                    if episode is None:
                        continue
                    proceeds = episode.qty * prices.get(token_id, _ZERO)
                    apply_remove(
                        token_id,
                        event.condition_id,
                        ts,
                        event.id,
                        episode.qty,
                        proceeds,
                        reason="resolution",
                        force_close=True,
                    )

        elif etype in ("REWARD", "MAKER_REBATE", "TAKER_REBATE"):
            if event.token_id and event.token_id in active:
                active[event.token_id].reward_income += Decimal(event.delta_usdc)
                active[event.token_id].consume(event.id)
                event_applications += 1

    for token_id in sorted(active):
        rows.append(_row(active[token_id], None, "open"))

    session.execute(text("DELETE FROM episodes WHERE wallet = :w"), {"w": wallet})
    for start in range(0, len(rows), 5000):
        session.execute(_INSERT_SQL, rows[start : start + 5000])
    session.commit()

    return EpisodesRebuildStats(
        wallet=wallet,
        events_processed=events_processed,
        episodes_written=len(rows),
        open_episodes=sum(1 for row in rows if row["close_reason"] == "open"),
        flat_closed_episodes=sum(1 for row in rows if row["close_reason"] == "flat"),
        resolution_closed_episodes=sum(1 for row in rows if row["close_reason"] == "resolution"),
        event_applications_consumed=event_applications,
        unmapped_condition_events=unmapped_condition_events,
        unmapped_condition_ids=len(unmapped_conditions),
        as_of_ts=max_ts,
    )


class EpisodesProjection(Projection):
    name = "episodes"
    version = EPISODES_PROJECTION_VERSION

    def __init__(self, dust_epsilon: Decimal = Decimal("0.000001")) -> None:
        self.dust_epsilon = dust_epsilon

    def rebuild(self, session: Session, wallet: str) -> EpisodesRebuildStats:
        return rebuild_episodes(session, wallet, dust_epsilon=self.dust_epsilon)


def fetch_episodes(
    session: Session,
    wallet: str,
    *,
    token_id: Optional[str] = None,
    open_only: bool = False,
) -> list:
    where = ["wallet = :wallet"]
    params = {"wallet": wallet.lower()}
    if token_id is not None:
        where.append("token_id = :token_id")
        params["token_id"] = token_id
    if open_only:
        where.append("close_reason = 'open'")
    rows = session.execute(
        text(
            "SELECT id, wallet, token_id, condition_id, open_ts, close_ts, close_reason, "
            "peak_qty, num_adds, num_partial_exits, wac_entry, realized_pnl, "
            "reward_income, fees_paid, events_consumed, projection_version "
            f"FROM episodes WHERE {' AND '.join(where)} ORDER BY open_ts, id"
        ),
        params,
    ).fetchall()
    return rows


def episode_stats(session: Session, wallet: str) -> EpisodeStats:
    rows = session.execute(
        text(
            "SELECT close_reason, open_ts, close_ts, realized_pnl, reward_income "
            "FROM episodes WHERE wallet = :wallet"
        ),
        {"wallet": wallet.lower()},
    ).fetchall()
    durations = [
        row.close_ts - row.open_ts
        for row in rows
        if row.close_ts is not None and row.close_ts >= row.open_ts
    ]
    durations_sorted = sorted(durations)
    p90 = None
    if durations_sorted:
        p90_index = int((len(durations_sorted) - 1) * Decimal("0.9"))
        p90 = float(durations_sorted[p90_index])
    micro_count = sum(1 for duration in durations if duration <= MICRO_EPISODE_SECONDS)
    count = len(rows)
    closed_count = len(durations)
    return EpisodeStats(
        wallet=wallet.lower(),
        count=count,
        open_count=sum(1 for row in rows if row.close_reason == "open"),
        flat_closed_count=sum(1 for row in rows if row.close_reason == "flat"),
        resolution_closed_count=sum(1 for row in rows if row.close_reason == "resolution"),
        duration_min=min(durations) if durations else None,
        duration_p50=float(median(durations)) if durations else None,
        duration_p90=p90,
        duration_max=max(durations) if durations else None,
        micro_episode_count=micro_count,
        micro_episode_share=(
            Decimal(micro_count) / Decimal(closed_count) if closed_count else _ZERO
        ),
        realized_pnl=sum((Decimal(row.realized_pnl) for row in rows), _ZERO),
        reward_income=sum((Decimal(row.reward_income) for row in rows), _ZERO),
    )
