"""Fingerprint registry, computation and persistence (Phase 13).

`compute_fingerprints` loads a wallet's projections (episodes, exposures,
daily_equity) plus ledger aggregates, slices them into (scope, window) bundles,
runs every registered feature over each bundle, and writes one `fingerprints`
row per (wallet, scope, feature, window, version) — a value or NULL-with-reason.

Scopes: "all" plus "category:<Label>" for every Gamma category the wallet
touched (unmatched tags bucket as "unknown", never dropped). Windows: "all"
(full history) and "90d" (the trailing 90 days of the wallet's *own* activity
timeline, i.e. relative to its latest event, so the value is reproducible).

No feature reads raw API data: every input is a projection or a ledger
aggregate. Exposure sums use float aggregation in SQL — acceptable because
bond_inventory_ratio is a bounded ratio averaged over days, precision-
insensitive; every other numeric is Decimal end-to-end.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .features import all_features
from .features.inputs import EpisodeRec, ExposureDayAgg, Feature, PnlRec, ScopeInput

FINGERPRINT_VERSION = 1
WINDOWS = ("all", "90d")
_WINDOW_90D_SECONDS = 90 * 24 * 3600
_ZERO = Decimal("0")


@dataclass(frozen=True)
class FingerprintStats:
    wallet: str
    scopes: int
    windows: int
    values_written: int
    null_written: int


@dataclass(frozen=True)
class FingerprintRow:
    wallet: str
    scope: str
    feature: str
    family: str
    value: Optional[str]
    value_type: Optional[str]
    null_reason: Optional[str]
    window: str
    computed_at: str
    version: int


# --- parsing helpers --------------------------------------------------------


def _decimal(value: object) -> Decimal:
    return Decimal(str(value if value not in (None, "") else 0))


def _parse_iso_ts(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    text_val = str(value).strip()
    if not text_val:
        return None
    try:
        if text_val.endswith("Z"):
            text_val = text_val[:-1] + "+00:00"
        dt = datetime.fromisoformat(text_val)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _utc_day_iso(ts: int) -> str:
    # timedelta arithmetic (not fromtimestamp) so negative/out-of-range epochs
    # from tiny test fixtures are handled on every platform, including Windows.
    return (_EPOCH + timedelta(seconds=int(ts))).date().isoformat()


# --- loaders (full history; windows filter these in memory) -----------------


def _load_episodes(session: Session, wallet: str) -> list[EpisodeRec]:
    rows = session.execute(
        text(
            "SELECT e.token_id, e.condition_id, COALESCE(m.category,'unknown') AS cat, "
            "e.open_ts, e.close_ts, e.close_reason, e.peak_qty, e.wac_entry, "
            "e.num_adds, e.num_partial_exits, e.realized_pnl, e.reward_income, "
            "m.start_date, m.resolution_prices_json "
            "FROM episodes e LEFT JOIN markets m ON m.condition_id = e.condition_id "
            "WHERE e.wallet = :w"
        ),
        {"w": wallet},
    )
    resolution_cache: dict[str, dict[str, Decimal]] = {}

    def token_price(condition_id: Optional[str], token_id: str, raw_json: Optional[str]) -> Optional[Decimal]:
        if not raw_json:
            return None
        key = condition_id or token_id
        prices = resolution_cache.get(key)
        if prices is None:
            try:
                payload = json.loads(raw_json)
            except json.JSONDecodeError:
                payload = {}
            prices = {str(k): _decimal(v) for k, v in payload.items()}
            resolution_cache[key] = prices
        return prices.get(str(token_id))

    episodes: list[EpisodeRec] = []
    for r in rows:
        episodes.append(
            EpisodeRec(
                token_id=r.token_id,
                condition_id=r.condition_id,
                category=r.cat or "unknown",
                open_ts=int(r.open_ts),
                close_ts=None if r.close_ts is None else int(r.close_ts),
                close_reason=r.close_reason,
                peak_qty=_decimal(r.peak_qty),
                wac_entry=_decimal(r.wac_entry),
                num_adds=int(r.num_adds),
                num_partial_exits=int(r.num_partial_exits),
                realized_pnl=_decimal(r.realized_pnl),
                reward_income=_decimal(r.reward_income),
                start_date_ts=_parse_iso_ts(r.start_date),
                resolution_price=token_price(r.condition_id, r.token_id, r.resolution_prices_json),
            )
        )
    return episodes


def _load_exposure_days(session: Session, wallet: str) -> list[tuple[str, str, Decimal, Decimal]]:
    """(date, category, bond_abs, directional_abs) rows, one per (date, cat)."""
    rows = session.execute(
        text(
            "SELECT ed.date AS date, COALESCE(m.category,'unknown') AS cat, "
            "COALESCE(SUM(ABS(CAST(NULLIF(ed.bond,'') AS REAL))),0) AS bond_abs, "
            "COALESCE(SUM(ABS(CAST(NULLIF(ed.directional,'') AS REAL))),0) AS dir_abs "
            "FROM exposures_daily ed LEFT JOIN markets m ON m.condition_id = ed.condition_id "
            "WHERE ed.wallet = :w GROUP BY ed.date, cat"
        ),
        {"w": wallet},
    )
    return [
        (r.date, r.cat or "unknown", _decimal(round(r.bond_abs, 6)), _decimal(round(r.dir_abs, 6)))
        for r in rows
    ]


def _load_daily_equity(session: Session, wallet: str) -> list[tuple[str, Decimal, Decimal]]:
    """(date, unrealized_pnl, stale_equity_share) rows ordered by date."""
    rows = session.execute(
        text(
            "SELECT date, unrealized_pnl, stale_equity_share FROM daily_equity "
            "WHERE wallet = :w ORDER BY date"
        ),
        {"w": wallet},
    )
    return [(r.date, _decimal(r.unrealized_pnl), _decimal(r.stale_equity_share)) for r in rows]


def _load_pnl(session: Session, wallet: str) -> dict[str, PnlRec]:
    """scope -> pnl_decomposition row. Full history only (the projection has no
    windowed variant), so it is used exclusively for the 'all' window."""
    rows = session.execute(
        text(
            "SELECT scope, directional_pnl, bond_merge_pnl, reward_income, "
            "redemption_pnl, fees FROM pnl_decomposition WHERE wallet = :w"
        ),
        {"w": wallet},
    )
    return {
        r.scope: PnlRec(
            directional=_decimal(r.directional_pnl),
            bond_merge=_decimal(r.bond_merge_pnl),
            reward_income=_decimal(r.reward_income),
            redemption=_decimal(r.redemption_pnl),
            fees=_decimal(r.fees),
        )
        for r in rows
    }


def _load_trade_aggregates(
    session: Session, wallet: str, cutoff_ts: Optional[int]
) -> dict[str, tuple[int, int, int, int]]:
    """category -> (total, maker, taker, enriched) TRADE counts."""
    clause = "" if cutoff_ts is None else " AND we.ts >= :cutoff"
    rows = session.execute(
        text(
            "SELECT COALESCE(m.category,'unknown') AS cat, COUNT(*) AS total, "
            "SUM(CASE WHEN fe.role='maker' THEN 1 ELSE 0 END) AS maker, "
            "SUM(CASE WHEN fe.role='taker' THEN 1 ELSE 0 END) AS taker, "
            "SUM(CASE WHEN fe.role IN ('maker','taker') THEN 1 ELSE 0 END) AS enriched "
            "FROM wallet_events we "
            "LEFT JOIN markets m ON m.condition_id = we.condition_id "
            "LEFT JOIN fill_enrichment fe ON fe.event_id = we.id "
            "WHERE we.wallet = :w AND we.event_type = 'TRADE'" + clause + " GROUP BY cat"
        ),
        {"w": wallet, "cutoff": cutoff_ts},
    )
    return {
        (r.cat or "unknown"): (int(r.total), int(r.maker or 0), int(r.taker or 0), int(r.enriched or 0))
        for r in rows
    }


def _load_cycle_aggregates(
    session: Session, wallet: str, cutoff_ts: Optional[int]
) -> dict[str, tuple[int, int]]:
    """category -> (merge_count, redeem_count)."""
    clause = "" if cutoff_ts is None else " AND ts >= :cutoff"
    rows = session.execute(
        text(
            "SELECT COALESCE(m.category,'unknown') AS cat, we.event_type AS etype, COUNT(*) AS c "
            "FROM wallet_events we LEFT JOIN markets m ON m.condition_id = we.condition_id "
            "WHERE we.wallet = :w AND we.event_type IN ('MERGE','REDEEM','REDEEM_PAYOUT')"
            + clause
            + " GROUP BY cat, etype"
        ),
        {"w": wallet, "cutoff": cutoff_ts},
    )
    out: dict[str, list[int]] = {}
    for r in rows:
        agg = out.setdefault(r.cat or "unknown", [0, 0])
        if r.etype == "MERGE":
            agg[0] += int(r.c)
        else:
            agg[1] += int(r.c)
    return {cat: (v[0], v[1]) for cat, v in out.items()}


def _load_active_days(
    session: Session, wallet: str, cutoff_ts: Optional[int]
) -> tuple[dict[str, int], int]:
    """(category -> active days, all-scope active days). A UTC day counts once
    per category it touched, and once overall."""
    clause = "" if cutoff_ts is None else " AND ts >= :cutoff"
    by_cat = {
        (r.cat or "unknown"): int(r.d)
        for r in session.execute(
            text(
                "SELECT COALESCE(m.category,'unknown') AS cat, "
                "COUNT(DISTINCT CAST(we.ts/86400 AS INTEGER)) AS d "
                "FROM wallet_events we LEFT JOIN markets m ON m.condition_id = we.condition_id "
                "WHERE we.wallet = :w" + clause + " GROUP BY cat"
            ),
            {"w": wallet, "cutoff": cutoff_ts},
        )
    }
    all_days = int(
        session.execute(
            text(
                "SELECT COUNT(DISTINCT CAST(ts/86400 AS INTEGER)) AS d "
                "FROM wallet_events WHERE wallet = :w" + clause
            ),
            {"w": wallet, "cutoff": cutoff_ts},
        ).scalar_one()
        or 0
    )
    return by_cat, all_days


# --- assembly ---------------------------------------------------------------


def _build_scope_inputs(
    wallet: str,
    window: str,
    cutoff_ts: Optional[int],
    episodes: list[EpisodeRec],
    exposure_days: list[tuple[str, str, Decimal, Decimal]],
    daily_equity: list[tuple[str, Decimal, Decimal]],
    trades: dict[str, tuple[int, int, int, int]],
    cycles: dict[str, tuple[int, int]],
    active_days_by_cat: dict[str, int],
    all_active_days: int,
    pnl_by_scope: dict[str, PnlRec],
) -> dict[str, ScopeInput]:
    cutoff_date = _utc_day_iso(cutoff_ts) if cutoff_ts is not None else None

    # Window-filter episodes by entry timestamp.
    if cutoff_ts is not None:
        episodes = [e for e in episodes if e.open_ts >= cutoff_ts]
        exposure_days = [d for d in exposure_days if d[0] >= cutoff_date]
        daily_equity = [d for d in daily_equity if d[0] >= cutoff_date]

    # Categories present across any source.
    categories = set()
    for e in episodes:
        categories.add(e.category)
    categories.update(d[1] for d in exposure_days)
    categories.update(trades.keys())
    categories.update(cycles.keys())
    categories.update(active_days_by_cat.keys())

    ep_by_cat: dict[str, list[EpisodeRec]] = {c: [] for c in categories}
    for e in episodes:
        ep_by_cat[e.category].append(e)
    category_episode_counts = {c: len(v) for c, v in ep_by_cat.items() if v}

    # Exposure day aggregates: per category, and combined for "all".
    exp_by_cat: dict[str, dict[str, list[Decimal]]] = {}
    exp_all: dict[str, list[Decimal]] = {}
    for date, cat, bond, dir_abs in exposure_days:
        cat_days = exp_by_cat.setdefault(cat, {})
        agg = cat_days.setdefault(date, [_ZERO, _ZERO])
        agg[0] += bond
        agg[1] += dir_abs
        agg_all = exp_all.setdefault(date, [_ZERO, _ZERO])
        agg_all[0] += bond
        agg_all[1] += dir_abs

    def exposure_list(days: dict[str, list[Decimal]]) -> list[ExposureDayAgg]:
        return [ExposureDayAgg(date=d, bond_abs=v[0], directional_abs=v[1]) for d, v in sorted(days.items())]

    latest_unrealized = daily_equity[-1][1] if daily_equity else None
    stale_shares = [row[2] for row in daily_equity]

    scopes: dict[str, ScopeInput] = {}

    # all-scope aggregates
    all_trades = _sum_trades(trades.values())
    all_cycles = _sum_cycles(cycles.values())
    scopes["all"] = ScopeInput(
        wallet=wallet,
        scope="all",
        window=window,
        episodes=episodes,
        exposure_days=exposure_list(exp_all),
        trade_total=all_trades[0],
        trade_maker=all_trades[1],
        trade_taker=all_trades[2],
        trade_enriched=all_trades[3],
        merge_count=all_cycles[0],
        redeem_count=all_cycles[1],
        active_days=all_active_days,
        pnl=pnl_by_scope.get("all"),
        latest_unrealized=latest_unrealized,
        stale_equity_shares=stale_shares,
        category_episode_counts=category_episode_counts,
    )

    for cat in sorted(categories):
        t = trades.get(cat, (0, 0, 0, 0))
        c = cycles.get(cat, (0, 0))
        scopes[f"category:{cat}"] = ScopeInput(
            wallet=wallet,
            scope=f"category:{cat}",
            window=window,
            episodes=ep_by_cat.get(cat, []),
            exposure_days=exposure_list(exp_by_cat.get(cat, {})),
            trade_total=t[0],
            trade_maker=t[1],
            trade_taker=t[2],
            trade_enriched=t[3],
            merge_count=c[0],
            redeem_count=c[1],
            active_days=active_days_by_cat.get(cat, 0),
            pnl=pnl_by_scope.get(f"category:{cat}"),
        )
    return scopes


def _sum_trades(values: Iterable[tuple[int, int, int, int]]) -> tuple[int, int, int, int]:
    total = maker = taker = enriched = 0
    for t, mk, tk, en in values:
        total += t
        maker += mk
        taker += tk
        enriched += en
    return total, maker, taker, enriched


def _sum_cycles(values: Iterable[tuple[int, int]]) -> tuple[int, int]:
    merge = redeem = 0
    for m, r in values:
        merge += m
        redeem += r
    return merge, redeem


# --- top level --------------------------------------------------------------


_INSERT_SQL = text(
    "INSERT INTO fingerprints "
    "(wallet, scope, feature, family, value, value_type, null_reason, window, "
    "computed_at, version) "
    "VALUES (:wallet, :scope, :feature, :family, :value, :value_type, :null_reason, "
    ":window, :computed_at, :version)"
)


def compute_fingerprints(
    session: Session,
    wallet: str,
    *,
    features: Optional[list[Feature]] = None,
    version: int = FINGERPRINT_VERSION,
) -> FingerprintStats:
    """Drop and rebuild all fingerprints for one wallet across scopes/windows."""
    wallet = wallet.lower()
    registry = features if features is not None else all_features()

    max_ts = session.execute(
        text("SELECT MAX(ts) FROM wallet_events WHERE wallet = :w"), {"w": wallet}
    ).scalar()

    episodes = _load_episodes(session, wallet)
    exposure_days = _load_exposure_days(session, wallet)
    daily_equity = _load_daily_equity(session, wallet)
    pnl_by_scope = _load_pnl(session, wallet)

    computed_at = datetime.now(timezone.utc).isoformat()
    rows: list[dict] = []
    values_written = 0
    null_written = 0
    scope_names: set[str] = set()

    for window in WINDOWS:
        if window == "all":
            cutoff_ts: Optional[int] = None
        else:
            if max_ts is None:
                continue
            cutoff_ts = int(max_ts) - _WINDOW_90D_SECONDS

        trades = _load_trade_aggregates(session, wallet, cutoff_ts)
        cycles = _load_cycle_aggregates(session, wallet, cutoff_ts)
        active_by_cat, all_active = _load_active_days(session, wallet, cutoff_ts)

        scope_inputs = _build_scope_inputs(
            wallet,
            window,
            cutoff_ts,
            episodes,
            exposure_days,
            daily_equity,
            trades,
            cycles,
            active_by_cat,
            all_active,
            pnl_by_scope if window == "all" else {},
        )

        for scope, inp in scope_inputs.items():
            scope_names.add(scope)
            for feature in registry:
                result = feature.fn(inp)
                if result.is_null:
                    null_written += 1
                    value = None
                    value_type = None
                else:
                    values_written += 1
                    if result.is_distribution:
                        value = json.dumps(result.value, separators=(",", ":"), sort_keys=True)
                        value_type = "json"
                    else:
                        value = str(result.value)
                        value_type = "scalar"
                rows.append(
                    {
                        "wallet": wallet,
                        "scope": scope,
                        "feature": feature.name,
                        "family": feature.family,
                        "value": value,
                        "value_type": value_type,
                        "null_reason": result.null_reason,
                        "window": window,
                        "computed_at": computed_at,
                        "version": version,
                    }
                )

    session.execute(text("DELETE FROM fingerprints WHERE wallet = :w"), {"w": wallet})
    if rows:
        session.execute(_INSERT_SQL, rows)
    session.commit()

    return FingerprintStats(
        wallet=wallet,
        scopes=len(scope_names),
        windows=len(WINDOWS),
        values_written=values_written,
        null_written=null_written,
    )


def fetch_fingerprints(
    session: Session,
    wallet: str,
    *,
    scope: Optional[str] = None,
    window: str = "all",
    version: Optional[int] = None,
) -> list[FingerprintRow]:
    where = ["wallet = :w", "window = :window"]
    params: dict = {"w": wallet.lower(), "window": window}
    if scope is not None:
        where.append("scope = :scope")
        params["scope"] = scope
    if version is None:
        where.append(
            "version = (SELECT MAX(version) FROM fingerprints WHERE wallet = :w)"
        )
    else:
        where.append("version = :version")
        params["version"] = version
    rows = session.execute(
        text(
            "SELECT wallet, scope, feature, family, value, value_type, null_reason, "
            "window, computed_at, version FROM fingerprints "
            f"WHERE {' AND '.join(where)} ORDER BY scope, family, feature"
        ),
        params,
    ).fetchall()
    return [
        FingerprintRow(
            wallet=r.wallet,
            scope=r.scope,
            feature=r.feature,
            family=r.family,
            value=r.value,
            value_type=r.value_type,
            null_reason=r.null_reason,
            window=r.window,
            computed_at=r.computed_at,
            version=int(r.version),
        )
        for r in rows
    ]


def fingerprint_scopes(session: Session, wallet: str, *, window: str = "all") -> list[str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT scope FROM fingerprints WHERE wallet = :w AND window = :window "
            "ORDER BY scope"
        ),
        {"w": wallet.lower(), "window": window},
    ).fetchall()
    return [r.scope for r in rows]
