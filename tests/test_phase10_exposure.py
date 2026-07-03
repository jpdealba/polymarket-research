"""Phase 10 — Exposure Engine tests.

Golden math for binary directional+bond, negRisk 3-sibling netting,
unclassified fallback, MERGE/bond interaction, unknown-structure dispatch,
determinism, and a grep guard that no outcome-label ("Yes"/"No") string logic
lives in pmresearch/exposure/.
"""

from datetime import date, datetime, time, timezone
from decimal import Decimal
from pathlib import Path
import json

from sqlalchemy import text

from pmresearch.exposure import binary, negrisk, unclassified
from pmresearch.exposure.descriptors import (
    STRUCTURE_BINARY,
    STRUCTURE_NEG_RISK_EVENT_MEMBER,
    STRUCTURE_UNCLASSIFIED,
)
from pmresearch.exposure.engine import market_exposure
from pmresearch.projections.exposures import (
    fetch_event_exposures,
    fetch_exposures,
    rebuild_exposures,
)

DUST = Decimal("0.000001")


def _ts(day: date) -> int:
    return int(datetime.combine(day, time(12, 0, 0, tzinfo=timezone.utc)).timestamp())


def _seed_event(session, event_id, neg_risk=1):
    session.execute(
        text(
            "INSERT INTO pm_events (event_id, title, slug, neg_risk, tags_json) "
            "VALUES (:event_id, :title, :slug, :neg_risk, '[]')"
        ),
        {"event_id": event_id, "title": event_id, "slug": event_id, "neg_risk": neg_risk},
    )
    session.commit()


def _seed_market(session, condition_id, token_ids, structure_type, *, event_id=None, labels=None):
    labels = labels or [f"{condition_id}_out{i}" for i in range(len(token_ids))]
    session.execute(
        text(
            "INSERT INTO markets "
            "(condition_id, question, category, event_id, outcomes_json, "
            "clob_token_ids_json, closed, structure_type, updated_at) "
            "VALUES (:condition_id, :question, 'Sports', :event_id, :outcomes, "
            ":tokens, 0, :structure_type, 'test')"
        ),
        {
            "condition_id": condition_id,
            "question": f"Q {condition_id}",
            "event_id": event_id,
            "outcomes": json.dumps(labels),
            "tokens": json.dumps(token_ids),
            "structure_type": structure_type,
        },
    )
    for index, token_id in enumerate(token_ids):
        session.execute(
            text(
                "INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
                "VALUES (:token_id, :condition_id, :outcome_index, :outcome_label)"
            ),
            {
                "token_id": token_id,
                "condition_id": condition_id,
                "outcome_index": index,
                "outcome_label": labels[index],
            },
        )
    session.commit()


def _raw_ref(session, wallet):
    return session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', :params, 'test', 200, 'none', :hash, 0) "
            "RETURNING id"
        ),
        {"params": f'{{"wallet":"{wallet}"}}', "hash": f"phase10-{wallet}"},
    ).scalar()


def _seed_ledger(session, wallet, events):
    raw_ref = _raw_ref(session, wallet)
    for index, event in enumerate(events):
        session.execute(
            text(
                "INSERT INTO wallet_events "
                "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
                "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
                "dedupe_key, ingested_at) "
                "VALUES (:wallet, :event_type, :ts, :tx_hash, :condition_id, :token_id, "
                "NULL, :delta_shares, :delta_usdc, '0', '0', 'test', 0, "
                ":raw_ref, :dedupe_key, 'test')"
            ),
            {
                "wallet": wallet,
                "event_type": event["type"],
                "ts": event["ts"],
                "tx_hash": event.get("tx_hash", f"0x{index}"),
                "condition_id": event.get("condition_id"),
                "token_id": event.get("token_id"),
                "delta_shares": event.get("delta_shares", "0"),
                "delta_usdc": event.get("delta_usdc", "0"),
                "raw_ref": raw_ref,
                "dedupe_key": f"phase10-{wallet}-{index}",
            },
        )
    session.commit()


# --------------------------------------------------------------------------
# Pure-function golden math
# --------------------------------------------------------------------------

def test_binary_decompose_golden():
    directional, bond = binary.decompose(Decimal("100"), Decimal("60"))
    assert directional == Decimal("40")
    assert bond == Decimal("60")


def test_engine_binary_golden():
    me = market_exposure(
        STRUCTURE_BINARY,
        ["tok0", "tok1"],
        {"tok0": Decimal("100"), "tok1": Decimal("60")},
    )
    assert me.structure_type == STRUCTURE_BINARY
    assert me.directional == Decimal("40")
    assert me.bond == Decimal("60")
    assert me.raw_vector is None
    assert not me.unknown_structure


