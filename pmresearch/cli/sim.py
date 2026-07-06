"""pmr sim: Phase 22 counterfactual simulation commands."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..simulation.engine import run_simulation, run_strategy_simulation
from ..simulation.report import generate_compare_report
from ..simulation.risk import RiskLimits


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
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    click.echo(f"report written to {out_path}")


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


def _validate_selector(rule_name: str | None, strategy_name: str | None) -> None:
    if bool(rule_name) == bool(strategy_name):
        raise click.ClickException("Provide exactly one of --rule or --strategy.")
