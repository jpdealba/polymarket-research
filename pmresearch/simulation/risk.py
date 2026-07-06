"""Risk limit definitions and checks for counterfactual simulation."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Optional

_ZERO = Decimal("0")


@dataclass
class RiskLimits:
    """Configurable risk limits for simulation."""

    max_position_per_token: Decimal = Decimal("500")
    max_directional_per_market: Decimal = Decimal("1000")
    max_event_exposure: Decimal = Decimal("2000")
    max_daily_loss: Decimal = Decimal("100")
    max_capital_deployed: Decimal = Decimal("5000")
    max_order_size: Decimal = Decimal("25")
    max_stale_book_age_s: int = 60

    def to_dict(self) -> dict:
        return {
            "max_position_per_token": str(self.max_position_per_token),
            "max_directional_per_market": str(self.max_directional_per_market),
            "max_event_exposure": str(self.max_event_exposure),
            "max_daily_loss": str(self.max_daily_loss),
            "max_capital_deployed": str(self.max_capital_deployed),
            "max_order_size": str(self.max_order_size),
            "max_stale_book_age_s": self.max_stale_book_age_s,
        }


@dataclass(frozen=True)
class RiskEvent:
    """A single risk limit breach."""

    event_type: str
    limit_name: str
    limit_value: str
    actual_value: str
    token_id: Optional[str] = None
    condition_id: Optional[str] = None
    description: str = ""
    timestamp: int = 0


def check_position_limit(
    token_id: str,
    qty_token: Decimal,
    limits: RiskLimits,
    ts: int,
) -> Optional[RiskEvent]:
    """Check if position in a single token exceeds the limit."""
    if abs(qty_token) > limits.max_position_per_token:
        return RiskEvent(
            event_type="position_breach",
            limit_name="max_position_per_token",
            limit_value=str(limits.max_position_per_token),
            actual_value=str(qty_token),
            token_id=token_id,
            description=(
                f"position {qty_token} exceeds "
                f"limit {limits.max_position_per_token}"
            ),
            timestamp=ts,
        )
    return None


def check_daily_loss(
    daily_pnl: Decimal,
    limits: RiskLimits,
    ts: int,
) -> Optional[RiskEvent]:
    """Check if daily PnL loss exceeds the limit."""
    if daily_pnl < _ZERO and abs(daily_pnl) > limits.max_daily_loss:
        return RiskEvent(
            event_type="daily_loss_breach",
            limit_name="max_daily_loss",
            limit_value=str(limits.max_daily_loss),
            actual_value=str(daily_pnl),
            description=(
                f"daily loss {daily_pnl} exceeds "
                f"limit {limits.max_daily_loss}"
            ),
            timestamp=ts,
        )
    return None


def check_capital_deployed(
    capital_used: Decimal,
    limits: RiskLimits,
    ts: int,
) -> Optional[RiskEvent]:
    """Check if total capital deployed exceeds the limit."""
    if capital_used > limits.max_capital_deployed:
        return RiskEvent(
            event_type="capital_breach",
            limit_name="max_capital_deployed",
            limit_value=str(limits.max_capital_deployed),
            actual_value=str(capital_used),
            description=(
                f"capital deployed {capital_used} exceeds "
                f"limit {limits.max_capital_deployed}"
            ),
            timestamp=ts,
        )
    return None


def check_event_exposure(
    event_exposure: Decimal,
    limits: RiskLimits,
    condition_id: Optional[str],
    ts: int,
) -> Optional[RiskEvent]:
    """Check if event-level exposure exceeds the limit."""
    if abs(event_exposure) > limits.max_event_exposure:
        return RiskEvent(
            event_type="event_exposure_breach",
            limit_name="max_event_exposure",
            limit_value=str(limits.max_event_exposure),
            actual_value=str(event_exposure),
            condition_id=condition_id,
            description=(
                f"event exposure {event_exposure} exceeds "
                f"limit {limits.max_event_exposure}"
            ),
            timestamp=ts,
        )
    return None


def check_all_risks(
    *,
    token_id: Optional[str],
    qty_token: Decimal,
    daily_pnl: Decimal,
    capital_used: Decimal,
    event_exposure: Decimal,
    condition_id: Optional[str],
    limits: RiskLimits,
    ts: int,
) -> list[RiskEvent]:
    """Run all risk checks and return any breaches."""
    events: list[RiskEvent] = []

    ev = check_position_limit(token_id or "", qty_token, limits, ts)
    if ev:
        events.append(ev)

    ev = check_daily_loss(daily_pnl, limits, ts)
    if ev:
        events.append(ev)

    ev = check_capital_deployed(capital_used, limits, ts)
    if ev:
        events.append(ev)

    ev = check_event_exposure(event_exposure, limits, condition_id, ts)
    if ev:
        events.append(ev)

    return events
