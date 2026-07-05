"""pmr evidence: read-only reproducible evidence audits over the ledger."""

from __future__ import annotations

from pathlib import Path

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..evidence.completion_sets import analyze_completion_sets, write_audit
from ..evidence.phase18_acceptance import analyze_phase18, write_reports
from ..evidence.phase20_acceptance import write_reports as write_phase20_reports

RN1_DEFAULT = "0x2005d16a84ceefa912d4e380cd32e7ff827875ea"


@click.group("evidence")
def evidence_group() -> None:
    """Reproducible read-only evidence audits."""


@evidence_group.command("rn1-completion-sets")
@click.option("--wallet", "wallet", default=RN1_DEFAULT, show_default=True)
@click.option(
    "--out", "out", type=click.Path(path_type=Path),
    default=Path("docs/evidence/rn1_completion_sets/"), show_default=True,
)
def rn1_completion_sets(wallet: str, out: Path) -> None:
    """Audit the completion-set / inventory-cycling hypothesis for a wallet."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        audit = analyze_completion_sets(session, wallet)
        paths = write_audit(audit, out)
    finally:
        session.close()

    s = audit.summary
    o, t = s["orphan"], s["temporal"]
    total_merge_edge = sum(float(r["realized_edge_usdc"]) for r in audit.merge_edge)
    total_merge_sets = sum(float(r["merge_sets"]) for r in audit.merge_edge)
    bridge = {r["scope"]: r for r in audit.pnl_bridge}

    click.echo(f"\nWallet: {wallet}")
    click.echo(f"Binary markets: {audit.coverage['binary_condition_ids']:,} "
               f"of {audit.coverage['ledger_condition_ids']:,} ledger conditions")

    click.echo("\n== MERGE realized edge ==")
    click.echo(f"  total realized edge: ${total_merge_edge:,.2f} over "
               f"{total_merge_sets:,.0f} sets")
    if total_merge_sets:
        eps = total_merge_edge / total_merge_sets
        click.echo(f"  weighted edge/set: {eps*100:.4f}c ({eps*10000:.1f} bps)")

    click.echo("\n== REDEEM / orphan summary ==")
    click.echo(f"  resolved markets: {o['resolved_markets']:,} "
               f"(unresolved: {o['unresolved_markets']:,})")
    click.echo(f"  unmatched winner qty: {float(o['unmatched_winner_qty']):,.0f} | "
               f"loser qty: {float(o['unmatched_loser_qty']):,.0f}")
    click.echo(f"  ambiguous matched-at-resolution qty: "
               f"{float(o['ambiguous_matched_qty']):,.0f}")
    click.echo(f"  residual winner share (by qty): "
               f"{float(o['pct_unmatched_residual_winner_by_qty']):.1f}%")

    click.echo("\n== Temporal imbalance summary ==")
    click.echo(f"  matched lots: {t['n_matched_lots']:,}")
    click.echo(f"  completed <=60s: {float(t['pct_under_60s']):.1f}% | "
               f"<=300s: {float(t['pct_under_300s']):.1f}%")
    click.echo(f"  seconds between legs p50/p90/p99: "
               f"{t['p50_seconds']:,} / {t['p90_seconds']:,} / {t['p99_seconds']:,}")
    click.echo(f"  imbalance ratio p50/p90: "
               f"{float(t['imbalance_ratio_p50']):.3f} / {float(t['imbalance_ratio_p90']):.3f}")

    click.echo("\n== PnL bridge (all) ==")
    b = bridge.get("all", {})
    for k in ("realized_merge_edge_usdc", "realized_redeem_edge_usdc",
              "realized_directional_pnl", "completion_mechanics_pnl",
              "ledger_gross_pnl", "pct_gross_from_completion",
              "estimated_fees", "reconstructed_net_after_fees",
              "xcheck_merge_delta", "xcheck_redeem_delta"):
        click.echo(f"  {k}: {b.get(k, 'NA')}")

    click.echo("\n== Files written ==")
    for p in paths:
        click.echo(f"  {p}")


@evidence_group.command("phase18-acceptance")
@click.option(
    "--out", "out", type=click.Path(path_type=Path),
    default=Path("docs/evidence/phase18_acceptance/"), show_default=True,
)
def phase18_acceptance(out: Path) -> None:
    """Close out Phase 18: check acceptance criteria and emit its outputs."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        acc = analyze_phase18(session, settings)
        paths = write_reports(session, settings, acc, out)
    finally:
        session.close()

    click.echo(
        f"\n=== Phase 18 Acceptance ({acc.passed}/{len(acc.checks)} passed) ===\n"
    )
    for c in acc.checks:
        icon = {"pass": "PASS", "fail": "FAIL", "warn": "WARN"}[c.status]
        click.echo(f"  [{icon}] {c.title}")
        click.echo(f"         {c.evidence}")

    click.echo("\n== Book-before-fill coverage (exit question) ==")
    for cov in acc.coverage:
        label = cov.label or cov.wallet[:10] + "…"
        click.echo(
            f"  {label}: {cov.total} fills | strict={cov.strict} "
            f"({cov.strict_share * 100:.1f}%) loose={cov.loose} "
            f"({cov.loose_share * 100:.1f}%)"
        )

    click.echo("\n== Files written ==")
    for p in paths:
        click.echo(f"  {p}")

    if acc.all_pass:
        click.echo("\nPhase 18 acceptance: PASS (no failing checks).")
    else:
        click.echo("\nPhase 18 acceptance: FAIL (see failing checks above).")


@evidence_group.command("phase20-report")
@click.option("--wallet", "wallet", default=RN1_DEFAULT, show_default=True)
@click.option(
    "--out", "out", type=click.Path(path_type=Path),
    default=Path("docs/evidence/phase20_dataset/"), show_default=True,
)
def phase20_report(wallet: str, out: Path) -> None:
    """Emit Phase 20 dataset quality reports for an already-built wallet dataset."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        paths = write_phase20_reports(session, wallet, out)
    finally:
        session.close()

    click.echo(f"\nWallet: {wallet}")
    click.echo("== Files written ==")
    for p in paths:
        click.echo(f"  {p}")
