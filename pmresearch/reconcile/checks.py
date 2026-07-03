"""Holdings-vs-Data-API reconciliation checks."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import json
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..reports.holdings_dq import (
    MissingTokenRow,
    NegativeHoldingRow,
    missing_conditions_report,
    missing_token_metadata_report,
    negative_holdings_report,
)
from ..sources.dataapi import PositionRow

RECONCILE_TOLERANCE = Decimal("0.0001")
WAC_TOLERANCE = Decimal("0.001")
REALIZED_PNL_TOLERANCE = Decimal("0.01")
_ZERO = Decimal("0")

PASS_REASONS = {"exact_match", "dust_only", "within_realized_pnl_band"}
WARN_REASONS = {
    "timing_skew",
    "metadata_unavailable_upstream",
    "merge_condition_scoped_size_gap",
    "same_timestamp_redeem_merge_ordering_ambiguity",
    "realized_pnl_trade_accounting_drift",
    "realized_pnl_merge_split_semantics",
    "realized_pnl_resolution_semantics",
    "realized_pnl_post_phase8_drift",
    "wac_size_reconciliation_not_clean",
    "wac_timing_skew",
}
FAIL_REASONS = {
    "local_negative_holding",
    "remote_missing_local_present",
    "local_missing_remote_present",
    "source_api_missing_fill",
    "wac_drift",
    "local_open_episode_missing",
    "unknown",
}
KNOWN_EXCEPTION_REASONS = {"source_api_missing_fill"}


def decimal_value(value: object) -> Decimal:
    try:
        return Decimal(str(value or 0))
    except (InvalidOperation, ValueError):
        return Decimal(0)


def decimal_string(value: Decimal) -> str:
    return format(value.normalize(), "f") if value else "0"


@dataclass(frozen=True)
class LocalHolding:
    token_id: str
    qty: Decimal
    wac_cost: Decimal
    as_of_ts: int
    condition_id: Optional[str]
    outcome_label: Optional[str]
    question: Optional[str]


@dataclass(frozen=True)
class LocalOpenEpisode:
    episode_id: int
    token_id: str
    condition_id: Optional[str]
    open_ts: int
    wac_entry: Decimal
    realized_pnl: Decimal
    events_consumed: tuple[int, ...]
    event_types: tuple[str, ...]
    duplicate_open_episodes: int = 0


@dataclass(frozen=True)
class ReconciliationFact:
    wallet: str
    ts: int
    check_type: str
    subject: str
    expected: Decimal
    computed: Decimal
    abs_diff: Decimal
    pct_diff: Decimal
    tolerance: Decimal
    status: str
    source: str
    reason_code: str
    notes: dict
    estimated_notional_impact: Decimal = _ZERO

    def db_params(self) -> dict:
        return {
            "wallet": self.wallet,
            "ts": self.ts,
            "check_type": self.check_type,
            "subject": self.subject,
            "expected": decimal_string(self.expected),
            "computed": decimal_string(self.computed),
            "abs_diff": decimal_string(self.abs_diff),
            "pct_diff": decimal_string(self.pct_diff),
            "tolerance": decimal_string(self.tolerance),
            "status": self.status,
            "source": self.source,
            "reason_code": self.reason_code,
            "notes": json.dumps(self.notes, sort_keys=True, separators=(",", ":")),
        }

    def as_dict(self) -> dict:
        return {
            "wallet": self.wallet,
            "ts": self.ts,
            "check_type": self.check_type,
            "subject": self.subject,
            "expected": decimal_string(self.expected),
            "computed": decimal_string(self.computed),
            "abs_diff": decimal_string(self.abs_diff),
            "pct_diff": decimal_string(self.pct_diff),
            "tolerance": decimal_string(self.tolerance),
            "status": self.status,
            "source": self.source,
            "reason_code": self.reason_code,
            "estimated_notional_impact": decimal_string(self.estimated_notional_impact),
            "notes": self.notes,
        }


@dataclass(frozen=True)
class PresenceProbe:
    token_id: str
    qty: Decimal
    appears_in_positions: bool
    reason_code: str

    def as_dict(self) -> dict:
        return {
            "token_id": self.token_id,
            "qty": decimal_string(self.qty),
            "appears_in_positions": self.appears_in_positions,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class ReconciliationResult:
    wallet: str
    run_ts: int
    tolerance: Decimal
    facts: tuple[ReconciliationFact, ...]
    remote_positions_total: int
    local_nonzero_holdings_total: int
    negative_holdings_presence: tuple[PresenceProbe, ...]
    missing_token_metadata_presence: tuple[PresenceProbe, ...]

    @property
    def size_facts(self) -> list[ReconciliationFact]:
        return [fact for fact in self.facts if fact.check_type == "positions_size"]

    def check_status_counts(self) -> dict[str, dict[str, int]]:
        checks: dict[str, dict[str, int]] = {}
        for fact in self.facts:
            row = checks.setdefault(
                fact.check_type,
                {"total": 0, "pass": 0, "warn": 0, "fail": 0, "skip": 0},
            )
            row["total"] += 1
            row[fact.status] = row.get(fact.status, 0) + 1
        return dict(sorted(checks.items()))

    def summary(self) -> dict:
        size_facts = self.size_facts
        exact = sum(1 for fact in size_facts if fact.reason_code == "exact_match")
        passes = sum(1 for fact in size_facts if fact.status == "pass")
        warnings = sum(1 for fact in size_facts if fact.status == "warn")
        failures = sum(1 for fact in size_facts if fact.status == "fail")
        return {
            "remote_positions": self.remote_positions_total,
            "local_nonzero_holdings": self.local_nonzero_holdings_total,
            "exact_matches": exact,
            "passes": passes,
            "warnings": warnings,
            "fails": failures,
            "checks": self.check_status_counts(),
        }

    def top_qty_discrepancies(self, limit: int = 20) -> list[dict]:
        return [
            fact.as_dict()
            for fact in sorted(self.size_facts, key=lambda f: f.abs_diff, reverse=True)
            if fact.abs_diff > _ZERO
        ][:limit]

    def top_notional_discrepancies(self, limit: int = 20) -> list[dict]:
        return [
            fact.as_dict()
            for fact in sorted(
                self.size_facts, key=lambda f: f.estimated_notional_impact, reverse=True
            )
            if fact.estimated_notional_impact > _ZERO
        ][:limit]

    def top_remote_positions(self, limit: int = 20) -> list[dict]:
        rows = []
        for fact in self.size_facts:
            remote_current_value = decimal_value(fact.notes.get("remote_current_value"))
            if remote_current_value <= _ZERO:
                continue
            row = fact.as_dict()
            row["remote_current_value"] = decimal_string(remote_current_value)
            rows.append(row)
        return sorted(rows, key=lambda r: decimal_value(r["remote_current_value"]), reverse=True)[
            :limit
        ]

    def known_exceptions(self) -> list[dict]:
        exceptions = []
        for fact in self.size_facts:
            if fact.reason_code not in KNOWN_EXCEPTION_REASONS:
                continue
            exceptions.append(
                {
                    "token_id": fact.subject,
                    "exception_type": fact.reason_code,
                    "classification": fact.notes.get("classification", "upstream_historical_gap"),
                    "status": fact.status,
                    "check_type": fact.check_type,
                    "expected": decimal_string(fact.expected),
                    "computed": decimal_string(fact.computed),
                    "abs_diff": decimal_string(fact.abs_diff),
                    "source": fact.source,
                    "condition_id": fact.notes.get("local_condition_id")
                    or fact.notes.get("remote_condition_id"),
                    "question": fact.notes.get("local_question") or fact.notes.get("remote_title"),
                    "outcome": fact.notes.get("local_outcome") or fact.notes.get("remote_outcome"),
                    "note": fact.notes.get("note"),
                }
            )
        return exceptions

    def as_dict(self, trust: Optional[dict] = None) -> dict:
        known_exceptions = self.known_exceptions()
        exception_types = sorted({item["exception_type"] for item in known_exceptions})
        return {
            "wallet": self.wallet,
            "run_ts": self.run_ts,
            "tolerance": decimal_string(self.tolerance),
            "summary": self.summary(),
            "trust": trust,
            "wallet_trust": trust,
            "known_exception_count": len(known_exceptions),
            "known_exceptions": known_exceptions,
            "analytics_trust_caveat": {
                "trust_status": trust["status"] if trust else None,
                "known_exception_count": len(known_exceptions),
                "known_exception_types": exception_types,
            },
            "check_status": self.check_status_counts(),
            "top_qty_discrepancies": self.top_qty_discrepancies(),
            "top_notional_discrepancies": self.top_notional_discrepancies(),
            "top_remote_positions": self.top_remote_positions(),
            "negative_holdings_presence": [
                probe.as_dict() for probe in self.negative_holdings_presence
            ],
            "missing_token_metadata_presence": [
                probe.as_dict() for probe in self.missing_token_metadata_presence
            ],
            "facts": [fact.as_dict() for fact in self.facts],
        }


def load_local_holdings(session: Session, wallet: str) -> dict[str, LocalHolding]:
    rows = session.execute(
        text(
            "SELECT h.token_id, h.qty, h.wac_cost, h.as_of_ts, "
            "t.condition_id, t.outcome_label, m.question "
            "FROM holdings h "
            "LEFT JOIN tokens t ON t.token_id = h.token_id "
            "LEFT JOIN markets m ON m.condition_id = t.condition_id "
            "WHERE h.wallet = :w"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    return {
        row.token_id: LocalHolding(
            token_id=row.token_id,
            qty=decimal_value(row.qty),
            wac_cost=decimal_value(row.wac_cost),
            as_of_ts=int(row.as_of_ts or 0),
            condition_id=row.condition_id,
            outcome_label=row.outcome_label,
            question=row.question,
        )
        for row in rows
    }


def load_open_episodes(session: Session, wallet: str) -> dict[str, LocalOpenEpisode]:
    rows = session.execute(
        text(
            "SELECT id, token_id, condition_id, open_ts, wac_entry, realized_pnl, "
            "events_consumed FROM episodes "
            "WHERE wallet = :w AND close_reason = 'open' "
            "ORDER BY token_id, open_ts DESC, id DESC"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    duplicates: dict[str, int] = {}
    selected: dict[str, object] = {}
    for row in rows:
        duplicates[row.token_id] = duplicates.get(row.token_id, 0) + 1
        selected.setdefault(row.token_id, row)

    event_ids = sorted(
        {
            event_id
            for row in selected.values()
            for event_id in _parse_event_ids(row.events_consumed)
        }
    )
    event_types_by_id: dict[int, str] = {}
    if event_ids:
        type_rows = session.execute(
            text(
                "SELECT id, event_type FROM wallet_events "
                f"WHERE id IN ({','.join(str(int(i)) for i in event_ids)})"
            )
        ).fetchall()
        event_types_by_id = {int(row.id): row.event_type for row in type_rows}

    episodes: dict[str, LocalOpenEpisode] = {}
    for token_id, row in selected.items():
        ids = _parse_event_ids(row.events_consumed)
        episodes[token_id] = LocalOpenEpisode(
            episode_id=int(row.id),
            token_id=row.token_id,
            condition_id=row.condition_id,
            open_ts=int(row.open_ts),
            wac_entry=decimal_value(row.wac_entry),
            realized_pnl=decimal_value(row.realized_pnl),
            events_consumed=ids,
            event_types=tuple(
                sorted({event_types_by_id[event_id] for event_id in ids if event_id in event_types_by_id})
            ),
            duplicate_open_episodes=duplicates.get(token_id, 1) - 1,
        )
    return episodes


def _parse_event_ids(value: object) -> tuple[int, ...]:
    try:
        payload = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return ()
    if not isinstance(payload, list):
        return ()
    out = []
    for item in payload:
        try:
            out.append(int(item))
        except (TypeError, ValueError):
            continue
    return tuple(out)


def _pct_diff(abs_diff: Decimal, expected: Decimal) -> Decimal:
    denominator = abs(expected)
    if denominator == _ZERO:
        return _ZERO
    return abs_diff / denominator


def _status_for_reason(reason_code: str) -> str:
    if reason_code in PASS_REASONS:
        return "pass"
    if reason_code in WARN_REASONS:
        return "warn"
    return "fail"


def _has_oracle_field(remote: PositionRow, field_name: str) -> bool:
    return field_name in remote.raw and remote.raw.get(field_name) not in (None, "")


def _open_episode_notes(
    local: Optional[LocalHolding],
    remote: PositionRow,
    episode: Optional[LocalOpenEpisode],
    note: str,
) -> dict:
    notes = _base_notes(local, remote, note)
    notes["comparison_scope"] = "current_open_episode"
    notes["open_episode_id"] = episode.episode_id if episode else None
    notes["open_episode_ts"] = episode.open_ts if episode else None
    notes["open_episode_wac_entry"] = decimal_string(episode.wac_entry) if episode else "0"
    notes["open_episode_realized_pnl"] = (
        decimal_string(episode.realized_pnl) if episode else "0"
    )
    notes["open_episode_events_consumed"] = list(episode.events_consumed) if episode else []
    notes["open_episode_event_types"] = list(episode.event_types) if episode else []
    notes["duplicate_open_episodes"] = episode.duplicate_open_episodes if episode else 0
    return notes


def _skipped_oracle_fact(
    *,
    wallet: str,
    run_ts: int,
    check_type: str,
    subject: str,
    tolerance: Decimal,
    source: str,
    notes: dict,
) -> ReconciliationFact:
    return ReconciliationFact(
        wallet=wallet,
        ts=run_ts,
        check_type=check_type,
        subject=subject,
        expected=_ZERO,
        computed=_ZERO,
        abs_diff=_ZERO,
        pct_diff=_ZERO,
        tolerance=tolerance,
        status="skip",
        source=source,
        reason_code="oracle_field_missing",
        notes=notes,
    )


def wac_vs_avgprice_fact(
    *,
    wallet: str,
    run_ts: int,
    local: Optional[LocalHolding],
    remote: PositionRow,
    episode: Optional[LocalOpenEpisode],
    tolerance: Decimal,
    size_fact: Optional[ReconciliationFact] = None,
    timing_skew: bool = False,
) -> ReconciliationFact:
    notes = _open_episode_notes(
        local,
        remote,
        episode,
        "avgPrice is compared against the current open episode WAC, not lifetime WAC",
    )
    if not _has_oracle_field(remote, "avgPrice"):
        notes["oracle_field"] = "avgPrice"
        return _skipped_oracle_fact(
            wallet=wallet,
            run_ts=run_ts,
            check_type="positions_wac_avg_price",
            subject=remote.token_id,
            tolerance=tolerance,
            source="dataapi/positions",
            notes=notes,
        )
    if episode is None:
        notes["note"] = "remote position has avgPrice but no local open episode"
        return ReconciliationFact(
            wallet=wallet,
            ts=run_ts,
            check_type="positions_wac_avg_price",
            subject=remote.token_id,
            expected=remote.avg_price,
            computed=_ZERO,
            abs_diff=abs(remote.avg_price),
            pct_diff=_pct_diff(abs(remote.avg_price), remote.avg_price),
            tolerance=tolerance,
            status="fail",
            source="dataapi/positions",
            reason_code="local_open_episode_missing",
            notes=notes,
        )

    abs_diff = abs(remote.avg_price - episode.wac_entry)
    reason = "exact_match" if abs_diff == _ZERO else (
        "dust_only" if abs_diff <= tolerance else "wac_drift"
    )
    status = "pass" if reason in PASS_REASONS else "fail"
    if reason == "wac_drift":
        if size_fact is not None and size_fact.status != "pass":
            status = "warn"
            reason = "wac_size_reconciliation_not_clean"
            notes["classification"] = "size_check_not_clean"
            notes["size_check_status"] = size_fact.status
            notes["size_check_reason"] = size_fact.reason_code
            notes["size_abs_diff"] = decimal_string(size_fact.abs_diff)
        elif timing_skew:
            status = "warn"
            reason = "wac_timing_skew"
            notes["classification"] = "local_sync_or_oracle_timing_skew"
        else:
            notes["classification"] = "current_open_episode_wac_drift"
    return ReconciliationFact(
        wallet=wallet,
        ts=run_ts,
        check_type="positions_wac_avg_price",
        subject=remote.token_id,
        expected=remote.avg_price,
        computed=episode.wac_entry,
        abs_diff=abs_diff,
        pct_diff=_pct_diff(abs_diff, remote.avg_price),
        tolerance=tolerance,
        status=status,
        source="dataapi/positions",
        reason_code=reason,
        notes=notes,
    )


def realized_vs_oracle_fact(
    *,
    wallet: str,
    run_ts: int,
    local: Optional[LocalHolding],
    remote: PositionRow,
    episode: Optional[LocalOpenEpisode],
    tolerance: Decimal,
    timing_skew: bool,
) -> ReconciliationFact:
    notes = _open_episode_notes(
        local,
        remote,
        episode,
        "realizedPnl is compared for the current open episode after derived payouts are replayed",
    )
    if not _has_oracle_field(remote, "realizedPnl"):
        notes["oracle_field"] = "realizedPnl"
        return _skipped_oracle_fact(
            wallet=wallet,
            run_ts=run_ts,
            check_type="positions_realized_pnl",
            subject=remote.token_id,
            tolerance=tolerance,
            source="dataapi/positions",
            notes=notes,
        )
    if episode is None:
        notes["note"] = "remote position has realizedPnl but no local open episode"
        return ReconciliationFact(
            wallet=wallet,
            ts=run_ts,
            check_type="positions_realized_pnl",
            subject=remote.token_id,
            expected=remote.realized_pnl,
            computed=_ZERO,
            abs_diff=abs(remote.realized_pnl),
            pct_diff=_pct_diff(abs(remote.realized_pnl), remote.realized_pnl),
            tolerance=tolerance,
            status="fail",
            source="dataapi/positions",
            reason_code="local_open_episode_missing",
            notes=notes,
        )

    abs_diff = abs(remote.realized_pnl - episode.realized_pnl)
    if abs_diff == _ZERO:
        reason = "exact_match"
        status = "pass"
        notes["classification"] = "matched"
    elif abs_diff <= tolerance:
        reason = "within_realized_pnl_band"
        status = "pass"
        notes["classification"] = "rounding_or_display_precision"
    else:
        status = "warn"
        event_types = set(episode.event_types)
        if timing_skew:
            reason = "timing_skew"
            notes["classification"] = "local_sync_or_oracle_timing_skew"
        elif "REDEEM_PAYOUT" in event_types:
            reason = "realized_pnl_post_phase8_drift"
            notes["classification"] = "post_phase8_realized_pnl_drift"
        elif "REDEEM" in event_types:
            reason = "realized_pnl_resolution_semantics"
            notes["classification"] = "zero_redeem_without_derived_payout"
        elif event_types & {"MERGE", "SPLIT"}:
            reason = "realized_pnl_merge_split_semantics"
            notes["classification"] = "oracle_merge_split_semantics_may_differ"
        else:
            reason = "realized_pnl_trade_accounting_drift"
            notes["classification"] = "trade_accounting_or_oracle_semantics_drift"
    return ReconciliationFact(
        wallet=wallet,
        ts=run_ts,
        check_type="positions_realized_pnl",
        subject=remote.token_id,
        expected=remote.realized_pnl,
        computed=episode.realized_pnl,
        abs_diff=abs_diff,
        pct_diff=_pct_diff(abs_diff, remote.realized_pnl),
        tolerance=tolerance,
        status=status,
        source="dataapi/positions",
        reason_code=reason,
        notes=notes,
    )


def _condition_has_same_ts_redeem_merge(
    session: Session, wallet: str, condition_id: Optional[str], ts: Optional[int]
) -> bool:
    if not condition_id or ts is None:
        return False
    rows = session.execute(
        text(
            "SELECT DISTINCT event_type FROM wallet_events "
            "WHERE wallet = :w AND condition_id = :c AND ts = :ts "
            "AND event_type IN ('REDEEM', 'MERGE')"
        ),
        {"w": wallet.lower(), "c": condition_id, "ts": ts},
    ).fetchall()
    return {row.event_type for row in rows} == {"REDEEM", "MERGE"}


def _closed_condition_merge_gap_explains(
    session: Session, wallet: str, condition_id: Optional[str], negative_qty: Decimal
) -> bool:
    if not condition_id or negative_qty >= _ZERO:
        return False
    row = session.execute(
        text(
            "SELECT m.closed AS closed, "
            "SUM(CASE WHEN we.event_type = 'MERGE' THEN ABS(CAST(we.delta_shares AS REAL)) ELSE 0 END) "
            "AS merge_size "
            "FROM markets m "
            "LEFT JOIN wallet_events we ON we.condition_id = m.condition_id AND we.wallet = :w "
            "WHERE m.condition_id = :c "
            "GROUP BY m.closed"
        ),
        {"w": wallet.lower(), "c": condition_id},
    ).fetchone()
    if row is None or not row.closed:
        return False
    return Decimal(str(row.merge_size or 0)) >= abs(negative_qty)


def _negative_maps(
    rows: list[NegativeHoldingRow],
) -> tuple[dict[str, NegativeHoldingRow], dict[str, list[NegativeHoldingRow]]]:
    by_token = {row.token_id: row for row in rows}
    by_condition: dict[str, list[NegativeHoldingRow]] = {}
    for row in rows:
        if row.condition_id:
            by_condition.setdefault(row.condition_id, []).append(row)
    return by_token, by_condition


def _price_for_notional(remote: Optional[PositionRow], local: Optional[LocalHolding]) -> Decimal:
    if remote is not None:
        if remote.cur_price > _ZERO:
            return remote.cur_price
        if remote.current_value > _ZERO and remote.size > _ZERO:
            return remote.current_value / remote.size
    if local is not None and local.wac_cost > _ZERO:
        return local.wac_cost
    return _ZERO


def _base_notes(
    local: Optional[LocalHolding], remote: Optional[PositionRow], reason_note: str
) -> dict:
    return {
        "local_present": local is not None,
        "local_qty": decimal_string(local.qty) if local else "0",
        "local_wac_cost": decimal_string(local.wac_cost) if local else "0",
        "local_as_of_ts": local.as_of_ts if local else None,
        "local_condition_id": local.condition_id if local else None,
        "local_outcome": local.outcome_label if local else None,
        "local_question": local.question if local else None,
        "remote_present": remote is not None,
        "remote_size": decimal_string(remote.size) if remote else "0",
        "remote_avg_price": decimal_string(remote.avg_price) if remote else "0",
        "remote_cur_price": decimal_string(remote.cur_price) if remote else "0",
        "remote_current_value": decimal_string(remote.current_value) if remote else "0",
        "remote_condition_id": remote.condition_id if remote else None,
        "remote_title": remote.title if remote else None,
        "remote_outcome": remote.outcome if remote else None,
        "note": reason_note,
    }


def _classify_reason(
    *,
    session: Session,
    wallet: str,
    token_id: str,
    local: Optional[LocalHolding],
    remote: Optional[PositionRow],
    abs_diff: Decimal,
    tolerance: Decimal,
    missing_upstream_conditions: set[str],
    missing_token_ids: set[str],
    negatives_by_token: dict[str, NegativeHoldingRow],
    negatives_by_condition: dict[str, list[NegativeHoldingRow]],
    timing_skew: bool,
) -> tuple[str, str]:
    if abs_diff == _ZERO:
        return "exact_match", "remote size equals local qty"
    if abs_diff <= tolerance:
        return "dust_only", "size difference is within reconciliation tolerance"

    local_condition = local.condition_id if local else None
    remote_condition = remote.condition_id if remote else None
    condition_id = local_condition or remote_condition

    if token_id in missing_token_ids or (
        condition_id is not None and condition_id in missing_upstream_conditions
    ):
        return "metadata_unavailable_upstream", "token or condition metadata is unavailable upstream"

    negative = negatives_by_token.get(token_id)
    condition_negatives = negatives_by_condition.get(condition_id or "", [])
    if negative and _condition_has_same_ts_redeem_merge(
        session, wallet, negative.condition_id, negative.cause_event_ts
    ):
        return (
            "same_timestamp_redeem_merge_ordering_ambiguity",
            "negative holding follows same-timestamp REDEEM/MERGE ordering ambiguity",
        )

    if negative and negative.cause_event_type == "MERGE":
        return (
            "merge_condition_scoped_size_gap",
            "local negative was caused by condition-scoped MERGE size replay",
        )
    if any(row.cause_event_type == "MERGE" for row in condition_negatives):
        return (
            "merge_condition_scoped_size_gap",
            "condition has MERGE-caused negative/asymmetric local holdings",
        )

    if negative and negative.cause_event_type == "TRADE":
        if local is not None and _closed_condition_merge_gap_explains(
            session, wallet, condition_id, local.qty
        ):
            return (
                "merge_condition_scoped_size_gap",
                "closed condition has prior condition-scoped MERGE size sufficient to explain residual",
            )
        return (
            "source_api_missing_fill",
            "sell-driven negative holding with no local acquisition or condition-scoped explanation",
        )

    if local is not None and local.qty < -tolerance:
        return "local_negative_holding", "local holding is negative beyond tolerance"

    if timing_skew:
        return "timing_skew", "local sync/replay may be stale relative to live oracle"

    if remote is None and local is not None and local.qty > tolerance:
        return "remote_missing_local_present", "local nonzero holding is missing from /positions"
    if local is None and remote is not None and remote.size > tolerance:
        return "local_missing_remote_present", "remote /positions token is missing locally"

    return "unknown", "unclassified size drift"


def _timing_skew(session: Session, wallet: str, run_ts: int, local_holdings: dict[str, LocalHolding]) -> bool:
    row = session.execute(
        text("SELECT last_incremental_ts FROM sync_state WHERE wallet = :w"),
        {"w": wallet.lower()},
    ).fetchone()
    if row is None or row.last_incremental_ts is None:
        return False
    max_local_ts = max((holding.as_of_ts for holding in local_holdings.values()), default=0)
    if int(row.last_incremental_ts) > max_local_ts:
        return True
    return run_ts - int(row.last_incremental_ts) > 600


def build_reconciliation_result(
    session: Session,
    *,
    wallet: str,
    run_ts: int,
    remote_positions: list[PositionRow],
    tolerance: Decimal = RECONCILE_TOLERANCE,
    wac_tolerance: Decimal = WAC_TOLERANCE,
    realized_pnl_tolerance: Decimal = REALIZED_PNL_TOLERANCE,
    dust_epsilon: Decimal = Decimal("0.000001"),
) -> ReconciliationResult:
    wallet = wallet.lower()
    local_holdings = load_local_holdings(session, wallet)
    open_episodes = load_open_episodes(session, wallet)
    remote_by_token = {row.token_id: row for row in remote_positions}
    local_nonzero_tokens = {
        token_id for token_id, holding in local_holdings.items() if abs(holding.qty) > dust_epsilon
    }

    negative_rows, _ = negative_holdings_report(session, wallet, dust_epsilon=dust_epsilon)
    negatives_by_token, negatives_by_condition = _negative_maps(negative_rows)
    missing_conditions = missing_conditions_report(session, wallet)
    missing_upstream_conditions = {
        row.condition_id for row in missing_conditions if row.classification == "unavailable_upstream"
    }
    missing_token_rows = missing_token_metadata_report(
        session, wallet, dust_epsilon=dust_epsilon
    )
    missing_token_ids = {row.token_id for row in missing_token_rows}
    timing = _timing_skew(session, wallet, run_ts, local_holdings)

    subjects = sorted(set(remote_by_token) | local_nonzero_tokens)
    facts: list[ReconciliationFact] = []
    for token_id in subjects:
        local = local_holdings.get(token_id)
        remote = remote_by_token.get(token_id)
        expected = remote.size if remote else _ZERO
        computed = local.qty if local else _ZERO
        diff = expected - computed
        abs_diff = abs(diff)
        reason, reason_note = _classify_reason(
            session=session,
            wallet=wallet,
            token_id=token_id,
            local=local,
            remote=remote,
            abs_diff=abs_diff,
            tolerance=tolerance,
            missing_upstream_conditions=missing_upstream_conditions,
            missing_token_ids=missing_token_ids,
            negatives_by_token=negatives_by_token,
            negatives_by_condition=negatives_by_condition,
            timing_skew=timing,
        )
        status = _status_for_reason(reason)
        price = _price_for_notional(remote, local)
        notes = _base_notes(local, remote, reason_note)
        notes["qty_diff"] = decimal_string(diff)
        notes["price_for_notional"] = decimal_string(price)
        if reason in KNOWN_EXCEPTION_REASONS:
            notes["classification"] = "upstream_historical_gap"
            notes["policy"] = "keep visible; do not fabricate acquisition"
        size_fact = ReconciliationFact(
            wallet=wallet,
            ts=run_ts,
            check_type="positions_size",
            subject=token_id,
            expected=expected,
            computed=computed,
            abs_diff=abs_diff,
            pct_diff=_pct_diff(abs_diff, expected),
            tolerance=tolerance,
            status=status,
            source="dataapi/positions",
            reason_code=reason,
            notes=notes,
            estimated_notional_impact=abs_diff * price,
        )
        facts.append(size_fact)

        if local is not None and remote is not None and computed > tolerance:
            episode = open_episodes.get(token_id)
            facts.append(
                wac_vs_avgprice_fact(
                    wallet=wallet,
                    run_ts=run_ts,
                    local=local,
                    remote=remote,
                    episode=episode,
                    tolerance=wac_tolerance,
                    size_fact=size_fact,
                    timing_skew=timing,
                )
            )
            facts.append(
                realized_vs_oracle_fact(
                    wallet=wallet,
                    run_ts=run_ts,
                    local=local,
                    remote=remote,
                    episode=episode,
                    tolerance=realized_pnl_tolerance,
                    timing_skew=timing,
                )
            )

    negative_presence = _presence_probes(negative_rows, remote_by_token, "local_negative_holding")
    missing_token_presence = _presence_probes(
        missing_token_rows, remote_by_token, "metadata_unavailable_upstream"
    )
    facts.extend(
        _presence_facts(
            wallet=wallet,
            run_ts=run_ts,
            tolerance=tolerance,
            check_type="negative_holding_presence",
            probes=negative_presence,
            source="holdings_dq+dataapi/positions",
            status="warn",
        )
    )
    facts.extend(
        _presence_facts(
            wallet=wallet,
            run_ts=run_ts,
            tolerance=tolerance,
            check_type="missing_token_metadata_presence",
            probes=missing_token_presence,
            source="holdings_dq+dataapi/positions",
            status="warn",
        )
    )
    return ReconciliationResult(
        wallet=wallet,
        run_ts=run_ts,
        tolerance=tolerance,
        facts=tuple(facts),
        remote_positions_total=len(remote_positions),
        local_nonzero_holdings_total=len(local_nonzero_tokens),
        negative_holdings_presence=tuple(negative_presence),
        missing_token_metadata_presence=tuple(missing_token_presence),
    )


def _presence_probes(
    rows: list[NegativeHoldingRow] | list[MissingTokenRow],
    remote_by_token: dict[str, PositionRow],
    reason_code: str,
) -> list[PresenceProbe]:
    probes = []
    for row in rows:
        probes.append(
            PresenceProbe(
                token_id=row.token_id,
                qty=row.qty,
                appears_in_positions=row.token_id in remote_by_token,
                reason_code=reason_code,
            )
        )
    return probes


def _presence_facts(
    *,
    wallet: str,
    run_ts: int,
    tolerance: Decimal,
    check_type: str,
    probes: list[PresenceProbe],
    source: str,
    status: str,
) -> list[ReconciliationFact]:
    facts = []
    for probe in probes:
        expected = Decimal(1) if probe.appears_in_positions else Decimal(0)
        notes = {
            "appears_in_positions": probe.appears_in_positions,
            "local_qty": decimal_string(probe.qty),
            "note": "presence probe for reconciliation reporting",
        }
        facts.append(
            ReconciliationFact(
                wallet=wallet,
                ts=run_ts,
                check_type=check_type,
                subject=probe.token_id,
                expected=expected,
                computed=probe.qty,
                abs_diff=abs(probe.qty),
                pct_diff=Decimal(0),
                tolerance=tolerance,
                status=status,
                source=source,
                reason_code=probe.reason_code,
                notes=notes,
            )
        )
    return facts
