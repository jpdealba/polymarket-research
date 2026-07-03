"""pmr reconcile / pmr trust - external-oracle data-quality checks."""

from __future__ import annotations

import json as json_lib

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..logging_setup import setup_logging
from ..reconcile.checks import decimal_string
from ..reconcile.runner import (
    latest_reconciliation_result,
    run_reconciliation,
    trust_dict,
)
from ..reconcile.trust import fetch_wallet_trust


@click.group("reconcile")
def reconcile_group() -> None:
    """Run and inspect external-oracle reconciliation."""


@reconcile_group.command("run")
@click.option("--wallet", "wallet", required=True)
@click.option("--json", "as_json", is_flag=True, default=False)
def reconcile_run(wallet: str, as_json: bool) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        result, trust = run_reconciliation(session, settings, wallet=wallet)
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()

    if as_json:
        click.echo(json_lib.dumps(result.as_dict(trust=trust_dict(trust)), indent=2))
        return
    _emit_result(result, trust_dict(trust))


@reconcile_group.command("status")
@click.option("--wallet", "wallet", default=None)
def reconcile_status(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        results = latest_reconciliation_result(session, wallet)
    finally:
        session.close()
    if not results:
        click.echo("No reconciliation facts. Run `pmr reconcile run --wallet <addr>` first.")
        return
    for result, trust in results:
        _emit_result(result, trust_dict(trust))


@click.group("trust")
def trust_group() -> None:
    """Inspect wallet trust status."""


@trust_group.command("status")
@click.option("--wallet", "wallet", default=None)
def trust_status(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_wallet_trust(session, wallet)
    finally:
        session.close()
    if not rows:
        click.echo("No wallet trust rows. Run reconciliation first.")
        return
    for row in rows:
        click.echo(
            f"{row.wallet}: status={row.status} since_ts={row.since_ts} "
            f"updated_ts={row.updated_ts} last_reconciliation_ts={row.last_reconciliation_ts} "
            f"reason={row.reason}"
        )


def _emit_result(result, trust: dict | None) -> None:
    summary = result.summary()
    click.echo(f"== Reconciliation: {result.wallet} ts={result.run_ts} ==")
    if trust is not None:
        click.echo(f"trust={trust['status']} reason={trust['reason']}")
    click.echo(f"tolerance={decimal_string(result.tolerance)}")
    click.echo(
        "remote_positions={remote_positions} local_nonzero_holdings={local_nonzero_holdings} "
        "exact_matches={exact_matches} passes={passes} warnings={warnings} fails={fails}".format(
            **summary
        )
    )
    click.echo("\n== Per-check status ==")
    for check_type, counts in result.check_status_counts().items():
        click.echo(
            f"{check_type}: total={counts['total']} pass={counts.get('pass', 0)} "
            f"warn={counts.get('warn', 0)} fail={counts.get('fail', 0)} "
            f"skip={counts.get('skip', 0)}"
        )
    _emit_known_exceptions(result)

    click.echo("\n== Negative holdings in /positions ==")
    _emit_presence(result.negative_holdings_presence)

    click.echo("\n== Missing-token holdings in /positions ==")
    _emit_presence(result.missing_token_metadata_presence)

    click.echo("\n== Top 20 qty discrepancies ==")
    _emit_discrepancies(result.top_qty_discrepancies())

    click.echo("\n== Top 20 estimated notional discrepancies ==")
    _emit_discrepancies(result.top_notional_discrepancies())

    click.echo("\n== Top 20 remote positions by current value ==")
    for row in result.top_remote_positions():
        notes = row["notes"]
        click.echo(
            f"token={row['subject']} value={row.get('remote_current_value')} "
            f"remote={row['expected']} local={row['computed']} diff={row['abs_diff']} "
            f"status={row['status']} reason={row['reason_code']} "
            f"title={notes.get('remote_title')!r} outcome={notes.get('remote_outcome')!r}"
        )

    click.echo("\n== WAC vs avgPrice discrepancies ==")
    _emit_oracle_discrepancies(result, "positions_wac_avg_price")

    click.echo("\n== realizedPnl discrepancies ==")
    _emit_oracle_discrepancies(result, "positions_realized_pnl")


def _emit_known_exceptions(result) -> None:
    exceptions = result.known_exceptions()
    types = sorted({item["exception_type"] for item in exceptions})
    type_text = ",".join(types) if types else "none"
    click.echo(
        f"known_exception_count={len(exceptions)} known_exception_types={type_text}"
    )
    if not exceptions:
        return
    click.echo("\n== Known exceptions ==")
    for item in exceptions:
        click.echo(
            f"token={item['token_id']} type={item['exception_type']} "
            f"classification={item['classification']} computed={item['computed']} "
            f"expected={item['expected']} condition={item['condition_id']} "
            f"title={item['question']!r} outcome={item['outcome']!r}"
        )


def _emit_presence(probes) -> None:
    if not probes:
        click.echo("(none)")
        return
    appeared = sum(1 for probe in probes if probe.appears_in_positions)
    click.echo(f"appears={appeared} total={len(probes)}")
    for probe in probes[:20]:
        click.echo(
            f"token={probe.token_id} qty={decimal_string(probe.qty)} "
            f"appears_in_positions={probe.appears_in_positions} reason={probe.reason_code}"
        )


def _emit_discrepancies(rows: list[dict]) -> None:
    if not rows:
        click.echo("(none)")
        return
    for row in rows[:20]:
        notes = row["notes"]
        click.echo(
            f"token={row['subject']} remote={row['expected']} local={row['computed']} "
            f"diff={row['abs_diff']} notional={row['estimated_notional_impact']} "
            f"status={row['status']} reason={row['reason_code']} "
            f"condition={notes.get('local_condition_id') or notes.get('remote_condition_id')} "
            f"title={notes.get('remote_title') or notes.get('local_question')!r} "
            f"outcome={notes.get('remote_outcome') or notes.get('local_outcome')!r}"
        )


def _emit_oracle_discrepancies(result, check_type: str) -> None:
    facts = [
        fact
        for fact in result.facts
        if fact.check_type == check_type and fact.status in {"warn", "fail", "skip"}
    ]
    if not facts:
        click.echo("(none)")
        return
    for fact in sorted(facts, key=lambda item: item.abs_diff, reverse=True)[:20]:
        notes = fact.notes
        click.echo(
            f"token={fact.subject} oracle={decimal_string(fact.expected)} "
            f"local={decimal_string(fact.computed)} diff={decimal_string(fact.abs_diff)} "
            f"tolerance={decimal_string(fact.tolerance)} status={fact.status} "
            f"reason={fact.reason_code} classification={notes.get('classification')} "
            f"scope={notes.get('comparison_scope')} title={notes.get('remote_title')!r} "
            f"outcome={notes.get('remote_outcome')!r}"
        )
