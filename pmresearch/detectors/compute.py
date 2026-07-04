"""Detector registry, computation and persistence (Phase 14).

`run_detectors` loads a wallet's persisted fingerprints (the latest version) for
a window, groups them into per-scope `DetectorInput` bundles, runs every
registered detector over every scope, and writes one `strategy_labels` row per
(wallet, scope, detector, detector_version) — a scored, evidenced, blind-spotted
hypothesis. Drop-and-rebuild per wallet: a run deletes the wallet's prior labels
and reinserts the current snapshot, so results are deterministic and a
detector-version bump replaces old rows.

Detectors read fingerprints ONLY — the sole query here selects from the
`fingerprints` table. No projection or ledger access.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from . import inventory_cycling, market_making, value_betting
from .base import DetectorInput, DetectorResult, FeatureCell, evaluate

# Ordered registry: all three MVP detectors.
DETECTOR_REGISTRY = [
    market_making.DETECTOR,
    inventory_cycling.DETECTOR,
    value_betting.DETECTOR,
]


def all_detectors():
    return list(DETECTOR_REGISTRY)


@dataclass(frozen=True)
class DetectorStats:
    wallet: str
    scopes: int
    labels_written: int


@dataclass(frozen=True)
class LabelRow:
    wallet: str
    scope: str
    detector_name: str
    detector_version: int
    label: str
    score: str
    confidence: str
    evidence_json: str
    blind_spots: str
    computed_at: str


# --- loading fingerprints ---------------------------------------------------


def _load_scope_inputs(
    session: Session, wallet: str, window: str
) -> tuple[dict[str, DetectorInput], Optional[int]]:
    """scope -> DetectorInput for the wallet's latest fingerprint version."""
    version = session.execute(
        text("SELECT MAX(version) FROM fingerprints WHERE wallet = :w"),
        {"w": wallet},
    ).scalar()
    if version is None:
        return {}, None
    version = int(version)

    rows = session.execute(
        text(
            "SELECT scope, feature, value, value_type, null_reason "
            "FROM fingerprints WHERE wallet = :w AND window = :window AND version = :v"
        ),
        {"w": wallet, "window": window, "v": version},
    ).fetchall()

    by_scope: dict[str, dict[str, FeatureCell]] = {}
    for r in rows:
        by_scope.setdefault(r.scope, {})[r.feature] = FeatureCell(
            value=r.value, value_type=r.value_type, null_reason=r.null_reason
        )

    inputs = {
        scope: DetectorInput(
            wallet=wallet,
            scope=scope,
            window=window,
            fingerprint_version=version,
            cells=cells,
        )
        for scope, cells in by_scope.items()
    }
    return inputs, version


# --- persistence ------------------------------------------------------------


_INSERT_SQL = text(
    "INSERT INTO strategy_labels "
    "(wallet, scope, detector_name, detector_version, label, score, confidence, "
    "evidence_json, blind_spots, computed_at) "
    "VALUES (:wallet, :scope, :detector_name, :detector_version, :label, :score, "
    ":confidence, :evidence_json, :blind_spots, :computed_at)"
)


def run_detectors(
    session: Session,
    wallet: str,
    *,
    detectors=None,
    window: str = "all",
) -> DetectorStats:
    """Drop and rebuild all strategy labels for one wallet across every scope."""
    wallet = wallet.lower()
    registry = detectors if detectors is not None else all_detectors()

    scope_inputs, _version = _load_scope_inputs(session, wallet, window)
    computed_at = datetime.now(timezone.utc).isoformat()

    rows: list[dict] = []
    for scope in sorted(scope_inputs):
        inp = scope_inputs[scope]
        for detector in registry:
            result: DetectorResult = evaluate(detector, inp)
            rows.append(
                {
                    "wallet": wallet,
                    "scope": scope,
                    "detector_name": result.detector_name,
                    "detector_version": result.detector_version,
                    "label": result.label,
                    "score": str(result.score),
                    "confidence": str(result.confidence),
                    "evidence_json": result.evidence_json(),
                    "blind_spots": result.blind_spots,
                    "computed_at": computed_at,
                }
            )

    session.execute(text("DELETE FROM strategy_labels WHERE wallet = :w"), {"w": wallet})
    if rows:
        session.execute(_INSERT_SQL, rows)
    session.commit()

    return DetectorStats(
        wallet=wallet, scopes=len(scope_inputs), labels_written=len(rows)
    )


def fetch_labels(
    session: Session,
    wallet: str,
    *,
    scope: Optional[str] = None,
    detector_name: Optional[str] = None,
) -> list[LabelRow]:
    where = ["wallet = :w"]
    params: dict = {"w": wallet.lower()}
    if scope is not None:
        where.append("scope = :scope")
        params["scope"] = scope
    if detector_name is not None:
        where.append("detector_name = :dn")
        params["dn"] = detector_name
    rows = session.execute(
        text(
            "SELECT wallet, scope, detector_name, detector_version, label, score, "
            "confidence, evidence_json, blind_spots, computed_at "
            "FROM strategy_labels "
            f"WHERE {' AND '.join(where)} ORDER BY scope, detector_name"
        ),
        params,
    ).fetchall()
    return [
        LabelRow(
            wallet=r.wallet,
            scope=r.scope,
            detector_name=r.detector_name,
            detector_version=int(r.detector_version),
            label=r.label,
            score=r.score,
            confidence=r.confidence,
            evidence_json=r.evidence_json,
            blind_spots=r.blind_spots,
            computed_at=r.computed_at,
        )
        for r in rows
    ]


def label_scopes(session: Session, wallet: str) -> list[str]:
    rows = session.execute(
        text(
            "SELECT DISTINCT scope FROM strategy_labels WHERE wallet = :w ORDER BY scope"
        ),
        {"w": wallet.lower()},
    ).fetchall()
    return [r.scope for r in rows]
