"""Run reconciliation checks, persist facts, and update wallet trust."""

from __future__ import annotations

from decimal import Decimal
import json
import time

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..rawstore.store import RawStore
from ..projections.daily_equity import latest_daily_equity
from ..sources.dataapi import DataApiSource, PositionsFetchIncomplete
from .checks import (
    RECONCILE_TOLERANCE,
    VALUE_TOLERANCE,
    PresenceProbe,
    ReconciliationFact,
    ReconciliationResult,
    build_reconciliation_result,
    decimal_string,
    decimal_value,
    value_check_fact,
    value_fetch_error_fact,
)
from .trust import WalletTrust, derive_trust, fetch_wallet_trust, upsert_wallet_trust

_INSERT_FACT_SQL = text(
    "INSERT INTO reconciliation_facts "
    "(wallet, ts, check_type, subject, expected, computed, abs_diff, pct_diff, "
    "tolerance, status, source, reason_code, notes) "
    "VALUES (:wallet, :ts, :check_type, :subject, :expected, :computed, :abs_diff, "
    ":pct_diff, :tolerance, :status, :source, :reason_code, :notes)"
)


def run_reconciliation(
    session: Session,
    settings: Settings,
    *,
    wallet: str,
    source: DataApiSource | None = None,
    tolerance: Decimal = RECONCILE_TOLERANCE,
    run_ts: int | None = None,
) -> tuple[ReconciliationResult, WalletTrust]:
    wallet = wallet.lower()
    run_ts = run_ts or int(time.time())
    raw_store = RawStore(settings, session)
    owns_source = source is None
    source = source or DataApiSource()
    try:
        try:
            fetched = source.fetch_positions(raw_store, wallet)
            result = build_reconciliation_result(
                session,
                wallet=wallet,
                run_ts=run_ts,
                remote_positions=list(fetched.positions),
                tolerance=tolerance,
                dust_epsilon=settings.dust_epsilon,
            )
            result = _with_value_check(
                session, source, raw_store, result, tolerance=VALUE_TOLERANCE
            )
        except PositionsFetchIncomplete as exc:
            result = _incomplete_fetch_result(wallet, run_ts, tolerance, str(exc))

        persist_facts(session, result.facts)
        trust = derive_trust(wallet, run_ts, list(result.facts))
        trust = upsert_wallet_trust(session, trust)
        session.commit()
        return result, trust
    except Exception:
        session.rollback()
        raise
    finally:
        if owns_source:
            source.close()


def _with_value_check(
    session: Session,
    source: DataApiSource,
    raw_store: RawStore,
    result: ReconciliationResult,
    *,
    tolerance: Decimal,
) -> ReconciliationResult:
    try:
        fetched = source.fetch_value(raw_store, result.wallet)
        latest = latest_daily_equity(session, result.wallet)
        value_fact = value_check_fact(
            wallet=result.wallet,
            run_ts=result.run_ts,
            oracle_value=fetched.value,
            local_value=latest.portfolio_value if latest else None,
            stale_equity_share=latest.stale_equity_share if latest else None,
            equity_date=latest.date if latest else None,
            tolerance=tolerance,
        )
    except Exception as exc:
        value_fact = value_fetch_error_fact(
            wallet=result.wallet,
            run_ts=result.run_ts,
            message=str(exc),
            tolerance=tolerance,
        )
    return ReconciliationResult(
        wallet=result.wallet,
        run_ts=result.run_ts,
        tolerance=result.tolerance,
        facts=result.facts + (value_fact,),
        remote_positions_total=result.remote_positions_total,
        local_nonzero_holdings_total=result.local_nonzero_holdings_total,
        negative_holdings_presence=result.negative_holdings_presence,
        missing_token_metadata_presence=result.missing_token_metadata_presence,
    )


def _incomplete_fetch_result(
    wallet: str, run_ts: int, tolerance: Decimal, message: str
) -> ReconciliationResult:
    fact = ReconciliationFact(
        wallet=wallet.lower(),
        ts=run_ts,
        check_type="positions_size",
        subject="__positions_fetch__",
        expected=Decimal(0),
        computed=Decimal(0),
        abs_diff=Decimal(0),
        pct_diff=Decimal(0),
        tolerance=tolerance,
        status="fail",
        source="dataapi/positions",
        reason_code="unknown",
        notes={
            "local_present": False,
            "remote_present": False,
            "note": message,
            "fetch_complete": False,
        },
    )
    return ReconciliationResult(
        wallet=wallet.lower(),
        run_ts=run_ts,
        tolerance=tolerance,
        facts=(fact,),
        remote_positions_total=0,
        local_nonzero_holdings_total=0,
        negative_holdings_presence=(),
        missing_token_metadata_presence=(),
    )


def persist_facts(session: Session, facts: tuple[ReconciliationFact, ...]) -> None:
    if not facts:
        return
    session.execute(_INSERT_FACT_SQL, [fact.db_params() for fact in facts])


