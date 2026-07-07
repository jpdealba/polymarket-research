"""pmr sim: Phase 22 counterfactual simulation commands."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..simulation.attribution import (
    fetch_event_attribution,
    fetch_market_attribution,
    generate_attribution_report,
)
from ..simulation.composite_search import (
    COMPONENT_CONTRIBUTION_FILENAME,
    COMPONENT_EFFECTIVENESS_FILENAME,
    COMPONENT_EFFECTIVENESS_REPORT_FILENAME,
    COMPOSITE_CANDIDATES_FILENAME,
    COMPOSITE_REPORT_FILENAME,
    COMPOSITE_TOP_FILENAME,
    EVENT_ROBUSTNESS_FILENAME,
    FORWARD_WATCH_FILENAME,
    PER_CANDIDATE_EVENT_PNL_FILENAME,
    SKIPPED_BY_COMPONENT_FILENAME,
    run_progressive_composite_search,
    write_composite_outputs,
)
from ..simulation.engine import run_simulation, run_strategy_simulation
from ..simulation.holdout_failure import (
    BOOK_AGE_FILENAME,
    CONDITION_FILENAME,
    PRICE_BUCKET_FILENAME,
    REPORT_FILENAME,
    SIDE_FILENAME,
    TIME_BUCKET_FILENAME,
    write_holdout_failure_outputs,
)
from ..simulation.pattern_rules import is_pattern_strategy, write_pattern_strategy_outputs
from ..simulation.inventory_cycling import fetch_lifecycle_summary
from ..simulation.report import generate_compare_report
from ..simulation.risk import RiskLimits
from ..simulation.search import (
    DEFAULT_MIN_TEST_FILLS,
    candidate_test_passes,
    candidate_validation_passes,
    fetch_latest_search,
    final_status,
    generate_search_report,
    run_strategy_search,
    selection_score,
    top_candidates,
    write_search_report,
)


@click.group("sim")
def sim_group() -> None:
    """Phase 22: counterfactual simulation."""


@sim_group.command("run")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=False, help="Rule name.")
@click.option("--strategy", "strategy_name", required=False, help="Strategy name.")
@click.option(
    "--scenario",
    "scenario_name",
    type=click.Choice(["conservative", "medium", "optimistic"], case_sensitive=False),
    required=True,
    help="Fill scenario.",
)
@click.option("--max-position", "max_position", type=float, default=500, show_default=True)
@click.option("--max-daily-loss", "max_daily_loss", type=float, default=100, show_default=True)
@click.option("--max-capital", "max_capital", type=float, default=5000, show_default=True)
def sim_run(
    wallet: str,
    rule_name: str | None,
    strategy_name: str | None,
    scenario_name: str,
    max_position: float,
    max_daily_loss: float,
    max_capital: float,
) -> None:
    """Run a counterfactual simulation for one rule and scenario."""
    _validate_selector(rule_name, strategy_name)
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        limits = RiskLimits(
            max_position_per_token=Decimal(str(max_position)),
            max_daily_loss=Decimal(str(max_daily_loss)),
            max_capital_deployed=Decimal(str(max_capital)),
        )
        if strategy_name:
            result = run_strategy_simulation(
                session,
                wallet,
                strategy_name,
                scenario_name,
                risk_limits=limits,
            )
        else:
            result = run_simulation(session, wallet, rule_name or "", scenario_name, risk_limits=limits)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()

    click.echo(
        f"run_id={result.run_id} wallet={result.wallet} "
        f"rule={result.rule_name} strategy={result.strategy_name} "
        f"v{result.rule_version} scenario={result.scenario}"
    )
    click.echo(
        f"  candidate_signals={result.candidate_signals_count} "
        f"accepted_orders={result.accepted_orders_count} "
        f"skipped={result.skipped_orders_count} "
        f"fills={result.simulated_fills_count} "
        f"fill_rate_on_candidates={_fmt_pct(result.fill_rate)}"
    )
    click.echo(
        f"  pnl={_fmt_usdc(result.simulated_pnl)} net_pnl={_fmt_usdc(result.net_pnl)}"
    )
    click.echo(
        f"  max_drawdown={_fmt_usdc(result.max_drawdown)} "
        f"capital_required={_fmt_usdc(result.capital_required)}"
    )
    click.echo(
        f"  risk_breaches={result.risk_breaches} "
        f"risk_prevented={result.risk_prevented_count} "
        f"stale_excluded={result.stale_context_excluded}"
    )
    session = get_session_factory(settings)()
    try:
        lifecycle = fetch_lifecycle_summary(session, result.run_id)
    finally:
        session.close()
    if lifecycle is not None:
        click.echo(
            f"  lifecycle merge_count={lifecycle.merge_count} merged_qty={lifecycle.merged_qty} "
            f"released_capital_total={_fmt_usdc(lifecycle.released_capital_total)} "
            f"capital_recycled_total={_fmt_usdc(lifecycle.capital_recycled_total)}"
        )
        click.echo(
            f"  lifecycle trading_pnl={_fmt_usdc(lifecycle.trading_pnl)} "
            f"merge_pnl={_fmt_usdc(lifecycle.merge_pnl)} "
            f"redeem_pnl={_fmt_usdc(lifecycle.redeem_pnl)} "
            f"unresolved_inventory_value={_fmt_usdc(lifecycle.unresolved_inventory_value)}"
        )
        click.echo(
            f"  lifecycle max_unpaired_inventory={lifecycle.max_unpaired_inventory} "
            f"capital_turnover_ratio={_fmt_decimal(lifecycle.capital_turnover_ratio)}"
        )
    if result.scenario == "conservative":
        gate = "PASS" if result.conservative_pass else "FAIL"
        click.echo(f"  conservative_gate={gate}")
        click.echo(f"  ordering_violation={result.ordering_violation}")


@sim_group.command("compare")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=False, help="Rule name.")
@click.option("--strategy", "strategy_name", required=False, help="Strategy name.")
def sim_compare(wallet: str, rule_name: str | None, strategy_name: str | None) -> None:
    """Run all three scenarios and compare results."""
    _validate_selector(rule_name, strategy_name)
    settings = get_settings()
    ensure_data_dirs(settings)
    results = []
    for scenario in ("optimistic", "medium", "conservative"):
        session = get_session_factory(settings)()
        try:
            if strategy_name:
                results.append(run_strategy_simulation(session, wallet, strategy_name, scenario))
            else:
                results.append(run_simulation(session, wallet, rule_name or "", scenario))
        except ValueError as exc:
            raise click.ClickException(f"{scenario}: {exc}") from exc
        finally:
            session.close()

    click.echo(generate_compare_report(results))
    if strategy_name and is_pattern_strategy(strategy_name):
        session = get_session_factory(settings)()
        try:
            paths = write_pattern_strategy_outputs(
                session,
                wallet=wallet,
                strategy_name=strategy_name,
            )
        finally:
            session.close()
        click.echo(f"wrote {paths['report']}")
        click.echo(f"wrote {paths['candidates']}")
        click.echo(f"wrote {paths['event_robustness']}")
        click.echo(f"wrote {paths['risk_events']}")
        click.echo(f"wrote {paths['parameter_grid']}")
        click.echo(f"wrote {paths['holdout']}")
        click.echo(f"wrote {paths['signal_diagnostics_report']}")
        click.echo(f"wrote {paths['signal_diagnostics']}")
        click.echo(f"wrote {paths['signal_sample_failures']}")
        click.echo(f"wrote {paths['pnl_attribution_report']}")
        click.echo(f"wrote {paths['pnl_attribution_by_event']}")
        click.echo(f"wrote {paths['pnl_attribution_by_condition']}")
        click.echo(f"wrote {paths['fill_ledger_sample']}")
        click.echo(f"wrote {paths['strategy_sanity_checks']}")
        click.echo(f"wrote {paths['redeem_directional_attribution_report']}")
        click.echo(f"wrote {paths['redeem_attribution_by_event']}")
        click.echo(f"wrote {paths['redeem_attribution_by_condition']}")
        click.echo(f"wrote {paths['redeem_attribution_by_token']}")
        click.echo(f"wrote {paths['directional_inventory_timeline']}")


@sim_group.command("report")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=False, help="Rule name.")
@click.option("--strategy", "strategy_name", required=False, help="Strategy name.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
def sim_report(wallet: str, rule_name: str | None, strategy_name: str | None, out_path: Path) -> None:
    """Generate a markdown comparison report for all three scenarios."""
    _validate_selector(rule_name, strategy_name)
    settings = get_settings()
    ensure_data_dirs(settings)
    results = []
    for scenario in ("optimistic", "medium", "conservative"):
        session = get_session_factory(settings)()
        try:
            if strategy_name:
                results.append(run_strategy_simulation(session, wallet, strategy_name, scenario))
            else:
                results.append(run_simulation(session, wallet, rule_name or "", scenario))
        except ValueError as exc:
            raise click.ClickException(f"{scenario}: {exc}") from exc
        finally:
            session.close()

    report = generate_compare_report(results)
    conservative = next((r for r in results if r.scenario == "conservative"), None)
    if conservative is not None:
        session = get_session_factory(settings)()
        try:
            report = report.rstrip() + "\n\n" + generate_attribution_report(session, conservative.run_id)
        finally:
            session.close()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    if strategy_name and is_pattern_strategy(strategy_name):
        session = get_session_factory(settings)()
        try:
            write_pattern_strategy_outputs(
                session,
                wallet=wallet,
                strategy_name=strategy_name,
                out_dir=out_path.parent,
                report_path=out_path,
            )
        finally:
            session.close()
    click.echo(f"report written to {out_path}")


@sim_group.command("attribution")
@click.option("--run-id", "run_id", type=int, required=True, help="Simulation run id.")
@click.option(
    "--by",
    "by",
    type=click.Choice(["market", "event"], case_sensitive=False),
    required=True,
    help="Attribution grouping.",
)
def sim_attribution(run_id: int, by: str) -> None:
    """Show market/event PnL attribution for a simulation run."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        if by.lower() == "market":
            rows = fetch_market_attribution(session, run_id)
        else:
            rows = fetch_event_attribution(session, run_id)
    finally:
        session.close()

    if not rows:
        click.echo(f"No attribution rows found for run_id={run_id}.")
        return

    if by.lower() == "market":
        click.echo(
            "run_id condition_id question event_id fills_count fill_notional "
            "realized_pnl unrealized_pnl total_pnl max_inventory turnover"
        )
        for row in rows:
            click.echo(
                f"{row.run_id} {row.condition_id} {_quote(row.question)} {row.event_id or ''} "
                f"{row.fills_count} {row.fill_notional} {row.realized_pnl} "
                f"{row.unrealized_pnl} {row.total_pnl} {row.max_inventory} {row.turnover}"
            )
        return

    click.echo(
        "run_id event_id event_title markets_count fills_count fill_notional "
        "realized_pnl unrealized_pnl total_pnl max_event_exposure turnover"
    )
    for row in rows:
        click.echo(
            f"{row.run_id} {row.event_id} {_quote(row.event_title)} {row.markets_count} "
            f"{row.fills_count} {row.fill_notional} {row.realized_pnl} "
            f"{row.unrealized_pnl} {row.total_pnl} {row.max_event_exposure} {row.turnover}"
        )


