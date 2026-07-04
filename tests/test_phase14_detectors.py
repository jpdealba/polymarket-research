"""Phase 14 — strategy detectors.

Synthetic fingerprint fixtures with hand-computed expected scores, NULL-degrades-
confidence semantics (never read as 0), machine-readable evidence completeness,
contrast across archetypal wallets, and an end-to-end compute over seeded
fingerprint rows with a detector-version bump.
"""

from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import text

from pmresearch.detectors import inventory_cycling, market_making, value_betting
from pmresearch.detectors.base import (
    DetectorInput,
    FeatureCell,
    evaluate,
    saturating,
)
from pmresearch.detectors.compute import fetch_labels, label_scopes, run_detectors

D = Decimal


def _input(scope: str, features: dict) -> DetectorInput:
    """Build a DetectorInput from {feature: value}. A dict value is stored as a
    json distribution, None becomes a NULL-with-reason cell, everything else a
    scalar."""
    cells: dict[str, FeatureCell] = {}
    for name, value in features.items():
        if value is None:
            cells[name] = FeatureCell(value=None, value_type=None, null_reason="uncomputable")
        elif isinstance(value, dict):
            cells[name] = FeatureCell(
                value=json.dumps(value), value_type="json", null_reason=None
            )
        else:
            cells[name] = FeatureCell(value=str(value), value_type="scalar", null_reason=None)
    return DetectorInput(
        wallet="0xw", scope=scope, window="all", fingerprint_version=1, cells=cells
    )


# --- archetype fixtures -----------------------------------------------------

_MM_ARCHETYPE = {
    "maker_fill_share": "0.9",
    "reward_income_share": "0.6",
    "bond_inventory_ratio": "0.7",
    "micro_episode_share": "0.8",
    "merge_frequency": "0.2",
    "redeem_frequency": "0.1",
    "taker_fill_share": "0.1",
    "episode_duration_p50": "30",
    "resolution_outcome_calibration": None,
    "market_category_concentration": "0.2",
}

_VALUE_ARCHETYPE = {
    "maker_fill_share": "0.15",
    "reward_income_share": "0.05",
    "bond_inventory_ratio": "0.1",
    "micro_episode_share": "0.05",
    "merge_frequency": "0.05",
    "redeem_frequency": "0.2",
    "taker_fill_share": "0.85",
    "episode_duration_p50": "604800",  # 7 days
    "resolution_outcome_calibration": {
        "[0.6,0.7)": {"n": 10, "actual_win_rate": "0.8", "implied": "0.65"},
    },
    "market_category_concentration": "0.8",
}

_CYCLER_ARCHETYPE = {
    "maker_fill_share": "0.4",
    "reward_income_share": "0.2",
    "bond_inventory_ratio": "0.9",
    "micro_episode_share": "0.2",
    "merge_frequency": "1.0",
    "redeem_frequency": "0.5",
    "taker_fill_share": "0.5",
    "episode_duration_p50": "3600",
    "resolution_outcome_calibration": None,
    "market_category_concentration": "0.3",
}


# --- hand-computed detector math --------------------------------------------


def test_market_making_score_hand_computed():
    res = evaluate(market_making.DETECTOR, _input("all", _MM_ARCHETYPE))
    # (0.35*0.9 + 0.25*0.6 + 0.20*0.7 + 0.20*0.8) / 1.0
    expected = (D("0.35") * D("0.9") + D("0.25") * D("0.6")
                + D("0.20") * D("0.7") + D("0.20") * D("0.8"))
    assert res.score == expected  # 0.765
    assert res.confidence == D("1")
    assert res.label == "market_making"


def test_value_betting_score_hand_computed():
    res = evaluate(value_betting.DETECTOR, _input("all", _VALUE_ARCHETYPE))
    # taker 0.85; duration sat(604800, 86400); edge = 0.8-0.65 = 0.15 -> 0.65; conc 0.8
    dur = saturating(D("604800"), D("86400"))
    expected = (D("0.30") * D("0.85") + D("0.25") * dur
                + D("0.25") * D("0.65") + D("0.20") * D("0.8"))
    assert res.score == expected
    assert res.confidence == D("1")


