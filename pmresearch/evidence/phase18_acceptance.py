"""Phase 18 acceptance audit — mechanical close-out of the Forward
Microstructure Watch.

Checks the Phase 18 acceptance criteria (see
docs/plan/phase18_to_24_rn1_microstructure_research_plan.md) against the live
DB and emits the three required Phase 18 outputs:

    context_coverage_report.md
    fill_context_table.csv
    watchlist_freshness_report.csv

Read-only: it never mutates ledger, watchlist, book or context state.
"""

from __future__ import annotations

import ast
import csv
import time
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..worldcup.status import (
    phase18_tables_exist,
    worldcup_tracked_wallets,
)
from ..watchlists.world_cup import list_watchlist_tokens


@dataclass(frozen=True)
class Phase18Check:
    key: str
    title: str
    status: str  # "pass", "fail", "warn"
    evidence: str


@dataclass(frozen=True)
class WalletCoverage:
    wallet: str
    label: str | None
    total: int
    excellent: int
    good: int
    usable: int
    weak: int
    stale: int
    missing: int

    @property
    def strict(self) -> int:
        return self.excellent + self.good

    @property
    def loose(self) -> int:
        return self.excellent + self.good + self.usable

    @property
    def strict_share(self) -> Decimal:
        return Decimal(self.strict) / Decimal(self.total) if self.total else Decimal(0)

    @property
    def loose_share(self) -> Decimal:
        return Decimal(self.loose) / Decimal(self.total) if self.total else Decimal(0)


@dataclass(frozen=True)
class Phase18Acceptance:
    watchlist_name: str
    checks: tuple[Phase18Check, ...]
    coverage: tuple[WalletCoverage, ...]
    generated_at: int

    @property
    def passed(self) -> int:
        return sum(1 for c in self.checks if c.status == "pass")

    @property
    def all_pass(self) -> bool:
        return all(c.status != "fail" for c in self.checks)


def _wallet_label(session: Session, wallet: str) -> str | None:
    return session.execute(
        text("SELECT display_name FROM wallets WHERE address = :w"),
        {"w": wallet.lower()},
    ).scalar()


def _watchlist_id(session: Session, name: str) -> int | None:
    row = session.execute(
        text("SELECT id FROM watchlists WHERE name = :name"), {"name": name}
    ).fetchone()
    return int(row.id) if row is not None else None


# --- individual checks -------------------------------------------------------


def check_watch_pipeline(session: Session, name: str) -> Phase18Check:
    """`pmr worldcup tick` runs end-to-end: sample runs complete."""
    wid = _watchlist_id(session, name)
    if wid is None:
        return Phase18Check(
            "watch_tick", "watch tick runs end-to-end", "fail",
            f"watchlist '{name}' does not exist",
        )
    total, finished, last = session.execute(
        text(
            "SELECT COUNT(*), "
            "SUM(CASE WHEN finished_at IS NOT NULL THEN 1 ELSE 0 END), "
            "MAX(started_at) "
            "FROM book_sample_runs WHERE watchlist_id = :wid"
        ),
        {"wid": wid},
    ).fetchone()
    total = int(total or 0)
    finished = int(finished or 0)
    if finished == 0:
        return Phase18Check(
            "watch_tick", "watch tick runs end-to-end", "fail",
            f"{total} sample runs, none finished",
        )
    age = int(time.time()) - int(last) if last else None
    return Phase18Check(
        "watch_tick", "watch tick runs end-to-end", "pass",
        f"{finished}/{total} sample runs finished; last run {age}s ago",
    )


def check_continuous_run(session: Session, name: str) -> Phase18Check:
    """`pmr worldcup watch` sustains the loop: runs span time."""
    wid = _watchlist_id(session, name)
    if wid is None:
        return Phase18Check(
            "watch_run", "watch run sustains loop", "fail",
            f"watchlist '{name}' does not exist",
        )
    day_ago = int(time.time()) - 86400
    runs_24h, span_min, span_max = session.execute(
        text(
            "SELECT COUNT(*), MIN(started_at), MAX(started_at) "
            "FROM book_sample_runs WHERE watchlist_id = :wid AND started_at >= :cut"
        ),
        {"wid": wid, "cut": day_ago},
    ).fetchone()
    runs_24h = int(runs_24h or 0)
    if runs_24h < 2:
        return Phase18Check(
            "watch_run", "watch run sustains loop", "warn",
            f"only {runs_24h} sample runs in last 24h — loop may not be running",
        )
    span_h = (int(span_max) - int(span_min)) / 3600 if span_min and span_max else 0
    return Phase18Check(
        "watch_run", "watch run sustains loop", "pass",
        f"{runs_24h} sample runs over {span_h:.1f}h in last 24h",
    )


