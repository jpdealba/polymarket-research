"""pmr replay / pmr holdings — rebuild and inspect ledger projections."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ledger.replay import ledger_wallets
from ..logging_setup import setup_logging
from ..projections.holdings import fetch_holdings, rebuild_holdings


@click.group("replay")
def replay_group() -> None:
    """Rebuild projections by replaying the wallet_events ledger."""


@replay_group.command("holdings")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def replay_holdings(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet] if wallet else ledger_wallets(session)
        for w in wallets:
            stats = rebuild_holdings(session, w, dust_epsilon=settings.dust_epsilon)
            click.echo(
                f"{stats.wallet}: {stats.events_processed} events -> "
                f"{stats.tokens_written} tokens ({stats.nonzero_tokens} nonzero), "
                f"as_of_ts={stats.as_of_ts}"
            )
            if stats.negative_qty_tokens or stats.unmapped_condition_events:
                click.echo(
                    f"  data-quality: {stats.negative_qty_tokens} tokens ended negative "
                    f"({stats.negative_qty_events} negative-qty events); "
                    f"{stats.unmapped_condition_events} MERGE/SPLIT/REDEEM events skipped "
                    f"over {stats.unmapped_condition_ids} conditions without token metadata."
                )
    finally:
        session.close()


@click.group("holdings")
def holdings_group() -> None:
    """Inspect the holdings projection."""


@holdings_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--nonzero", is_flag=True, default=False, help="Only holdings above the dust epsilon.")
def holdings_show(wallet: str, nonzero: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_holdings(
            session, wallet, nonzero=nonzero, dust_epsilon=settings.dust_epsilon
        )
    finally:
        session.close()
    if not rows:
        click.echo("No holdings rows. Run `pmr replay holdings` first.")
        return
    for row in rows:
        question = row.question or "(no market metadata)"
        outcome = row.outcome_label or "?"
        click.echo(
            f"qty={row.qty:>18s} wac={row.wac_cost:>12.12s} as_of={row.as_of_ts} "
            f"| {question} [{outcome}] token={row.token_id}"
        )
    click.echo(f"({len(rows)} rows)")
