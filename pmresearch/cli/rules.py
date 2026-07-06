"""pmr rules: Phase 21 interpretable rule reconstruction."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..rules.candidate_rules import (
    ALL_CANDIDATE_RULES,
    default_rule_instances,
)
from ..rules.evaluate import (
    evaluate_rule,
    explain_fill,
    fetch_candidates,
    fetch_stored_fit_results,
    fit_result_from_eval,
    store_fit_result,
)
from ..rules.fit import fit_rules
from ..rules.report import (
    export_fill_details_csv,
    generate_all_report,
    generate_rule_report,
)


def _get_rule_by_name(name: str):
    """Resolve a rule name to a default-instance rule object."""
    for inst in default_rule_instances():
        if inst.name == name:
            return inst
    return None


@click.group("rules")
def rules_group() -> None:
    """Phase 21: interpretable rule reconstruction."""


@rules_group.command("list")
def rules_list() -> None:
    """List all candidate rules."""
    for inst in default_rule_instances():
        click.echo(
            f"{inst.name:<32s} v{inst.version}  "
            f"params={inst.parameters}"
        )


@rules_group.command("fit")
@click.option("--wallet", "wallet", required=True, help="Wallet address.")
@click.option(
    "--rule", "rule_names", multiple=True,
    help="Rule name(s) to fit. Repeat for multiple. Default: all.",
)
@click.option(
    "--train-ratio", "train_ratio", type=float, default=0.6, show_default=True,
)
@click.option(
    "--validation-ratio", "validation_ratio", type=float, default=0.2, show_default=True,
)
@click.option("--store", "store_results", is_flag=True, default=False,
              help="Persist results to strategy_candidates / rule_evaluations.")
def rules_fit(
    wallet: str,
    rule_names: tuple[str, ...],
    train_ratio: float,
    validation_ratio: float,
    store_results: bool,
) -> None:
    """Fit candidate rules with temporal validation."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        names = list(rule_names) if rule_names else None
        stats = fit_rules(
            session,
            wallet,
            rule_names=names,
            train_ratio=train_ratio,
            validation_ratio=validation_ratio,
        )
    finally:
        session.close()

    click.echo(
        f"wallet={stats.wallet} fills={stats.total_fills} "
        f"evaluated={stats.candidates_evaluated} promoted={stats.candidates_promoted}"
    )
    for r in stats.results:
        status = "PROMOTED" if r.promoted else "not promoted"
        click.echo(
            f"  {r.rule_name:<30s} v{r.rule_version}  "
            f"train={r.train.fill_explained_rate:.3f} "
            f"val={r.validation.fill_explained_rate:.3f} "
            f"test={r.test.fill_explained_rate:.3f}  "
            f"test_prec={r.test.precision:.3f}  [{status}]"
        )
        if store_results:
            session2 = get_session_factory(settings)()
            try:
                store_fit_result(session2, wallet, r)
            finally:
                session2.close()

    if store_results:
        click.echo("Results stored to strategy_candidates / rule_evaluations.")


@rules_group.command("evaluate")
@click.option("--wallet", "wallet", required=True)
@click.option("--rule", "rule_name", required=True, help="Rule name to evaluate.")
@click.option(
    "--train-ratio", "train_ratio", type=float, default=0.6, show_default=True,
)
@click.option(
    "--validation-ratio", "validation_ratio", type=float, default=0.2, show_default=True,
)
def rules_evaluate(wallet: str, rule_name: str, train_ratio: float, validation_ratio: float) -> None:
    """Evaluate a single rule on a wallet's dataset."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rule = _get_rule_by_name(rule_name)
        if rule is None:
            click.echo(f"Unknown rule: {rule_name!r}")
            return
        result = evaluate_rule(
            session, wallet, rule,
            train_ratio=train_ratio, validation_ratio=validation_ratio,
        )
    finally:
        session.close()

    click.echo(f"wallet={result.wallet} rule={result.rule_name} v{result.rule_version}")
    click.echo(f"explained_pct={result.explained_fills_pct:.4f} promoted={result.promoted}")
    click.echo(
        f"  train: fills={result.train.total_fills} explained={result.train.explained_fills} "
        f"rate={result.train.fill_explained_rate:.3f} prec={result.train.precision:.3f}"
    )
    click.echo(
        f"  val:   fills={result.validation.total_fills} explained={result.validation.explained_fills} "
        f"rate={result.validation.fill_explained_rate:.3f} prec={result.validation.precision:.3f}"
    )
    click.echo(
        f"  test:  fills={result.test.total_fills} explained={result.test.explained_fills} "
        f"rate={result.test.fill_explained_rate:.3f} prec={result.test.precision:.3f}"
    )
    if result.test.avg_markout_5m is not None:
        click.echo(f"  test markout_5m={result.test.avg_markout_5m}")
    if result.test.avg_pnl_episode is not None:
        click.echo(f"  test avg_pnl_episode={result.test.avg_pnl_episode}")


@rules_group.command("explain-fill")
@click.option("--event-id", "event_id", type=int, required=True, help="Fill event_id.")
@click.option("--rule", "rule_name", required=True, help="Rule name.")
def rules_explain_fill(event_id: int, rule_name: str) -> None:
    """Explain why a specific fill was or wasn't matched by a rule."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rule = _get_rule_by_name(rule_name)
        if rule is None:
            click.echo(f"Unknown rule: {rule_name!r}")
            return
        result = explain_fill(session, event_id, rule)
    finally:
        session.close()

    if result is None:
        click.echo(f"No fill found for event_id={event_id}")
        return

    click.echo(f"event_id={result.event_id} wallet={result.wallet}")
    click.echo(f"rule={result.rule_name} v{result.rule_version}")
    click.echo(f"applies={result.applies}")
    click.echo(f"explanation: {result.explanation}")
    click.echo("features_used:")
    for k, v in result.features_used.items():
        click.echo(f"  {k}: {v}")
    click.echo("fill_context:")
    for k, v in result.fill_context.items():
        click.echo(f"  {k}: {v}")