def check_watchlist_tokens(session: Session, name: str) -> Phase18Check:
    wid = _watchlist_id(session, name)
    active = session.execute(
        text(
            "SELECT COUNT(*) FROM watchlist_tokens "
            "WHERE watchlist_id = :wid AND is_active = 1"
        ),
        {"wid": wid},
    ).scalar() or 0
    status = "pass" if active > 0 else "fail"
    return Phase18Check(
        "watchlist_tokens", "watchlist_tokens has active tokens", status,
        f"{int(active)} active tokens in '{name}'",
    )


def check_book_snapshots_linked(session: Session, name: str) -> Phase18Check:
    """book_snapshots are generated and tied to sample runs."""
    wid = _watchlist_id(session, name)
    linked = session.execute(
        text(
            "SELECT COUNT(*) FROM book_snapshots bs "
            "JOIN book_sample_runs r ON r.id = bs.sample_run_id "
            "WHERE r.watchlist_id = :wid"
        ),
        {"wid": wid},
    ).scalar() or 0
    orphan = session.execute(
        text(
            "SELECT COUNT(*) FROM book_snapshots "
            "WHERE watchlist_id = :wid AND sample_run_id IS NULL"
        ),
        {"wid": wid},
    ).scalar() or 0
    if linked == 0:
        return Phase18Check(
            "books_linked", "book_snapshots linked to sample runs", "fail",
            "no book_snapshots joined to a sample run",
        )
    return Phase18Check(
        "books_linked", "book_snapshots linked to sample runs", "pass",
        f"{int(linked)} snapshots linked to sample runs ({int(orphan)} unlinked)",
    )


def check_context_classification(session: Session, wallets: list[str]) -> Phase18Check:
    """fill context classifies fills across freshness buckets."""
    if not wallets:
        return Phase18Check(
            "context_freshness", "fill context classifies by freshness", "fail",
            "no tracked wallets selected",
        )
    parts = []
    ok = False
    for w in wallets:
        rows = session.execute(
            text(
                "SELECT context_status, COUNT(*) FROM maker_fill_context "
                "WHERE wallet = :w GROUP BY context_status"
            ),
            {"w": w},
        ).fetchall()
        buckets = {r.context_status: int(r[1]) for r in rows}
        total = sum(buckets.values())
        if total > 0 and len(buckets) >= 2:
            ok = True
        parts.append(f"{w[:10]}…: {total} fills across {len(buckets)} buckets")
    status = "pass" if ok else "fail"
    return Phase18Check(
        "context_freshness", "fill context classifies by freshness", status,
        "; ".join(parts),
    )


def check_dashboard(session: Session) -> Phase18Check:
    """Dashboard surfaces status/watchlist/books/context via pmresearch.api only."""
    page = (
        Path(__file__).resolve().parents[2]
        / "apps" / "dashboard" / "pages" / "11_World_Cup_Watch.py"
    )
    if not page.exists():
        return Phase18Check(
            "dashboard", "dashboard surfaces phase 18 state", "fail",
            "apps/dashboard/pages/11_World_Cup_Watch.py missing",
        )
    tree = ast.parse(page.read_text(encoding="utf-8"))
    violations = [
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        and node.module
        and node.module.startswith("pmresearch.")
        and node.module != "pmresearch.api"
    ]
    if violations:
        return Phase18Check(
            "dashboard", "dashboard surfaces phase 18 state", "fail",
            f"dashboard bypasses api: imports {', '.join(violations[:3])}",
        )
    # The api must actually re-export the four surfaces the page needs.
    from .. import api

    required = (
        "worldcup_collector_status",
        "worldcup_watchlist_tokens",
        "worldcup_book_history",
        "worldcup_recent_maker_fills",
        "worldcup_context_coverage",
    )
    missing = [fn for fn in required if not hasattr(api, fn)]
    if missing:
        return Phase18Check(
            "dashboard", "dashboard surfaces phase 18 state", "fail",
            f"pmresearch.api missing: {', '.join(missing)}",
        )
    return Phase18Check(
        "dashboard", "dashboard surfaces phase 18 state", "pass",
        "page 11 renders status/watchlist/books/context via pmresearch.api only",
    )


