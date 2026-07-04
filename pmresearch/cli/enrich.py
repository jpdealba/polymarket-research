"""pmr enrich - maker/taker enrichment from subgraph, RPC, or PolygonScan."""

from __future__ import annotations

import time
from decimal import Decimal

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ingest.enrichment import (
    _current_subgraph_ts,
    enrichment_coverage,
    run_enrichment,
)
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
    type=click.Choice(["subgraph", "rpc", "polygonscan"]),
    default="subgraph",
    show_default=True,
)
@click.option("--from-block", "from_block", default=0, show_default=True,
              help="Block source: start block. 0 = derive from the subgraph head timestamp.")
@click.option("--to-block", "to_block", default=None, type=int,
              help="Block source: end block. Omit to use the current chain head.")
@click.option("--chunk-blocks", "chunk_blocks", default=200000, show_default=True,
              help="Block source: block range per fetch. RPC auto-halves if capped; "
                   "PolygonScan paginates inside each chunk.")
@click.option("--ignore-watermark", is_flag=True,
              help="Block source: rescan from --from-block even if a later watermark exists.")
def enrich_run(
    wallet: str | None,
    source: str,
    from_block: int,
    to_block: int | None,
    chunk_blocks: int,
    ignore_watermark: bool,
) -> None:
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
    if source == "polygonscan" and not settings.polygonscan_api_key:
        raise click.ClickException(
            "PMR_POLYGONSCAN_API_KEY is not configured; set it to enable --source polygonscan."
        )

    session = get_session_factory(settings)()
    block_source = None
    head_block = to_block
    try:
        wallets = _target_wallets(session, wallet)
        if not wallets:
            click.echo("No wallets to enrich.")
            return

        if source in ("rpc", "polygonscan"):
            if source == "rpc":
                from ..sources.rpc import RpcSource

                block_source = RpcSource(settings.rpc_url)
            else:
                from ..sources.polygonscan import PolygonscanSource

                block_source = PolygonscanSource(settings.polygonscan_api_key)

            assert block_source is not None
            label = "RPC" if source == "rpc" else "PolygonScan"
            if head_block is None:
                head_block = block_source.get_block_number()
                click.echo(f"{label} head block = {head_block}")

        for address in wallets:
            wallet_from = from_block
            if source in ("rpc", "polygonscan") and from_block == 0:
                assert block_source is not None
                # No explicit start: begin at the block covering the subgraph's
                # head timestamp, i.e. exactly where subgraph coverage stops.
                subgraph_ts = _current_subgraph_ts(session, address)
                if subgraph_ts:
                    wallet_from = block_source.find_block_by_timestamp(subgraph_ts)
                    click.echo(
                        f"{address}: subgraph head ts={subgraph_ts} -> from_block={wallet_from}"
                    )

            stats = run_enrichment(
                session,
                settings,
                address,
                source=source,
                rpc=block_source,
                from_block=wallet_from,
                to_block=head_block,
                chunk_blocks=chunk_blocks,
                ignore_watermark=ignore_watermark,
            )
            if source in ("rpc", "polygonscan"):
                click.echo(
                    f"{address}: source={source} from_block={wallet_from} to_block={head_block} "
                    f"fills={stats.fills_seen} enriched={stats.enriched} "
                    f"ambiguous={stats.ambiguous} unmatched={stats.unmatched} "
                    f"already={stats.already_enriched}"
                )
            else:
                click.echo(
                    f"{address}: source={source} fills={stats.fills_seen} "
                    f"enriched={stats.enriched} ambiguous={stats.ambiguous} "
                    f"unmatched={stats.unmatched} already={stats.already_enriched} "
                    f"head_ts={stats.head_ts}"
                )
    finally:
        if block_source is not None:
            block_source.close()
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
