"""pmr context - forward context builders."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..context.maker_fills import build_all_fill_context, build_maker_fill_context
from ..db.engine import get_session_factory


@click.group("context")
def context_group() -> None:
    """Build forward context projections."""


@context_group.command("maker-fills")
@click.option("--wallet", "wallet", required=True)
@click.option("--watchlist", "watchlist", default=None)
@click.option("--max-age-s", "max_age_s", default=None, type=int)
def context_maker_fills(wallet: str, watchlist: str | None, max_age_s: int | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        stats = build_maker_fill_context(
            session,
            wallet=wallet,
            watchlist=watchlist or settings.worldcup_watchlist_name,
            max_age_s=max_age_s or settings.worldcup_context_max_age_s,
        )
    finally:
        session.close()
    click.echo(
        f"fills_seen={stats.fills_seen} contexts_written={stats.contexts_written} "
        f"excellent={stats.excellent} good={stats.good} usable={stats.usable} "
        f"weak={stats.weak} stale={stats.stale} missing={stats.missing}"
    )


@context_group.command("all-fills")
@click.option("--wallet", "wallet", required=True)
@click.option("--watchlist", "watchlist", default=None)
@click.option("--max-age-s", "max_age_s", default=None, type=int)
def context_all_fills(wallet: str, watchlist: str | None, max_age_s: int | None) -> None:
    """Build all-fill context without requiring maker/taker enrichment."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        stats = build_all_fill_context(
            session,
            wallet=wallet,
            watchlist=watchlist or settings.worldcup_watchlist_name,
            max_age_s=max_age_s or settings.worldcup_context_max_age_s,
        )
    finally:
        session.close()
    click.echo(
        f"fills_seen={stats.fills_seen} contexts_written={stats.contexts_written} "
        f"enriched={stats.enriched} unenriched={stats.unenriched} "
        f"excellent={stats.excellent} good={stats.good} usable={stats.usable} "
        f"weak={stats.weak} stale={stats.stale} missing={stats.missing}"
    )
