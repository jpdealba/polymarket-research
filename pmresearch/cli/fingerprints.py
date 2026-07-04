"""pmr fingerprints: compute, show and compare behavioral fingerprints."""

from __future__ import annotations

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..fingerprints.compute import (
    FINGERPRINT_VERSION,
    compute_fingerprints,
    fetch_fingerprints,
    fingerprint_scopes,
)
from ..ledger.replay import ledger_wallets


@click.group("fingerprints")
def fingerprints_group() -> None:
    """Behavioral fingerprints (Phase 13)."""


@fingerprints_group.command("compute")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def fingerprints_compute(wallet: str | None) -> None:
    """Compute fingerprints (all scopes + windows) from projections."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet] if wallet else ledger_wallets(session)
        if not wallets:
            click.echo("No wallets found.")
            return
        for w in wallets:
            click.echo(f"{w.lower()}: computing fingerprints (v{FINGERPRINT_VERSION})...")
            stats = compute_fingerprints(session, w)
            click.echo(
                f"{stats.wallet}: scopes={stats.scopes} windows={stats.windows} "
                f"values={stats.values_written} null={stats.null_written}"
            )
    finally:
        session.close()


def _format_value(row) -> str:
    if row.value is None:
        return f"NULL ({row.null_reason})"
    if row.value_type == "json":
        return row.value
    return row.value


@fingerprints_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--scope", "scope", default="all", show_default=True,
              help='Scope: "all" or "category:<Label>".')
@click.option("--window", "window", default="all", show_default=True,
              help='Window: "all" or "90d".')
def fingerprints_show(wallet: str, scope: str, window: str) -> None:
    """Show one wallet's fingerprint for a scope/window."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_fingerprints(session, wallet, scope=scope, window=window)
        available = fingerprint_scopes(session, wallet, window=window)
    finally:
        session.close()
    if not rows:
        click.echo(
            f"No fingerprint rows for wallet={wallet.lower()} scope={scope} window={window}. "
            "Run `pmr fingerprints compute` first."
        )
        if available:
            click.echo(f"Available scopes (window={window}): {', '.join(available)}")
        return
    click.echo(f"wallet={wallet.lower()} scope={scope} window={window} "
               f"version={rows[0].version} computed_at={rows[0].computed_at}")
    current_family = None
    for row in rows:
        if row.family != current_family:
            current_family = row.family
            click.echo(f"[{current_family}]")
        click.echo(f"  {row.feature:<32s} {_format_value(row)}")
    click.echo(f"({len(rows)} features)")


@fingerprints_group.command("compare")
@click.option("--wallets", "wallets", required=True,
              help="Comma-separated wallet addresses.")
@click.option("--scope", "scope", default="all", show_default=True)
@click.option("--window", "window", default="all", show_default=True)
def fingerprints_compare(wallets: str, scope: str, window: str) -> None:
    """Compare fingerprints across wallets, one column per wallet."""
    addrs = [w.strip().lower() for w in wallets.split(",") if w.strip()]
    if len(addrs) < 2:
        raise click.ClickException("Provide at least two wallets to compare.")
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        by_wallet = {
            addr: {r.feature: r for r in fetch_fingerprints(session, addr, scope=scope, window=window)}
            for addr in addrs
        }
    finally:
        session.close()

    # Feature order from the first wallet that has any rows (family-grouped).
    ordered: list[tuple[str, str]] = []
    for addr in addrs:
        if by_wallet[addr]:
            ordered = [(r.family, r.feature) for r in by_wallet[addr].values()]
            break
    if not ordered:
        click.echo("No fingerprint rows for any of the given wallets. "
                   "Run `pmr fingerprints compute` first.")
        return

    header = f"{'feature':<32s}" + "".join(f"{a[:12]:>16s}" for a in addrs)
    click.echo(f"scope={scope} window={window}")
    click.echo(header)
    current_family = None
    for family, feature in ordered:
        if family != current_family:
            current_family = family
            click.echo(f"[{family}]")
        cells = ""
        for addr in addrs:
            row = by_wallet[addr].get(feature)
            if row is None:
                cell = "-"
            elif row.value is None:
                cell = "NULL"
            elif row.value_type == "json":
                cell = "<dist>"
            else:
                cell = _short_num(row.value)
            cells += f"{cell:>16s}"
        click.echo(f"  {feature:<30s}{cells}")


def _short_num(value: str) -> str:
    try:
        from decimal import Decimal

        d = Decimal(value)
    except Exception:
        return value[:15]
    if d == d.to_integral_value():
        return str(int(d))
    return f"{d:.4f}"