def check_no_historical_claims(session: Session) -> Phase18Check:
    """The core Phase 18 invariant: a book labelled 'before' the fill must
    actually predate the fill, and a book labelled 'after' must follow it.
    Any violation would mean we captured the book after the fill and passed
    it off as pre-fill evidence."""
    before_viol = session.execute(
        text(
            "SELECT COUNT(*) FROM maker_fill_context "
            "WHERE book_before_ts IS NOT NULL AND book_before_ts > trade_ts"
        )
    ).scalar() or 0
    after_viol = session.execute(
        text(
            "SELECT COUNT(*) FROM maker_fill_context "
            "WHERE book_after_ts IS NOT NULL AND book_after_ts < trade_ts"
        )
    ).scalar() or 0
    # Fresh-labelled rows (excellent/good/usable/weak) must have a real
    # pre-fill book; only stale/missing may lack one.
    fresh_without_before = session.execute(
        text(
            "SELECT COUNT(*) FROM maker_fill_context "
            "WHERE context_status IN ('excellent','good','usable','weak') "
            "AND book_before_ts IS NULL"
        )
    ).scalar() or 0
    total_viol = int(before_viol) + int(after_viol) + int(fresh_without_before)
    if total_viol == 0:
        return Phase18Check(
            "no_historical_claims", "no post-fill book labelled as pre-fill", "pass",
            "0 before/after ordering violations across all fill contexts",
        )
    return Phase18Check(
        "no_historical_claims", "no post-fill book labelled as pre-fill", "fail",
        f"{int(before_viol)} before-after, {int(after_viol)} after-before, "
        f"{int(fresh_without_before)} fresh-without-before",
    )


def _wallet_coverage(session: Session, wallet: str, label: str | None) -> WalletCoverage:
    rows = session.execute(
        text(
            "SELECT context_status, COUNT(*) FROM maker_fill_context "
            "WHERE wallet = :w GROUP BY context_status"
        ),
        {"w": wallet},
    ).fetchall()
    c = {r.context_status: int(r[1]) for r in rows}
    return WalletCoverage(
        wallet=wallet,
        label=label,
        total=sum(c.values()),
        excellent=c.get("excellent", 0),
        good=c.get("good", 0),
        usable=c.get("usable", 0),
        weak=c.get("weak", 0),
        stale=c.get("stale", 0),
        missing=c.get("missing", 0),
    )


def analyze_phase18(session: Session, settings: Settings) -> Phase18Acceptance:
    name = settings.worldcup_watchlist_name
    if not phase18_tables_exist(session):
        return Phase18Acceptance(
            watchlist_name=name,
            checks=(
                Phase18Check(
                    "tables", "phase 18 tables exist", "fail",
                    "watchlists/watchlist_tokens/book_sample_runs/maker_fill_context missing",
                ),
            ),
            coverage=(),
            generated_at=int(time.time()),
        )
    wallets = worldcup_tracked_wallets(session, settings)
    checks = (
        check_watch_pipeline(session, name),
        check_continuous_run(session, name),
        check_watchlist_tokens(session, name),
        check_book_snapshots_linked(session, name),
        check_context_classification(session, wallets),
        check_dashboard(session),
        check_no_historical_claims(session),
    )
    coverage = tuple(
        _wallet_coverage(session, w, _wallet_label(session, w)) for w in wallets
    )
    return Phase18Acceptance(
        watchlist_name=name,
        checks=checks,
        coverage=coverage,
        generated_at=int(time.time()),
    )


# --- outputs -----------------------------------------------------------------


def _fmt_pct(d: Decimal) -> str:
    return f"{d * 100:.1f}%"


