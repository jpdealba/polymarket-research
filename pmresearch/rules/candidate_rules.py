"""Phase 21 candidate rules (A–G).

Each rule reads only pre-fill features from the ``microstructure_lifecycle_dataset``
rows.  The ``FORBIDDEN_FEATURES`` guard in ``base.py`` documents which columns
must never be used for rule decisions.

Rules are dataclasses so they carry their tuned parameters after fitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

from .base import RuleDecision, row_decimal

_ZERO = Decimal("0")
_ONE = Decimal("1")


# ── Rule A — Spread Capture ──────────────────────────────────────────────────


@dataclass
class SpreadCapture:
    """Enter as maker if spread_before >= threshold and fill price is on the
    favourable side of mid.

    Parameters
    ----------
    min_spread_bps : Decimal
        Minimum spread in basis points for the rule to fire.
    min_fill_improvement_bps : Decimal
        Minimum improvement of fill price vs mid (in bps) — i.e. the fill
        must be at or better than mid minus this threshold for a BUY, or mid
        plus this threshold for a SELL.
    """

    min_spread_bps: Decimal = Decimal("50")
    min_fill_improvement_bps: Decimal = Decimal("10")

    @property
    def name(self) -> str:
        return "spread_capture"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter as maker when spread is wide and fill price improves against mid."

    @property
    def parameters(self) -> dict:
        return {
            "min_spread_bps": str(self.min_spread_bps),
            "min_fill_improvement_bps": str(self.min_fill_improvement_bps),
        }

    def applies(self, row: dict) -> RuleDecision:
        spread_bps = row_decimal(row, "spread_bps")
        mid = row_decimal(row, "mid_before")
        fill_price = row_decimal(row, "fill_price")
        side = row.get("side")

        features = {
            "spread_bps": row.get("spread_bps"),
            "mid_before": row.get("mid_before"),
            "fill_price": row.get("fill_price"),
            "side": side,
        }

        if spread_bps is None or mid is None or fill_price is None or side is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing required feature(s)",
            )

        if spread_bps < self.min_spread_bps:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"spread_bps {spread_bps} < threshold {self.min_spread_bps}",
            )

        if side == "BUY":
            improvement = mid - fill_price
        elif side == "SELL":
            improvement = fill_price - mid
        else:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"unknown side {side!r}",
            )

        if mid == _ZERO:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="mid is zero",
            )

        improvement_bps = improvement / mid * Decimal(10000)
        if improvement_bps < self.min_fill_improvement_bps:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=(
                    f"fill improvement {improvement_bps:.1f} bps < "
                    f"threshold {self.min_fill_improvement_bps} bps"
                ),
            )

        return RuleDecision(
            applies=True,
            features_used=features,
            explanation=(
                f"spread {spread_bps:.1f} bps >= {self.min_spread_bps}, "
                f"fill improvement {improvement_bps:.1f} bps"
            ),
        )


# ── Rule B — Inventory Balancing ─────────────────────────────────────────────


@dataclass
class InventoryBalancing:
    """Enter if the fill reduces directional exposure or increases bond inventory.

    Parameters
    ----------
    require_directional_reduction : bool
        If True, the fill must reduce |directional_before|.
    require_bond_increase : bool
        If True, the fill must increase bond inventory.
    min_abs_directional_before : Decimal
        Only consider this rule when existing directional exposure is at least
        this large (avoids trivial signals on near-flat positions).
    """

    require_directional_reduction: bool = True
    require_bond_increase: bool = True
    min_abs_directional_before: Decimal = Decimal("1")

    @property
    def name(self) -> str:
        return "inventory_balancing"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter if the fill reduces directional exposure or increases bond inventory."

    @property
    def parameters(self) -> dict:
        return {
            "require_directional_reduction": str(self.require_directional_reduction),
            "require_bond_increase": str(self.require_bond_increase),
            "min_abs_directional_before": str(self.min_abs_directional_before),
        }

    def applies(self, row: dict) -> RuleDecision:
        directional_before = row_decimal(row, "directional_before")
        bond_before = row_decimal(row, "bond_before")
        side = row.get("side")
        qty_token_before = row_decimal(row, "qty_token_before")
        qty_complement_before = row_decimal(row, "qty_complement_before")

        features = {
            "directional_before": row.get("directional_before"),
            "bond_before": row.get("bond_before"),
            "side": side,
            "qty_token_before": row.get("qty_token_before"),
            "qty_complement_before": row.get("qty_complement_before"),
        }

        if directional_before is None or bond_before is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing directional_before or bond_before",
            )

        if abs(directional_before) < self.min_abs_directional_before:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=(
                    f"|directional_before|={abs(directional_before)} < "
                    f"threshold {self.min_abs_directional_before}"
                ),
            )

        reasons: list[str] = []
        directional_reduced = False
        bond_increased = False

        if self.require_directional_reduction:
            # Infer directional reduction from side + directional_before:
            # SELL when long (directional_before > 0) reduces directional,
            # BUY when short (directional_before < 0) reduces directional.
            if side == "SELL" and directional_before > 0:
                directional_reduced = True
                reasons.append(f"SELL into long position (directional_before={directional_before:.2f})")
            elif side == "BUY" and directional_before < 0:
                directional_reduced = True
                reasons.append(f"BUY into short position (directional_before={directional_before:.2f})")
            else:
                reasons.append(
                    f"side={side} does not reduce directional "
                    f"(directional_before={directional_before:.2f})"
                )

        if self.require_bond_increase:
            # Infer bond increase from side + position:
            # BUY when holding complement (qty_complement_before > 0) pairs tokens,
            # SELL when holding token (qty_token_before > 0) pairs tokens.
            if side == "BUY" and qty_complement_before is not None and qty_complement_before > 0:
                bond_increased = True
                reasons.append(f"BUY with complement position ({qty_complement_before:.2f})")
            elif side == "SELL" and qty_token_before is not None and qty_token_before > 0:
                bond_increased = True
                reasons.append(f"SELL with token position ({qty_token_before:.2f})")
            else:
                reasons.append(
                    f"side={side} does not increase bond "
                    f"(token={qty_token_before}, complement={qty_complement_before})"
                )

        fires = True
        if self.require_directional_reduction and not directional_reduced:
            fires = False
        if self.require_bond_increase and not bond_increased:
            fires = False

        return RuleDecision(
            applies=fires,
            features_used=features,
            explanation="; ".join(reasons) if reasons else "no conditions checked",
        )


# ── Rule C — Completion-Set Edge ─────────────────────────────────────────────


@dataclass
class CompletionSetEdge:
    """Enter if token + complement can form a bond with expected cost < 1.

    The rule estimates the total cost of acquiring a full bond pair by
    checking whether the fill price and the complement's best ask are
    together below the bond redemption value ($1).

    Parameters
    ----------
    max_bond_cost : Decimal
        Maximum total cost (fill price + complement ask) to consider the
        edge present.  Default 0.98 gives a 2-cent edge.
    """

    max_bond_cost: Decimal = Decimal("0.98")

    @property
    def name(self) -> str:
        return "completion_set_edge"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter if token + complement can form a bond with total cost < $1."

    @property
    def parameters(self) -> dict:
        return {"max_bond_cost": str(self.max_bond_cost)}

    def applies(self, row: dict) -> RuleDecision:
        fill_price = row_decimal(row, "fill_price")
        best_ask_before = row_decimal(row, "best_ask_before")
        side = row.get("side")
        qty_token_before = row_decimal(row, "qty_token_before")
        qty_complement_before = row_decimal(row, "qty_complement_before")

        features = {
            "fill_price": row.get("fill_price"),
            "best_ask_before": row.get("best_ask_before"),
            "side": side,
            "qty_token_before": row.get("qty_token_before"),
            "qty_complement_before": row.get("qty_complement_before"),
        }

        if fill_price is None or best_ask_before is None or side is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing fill_price, best_ask_before, or side",
            )

        if qty_token_before is None or qty_complement_before is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing position data (qty_token_before or qty_complement_before)",
            )

        if side == "BUY":
            cost_this_token = fill_price
            cost_complement = best_ask_before
        elif side == "SELL":
            cost_this_token = best_ask_before
            cost_complement = fill_price
        else:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"unknown side {side!r}",
            )

        total_cost = cost_this_token + cost_complement

        if total_cost > self.max_bond_cost:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"total bond cost {total_cost:.4f} > {self.max_bond_cost}",
            )

        return RuleDecision(
            applies=True,
            features_used=features,
            explanation=f"total bond cost {total_cost:.4f} < {self.max_bond_cost}",
        )


# ── Rule D — Depth / Imbalance ───────────────────────────────────────────────


@dataclass
class DepthImbalance:
    """Enter if book depth imbalance favours passive filling without getting trapped.

    Parameters
    ----------
    min_imbalance : Decimal
        Minimum absolute book_imbalance_top5 to trigger (range 0..1).
    require_favourable_side : bool
        If True, the fill must be on the same side as the depth imbalance
        (i.e. more resting orders on the fill's side = more support).
    """

    min_imbalance: Decimal = Decimal("0.3")
    require_favourable_side: bool = True

    @property
    def name(self) -> str:
        return "depth_imbalance"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter if depth imbalance favours passive filling without getting trapped."

    @property
    def parameters(self) -> dict:
        return {
            "min_imbalance": str(self.min_imbalance),
            "require_favourable_side": str(self.require_favourable_side),
        }

    def applies(self, row: dict) -> RuleDecision:
        imbalance = row_decimal(row, "book_imbalance_top5")
        side = row.get("side")
        bid_depth = row_decimal(row, "bid_depth_top5")
        ask_depth = row_decimal(row, "ask_depth_top5")

        features = {
            "book_imbalance_top5": row.get("book_imbalance_top5"),
            "side": side,
            "bid_depth_top5": row.get("bid_depth_top5"),
            "ask_depth_top5": row.get("ask_depth_top5"),
        }

        if imbalance is None or side is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing book_imbalance_top5 or side",
            )

        if abs(imbalance) < self.min_imbalance:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"|imbalance|={abs(imbalance):.3f} < {self.min_imbalance}",
            )

        if self.require_favourable_side:
            if side == "BUY" and imbalance < 0:
                return RuleDecision(
                    applies=False,
                    features_used=features,
                    explanation=(
                        f"BUY but imbalance={imbalance:.3f} "
                        "(more ask depth — unfavorable for passive buy)"
                    ),
                )
            if side == "SELL" and imbalance > 0:
                return RuleDecision(
                    applies=False,
                    features_used=features,
                    explanation=(
                        f"SELL but imbalance={imbalance:.3f} "
                        "(more bid depth — unfavorable for passive sell)"
                    ),
                )

        return RuleDecision(
            applies=True,
            features_used=features,
            explanation=f"imbalance={imbalance:.3f} favours {side} filling",
        )


# ── Rule E — Event Timing ────────────────────────────────────────────────────


@dataclass
class EventTiming:
    """Enter only in specific windows before/during events where flow is high.

    Parameters
    ----------
    allowed_hours_utc : tuple[int, ...]
        Hours of the day (UTC) when the rule fires.  Empty tuple = all hours.
    max_time_to_event_start_s : Optional[int]
        If set, only fire when the trade is within this many seconds of event
        start (i.e. time_to_event_start_s <= this value).
    min_time_to_event_start_s : Optional[int]
        If set, only fire when the trade is at least this many seconds before
        event start (i.e. time_to_event_start_s >= this value).
    """

    allowed_hours_utc: tuple[int, ...] = ()
    max_time_to_event_start_s: Optional[int] = None
    min_time_to_event_start_s: Optional[int] = None

    @property
    def name(self) -> str:
        return "event_timing"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter only in specific time windows relative to events."

    @property
    def parameters(self) -> dict:
        return {
            "allowed_hours_utc": list(self.allowed_hours_utc),
            "max_time_to_event_start_s": self.max_time_to_event_start_s,
            "min_time_to_event_start_s": self.min_time_to_event_start_s,
        }

    def has_active_predicate(self) -> bool:
        return bool(
            self.allowed_hours_utc
            or self.max_time_to_event_start_s is not None
            or self.min_time_to_event_start_s is not None
        )

    def applies(self, row: dict) -> RuleDecision:
        trade_hour = row.get("trade_hour_utc")
        time_to_start = row.get("time_to_event_start_s")

        features = {
            "trade_hour_utc": row.get("trade_hour_utc"),
            "time_to_event_start_s": row.get("time_to_event_start_s"),
        }

        if trade_hour is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing trade_hour_utc",
            )

        try:
            hour = int(trade_hour_utc := trade_hour)
        except (TypeError, ValueError):
            hour = None

        if hour is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"unparseable trade_hour_utc={trade_hour!r}",
            )

        if self.allowed_hours_utc and hour not in self.allowed_hours_utc:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"hour {hour} not in allowed {self.allowed_hours_utc}",
            )

        if time_to_start is not None:
            try:
                tts = int(time_to_start)
            except (TypeError, ValueError):
                tts = None
            if tts is not None:
                if self.max_time_to_event_start_s is not None and tts > self.max_time_to_event_start_s:
                    return RuleDecision(
                        applies=False,
                        features_used=features,
                        explanation=(
                            f"time_to_event_start {tts}s > max "
                            f"{self.max_time_to_event_start_s}s"
                        ),
                    )
                if self.min_time_to_event_start_s is not None and tts < self.min_time_to_event_start_s:
                    return RuleDecision(
                        applies=False,
                        features_used=features,
                        explanation=(
                            f"time_to_event_start {tts}s < min "
                            f"{self.min_time_to_event_start_s}s"
                        ),
                    )

        return RuleDecision(
            applies=True,
            features_used=features,
            explanation=f"hour {hour} within allowed window",
        )


# ── Rule F — Correlated Sibling Markets ──────────────────────────────────────


@dataclass
class CorrelatedSiblingMarkets:
    """Enter if the wallet holds positions in multiple sibling markets of a
    negRisk event and the net event-level exposure suggests misalignment.

    This rule fires when:
    - the market is a negRisk event member,
    - the wallet has non-zero event-level exposure before the fill, and
    - the fill improves (reduces |event_exposure_before|).

    Parameters
    ----------
    min_abs_event_exposure : Decimal
        Minimum absolute event-level exposure before the fill to consider
        the rule relevant.
    """

    min_abs_event_exposure: Decimal = Decimal("5")

    @property
    def name(self) -> str:
        return "correlated_sibling_markets"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return "Enter if sibling markets within the same event are misaligned."

    @property
    def parameters(self) -> dict:
        return {"min_abs_event_exposure": str(self.min_abs_event_exposure)}

    def applies(self, row: dict) -> RuleDecision:
        event_exposure_before = row_decimal(row, "event_exposure_before")
        side = row.get("side")

        features = {
            "event_exposure_before": row.get("event_exposure_before"),
            "side": side,
        }

        if event_exposure_before is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing event_exposure_before (not a negRisk event or no sibling data)",
            )

        if side is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing side",
            )

        if abs(event_exposure_before) < self.min_abs_event_exposure:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=(
                    f"|event_exposure_before|={abs(event_exposure_before)} < "
                    f"threshold {self.min_abs_event_exposure}"
                ),
            )

        # Infer whether the fill would reduce exposure:
        # SELL when net long (exposure > 0) or BUY when net short (exposure < 0)
        improving = (
            (event_exposure_before > 0 and side == "SELL")
            or (event_exposure_before < 0 and side == "BUY")
        )

        return RuleDecision(
            applies=improving,
            features_used=features,
            explanation=(
                f"event exposure before={event_exposure_before:.2f}, side={side}"
                f"{' (improving)' if improving else ' (not improving)'}"
            ),
        )


# ── Rule G — Closed-Cycle Event Trading ──────────────────────────────────────


@dataclass
class ClosedCycleEventTrading:
    """Enter if there is an active event with high liquidity, sufficient spread,
    and the possibility to close exposure within the same event.

    Composite rule combining: wide spread + negRisk event + existing event
    exposure + favourable fill direction.

    Parameters
    ----------
    min_spread_bps : Decimal
        Minimum spread in basis points.
    min_abs_event_exposure : Decimal
        Minimum absolute event-level exposure before the fill.
    """

    min_spread_bps: Decimal = Decimal("30")
    min_abs_event_exposure: Decimal = Decimal("5")

    @property
    def name(self) -> str:
        return "closed_cycle_event_trading"

    @property
    def version(self) -> int:
        return 1

    @property
    def description(self) -> str:
        return (
            "Enter if active event with high liquidity, sufficient spread, "
            "and possibility to close exposure within the same event."
        )

    @property
    def parameters(self) -> dict:
        return {
            "min_spread_bps": str(self.min_spread_bps),
            "min_abs_event_exposure": str(self.min_abs_event_exposure),
        }

    def applies(self, row: dict) -> RuleDecision:
        spread_bps = row_decimal(row, "spread_bps")
        event_exposure_before = row_decimal(row, "event_exposure_before")
        side = row.get("side")
        directional_before = row_decimal(row, "directional_before")

        features = {
            "spread_bps": row.get("spread_bps"),
            "event_exposure_before": row.get("event_exposure_before"),
            "side": side,
            "directional_before": row.get("directional_before"),
        }

        reasons: list[str] = []

        if spread_bps is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="missing spread_bps",
            )

        if spread_bps < self.min_spread_bps:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=f"spread {spread_bps:.1f} bps < {self.min_spread_bps}",
            )
        reasons.append(f"spread {spread_bps:.1f} bps")

        if event_exposure_before is None:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation="no event-level exposure (not negRisk or no siblings)",
            )

        if abs(event_exposure_before) < self.min_abs_event_exposure:
            return RuleDecision(
                applies=False,
                features_used=features,
                explanation=(
                    f"|event_exposure|={abs(event_exposure_before)} < "
                    f"{self.min_abs_event_exposure}"
                ),
            )
        reasons.append(f"event_exposure={event_exposure_before:.2f}")

        if side is not None and directional_before is not None:
            if side == "BUY" and directional_before > 0:
                reasons.append("BUY into existing long — scaling")
            elif side == "SELL" and directional_before < 0:
                reasons.append("SELL into existing short — scaling")
            elif side == "BUY" and directional_before < 0:
                reasons.append("BUY opposing existing short — hedging")
            elif side == "SELL" and directional_before > 0:
                reasons.append("SELL opposing existing long — hedging")

        return RuleDecision(
            applies=True,
            features_used=features,
            explanation="; ".join(reasons),
        )


# ── registry ─────────────────────────────────────────────────────────────────

ALL_CANDIDATE_RULES: list[type] = [
    SpreadCapture,
    InventoryBalancing,
    CompletionSetEdge,
    DepthImbalance,
    EventTiming,
    CorrelatedSiblingMarkets,
    ClosedCycleEventTrading,
]


def default_rule_instances() -> list[Rule]:
    """Return one instance of each candidate rule with default parameters."""
    return [cls() for cls in ALL_CANDIDATE_RULES]  # type: ignore[call-arg]
