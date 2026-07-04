"""pmr watchlist - named token watchlists."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..watchlists.world_cup import (
    add_manual_token,
    build_world_cup_watchlist,
    deactivate_token,
    list_watchlist_tokens,
)


@click.group("watchlist")
def watchlist_group() -> None:
    """Named token watchlists."""


@watchlist_group.command("build-world-cup")
@click.option("--wallet", "wallet", required=True)
@click.option("--name", "name", default=None)
def watchlist_build_world_cup(wallet: str, name: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        stats = build_world_cup_watchlist(
            session,
            wallet,
            name=name or settings.worldcup_watchlist_name,
            dust_epsilon=str(settings.dust_epsilon),
        )
    finally:
        session.close()
    click.echo(
        f"watchlist_id={stats.watchlist_id} tokens_seen={stats.tokens_seen} "
        f"tokens_upserted={stats.tokens_upserted} active_tokens={stats.active_tokens}"
    )


@watchlist_group.command("show")
@click.option("--name", "name", required=True)
@click.option("--active-only", is_flag=True, default=False)
def watchlist_show(name: str, active_only: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = list_watchlist_tokens(session, name=name, active_only=active_only)
    finally:
        session.close()
    if not rows:
        click.echo("No watchlist tokens.")
        return
    for row in rows:
        click.echo(
            f"priority={row.priority:3d} active={row.is_active} source={row.source:22s} "
            f"token={row.token_id} outcome={row.outcome_label or '?'} "
            f"bid={row.latest_best_bid or '-'} ask={row.latest_best_ask or '-'} "
            f"question={row.question or '-'}"
        )
    click.echo(f"({len(rows)} rows)")


@watchlist_group.command("add-token")
@click.option("--name", "name", required=True)
@click.option("--token-id", "token_id", required=True)
@click.option("--reason", "reason", default="manual add")
def watchlist_add_token(name: str, token_id: str, reason: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        inserted = add_manual_token(session, name=name, token_id=token_id, reason=reason)
    finally:
        session.close()
    click.echo(f"token={token_id} inserted={inserted}")


@watchlist_group.command("deactivate-token")
@click.option("--name", "name", required=True)
@click.option("--token-id", "token_id", required=True)
def watchlist_deactivate_token(name: str, token_id: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        changed = deactivate_token(session, name=name, token_id=token_id)
    finally:
        session.close()
    click.echo(f"token={token_id} deactivated={changed}")

