"""Phase 15 — Markdown rendering of a `WalletProfile`.

Pure string assembly. Every numeric token emitted here is interpolated from the
`WalletProfile` (itself read from stored projections) — the template carries no
numeric literals of its own, so a rendered figure always traces back to a
queried value. Sections with no data render an explicit *insufficient data*
block rather than being silently dropped.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Optional

from ..reconcile.checks import decimal_string
from .wallet_profile import (
    FeatureValue,
    WalletProfile,
)

_INSUFFICIENT = "_Insufficient data — {reason}._"


def _d(value: Decimal) -> str:
    return decimal_string(value)


def _usd(value: Decimal) -> str:
    """Money display: quantize a queried USDC figure to cents (still traces to the
    queried value — this is presentation rounding, not a new metric)."""
    return decimal_string(value.quantize(Decimal("0.01")))


def _pct(value: Optional[Decimal]) -> str:
    if value is None:
        return "n/a"
    return f"{(value * Decimal('100')).quantize(Decimal('0.01'))}%"


def _feature_line(fv: FeatureValue) -> str:
    if fv.is_null:
        return f"- `{fv.feature}`: _null — {fv.null_reason}_"
    if fv.value_type == "json":
        return f"- `{fv.feature}`: `{fv.value}`"
    return f"- `{fv.feature}`: {fv.value}"


def _trust_banner(profile: WalletProfile) -> list[str]:
    trust = profile.trust
    if trust is None:
        return [
            "> ⚠️ **DATA QUALITY: trust status unknown** — no reconciliation has "
            "run for this wallet. Treat every figure below as unverified.",
        ]
    if trust.status == "untrusted":
        return [
            f"> 🛑 **DATA QUALITY: WALLET UNTRUSTED** — {trust.reason}",
            ">",
            "> Reconciliation against Polymarket's own accounting is failing. The "
            "numbers below are derived from a ledger that does not currently match "
            "the external oracle; **do not treat any conclusion as reliable** until "
            "the drift is resolved.",
        ]
    if trust.status == "warn":
        return [
            f"> ⚠️ **DATA QUALITY: trust = warn** — {trust.reason}",
        ]
    return [
        f"> ✅ **Data quality: trust = trusted** — {trust.reason}",
    ]


def _executive_summary(profile: WalletProfile) -> list[str]:
    lines = ["## Executive summary", ""]
    pnl = profile.pnl
    if pnl is None:
        lines.append(_INSUFFICIENT.format(reason="no PnL decomposition; run `pmr derive run`"))
        lines.append("")
        return lines
    lines.append(f"- **Total decomposed PnL:** {_usd(pnl.total)} USDC (gross base {_usd(pnl.gross_base)} − fees {_usd(pnl.fees)}).")
    if pnl.dominant is not None:
        dominant = next(c for c in pnl.contributions if c.name == pnl.dominant)
        lines.append(
            f"- **Dominant income source:** `{dominant.name}` at {_usd(dominant.value)} USDC "
            f"({_pct(dominant.share_of_magnitude)} of gross magnitude)."
        )
    else:
        lines.append("- **Dominant income source:** none — no component is positive.")
    top = profile.top_hypothesis
    if top is not None:
        lines.append(
            f"- **Leading strategy hypothesis:** `{top.detector_name}` "
            f"(score {_d(top.score)}, confidence {_d(top.confidence)})."
        )
    else:
        lines.append("- **Leading strategy hypothesis:** none computed.")
    lines.append("")
    return lines


def _pnl_decomposition(profile: WalletProfile) -> list[str]:
    lines = ["## PnL decomposition", ""]
    pnl = profile.pnl
    if pnl is None:
        lines.append(_INSUFFICIENT.format(reason="no `pnl_decomposition` rows; run `pmr derive run`"))
        lines.append("")
        return lines
    lines.append(f"Projection version {pnl.projection_version}. Components (signed USDC):")
    lines.append("")
    lines.append("| Component | USDC | Share of gross magnitude |")
    lines.append("|---|---:|---:|")
    for c in pnl.contributions:
        lines.append(f"| {c.name} | {_usd(c.value)} | {_pct(c.share_of_magnitude)} |")
    lines.append(f"| **gross base (pre-fee)** | **{_usd(pnl.gross_base)}** | |")
    lines.append(f"| estimated fees | {_usd(pnl.fees)} | |")
    lines.append(f"| **total (net of projection fees)** | **{_usd(pnl.total)}** | |")
    lines.append("")
    lines.append(
        "_`bond_merge` is the inventory-cycling / bond leg (SPLIT↔MERGE pairs); "
        "`redemption` is resolution/redeem proceeds. Fees here are the projection's "
        "own estimate — see the fee caveat in Limitations._"
    )
    lines.append("")
    return lines


def _category_breakdown(profile: WalletProfile) -> list[str]:
    lines = ["## Category breakdown", ""]
    if not profile.categories:
        lines.append(_INSUFFICIENT.format(reason="no per-category PnL rows"))
        lines.append("")
        return lines
    lines.append("| Category | directional | bond_merge | rewards | redemption | fees | total |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|")
    for c in profile.categories:
        lines.append(
            f"| {c.category} | {_usd(c.directional)} | {_usd(c.bond_merge)} | "
            f"{_usd(c.reward_income)} | {_usd(c.redemption)} | {_usd(c.fees)} | {_usd(c.total)} |"
        )
    lines.append("")
    return lines


def _episode_behavior(profile: WalletProfile) -> list[str]:
    lines = ["## Episode behavior", ""]
    ep = profile.episodes
    if ep is None:
        lines.append(_INSUFFICIENT.format(reason="no episodes; run `pmr replay episodes`"))
        lines.append("")
        return lines
    lines.append(
        f"- **Episodes:** {ep.count} "
        f"({ep.open_count} open, {ep.flat_closed_count} flat-closed, "
        f"{ep.resolution_closed_count} resolution-closed)."
    )
    lines.append(
        f"- **Duration (s):** min {ep.duration_min}, p50 {ep.duration_p50}, "
        f"p90 {ep.duration_p90}, max {ep.duration_max}."
    )
    lines.append(
        f"- **Micro-episodes:** {ep.micro_episode_count} "
        f"({_pct(ep.micro_episode_share)} of all episodes)."
    )
    lines.append(
        f"- **Realized PnL (episodes):** {_usd(ep.realized_pnl)}; "
        f"**reward income:** {_usd(ep.reward_income)}."
    )
    lines.append("")
    return lines


def _equity(profile: WalletProfile) -> list[str]:
    lines = ["## Equity & drawdown", ""]
    eq = profile.equity
    if eq is None:
        lines.append(_INSUFFICIENT.format(reason="no daily equity; run `pmr equity build`"))
        lines.append("")
        return lines
    lines.append(f"- **Date range:** {eq.first_date} → {eq.last_date} ({eq.rows} days).")
    lines.append(f"- **Latest portfolio value:** {_usd(eq.latest_portfolio_value)} USDC.")
    lines.append(f"- **Latest marked PnL:** {_usd(eq.latest_marked_pnl)} USDC.")
    lines.append(
        f"- **Max drawdown:** {_usd(eq.max_drawdown)} (basis: {eq.drawdown_basis}; "
        "daily-close based — intraday drawdown is approximate)."
    )
    lines.append(f"- **Latest stale-equity share:** {_pct(eq.latest_stale_equity_share)}.")
    lines.append("")
    return lines


def _execution_evidence(profile: WalletProfile) -> list[str]:
    lines = ["## Maker/taker & execution evidence", ""]
    coverage = next(
        (f for f in profile.execution_features if f.feature == "enrichment_coverage"),
        None,
    )
    if coverage is not None and coverage.is_null:
        lines.append(
            "> Maker/taker shares are conditioned on on-chain enrichment coverage, "
            "which is **null** for this scope — the shares below may be uncomputable "
            "or unreliable."
        )
        lines.append("")
    for fv in profile.execution_features:
        lines.append(_feature_line(fv))
    lines.append("")
    lines.append(
        "_Maker/taker fill share is only meaningful over the subgraph-covered "
        "period; recent fills may be pending enrichment (see `enrichment_coverage`)._"
    )
    lines.append("")
    return lines


def _income_evidence(profile: WalletProfile) -> list[str]:
    lines = ["## Income & sizing evidence", ""]
    for fv in profile.income_features:
        lines.append(_feature_line(fv))
    lines.append("")
    return lines


def _behavior_evidence(profile: WalletProfile) -> list[str]:
    lines = ["## Inventory & behavior fingerprint", ""]
    for fv in profile.behavior_features:
        lines.append(_feature_line(fv))
    lines.append("")
    return lines


def _hypotheses(profile: WalletProfile) -> list[str]:
    lines = ["## Strategy hypotheses", ""]
    if not profile.hypotheses_all:
        lines.append(_INSUFFICIENT.format(reason="no strategy labels; run `pmr detect run`"))
        lines.append("")
        return lines
    lines.append("Scores are 0–1 with a separate confidence (share of input features available). "
                 "A high score at low confidence is not a verdict.")
    lines.append("")
    lines.append("| Detector | Version | Score | Confidence |")
    lines.append("|---|---:|---:|---:|")
    for h in profile.hypotheses_all:
        lines.append(
            f"| {h.detector_name} | {h.detector_version} | {_d(h.score)} | {_d(h.confidence)} |"
        )
    lines.append("")
    lines.append("### Blind spots")
    lines.append("")
    for h in profile.hypotheses_all:
        lines.append(f"- **{h.detector_name}:** {h.blind_spots}")
    lines.append("")
    if profile.hypothesis_scopes:
        scopes = ", ".join(f"`{s}`" for s in profile.hypothesis_scopes)
        lines.append(f"_Labels also exist for scopes: {scopes} (see `pmr detect explain`)._")
        lines.append("")
    return lines


def _worldcup_forward_watch(profile: WalletProfile) -> list[str]:
    section = profile.worldcup_forward
    if section is None:
        return []
    lines = ["## World Cup Forward Watch", ""]
    lines.append(f"- **Active watchlist tokens:** {section.active_watchlist_tokens}.")
    lines.append(f"- **Latest sample time:** {section.latest_sample_time or 'never'}.")
    lines.append(
        f"- **Maker fills with excellent/good context:** "
        f"{section.excellent_good_context} / {section.maker_fills_total}."
    )
    lines.append(f"- **Strict coverage:** {_pct(section.strict_coverage_share)}.")
    lines.append(f"- **Loose coverage:** {_pct(section.loose_coverage_share)}.")
    if section.excellent_good_context == 0:
        lines.append("")
        lines.append(
            "_Insufficient forward book context. The collector has not been running "
            "long enough before RN1 fills._"
        )
    lines.append("")
    return lines


def _reconciliation(profile: WalletProfile) -> list[str]:
    lines = ["## Reconciliation & trust", ""]
    rec = profile.reconciliation
    if profile.trust is not None:
        t = profile.trust
        lines.append(
            f"- **Trust:** {t.status} — {t.reason} (since ts {t.since_ts}, "
            f"last reconciliation ts {t.last_reconciliation_ts})."
        )
    if rec is None:
        lines.append(_INSUFFICIENT.format(reason="no reconciliation facts; run `pmr reconcile run`"))
        lines.append("")
        return lines
    s = rec.summary
    lines.append(
        f"- **Holdings vs /positions:** remote {s['remote_positions']}, "
        f"local nonzero {s['local_nonzero_holdings']}, exact matches {s['exact_matches']}, "
        f"pass {s['passes']}, warn {s['warnings']}, fail {s['fails']}."
    )
    lines.append(f"- **Tolerance:** {_d(rec.tolerance)}. **Known exceptions:** {rec.known_exception_count}.")
    lines.append("")
    lines.append("| Check | total | pass | warn | fail | skip |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for check, counts in rec.check_status_counts.items():
        lines.append(
            f"| {check} | {counts['total']} | {counts.get('pass', 0)} | "
            f"{counts.get('warn', 0)} | {counts.get('fail', 0)} | {counts.get('skip', 0)} |"
        )
    lines.append("")
    if rec.value_check is not None:
        v = rec.value_check
        lines.append(
            f"- **Portfolio /value:** oracle {_usd(v['oracle'])}, local {_usd(v['local'])}, "
            f"pct_diff {_d(v['pct_diff'])}, status {v['status']} ({v['reason_code']}), "
            f"stale_equity_share {v.get('stale_equity_share')}."
        )
        lines.append("")
    return lines


def _limitations(profile: WalletProfile) -> list[str]:
    lines = ["## Limitations & data-quality notes", ""]
    lines.append(
        "- **Fees are estimates.** The decomposition's fee figure is the schedule-based "
        "estimate, not per-fill actuals, except where Phase 11 enrichment supplied a real fee."
    )
    lines.append(
        "- **Maker/taker coverage.** Shares depend on on-chain enrichment; the "
        "`enrichment_coverage` feature above bounds how much of the fill history is observed."
    )
    lines.append(
        "- **Mark staleness.** Unrealized value uses marks that may be stale; the equity "
        "section reports the stale-equity share, and drawdown is daily-close based."
    )
    lines.append(
        "- **Redemption PnL.** Resolution-closed episode PnL relies on derived redemption "
        "events (`pmr derive run`); markets not yet resolved on Gamma are excluded, not assumed lost."
    )
    lines.append(
        "- **Detector blind spots.** Each strategy score carries its own blind spots "
        "(listed above); scores read fingerprints only and are not ground truth."
    )
    if profile.is_untrusted:
        lines.append(
            "- **⚠️ This wallet is UNTRUSTED** — reconciliation drift means the ledger does "
            "not match Polymarket's accounting; conclusions are provisional."
        )
    lines.append("")
    return lines


def render_wallet_profile(profile: WalletProfile) -> str:
    """Render a `WalletProfile` to a Markdown memo string."""
    lines: list[str] = []
    lines.append(f"# Why is `{profile.wallet}` profitable?")
    lines.append("")
    lines.append(
        f"_Generated {profile.generated_at} · window `{profile.window}` · "
        f"fingerprint version {profile.fingerprint_version}_"
    )
    lines.append("")
    lines.extend(_trust_banner(profile))
    lines.append("")
    lines.extend(_executive_summary(profile))
    lines.extend(_pnl_decomposition(profile))
    lines.extend(_category_breakdown(profile))
    lines.extend(_episode_behavior(profile))
    lines.extend(_equity(profile))
    lines.extend(_execution_evidence(profile))
    lines.extend(_income_evidence(profile))
    lines.extend(_behavior_evidence(profile))
    lines.extend(_hypotheses(profile))
    lines.extend(_worldcup_forward_watch(profile))
    lines.extend(_reconciliation(profile))
    lines.extend(_limitations(profile))
    return "\n".join(lines).rstrip() + "\n"
