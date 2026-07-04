"""Phase 17 — Staleness and failure alerting.

Log-based alerts surfaced in `pmr sync status` and the dashboard data-quality
view.  Optional Telegram notification hook (config-gated: PMR_TELEGRAM_BOT_TOKEN
+ PMR_TELEGRAM_CHAT_ID must both be set).

Alerts are emitted as structured log messages at WARNING/ERROR level.  The
Telegram hook sends a plain-text message when a wallet transitions to a
stale/failing state or recovers.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Optional

import httpx
from sqlalchemy import text
from sqlalchemy.orm import Session

from .config import Settings
from .walletmanager.manager import SyncStateRow, get_sync_state, is_stale, list_wallets

logger = logging.getLogger(__name__)

# Default cadence: 15 minutes.  A wallet that hasn't synced in 3× this is stale.
DEFAULT_CADENCE_S = 900
STALE_MULTIPLIER = 3.0
FAILURE_THRESHOLD = 3


class AlertSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


@dataclass(frozen=True)
class WalletAlert:
    wallet: str
    severity: AlertSeverity
    alert_type: str
    message: str
    timestamp: str
    consecutive_failures: int = 0
    last_success_at: Optional[str] = None
    last_error: Optional[str] = None


def check_wallet_alerts(
    session: Session,
    settings: Settings,
    *,
    cadence_s: int = DEFAULT_CADENCE_S,
    stale_multiplier: float = STALE_MULTIPLIER,
    failure_threshold: int = FAILURE_THRESHOLD,
) -> list[WalletAlert]:
    """Scan all active wallets and emit alerts for staleness / failures."""
    alerts: list[WalletAlert] = []
    now = datetime.now(timezone.utc).isoformat()

    for wallet_row in list_wallets(session, active_only=True):
        address = wallet_row.address
        state = get_sync_state(session, address)
        if state is None:
            continue

        # Never-synced wallets: informational
        if state.last_success_at is None and state.status == "new":
            alerts.append(
                WalletAlert(
                    wallet=address,
                    severity=AlertSeverity.INFO,
                    alert_type="wallet_new",
                    message=f"Wallet {address} has never synced successfully.",
                    timestamp=now,
                )
            )
            continue

        # Staleness check
        stale = is_stale(session, address, cadence_s=cadence_s, stale_multiplier=stale_multiplier)
        if stale:
            alerts.append(
                WalletAlert(
                    wallet=address,
                    severity=AlertSeverity.WARNING,
                    alert_type="sync_stale",
                    message=(
                        f"Wallet {address} is STALE — last success "
                        f"{state.last_success_at} ({_age_str(state.last_success_at)})"
                    ),
                    timestamp=now,
                    consecutive_failures=state.consecutive_failures,
                    last_success_at=state.last_success_at,
                    last_error=state.last_error,
                )
            )

        # Consecutive failure threshold
        if state.consecutive_failures >= failure_threshold:
            alerts.append(
                WalletAlert(
                    wallet=address,
                    severity=AlertSeverity.ERROR,
                    alert_type="sync_failing",
                    message=(
                        f"Wallet {address} has {state.consecutive_failures} "
                        f"consecutive sync failures. Last error: {state.last_error or 'unknown'}"
                    ),
                    timestamp=now,
                    consecutive_failures=state.consecutive_failures,
                    last_success_at=state.last_success_at,
                    last_error=state.last_error,
                )
            )

    return alerts


def emit_alerts(alerts: list[WalletAlert]) -> None:
    """Emit alerts as structured log messages."""
    for alert in alerts:
        msg = json.dumps(
            {
                "wallet": alert.wallet,
                "severity": alert.severity.value,
                "alert_type": alert.alert_type,
                "message": alert.message,
                "timestamp": alert.timestamp,
                "consecutive_failures": alert.consecutive_failures,
                "last_success_at": alert.last_success_at,
                "last_error": alert.last_error,
            }
        )
        if alert.severity == AlertSeverity.ERROR:
            logger.error("ALERT: %s", msg)
        elif alert.severity == AlertSeverity.WARNING:
            logger.warning("ALERT: %s", msg)
        else:
            logger.info("ALERT: %s", msg)


def send_telegram_alerts(
    alerts: list[WalletAlert],
    *,
    bot_token: str = "",
    chat_id: str = "",
) -> int:
    """Send alerts via Telegram.  Returns the number of messages sent.
    Silently returns 0 if config is incomplete or HTTP fails."""
    bot_token = bot_token or os.environ.get("PMR_TELEGRAM_BOT_TOKEN", "")
    chat_id = chat_id or os.environ.get("PMR_TELEGRAM_CHAT_ID", "")
    if not bot_token or not chat_id:
        return 0

    # Only send WARNING and ERROR alerts
    sendable = [a for a in alerts if a.severity in (AlertSeverity.WARNING, AlertSeverity.ERROR)]
    if not sendable:
        return 0

    sent = 0
    for alert in sendable:
        text_msg = (
            f"[{alert.severity.value.upper()}] {alert.alert_type}\n"
            f"{alert.message}\n"
            f"Time: {alert.timestamp}"
        )
        try:
            resp = httpx.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": text_msg},
                timeout=10,
            )
            if resp.status_code == 200:
                sent += 1
        except Exception:
            logger.debug("Telegram send failed for alert %s", alert.alert_type, exc_info=True)
    return sent


def run_staleness_check(settings: Settings) -> list[WalletAlert]:
    """Full staleness check cycle: scan, emit logs, send Telegram."""
    from .db.engine import get_session_factory

    session = get_session_factory(settings)()
    try:
        alerts = check_wallet_alerts(session, settings)
        emit_alerts(alerts)
        send_telegram_alerts(alerts)
        return alerts
    finally:
        session.close()


def _age_str(iso_ts: Optional[str]) -> str:
    """Human-readable age string from an ISO timestamp."""
    if iso_ts is None:
        return "never"
    try:
        dt = datetime.fromisoformat(iso_ts)
        age_s = (datetime.now(timezone.utc) - dt).total_seconds()
        if age_s < 60:
            return f"{age_s:.0f}s ago"
        if age_s < 3600:
            return f"{age_s / 60:.0f}m ago"
        if age_s < 86400:
            return f"{age_s / 3600:.1f}h ago"
        return f"{age_s / 86400:.1f}d ago"
    except (ValueError, TypeError):
        return "unknown"