@sim_group.command("search")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=True, help="Search rule.")
@click.option("--max-combos", "max_combos", type=int, required=True, help="Maximum parameter combinations.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
def sim_search(wallet: str, rule_name: str, max_combos: int, out_path: Path) -> None:
    """Run Phase 22.2 train/validation/test strategy search."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    last_progress = {"completed": 0}

    def progress(completed: int, total: int, eligible: int, elapsed_s: float) -> None:
        if completed != total and completed != 1 and completed - last_progress["completed"] < 10:
            return
        last_progress["completed"] = completed
        rate = completed / elapsed_s if elapsed_s > 0 else 0
        remaining = total - completed
        eta_s = remaining / rate if rate > 0 else 0
        click.echo(
            f"progress {completed}/{total} ({completed / total:.1%}) "
            f"eligible={eligible} elapsed={elapsed_s:.1f}s eta={eta_s:.1f}s",
            err=True,
        )

    try:
        result = run_strategy_search(
            session,
            wallet,
            rule_name,
            max_combos=max_combos,
            progress_callback=progress,
        )
        write_search_report(result, out_path)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()

    click.echo(
        f"search_run_id={result.run_id} wallet={result.wallet} rule={result.rule_name} "
        f"evaluated={result.evaluated_combos}/{result.total_combos}"
    )
    if result.selected_candidate_id is None:
        click.echo("no eligible candidate")
    else:
        top = result.ranked_candidates[0]
        train = top.metrics["train"]
        validation = top.metrics["validation"]
        test = top.metrics["test"]
        click.echo(
            f"top_candidate={top.candidate_id} rank=1 "
            f"score={_fmt_decimal(selection_score(top))} "
            f"train_net_pnl={_fmt_usdc(train.net_pnl)} "
            f"validation_net_pnl={_fmt_usdc(validation.net_pnl)} "
            f"test_net_pnl={_fmt_usdc(test.net_pnl)} "
            f"train_fills={train.simulated_fills_count} "
            f"validation_fills={validation.simulated_fills_count} "
            f"test_fills={test.simulated_fills_count} "
            f"final_status={final_status(top)}"
        )
    click.echo(f"report written to {out_path}")


@sim_group.command("top")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=True, help="Search rule.")
@click.option("--limit", "limit", type=int, required=True, help="Maximum candidates to show.")
@click.option(
    "--eligible-only",
    "eligible_only",
    is_flag=True,
    help="Show only train+validation selected candidates that also pass test holdout.",
)
def sim_top(wallet: str, rule_name: str, limit: int, eligible_only: bool) -> None:
    """Show top eligible candidates from the latest search run."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        candidates = top_candidates(
            session,
            wallet,
            rule_name,
            limit=limit,
            eligible_only=eligible_only,
            min_test_fills=DEFAULT_MIN_TEST_FILLS,
        )
    finally:
        session.close()

    if not candidates:
        click.echo("No eligible candidates found.")
        return

    click.echo(
        "rank candidate score train_net_pnl validation_net_pnl test_net_pnl "
        "train_fills validation_fills test_fills validation_pass test_pass final_status"
    )
    for candidate in candidates:
        train = candidate.metrics["train"]
        validation = candidate.metrics["validation"]
        test = candidate.metrics["test"]
        click.echo(
            f"{candidate.rank_index} {candidate.candidate_id} "
            f"{_fmt_decimal(selection_score(candidate))} "
            f"{_fmt_usdc(train.net_pnl)} {_fmt_usdc(validation.net_pnl)} {_fmt_usdc(test.net_pnl)} "
            f"{train.simulated_fills_count} {validation.simulated_fills_count} {test.simulated_fills_count} "
            f"{_fmt_bool(candidate_validation_passes(candidate))} {_fmt_bool(candidate_test_passes(candidate))} "
            f"{final_status(candidate)}"
        )


