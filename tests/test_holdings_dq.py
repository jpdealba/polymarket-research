from decimal import Decimal

from sqlalchemy import text

from pmresearch.projections.holdings import rebuild_holdings
from pmresearch.reports.holdings_dq import (
    missing_conditions_report,
    missing_token_metadata_report,
    negative_holdings_report,
    normalize_condition_id,
    undocumented_events_report,
)

from .test_replay_holdings import _seed_conditions, _seed_ledger

DUST = Decimal("0.000001")


def test_normalize_condition_id_bytea_prefix():
    assert normalize_condition_id("\\xdeadbeef") == "0xdeadbeef"
    assert normalize_condition_id("0xdeadbeef") == "0xdeadbeef"


def test_negative_holdings_report_finds_redeem_then_merge_pair(session):
    wallet = "0xneg"
    condition_id = "0xc1"
    tokens = ["tok_a", "tok_b"]
    _seed_conditions(session, {condition_id: tokens})
    session.commit()

    events = [
        {"type": "TRADE", "ts": 100, "condition_id": condition_id, "token_id": "tok_a",
         "delta_shares": "10", "delta_usdc": "-6"},
        {"type": "TRADE", "ts": 100, "condition_id": condition_id, "token_id": "tok_b",
         "delta_shares": "10", "delta_usdc": "-4"},
        # REDEEM zeroes both tokens, then a MERGE at the same ts (ordered
        # after REDEEM by insertion id) subtracts from an already-zero
        # balance, driving both tokens equally negative.
        {"type": "REDEEM", "ts": 200, "condition_id": condition_id,
         "delta_shares": "-10", "delta_usdc": "10"},
        {"type": "MERGE", "ts": 200, "condition_id": condition_id,
         "delta_shares": "-3", "delta_usdc": "3"},
    ]
    _seed_ledger(session, wallet, events)
    rebuild_holdings(session, wallet, dust_epsilon=DUST)

    rows, summary = negative_holdings_report(session, wallet, dust_epsilon=DUST)

    assert summary.negative_token_count == 2
    assert summary.negative_condition_count == 1
    assert summary.paired_equal_magnitude_conditions == 1
    assert summary.cause_event_type_counts == {"MERGE": 2}
    for row in rows:
        assert row.qty == Decimal("-3")
        assert row.cause_event_type == "MERGE"
        assert row.condition_id == condition_id


def test_missing_conditions_report_classifies_bytea_encoding_bug(session):
    condition_id = "0xabc123"
    _seed_conditions(session, {condition_id: ["tokx"]})
    session.commit()

    wallet = "0xmiss"
    events = [
        {"type": "MERGE", "ts": 100, "condition_id": "\\xabc123",
         "delta_shares": "-5", "delta_usdc": "5"},
        {"type": "TRADE", "ts": 200, "condition_id": "0xreallymissing",
         "token_id": "toky", "delta_shares": "5", "delta_usdc": "-2"},
    ]
    _seed_ledger(session, wallet, events)

    rows = {row.condition_id: row for row in missing_conditions_report(session, wallet)}

    assert rows["\\xabc123"].classification == "encoding_bug_bytea_prefix"
    assert rows["\\xabc123"].normalized_match_question is None
    assert rows["0xreallymissing"].classification == "unavailable_upstream"


def test_missing_token_metadata_report_excludes_dust(session):
    wallet = "0xtokmiss"
    session.execute(
        text(
            "INSERT INTO holdings (wallet, token_id, qty, wac_cost, as_of_ts, projection_version) "
            "VALUES (:w, 'tok_no_meta', '1500', '0.72', 100, 1), "
            "(:w, 'tok_dust', '0.0000001', '0', 100, 1)"
        ),
        {"w": wallet},
    )
    session.commit()

    rows = missing_token_metadata_report(session, wallet, dust_epsilon=DUST)

    assert len(rows) == 1
    assert rows[0].token_id == "tok_no_meta"
    assert rows[0].qty == Decimal("1500")


def test_undocumented_events_report_surfaces_conversion(session):
    wallet = "0xconv"
    events = [
        {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "tok_a",
         "delta_shares": "10", "delta_usdc": "-6"},
        {"type": "CONVERSION", "ts": 200, "condition_id": "0xc2",
         "delta_shares": "0", "delta_usdc": "0"},
    ]
    _seed_ledger(session, wallet, events)

    rows = undocumented_events_report(session, wallet)

    assert len(rows) == 1
    assert rows[0].event_type == "CONVERSION"
    assert rows[0].condition_id == "0xc2"
