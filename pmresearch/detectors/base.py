"""Detector contract and the shared weighted-scoring framework.

A detector is a set of weighted `Signal`s. Each signal reads one fingerprint
feature from a `DetectorInput` and maps it — via a documented, monotone
transform — to a sub-score in [0, 1] measuring how strongly that feature
supports the detector's hypothesis. `evaluate` combines them:

    score = Σ(weight_i · sub_score_i) / Σ(weight_i)   over available signals
    confidence = Σ(available weight) / Σ(total weight)

NULL / missing features are *excluded* from both the numerator and the
denominator of `score` (never read as 0 — that would penalise low-coverage
wallets, a documented failure mode). They instead lower `confidence` and are
listed in the evidence. When no signal is available, `score` is 0 at
`confidence` 0 with an explicit "insufficient data" blind spot — the score is
then structurally meaningless and must be read via confidence, never as a
verdict.

Detectors read fingerprints ONLY: the sole data a `DetectorInput` exposes is
the persisted `fingerprints` rows for one (wallet, scope, window). No detector
touches a projection, the ledger, or raw SQL beyond that single table.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Optional

_ZERO = Decimal("0")
_ONE = Decimal("1")


def clamp01(value: Decimal) -> Decimal:
    """Clamp a Decimal into [0, 1]."""
    if value < _ZERO:
        return _ZERO
    if value > _ONE:
        return _ONE
    return value


def saturating(value: Decimal, half: Decimal) -> Decimal:
    """Monotone map of a non-negative unbounded rate onto [0, 1): value /
    (value + half). `half` is the value at which the sub-score reaches 0.5 —
    each detector documents the scale it picks."""
    if value <= _ZERO:
        return _ZERO
    return value / (value + half)


# --- input: the fingerprint bundle a detector may read ----------------------


@dataclass(frozen=True)
class FeatureCell:
    """One persisted fingerprint value (or NULL-with-reason)."""

    value: Optional[str]
    value_type: Optional[str]  # "scalar" | "json" | None
    null_reason: Optional[str]


@dataclass(frozen=True)
class DetectorInput:
    """Everything a detector may read for one (wallet, scope, window): the
    fingerprint feature cells, keyed by feature name."""

    wallet: str
    scope: str
    window: str
    fingerprint_version: int
    cells: dict[str, FeatureCell]

    def scalar(self, feature: str) -> Optional[Decimal]:
        cell = self.cells.get(feature)
        if cell is None or cell.value is None or cell.value_type != "scalar":
            return None
        return Decimal(cell.value)

    def distribution(self, feature: str) -> Optional[dict]:
        cell = self.cells.get(feature)
        if cell is None or cell.value is None or cell.value_type != "json":
            return None
        return json.loads(cell.value)

    def raw(self, feature: str) -> Optional[str]:
        cell = self.cells.get(feature)
        return None if cell is None else cell.value

    def reason(self, feature: str) -> str:
        cell = self.cells.get(feature)
        if cell is None:
            return "feature not computed"
        return cell.null_reason or "value not usable"


# --- signal: one weighted feature -------------------------------------------


@dataclass(frozen=True)
class SignalReading:
    """A signal's evaluation: the raw feature value (for evidence) plus a
    sub-score in [0, 1], or None when the feature is unavailable."""

    raw: Optional[str]
    sub_score: Optional[Decimal]
    note: Optional[str] = None  # why sub_score is None, when it is


@dataclass(frozen=True)
class Signal:
    """One weighted feature contribution to a detector's score."""

    feature: str
    weight: Decimal
    read: Callable[[DetectorInput], SignalReading]


def scalar_signal(feature: str, weight: Decimal) -> Signal:
    """A signal whose feature is already a [0, 1] share used directly as the
    sub-score (clamped defensively)."""

    def read(inp: DetectorInput) -> SignalReading:
        value = inp.scalar(feature)
        if value is None:
            return SignalReading(raw=inp.raw(feature), sub_score=None, note=inp.reason(feature))
        return SignalReading(raw=str(value), sub_score=clamp01(value))

    return Signal(feature=feature, weight=weight, read=read)


def saturating_signal(feature: str, weight: Decimal, half: Decimal) -> Signal:
    """A signal whose feature is a non-negative unbounded rate mapped through
    `saturating(value, half)`."""

    def read(inp: DetectorInput) -> SignalReading:
        value = inp.scalar(feature)
        if value is None:
            return SignalReading(raw=inp.raw(feature), sub_score=None, note=inp.reason(feature))
        return SignalReading(raw=str(value), sub_score=saturating(value, half))

    return Signal(feature=feature, weight=weight, read=read)


# --- detector + result ------------------------------------------------------


@dataclass(frozen=True)
class Detector:
    name: str
    version: int
    signals: list[Signal]
    blind_spots: str


@dataclass(frozen=True)
class DetectorResult:
    detector_name: str
    detector_version: int
    label: str
    score: Decimal
    confidence: Decimal
    evidence: dict
    blind_spots: str

    def evidence_json(self) -> str:
        return json.dumps(self.evidence, separators=(",", ":"), sort_keys=True)


def evaluate(detector: Detector, inp: DetectorInput) -> DetectorResult:
    """Run every signal, combine into score + confidence, assemble evidence."""
    features: dict[str, dict] = {}
    total_weight = _ZERO
    avail_weight = _ZERO
    weighted_sub = _ZERO
    missing: list[str] = []

    for sig in detector.signals:
        total_weight += sig.weight
        reading = sig.read(inp)
        entry: dict = {
            "value": reading.raw,
            "weight": str(sig.weight),
            "sub_score": None if reading.sub_score is None else str(reading.sub_score),
        }
        if reading.sub_score is None:
            entry["null_reason"] = reading.note or inp.reason(sig.feature)
            missing.append(sig.feature)
        else:
            avail_weight += sig.weight
            weighted_sub += sig.weight * reading.sub_score
        features[sig.feature] = entry

    if avail_weight > _ZERO:
        score = weighted_sub / avail_weight
        confidence = avail_weight / total_weight if total_weight > _ZERO else _ZERO
    else:
        score = _ZERO
        confidence = _ZERO

    blind_spots = detector.blind_spots
    if confidence == _ZERO:
        blind_spots = (
            "INSUFFICIENT DATA: no input feature was available; score is not "
            "meaningful (confidence 0). " + blind_spots
        )
    elif missing:
        blind_spots = (
            blind_spots
            + f" Score computed over {len(detector.signals) - len(missing)} of "
            f"{len(detector.signals)} features; missing: {', '.join(missing)}."
        )

    evidence = {
        "features": features,
        "confidence": str(confidence),
        "missing_features": missing,
        "score_formula": (
            "weighted mean of per-feature sub-scores over available features; "
            "NULL features excluded from numerator and denominator"
        ),
    }
    return DetectorResult(
        detector_name=detector.name,
        detector_version=detector.version,
        label=detector.name,
        score=score,
        confidence=confidence,
        evidence=evidence,
        blind_spots=blind_spots,
    )
