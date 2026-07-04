"""pmr report - Phase 15 wallet research memo generator."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..reports.render import render_wallet_profile
from ..reports.wallet_profile import build_wallet_profile


@click.group("report")
def report_group() -> None:
    """Generate research reports from stored projections."""


@report_group.command("wallet")
@click.argument("address")
@click.option("--out", "out", type=click.Path(path_type=Path), default=None,
              help="Output path (default: {data}/exports/wallet_profile_<addr>_<ts>.md).")
@click.option("--window", "window", default="all", show_default=True,
              help="Fingerprint window: 'all' or '90d'.")
def report_wallet(address: str, out: Path | None, window: str) -> None:
    """Render the "Why is <wallet> profitable?" memo for ADDRESS."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        profile = build_wallet_profile(session, address, window=window)
    finally:
        session.close()

    markdown = render_wallet_profile(profile)

    if out is None:
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        out = settings.exports_dir / f"wallet_profile_{address.lower()}_{ts}.md"
    out.write_text(markdown, encoding="utf-8")

    # The memo contains non-latin-1 glyphs (⚠️/✅). Best-effort UTF-8 stdout so
    # the echo does not crash on a legacy Windows console; the file is UTF-8 either way.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass
    click.echo(markdown)
    click.echo(f"\n[written to {out}]", err=True)
