"""Phase 22.5b actionable rule candidate extraction.

This module reads existing Phase 22.5 CSV outputs. It does not rebuild the
pattern dataset, simulate strategies, optimize parameters, or promote rules.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

CORE_INPUT_FILES = (
    "order_timing_dataset.csv",
    "condition_inventory_timeline.csv",
    "pair_completion_report.csv",
    "merge_timing_report.csv",
    "unpaired_inventory_duration_report.csv",
    "pattern_mining_summary.md",
)

OPTIONAL_INPUT_FILES = ("sibling_market_sequence_report.csv",)

RULE_CANDIDATE_COLUMNS = [
    "rule_id",
    "rule_name",
    "rule_type",
    "wallet",
    "short_description",
    "trigger_logic_plain_english",
    "trigger_features_pre_fill",
    "diagnostic_features_post_fill",
    "excluded_features_due_to_leakage",
    "threshold_values_observed_json",
    "fills_supported",
    "fills_supported_pct",
    "events_supported",
    "conditions_supported",
    "median_fill_price",
    "median_fill_size",
    "median_fill_notional",
    "median_complete_set_cost",
    "pct_complete_set_cost_lt_095",
    "pct_complete_set_cost_lt_098",
    "pct_complete_set_cost_lt_100",
    "p50_time_to_complement_s",
    "p90_time_to_complement_s",
    "pct_increases_bond",
    "pct_reduces_unpaired",
    "pct_increases_unpaired",
    "median_unpaired_duration_s",
    "p90_unpaired_duration_s",
    "median_event_market_count_active_before",
    "median_event_unpaired_inventory_before",
    "median_event_bond_qty_before",
    "merge_followed_pct",
    "median_time_to_merge_s",
    "total_merge_capital_released_if_applicable",
    "support_stability",
    "concentration_risk",
    "leakage_risk",
    "simulator_eligible",
    "live_eligible_later",
    "failure_modes",
    "recommendation",
    "lifecycle_sample_scope",
    "closed_complete_events_supported",
    "live_or_censored_events_supported",
    "censored_observation_share",
    "lifecycle_metrics_reliable",
    "censoring_notes",
]

EVIDENCE_COLUMNS = [
    "rule_id",
    "evidence_scope",
    "wallet",
    "event_id",
    "condition_id",
    "fill_event_id",
    "fill_utc",
    "question",
    "trigger_snapshot",
    "post_fill_outcome",
    "lifecycle_classification",
    "notes",
]

_DUST = 1e-6
_RECENT_WINDOW_S = 24 * 60 * 60


@dataclass(frozen=True)
class RuleExtractionStats:
    in_dir: Path
    out_dir: Path
    rule_candidates: Path
    extraction_report: Path
    evidence_examples: Path
    quality_report: Path
    rules: int


def extract_rule_candidates(in_dir: Path, out_dir: Path | None = None) -> RuleExtractionStats:
    """Extract Phase 22.5b rule candidates from existing Phase 22.5 outputs."""
    in_dir = Path(in_dir)
    out_dir = Path(out_dir or in_dir)
    _validate_inputs(in_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    orders = _read_csv(in_dir / "order_timing_dataset.csv")
    timeline = _read_csv(in_dir / "condition_inventory_timeline.csv")
    pairs = _read_csv(in_dir / "pair_completion_report.csv")
    merges = _read_csv(in_dir / "merge_timing_report.csv")
    durations = _read_csv(in_dir / "unpaired_inventory_duration_report.csv")

    classifications = _classify_lifecycle(orders, pairs, durations)
    quality = _quality_checks(in_dir, orders, timeline, pairs, merges, durations)
    rules, evidence = _build_rules(orders, pairs, merges, durations, classifications, quality)

    rule_candidates = out_dir / "rule_candidates.csv"
    extraction_report = out_dir / "rule_candidate_extraction_report.md"
    evidence_examples = out_dir / "rule_evidence_examples.csv"
    quality_report = out_dir / "pattern_quality_report.md"

    _write_csv(rule_candidates, RULE_CANDIDATE_COLUMNS, rules)
    _write_csv(evidence_examples, EVIDENCE_COLUMNS, evidence)
    quality_report.write_text(_render_quality_report(quality, classifications), encoding="utf-8")
    extraction_report.write_text(
        _render_extraction_report(rules, quality, classifications),
        encoding="utf-8",
    )

    return RuleExtractionStats(
        in_dir=in_dir,
        out_dir=out_dir,
        rule_candidates=rule_candidates,
        extraction_report=extraction_report,
        evidence_examples=evidence_examples,
        quality_report=quality_report,
        rules=len(rules),
    )


def _validate_inputs(in_dir: Path) -> None:
    missing = [name for name in CORE_INPUT_FILES if not (in_dir / name).exists()]
    if missing:
        raise ValueError(f"missing Phase 22.5 outputs: {', '.join(missing)}")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, object]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({col: _csv_value(row.get(col)) for col in columns})


def _csv_value(value: object) -> object:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True)
    return "" if value is None else value


def _num(row: dict[str, str], key: str) -> float | None:
    value = row.get(key)
    if value in (None, ""):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    if math.isnan(parsed) or math.isinf(parsed):
        return None
    return parsed


def _text(row: dict[str, str], key: str) -> str:
    return str(row.get(key) or "")


def _median(values: Iterable[float | None]) -> float | None:
    clean = [v for v in values if v is not None]
    if not clean:
        return None
    return round(statistics.median(clean), 6)


def _p90(values: Iterable[float | None]) -> float | None:
    clean = sorted(v for v in values if v is not None)
    if not clean:
        return None
    index = math.ceil(0.9 * len(clean)) - 1
    return round(clean[max(0, min(index, len(clean) - 1))], 6)


def _pct(part: int | float, total: int | float) -> float | None:
    if not total:
        return None
    return round(100.0 * float(part) / float(total), 4)


def _counter(rows: Iterable[dict[str, str]], key: str) -> Counter:
    return Counter((row.get(key) or "unknown") for row in rows)


def _event_key(row: dict[str, str]) -> str:
    return _text(row, "event_id")


def _condition_key(row: dict[str, str]) -> tuple[str, str]:
    return (_text(row, "event_id"), _text(row, "condition_id"))


def _classify_lifecycle(
    orders: list[dict[str, str]],
    pairs: list[dict[str, str]],
    durations: list[dict[str, str]],
) -> dict[str, object]:
    latest_ts = max((_num(row, "fill_ts") or 0 for row in orders), default=0)
    condition_stats: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {
            "latest_ts": 0.0,
            "latest_phase": "",
            "phases": Counter(),
            "latest_unpaired_after": 0.0,
            "has_final_resolution": False,
            "has_open_unpaired": False,
            "pair_not_completed": 0,
            "pair_completed": 0,
        }
    )

    for row in orders:
        key = _condition_key(row)
        stats = condition_stats[key]
        ts = _num(row, "fill_ts") or 0.0
        phase = _text(row, "event_phase") or "unknown"
        stats["phases"][phase] += 1
        if ts >= float(stats["latest_ts"]):
            stats["latest_ts"] = ts
            stats["latest_phase"] = phase
            stats["latest_unpaired_after"] = _num(row, "event_unpaired_inventory_after") or 0.0

    for row in durations:
        key = _condition_key(row)
        resolved_by = (_text(row, "resolved_by") or "unknown").lower()
        stats = condition_stats[key]
        if resolved_by == "still_open":
            stats["has_open_unpaired"] = True
        if resolved_by in {"redeem", "resolution"}:
            stats["has_final_resolution"] = True

    for row in pairs:
        key = _condition_key(row)
        stats = condition_stats[key]
        if _text(row, "completion_confidence") == "not_completed":
            stats["pair_not_completed"] = int(stats["pair_not_completed"]) + 1
        else:
            stats["pair_completed"] = int(stats["pair_completed"]) + 1

    condition_class: dict[tuple[str, str], str] = {}
    notes: dict[tuple[str, str], str] = {}
    for key, stats in condition_stats.items():
        phase = str(stats["latest_phase"])
        age_s = latest_ts - float(stats["latest_ts"])
        has_open = bool(stats["has_open_unpaired"])
        has_final = bool(stats["has_final_resolution"])
        latest_unpaired = float(stats["latest_unpaired_after"])
        if has_open or phase in {"early_live", "late_live", "halftime_or_pause"}:
            klass = "live_or_in_progress"
            note = "open unpaired duration or latest fill phase indicates live/in-progress"
        elif has_final and latest_unpaired <= _DUST:
            klass = "closed_complete"
            note = "final resolution/redeem signal and no material unpaired inventory"
        elif has_final and not has_open:
            klass = "closed_complete"
            note = "unpaired periods resolved by final lifecycle event"
        elif phase == "post_event" and age_s <= _RECENT_WINDOW_S:
            klass = "recently_closed_pending_settlement"
            note = "post-event but within 24h of latest observed fill; settlement may arrive later"
        elif phase == "post_event":
            klass = "open_unknown"
            note = "post-event without final redeem/resolution evidence"
        else:
            klass = "open_unknown"
            note = "insufficient finality evidence"
        condition_class[key] = klass
        notes[key] = note

    by_event: dict[str, list[str]] = defaultdict(list)
    for (event_id, _condition_id), klass in condition_class.items():
        by_event[event_id].append(klass)

    event_class: dict[str, str] = {}
    for event_id, classes in by_event.items():
        if any(c == "live_or_in_progress" for c in classes):
            event_class[event_id] = "live_or_in_progress"
        elif any(c == "recently_closed_pending_settlement" for c in classes):
            event_class[event_id] = "recently_closed_pending_settlement"
        elif classes and all(c == "closed_complete" for c in classes):
            event_class[event_id] = "closed_complete"
        else:
            event_class[event_id] = "open_unknown"

    return {
        "condition_class": condition_class,
        "event_class": event_class,
        "condition_notes": notes,
        "condition_counts": Counter(condition_class.values()),
        "event_counts": Counter(event_class.values()),
        "latest_observed_ts": latest_ts,
    }


def _quality_checks(
    in_dir: Path,
    orders: list[dict[str, str]],
    timeline: list[dict[str, str]],
    pairs: list[dict[str, str]],
    merges: list[dict[str, str]],
    durations: list[dict[str, str]],
) -> dict[str, object]:
    row_counts: dict[str, object] = {
        "order_timing_dataset.csv": len(orders),
        "condition_inventory_timeline.csv": len(timeline),
        "pair_completion_report.csv": len(pairs),
        "merge_timing_report.csv": len(merges),
        "unpaired_inventory_duration_report.csv": len(durations),
    }
    summary_path = in_dir / "pattern_mining_summary.md"
    row_counts["pattern_mining_summary.md"] = "present" if summary_path.exists() else "missing"

    sibling_path = in_dir / "sibling_market_sequence_report.csv"
    sibling_status = "missing"
    sibling_sample_rows: list[dict[str, str]] = []
    if sibling_path.exists():
        size = sibling_path.stat().st_size
        sibling_status = f"present size_bytes={size}"
        with sibling_path.open("r", newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for idx, row in enumerate(reader):
                if idx >= 1000:
                    break
                sibling_sample_rows.append(row)
        if size > 250_000_000:
            row_counts["sibling_market_sequence_report.csv"] = (
                f"not fully counted; sampled_rows={len(sibling_sample_rows)}; "
                f"size_bytes={size}"
            )
        else:
            row_counts["sibling_market_sequence_report.csv"] = _count_csv_rows(sibling_path)
    else:
        row_counts["sibling_market_sequence_report.csv"] = "missing"

    unique_events = len({_event_key(row) for row in orders if _event_key(row)})
    unique_conditions = len({_text(row, "condition_id") for row in orders if _text(row, "condition_id")})
    market_family_counter = _counter(orders, "market_family")
    unknown_family = sum(
        count for value, count in market_family_counter.items()
        if value.strip().lower() in {"", "unknown", "null", "none"}
    )
    family_unknown_share = _pct(unknown_family, len(orders)) or 0
    phase_counter = _counter(orders, "event_phase")
    post_event_share = _pct(phase_counter.get("post_event", 0), len(orders)) or 0
    feature_counter = _counter(orders, "feature_availability")
    post_fill_feature_share = _pct(feature_counter.get("post_fill_diagnostic", 0), len(orders)) or 0
    sample_same_condition = sum(
        1 for row in sibling_sample_rows
        if row.get("anchor_condition_id") == row.get("sibling_condition_id")
    )
    sibling_pairwise_exploded = bool(
        sibling_sample_rows
        and ((len(sibling_sample_rows) >= 1000 and (in_dir / "sibling_market_sequence_report.csv").stat().st_size > 10 * (in_dir / "order_timing_dataset.csv").stat().st_size)
             or sample_same_condition > 0)
    )

    return {
        "row_counts": row_counts,
        "unique_events": unique_events,
        "unique_conditions": unique_conditions,
        "role_distribution": _counter(orders, "role"),
        "order_time_confidence_distribution": _counter(orders, "order_time_confidence"),
        "market_family_distribution": market_family_counter,
        "market_family_unknown_share": round(family_unknown_share, 4),
        "event_phase_distribution": phase_counter,
        "feature_availability_distribution": feature_counter,
        "feature_availability_too_coarse": post_fill_feature_share > 80,
        "sibling_status": sibling_status,
        "sibling_sample_rows": len(sibling_sample_rows),
        "sibling_pairwise_exploded": sibling_pairwise_exploded,
        "event_phase_reliable_for_rules": post_event_share <= 50,
        "market_family_strong_feature": family_unknown_share <= 25,
    }


def _count_csv_rows(path: Path) -> int:
    with path.open("r", newline="", encoding="utf-8") as f:
        return max(0, sum(1 for _ in f) - 1)


def _build_rules(
    orders: list[dict[str, str]],
    pairs: list[dict[str, str]],
    merges: list[dict[str, str]],
    durations: list[dict[str, str]],
    classifications: dict[str, object],
    quality: dict[str, object],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    total_fills = len(orders)
    wallet = _text(orders[0], "wallet") if orders else ""

    def rows_where(predicate) -> list[dict[str, str]]:
        return [row for row in orders if predicate(row)]

    rule_specs = [
        {
            "rule_id": "A",
            "rule_name": "Complement Catch-Up",
            "rule_type": "entry",
            "rows": rows_where(_is_complement_catchup),
            "pair_rows": [row for row in pairs if _text(row, "completion_confidence") != "not_completed"],
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "Buy the opposite token when the wallet already carries unpaired inventory and the observed combined set cost is below a threshold.",
            "trigger_logic": "If condition-level inventory is unpaired on one side, place a BUY on the complement side only when current complement price plus the existing leg cost is below the chosen complete-set threshold.",
            "trigger_features": "wallet, event_id, condition_id, fill_token_side, qty_yes_before, qty_no_before, unpaired_yes_before, unpaired_no_before, fill/quote price, pre-fill inventory WAC or prior leg cost",
            "diagnostic_features": "complete_set_cost, time_to_complement_s, completed_pair_qty, MERGE timing, REDEEM/resolution",
            "excluded": "complement_fill_ts as a trigger, future complete_set_cost computed after the fill, MERGE/REDEEM, final unpaired duration",
            "thresholds": "complete_set_cost",
            "simulator_eligible": True,
            "live_eligible_later": True,
            "leakage_risk": "low if complete-set cost is computed from pre-fill inventory cost and live quote, not from future complement rows",
            "recommendation": "Implement first in Phase 22.6 simulator; use closed-complete lifecycle metrics only for completion assumptions.",
            "failure_modes": "Existing inventory WAC may be stale; complement quote may not fill; live events can remain unpaired for hours; threshold support is censored for open/recent events.",
        },
        {
            "rule_id": "B",
            "rule_name": "Bond-Increasing BUY",
            "rule_type": "entry",
            "rows": rows_where(lambda row: (_num(row, "bond_delta") or 0) > _DUST),
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "Allow BUY fills only when the intended token would increase paired/bond inventory.",
            "trigger_logic": "Given current YES/NO quantities, BUY only the side that increases min(qty_yes, qty_no).",
            "trigger_features": "wallet, event_id, condition_id, intended token side, qty_yes_before, qty_no_before, paired_qty_before, quote size",
            "diagnostic_features": "bond_delta, paired_qty_after, merge timing, later unpaired duration",
            "excluded": "paired_qty_after except as validation, future MERGE/REDEEM, time_to_complement_s",
            "thresholds": "bond_delta",
            "simulator_eligible": True,
            "live_eligible_later": True,
            "leakage_risk": "low; trigger can be computed from pre-fill inventory and intended token side",
            "recommendation": "Implement as a Phase 22.6 simulator entry filter or size cap.",
            "failure_modes": "May miss profitable first-leg inventory building; assumes local inventory state is correct at quote time.",
        },
        {
            "rule_id": "C",
            "rule_name": "Unpaired-Reducing BUY",
            "rule_type": "entry",
            "rows": rows_where(lambda row: (_num(row, "unpaired_delta") or 0) < -_DUST),
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "Allow BUY fills only when they reduce existing unpaired exposure.",
            "trigger_logic": "BUY YES when NO is unpaired, or BUY NO when YES is unpaired; measure separately from bond increase.",
            "trigger_features": "wallet, event_id, condition_id, fill_token_side, unpaired_yes_before, unpaired_no_before, quote size",
            "diagnostic_features": "unpaired_delta, paired_qty_after, unpaired duration, MERGE/REDEEM",
            "excluded": "unpaired_after as a trigger, future complement and final outcome fields",
            "thresholds": "unpaired_delta",
            "simulator_eligible": True,
            "live_eligible_later": True,
            "leakage_risk": "low; trigger can be computed before fill from inventory and intended side",
            "recommendation": "Implement only if Phase 22.6 wants a stricter version of Rule B; otherwise keep as an ablation.",
            "failure_modes": "Overlaps heavily with Rule B and may be too restrictive during basket build-up.",
        },
        {
            "rule_id": "D",
            "rule_name": "Event Basket Activation",
            "rule_type": "gating",
            "rows": rows_where(_is_event_basket_active),
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "Operate only after an event already has multiple active markets and event-level inventory exists.",
            "trigger_logic": "Enable entry rules only when event_market_count_active_before >= 2 and either event_unpaired_inventory_before or event_bond_qty_before is positive.",
            "trigger_features": "event_id, event_market_count_active_before, event_unpaired_inventory_before, event_bond_qty_before",
            "diagnostic_features": "event_market_count_active_after, event inventory after fill, sibling activity summary",
            "excluded": "market_family if unknown/null share is high, future sibling sequence rows",
            "thresholds": "event_inventory",
            "simulator_eligible": True,
            "live_eligible_later": True,
            "leakage_risk": "low if used only as a pre-fill gate; do not rely on pairwise-exploded sibling report",
            "recommendation": "Implement as a Phase 22.6 gating rule with Rule A/B, not as a standalone entry.",
            "failure_modes": "Can block first useful event fills; event metadata grouping errors can misstate active market count.",
        },
        {
            "rule_id": "E",
            "rule_name": "Unpaired Inventory Tolerance",
            "rule_type": "risk_management",
            "rows": orders,
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "Do not force immediate exit solely because inventory is temporarily unpaired.",
            "trigger_logic": "After a fill, tolerate unpaired exposure within event and wallet risk caps because observed unpaired periods can last hours.",
            "trigger_features": "pre-fill inventory, event-level exposure, max unpaired inventory cap, elapsed time since unpaired start",
            "diagnostic_features": "unpaired duration, resolved_by, final PnL if available",
            "excluded": "final duration and resolved_by as entry triggers",
            "thresholds": "duration",
            "simulator_eligible": False,
            "live_eligible_later": True,
            "leakage_risk": "medium; duration evidence is post-fill lifecycle diagnostic",
            "recommendation": "Use as a risk-management assumption in simulator, not as an entry signal.",
            "failure_modes": "Open events are right-censored; tolerance can become uncontrolled directional exposure without hard caps.",
        },
        {
            "rule_id": "F",
            "rule_name": "Batch Merge",
            "rule_type": "exit_capital_recycling",
            "rows": orders,
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "When paired quantity exists across event conditions, merge in batches to release capital.",
            "trigger_logic": "Run MERGE/recycle when paired_qty exists and batch size or event capital release threshold is met.",
            "trigger_features": "paired_qty_before/current, condition_id, event_id, wallet, merge batch threshold",
            "diagnostic_features": "merge_ts, time_from_last_complement_fill_s, capital_released, merge_batch_id",
            "excluded": "future merge timing as an entry signal",
            "thresholds": "merge",
            "simulator_eligible": False,
            "live_eligible_later": True,
            "leakage_risk": "medium; MERGE report is post-fill exit evidence",
            "recommendation": "Implement after entry simulation exists as capital recycling logic.",
            "failure_modes": "MERGE can be delayed by settlement, gas/contract conditions, or unresolved markets.",
        },
        {
            "rule_id": "G",
            "rule_name": "Passive Maker Wait",
            "rule_type": "execution_style",
            "rows": rows_where(_is_passive_maker_wait),
            "pair_rows": pairs,
            "merge_rows": merges,
            "duration_rows": durations,
            "short_description": "RN1 often appears to wait before fills, but evidence comes mostly from compatible book snapshots.",
            "trigger_logic": "Prefer passive BUY quotes that can rest before fill, while treating estimated_book_seen only as compatibility evidence.",
            "trigger_features": "role, order_time_confidence, fill_after_first_seen_s when exact order data is unavailable",
            "diagnostic_features": "fill_after_first_seen_s, order_lifetime_s",
            "excluded": "estimated_book_seen as proof of RN1 ownership, future fill timing",
            "thresholds": "wait",
            "simulator_eligible": False,
            "live_eligible_later": False,
            "leakage_risk": "high without exact order placement/cancel data",
            "recommendation": "Do not implement as Phase 22.6 entry logic unless exact order placement data is added.",
            "failure_modes": "Book snapshots show compatible resting liquidity, not necessarily RN1 orders.",
        },
    ]

    rules: list[dict[str, object]] = []
    evidence: list[dict[str, object]] = []
    for spec in rule_specs:
        row = _summarize_rule(spec, wallet, total_fills, classifications)
        rules.append(row)
        evidence.extend(_evidence_for_rule(spec, classifications))

    rules.sort(key=lambda row: str(row["rule_id"]))
    return rules, evidence


def _is_complement_catchup(row: dict[str, str]) -> bool:
    side = _text(row, "fill_token_side").upper()
    if side == "YES":
        return (_num(row, "unpaired_no_before") or 0) > _DUST and (_num(row, "bond_delta") or 0) > _DUST
    if side == "NO":
        return (_num(row, "unpaired_yes_before") or 0) > _DUST and (_num(row, "bond_delta") or 0) > _DUST
    return (_num(row, "bond_delta") or 0) > _DUST and (_num(row, "unpaired_delta") or 0) < -_DUST


def _is_event_basket_active(row: dict[str, str]) -> bool:
    return (
        (_num(row, "event_market_count_active_before") or 0) >= 2
        and (
            (_num(row, "event_unpaired_inventory_before") or 0) > _DUST
            or (_num(row, "event_bond_qty_before") or 0) > _DUST
        )
    )


def _is_passive_maker_wait(row: dict[str, str]) -> bool:
    return (
        _text(row, "role").lower() == "maker"
        and _text(row, "order_time_confidence") in {"estimated_book_seen", "exact_onchain"}
        and (_num(row, "fill_after_first_seen_s") or 0) > 0
    )


def _summarize_rule(
    spec: dict[str, object],
    wallet: str,
    total_fills: int,
    classifications: dict[str, object],
) -> dict[str, object]:
    rows: list[dict[str, str]] = spec["rows"]  # type: ignore[assignment]
    support_events = {_event_key(row) for row in rows if _event_key(row)}
    support_conditions = {_condition_key(row) for row in rows if _condition_key(row) != ("", "")}
    event_class: dict[str, str] = classifications["event_class"]  # type: ignore[assignment]
    closed_events = {event for event in support_events if event_class.get(event) == "closed_complete"}
    censored_events = support_events - closed_events
    censored_rows = [row for row in rows if event_class.get(_event_key(row)) != "closed_complete"]
    closed_rows = [row for row in rows if event_class.get(_event_key(row)) == "closed_complete"]

    pair_rows: list[dict[str, str]] = _filter_lifecycle_rows(  # type: ignore[assignment]
        spec["pair_rows"], support_events, support_conditions
    )
    merge_rows: list[dict[str, str]] = _filter_lifecycle_rows(  # type: ignore[assignment]
        spec["merge_rows"], support_events, support_conditions
    )
    duration_rows: list[dict[str, str]] = _filter_lifecycle_rows(  # type: ignore[assignment]
        spec["duration_rows"], support_events, support_conditions
    )

    closed_pair_rows = [row for row in pair_rows if event_class.get(_event_key(row)) == "closed_complete"]
    completed_closed_pairs = [
        row for row in closed_pair_rows
        if _text(row, "completion_confidence") != "not_completed"
    ]
    closed_duration_rows = [
        row for row in duration_rows
        if event_class.get(_event_key(row)) == "closed_complete"
        and _text(row, "resolved_by") != "still_open"
    ]
    closed_merge_rows = [row for row in merge_rows if event_class.get(_event_key(row)) == "closed_complete"]

    complete_costs = [_num(row, "complete_set_cost") for row in completed_closed_pairs]
    times_to_complement = [_num(row, "time_to_complement_s") for row in completed_closed_pairs]
    merge_condition_keys = {_condition_key(row) for row in closed_merge_rows}

    lifecycle_basis = max(len(closed_pair_rows), len(closed_duration_rows), len(closed_merge_rows), len(closed_rows))
    lifecycle_reliable = bool(lifecycle_basis >= 10 and (len(censored_rows) / len(rows) if rows else 1) <= 0.5)
    if spec["rule_id"] in {"E", "F"}:
        lifecycle_reliable = bool(lifecycle_basis >= 10)

    thresholds = _thresholds_for_rule(spec, rows, completed_closed_pairs, closed_duration_rows, closed_merge_rows)
    thresholds["post_fill_diagnostic_metric_views"] = _post_fill_metric_views(
        pair_rows=pair_rows,
        duration_rows=duration_rows,
        merge_rows=merge_rows,
        event_class=event_class,
    )

    return {
        "rule_id": spec["rule_id"],
        "rule_name": spec["rule_name"],
        "rule_type": spec["rule_type"],
        "wallet": wallet,
        "short_description": spec["short_description"],
        "trigger_logic_plain_english": spec["trigger_logic"],
        "trigger_features_pre_fill": spec["trigger_features"],
        "diagnostic_features_post_fill": spec["diagnostic_features"],
        "excluded_features_due_to_leakage": spec["excluded"],
        "threshold_values_observed_json": thresholds,
        "fills_supported": len(rows),
        "fills_supported_pct": _pct(len(rows), total_fills),
        "events_supported": len(support_events),
        "conditions_supported": len(support_conditions),
        "median_fill_price": _median(_num(row, "fill_price") for row in rows),
        "median_fill_size": _median(_num(row, "fill_size") for row in rows),
        "median_fill_notional": _median(_num(row, "fill_notional_usdc") for row in rows),
        "median_complete_set_cost": _median(complete_costs),
        "pct_complete_set_cost_lt_095": _pct(sum(1 for v in complete_costs if v is not None and v < 0.95), len([v for v in complete_costs if v is not None])),
        "pct_complete_set_cost_lt_098": _pct(sum(1 for v in complete_costs if v is not None and v < 0.98), len([v for v in complete_costs if v is not None])),
        "pct_complete_set_cost_lt_100": _pct(sum(1 for v in complete_costs if v is not None and v < 1.00), len([v for v in complete_costs if v is not None])),
        "p50_time_to_complement_s": _median(times_to_complement),
        "p90_time_to_complement_s": _p90(times_to_complement),
        "pct_increases_bond": _pct(sum(1 for row in rows if (_num(row, "bond_delta") or 0) > _DUST), len(rows)),
        "pct_reduces_unpaired": _pct(sum(1 for row in rows if (_num(row, "unpaired_delta") or 0) < -_DUST), len(rows)),
        "pct_increases_unpaired": _pct(sum(1 for row in rows if (_num(row, "unpaired_delta") or 0) > _DUST), len(rows)),
        "median_unpaired_duration_s": _median(_num(row, "duration_s") for row in closed_duration_rows),
        "p90_unpaired_duration_s": _p90(_num(row, "duration_s") for row in closed_duration_rows),
        "median_event_market_count_active_before": _median(_num(row, "event_market_count_active_before") for row in rows),
        "median_event_unpaired_inventory_before": _median(_num(row, "event_unpaired_inventory_before") for row in rows),
        "median_event_bond_qty_before": _median(_num(row, "event_bond_qty_before") for row in rows),
        "merge_followed_pct": _pct(len(merge_condition_keys), len({key for key in support_conditions if key[0] in closed_events})),
        "median_time_to_merge_s": _median(_num(row, "time_from_last_complement_fill_s") for row in closed_merge_rows),
        "total_merge_capital_released_if_applicable": round(sum(_num(row, "capital_released") or 0 for row in closed_merge_rows), 6),
        "support_stability": _support_stability(rows),
        "concentration_risk": _concentration_risk(rows),
        "leakage_risk": spec["leakage_risk"],
        "simulator_eligible": spec["simulator_eligible"],
        "live_eligible_later": spec["live_eligible_later"],
        "failure_modes": spec["failure_modes"],
        "recommendation": spec["recommendation"],
        "lifecycle_sample_scope": "closed_complete_only_view for lifecycle metrics; all_events_view for pre-fill support",
        "closed_complete_events_supported": len(closed_events),
        "live_or_censored_events_supported": len(censored_events),
        "censored_observation_share": _pct(len(censored_rows), len(rows)),
        "lifecycle_metrics_reliable": lifecycle_reliable,
        "censoring_notes": _censoring_notes(spec["rule_id"], censored_events, rows),
    }


def _filter_lifecycle_rows(
    rows: object,
    support_events: set[str],
    support_conditions: set[tuple[str, str]],
) -> list[dict[str, str]]:
    if not isinstance(rows, list):
        return []
    if not support_events:
        return rows
    return [
        row for row in rows
        if _event_key(row) in support_events
        and (not _text(row, "condition_id") or _condition_key(row) in support_conditions or not support_conditions)
    ]


def _thresholds_for_rule(
    spec: dict[str, object],
    rows: list[dict[str, str]],
    pairs: list[dict[str, str]],
    durations: list[dict[str, str]],
    merges: list[dict[str, str]],
) -> dict[str, object]:
    kind = spec["thresholds"]
    if kind == "complete_set_cost":
        costs = [_num(row, "complete_set_cost") for row in pairs]
        clean = [v for v in costs if v is not None]
        return {
            "complete_set_cost_closed_complete_only": {
                "p50": _median(clean),
                "p90": _p90(clean),
                "pct_lt_0.95": _pct(sum(1 for v in clean if v < 0.95), len(clean)),
                "pct_lt_0.98": _pct(sum(1 for v in clean if v < 0.98), len(clean)),
                "pct_lt_1.00": _pct(sum(1 for v in clean if v < 1.00), len(clean)),
            },
            "candidate_thresholds": [0.95, 0.98, 1.00],
        }
    if kind == "bond_delta":
        return {"bond_delta_trigger": "> 0", "observed_p50_bond_delta": _median(_num(row, "bond_delta") for row in rows)}
    if kind == "unpaired_delta":
        return {"unpaired_delta_trigger": "< 0", "observed_p50_unpaired_delta": _median(_num(row, "unpaired_delta") for row in rows)}
    if kind == "event_inventory":
        return {
            "event_market_count_active_before_min": 2,
            "observed_p50_active_markets": _median(_num(row, "event_market_count_active_before") for row in rows),
            "observed_p50_event_unpaired_before": _median(_num(row, "event_unpaired_inventory_before") for row in rows),
        }
    if kind == "duration":
        return {
            "closed_complete_duration_p50_s": _median(_num(row, "duration_s") for row in durations),
            "closed_complete_duration_p90_s": _p90(_num(row, "duration_s") for row in durations),
            "resolved_by_distribution": dict(_counter(durations, "resolved_by")),
        }
    if kind == "merge":
        return {
            "merge_qty_p50": _median(_num(row, "merge_qty") for row in merges),
            "time_from_last_complement_fill_p50_s": _median(_num(row, "time_from_last_complement_fill_s") for row in merges),
            "capital_released_total": round(sum(_num(row, "capital_released") or 0 for row in merges), 6),
        }
    return {
        "fill_after_first_seen_p50_s": _median(_num(row, "fill_after_first_seen_s") for row in rows),
        "order_time_confidence_distribution": dict(_counter(rows, "order_time_confidence")),
    }


def _post_fill_metric_views(
    *,
    pair_rows: list[dict[str, str]],
    duration_rows: list[dict[str, str]],
    merge_rows: list[dict[str, str]],
    event_class: dict[str, str],
) -> dict[str, object]:
    closed_pairs = [row for row in pair_rows if event_class.get(_event_key(row)) == "closed_complete"]
    closed_durations = [
        row for row in duration_rows
        if event_class.get(_event_key(row)) == "closed_complete"
        and _text(row, "resolved_by") != "still_open"
    ]
    closed_merges = [row for row in merge_rows if event_class.get(_event_key(row)) == "closed_complete"]
    return {
        "all_events_view": _post_fill_view(pair_rows, duration_rows, merge_rows),
        "closed_complete_only_view": _post_fill_view(closed_pairs, closed_durations, closed_merges),
        "usage_note": (
            "Use all_events_view for descriptive diagnostics only. Use "
            "closed_complete_only_view for final complement, not_completed, "
            "unpaired duration, merge, redeem/resolution, and lifecycle-completion support."
        ),
    }


def _post_fill_view(
    pair_rows: list[dict[str, str]],
    duration_rows: list[dict[str, str]],
    merge_rows: list[dict[str, str]],
) -> dict[str, object]:
    completed_pairs = [
        row for row in pair_rows
        if _text(row, "completion_confidence") != "not_completed"
    ]
    complete_costs = [_num(row, "complete_set_cost") for row in completed_pairs]
    times = [_num(row, "time_to_complement_s") for row in completed_pairs]
    durations = [_num(row, "duration_s") for row in duration_rows]
    return {
        "pair_rows": len(pair_rows),
        "completed_pair_rows": len(completed_pairs),
        "not_completed_pct": _pct(len(pair_rows) - len(completed_pairs), len(pair_rows)),
        "median_complete_set_cost": _median(complete_costs),
        "pct_complete_set_cost_lt_0.95": _pct(sum(1 for v in complete_costs if v is not None and v < 0.95), len([v for v in complete_costs if v is not None])),
        "pct_complete_set_cost_lt_0.98": _pct(sum(1 for v in complete_costs if v is not None and v < 0.98), len([v for v in complete_costs if v is not None])),
        "pct_complete_set_cost_lt_1.00": _pct(sum(1 for v in complete_costs if v is not None and v < 1.00), len([v for v in complete_costs if v is not None])),
        "p50_time_to_complement_s": _median(times),
        "p90_time_to_complement_s": _p90(times),
        "unpaired_duration_rows": len(duration_rows),
        "still_open_unpaired_rows": sum(1 for row in duration_rows if _text(row, "resolved_by") == "still_open"),
        "median_unpaired_duration_s": _median(durations),
        "p90_unpaired_duration_s": _p90(durations),
        "merge_rows": len(merge_rows),
        "median_time_to_merge_s": _median(_num(row, "time_from_last_complement_fill_s") for row in merge_rows),
        "total_merge_capital_released": round(sum(_num(row, "capital_released") or 0 for row in merge_rows), 6),
        "resolved_by_distribution": dict(_counter(duration_rows, "resolved_by")),
    }


def _support_stability(rows: list[dict[str, str]]) -> str:
    events = Counter(_event_key(row) for row in rows if _event_key(row))
    if not events:
        return "no support"
    top_event, top_count = events.most_common(1)[0]
    share = _pct(top_count, len(rows)) or 0
    if len(events) >= 10 and share < 35:
        label = "broad"
    elif len(events) >= 4 and share < 60:
        label = "moderate"
    else:
        label = "thin"
    return f"{label}; events={len(events)}; top_event={top_event}; top_event_share_pct={share}"


def _concentration_risk(rows: list[dict[str, str]]) -> str:
    events = Counter(_event_key(row) for row in rows if _event_key(row))
    if not events:
        return "high; no event spread"
    share = _pct(events.most_common(1)[0][1], len(rows)) or 0
    if share >= 60:
        return f"high; top event share {share}%"
    if share >= 35:
        return f"medium; top event share {share}%"
    return f"low; top event share {share}%"


def _censoring_notes(rule_id: object, censored_events: set[str], rows: list[dict[str, str]]) -> str:
    if not rows:
        return "no support rows"
    if not censored_events:
        return "all supported events classified closed_complete"
    return (
        f"{len(censored_events)} supported events are live/recent/open_unknown; "
        f"Rule {rule_id} lifecycle completion metrics must not count missing complement/MERGE/REDEEM as failures."
    )


def _evidence_for_rule(spec: dict[str, object], classifications: dict[str, object]) -> list[dict[str, object]]:
    rows: list[dict[str, str]] = spec["rows"]  # type: ignore[assignment]
    event_class: dict[str, str] = classifications["event_class"]  # type: ignore[assignment]
    evidence: list[dict[str, object]] = []
    for row in rows[:12]:
        trigger = {
            "price": row.get("fill_price"),
            "size": row.get("fill_size"),
            "fill_token_side": row.get("fill_token_side"),
            "qty_yes_before": row.get("qty_yes_before"),
            "qty_no_before": row.get("qty_no_before"),
            "event_markets_before": row.get("event_market_count_active_before"),
            "event_unpaired_before": row.get("event_unpaired_inventory_before"),
            "event_bond_before": row.get("event_bond_qty_before"),
        }
        outcome = {
            "bond_delta": row.get("bond_delta"),
            "unpaired_delta": row.get("unpaired_delta"),
            "paired_qty_after": row.get("paired_qty_after"),
            "feature_availability": row.get("feature_availability"),
        }
        evidence.append(
            {
                "rule_id": spec["rule_id"],
                "evidence_scope": "all_events_view_pre_fill_support",
                "wallet": row.get("wallet"),
                "event_id": row.get("event_id"),
                "condition_id": row.get("condition_id"),
                "fill_event_id": row.get("fill_event_id"),
                "fill_utc": row.get("fill_utc"),
                "question": row.get("question"),
                "trigger_snapshot": trigger,
                "post_fill_outcome": outcome,
                "lifecycle_classification": event_class.get(_event_key(row), "open_unknown"),
                "notes": "Post-fill fields are diagnostic only; trigger snapshot uses pre-fill inventory/context.",
            }
        )
    return evidence


def _render_quality_report(quality: dict[str, object], classifications: dict[str, object]) -> str:
    lines = [
        "# Phase 22.5b Pattern Quality Report",
        "",
        "## File Row Counts",
        *_dict_lines(quality["row_counts"]),  # type: ignore[arg-type]
        "",
        "## Coverage",
        f"- unique_events: {quality['unique_events']}",
        f"- unique_condition_ids: {quality['unique_conditions']}",
        "",
        "## Role Distribution",
        *_dict_lines(quality["role_distribution"]),  # type: ignore[arg-type]
        "",
        "## Order Time Confidence Distribution",
        *_dict_lines(quality["order_time_confidence_distribution"]),  # type: ignore[arg-type]
        "",
        "## Market Family Quality",
        *_dict_lines(quality["market_family_distribution"]),  # type: ignore[arg-type]
        f"- unknown_or_null_share_pct: {quality['market_family_unknown_share']}",
        f"- use_as_strong_rule_feature: {quality['market_family_strong_feature']}",
        "",
        "## Event Phase Quality",
        *_dict_lines(quality["event_phase_distribution"]),  # type: ignore[arg-type]
        f"- use_event_phase_in_rule_extraction: {quality['event_phase_reliable_for_rules']}",
        "- decision: exclude event_phase as a rule trigger when post_event dominates or timing metadata is only inferred.",
        "",
        "## Feature Availability Quality",
        *_dict_lines(quality["feature_availability_distribution"]),  # type: ignore[arg-type]
        f"- too_coarse_due_to_pre_fill_post_fill_mix: {quality['feature_availability_too_coarse']}",
        "- decision: split features into pre_fill_available, post_fill_diagnostic, and final_outcome_only in rule_candidates.csv.",
        "",
        "## Sibling Sequence Quality",
        f"- status: {quality['sibling_status']}",
        f"- sampled_rows: {quality['sibling_sample_rows']}",
        f"- appears_pairwise_exploded: {quality['sibling_pairwise_exploded']}",
        "- decision: do not require or fully load sibling_market_sequence_report for rule extraction.",
        "",
        "## Lifecycle Classification",
        "Event-condition classes:",
        *_dict_lines(classifications["condition_counts"]),  # type: ignore[arg-type]
        "",
        "Event classes:",
        *_dict_lines(classifications["event_counts"]),  # type: ignore[arg-type]
        "",
        "Live, recently closed, open_unknown, and still-open observations are treated as right-censored for lifecycle metrics.",
    ]
    return "\n".join(lines) + "\n"


def _render_extraction_report(
    rules: list[dict[str, object]],
    quality: dict[str, object],
    classifications: dict[str, object],
) -> str:
    simulator_candidates = [row for row in rules if row["rule_id"] in {"A", "B", "D"}]
    lines = [
        "# Phase 22.5b Rule Candidate Extraction Report",
        "",
        "This report extracts actionable rule candidates from existing Phase 22.5 pattern outputs only. It does not rebuild datasets, run a simulator, optimize strategy parameters, or promote rules to paper.",
        "",
        "## Pattern Quality Summary",
        f"- rows analyzed: {quality['row_counts'].get('order_timing_dataset.csv')}",  # type: ignore[union-attr]
        f"- unique events: {quality['unique_events']}",
        f"- unique condition_ids: {quality['unique_conditions']}",
        f"- event_phase used as trigger feature: {quality['event_phase_reliable_for_rules']}",
        f"- market_family used as strong feature: {quality['market_family_strong_feature']}",
        f"- sibling report pairwise-exploded: {quality['sibling_pairwise_exploded']}",
        "",
        "## Lifecycle Censoring",
        "Before rule extraction, event/condition observations were classified as closed_complete, recently_closed_pending_settlement, live_or_in_progress, or open_unknown.",
        "",
        "Event classes:",
        *_dict_lines(classifications["event_counts"]),  # type: ignore[arg-type]
        "",
        "Missing complement, MERGE, remaining unpaired inventory, and missing REDEEM are not counted as final failures for live/recent/open_unknown observations.",
        "",
        "## Pre-fill pattern evidence",
        "Can use all events.",
        "",
        "| Rule | Type | Fills | Events | Conditions | Simulator eligible | Pre-fill trigger basis |",
        "| --- | --- | ---: | ---: | ---: | --- | --- |",
    ]
    for row in rules:
        lines.append(
            f"| {row['rule_id']} {row['rule_name']} | {row['rule_type']} | "
            f"{row['fills_supported']} | {row['events_supported']} | {row['conditions_supported']} | "
            f"{row['simulator_eligible']} | {row['trigger_features_pre_fill']} |"
        )

    lines.extend(
        [
            "",
            "Pre-fill usable fields include inventory before the fill, intended token side, role/maker/taker, fill/quote price context, book freshness/order timing confidence as compatibility evidence, bond_delta computable from intended fill, event inventory before/after immediate fill diagnostics, and event active-market counts. `estimated_book_seen` is not treated as proof RN1 owned the order.",
            "",
            "## Lifecycle completion evidence",
            "Use only closed_complete events unless explicitly marked as censored.",
            "",
            "| Rule | Closed events | Censored events | Censored share pct | Completion cost p50 | Time-to-complement p50 | Merge followed pct | Reliable |",
            "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
        ]
    )
    for row in rules:
        lines.append(
            f"| {row['rule_id']} {row['rule_name']} | {row['closed_complete_events_supported']} | "
            f"{row['live_or_censored_events_supported']} | {row['censored_observation_share']} | "
            f"{row['median_complete_set_cost']} | {row['p50_time_to_complement_s']} | "
            f"{row['merge_followed_pct']} | {row['lifecycle_metrics_reliable']} |"
        )

    lines.extend(
        [
            "",
            "Rules that look strong only because live/recent events have not completed are marked unreliable through `lifecycle_metrics_reliable=0` and a non-zero censored observation share.",
            "",
            "## Candidate Notes",
        ]
    )
    for row in rules:
        lines.extend(
            [
                f"### Rule {row['rule_id']} - {row['rule_name']}",
                f"- recommendation: {row['recommendation']}",
                f"- leakage risk: {row['leakage_risk']}",
                f"- support stability: {row['support_stability']}",
                f"- failure modes: {row['failure_modes']}",
                f"- censoring: {row['censoring_notes']}",
                "",
            ]
        )

    lines.extend(
        [
            "## Recommended Phase 22.6 simulator candidates",
            "",
            "These are implementation candidates only; this report does not claim profitability.",
            "",
        ]
    )
    for row in simulator_candidates:
        lines.extend(_simulator_candidate_block(row))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _simulator_candidate_block(row: dict[str, object]) -> list[str]:
    if row["rule_id"] == "A":
        return [
            "### Rule A - Complement Catch-Up",
            "1. exact pre-fill trigger: condition has unpaired inventory on the opposite side and live complement quote keeps existing leg cost plus quote price below threshold, initially test 0.98 and 1.00.",
            "2. quote side: BUY complement token.",
            "3. quote price formula: max_bid = threshold - WAC/opportunity cost of existing unpaired opposite leg; quote no higher than current best bid/max_bid.",
            "4. size formula: min(opposite_unpaired_qty, per-condition cap remaining, event cap remaining / quote_price).",
            "5. inventory requirement: unpaired_yes_before > 0 when buying NO, or unpaired_no_before > 0 when buying YES.",
            "6. event-level risk limit: cap total event unpaired inventory and total event capital before quoting.",
            "7. cancel condition: combined cost >= threshold, opposite unpaired inventory is gone, book becomes stale, or event risk cap is hit.",
            "8. merge/recycle condition: merge when paired_qty exceeds minimum merge size or after a batch window, using only already paired inventory.",
            "9. required data fields: token side, condition_id, event_id, qty_yes/no before, unpaired_yes/no before, WAC/cost basis, best bid/ask/book age, fill size/price.",
            "10. why it is not leakage: every trigger input is known before quote/fill; complement timing and final lifecycle outcomes are diagnostics only.",
        ]
    if row["rule_id"] == "B":
        return [
            "### Rule B - Bond-Increasing BUY",
            "1. exact pre-fill trigger: intended BUY increases min(qty_yes, qty_no) relative to current inventory.",
            "2. quote side: BUY the side that increases paired/bond quantity.",
            "3. quote price formula: quote at or below a configured max complete-set cost minus opposite-side inventory WAC; otherwise skip.",
            "4. size formula: min(opposite_side_qty - same_side_qty if positive, event cap remaining / quote_price, order size cap).",
            "5. inventory requirement: opposite side quantity exceeds same side quantity before the fill.",
            "6. event-level risk limit: max event capital used and max residual unpaired exposure.",
            "7. cancel condition: quote no longer increases bond inventory, book is stale, or event cap is reached.",
            "8. merge/recycle condition: merge paired quantity once minimum batch size is reached.",
            "9. required data fields: qty_yes/no before, intended token side, quote price/size, condition_id, event_id, book freshness.",
            "10. why it is not leakage: bond_delta can be computed from current inventory and proposed order before the fill.",
        ]
    return [
        "### Rule D - Event Basket Activation",
        "1. exact pre-fill trigger: event_market_count_active_before >= 2 and event_unpaired_inventory_before > 0 or event_bond_qty_before > 0.",
        "2. quote side: inherited from the active entry rule, usually BUY.",
        "3. quote price formula: no standalone price; apply as a gate before Rule A/B price formulas.",
        "4. size formula: no standalone size; pass through Rule A/B size after event cap check.",
        "5. inventory requirement: event-level inventory already exists before the candidate fill.",
        "6. event-level risk limit: require event capital and unpaired inventory to remain below configured caps.",
        "7. cancel condition: event inventory goes flat, active-market count drops below threshold, or event data becomes stale.",
        "8. merge/recycle condition: inherited from active entry rule; merge paired quantity by condition/event batch.",
        "9. required data fields: event_id, condition_id, event_market_count_active_before, event_unpaired_inventory_before, event_bond_qty_before.",
        "10. why it is not leakage: all gate inputs are pre-fill inventory or event context; sibling sequence futures are not used.",
    ]


def _dict_lines(values: object) -> list[str]:
    if isinstance(values, Counter):
        iterable = values.most_common()
    elif isinstance(values, dict):
        iterable = values.items()
    else:
        return [f"- {values}"]
    return [f"- {key}: {value}" for key, value in iterable]
