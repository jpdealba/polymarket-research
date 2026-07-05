"""Read-only World Cup watch status/query helpers."""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..watchlists.world_cup import WatchlistToken, list_watchlist_tokens


@dataclass(frozen=True)
class WorldCupCollectorStatus:
    tables_exist: bool
    enabled: bool
    watchlist_name: str
    wallet: str
    tracked_wallets: tuple[str, ...]
    book_interval_s: int
    fast_book_interval_s: int
    sync_interval_s: int
    active_tokens: int
    last_sample_run_ts: int | None
    latest_book_ts: int | None
    latest_wallet_event_ts: int | None
    latest_context_ts: int | None

    @property
    def latest_book_age_s(self) -> int | None:
        if self.latest_book_ts is None:
            return None
        return max(0, int(time.time()) - int(self.latest_book_ts))


@dataclass(frozen=True)
class MakerFillContextRow:
    event_id: int
    wallet: str
    token_id: str
    condition_id: str | None
    trade_ts: int
    trade_utc: str
    side: str | None
    fill_price: str | None
    fill_size: str | None
    fill_shares: str | None
    fill_notional_usdc: str | None
    role: str | None
    book_before_age_s: int | None
    best_bid_before: str | None
    best_ask_before: str | None
    spread_before: str | None
    mid_before: str | None
    book_after_age_s: int | None
    best_bid_after: str | None
    best_ask_after: str | None
    context_status: str
    null_reason: str | None
    question: str | None = None
    outcome_label: str | None = None


@dataclass(frozen=True)
class WorldCupContextCoverage:
    total: int
    excellent: int
    good: int
    usable: int
    weak: int
    stale: int
    missing: int

    @property
    def strict_count(self) -> int:
        return self.excellent + self.good

    @property
    def loose_count(self) -> int:
        return self.excellent + self.good + self.usable

    @property
    def strict_share(self) -> Decimal:
        return Decimal(self.strict_count) / Decimal(self.total) if self.total else Decimal("0")

    @property
    def loose_share(self) -> Decimal:
        return Decimal(self.loose_count) / Decimal(self.total) if self.total else Decimal("0")


@dataclass(frozen=True)
class BookHistoryRow:
    token_id: str
    ts: int
    best_bid: str | None
    best_ask: str | None
    spread: str | None
    mid: str | None
    depth_top_json: str | None


@dataclass(frozen=True)
class WorldCupTrackedWallet:
    wallet: str
    display_name: str | None
    priority: int
    source: str
    selected_at: int
    is_active: int


def table_exists(session: Session, name: str) -> bool:
    return bool(
        session.execute(
            text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = :name"),
            {"name": name},
        ).fetchone()
    )


def phase18_tables_exist(session: Session) -> bool:
    return all(
        table_exists(session, name)
        for name in ("watchlists", "watchlist_tokens", "book_sample_runs", "maker_fill_context")
    )


def all_fill_context_table_exists(session: Session) -> bool:
    return table_exists(session, "all_fill_context")


def tracked_wallets_table_exists(session: Session) -> bool:
    return table_exists(session, "worldcup_tracked_wallets")


def worldcup_tracked_wallets(session: Session, settings: Settings | None = None) -> list[str]:
    wallets: list[str] = []
    if tracked_wallets_table_exists(session):
        rows = session.execute(
            text(
                "SELECT wallet FROM worldcup_tracked_wallets "
                "WHERE is_active = 1 ORDER BY priority, selected_at LIMIT 2"
            )
        ).fetchall()
        wallets = [row.wallet.lower() for row in rows]
    if not wallets and settings is not None and settings.worldcup_wallet:
        wallets = [settings.worldcup_wallet.lower()]
    return wallets[:2]