def test_inventory_cycling_score_hand_computed():
    res = evaluate(inventory_cycling.DETECTOR, _input("all", _CYCLER_ARCHETYPE))
    # bond 0.9 direct; merge sat(1.0,0.5)=2/3; redeem sat(0.5,0.5)=0.5
    expected = (D("0.40") * D("0.9")
                + D("0.35") * saturating(D("1.0"), D("0.5"))
                + D("0.25") * saturating(D("0.5"), D("0.5")))
    assert res.score == expected


def test_score_always_in_unit_interval():
    for archetype in (_MM_ARCHETYPE, _VALUE_ARCHETYPE, _CYCLER_ARCHETYPE):
        inp = _input("all", archetype)
        for detector in (market_making.DETECTOR, inventory_cycling.DETECTOR, value_betting.DETECTOR):
            res = evaluate(detector, inp)
            assert D("0") <= res.score <= D("1")
            assert D("0") <= res.confidence <= D("1")


# --- contrast across archetypes ---------------------------------------------


def test_archetypes_separate():
    mm = _input("all", _MM_ARCHETYPE)
    vb = _input("all", _VALUE_ARCHETYPE)
    cy = _input("all", _CYCLER_ARCHETYPE)

    # MM wallet: market_making is its top detector, and >> its value_betting.
    mm_scores = {d.name: evaluate(d, mm).score for d in
                 (market_making.DETECTOR, inventory_cycling.DETECTOR, value_betting.DETECTOR)}
    assert max(mm_scores, key=mm_scores.get) == "market_making"
    assert mm_scores["market_making"] > mm_scores["value_betting"]

    # Value bettor: value_betting is top and >> its market_making.
    vb_scores = {d.name: evaluate(d, vb).score for d in
                 (market_making.DETECTOR, inventory_cycling.DETECTOR, value_betting.DETECTOR)}
    assert max(vb_scores, key=vb_scores.get) == "value_betting"
    assert vb_scores["value_betting"] > vb_scores["market_making"]

    # Cycler: inventory_cycling is top.
    cy_scores = {d.name: evaluate(d, cy).score for d in
                 (market_making.DETECTOR, inventory_cycling.DETECTOR, value_betting.DETECTOR)}
    assert max(cy_scores, key=cy_scores.get) == "inventory_cycling"


# --- NULL handling ----------------------------------------------------------


def test_null_feature_excluded_not_treated_as_zero():
    features = dict(_MM_ARCHETYPE)
    features["maker_fill_share"] = None  # drop the heaviest signal
    res = evaluate(market_making.DETECTOR, _input("all", features))
    # score = weighted mean over the 3 remaining, NOT (0 + those)/1.0
    weighted = D("0.25") * D("0.6") + D("0.20") * D("0.7") + D("0.20") * D("0.8")
    avail = D("0.25") + D("0.20") + D("0.20")
    assert res.score == weighted / avail  # ~0.6923, not 0.45
    assert res.confidence == avail  # 0.65 of total weight 1.0
    assert "maker_fill_share" in res.evidence["missing_features"]
    assert res.evidence["features"]["maker_fill_share"]["sub_score"] is None
    assert res.evidence["features"]["maker_fill_share"]["null_reason"]


def test_all_features_null_is_insufficient_data():
    features = {k: None for k in _MM_ARCHETYPE}
    res = evaluate(market_making.DETECTOR, _input("all", features))
    assert res.score == D("0")
    assert res.confidence == D("0")
    assert "INSUFFICIENT DATA" in res.blind_spots


def test_missing_feature_row_degrades_gracefully():
    # A DetectorInput entirely lacking a feature key (not even a NULL cell).
    res = evaluate(market_making.DETECTOR, _input("all", {"maker_fill_share": "0.5"}))
    assert res.evidence["features"]["reward_income_share"]["sub_score"] is None
    assert res.evidence["features"]["reward_income_share"]["null_reason"]
    # Only maker_fill_share contributed.
    assert res.score == D("0.5")
    assert res.confidence == D("0.35")


# --- evidence completeness --------------------------------------------------


def test_evidence_contains_every_input_feature_with_value():
    res = evaluate(value_betting.DETECTOR, _input("all", _VALUE_ARCHETYPE))
    features = res.evidence["features"]
    for signal in value_betting.DETECTOR.signals:
        assert signal.feature in features
        cell = features[signal.feature]
        assert "value" in cell and "weight" in cell and "sub_score" in cell
    # calibration_edge is a derived signal: value is the numeric edge string.
    assert Decimal(features["calibration_edge"]["value"]) == D("0.15")


