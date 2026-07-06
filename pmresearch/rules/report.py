"""Phase 21 rule report generation.

Produces markdown reports and CSV exports for fitted rules.
"""

from __future__ import annotations

import csv
import json
from decimal import Decimal
from pathlib import Path
from typing import Optional

from .base import FitResult, SplitMetrics


def _fmt(value, precision: int = 4) -> str:
    if value is None:
        return "N/A"
    if isinstance(value, Decimal):
        return f"{value:.{precision}f}"
    return str(value)


def _split_table(name: str, m: SplitMetrics) -> str:
    lines = [
        f"### {name.title()} Window",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Total fills | {m.total_fills} |",
        f"| Explained fills | {m.explained_fills} |",
        f"| Fill explained rate | {_fmt(m.fill_explained_rate)} |",
        f"| Precision | {_fmt(m.precision)} |",
        f"| Coverage | {_fmt(m.coverage)} |",
        f"| Avg markout 5m | {_fmt(m.avg_markout_5m)} |",
        f"| Avg markout 1h | {_fmt(m.avg_markout_1h)} |",
        f"| Avg PnL episode | {_fmt(m.avg_pnl_episode)} |",
        f"| Avg bond delta | {_fmt(m.avg_bond_delta)} |",
        f"| Avg exposure delta | {_fmt(m.avg_exposure_delta)} |",
        f"| Max inventory required | {_fmt(m.max_inventory_required)} |",
        f"| Out-of-sample edge (bps) | {_fmt(m.out_of_sample_edge_bps)} |",
        f"| Out-of-sample PnL | {_fmt(m.out_of_sample_pnl)} |",
        "",
    ]
    return "\n".join(lines)


def generate_rule_report(result: FitResult) -> str:
    """Generate a markdown report for a single rule fit result."""
    promoted_str = "YES" if result.promoted else "NO"
    lines = [
        f"# Rule Report: {result.rule_name} v{result.rule_version}",
        "",
        f"**Promoted:** {promoted_str}",
        "",
        "## Parameters",
        "",
        "```json",
        json.dumps(result.parameters, indent=2, default=str),
        "```",
        "",
        "## Features Used",
        "",
        ", ".join(result.features_used) if result.features_used else "none",
        "",
        "## Temporal Validation",
        "",
        _split_table("train", result.train),
        _split_table("validation", result.validation),
        _split_table("test", result.test),
        "## Summary",
        "",
        f"- Explained fills: {_fmt(result.explained_fills_pct)} of all fills",
        f"- Expected PnL / markout: {_fmt(result.expected_pnl_or_markout)}",
        f"- Inventory impact: {_fmt(result.inventory_impact)}",
        f"- Promotion rejection reason: {result.promotion_rejection_reason or 'N/A'}",
        "",
        "## Risk Requirements",
        "",
        result.risk_requirements,
        "",
        "## Blind Spots",
        "",
        result.blind_spots,
        "",
    ]
    return "\n".join(lines)


def generate_all_report(results: list[FitResult]) -> str:
    """Generate a consolidated report for all fitted rules."""
    lines = [
        "# Phase 21 — Rule Evaluation Report",
        "",
        "## Overview",
        "",
        f"- Rules evaluated: {len(results)}",
        f"- Rules promoted: {sum(1 for r in results if r.promoted)}",
        "",
    ]

    if results:
        lines.append("## Results Summary")
        lines.append("")
        lines.append("| Rule | Version | Promoted | Train Rate | Test Rate | Test Precision |")
        lines.append("|------|---------|----------|------------|-----------|----------------|")
        for r in results:
            promoted = "yes" if r.promoted else "no"
            lines.append(
                f"| {r.rule_name} | {r.rule_version} | {promoted} | "
                f"{_fmt(r.train.fill_explained_rate)} | "
                f"{_fmt(r.test.fill_explained_rate)} | "
                f"{_fmt(r.test.precision)} |"
            )
        lines.append("")

    for r in results:
        lines.append("---")
        lines.append("")
        lines.append(generate_rule_report(r))

    return "\n".join(lines)


def export_explained_fills_csv(
    result: FitResult,
    out_path: Path,
) -> int:
    """Export fills explained/unexplained by a rule to CSV."""
    from .evaluate import EvalResult, FillDetail

    # Reconstruct from FitResult's fill_details (if attached) or return 0
    # This requires an EvalResult — we'll use a simpler approach:
    # write from the FitResult's available data.
    # The actual fill details live in EvalResult; this function is called
    # from the CLI with the EvalResult.
    return 0


def export_fill_details_csv(
    fill_details: list,
    out_path: Path,
    *,
    explained_only: bool = False,
) -> int:
    """Export fill details to CSV."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if not fill_details:
        return 0

    fieldnames = [
        "event_id", "trade_ts", "trade_utc", "token_id", "side",
        "fill_price", "applies", "explanation",
        "markout_5m", "markout_1h", "pnl_episode",
    ]

    with out_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        count = 0
        for fd in fill_details:
            if explained_only and not fd.applies:
                continue
            writer.writerow({
                "event_id": fd.event_id,
                "trade_ts": fd.trade_ts,
                "trade_utc": fd.trade_utc,
                "token_id": fd.token_id,
                "side": fd.side,
                "fill_price": fd.fill_price,
                "applies": fd.applies,
                "explanation": fd.explanation,
                "markout_5m": fd.markout_5m,
                "markout_1h": fd.markout_1h,
                "pnl_episode": fd.pnl_episode,
            })
            count += 1
    return count
