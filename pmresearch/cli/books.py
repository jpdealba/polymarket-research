"""pmr books — orderbook snapshot sampling and retention."""

from __future__ import annotations

import click
from sqlalchemy import text

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..logging_setup import setup_logging


@click.group("books")
def books_group() -> None:
    """Orderbook snapshot sampling for Relevant Tokens."""


@books_group.command("sample-once")
@click.option("--max-tokens", "max_tokens", default=50, show_default=True,
              help="Max tokens to poll per tick.")
def books_sample_once(max_tokens: int) -> None:
    """Snapshot orderbook for Relevant Tokens (open positions + recent trades)."""
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)

    from ..booksampler.sampler import sample_once

    stats = sample_once(settings, max_tokens=max_tokens)
    click.echo(
        f"Queried {stats.tokens_queried}/{stats.total_relevant} tokens; "
        f"written={stats.snapshots_written} found={stats.books_found} "
        f"empty={stats.empty_books} errors={stats.errors}"
    )


@books_group.command("status")
def books_status() -> None:
    """Show book sampler status: tracked tokens, snapshot counts, storage."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        token_count = session.execute(
            text("SELECT COUNT(DISTINCT token_id) FROM book_snapshots")
        ).scalar() or 0

        snapshot_count = session.execute(
            text("SELECT COUNT(*) FROM book_snapshots")
        ).scalar() or 0

        raw_count = session.execute(
            text("SELECT COUNT(*) FROM book_snapshots WHERE raw_ref IS NOT NULL")
        ).scalar() or 0

        oldest = session.execute(
            text("SELECT MIN(ts) FROM book_snapshots")
        ).scalar()

        newest = session.execute(
            text("SELECT MAX(ts) FROM book_snapshots")
        ).scalar()

        # Storage estimate: sum of raw gzip files for clob/book
        storage_row = session.execute(
            text(
                "SELECT COUNT(*), COALESCE(SUM(LENGTH(content_hash)), 0) "
                "FROM raw_fetches WHERE source = 'clob' AND endpoint = 'book'"
            )
        ).fetchone()

        click.echo(f"Tokens tracked:      {token_count}")
        click.echo(f"Snapshots total:     {snapshot_count}")
        click.echo(f"With raw ref:        {raw_count}")
        click.echo(f"Raw fetches (clob):  {storage_row[0] if storage_row else 0}")
        if oldest:
            from datetime import datetime, timezone
            oldest_dt = datetime.fromtimestamp(oldest, tz=timezone.utc)
            newest_dt = datetime.fromtimestamp(newest, tz=timezone.utc) if newest else None
            click.echo(f"Oldest snapshot:     {oldest_dt.isoformat()}")
            click.echo(f"Newest snapshot:     {newest_dt.isoformat() if newest_dt else 'N/A'}")

        # Estimate disk usage from raw files
        import os
        raw_books_dir = settings.raw_dir / "clob" / "book"
        if raw_books_dir.exists():
            total_bytes = sum(
                f.stat().st_size for f in raw_books_dir.rglob("*.json.gz") if f.is_file()
            )
            if total_bytes > 1_000_000:
                click.echo(f"Raw storage:         {total_bytes / 1_000_000:.1f} MB")
            else:
                click.echo(f"Raw storage:         {total_bytes / 1_000:.1f} KB")
        else:
            click.echo("Raw storage:         0 KB")
    finally:
        session.close()


@books_group.command("prune")
@click.option("--retention-days", "retention_days", default=None, type=int,
              help="Override PMR_BOOK_RETENTION_RAW_DAYS.")
def books_prune(retention_days: int | None) -> None:
    """Prune raw book snapshots older than the retention window."""
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        from ..booksampler.retention import prune_raw_books

        stats = prune_raw_books(session, settings, retention_days=retention_days)
        click.echo(
            f"Checked {stats.snapshots_checked} snapshots; "
            f"deleted {stats.raw_files_deleted} raw files, "
            f"nulled {stats.raw_refs_nulled} refs, "
            f"removed {stats.raw_fetches_deleted} raw_fetches rows; "
            f"freed {stats.bytes_freed} bytes"
        )
    finally:
        session.close()
