"""pmr enrich - maker/taker enrichment from the Goldsky subgraph (+ optional RPC)."""

from __future__ import annotations

import time
from decimal import Decimal

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.enrichment import enrichment_coverage, run_enrichment
from ..ledger.replay import ledger_wallets
from ..logging_setup import setup_logging
from ..walletmanager.manager import list_wallets


@click.group("enrich")
def enrich_group() -> None:
    """Maker/taker enrichment of ledger fills."""


def _target_wallets(session, wallet: str | None) -> list[str]:
    if wallet:
        return [wallet.lower()]
    active = [row.address for row in list_wallets(session, active_only=True)]
    return active or ledger_wallets(session)


@enrich_group.command("run")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
@click.option(
    "--source",
    type=click.Choice(["subgraph", "rpc"]),
    default="subgraph",
    show_default=True,
)
@click.option("--from-block", "from_block", default=0, show_default=True, help="RPC: start block.")
@click.option("--to-block", "to_block", default=None, type=int, help="RPC: end block (required for --source rpc).")
def enrich_run(wallet: str | None, source: str, from_block: int, to_block: int | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)

    if source == "subgraph" and not settings.subgraph_url:
        raise click.ClickException(
            "PMR_SUBGRAPH_URL is not configured; subgraph enrichment is unavailable."
        )
    if source == "rpc" and not settings.rpc_url:
        raise click.ClickException(
            "PMR_RPC_URL is not configured; RPC enrichment is off. Set it to enable --source rpc."
        )
    if source == "rpc" and to_block is None:
        raise click.ClickException("--to-block is required for --source rpc.")

    session = get_session_factory(settings)()
    try:
        wallets = _target_wallets(session, wallet)
        if not wallets:
            click.echo("No wallets to enrich.")
            return
        for address in wallets:
            stats = run_enrichment(
                session,
                settings,
                address,
                source=source,
                from_block=from_block,
                to_block=to_block,
            )
            click.echo(
                f"{address}: source={source} fills={stats.fills_seen} "
                f"enriched={stats.enriched} ambiguous={stats.ambiguous} "
                f"unmatched={stats.unmatched} already={stats.already_enriched} "
                f"head_ts={stats.head_ts}"
            )
    finally:
        session.close()


@enrich_group.command("coverage")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def enrich_coverage(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    now_ts = int(time.time())
    try:
        wallets = _target_wallets(session, wallet)
        if not wallets:
            click.echo("No wallets found.")
            return
        for address in wallets:
            cov = enrichment_coverage(session, address, now_ts=now_ts)
            share = (cov.enriched_share * 100).quantize(Decimal("0.01"))
            click.echo(
                f"{address}: trades={cov.total} enriched={cov.enriched} ({share}%) "
                f"pending={cov.pending} ambiguous={cov.ambiguous} missing={cov.missing} "
                f"subgraph_head_ts={cov.head_ts}"
            )
            for bucket in cov.buckets:
                click.echo(
                    f"  {bucket.label}: total={bucket.total} enriched={bucket.enriched} "
                    f"pending={bucket.pending} ambiguous={bucket.ambiguous} missing={bucket.missing}"
                )
    finally:
        session.close()