def latest_reconciliation_result(
    session: Session, wallet: str | None = None
) -> list[tuple[ReconciliationResult, WalletTrust | None]]:
    latest_rows = _latest_run_rows(session, wallet)
    out: list[tuple[ReconciliationResult, WalletTrust | None]] = []
    for row in latest_rows:
        facts = _load_facts(session, row.wallet, int(row.ts))
        result = _result_from_facts(row.wallet, int(row.ts), facts)
        trust_rows = fetch_wallet_trust(session, row.wallet)
        out.append((result, trust_rows[0] if trust_rows else None))
    return out


def _latest_run_rows(session: Session, wallet: str | None) -> list:
    where = ""
    params = {}
    if wallet is not None:
        where = "WHERE wallet = :w"
        params["w"] = wallet.lower()
    return session.execute(
        text(
            "SELECT wallet, MAX(ts) AS ts FROM reconciliation_facts "
            f"{where} GROUP BY wallet ORDER BY wallet"
        ),
        params,
    ).fetchall()


def _load_facts(session: Session, wallet: str, run_ts: int) -> tuple[ReconciliationFact, ...]:
    rows = session.execute(
        text(
            "SELECT wallet, ts, check_type, subject, expected, computed, abs_diff, "
            "pct_diff, tolerance, status, source, reason_code, notes "
            "FROM reconciliation_facts WHERE wallet = :w AND ts = :ts "
            "ORDER BY check_type, subject, id"
        ),
        {"w": wallet.lower(), "ts": run_ts},
    ).fetchall()
    facts = []
    for row in rows:
        notes = json.loads(row.notes or "{}")
        abs_diff = decimal_value(row.abs_diff)
        price = decimal_value(notes.get("price_for_notional"))
        facts.append(
            ReconciliationFact(
                wallet=row.wallet,
                ts=int(row.ts),
                check_type=row.check_type,
                subject=row.subject,
                expected=decimal_value(row.expected),
                computed=decimal_value(row.computed),
                abs_diff=abs_diff,
                pct_diff=decimal_value(row.pct_diff),
                tolerance=decimal_value(row.tolerance),
                status=row.status,
                source=row.source,
                reason_code=row.reason_code,
                notes=notes,
                estimated_notional_impact=abs_diff * price,
            )
        )
    return tuple(facts)


def _result_from_facts(
    wallet: str, run_ts: int, facts: tuple[ReconciliationFact, ...]
) -> ReconciliationResult:
    size_facts = [fact for fact in facts if fact.check_type == "positions_size"]
    tolerance = size_facts[0].tolerance if size_facts else RECONCILE_TOLERANCE
    remote_total = sum(1 for fact in size_facts if fact.notes.get("remote_present"))
    local_nonzero = sum(
        1
        for fact in size_facts
        if fact.notes.get("local_present")
        and abs(decimal_value(fact.notes.get("local_qty"))) > Decimal("0.000001")
    )
    negative_presence = [
        PresenceProbe(
            token_id=fact.subject,
            qty=decimal_value(fact.notes.get("local_qty")),
            appears_in_positions=bool(fact.notes.get("appears_in_positions")),
            reason_code="local_negative_holding",
        )
        for fact in facts
        if fact.check_type == "negative_holding_presence"
    ]
    missing_token_presence = [
        PresenceProbe(
            token_id=fact.subject,
            qty=decimal_value(fact.notes.get("local_qty")),
            appears_in_positions=bool(fact.notes.get("appears_in_positions")),
            reason_code="metadata_unavailable_upstream",
        )
        for fact in facts
        if fact.check_type == "missing_token_metadata_presence"
    ]
    if not negative_presence:
        negative_presence = [
            PresenceProbe(
                token_id=fact.subject,
                qty=decimal_value(fact.notes.get("local_qty")),
                appears_in_positions=bool(fact.notes.get("remote_present")),
                reason_code="local_negative_holding",
            )
            for fact in size_facts
            if decimal_value(fact.notes.get("local_qty")) < -tolerance
        ]
    if not missing_token_presence:
        missing_token_presence = [
            PresenceProbe(
                token_id=fact.subject,
                qty=decimal_value(fact.notes.get("local_qty")),
                appears_in_positions=bool(fact.notes.get("remote_present")),
                reason_code="metadata_unavailable_upstream",
            )
            for fact in size_facts
            if fact.notes.get("local_present")
            and fact.notes.get("local_condition_id") is None
            and abs(decimal_value(fact.notes.get("local_qty"))) > Decimal("0.000001")
        ]
    return ReconciliationResult(
        wallet=wallet,
        run_ts=run_ts,
        tolerance=tolerance,
        facts=facts,
        remote_positions_total=remote_total,
        local_nonzero_holdings_total=local_nonzero,
        negative_holdings_presence=tuple(negative_presence),
        missing_token_metadata_presence=tuple(missing_token_presence),
    )


def trust_dict(trust: WalletTrust | None) -> dict | None:
    return trust.as_dict() if trust is not None else None
