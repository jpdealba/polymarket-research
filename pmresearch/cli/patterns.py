"""pmr patterns: Phase 22.5 descriptive pattern mining outputs."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..patterns.dataset import (
    OUTPUT_FILES,
    build_patterns_dataset,
    export_patterns_dataset,
    write_pattern_summary,
)
from ..patterns.rule_candidates import extract_rule_candidates


@click.group("patterns")
def patterns_group() -> None:
    """Phase 22.5 order timing and pattern mining."""


@patterns_group.command("build")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path), required=True)
@click.option("--event-id", "event_id", required=False)
@click.option("--watchlist", "watchlist", required=False)
@click.option(
    "--min-context",
    "min_context",
    type=click.Choice(["excellent", "good", "usable", "weak"], case_sensitive=False),
    default="usable",
    show_default=True,
)
@click.option("--lookback-s", "lookback_s", type=int, default=7200, show_default=True)
@click.option(
    "--book-match-tolerance-bps",
    "book_match_tolerance_bps",
    type=int,
    default=5,
    show_default=True,
)
@click.option("--include-gap-wallet", "include_gap_wallet", is_flag=True, default=False)
def patterns_build(
    wallet: str,
    out_dir: Path,
    event_id: str | None,
    watchlist: str | None,
    min_context: str,
    lookback_s: int,
    book_match_tolerance_bps: int,
    include_gap_wallet: bool,
) -> None:
    """Build the Phase 22.5 CSV dataset and reports."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    last_progress: dict[str, int] = {}

    def progress(stage: str, count: int) -> None:
        previous = last_progress.get(stage)
        if previous == count:
            return
        last_progress[stage] = count
        click.echo(f"progress patterns {stage} count={count}", err=True)

    try:
        stats = build_patterns_dataset(
            session,
            wallet=wallet,
            out_dir=out_dir,
            event_id=event_id,
            watchlist=watchlist,
            min_context=min_context.lower(),
            lookback_s=lookback_s,
            book_match_tolerance_bps=book_match_tolerance_bps,
            include_gap_wallet=include_gap_wallet,
            progress_callback=progress,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()
    click.echo(
        f"patterns wallet={stats.wallet} fills={stats.fills} "
        f"pair_rows={stats.pair_completions} merges={stats.merges} "
        f"sibling_sequences={stats.sibling_sequences} unpaired_periods={stats.unpaired_periods}"
    )
    for filename in OUTPUT_FILES:
        click.echo(f"wrote {out_dir / filename}")


@patterns_group.command("report")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path), required=True)
def patterns_report(wallet: str, out_dir: Path) -> None:
    """Regenerate the markdown summary from existing Phase 22.5 CSVs."""
    _ = wallet
    path = write_pattern_summary(out_dir)
    click.echo(f"wrote {path}")


@patterns_group.command("extract-rules")
@click.option(
    "--in-dir",
    "in_dir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    required=True,
    help="Directory containing existing Phase 22.5 outputs.",
)
@click.option(
    "--out-dir",
    "out_dir",
    type=click.Path(file_okay=False, path_type=Path),
    required=False,
    help="Directory for Phase 22.5b outputs. Defaults to --in-dir.",
)
def patterns_extract_rules(in_dir: Path, out_dir: Path | None) -> None:
    """Extract Phase 22.5b actionable rule candidates from existing outputs."""
    try:
        stats = extract_rule_candidates(in_dir=in_dir, out_dir=out_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"rules extracted={stats.rules} out_dir={stats.out_dir}")
    click.echo(f"wrote {stats.rule_candidates}")
    click.echo(f"wrote {stats.extraction_report}")
    click.echo(f"wrote {stats.evidence_examples}")
    click.echo(f"wrote {stats.quality_report}")


@patterns_group.command("export")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
def patterns_export(wallet: str, out_path: Path) -> None:
    """Export the main order timing dataset CSV."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = export_patterns_dataset(session, wallet=wallet, out=out_path)
    finally:
        session.close()
    click.echo(f"exported rows={rows} out={out_path}")
