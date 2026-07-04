"""pmr books — orderbook snapshot sampling and retention."""

from __future__ import annotations

import click

from ..booksampler.status import book_sampler_status
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
        status = book_sampler_status(session, settings)

        click.echo(f"Tokens tracked:      {status.token_count}")
        click.echo(f"Snapshots total:     {status.snapshot_count}")
        click.echo(f"With raw ref:        {status.with_raw_ref}")
        click.echo(f"Raw fetches (clob):  {status.raw_fetch_count}")
        if status.oldest_ts:
            from datetime import datetime, timezone
            oldest_dt = datetime.fromtimestamp(status.oldest_ts, tz=timezone.utc)
            newest_dt = (
                datetime.fromtimestamp(status.newest_ts, tz=timezone.utc)
                if status.newest_ts
                else None
            )
            click.echo(f"Oldest snapshot:     {oldest_dt.isoformat()}")
            click.echo(f"Newest snapshot:     {newest_dt.isoformat() if newest_dt else 'N/A'}")

        if status.raw_storage_bytes > 1_000_000:
            click.echo(f"Raw storage:         {status.raw_storage_bytes / 1_000_000:.1f} MB")
        else:
            click.echo(f"Raw storage:         {status.raw_storage_bytes / 1_000:.1f} KB")
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