def write_reports(
    session: Session,
    settings: Settings,
    acc: Phase18Acceptance,
    out: Path,
) -> list[Path]:
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []

    # 1. phase18_acceptance.md — the checks + exit-question verdict
    md = out / "phase18_acceptance.md"
    lines = ["# Phase 18 — Forward Microstructure Watch — Acceptance\n"]
    lines.append(f"Watchlist: `{acc.watchlist_name}`  ")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(acc.generated_at))}\n")
    lines.append(f"**{acc.passed}/{len(acc.checks)} checks passed"
                 f"{' — no failures' if acc.all_pass else ' — FAILURES present'}.**\n")
    lines.append("| Check | Status | Evidence |")
    lines.append("|---|---|---|")
    for c in acc.checks:
        icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[c.status]
        lines.append(f"| {c.title} | {icon} | {c.evidence} |")
    lines.append("\n## Book-before-fill coverage (exit question)\n")
    lines.append("| Wallet | Fills | strict (exc+good) | loose (+usable) | strict share | loose share |")
    lines.append("|---|---|---|---|---|---|")
    for cov in acc.coverage:
        label = cov.label or cov.wallet[:10] + "…"
        lines.append(
            f"| {label} | {cov.total} | {cov.strict} | {cov.loose} | "
            f"{_fmt_pct(cov.strict_share)} | {_fmt_pct(cov.loose_share)} |"
        )
    lines.append("")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths.append(md)

    # 2. context_coverage_report.md — per-wallet freshness buckets
    cov_md = out / "context_coverage_report.md"
    clines = ["# Phase 18 — Context Coverage Report\n"]
    clines.append("| Wallet | total | excellent | good | usable | weak | stale | missing |")
    clines.append("|---|---|---|---|---|---|---|---|")
    for cov in acc.coverage:
        label = cov.label or cov.wallet[:10] + "…"
        clines.append(
            f"| {label} | {cov.total} | {cov.excellent} | {cov.good} | "
            f"{cov.usable} | {cov.weak} | {cov.stale} | {cov.missing} |"
        )
    cov_md.write_text("\n".join(clines) + "\n", encoding="utf-8")
    paths.append(cov_md)

    # 3. fill_context_table.csv — the Phase 18 minimal research table
    fc = out / "fill_context_table.csv"
    rows = session.execute(
        text(
            "SELECT mfc.trade_utc, mfc.wallet, w.display_name AS wallet_label, "
            "mfc.token_id, mfc.condition_id, wt.question, wt.outcome_label, "
            "mfc.side, mfc.role, mfc.fill_price, mfc.fill_size, "
            "mfc.book_before_age_s, mfc.best_bid_before, mfc.best_ask_before, "
            "mfc.mid_before, mfc.spread_before, mfc.book_after_age_s, "
            "mfc.best_bid_after, mfc.best_ask_after, mfc.context_status, mfc.null_reason "
            "FROM maker_fill_context mfc "
            "LEFT JOIN wallets w ON w.address = mfc.wallet "
            "LEFT JOIN watchlists wl ON wl.name = :name "
            "LEFT JOIN watchlist_tokens wt "
            "  ON wt.watchlist_id = wl.id AND wt.token_id = mfc.token_id "
            "ORDER BY mfc.trade_ts DESC"
        ),
        {"name": acc.watchlist_name},
    ).fetchall()
    with fc.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        cols = [
            "trade_utc", "wallet", "wallet_label", "token_id", "condition_id",
            "question", "outcome_label", "side", "role", "fill_price", "fill_size",
            "book_before_age_s", "best_bid_before", "best_ask_before", "mid_before",
            "spread_before", "book_after_age_s", "best_bid_after", "best_ask_after",
            "context_status", "null_reason",
        ]
        writer.writerow(cols)
        for r in rows:
            writer.writerow([r._mapping[c] for c in cols])
    paths.append(fc)

    # 4. watchlist_freshness_report.csv — per active token, latest book age
    fr = out / "watchlist_freshness_report.csv"
    tokens = list_watchlist_tokens(session, name=acc.watchlist_name, active_only=True)
    now = int(time.time())
    with fr.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "token_id", "question", "outcome_label", "priority", "source",
            "latest_book_ts", "latest_book_age_s", "latest_mid", "last_seen_ts",
        ])
        for t in tokens:
            age = now - int(t.latest_book_ts) if t.latest_book_ts else None
            writer.writerow([
                t.token_id, t.question, t.outcome_label, t.priority, t.source,
                t.latest_book_ts, age, t.latest_mid, t.last_seen_ts,
            ])
    paths.append(fr)

    return paths
