"""pmr derive / pmr pnl - Phase 8 derived payouts and PnL decomposition."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.derived import derive_redeem_payouts
from ..ledger.replay import ledger_wallets
from ..projections.episodes import rebuild_episodes
from ..projections.pnl_decomposition import (
    fetch_pnl_decomposition,
    rebuild_pnl_decomposition,
)
from ..reconcile.checks import decimal_string


@click.group("derive")
def derive_group() -> None:
    """Append idempotent derived ledger events."""


@derive_group.command("run")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def derive_run(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet.lower()] if wallet else ledger_wallets(session)
        for w in wallets:
            stats = derive_redeem_payouts(session, w, dust_epsilon=settings.dust_epsilon)
            episode_stats = rebuild_episodes(session, w, dust_epsilon=settings.dust_epsilon)
            pnl_stats = rebuild_pnl_decomposition(session, w, dust_epsilon=settings.dust_epsilon)
            click.echo(
                f"{stats.wallet}: zero_redeems={stats.zero_redeems_seen} "
                f"derived_inserted={stats.derived_events_inserted} "
                f"nonzero_redeems_skipped={stats.nonzero_redeems_skipped} "
                f"unresolved_redeems_skipped={stats.unresolved_redeems_skipped}"
            )
            click.echo(
                f"  episodes={episode_stats.episodes_written} "
                f"resolution={episode_stats.resolution_closed_episodes}; "
                f"pnl_rows={pnl_stats.rows_written} total_pnl={decimal_string(pnl_stats.total_pnl)}"
            )
    finally:
        session.close()


@click.group("pnl")
def pnl_group() -> None:
    """Inspect PnL decomposition."""


@pnl_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--by-category", is_flag=True, default=False)
def pnl_show(wallet: str, by_category: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_pnl_decomposition(session, wallet, by_category=by_category)
    finally:
        session.close()
    if not rows:
        click.echo("No PnL decomposition rows. Run `pmr derive run --wallet <addr>` first.")
        return
    for row in rows:
        scope = row.scope.removeprefix("category:") if by_category else row.scope
        click.echo(
            f"{scope}: directional={decimal_string(row.directional_pnl)} "
            f"bond_merge={decimal_string(row.bond_merge_pnl)} "
            f"rewards={decimal_string(row.reward_income)} "
            f"redemption={decimal_string(row.redemption_pnl)} "
            f"fees={decimal_string(row.fees)} total={decimal_string(row.total_pnl)}"
        )
