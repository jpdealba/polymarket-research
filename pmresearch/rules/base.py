"""Rule contract, temporal splits, and evaluation metrics for Phase 21.

Every rule receives a single dataset row (``dict[str, Optional[str]]``) from
the ``microstructure_lifecycle_dataset`` table and decides — using *only*
pre-fill features — whether it fires on that fill.

Temporal validation splits fills chronologically into train / validation / test
windows. A rule is only promoted if it maintains positive signal in the
out-of-sample (test) window.

Future-information guard
------------------------
Features that reveal post-fill outcomes are **forbidden** for rule matching:

- book_after, best_bid/ask_after, spread_after, mid_after, depth_top_after_json
- close_path, close_ts, hold_seconds
- realized_pnl_wac, realized_pnl_per_share, realized_pnl_bps_on_cost
- pnl_episode, pnl_at_resolution
- markout_5m / 15m / 1h / 24h
- qty_token_after, qty_complement_after, directional_after, bond_after,
  bond_ratio_after, event_exposure_after, event_exposure_delta,
  bond_delta, directional_delta
- remaining_open_qty_after_24h, is_open_after_24h
- closed_by_merge, closed_by_redeem, closed_by_sell, closed_by_resolution,
  closed_by_unresolved_open

These columns may only appear in evaluation/reporting contexts.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Optional, Protocol, runtime_checkable

_ZERO = Decimal("0")

# ── feature guard ────────────────────────────────────────────────────────────

FORBIDDEN_FEATURES: frozenset[str] = frozenset({
    "best_bid_after", "best_ask_after", "spread_after", "mid_after",
    "depth_top_after_json", "book_after_age_s",
    "close_path", "close_ts", "hold_seconds",
    "realized_pnl_wac", "realized_pnl_per_share", "realized_pnl_bps_on_cost",
    "pnl_episode", "pnl_at_resolution",
    "markout_5m", "markout_15m", "markout_1h", "markout_24h",
    "qty_token_after", "qty_complement_after",
    "directional_after", "bond_after", "bond_ratio_after",
    "event_exposure_after", "event_exposure_delta",
    "bond_delta", "directional_delta",
    "remaining_open_qty_after_24h", "is_open_after_24h",
    "closed_by_merge", "closed_by_redeem", "closed_by_sell",
    "closed_by_resolution", "closed_by_unresolved_open",
})


class FutureFeatureAccessError(ValueError):
    """Raised when a rule attempts to read a post-fill feature."""


class GuardedRow(dict):
    """Dataset row wrapper that blocks post-fill feature access."""

    def __getitem__(self, key):
        if key in FORBIDDEN_FEATURES:
            raise FutureFeatureAccessError(f"Forbidden future feature accessed: {key}")
        return super().__getitem__(key)

    def get(self, key, default=None):
        if key in FORBIDDEN_FEATURES:
            raise FutureFeatureAccessError(f"Forbidden future feature accessed: {key}")
        return super().get(key, default)


def apply_rule_no_future(rule: "Rule", row: dict) -> "RuleDecision":
    """Apply a rule with active future-feature protection."""
    decision = rule.applies(GuardedRow(row))
    used_forbidden = set(decision.features_used) & FORBIDDEN_FEATURES
    if used_forbidden:
        raise FutureFeatureAccessError(
            f"Rule {rule.name} v{rule.version} reported forbidden features: "
            f"{sorted(used_forbidden)}"
        )
    return decision

# ── helpers ──────────────────────────────────────────────────────────────────


def opt_decimal(value: Optional[str]) -> Optional[Decimal]:
    """Parse a string column value to Decimal, returning None on failure."""
    if value is None:
        return None
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


def row_decimal(row: dict, key: str) -> Optional[Decimal]:
    """Extract a Decimal from a dataset row by key, or None."""
    return opt_decimal(row.get(key))


# ── rule decision ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RuleDecision:
    """Result of applying one rule to a single fill row."""

    applies: bool
    features_used: dict[str, Optional[str]]
    explanation: str


# ── rule protocol ────────────────────────────────────────────────────────────


@runtime_checkable
class Rule(Protocol):
    """A candidate rule that fires (or not) on a single dataset row.

    Rules may only read pre-fill features.  The ``features_used`` dict in the
    returned ``RuleDecision`` must list every feature the rule touched — this
    is used both for evidence and for the features_used column in
    ``strategy_candidates``.
    """

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> int: ...

    @property
    def description(self) -> str: ...

    @property
    def parameters(self) -> dict: ...

    def applies(self, row: dict) -> RuleDecision:
        """Return whether the rule fires on *row* and which features drove it."""
        ...


# ── temporal split ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TemporalSplit:
    """Three chronological slices of a row-sorted dataset."""

    train: list[dict]
    validation: list[dict]
    test: list[dict]


def temporal_split(
    rows: list[dict],
    *,
    train_ratio: float = 0.6,
    validation_ratio: float = 0.2,
) -> TemporalSplit:
    """Split *rows* (already sorted by trade_ts) into three chronological windows.

    ``train_ratio`` + ``validation_ratio`` must be < 1.0; the remainder is
    the test window.
    """
    if train_ratio < 0 or validation_ratio < 0:
        raise ValueError("train_ratio and validation_ratio must be non-negative")
    if train_ratio + validation_ratio >= 1.0:
        raise ValueError("train_ratio + validation_ratio must leave a non-empty test window")
    n = len(rows)
    train_end = int(n * train_ratio)
    val_end = int(n * (train_ratio + validation_ratio))
    return TemporalSplit(
        train=rows[:train_end],
        validation=rows[train_end:val_end],
        test=rows[val_end:],
    )


# ── metrics ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SplitMetrics:
    """Metrics for one temporal window."""

    total_fills: int
    explained_fills: int
    fill_explained_rate: Decimal
    false_positives: int
    false_positive_rate: Decimal
    precision: Decimal
    coverage: Decimal
    avg_markout_5m: Optional[Decimal]
    avg_markout_1h: Optional[Decimal]
    avg_pnl_episode: Optional[Decimal]
    avg_bond_delta: Optional[Decimal]
    avg_exposure_delta: Optional[Decimal]
    max_inventory_required: Optional[Decimal]
    out_of_sample_edge_bps: Optional[Decimal]
    out_of_sample_pnl: Optional[Decimal]


@dataclass(frozen=True)
class FitResult:
    """Complete result of fitting a rule with temporal validation."""

    rule_name: str
    rule_version: int
    parameters: dict
    features_used: list[str]
    train: SplitMetrics
    validation: SplitMetrics
    test: SplitMetrics
    explained_fills_pct: Decimal
    expected_pnl_or_markout: Optional[Decimal]
    inventory_impact: Optional[Decimal]
    risk_requirements: str
    blind_spots: str
    promoted: bool
    promotion_rejection_reason: Optional[str] = None


def compute_split_metrics(
    rows: list[dict],
    decisions: dict[int, RuleDecision],
    *,
    label_mode: bool = False,
) -> SplitMetrics:
    """Compute metrics for one temporal split.

    Parameters
    ----------
    rows:
        Dataset rows in this split.
    decisions:
        ``{row_index: RuleDecision}`` for rows in this split.
    label_mode:
        If True, use future-information columns for evaluation only
        (markout, pnl_episode, etc.).  The rule itself must never see these.
    """
    total = len(rows)
    explained = sum(1 for d in decisions.values() if d.applies)

    explained_rate = Decimal(explained) / Decimal(total) if total > 0 else _ZERO
    coverage = explained_rate

    true_positives = 0
    false_positives = 0
    labeled_explained = 0

    markout_5m_values: list[Decimal] = []
    markout_1h_values: list[Decimal] = []
    pnl_values: list[Decimal] = []
    bond_delta_values: list[Decimal] = []
    exposure_delta_values: list[Decimal] = []
    inventory_values: list[Decimal] = []

    for idx, row in enumerate(rows):
        decision = decisions.get(idx)
        if decision is None or not decision.applies:
            continue

        if label_mode:
            m5 = opt_decimal(row.get("markout_5m"))
            if m5 is not None:
                markout_5m_values.append(m5)
            m1h = opt_decimal(row.get("markout_1h"))
            if m1h is not None:
                markout_1h_values.append(m1h)
            pnl = opt_decimal(row.get("pnl_episode"))
            if pnl is not None:
                pnl_values.append(pnl)
            label = pnl if pnl is not None else m5
            if label is not None:
                labeled_explained += 1
                if label > _ZERO:
                    true_positives += 1
                else:
                    false_positives += 1
            bd = opt_decimal(row.get("bond_delta"))
            if bd is not None:
                bond_delta_values.append(bd)
            ed = opt_decimal(row.get("event_exposure_delta"))
            if ed is not None:
                exposure_delta_values.append(ed)
            inv = opt_decimal(row.get("qty_token_before"))
            if inv is not None:
                inventory_values.append(abs(inv))

    def _avg(vals: list[Decimal]) -> Optional[Decimal]:
        if not vals:
            return None
        return sum(vals, _ZERO) / Decimal(len(vals))

    avg_m5 = _avg(markout_5m_values)
    avg_m1h = _avg(markout_1h_values)
    avg_pnl = _avg(pnl_values)
    avg_bd = _avg(bond_delta_values)
    avg_ed = _avg(exposure_delta_values)
    max_inv = max(inventory_values) if inventory_values else None

    edge_bps: Optional[Decimal] = None
    if avg_m5 is not None:
        edge_bps = avg_m5 * Decimal(10000)

    oos_pnl = sum(pnl_values, _ZERO) if pnl_values else None
    precision = (
        Decimal(true_positives) / Decimal(labeled_explained)
        if label_mode and labeled_explained > 0
        else _ZERO
    )
    false_positive_rate = (
        Decimal(false_positives) / Decimal(labeled_explained)
        if label_mode and labeled_explained > 0
        else _ZERO
    )

    return SplitMetrics(
        total_fills=total,
        explained_fills=explained,
        fill_explained_rate=explained_rate,
        false_positives=false_positives,
        false_positive_rate=false_positive_rate,
        precision=precision,
        coverage=coverage,
        avg_markout_5m=avg_m5,
        avg_markout_1h=avg_m1h,
        avg_pnl_episode=avg_pnl,
        avg_bond_delta=avg_bd,
        avg_exposure_delta=avg_ed,
        max_inventory_required=max_inv,
        out_of_sample_edge_bps=edge_bps,
        out_of_sample_pnl=oos_pnl,
    )


def split_has_positive_signal(metrics: SplitMetrics, *, min_explained: int = 1) -> bool:
    """Return True when an evaluation split has enough positive OOS evidence."""
    if metrics.explained_fills < min_explained:
        return False
    if metrics.avg_pnl_episode is not None:
        return metrics.avg_pnl_episode > _ZERO
    if metrics.avg_markout_5m is not None:
        return metrics.avg_markout_5m > _ZERO
    return False


def promotion_eligible(
    validation: SplitMetrics,
    test: SplitMetrics,
    *,
    min_explained_per_window: int = 1,
) -> bool:
    """Promotion requires positive validation and test signal."""
    return (
        split_has_positive_signal(validation, min_explained=min_explained_per_window)
        and split_has_positive_signal(test, min_explained=min_explained_per_window)
    )


def has_active_predicate(rule: Rule) -> bool:
    """Return whether a fitted rule has at least one active filtering predicate."""
    checker = getattr(rule, "has_active_predicate", None)
    if checker is None:
        return True
    return bool(checker() if callable(checker) else checker)


def promotion_rejection_reason(
    rule: Rule,
    validation: SplitMetrics,
    test: SplitMetrics,
    *,
    min_explained_per_window: int = 1,
) -> Optional[str]:
    """Return the reason a rule cannot be promoted, or None if eligible."""
    if not has_active_predicate(rule):
        return "no_active_predicate"
    if not promotion_eligible(
        validation,
        test,
        min_explained_per_window=min_explained_per_window,
    ):
        return "insufficient_oos_signal"
    return None
