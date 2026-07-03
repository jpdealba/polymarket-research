"""pmr episodes: replay and inspect flat-to-flat episode projections."""

from __future__ import annotations

import json

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ledger.replay import ledger_wallets
from ..projections.episodes import episode_stats, fetch_episodes, rebuild_episodes

_PHASE8_CAVEAT = (
    "Note: resolution-closed episode PnL is understated until Phase 8 derives "
    "redemption proceeds."
)


@click.command("episodes")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def replay_episodes(wallet: str | None) -> None:
    """Rebuild flat-to-flat episodes from wallet_events."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet] if wallet else ledger_wallets(session)
        for w in wallets:
            stats = rebuild_episodes(session, w, dust_epsilon=settings.dust_epsilon)
            click.echo(
                f"{stats.wallet}: {stats.events_processed} events -> "
                f"{stats.episodes_written} episodes "
                f"({stats.open_episodes} open, {stats.flat_closed_episodes} flat, "
                f"{stats.resolution_closed_episodes} resolution), "
                f"as_of_ts={stats.as_of_ts}"
            )
            if stats.unmapped_condition_events:
                click.echo(
                    f"  data-quality: {stats.unmapped_condition_events} "
                    "MERGE/SPLIT/REDEEM events skipped over "
                    f"{stats.unmapped_condition_ids} conditions without token metadata."
                )
        click.echo(_PHASE8_CAVEAT)
    finally:
        session.close()


@click.group("episodes")
def episodes_group() -> None:
    """Inspect flat-to-flat episodes."""


@episodes_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--token", "token_id", default=None, help="Limit to one token_id.")
@click.option("--open", "open_only", is_flag=True, default=False, help="Only open episodes.")
def episodes_show(wallet: str, token_id: str | None, open_only: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_episodes(session, wallet, token_id=token_id, open_only=open_only)
    finally:
        session.close()
    if not rows:
        click.echo("No episode rows. Run `pmr replay episodes` first.")
        return
    for row in rows:
        consumed_count = len(json.loads(row.events_consumed))
        close_ts = row.close_ts if row.close_ts is not None else "-"
        click.echo(
            f"id={row.id} {row.close_reason:>10s} open={row.open_ts} close={close_ts} "
            f"peak={row.peak_qty} wac={row.wac_entry} realized={row.realized_pnl} "
            f"adds={row.num_adds} partial_exits={row.num_partial_exits} "
            f"events={consumed_count} token={row.token_id}"
        )
    click.echo(f"({len(rows)} rows)")
    click.echo(_PHASE8_CAVEAT)


@episodes_group.command("stats")
@click.option("--wallet", "wallet", required=True)
def episodes_stats(wallet: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        stats = episode_stats(session, wallet)
    finally:
        session.close()
    click.echo(f"wallet={stats.wallet}")
    click.echo(
        f"episodes={stats.count} open={stats.open_count} flat={stats.flat_closed_count} "
        f"resolution={stats.resolution_closed_count}"
    )
    click.echo(
        "duration_seconds="
        f"min={stats.duration_min} p50={stats.duration_p50} "
        f"p90={stats.duration_p90} max={stats.duration_max}"
    )
    click.echo(
        f"micro_episodes={stats.micro_episode_count} "
        f"micro_episode_share={stats.micro_episode_share}"
    )
    click.echo(f"realized_pnl={stats.realized_pnl} reward_income={stats.reward_income}")
    click.echo(_PHASE8_CAVEAT)
