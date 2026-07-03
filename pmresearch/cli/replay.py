"""pmr replay / pmr holdings — rebuild and inspect ledger projections."""

from __future__ import annotations

import json as json_lib

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..ledger.replay import ledger_wallets
from ..logging_setup import setup_logging
from ..projections.holdings import fetch_holdings, rebuild_holdings
from ..reports.holdings_dq import (
    missing_conditions_report,
    missing_token_metadata_report,
    negative_holdings_report,
    undocumented_events_report,
)


@click.group("replay")
def replay_group() -> None:
    """Rebuild projections by replaying the wallet_events ledger."""


@replay_group.command("holdings")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def replay_holdings(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet] if wallet else ledger_wallets(session)
        for w in wallets:
            stats = rebuild_holdings(session, w, dust_epsilon=settings.dust_epsilon)
            click.echo(
                f"{stats.wallet}: {stats.events_processed} events -> "
                f"{stats.tokens_written} tokens ({stats.nonzero_tokens} nonzero), "
                f"as_of_ts={stats.as_of_ts}"
            )
            if stats.negative_qty_tokens or stats.unmapped_condition_events:
                click.echo(
                    f"  data-quality: {stats.negative_qty_tokens} tokens ended negative "
                    f"({stats.negative_qty_events} negative-qty events); "
                    f"{stats.unmapped_condition_events} MERGE/SPLIT/REDEEM events skipped "
                    f"over {stats.unmapped_condition_ids} conditions without token metadata."
                )
    finally:
        session.close()


@click.group("holdings")
def holdings_group() -> None:
    """Inspect the holdings projection."""


@holdings_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--nonzero", is_flag=True, default=False, help="Only holdings above the dust epsilon.")
def holdings_show(wallet: str, nonzero: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_holdings(
            session, wallet, nonzero=nonzero, dust_epsilon=settings.dust_epsilon
        )
    finally:
        session.close()
    if not rows:
        click.echo("No holdings rows. Run `pmr replay holdings` first.")
        return
    for row in rows:
        question = row.question or "(no market metadata)"
        outcome = row.outcome_label or "?"
        click.echo(
            f"qty={row.qty:>18s} wac={row.wac_cost:>12.12s} as_of={row.as_of_ts} "
            f"| {question} [{outcome}] token={row.token_id}"
        )
    click.echo(f"({len(rows)} rows)")


@holdings_group.command("dq")
@click.option("--wallet", "wallet", required=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON instead.")
def holdings_dq(wallet: str, as_json: bool) -> None:
    """Phase 4 data-quality report: negative holdings, unmapped condition_ids,
    holdings without token metadata, and event types outside the documented
    ledger enum (e.g. CONVERSION). Read-only; run `pmr replay holdings` first."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        neg_rows, neg_summary = negative_holdings_report(session, wallet, dust_epsilon=settings.dust_epsilon)
        missing_conditions = missing_conditions_report(session, wallet)
        missing_tokens = missing_token_metadata_report(session, wallet, dust_epsilon=settings.dust_epsilon)
        undocumented = undocumented_events_report(session, wallet)
    finally:
        session.close()

    if as_json:
        payload = {
            "wallet": wallet.lower(),
            "negative_holdings_summary": {
                "negative_token_count": neg_summary.negative_token_count,
                "negative_condition_count": neg_summary.negative_condition_count,
                "paired_equal_magnitude_conditions": neg_summary.paired_equal_magnitude_conditions,
                "total_negative_qty": str(neg_summary.total_negative_qty),
                "cause_event_type_counts": neg_summary.cause_event_type_counts,
            },
            "negative_holdings_detail": [
                {
                    "token_id": r.token_id,
                    "qty": str(r.qty),
                    "wac_cost": str(r.wac_cost),
                    "as_of_ts": r.as_of_ts,
                    "condition_id": r.condition_id,
                    "outcome_label": r.outcome_label,
                    "question": r.question,
                    "category": r.category,
                    "closed": r.closed,
                    "cause_event_id": r.cause_event_id,
                    "cause_event_type": r.cause_event_type,
                    "cause_event_ts": r.cause_event_ts,
                }
                for r in neg_rows
            ],
            "missing_conditions": [
                {
                    "condition_id": r.condition_id,
                    "event_count": r.event_count,
                    "event_types": r.event_types,
                    "total_usdc_size": str(r.total_usdc_size),
                    "first_ts": r.first_ts,
                    "last_ts": r.last_ts,
                    "classification": r.classification,
                    "normalized_match_question": r.normalized_match_question,
                }
                for r in missing_conditions
            ],
            "missing_token_metadata": [
                {
                    "token_id": r.token_id,
                    "qty": str(r.qty),
                    "wac_cost": str(r.wac_cost),
                    "as_of_ts": r.as_of_ts,
                }
                for r in missing_tokens
            ],
            "undocumented_events": [
                {
                    "id": r.id,
                    "event_type": r.event_type,
                    "ts": r.ts,
                    "condition_id": r.condition_id,
                    "usdc_size": str(r.usdc_size),
                    "tx_hash": r.tx_hash,
                    "raw_ref": r.raw_ref,
                }
                for r in undocumented
            ],
        }
        click.echo(json_lib.dumps(payload, indent=2))
        return

    click.echo("== Negative holdings summary ==")
    click.echo(
        f"negative_tokens={neg_summary.negative_token_count} "
        f"negative_conditions={neg_summary.negative_condition_count} "
        f"paired_equal_magnitude_conditions={neg_summary.paired_equal_magnitude_conditions} "
        f"total_negative_qty={neg_summary.total_negative_qty}"
    )
    click.echo(f"cause_event_type_counts={neg_summary.cause_event_type_counts}")

    click.echo("\n== Negative holdings detail ==")
    for r in neg_rows:
        click.echo(
            f"token={r.token_id} qty={r.qty} wac={r.wac_cost} condition_id={r.condition_id} "
            f"question={r.question!r} outcome={r.outcome_label} category={r.category} closed={r.closed} "
            f"cause_event_id={r.cause_event_id} cause_event_type={r.cause_event_type} cause_event_ts={r.cause_event_ts}"
        )

    click.echo("\n== Missing condition_ids (MERGE/REDEEM/etc. skipped for lacking token metadata) ==")
    for r in missing_conditions:
        click.echo(
            f"condition_id={r.condition_id} events={r.event_count} types={r.event_types} "
            f"total_usdc={r.total_usdc_size} first_ts={r.first_ts} last_ts={r.last_ts} "
            f"classification={r.classification} normalized_match={r.normalized_match_question!r}"
        )

    click.echo("\n== Holdings without token metadata ==")
    for r in missing_tokens:
        click.echo(f"token={r.token_id} qty={r.qty} wac={r.wac_cost} as_of_ts={r.as_of_ts}")

    click.echo("\n== Events outside the documented ledger enum (e.g. CONVERSION) ==")
    for r in undocumented:
        click.echo(
            f"id={r.id} type={r.event_type} ts={r.ts} condition_id={r.condition_id} "
            f"usdc_size={r.usdc_size} tx_hash={r.tx_hash} raw_ref={r.raw_ref}"
        )