def test_negrisk_three_sibling_netting_hand_computed():
    # cA: 50 idx0 / 10 idx1 -> +40 ; cB: 30/30 -> 0 ; cC: 5/20 -> -15
    ev = negrisk.event_exposure(
        "ev",
        [
            ("cA", Decimal("50"), Decimal("10")),
            ("cB", Decimal("30"), Decimal("30")),
            ("cC", Decimal("5"), Decimal("20")),
        ],
    )
    assert ev.exposure_vector == {"cA": "40", "cB": "0", "cC": "-15"}
    assert ev.net_after_exclusivity == Decimal("25")


def test_unclassified_raw_vector_no_decomposition():
    me = market_exposure(
        STRUCTURE_UNCLASSIFIED,
        ["a", "b", "c"],
        {"a": Decimal("3"), "b": Decimal("0"), "c": Decimal("7")},
    )
    assert me.structure_type == STRUCTURE_UNCLASSIFIED
    assert me.directional is None
    assert me.bond is None
    assert me.raw_vector == {"a": "3", "b": "0", "c": "7"}
    assert not me.unknown_structure


def test_dispatch_never_guesses_unknown_structure():
    me = market_exposure(
        "some-future-structure",
        ["tok0", "tok1"],
        {"tok0": Decimal("5"), "tok1": Decimal("2")},
    )
    # Routed to the unclassified path, flagged, NOT decomposed.
    assert me.structure_type == STRUCTURE_UNCLASSIFIED
    assert me.directional is None
    assert me.bond is None
    assert me.unknown_structure


# --------------------------------------------------------------------------
# Projection: end-to-end replay
# --------------------------------------------------------------------------

def test_projection_binary_golden(session):
    wallet = "0xbin"
    _seed_market(session, "cond", ["tok0", "tok1"], STRUCTURE_BINARY)
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cond", "token_id": "tok0", "delta_shares": "100"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cond", "token_id": "tok1", "delta_shares": "60"},
        ],
    )
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    rows = fetch_exposures(session, wallet, condition_id="cond")
    assert rows[-1].directional == Decimal("40")
    assert rows[-1].bond == Decimal("60")
    assert rows[-1].structure_type == STRUCTURE_BINARY


def test_projection_team_name_labels_still_decompose(session):
    """Outcome labels are team names, not Yes/No — decomposition is label-agnostic."""
    wallet = "0xteam"
    _seed_market(
        session, "match", ["mex", "kor"], STRUCTURE_BINARY, labels=["Mexico", "Korea"]
    )
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day), "condition_id": "match", "token_id": "mex", "delta_shares": "100"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "match", "token_id": "kor", "delta_shares": "60"},
        ],
    )
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    rows = fetch_exposures(session, wallet, condition_id="match")
    assert rows[-1].directional == Decimal("40")
    assert rows[-1].bond == Decimal("60")


def test_projection_negrisk_event_vector(session):
    wallet = "0xnr"
    _seed_event(session, "ev1")
    _seed_market(session, "cA", ["a0", "a1"], STRUCTURE_NEG_RISK_EVENT_MEMBER, event_id="ev1")
    _seed_market(session, "cB", ["b0", "b1"], STRUCTURE_NEG_RISK_EVENT_MEMBER, event_id="ev1")
    _seed_market(session, "cC", ["c0", "c1"], STRUCTURE_NEG_RISK_EVENT_MEMBER, event_id="ev1")
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cA", "token_id": "a0", "delta_shares": "50"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cA", "token_id": "a1", "delta_shares": "10"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cB", "token_id": "b0", "delta_shares": "30"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cB", "token_id": "b1", "delta_shares": "30"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cC", "token_id": "c0", "delta_shares": "5"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cC", "token_id": "c1", "delta_shares": "20"},
        ],
    )
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    events = fetch_event_exposures(session, wallet, event_id="ev1")
    assert len(events) == 1
    assert events[0].exposure_vector == {"cA": "40", "cB": "0", "cC": "-15"}
    assert events[0].net_after_exclusivity == Decimal("25")


def test_projection_unclassified_market(session):
    wallet = "0xun"
    _seed_market(session, "multi", ["t0", "t1", "t2"], STRUCTURE_UNCLASSIFIED)
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day), "condition_id": "multi", "token_id": "t0", "delta_shares": "3"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "multi", "token_id": "t2", "delta_shares": "7"},
        ],
    )
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    rows = fetch_exposures(session, wallet, condition_id="multi")
    assert rows[-1].structure_type == STRUCTURE_UNCLASSIFIED
    assert rows[-1].directional is None
    assert rows[-1].bond is None


