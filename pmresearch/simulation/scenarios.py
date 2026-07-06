"""Scenario assumptions for Phase 22 counterfactual simulation."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True)
class SimOrder:
    """A prospective order produced from allowed pre-snapshot context."""

    side: str
    order_price: Decimal
    order_size: Decimal
    reason: str


@dataclass(frozen=True)
class FillAssumption:
    """How a prospective order fills under a scenario."""

    would_fill: bool
    fill_price: Optional[Decimal] = None
    fill_size: Optional[Decimal] = None
    fill_reason: str = ""


@dataclass(frozen=True)
class ScenarioConfig:
    """Configuration for a simulation scenario."""

    name: str
    description: str
    fill_rate_multiplier: Decimal
    slippage_bps: Decimal
    max_order_size: Decimal
    depth_fraction: Decimal
    min_spread_bps_for_fill: Optional[Decimal]
    min_book_depth_for_fill: Optional[Decimal]
    max_book_age_s: Optional[int]
    min_context_status: str
    risk_limit_multiplier: Decimal


OPTIMISTIC = ScenarioConfig(
    name="optimistic",
    description="Highest fillability, no extra slippage, base risk limits.",
    fill_rate_multiplier=Decimal("1.0"),
    slippage_bps=Decimal("0"),
    max_order_size=Decimal("25"),
    depth_fraction=Decimal("1.0"),
    min_spread_bps_for_fill=None,
    min_book_depth_for_fill=None,
    max_book_age_s=None,
    min_context_status="weak",
    risk_limit_multiplier=Decimal("1.0"),
)

MEDIUM = ScenarioConfig(
    name="medium",
    description="Lower fillability, moderate slippage, usable context required.",
    fill_rate_multiplier=Decimal("0.4"),
    slippage_bps=Decimal("15"),
    max_order_size=Decimal("15"),
    depth_fraction=Decimal("0.6"),
    min_spread_bps_for_fill=Decimal("20"),
    min_book_depth_for_fill=Decimal("5"),
    max_book_age_s=45,
    min_context_status="usable",
    risk_limit_multiplier=Decimal("0.75"),
)

CONSERVATIVE = ScenarioConfig(
    name="conservative",
    description="Lowest fillability, worst slippage, strict context and risk limits.",
    fill_rate_multiplier=Decimal("0.2"),
    slippage_bps=Decimal("30"),
    max_order_size=Decimal("10"),
    depth_fraction=Decimal("0.35"),
    min_spread_bps_for_fill=Decimal("30"),
    min_book_depth_for_fill=Decimal("10"),
    max_book_age_s=30,
    min_context_status="good",
    risk_limit_multiplier=Decimal("0.5"),
)

ALL_SCENARIOS: dict[str, ScenarioConfig] = {
    "optimistic": OPTIMISTIC,
    "medium": MEDIUM,
    "conservative": CONSERVATIVE,
}

_CONTEXT_RANK: dict[str, int] = {
    "excellent": 5,
    "good": 4,
    "usable": 3,
    "weak": 2,
    "stale": 1,
    "missing": 0,
}


def context_meets_minimum(actual: str, minimum: str) -> bool:
    return _CONTEXT_RANK.get(actual, 0) >= _CONTEXT_RANK.get(minimum, 0)


def decide_fill(
    *,
    order: SimOrder,
    scenario: ScenarioConfig,
    context_status: str,
    book_age_s: Optional[int],
    spread_bps: Optional[Decimal],
    bid_depth_top1: Optional[Decimal],
    ask_depth_top1: Optional[Decimal],
    deterministic_seq: int,
) -> FillAssumption:
    """Decide whether a prospective order fills using only book-before context."""
    if not context_meets_minimum(context_status, scenario.min_context_status):
        return FillAssumption(
            would_fill=False,
            fill_reason=f"context {context_status} < required {scenario.min_context_status}",
        )

    if scenario.max_book_age_s is not None and book_age_s is not None:
        if book_age_s > scenario.max_book_age_s:
            return FillAssumption(
                would_fill=False,
                fill_reason=f"book age {book_age_s}s > max {scenario.max_book_age_s}s",
            )

    if scenario.min_spread_bps_for_fill is not None and spread_bps is not None:
        if spread_bps < scenario.min_spread_bps_for_fill:
            return FillAssumption(
                would_fill=False,
                fill_reason=(
                    f"spread {spread_bps} bps < "
                    f"required {scenario.min_spread_bps_for_fill} bps"
                ),
            )

    relevant_depth = ask_depth_top1 if order.side == "BUY" else bid_depth_top1
    if scenario.min_book_depth_for_fill is not None and relevant_depth is not None:
        if relevant_depth < scenario.min_book_depth_for_fill:
            return FillAssumption(
                would_fill=False,
                fill_reason=(
                    f"book depth {relevant_depth} < "
                    f"required {scenario.min_book_depth_for_fill}"
                ),
            )

    if scenario.fill_rate_multiplier < _ONE:
        period = 5
        accept_count = int(scenario.fill_rate_multiplier * Decimal(period))
        accept_count = max(1, accept_count)
        if deterministic_seq % period >= accept_count:
            return FillAssumption(
                would_fill=False,
                fill_reason=(
                    f"fill rate gate: multiplier={scenario.fill_rate_multiplier}, "
                    f"seq={deterministic_seq}"
                ),
            )

    depth_cap = order.order_size
    if relevant_depth is not None and relevant_depth > _ZERO:
        depth_cap = min(depth_cap, relevant_depth * scenario.depth_fraction)
    fill_size = min(order.order_size, depth_cap, scenario.max_order_size)
    if fill_size <= _ZERO:
        return FillAssumption(would_fill=False, fill_reason="zero fillable size")

    fill_price = order.order_price
    if scenario.slippage_bps > _ZERO and fill_price > _ZERO:
        slippage = scenario.slippage_bps / Decimal("10000")
        if order.side == "BUY":
            fill_price = fill_price * (_ONE + slippage)
        else:
            fill_price = fill_price * (_ONE - slippage)

    return FillAssumption(
        would_fill=True,
        fill_price=fill_price,
        fill_size=fill_size,
        fill_reason="accepted",
    )
