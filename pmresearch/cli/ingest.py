"""pmr ingest / pmr ledger — parse Raw Store activity payloads into wallet_events."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import click
from sqlalchemy.exc import OperationalError
from sqlalchemy import text

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..fees.estimate import compute_fee_estimates
from ..ingest.runner import reparse_wallet, run_ingest_with_progress
from ..logging_setup import setup_logging

SPORTS_FEE_CUTOFF_TS = 1774828800  # 2026-03-30T00:00:00Z
FEE_PERIODS = (
    ("pre_sports_fee", None, SPORTS_FEE_CUTOFF_TS),
    ("post_sports_fee", SPORTS_FEE_CUTOFF_TS, None),
)


def _decimal(value: object) -> Decimal:
    return Decimal(str(value or "0"))


def _fmt_usdc(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.000001'))}"


def _fmt_roi(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def _ledger_totals(rows, *, open_value: Decimal) -> dict[str, Decimal]:
    totals = {
        "buy": Decimal("0"),
        "sell": Decimal("0"),
        "redeem": Decimal("0"),
        "merge": Decimal("0"),
        "reward": Decimal("0"),
        "taker_rebate": Decimal("0"),
        "maker_rebate": Decimal("0"),
        "split": Decimal("0"),
        "open_value": open_value,
    }
    for row in rows:
        event_type = row.event_type
        side = row.side
        delta_usdc = _decimal(row.delta_usdc)
        usdc_size = _decimal(row.usdc_size)

        if event_type == "TRADE" and side == "BUY":
            totals["buy"] += -delta_usdc
        elif event_type == "TRADE" and side == "SELL":
            totals["sell"] += delta_usdc
        elif event_type == "REDEEM":
            totals["redeem"] += delta_usdc
        elif event_type == "MERGE":
            totals["merge"] += delta_usdc
        elif event_type == "REWARD":
            totals["reward"] += delta_usdc
        elif event_type == "TAKER_REBATE":
            totals["taker_rebate"] += delta_usdc if delta_usdc else usdc_size
        elif event_type == "MAKER_REBATE":
            totals["maker_rebate"] += delta_usdc if delta_usdc else usdc_size
        elif event_type == "SPLIT":
            totals["split"] += -delta_usdc

    totals["pnl"] = (
        totals["sell"]
        + totals["redeem"]
        + totals["merge"]
        + totals["reward"]
        + totals["taker_rebate"]
        + totals["maker_rebate"]
        - totals["buy"]
        - totals["split"]
        + totals["open_value"]
    )
    return totals


def _event_counts(rows) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row.event_type] = counts.get(row.event_type, 0) + 1
    return counts


def _period_rows(rows, start_ts: int | None, end_ts: int | None):
    return [
        row
        for row in rows
        if (start_ts is None or row.ts >= start_ts) and (end_ts is None or row.ts < end_ts)
    ]


def _roi_on_buy_volume(totals: dict[str, Decimal]) -> Decimal | None:
    if totals["buy"] == 0:
        return None
    return totals["pnl"] / totals["buy"]


def _post_cutoff_fee_scenario(session, wallet: str | None) -> dict[str, Decimal | int]:
    def progress(processed: int, total: int) -> None:
        click.echo(f"fee_estimates_progress={processed}/{total}", err=True)

    compute_fee_estimates(session, wallet=wallet, on_progress=progress)
    query = (
        "SELECT fe.estimated_fee, fe.worst_case_fee "
        "FROM fee_estimates fe "
        "JOIN wallet_events we ON we.id = fe.event_id "
        "WHERE we.event_type = 'TRADE' AND we.ts >= :cutoff"
    )
    params: dict[str, object] = {"cutoff": SPORTS_FEE_CUTOFF_TS}
    if wallet:
        query += " AND lower(we.wallet) = lower(:wallet)"
        params["wallet"] = wallet
    rows = session.execute(text(query), params).fetchall()
    return {
        "trades": len(rows),
        "estimated_fee": sum((_decimal(row.estimated_fee) for row in rows), Decimal("0")),
        "worst_case_fee": sum((_decimal(row.worst_case_fee) for row in rows), Decimal("0")),
    }


@click.group("ingest")
def ingest_group() -> None:
    """Parse Raw Store activity payloads into the wallet_events ledger."""


@ingest_group.command("run")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
def ingest_run(wallet: str | None) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        def progress(processed: int, total: int, seen: int, inserted: int) -> None:
            click.echo(
                f"ingest_progress={processed}/{total} events_seen={seen} "
                f"events_inserted={inserted}"
            )

        stats = run_ingest_with_progress(session, wallet=wallet, on_progress=progress)
    finally:
        session.close()
    click.echo(
        f"Processed {stats.raw_fetches_processed} raw fetches, "
        f"saw {stats.events_seen} events, inserted {stats.events_inserted} new rows."
    )


@ingest_group.command("reparse")
@click.option("--wallet", "wallet", required=True)
def ingest_reparse(wallet: str) -> None:
    settings = get_settings()
    ensure_data_dirs(settings)
    setup_logging(settings)
    session = get_session_factory(settings)()
    try:
        click.echo(f"reparse_start wallet={wallet.lower()}")

        def progress(processed: int, total: int, seen: int, inserted: int) -> None:
            click.echo(
                f"reparse_progress={processed}/{total} events_seen={seen} "
                f"events_inserted={inserted}"
            )

        stats = reparse_wallet(session, wallet, on_progress=progress)
    finally:
        session.close()
    click.echo(
        f"Reparsed {wallet.lower()}: {stats.raw_fetches_processed} raw fetches, "
        f"{stats.events_inserted} rows inserted."
    )


@click.group("ledger")
def ledger_group() -> None:
    """Ledger inspection."""


@ledger_group.command("stats")
@click.option("--wallet", "wallet", default=None)
@click.option(
    "--open-value",
    default="0",
    help="USDC value of open positions to include in gross/base PnL. Default: 0.",
)
def ledger_stats(wallet: str | None, open_value: str) -> None:
    try:
        open_value_decimal = Decimal(open_value)
    except InvalidOperation as exc:
        raise click.BadParameter("must be a decimal number", param_hint="--open-value") from exc

    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        query = (
            "SELECT event_type, COUNT(*) AS cnt, MIN(ts) AS min_ts, MAX(ts) AS max_ts "
            "FROM wallet_events"
        )
        params = {}
        if wallet:
            query += " WHERE wallet = :w"
            params["w"] = wallet.lower()
        query += " GROUP BY event_type ORDER BY cnt DESC"
        rows = session.execute(text(query), params).fetchall()

        detail_query = "SELECT event_type, side, delta_usdc, usdc_size, ts FROM wallet_events"
        if wallet:
            detail_query += " WHERE wallet = :w"
        detail_rows = session.execute(text(detail_query), params).fetchall()

        fee_scenario = None
        fee_scenario_unavailable = None
        try:
            fee_scenario = _post_cutoff_fee_scenario(session, wallet)
        except OperationalError as exc:
            session.rollback()
            if "no such table:" in str(exc):
                fee_scenario_unavailable = "run `pmr db upgrade` to enable fee estimates"
            else:
                fee_scenario_unavailable = str(exc)
    finally:
        session.close()
    if not rows:
        click.echo("No ledger events.")
        return
    for row in rows:
        click.echo(f"{row.event_type:15s} count={row.cnt:8d} ts=[{row.min_ts}, {row.max_ts}]")

    totals = _ledger_totals(detail_rows, open_value=open_value_decimal)
    click.echo("")
    click.echo("Gross/base ledger USDC totals (fees not applied):")
    click.echo(f"TRADE BUY total       {_fmt_usdc(totals['buy'])}")
    click.echo(f"TRADE SELL total      {_fmt_usdc(totals['sell'])}")
    click.echo(f"REDEEM total          {_fmt_usdc(totals['redeem'])}")
    click.echo(f"MERGE total           {_fmt_usdc(totals['merge'])}")
    click.echo(f"REWARD total          {_fmt_usdc(totals['reward'])}")
    click.echo(f"TAKER_REBATE total    {_fmt_usdc(totals['taker_rebate'])}")
    click.echo(f"MAKER_REBATE total    {_fmt_usdc(totals['maker_rebate'])}")
    click.echo(f"SPLIT total           {_fmt_usdc(totals['split'])}")
    click.echo(f"OPEN_VALUE            {_fmt_usdc(totals['open_value'])}")
    click.echo("")
    click.echo(f"Gross/base PnL        {_fmt_usdc(totals['pnl'])}")

    click.echo("")
    click.echo("Sports fee date periods (gross/base only; fees not applied):")
    for index, (period_name, start_ts, end_ts) in enumerate(FEE_PERIODS, start=1):
        period_rows = _period_rows(detail_rows, start_ts, end_ts)
        period_totals = _ledger_totals(period_rows, open_value=Decimal("0"))
        period_counts = _event_counts(period_rows)
        roi = _roi_on_buy_volume(period_totals)
        label = "Period A" if index == 1 else "Period B"
        window = (
            "ts < 2026-03-30 00:00:00 UTC"
            if end_ts is not None
            else "ts >= 2026-03-30 00:00:00 UTC"
        )

        click.echo("")
        click.echo(f"{label}: {period_name} ({window})")
        click.echo(f"TRADE BUY total       {_fmt_usdc(period_totals['buy'])}")
        click.echo(f"TRADE SELL total      {_fmt_usdc(period_totals['sell'])}")
        click.echo(f"REDEEM total          {_fmt_usdc(period_totals['redeem'])}")
        click.echo(f"MERGE total           {_fmt_usdc(period_totals['merge'])}")
        click.echo(f"REWARD total          {_fmt_usdc(period_totals['reward'])}")
        click.echo(f"TAKER_REBATE total    {_fmt_usdc(period_totals['taker_rebate'])}")
        click.echo(f"MAKER_REBATE total    {_fmt_usdc(period_totals['maker_rebate'])}")
        click.echo(f"SPLIT total           {_fmt_usdc(period_totals['split'])}")
        click.echo(f"Gross/base PnL        {_fmt_usdc(period_totals['pnl'])}")
        click.echo(f"Gross/base ROI on BUY volume {_fmt_roi(roi)}")
        click.echo("event counts by type:")
        if not period_counts:
            click.echo("  none")
        else:
            for event_type in sorted(period_counts):
                click.echo(f"  {event_type:15s} {period_counts[event_type]}")

    post_rows = _period_rows(detail_rows, SPORTS_FEE_CUTOFF_TS, None)
    post_totals = _ledger_totals(post_rows, open_value=Decimal("0"))
    click.echo("")
    click.echo("Estimated fee scenario:")
    click.echo(
        "Scope: post 2026-03-30 trades only; schedule-based taker assumption, "
        "not actual net PnL. wallet_events gross/base PnL above is unchanged."
    )
    if fee_scenario_unavailable:
        click.echo(f"unavailable={fee_scenario_unavailable}")
    else:
        estimated_fee = fee_scenario["estimated_fee"] if fee_scenario else Decimal("0")
        worst_case_fee = fee_scenario["worst_case_fee"] if fee_scenario else Decimal("0")
        trades = fee_scenario["trades"] if fee_scenario else 0
        click.echo(f"post_2026_03_30_trades_with_fee_rows {trades}")
        click.echo(f"post_2026_03_30_gross_base_pnl     {_fmt_usdc(post_totals['pnl'])}")
        click.echo(f"estimated_fee_after_2026_03_30     {_fmt_usdc(estimated_fee)}")
        click.echo(f"worst_case_fee_after_2026_03_30    {_fmt_usdc(worst_case_fee)}")
        click.echo(
            f"estimated_net_pnl_after_fee         "
            f"{_fmt_usdc(post_totals['pnl'] - estimated_fee)}"
        )