@sim_group.command("report-search")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=True, help="Search rule.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
def sim_report_search(wallet: str, rule_name: str, out_path: Path) -> None:
    """Write a report for the latest Phase 22.2 search run."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        result = fetch_latest_search(session, wallet, rule_name)
    finally:
        session.close()

    if result is None:
        raise click.ClickException("No search run found for wallet/rule.")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(generate_search_report(result), encoding="utf-8")
    click.echo(f"report written to {out_path}")


@sim_group.command("holdout-failure")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option("--rule", "rule_name", required=True, help="Search rule.")
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path), required=True)
@click.option("--search-run-id", "search_run_id", type=int, required=False, help="Specific search run id.")
def sim_holdout_failure(
    wallet: str,
    rule_name: str,
    out_dir: Path,
    search_run_id: int | None,
) -> None:
    """Write Phase 22.3 holdout failure attribution diagnostics."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        diagnostics = write_holdout_failure_outputs(
            session,
            wallet,
            rule_name,
            out_dir,
            search_run_id=search_run_id,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()

    click.echo(
        f"holdout_failure search_run_id={diagnostics.search_run.run_id} "
        f"candidate={diagnostics.candidate.candidate_id} "
        f"status={final_status(diagnostics.candidate)} "
        f"test_net_pnl={_fmt_usdc(diagnostics.candidate.metrics['test'].net_pnl)}"
    )
    for filename in (
        REPORT_FILENAME,
        CONDITION_FILENAME,
        PRICE_BUCKET_FILENAME,
        BOOK_AGE_FILENAME,
        SIDE_FILENAME,
        TIME_BUCKET_FILENAME,
    ):
        click.echo(f"wrote {out_dir / filename}")


