"""Phase 15 — "Why is <wallet> profitable?" report assembly.

This module ASSEMBLES a `WalletProfile` from already-computed, persisted
projections/fingerprints/labels/reconciliation facts. It performs **no new
computation**: every figure is read through the same fetch functions the CLI
and dashboard use. The only arithmetic here is presentation-layer derivation of
ratios (a component's share of the decomposition) and sums that already exist as
dataclass properties on the queried rows — never a fresh metric read off the
ledger. Keeping the numbers here identical to the ones dashboards show is the
whole point (ADR 0006 point 7).

`render.py` turns a `WalletProfile` into Markdown; `cli/report.py` writes it to
`/data/exports/`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import Session

from ..projections.daily_equity import DailyEquityRow, fetch_daily_equity
from ..projections.episodes import EpisodeStats, episode_stats
from ..projections.pnl_decomposition import (
    PnlDecompositionRow,
    fetch_pnl_decomposition,
)
from ..reconcile.runner import latest_reconciliation_result
from ..reconcile.trust import WalletTrust, fetch_wallet_trust
from ..detectors.compute import LabelRow, fetch_labels
from ..fingerprints.compute import FingerprintRow, fetch_fingerprints

_ZERO = Decimal("0")

# The fingerprint features the report surfaces, grouped by the narrative section
# they support. Every one is read from the persisted `fingerprints` rows.
_EXECUTION_FEATURES = ("maker_fill_share", "taker_fill_share", "enrichment_coverage")
_INCOME_FEATURES = ("reward_income_share", "realized_pnl", "unrealized_pnl")
_BEHAVIOR_FEATURES = (
    "bond_inventory_ratio",
    "merge_frequency",
    "redeem_frequency",
    "episode_count",
    "episode_duration_p50",
    "episode_duration_p90",
    "micro_episode_share",
    "adds_per_episode",
    "partial_exit_frequency",
    "market_category_concentration",
)
_QUALITY_FEATURES = ("stale_mark_share", "enrichment_coverage")


# --- section value objects --------------------------------------------------


@dataclass(frozen=True)
class Contribution:
    """One PnL-decomposition component with its magnitude share."""

    name: str
    value: Decimal
    share_of_magnitude: Optional[Decimal]  # value / Σ|components|, None if Σ==0


@dataclass(frozen=True)
class PnlSection:
    directional: Decimal
    bond_merge: Decimal
    reward_income: Decimal
    redemption: Decimal
    fees: Decimal
    gross_base: Decimal  # Σ of the four gross components (pre-fee)
    total: Decimal  # gross_base − fees
    contributions: tuple[Contribution, ...]
    dominant: Optional[str]  # component with the largest positive value
    projection_version: int


@dataclass(frozen=True)
class CategoryPnl:
    category: str
    directional: Decimal
    bond_merge: Decimal
    reward_income: Decimal
    redemption: Decimal
    fees: Decimal
    total: Decimal


@dataclass(frozen=True)
class EquitySection:
    first_date: str
    last_date: str
    latest_portfolio_value: Decimal
    latest_marked_pnl: Decimal
    latest_stale_equity_share: Decimal
    max_drawdown: Decimal
    drawdown_basis: str
    rows: int


@dataclass(frozen=True)
class FeatureValue:
    feature: str
    value: Optional[str]
    value_type: Optional[str]
    null_reason: Optional[str]

    @property
    def is_null(self) -> bool:
        return self.value is None


@dataclass(frozen=True)
class HypothesisRow:
    detector_name: str
    detector_version: int
    score: Decimal
    confidence: Decimal
    blind_spots: str


@dataclass(frozen=True)
class ReconciliationSection:
    run_ts: int
    tolerance: Decimal
    summary: dict
    check_status_counts: dict
    known_exception_count: int
    value_check: Optional[dict]  # {oracle, local, pct_diff, status, ...} or None


@dataclass(frozen=True)
class WalletProfile:
    wallet: str
    generated_at: str
    window: str
    fingerprint_version: Optional[int]

    trust: Optional[WalletTrust]
    reconciliation: Optional[ReconciliationSection]
    pnl: Optional[PnlSection]
    categories: tuple[CategoryPnl, ...]
    episodes: Optional[EpisodeStats]
    equity: Optional[EquitySection]
    execution_features: tuple[FeatureValue, ...]
    income_features: tuple[FeatureValue, ...]
    behavior_features: tuple[FeatureValue, ...]
    quality_features: tuple[FeatureValue, ...]
    hypotheses_all: tuple[HypothesisRow, ...]
    hypothesis_scopes: tuple[str, ...]

    @property
    def is_untrusted(self) -> bool:
        return self.trust is not None and self.trust.status == "untrusted"

    @property
    def top_hypothesis(self) -> Optional[HypothesisRow]:
        if not self.hypotheses_all:
            return None
        return max(self.hypotheses_all, key=lambda h: h.score)


# --- assembly ---------------------------------------------------------------


def _pnl_section(rows: list[PnlDecompositionRow]) -> Optional[PnlSection]:
    row = next((r for r in rows if r.scope == "all"), None)
    if row is None:
        return None
    components = [
        ("directional", row.directional_pnl),
        ("bond_merge", row.bond_merge_pnl),
        ("reward_income", row.reward_income),
        ("redemption", row.redemption_pnl),
    ]
    magnitude = sum((abs(v) for _n, v in components), _ZERO)
    contributions = tuple(
        Contribution(
            name=name,
            value=value,
            share_of_magnitude=(value / magnitude) if magnitude != _ZERO else None,
        )
        for name, value in components
    )
    positives = [(n, v) for n, v in components if v > _ZERO]
    dominant = max(positives, key=lambda nv: nv[1])[0] if positives else None
    gross_base = sum((v for _n, v in components), _ZERO)
    return PnlSection(
        directional=row.directional_pnl,
        bond_merge=row.bond_merge_pnl,
        reward_income=row.reward_income,
        redemption=row.redemption_pnl,
        fees=row.fees,
        gross_base=gross_base,
        total=row.total_pnl,
        contributions=contributions,
        dominant=dominant,
        projection_version=row.projection_version,
    )


def _category_section(rows: list[PnlDecompositionRow]) -> tuple[CategoryPnl, ...]:
    cats = [
        CategoryPnl(
            category=r.scope.removeprefix("category:"),
            directional=r.directional_pnl,
            bond_merge=r.bond_merge_pnl,
            reward_income=r.reward_income,
            redemption=r.redemption_pnl,
            fees=r.fees,
            total=r.total_pnl,
        )
        for r in rows
        if r.scope.startswith("category:")
    ]
    return tuple(sorted(cats, key=lambda c: c.total, reverse=True))


def _equity_section(rows: list[DailyEquityRow]) -> Optional[EquitySection]:
    if not rows:
        return None
    latest = rows[-1]
    return EquitySection(
        first_date=rows[0].date,
        last_date=latest.date,
        latest_portfolio_value=latest.portfolio_value,
        latest_marked_pnl=latest.marked_pnl,
        latest_stale_equity_share=latest.stale_equity_share,
        max_drawdown=max(row.drawdown for row in rows),
        drawdown_basis=latest.drawdown_basis,
        rows=len(rows),
    )


def _feature_map(rows: list[FingerprintRow]) -> dict[str, FingerprintRow]:
    return {r.feature: r for r in rows}


def _feature_values(
    fmap: dict[str, FingerprintRow], names: tuple[str, ...]
) -> tuple[FeatureValue, ...]:
    out = []
    for name in names:
        row = fmap.get(name)
        if row is None:
            out.append(
                FeatureValue(feature=name, value=None, value_type=None, null_reason="feature not computed")
            )
        else:
            out.append(
                FeatureValue(
                    feature=name,
                    value=row.value,
                    value_type=row.value_type,
                    null_reason=row.null_reason,
                )
            )
    return tuple(out)


def _hypotheses(labels: list[LabelRow], scope: str) -> tuple[HypothesisRow, ...]:
    rows = [
        HypothesisRow(
            detector_name=lbl.detector_name,
            detector_version=lbl.detector_version,
            score=Decimal(lbl.score),
            confidence=Decimal(lbl.confidence),
            blind_spots=lbl.blind_spots,
        )
        for lbl in labels
        if lbl.scope == scope
    ]
    return tuple(sorted(rows, key=lambda h: h.score, reverse=True))


def _reconciliation_section(
    session: Session, wallet: str
) -> tuple[Optional[ReconciliationSection], Optional[WalletTrust]]:
    results = latest_reconciliation_result(session, wallet)
    match = next(((r, t) for r, t in results if r.wallet == wallet.lower()), None)
    if match is None:
        return None, None
    result, trust = match

    value_check = None
    value_facts = [f for f in result.facts if f.check_type == "portfolio_value"]
    if value_facts:
        fact = value_facts[-1]
        value_check = {
            "oracle": fact.expected,
            "local": fact.computed,
            "abs_diff": fact.abs_diff,
            "pct_diff": fact.pct_diff,
            "tolerance": fact.tolerance,
            "status": fact.status,
            "reason_code": fact.reason_code,
            "stale_equity_share": fact.notes.get("stale_equity_share"),
            "equity_date": fact.notes.get("equity_date"),
        }

    section = ReconciliationSection(
        run_ts=result.run_ts,
        tolerance=result.tolerance,
        summary=result.summary(),
        check_status_counts=result.check_status_counts(),
        known_exception_count=len(result.known_exceptions()),
        value_check=value_check,
    )
    return section, trust


def build_wallet_profile(
    session: Session, wallet: str, *, window: str = "all"
) -> WalletProfile:
    """Assemble the full research profile for one wallet from stored projections."""
    wallet = wallet.lower()

    pnl_all = fetch_pnl_decomposition(session, wallet, by_category=False)
    pnl_cat = fetch_pnl_decomposition(session, wallet, by_category=True)
    equity_rows = fetch_daily_equity(session, wallet)
    ep_stats = episode_stats(session, wallet)
    fingerprints = fetch_fingerprints(session, wallet, scope="all", window=window)
    labels = fetch_labels(session, wallet)
    reconciliation, trust_from_recon = _reconciliation_section(session, wallet)

    # Trust: prefer the row keyed to the latest reconciliation; fall back to the
    # persisted wallet_trust table (they are the same source, one is denormalised).
    trust = trust_from_recon
    if trust is None:
        trust_rows = fetch_wallet_trust(session, wallet)
        trust = trust_rows[0] if trust_rows else None

    fmap = _feature_map(fingerprints)
    fp_version = fingerprints[0].version if fingerprints else None

    # Episodes projection always returns a stats object; treat an all-zero one as
    # "no episodes" so the section can degrade to an insufficient-data block.
    episodes = ep_stats if ep_stats.count > 0 else None

    hypothesis_scopes = tuple(sorted({lbl.scope for lbl in labels}))

    return WalletProfile(
        wallet=wallet,
        generated_at=datetime.now(timezone.utc).isoformat(),
        window=window,
        fingerprint_version=fp_version,
        trust=trust,
        reconciliation=reconciliation,
        pnl=_pnl_section(pnl_all),
        categories=_category_section(pnl_cat),
        episodes=episodes,
        equity=_equity_section(equity_rows),
        execution_features=_feature_values(fmap, _EXECUTION_FEATURES),
        income_features=_feature_values(fmap, _INCOME_FEATURES),
        behavior_features=_feature_values(fmap, _BEHAVIOR_FEATURES),
        quality_features=_feature_values(fmap, _QUALITY_FEATURES),
        hypotheses_all=_hypotheses(labels, "all"),
        hypothesis_scopes=hypothesis_scopes,
    )
