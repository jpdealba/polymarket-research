"""pmr detect: run strategy detectors and inspect their scored labels."""

from __future__ import annotations

import json
from decimal import Decimal

import click

from ..config import ensure_data_dirs, get_settings
from ..db.engine import get_session_factory
from ..detectors.compute import fetch_labels, label_scopes, run_detectors
from ..ledger.replay import ledger_wallets


@click.group("detect")
def detect_group() -> None:
    """Strategy detectors (Phase 14): scored labels over fingerprints."""


@detect_group.command("run")
@click.option("--wallet", "wallet", default=None, help="Limit to one wallet.")
@click.option("--window", "window", default="all", show_default=True,
              help='Fingerprint window to read: "all" or "90d".')
def detect_run(wallet: str | None, window: str) -> None:
    """Run every detector over every fingerprint scope and store labels."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        wallets = [wallet] if wallet else ledger_wallets(session)
        if not wallets:
            click.echo("No wallets found.")
            return
        for w in wallets:
            stats = run_detectors(session, w, window=window)
            if stats.scopes == 0:
                click.echo(
                    f"{stats.wallet}: no fingerprints (window={window}). "
                    "Run `pmr fingerprints compute` first."
                )
            else:
                click.echo(
                    f"{stats.wallet}: scopes={stats.scopes} labels={stats.labels_written}"
                )
    finally:
        session.close()


def _fmt(value: str) -> str:
    try:
        return f"{Decimal(value):.3f}"
    except Exception:
        return value


@detect_group.command("show")
@click.option("--wallet", "wallet", required=True)
@click.option("--scope", "scope", default="all", show_default=True,
              help='Scope: "all" or "category:<Label>".')
def detect_show(wallet: str, scope: str) -> None:
    """Show every detector's score for a wallet/scope (evidence collapsed)."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_labels(session, wallet, scope=scope)
        available = label_scopes(session, wallet)
    finally:
        session.close()
    if not rows:
        click.echo(
            f"No strategy labels for wallet={wallet.lower()} scope={scope}. "
            "Run `pmr detect run` first."
        )
        if available:
            click.echo(f"Available scopes: {', '.join(available)}")
        return
    click.echo(f"wallet={wallet.lower()} scope={scope} computed_at={rows[0].computed_at}")
    click.echo(f"{'detector':<22s}{'ver':>4s}{'score':>9s}{'conf':>9s}  missing")
    for r in rows:
        evidence = json.loads(r.evidence_json)
        missing = evidence.get("missing_features") or []
        miss = "-" if not missing else ",".join(missing)
        click.echo(
            f"{r.detector_name:<22s}{r.detector_version:>4d}"
            f"{_fmt(r.score):>9s}{_fmt(r.confidence):>9s}  {miss}"
        )


@detect_group.command("explain")
@click.option("--wallet", "wallet", required=True)
@click.option("--detector", "detector", required=True, help="Detector name.")
@click.option("--scope", "scope", default="all", show_default=True)
def detect_explain(wallet: str, detector: str, scope: str) -> None:
    """Full evidence + blind spots for one detector on one wallet/scope."""
    settings = get_settings()
    ensure_data_dirs(settings)
    session = get_session_factory(settings)()
    try:
        rows = fetch_labels(session, wallet, scope=scope, detector_name=detector)
    finally:
        session.close()
    if not rows:
        click.echo(
            f"No label for wallet={wallet.lower()} scope={scope} detector={detector}. "
            "Run `pmr detect run` first."
        )
        return
    r = rows[0]
    evidence = json.loads(r.evidence_json)
    click.echo(f"wallet={r.wallet} scope={r.scope}")
    click.echo(f"detector={r.detector_name} v{r.detector_version} computed_at={r.computed_at}")
    click.echo(f"label={r.label}  score={_fmt(r.score)}  confidence={_fmt(r.confidence)}")
    click.echo("evidence (feature: value | weight | sub_score):")
    for feature, cell in evidence.get("features", {}).items():
        sub = cell.get("sub_score")
        if sub is None:
            click.echo(
                f"  {feature:<32s} NULL (weight {cell['weight']}) "
                f"— {cell.get('null_reason', 'unavailable')}"
            )
        else:
            click.echo(
                f"  {feature:<32s} value={cell['value']} weight={cell['weight']} "
                f"sub_score={_fmt(sub)}"
            )
    missing = evidence.get("missing_features") or []
    if missing:
        click.echo(f"missing features: {', '.join(missing)}")
    click.echo(f"score_formula: {evidence.get('score_formula')}")
    click.echo("blind spots:")
    click.echo(f"  {r.blind_spots}")
