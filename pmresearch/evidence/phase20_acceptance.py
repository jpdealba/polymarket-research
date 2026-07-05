"""Phase 20 evidence outputs — dataset quality report over the
`microstructure_lifecycle_dataset` table (see
docs/plan/phase18_to_24_rn1_microstructure_research_plan.md, Phase 20).

Read-only: only reads microstructure_lifecycle_dataset; never mutates it.
"""

from __future__ import annotations

import csv
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..microstructure.dataset import dataset_stats


def _wallet_label(session: Session, wallet: str) -> str | None:
    return session.execute(
        text("SELECT display_name FROM wallets WHERE address = :w"), {"w": wallet.lower()}
    ).scalar()


def write_reports(session: Session, wallet: str, out: Path) -> list[Path]:
    """Emit dataset_quality_report.md, feature_null_reason_report.csv,
    close_path_summary.csv for one wallet's built dataset."""
    out.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    stats = dataset_stats(session, wallet)
    label = _wallet_label(session, wallet) or wallet

    # 1. dataset_quality_report.md
    md = out / "dataset_quality_report.md"
    lines = ["# Phase 20 — Microstructure Lifecycle Dataset Quality Report\n"]
    lines.append(f"Wallet: `{wallet}` ({label})  ")
    lines.append(f"Generated: {time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime())}\n")
    lines.append(f"Total rows: **{stats['total_rows']}**\n")
    lines.append("## Rows by context_status\n")
    lines.append("| context_status | rows |")
    lines.append("|---|---|")
    for status, count in sorted(stats["by_context_status"].items()):
        lines.append(f"| {status} | {count} |")
    lines.append("\n## Rows by close_path\n")
    lines.append("| close_path | rows |")
    lines.append("|---|---|")
    for path, count in sorted(stats["by_close_path"].items()):
        lines.append(f"| {path} | {count} |")
    lines.append("\n## Null-reason frequency (top 20)\n")
    lines.append("| feature:reason | count |")
    lines.append("|---|---|")
    top = sorted(stats["null_reason_counts"].items(), key=lambda kv: -kv[1])[:20]
    for key, count in top:
        lines.append(f"| {key} | {count} |")
    lines.append("")
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths.append(md)

    # 2. feature_null_reason_report.csv — one row per (feature, reason, count)
    fr = out / "feature_null_reason_report.csv"
    with fr.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["feature", "reason", "count"])
        for key, count in sorted(stats["null_reason_counts"].items()):
            feature, _, reason = key.partition(":")
            writer.writerow([feature, reason, count])
    paths.append(fr)

    # 3. close_path_summary.csv — counts + avg realized_pnl_bps_on_cost by close_path
    cp = out / "close_path_summary.csv"
    rows = session.execute(
        text(
            "SELECT close_path, realized_pnl_bps_on_cost FROM microstructure_lifecycle_dataset "
            "WHERE wallet = :w"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    by_path: dict[str, list[Decimal]] = {}
    for r in rows:
        by_path.setdefault(r.close_path, [])
        if r.realized_pnl_bps_on_cost is not None:
            try:
                by_path[r.close_path].append(Decimal(r.realized_pnl_bps_on_cost))
            except InvalidOperation:
                pass
    with cp.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["close_path", "count", "avg_realized_pnl_bps_on_cost"])
        for path, values in sorted(by_path.items()):
            avg = sum(values) / len(values) if values else None
            writer.writerow([path, stats["by_close_path"].get(path, 0), avg])
    paths.append(cp)

    return paths
