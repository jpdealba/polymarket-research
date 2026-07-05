"""pmr cluster: Phase 19 wallet cluster comparison and candidate ranking
(read-only, no ledger/projection writes -- ADR 0006)."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..reports.cluster import (
    analyze_cluster_candidates,
    analyze_cluster_compare,
    write_candidates_csv,
    write_cluster_report,
    write_compare_report,
)

RN1_DEFAULT = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"


@click.group("cluster")
def cluster_group() -> None:
    """Wallet cluster comparison and candidate ranking (Phase 19, read-only)."""


@cluster_group.command("compare")
@click.option("--leader", "leader", default=RN1_DEFAULT, show_default=True)
@click.option("--wallet", "wallet", required=True)
@click.option("--window-s", "window_s", type=int, default=300, show_default=True)
@click.option("--out", "out", type=click.Path(path_type=Path), default=None)
def compare(leader: str, wallet: str, window_s: int, out: Path | None) -> None:
    """Compare a candidate wallet's trades against the leader's within a time window."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        result = analyze_cluster_compare(session, leader, wallet, window_s=window_s)
    finally:
        session.close()

    if out is None:
        out = Path("docs/evidence/cluster") / wallet.lower()[:10]
    paths = write_compare_report(result, out)

    s = result.summary
    click.echo(f"\nLeader: {leader}")
    click.echo(f"Candidate: {wallet}")
    click.echo(f"Window: +/-{window_s}s")
    click.echo(
        f"Matched: {s['matched_trade_count']}/{s['total_candidate_trades']} candidate trades"
    )
    click.echo(f"  match_rate_same_token: {s['match_rate_same_token']*100:.1f}%")
    click.echo(f"  match_rate_same_question: {s['match_rate_same_question']*100:.1f}%")
    click.echo(f"  same_side_match_rate: {s['same_side_match_rate']*100:.1f}%")
    click.echo(f"  median_delay_seconds: {s['median_delay_seconds']}")
    click.echo(
        f"  rn1_first_share: {s['rn1_first_share']*100:.1f}%  "
        f"candidate_first_share: {s['candidate_first_share']*100:.1f}%"
    )
    click.echo(f"  same_event_share: {s['same_event_share']*100:.1f}%")
    click.echo(f"\nClassification: {result.classification}")
    click.echo("\n== Files written ==")
    for p in paths:
        click.echo(f"  {p}")


@cluster_group.command("report")
@click.option("--leader", "leader", default=RN1_DEFAULT, show_default=True)
@click.option(
    "--wallets", "wallets", required=True, help="Comma-separated candidate wallet addresses."
)
@click.option("--window-s", "window_s", type=int, default=300, show_default=True)
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    default=Path("docs/evidence/cluster/rn1_cluster.md"),
    show_default=True,
)
def report(leader: str, wallets: str, window_s: int, out: Path) -> None:
    """Compare multiple candidate wallets against the leader and write one markdown report."""
    wallet_list = [w.strip() for w in wallets.split(",") if w.strip()]
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        results = [
            analyze_cluster_compare(session, leader, w, window_s=window_s) for w in wallet_list
        ]
    finally:
        session.close()

    path = write_cluster_report(results, out)

    click.echo(f"\nLeader: {leader}")
    for r in results:
        click.echo(
            f"  {r.wallet}: {r.classification} "
            f"({r.summary['matched_trade_count']}/{r.summary['total_candidate_trades']} matched)"
        )
    click.echo(f"\nWritten: {path}")


@cluster_group.command("candidates")
@click.option("--leader", "leader", default=RN1_DEFAULT, show_default=True)
@click.option(
    "--wallets",
    "wallets",
    default=None,
    help="Comma-separated candidate wallets. Defaults to the active watchlist minus --leader.",
)
@click.option("--window-s", "window_s", type=int, default=300, show_default=True)
@click.option(
    "--out",
    "out",
    type=click.Path(path_type=Path),
    default=Path("docs/evidence/cluster/wallet_candidates_ranked.csv"),
    show_default=True,
)
def candidates(leader: str, wallets: str | None, window_s: int, out: Path) -> None:
    """Rank candidate wallets by overlap with the leader's trading."""
    wallet_list = [w.strip() for w in wallets.split(",") if w.strip()] if wallets else None
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        scores = analyze_cluster_candidates(
            session, leader, wallets=wallet_list, window_s=window_s
        )
    finally:
        session.close()

    path = write_candidates_csv(scores, out)

    click.echo(f"\nLeader: {leader}")
    click.echo(f"Candidates ranked: {len(scores)}")
    for s in scores[:10]:
        click.echo(
            f"  {s.wallet} [{s.classification}] priority={s.research_priority:.3f} "
            f"overlap={s.overlap_score*100:.1f}%"
        )
    click.echo(f"\nWritten: {path}")
