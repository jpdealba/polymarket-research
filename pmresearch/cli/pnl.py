"""pmr derive / pmr pnl - Phase 8 derived payouts and PnL decomposition."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..fees.estimate import compute_fee_estimates
from ..ingest.derived import (
    DeriveProgress,
    derive_redeem_payouts,
    derive_resolution_settlements,
)
from ..ledger.replay import ledger_wallets
from ..projections.episodes import EpisodesProgress, rebuild_episodes
from ..projections.pnl_decomposition import (
    PnlProgress,
    fetch_pnl_decomposition,
    rebuild_pnl_decomposition,
)
from ..reconcile.checks import decimal_string
from ..reports.fee_attribution import fee_attribution_report


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
            click.echo(f"{w}: starting derive")
            stats = derive_redeem_payouts(
                session,
                w,
                dust_epsilon=settings.dust_epsilon,
                on_progress=_emit_derive_progress,
            )
            click.echo(f"{w}: deriving resolution settlements")
            settlement_stats = derive_resolution_settlements(
                session,
                w,
                dust_epsilon=settings.dust_epsilon,
                on_progress=_emit_derive_progress,
            )
            click.echo(
                f"{w}: resolved_open_tokens_seen={settlement_stats.resolved_open_tokens_seen} "
                f"settlements_inserted={settlement_stats.derived_events_inserted} "
                f"dust_skipped={settlement_stats.dust_skipped}"
            )
            click.echo(f"{w}: rebuilding episodes after derive")
            episode_stats = rebuild_episodes(
                session,
                w,
                dust_epsilon=settings.dust_epsilon,
                on_progress=_emit_episode_progress,
            )
            click.echo(f"{w}: rebuilding PnL decomposition")
            pnl_stats = rebuild_pnl_decomposition(
                session,
                w,
                dust_epsilon=settings.dust_epsilon,
                on_progress=_emit_pnl_progress,
            )
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


def _emit_derive_progress(progress: DeriveProgress) -> None:
    if progress.stage == "start":
        click.echo(f"  derive start: events_total={progress.events_total}")
    elif progress.stage == "events":
        click.echo(
            f"  derive events: {progress.events_processed}/{progress.events_total} "
            f"ts={progress.current_ts} inserted={progress.derived_inserted}"
        )
    elif progress.stage == "insert_flush":
        click.echo(
            f"  flush derived: inserted={progress.derived_inserted} "
            f"events={progress.events_processed}/{progress.events_total}"
        )


def _emit_episode_progress(progress: EpisodesProgress) -> None:
    if progress.stage == "start":
        click.echo(f"  episodes start: events_total={progress.events_total}")
    elif progress.stage == "events":
        click.echo(
            f"  episodes events: {progress.events_processed}/{progress.events_total} "
            f"ts={progress.current_ts} rows={progress.rows_written}"
        )
    elif progress.stage == "insert_flush":
        click.echo(
            f"  flush episodes: rows={progress.rows_written} "
            f"events={progress.events_processed}/{progress.events_total}"
        )


def _emit_pnl_progress(progress: PnlProgress) -> None:
    if progress.stage == "start":
        click.echo(f"  pnl start: events_total={progress.events_total}")
    elif progress.stage == "events":
        click.echo(
            f"  pnl events: {progress.events_processed}/{progress.events_total} "
            f"ts={progress.current_ts}"
        )
    elif progress.stage == "insert_flush":
        click.echo(
            f"  flush pnl_decomposition: rows={progress.rows_written} "
            f"events={progress.events_processed}/{progress.events_total}"
        )


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
        compute_fee_estimates(session, wallet=wallet)
        fee_rows = fee_attribution_report(session, wallet=wallet, by_category=by_category)
        rows = fetch_pnl_decomposition(session, wallet, by_category=by_category)
    finally:
        session.close()
    if not rows:
        click.echo("No PnL decomposition rows. Run `pmr derive run --wallet <addr>` first.")
        return
    fee_by_scope = {
        (row.category.lower() if by_category else "all"): row.blended_fee
        for row in fee_rows
    }
    for row in rows:
        scope = row.scope.removeprefix("category:") if by_category else row.scope
        fee_key = scope.lower() if by_category else "all"
        estimated_fees = fee_by_scope.get(fee_key, row.fees)
        gross_base_total = (
            row.directional_pnl
            + row.bond_merge_pnl
            + row.reward_income
            + row.redemption_pnl
        )
        click.echo(
            f"{scope}: directional={decimal_string(row.directional_pnl)} "
            f"bond_merge={decimal_string(row.bond_merge_pnl)} "
            f"rewards={decimal_string(row.reward_income)} "
            f"redemption={decimal_string(row.redemption_pnl)} "
            f"projection_fees={decimal_string(row.fees)} "
            f"gross_base_total={decimal_string(gross_base_total)} "
            f"estimated_fees={decimal_string(estimated_fees)} "
            f"estimated_net_pnl_scenario={decimal_string(gross_base_total - estimated_fees)}"
        )