def test_projection_bond_drops_after_merge(session):
    wallet = "0xmerge"
    _seed_market(session, "cond", ["tok0", "tok1"], STRUCTURE_BINARY)
    day1 = date(2026, 1, 1)
    day2 = date(2026, 1, 2)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day1), "condition_id": "cond", "token_id": "tok0", "delta_shares": "100"},
            {"type": "TRADE", "ts": _ts(day1), "condition_id": "cond", "token_id": "tok1", "delta_shares": "100"},
            # MERGE burns 40 of each: delta_shares is the signed (negative) removal.
            {"type": "MERGE", "ts": _ts(day2), "condition_id": "cond", "delta_shares": "-40"},
        ],
    )
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day2)
    rows = fetch_exposures(session, wallet, condition_id="cond")
    by_date = {row.date: row for row in rows}
    assert by_date["2026-01-01"].bond == Decimal("100")
    assert by_date["2026-01-02"].bond == Decimal("60")
    # Directional stays 0 (symmetric position through the merge).
    assert by_date["2026-01-02"].directional == Decimal("0")


def test_projection_unknown_structure_counted_not_crash(session):
    wallet = "0xunknown"
    _seed_market(session, "weird", ["w0", "w1"], "some-future-structure")
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [{"type": "TRADE", "ts": _ts(day), "condition_id": "weird", "token_id": "w0", "delta_shares": "9"}],
    )
    stats = rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    assert stats.unknown_structure_warnings >= 1
    rows = fetch_exposures(session, wallet, condition_id="weird")
    assert rows[-1].structure_type == STRUCTURE_UNCLASSIFIED
    assert rows[-1].directional is None


def test_projection_determinism_byte_identical(session):
    wallet = "0xdet"
    _seed_event(session, "ev1")
    _seed_market(session, "cA", ["a0", "a1"], STRUCTURE_NEG_RISK_EVENT_MEMBER, event_id="ev1")
    _seed_market(session, "cB", ["b0", "b1"], STRUCTURE_NEG_RISK_EVENT_MEMBER, event_id="ev1")
    day = date(2026, 1, 1)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cA", "token_id": "a0", "delta_shares": "50"},
            {"type": "TRADE", "ts": _ts(day), "condition_id": "cB", "token_id": "b1", "delta_shares": "20"},
        ],
    )

    def snapshot():
        cond = session.execute(
            text(
                "SELECT wallet, condition_id, date, directional, bond, structure_type, "
                "event_id, projection_version FROM exposures_daily WHERE wallet = :w "
                "ORDER BY condition_id, date"
            ),
            {"w": wallet},
        ).fetchall()
        ev = session.execute(
            text(
                "SELECT wallet, event_id, date, exposure_vector_json, "
                "net_after_exclusivity, projection_version FROM event_exposures_daily "
                "WHERE wallet = :w ORDER BY event_id, date"
            ),
            {"w": wallet},
        ).fetchall()
        return [tuple(r) for r in cond], [tuple(r) for r in ev]

    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    first = snapshot()
    rebuild_exposures(session, wallet, dust_epsilon=DUST, through_date=day)
    second = snapshot()
    assert first == second


# --------------------------------------------------------------------------
# Acceptance criterion: zero hardcoded outcome-label logic in the module.
# --------------------------------------------------------------------------

def test_no_hardcoded_outcome_labels_in_exposure_module():
    """Grep-able acceptance criterion: no outcome-label string *comparisons*.

    Docstrings legitimately reference "Yes"/"No" to document that they must
    never be used; the ban is on executable logic. We strip comments and
    docstrings, then grep the remaining code for the banned tokens.
    """
    import io
    import tokenize

    banned = ('"Yes"', "'Yes'", '"No"', "'No'")
    module_dir = Path(binary.__file__).parent
    offenders = []
    for path in module_dir.glob("*.py"):
        source = path.read_text(encoding="utf-8")
        code_pieces = []
        prev_type = tokenize.INDENT
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                continue
            # A STRING token right after NEWLINE/INDENT/DEDENT is a docstring.
            if tok.type == tokenize.STRING and prev_type in (
                tokenize.NEWLINE, tokenize.NL, tokenize.INDENT, tokenize.DEDENT, tokenize.ENCODING,
            ):
                prev_type = tok.type
                continue
            code_pieces.append(tok.string)
            if tok.type not in (tokenize.NL, tokenize.NEWLINE):
                prev_type = tok.type
        code = " ".join(code_pieces)
        if any(token in code for token in banned):
            offenders.append(path.name)
    assert offenders == [], f"outcome-label string logic found in: {offenders}"
