"""Markdown report generation for Phase 22 simulations."""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .engine import SimRunResult


def generate_sim_report(result: SimRunResult) -> str:
    """Generate a markdown report for a single simulation run."""
    lines = [
        f"# Simulation Report: {result.rule_name} v{result.rule_version}",
        "",
        f"- **Wallet:** `{result.wallet}`",
        f"- **Rule:** {result.rule_name} v{result.rule_version}",
        f"- **Strategy:** {result.strategy_name}",
        f"- **Scenario:** {result.scenario}",
        f"- **Run ID:** {result.run_id}",
        f"- **Elapsed:** {result.elapsed_ms} ms",
        "",
        "## Assumptions",
        "",
        *_scenario_assumptions(result.scenario),
        "",
        "## Order and Fill Summary",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Candidate signals | {result.candidate_signals_count} |",
        f"| Accepted orders | {result.accepted_orders_count} |",
        f"| Skipped candidates | {result.skipped_orders_count} |",
        f"| Simulated fills | {result.simulated_fills_count} |",
        f"| Fill rate on candidates | {_fmt_pct(result.fill_rate)} |",
        f"| Risk prevented | {result.risk_prevented_count} |",
        f"| Stale context excluded | {result.stale_context_excluded} |",
        "",
        "## PnL and Risk",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Simulated PnL | {_fmt_usdc(result.simulated_pnl)} |",
        f"| Net PnL (after fees) | {_fmt_usdc(result.net_pnl)} |",
        f"| Max drawdown | {_fmt_usdc(result.max_drawdown)} |",
        f"| Max inventory | {result.max_inventory:.2f} shares |",
        f"| Capital required | {_fmt_usdc(result.capital_required)} |",
        f"| Turnover | {_fmt_usdc(result.turnover)} |",
        f"| Risk breaches | {result.risk_breaches} |",
        f"| Ordering violation | {'yes' if result.ordering_violation else 'no'} |",
        "",
    ]

    if result.scenario == "conservative":
        lines.extend(_conservative_gate_lines(result))

    lines.extend(
        [
            "",
            "## Scenario Comparison",
            "",
            "_Run `pmr sim compare` to see the full three-scenario comparison._",
            "",
        ]
    )
    return "\n".join(lines)


def generate_compare_report(results: list[SimRunResult]) -> str:
    """Generate a comparison report across optimistic, medium, and conservative."""
    if not results:
        return "# No simulation results to compare.\n"

    by_scenario = {r.scenario: r for r in results}
    lines = [
        "# Scenario Comparison",
        "",
        f"- **Wallet:** `{results[0].wallet}`",
        f"- **Rule:** {results[0].rule_name} v{results[0].rule_version}",
        f"- **Strategy:** {results[0].strategy_name}",
        "",
        "## Metrics by Scenario",
        "",
        "| Metric | Optimistic | Medium | Conservative |",
        "|--------|-----------|--------|-------------|",
    ]

    def row(label: str, attr: str, fmt: str = "usdc") -> str:
        vals: list[str] = []
        for scenario in ("optimistic", "medium", "conservative"):
            result = by_scenario.get(scenario)
            if result is None:
                vals.append("-")
                continue
            value = getattr(result, attr)
            if fmt == "pct":
                vals.append(_fmt_pct(value))
            elif fmt == "int":
                vals.append(str(value))
            elif fmt == "bool":
                vals.append("yes" if value else "no")
            else:
                vals.append(_fmt_usdc(value) if isinstance(value, Decimal) else str(value))
        return f"| {label} | {vals[0]} | {vals[1]} | {vals[2]} |"

    lines.append(row("Candidate signals", "candidate_signals_count", "int"))
    lines.append(row("Accepted orders", "accepted_orders_count", "int"))
    lines.append(row("Skipped candidates", "skipped_orders_count", "int"))
    lines.append(row("Simulated fills", "simulated_fills_count", "int"))
    lines.append(row("Fill rate on candidates", "fill_rate", "pct"))
    lines.append(row("Risk prevented", "risk_prevented_count", "int"))
    lines.append(row("Simulated PnL", "simulated_pnl"))
    lines.append(row("Net PnL", "net_pnl"))
    lines.append(row("Max drawdown", "max_drawdown"))
    lines.append(row("Max inventory", "max_inventory"))
    lines.append(row("Capital required", "capital_required"))
    lines.append(row("Turnover", "turnover"))
    lines.append(row("Risk breaches", "risk_breaches", "int"))
    lines.append(row("Stale excluded", "stale_context_excluded", "int"))
    lines.append(row("Ordering violation", "ordering_violation", "bool"))

    lines.extend(["", "## Conservative Gate", ""])
    conservative = by_scenario.get("conservative")
    if conservative is None:
        lines.append("No conservative scenario result found.")
    else:
        lines.extend(_conservative_gate_lines(conservative, include_heading=False))

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "- Optimistic has highest fillability and base risk limits.",
            "- Medium applies lower fillability, slippage, and tighter risk limits.",
            "- Conservative applies lowest fillability, worst slippage, and strict risk limits.",
            "",
            "Conservative must pass for the rule to advance to paper trading.",
            "",
        ]
    )
    return "\n".join(lines)


def fetch_run_result(session: Session, run_id: int) -> Optional[SimRunResult]:
    row = session.execute(
        text("SELECT * FROM simulation_runs WHERE id = :id"),
        {"id": run_id},
    ).mappings().fetchone()
    return None if row is None else _row_to_result(dict(row))