def worldcup_tracked_wallet_rows(session: Session) -> list[WorldCupTrackedWallet]:
    if not tracked_wallets_table_exists(session):
        return []
    rows = session.execute(
        text(
            "SELECT wallet, display_name, priority, source, selected_at, is_active "
            "FROM worldcup_tracked_wallets WHERE is_active = 1 "
            "ORDER BY priority, selected_at LIMIT 2"
        )
    ).fetchall()
    return [WorldCupTrackedWallet(**dict(row._mapping)) for row in rows]


def set_worldcup_tracked_wallets(
    session: Session,
    wallets: list[str] | tuple[str, ...],
    *,
    source: str = "dashboard",
) -> list[str]:
    if not tracked_wallets_table_exists(session):
        raise RuntimeError("worldcup_tracked_wallets table is missing; run `pmr db upgrade`")
    normalized: list[str] = []
    for wallet in wallets:
        w = wallet.lower().strip()
        if not w or w in normalized:
            continue
        normalized.append(w)
    if len(normalized) > 2:
        raise ValueError("World Cup watch supports at most 2 tracked wallets")

    now = int(time.time())
    session.execute(text("UPDATE worldcup_tracked_wallets SET is_active = 0"))
    for priority, wallet in enumerate(normalized, start=1):
        display = session.execute(
            text("SELECT display_name FROM wallets WHERE address = :wallet"),
            {"wallet": wallet},
        ).scalar()
        session.execute(
            text(
                "INSERT INTO worldcup_tracked_wallets "
                "(wallet, display_name, priority, source, selected_at, is_active) "
                "VALUES (:wallet, :display_name, :priority, :source, :selected_at, 1) "
                "ON CONFLICT(wallet) DO UPDATE SET "
                "display_name = excluded.display_name, "
                "priority = excluded.priority, source = excluded.source, "
                "selected_at = excluded.selected_at, is_active = 1"
            ),
            {
                "wallet": wallet,
                "display_name": display,
                "priority": priority,
                "source": source,
                "selected_at": now,
            },
        )
    session.commit()
    return normalized


def worldcup_collector_status(
    session: Session,
    settings: Settings,
) -> WorldCupCollectorStatus:
    exists = phase18_tables_exist(session)
    if not exists:
        tracked = tuple(worldcup_tracked_wallets(session, settings))
        return WorldCupCollectorStatus(
            tables_exist=False,
            enabled=settings.worldcup_watch_enabled,
            watchlist_name=settings.worldcup_watchlist_name,
            wallet=settings.worldcup_wallet,
            tracked_wallets=tracked,
            book_interval_s=settings.worldcup_book_interval_s,
            fast_book_interval_s=settings.worldcup_fast_book_interval_s,
            sync_interval_s=settings.worldcup_sync_interval_s,
            active_tokens=0,
            last_sample_run_ts=None,
            latest_book_ts=None,
            latest_wallet_event_ts=None,
            latest_context_ts=None,
        )

    active_tokens = session.execute(
        text(
            "SELECT COUNT(*) FROM watchlist_tokens wt "
            "JOIN watchlists w ON w.id = wt.watchlist_id "
            "WHERE w.name = :name AND wt.is_active = 1"
        ),
        {"name": settings.worldcup_watchlist_name},
    ).scalar() or 0
    last_run = session.execute(
        text(
            "SELECT MAX(started_at) FROM book_sample_runs bsr "
            "JOIN watchlists w ON w.id = bsr.watchlist_id WHERE w.name = :name"
        ),
        {"name": settings.worldcup_watchlist_name},
    ).scalar()
    latest_book = session.execute(
        text(
            "SELECT MAX(bs.ts) FROM book_snapshots bs "
            "JOIN watchlists w ON w.id = bs.watchlist_id WHERE w.name = :name"
        ),
        {"name": settings.worldcup_watchlist_name},
    ).scalar()
    tracked = tuple(worldcup_tracked_wallets(session, settings))
    latest_wallet = None
    latest_wallets = tracked or ((settings.worldcup_wallet,) if settings.worldcup_wallet else ())
    if latest_wallets:
        placeholders = ", ".join(f":w{i}" for i, _ in enumerate(latest_wallets))
        params = {f"w{i}": wallet for i, wallet in enumerate(latest_wallets)}
        latest_wallet = session.execute(
            text(f"SELECT MAX(ts) FROM wallet_events WHERE wallet IN ({placeholders})"),
            params,
        ).scalar()
    latest_context = session.execute(
        text("SELECT MAX(updated_at) FROM maker_fill_context")
    ).scalar()
    if all_fill_context_table_exists(session):
        latest_all_context = session.execute(
            text("SELECT MAX(updated_at) FROM all_fill_context")
        ).scalar()
        if latest_all_context is not None:
            latest_context = max(int(latest_context or 0), int(latest_all_context))
    return WorldCupCollectorStatus(
        tables_exist=True,
        enabled=settings.worldcup_watch_enabled,
        watchlist_name=settings.worldcup_watchlist_name,
        wallet=settings.worldcup_wallet,
        tracked_wallets=tracked,
        book_interval_s=settings.worldcup_book_interval_s,
        fast_book_interval_s=settings.worldcup_fast_book_interval_s,
        sync_interval_s=settings.worldcup_sync_interval_s,
        active_tokens=int(active_tokens),
        last_sample_run_ts=last_run,
        latest_book_ts=latest_book,
        latest_wallet_event_ts=latest_wallet,
        latest_context_ts=latest_context,
    )