@rules_group.command("report")
@click.option("--wallet", "wallet", required=True)
@click.option("--rule", "rule_name", required=True, help="Rule name.")
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
@click.option(
    "--train-ratio", "train_ratio", type=float, default=0.6, show_default=True,
)
@click.option(
    "--validation-ratio", "validation_ratio", type=float, default=0.2, show_default=True,
)
def rules_report(
    wallet: str,
    rule_name: str,
    out_path: Path,
    train_ratio: float,
    validation_ratio: float,
) -> None:
    """Generate a markdown report for a rule evaluation."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rule = _get_rule_by_name(rule_name)
        if rule is None:
            click.echo(f"Unknown rule: {rule_name!r}")
            return
        result = evaluate_rule(
            session, wallet, rule,
            train_ratio=train_ratio, validation_ratio=validation_ratio,
        )
    finally:
        session.close()

    report = generate_rule_report(fit_result_from_eval(result))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    click.echo(f"report written to {out_path}")


@rules_group.command("report-all")
@click.option("--wallet", "wallet", required=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
@click.option(
    "--train-ratio", "train_ratio", type=float, default=0.6, show_default=True,
)
@click.option(
    "--validation-ratio", "validation_ratio", type=float, default=0.2, show_default=True,
)
@click.option(
    "--fresh",
    "--recompute",
    "recompute",
    is_flag=True,
    default=False,
    help="Recompute rule evaluations instead of using stored strategy_candidates.",
)
def rules_report_all(
    wallet: str,
    out_path: Path,
    train_ratio: float,
    validation_ratio: float,
    recompute: bool,
) -> None:
    """Generate a consolidated report for all candidate rules."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        results = [] if recompute else fetch_stored_fit_results(session, wallet)
        if not results:
            click.echo(
                "Recomputing rule evaluations; not using stored strategy_candidates."
            )
            for inst in default_rule_instances():
                result = evaluate_rule(
                    session, wallet, inst,
                    train_ratio=train_ratio, validation_ratio=validation_ratio,
                )
                results.append(fit_result_from_eval(result))
    finally:
        session.close()

    report = generate_all_report(results)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    click.echo(f"report written to {out_path}")


@rules_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--promoted-only", "promoted_only", is_flag=True, default=False)
def rules_show(wallet: str, promoted_only: bool) -> None:
    """Show stored strategy_candidates for a wallet."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        candidates = fetch_candidates(session, wallet, promoted_only=promoted_only)
    finally:
        session.close()

    if not candidates:
        click.echo(f"No strategy_candidates for wallet={wallet.lower()}")
        return

    for c in candidates:
        status = "PROMOTED" if c.promoted else "not promoted"
        click.echo(
            f"{c.rule_name:<30s} v{c.rule_version}  [{status}]  "
            f"explained={c.explained_fills_pct}"
        )
        click.echo(f"  params: {c.parameters}")
        click.echo(f"  features: {', '.join(c.features_used)}")
        click.echo(f"  fitted_at: {c.fitted_at}")


@rules_group.command("export-explained")
@click.option("--wallet", "wallet", required=True)
@click.option("--rule", "rule_name", required=True)
@click.option("--out", "out_path", type=click.Path(path_type=Path), required=True)
@click.option("--explained-only", "explained_only", is_flag=True, default=False)
@click.option(
    "--train-ratio", "train_ratio", type=float, default=0.6, show_default=True,
)
@click.option(
    "--validation-ratio", "validation_ratio", type=float, default=0.2, show_default=True,
)
def rules_export(
    wallet: str,
    rule_name: str,
    out_path: Path,
    explained_only: bool,
    train_ratio: float,
    validation_ratio: float,
) -> None:
    """Export fill details (explained/unexplained) to CSV."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rule = _get_rule_by_name(rule_name)
        if rule is None:
            click.echo(f"Unknown rule: {rule_name!r}")
            return
        result = evaluate_rule(
            session, wallet, rule,
            train_ratio=train_ratio, validation_ratio=validation_ratio,
        )
    finally:
        session.close()

    n = export_fill_details_csv(result.fill_details, out_path, explained_only=explained_only)
    click.echo(f"rows_written={n} out={out_path}")