def fetch_latest_runs(session: Session, wallet: str, rule_name: str) -> list[SimRunResult]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_runs "
            "WHERE wallet = :w AND rule_name = :r "
            "ORDER BY run_ts DESC, id DESC"
        ),
        {"w": wallet.lower(), "r": rule_name},
    ).mappings().fetchall()
    seen: dict[str, SimRunResult] = {}
    for row in rows:
        result = _row_to_result(dict(row))
        seen.setdefault(result.scenario, result)
    return [seen[s] for s in ("optimistic", "medium", "conservative") if s in seen]


def fetch_latest_strategy_runs(session: Session, wallet: str, strategy_name: str) -> list[SimRunResult]:
    rows = session.execute(
        text(
            "SELECT * FROM simulation_runs "
            "WHERE wallet = :w AND strategy_name = :s "
            "ORDER BY run_ts DESC, id DESC"
        ),
        {"w": wallet.lower(), "s": strategy_name.lower()},
    ).mappings().fetchall()
    seen: dict[str, SimRunResult] = {}
    for row in rows:
        result = _row_to_result(dict(row))
        seen.setdefault(result.scenario, result)
    return [seen[s] for s in ("optimistic", "medium", "conservative") if s in seen]


def _row_to_result(row: dict) -> SimRunResult:
    return SimRunResult(
        run_id=row["id"],
        wallet=row["wallet"],
        rule_name=row["rule_name"],
        strategy_name=row.get("strategy_name") or row["rule_name"],
        base_rule=row["rule_name"],
        rule_version=row["rule_version"],
        scenario=row["scenario"],
        parameters=_json_dict(row.get("parameters_json")),
        risk_limits=_json_dict(row.get("risk_limits_json")),
        candidate_signals_count=(row.get("orders_count") or 0) + (row.get("skipped_orders_count") or 0),
        accepted_orders_count=row["orders_count"],
        orders_count=row["orders_count"],
        simulated_fills_count=row["simulated_fills_count"],
        fill_rate=Decimal(row["fill_rate"]) if row.get("fill_rate") else None,
        simulated_pnl=_decimal_or_zero(row.get("simulated_pnl")),
        net_pnl=_decimal_or_zero(row.get("net_pnl")),
        max_drawdown=_decimal_or_zero(row.get("max_drawdown")),
        max_inventory=_decimal_or_zero(row.get("max_inventory")),
        capital_required=_decimal_or_zero(row.get("capital_required")),
        turnover=_decimal_or_zero(row.get("turnover")),
        skipped_orders_count=row.get("skipped_orders_count") or 0,
        skipped_by_reason=_json_int_dict(row.get("skipped_by_reason_json")),
        risk_prevented_count=row.get("risk_prevented_count") or 0,
        risk_breaches=row["risk_breaches"],
        stale_context_excluded=row["stale_context_excluded"],
        conservative_pass=bool(row["conservative_pass"]),
        ordering_violation=bool(row.get("ordering_violation", 0)),
        elapsed_ms=row.get("elapsed_ms") or 0,
    )


def _scenario_assumptions(scenario: str) -> list[str]:
    if scenario == "optimistic":
        return [
            "Fillability: highest.",
            "Fill price: prospective order price from book-before.",
            "Additional slippage: 0 bps.",
        ]
    if scenario == "medium":
        return [
            "Fillability: around 40% deterministic acceptance.",
            "Fill price: book-before order price plus 15 bps adverse slippage.",
            "Requires usable context and tighter risk limits.",
        ]
    if scenario == "conservative":
        return [
            "Fillability: around 20% deterministic acceptance.",
            "Fill price: book-before order price plus 30 bps adverse slippage.",
            "Requires good, fresh context, minimum depth, and strict risk limits.",
        ]
    return [f"Scenario: {scenario}"]


def _conservative_gate_lines(result: SimRunResult, *, include_heading: bool = True) -> list[str]:
    lines = ["## Conservative Gate", ""] if include_heading else []
    if result.conservative_pass:
        lines.append(
            "**PASS** - Conservative scenario is profitable with no risk breaches. "
            "Rule is eligible for paper trading."
        )
        return lines

    lines.append(
        "**FAIL** - Conservative scenario does not pass. "
        "Rule is NOT eligible for paper trading."
    )
    reasons: list[str] = []
    if result.net_pnl <= Decimal("0"):
        reasons.append(f"net PnL is non-positive ({_fmt_usdc(result.net_pnl)})")
    if result.risk_breaches > 0:
        reasons.append(f"{result.risk_breaches} risk limit breach(es) occurred")
    if result.ordering_violation:
        reasons.append("conservative result was better than optimistic")
    if result.simulated_fills_count <= 0:
        reasons.append("no simulated fills occurred")
    if reasons:
        lines.extend(["", "Reasons:"])
        lines.extend(f"- {reason}" for reason in reasons)
    return lines


def _json_dict(raw: Optional[str]) -> dict:
    if not raw:
        return {}
    import json

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _json_int_dict(raw: Optional[str]) -> dict[str, int]:
    parsed = _json_dict(raw)
    result: dict[str, int] = {}
    for key, value in parsed.items():
        try:
            result[str(key)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def _decimal_or_zero(value) -> Decimal:
    if value is None:
        return Decimal("0")
    return Decimal(str(value))


def _fmt_usdc(v: Optional[Decimal]) -> str:
    if v is None:
        return "-"
    return f"${v:+.2f}"


def _fmt_pct(v: Optional[Decimal]) -> str:
    if v is None:
        return "-"
    return f"{v * 100:.1f}%"