def worldcup_watchlist_tokens(
    session: Session,
    *,
    name: str,
    active_only: bool = True,
) -> list[WatchlistToken]:
    if not phase18_tables_exist(session):
        return []
    return list_watchlist_tokens(session, name=name, active_only=active_only)


def worldcup_recent_maker_fills(
    session: Session,
    *,
    wallet: str,
    watchlist: str,
    limit: int = 100,
    role: str | None = None,
) -> list[MakerFillContextRow]:
    if not phase18_tables_exist(session):
        return []
    query = (
        "SELECT mfc.event_id, mfc.wallet, mfc.token_id, mfc.condition_id, "
        "mfc.trade_ts, mfc.trade_utc, mfc.side, mfc.fill_price, "
        "mfc.fill_size, mfc.fill_shares, mfc.fill_notional_usdc, "
        "mfc.role, mfc.book_before_age_s, "
        "mfc.best_bid_before, mfc.best_ask_before, "
        "mfc.spread_before, mfc.mid_before, "
        "mfc.book_after_age_s, mfc.best_bid_after, mfc.best_ask_after, "
        "mfc.context_status, "
        "mfc.null_reason, wt.question, wt.outcome_label "
        "FROM maker_fill_context mfc "
        "JOIN watchlists w ON w.name = :watchlist "
        "LEFT JOIN watchlist_tokens wt ON wt.watchlist_id = w.id "
        "AND wt.token_id = mfc.token_id "
        "WHERE mfc.wallet = :wallet "
    )
    params: dict[str, object] = {"wallet": wallet.lower(), "watchlist": watchlist, "limit": limit}
    if role is not None:
        query += "AND mfc.role = :role "
        params["role"] = role
    query += "ORDER BY mfc.trade_ts DESC, mfc.event_id DESC LIMIT :limit"
    rows = session.execute(text(query), params).fetchall()
    return [MakerFillContextRow(**dict(row._mapping)) for row in rows]


