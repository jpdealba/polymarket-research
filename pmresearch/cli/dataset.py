"""pmr dataset: Phase 20 microstructure + lifecycle dataset (read-only join
layer over maker_fill_context + episodes + exposure)."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..microstructure.dataset import build_microstructure_dataset, dataset_stats, export_dataset


@click.group("dataset")
def dataset_group() -> None:
    """Research datasets built over the ledger and forward-collected evidence."""


@dataset_group.group("microstructure")
def microstructure_group() -> None:
    """Phase 20 microstructure + lifecycle dataset."""


@microstructure_group.command("build")
@click.option("--wallet", "wallet", required=True)
@click.option("--watchlist", "watchlist", default="world_cup_2026", show_default=True)
@click.option(
    "--min-context",
    "min_context",
    type=click.Choice(["excellent", "good", "usable", "weak", "stale", "missing"]),
    default="usable",
    show_default=True,
)
def build(wallet: str, watchlist: str, min_context: str) -> None:
    """Build/rebuild the microstructure_lifecycle_dataset rows for a wallet."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        stats = build_microstructure_dataset(
            session, wallet=wallet, watchlist=watchlist, min_context=min_context
        )
    finally:
        session.close()
    click.echo(f"fills_seen={stats.fills_seen} rows_written={stats.rows_written}")
    click.echo(f"by_context_status={stats.by_context_status}")
    click.echo(f"by_close_path={stats.by_close_path}")


@microstructure_group.command("stats")
@click.option("--wallet", "wallet", required=True)
def stats_cmd(wallet: str) -> None:
    """Show aggregate stats for a wallet's built dataset."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        result = dataset_stats(session, wallet)
    finally:
        session.close()
    click.echo(f"wallet={result['wallet']}")
    click.echo(f"total_rows={result['total_rows']}")
    click.echo(f"by_context_status={result['by_context_status']}")
    click.echo(f"by_close_path={result['by_close_path']}")
    click.echo("null_reason_counts:")
    for key, count in sorted(result["null_reason_counts"].items()):
        click.echo(f"  {key}: {count}")


@microstructure_group.command("export")
@click.option("--wallet", "wallet", required=True)
@click.option("--out", "out", type=click.Path(path_type=Path), required=True)
@click.option(
    "--format", "fmt", type=click.Choice(["parquet", "csv"]), default="parquet", show_default=True
)
def export(wallet: str, out: Path, fmt: str) -> None:
    """Export a wallet's built dataset to Parquet or CSV."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        n = export_dataset(session, wallet, out, fmt=fmt)
    finally:
        session.close()
    click.echo(f"rows_written={n} out={out}")