def test_no_boolean_values_anywhere():
    for detector in (market_making.DETECTOR, inventory_cycling.DETECTOR, value_betting.DETECTOR):
        res = evaluate(detector, _input("all", _MM_ARCHETYPE))
        assert isinstance(res.score, Decimal)
        assert isinstance(res.confidence, Decimal)
        # Evidence carries decimal strings / None, never python booleans.
        for cell in res.evidence["features"].values():
            assert not isinstance(cell["sub_score"], bool)
            assert cell["sub_score"] is None or isinstance(cell["sub_score"], str)


# --- calibration edge -------------------------------------------------------


def test_calibration_edge_negative_scores_below_half():
    features = dict(_VALUE_ARCHETYPE)
    features["resolution_outcome_calibration"] = {
        "[0.7,0.8)": {"n": 5, "actual_win_rate": "0.5", "implied": "0.75"},
    }
    res = evaluate(value_betting.DETECTOR, _input("all", features))
    edge = res.evidence["features"]["calibration_edge"]
    assert Decimal(edge["value"]) == D("-0.25")
    assert Decimal(edge["sub_score"]) == D("0.25")  # clamp01(0.5 - 0.25)


# --- end to end over seeded fingerprints ------------------------------------


WALLET = "0x2222222222222222222222222222222222222222"


def _seed_fingerprints(session, wallet: str, scope: str, features: dict, version: int = 1):
    for name, value in features.items():
        if value is None:
            v, vt, nr = None, None, "uncomputable"
        elif isinstance(value, dict):
            v, vt, nr = json.dumps(value), "json", None
        else:
            v, vt, nr = str(value), "scalar", None
        session.execute(
            text(
                "INSERT INTO fingerprints (wallet, scope, feature, family, value, "
                "value_type, null_reason, window, computed_at, version) VALUES "
                "(:w,:s,:f,'test',:v,:vt,:nr,'all','2026-07-04T00:00:00+00:00',:ver)"
            ),
            {"w": wallet.lower(), "s": scope, "f": name, "v": v, "vt": vt, "nr": nr, "ver": version},
        )
    session.commit()


def test_run_detectors_end_to_end(session):
    _seed_fingerprints(session, WALLET, "all", _MM_ARCHETYPE)
    _seed_fingerprints(session, WALLET, "category:Sports", _MM_ARCHETYPE)

    stats = run_detectors(session, WALLET)
    assert stats.scopes == 2
    assert stats.labels_written == 6  # 2 scopes * 3 detectors

    assert label_scopes(session, WALLET) == ["all", "category:Sports"]

    labels = {r.detector_name: r for r in fetch_labels(session, WALLET, scope="all")}
    assert set(labels) == {"market_making", "inventory_cycling", "value_betting"}
    # market_making is the dominant hypothesis for the MM archetype.
    assert Decimal(labels["market_making"].score) > Decimal(labels["value_betting"].score)
    # Every label carries evidence with all its input features.
    ev = json.loads(labels["market_making"].evidence_json)
    assert set(ev["features"]) == {
        "maker_fill_share", "reward_income_share", "bond_inventory_ratio", "micro_episode_share"
    }


def test_run_detectors_reads_only_latest_fingerprint_version(session):
    _seed_fingerprints(session, WALLET, "all", _VALUE_ARCHETYPE, version=1)
    _seed_fingerprints(session, WALLET, "all", _MM_ARCHETYPE, version=2)
    run_detectors(session, WALLET)
    labels = {r.detector_name: r for r in fetch_labels(session, WALLET, scope="all")}
    # v2 is the MM archetype -> market_making should now dominate.
    assert Decimal(labels["market_making"].score) > Decimal(labels["value_betting"].score)


def test_run_detectors_drop_and_rebuild(session):
    _seed_fingerprints(session, WALLET, "all", _MM_ARCHETYPE)
    run_detectors(session, WALLET)
    first = {r.detector_name: r.score for r in fetch_labels(session, WALLET, scope="all")}
    run_detectors(session, WALLET)
    rows = fetch_labels(session, WALLET, scope="all")
    # No accumulation across runs: still exactly one row per detector.
    assert len(rows) == 3
    second = {r.detector_name: r.score for r in rows}
    assert first == second


def test_no_fingerprints_writes_nothing(session):
    stats = run_detectors(session, "0x9999999999999999999999999999999999999999")
    assert stats.scopes == 0
    assert stats.labels_written == 0
