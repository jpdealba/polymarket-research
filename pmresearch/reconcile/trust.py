"""Wallet trust derivation from reconciliation facts."""

from __future__ import annotations

from dataclasses import dataclass
from collections import Counter

from sqlalchemy import text
from sqlalchemy.orm import Session

from .checks import ReconciliationFact


@dataclass(frozen=True)
class WalletTrust:
    wallet: str
    status: str
    since_ts: int
    updated_ts: int
    reason: str
    last_reconciliation_ts: int

    def as_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "status": self.status,
            "since_ts": self.since_ts,
            "updated_ts": self.updated_ts,
            "reason": self.reason,
            "last_reconciliation_ts": self.last_reconciliation_ts,
        }


def derive_trust(wallet: str, run_ts: int, facts: list[ReconciliationFact]) -> WalletTrust:
    size_facts = [fact for fact in facts if fact.check_type == "positions_size"]
    failures = [fact for fact in size_facts if fact.status == "fail"]
    warnings = [fact for fact in size_facts if fact.status == "warn"]
    if failures:
        status = "untrusted"
        reason = _reason("fail", failures)
    elif warnings:
        status = "warn"
        reason = _reason("warn", warnings)
    else:
        status = "trusted"
        reason = "all hard reconciliation checks passed"
    return WalletTrust(
        wallet=wallet.lower(),
        status=status,
        since_ts=run_ts,
        updated_ts=run_ts,
        reason=reason,
        last_reconciliation_ts=run_ts,
    )


def _reason(prefix: str, facts: list[ReconciliationFact]) -> str:
    counts = Counter(fact.reason_code for fact in facts)
    parts = ", ".join(f"{reason}={count}" for reason, count in counts.most_common())
    return f"{prefix}: {parts}"


def upsert_wallet_trust(session: Session, trust: WalletTrust) -> WalletTrust:
    existing = session.execute(
        text("SELECT status, since_ts FROM wallet_trust WHERE wallet = :w"),
        {"w": trust.wallet},
    ).fetchone()
    since_ts = trust.since_ts
    if existing is not None and existing.status == trust.status:
        since_ts = int(existing.since_ts)
    session.execute(
        text(
            "INSERT INTO wallet_trust "
            "(wallet, status, since_ts, updated_ts, reason, last_reconciliation_ts) "
            "VALUES (:wallet, :status, :since_ts, :updated_ts, :reason, :last_ts) "
            "ON CONFLICT(wallet) DO UPDATE SET "
            "status = excluded.status, "
            "since_ts = excluded.since_ts, "
            "updated_ts = excluded.updated_ts, "
            "reason = excluded.reason, "
            "last_reconciliation_ts = excluded.last_reconciliation_ts"
        ),
        {
            "wallet": trust.wallet,
            "status": trust.status,
            "since_ts": since_ts,
            "updated_ts": trust.updated_ts,
            "reason": trust.reason,
            "last_ts": trust.last_reconciliation_ts,
        },
    )
    return WalletTrust(
        wallet=trust.wallet,
        status=trust.status,
        since_ts=since_ts,
        updated_ts=trust.updated_ts,
        reason=trust.reason,
        last_reconciliation_ts=trust.last_reconciliation_ts,
    )


def fetch_wallet_trust(session: Session, wallet: str | None = None) -> list[WalletTrust]:
    where = ""
    params = {}
    if wallet is not None:
        where = "WHERE wallet = :w"
        params["w"] = wallet.lower()
    rows = session.execute(
        text(
            "SELECT wallet, status, since_ts, updated_ts, reason, last_reconciliation_ts "
            f"FROM wallet_trust {where} ORDER BY wallet"
        ),
        params,
    ).fetchall()
    return [
        WalletTrust(
            wallet=row.wallet,
            status=row.status,
            since_ts=int(row.since_ts),
            updated_ts=int(row.updated_ts),
            reason=row.reason,
            last_reconciliation_ts=int(row.last_reconciliation_ts),
        )
        for row in rows
    ]