@sim_group.command("composite-search")
@click.option("--out-dir", "out_dir", type=click.Path(path_type=Path), required=True)
@click.option("--wallet", "wallet", default="all", show_default=True, help="Wallet scope or 'all'.")
@click.option(
    "--strategy-family",
    "strategy_family",
    type=click.Choice(["composite", "event_inventory_cycling"], case_sensitive=False),
    default="composite",
    show_default=True,
)
@click.option("--max-components", "max_components", type=int, required=True)
@click.option("--max-candidates", "max_candidates", type=int, required=True)
@click.option("--seed", "seed", type=int, default=2204, show_default=True)
@click.option(
    "--capital-mode",
    "capital_mode",
    type=click.Choice(["small", "scaled"], case_sensitive=False),
    required=True,
)
@click.option("--max-capital", "max_capital", type=float, required=True)
@click.option("--max-order-size", "max_order_size", type=float, required=True)
@click.option("--min-events", "min_events", type=int, required=True)
@click.option("--min-fills", "min_fills", type=int, required=True)
def sim_composite_search(
    out_dir: Path,
    wallet: str,
    strategy_family: str,
    max_components: int,
    max_candidates: int,
    seed: int,
    capital_mode: str,
    max_capital: float,
    max_order_size: float,
    min_events: int,
    min_fills: int,
) -> None:
    """Run Phase 22.4 progressive composite strategy search."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    last_progress = {"completed": 0, "ts": 0.0}

    def progress(kind: str, completed: int, total: int, stage: int, promoted: int, elapsed_s: float) -> None:
        should_print = (
            completed in {0, 1, total}
            or kind == "promote"
            or completed - last_progress["completed"] >= 10
            or elapsed_s - last_progress["ts"] >= 30
        )
        if not should_print:
            return
        last_progress["completed"] = completed
        last_progress["ts"] = elapsed_s
        rate = completed / elapsed_s if elapsed_s > 0 else 0
        remaining = max(total - completed, 0)
        eta_s = remaining / rate if rate > 0 else 0
        click.echo(
            f"progress phase22.4 {kind} stage={stage} evaluated={completed}/{total} "
            f"promoted={promoted} elapsed={elapsed_s:.1f}s eta={eta_s:.1f}s",
            err=True,
        )

    try:
        result = run_progressive_composite_search(
            session,
            max_components=max_components,
            max_candidates=max_candidates,
            seed=seed,
            capital_mode=capital_mode,
            max_capital=Decimal(str(max_capital)),
            max_order_size=Decimal(str(max_order_size)),
            min_events=min_events,
            min_fills=min_fills,
            wallet=wallet,
            strategy_family=strategy_family,
            progress_callback=progress,
        )
        write_composite_outputs(result, out_dir)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    finally:
        session.close()

    selected = result.selected_candidate
    click.echo(
        f"composite_search evaluated={result.evaluated_candidates}/{result.max_candidates} "
        f"ranked={len(result.ranked_candidates)} elapsed_ms={result.elapsed_ms}"
    )
    if selected is None:
        click.echo("selected_candidate=none")
    else:
        validation = selected.metrics["validation"]
        test = selected.metrics["test"]
        click.echo(
            f"selected_candidate={selected.candidate_id} rank={selected.rank_index} "
            f"components={selected.component_count} "
            f"validation_score={_fmt_decimal(selected.validation_score)} "
            f"validation_net_pnl={_fmt_usdc(validation.net_pnl)} "
            f"test_net_pnl={_fmt_usdc(test.net_pnl)} "
            f"rn1_similarity={_fmt_decimal(selected.rn1_similarity_score)} "
            f"gap_similarity={_fmt_decimal(selected.gap_similarity_score)} "
            f"final_status={selected.final_status}"
        )
    for filename in (
        COMPOSITE_REPORT_FILENAME,
        COMPOSITE_CANDIDATES_FILENAME,
        COMPOSITE_TOP_FILENAME,
        FORWARD_WATCH_FILENAME,
        COMPONENT_EFFECTIVENESS_REPORT_FILENAME,
        COMPONENT_EFFECTIVENESS_FILENAME,
        PER_CANDIDATE_EVENT_PNL_FILENAME,
        SKIPPED_BY_COMPONENT_FILENAME,
        COMPONENT_CONTRIBUTION_FILENAME,
        EVENT_ROBUSTNESS_FILENAME,
    ):
        click.echo(f"wrote {out_dir / filename}")


def _fmt_usdc(value) -> str:
    if value is None:
        return "-"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"${value:+.2f}"


def _fmt_pct(value) -> str:
    if value is None:
        return "-"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value * 100:.1f}%"


def _fmt_decimal(value) -> str:
    if value is None:
        return "-"
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return f"{value:.6f}"


def _fmt_bool(value: bool) -> str:
    return "true" if value else "false"


def _quote(value: str | None) -> str:
    if value is None:
        return '""'
    return '"' + value.replace('"', '\\"') + '"'


def _validate_selector(rule_name: str | None, strategy_name: str | None) -> None:
    if bool(rule_name) == bool(strategy_name):
        raise click.ClickException("Provide exactly one of --rule or --strategy.")