def worldcup_recent_all_fills(
    session: Session,
    *,
    wallet: str,
    watchlist: str,
    limit: int = 100,
    role: str | None = None,
) -> list[MakerFillContextRow]:
    if not phase18_tables_exist(session) or not all_fill_context_table_exists(session):
        return []
    query = (
        "SELECT afc.event_id, afc.wallet, afc.token_id, afc.condition_id, "
        "afc.trade_ts, afc.trade_utc, afc.side, afc.fill_price, "
        "afc.fill_size, afc.fill_shares, afc.fill_notional_usdc, "
        "afc.role, afc.book_before_age_s, "
        "afc.best_bid_before, afc.best_ask_before, "
        "afc.spread_before, afc.mid_before, "
        "afc.book_after_age_s, afc.best_bid_after, afc.best_ask_after, "
        "afc.context_status, "
        "afc.null_reason, wt.question, wt.outcome_label "
        "FROM all_fill_context afc "
        "JOIN watchlists w ON w.name = :watchlist "
        "JOIN watchlist_tokens wt ON wt.watchlist_id = w.id "
        "AND wt.token_id = afc.token_id AND wt.is_active = 1 "
        "WHERE afc.wallet = :wallet "
    )
    params: dict[str, object] = {"wallet": wallet.lower(), "watchlist": watchlist, "limit": limit}
    if role is not None:
        query += "AND afc.role = :role "
        params["role"] = role
    query += "ORDER BY afc.trade_ts DESC, afc.event_id DESC LIMIT :limit"
    rows = session.execute(text(query), params).fetchall()
    return [
        MakerFillContextRow(
            **{**dict(row._mapping), "role": row.role if row.role is not None else "UNKNOWN"}
        )
        for row in rows
    ]


def worldcup_context_coverage(
    session: Session,
    *,
    wallet: str,
    role: str | None = None,
) -> WorldCupContextCoverage:
    if not phase18_tables_exist(session):
        return WorldCupContextCoverage(0, 0, 0, 0, 0, 0, 0)
    query = "SELECT context_status, COUNT(*) AS cnt FROM maker_fill_context WHERE wallet = :wallet "
    params: dict[str, object] = {"wallet": wallet.lower()}
    if role is not None:
        query += "AND role = :role "
        params["role"] = role
    query += "GROUP BY context_status"
    rows = session.execute(text(query), params).fetchall()
    counts = {row.context_status: int(row.cnt) for row in rows}
    return WorldCupContextCoverage(
        total=sum(counts.values()),
        excellent=counts.get("excellent", 0),
        good=counts.get("good", 0),
        usable=counts.get("usable", 0),
        weak=counts.get("weak", 0),
        stale=counts.get("stale", 0),
        missing=counts.get("missing", 0),
    )


def worldcup_all_context_coverage(
    session: Session,
    *,
    wallet: str,
    watchlist: str,
    role: str | None = None,
) -> WorldCupContextCoverage:
    if not phase18_tables_exist(session) or not all_fill_context_table_exists(session):
        return WorldCupContextCoverage(0, 0, 0, 0, 0, 0, 0)
    query = (
        "SELECT afc.context_status, COUNT(*) AS cnt "
        "FROM all_fill_context afc "
        "JOIN watchlists w ON w.name = :watchlist "
        "JOIN watchlist_tokens wt ON wt.watchlist_id = w.id "
        "AND wt.token_id = afc.token_id AND wt.is_active = 1 "
        "WHERE afc.wallet = :wallet "
    )
    params: dict[str, object] = {"wallet": wallet.lower(), "watchlist": watchlist}
    if role is not None:
        query += "AND afc.role = :role "
        params["role"] = role
    query += "GROUP BY afc.context_status"
    rows = session.execute(text(query), params).fetchall()
    counts = {row.context_status: int(row.cnt) for row in rows}
    return WorldCupContextCoverage(
        total=sum(counts.values()),
        excellent=counts.get("excellent", 0),
        good=counts.get("good", 0),
        usable=counts.get("usable", 0),
        weak=counts.get("weak", 0),
        stale=counts.get("stale", 0),
        missing=counts.get("missing", 0),
    )


def worldcup_book_history(session: Session, *, token_id: str, limit: int = 200) -> list[BookHistoryRow]:
    rows = session.execute(
        text(
            "SELECT token_id, ts, best_bid, best_ask, spread, mid, depth_top_json "
            "FROM book_snapshots WHERE token_id = :token_id "
            "ORDER BY ts DESC LIMIT :limit"
        ),
        {"token_id": token_id, "limit": limit},
    ).fetchall()
    return [BookHistoryRow(**dict(row._mapping)) for row in rows]
