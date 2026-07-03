from types import SimpleNamespace
from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.projections.holdings import rebuild_holdings
from pmresearch.reconcile.checks import RECONCILE_TOLERANCE, build_reconciliation_result
from pmresearch.reconcile.runner import run_reconciliation
from pmresearch.sources.dataapi import PositionRow, PositionsFetchIncomplete

from .test_replay_holdings import _seed_conditions, _seed_ledger


WALLET = "0xabc"


def _position(token_id, size, *, condition_id="0xc1", price="0.5", value=None):
    size_s = str(size)
    return PositionRow(
        token_id=token_id,
        size=Decimal(size_s),
        avg_price=Decimal("0.5"),
        cur_price=Decimal(price),
        current_value=Decimal(str(value if value is not None else size)),
        condition_id=condition_id,
        title="Question",
        outcome="Yes",
        raw={},
    )


def _insert_holding(session, token_id, qty, *, wallet=WALLET, condition_id="0xc1"):
    if condition_id is not None:
        session.execute(
            text(
                "INSERT OR IGNORE INTO markets "
                "(condition_id, outcomes_json, clob_token_ids_json, structure_type, updated_at) "
                "VALUES (:c, '[]', '[]', 'binary', 'test')"
            ),
            {"c": condition_id},
        )
        session.execute(
            text(
                "INSERT OR IGNORE INTO tokens (token_id, condition_id, outcome_index) "
                "VALUES (:t, :c, 0)"
            ),
            {"t": token_id, "c": condition_id},
        )
    session.execute(
        text(
            "INSERT INTO holdings (wallet, token_id, qty, wac_cost, as_of_ts, projection_version) "
            "VALUES (:w, :t, :q, '0.5', 100, 1)"
        ),
        {"w": wallet, "t": token_id, "q": str(qty)},
    )
    session.commit()


class FakePositionsSource:
    def __init__(self, positions):
        self.positions = tuple(positions)
        self.closed = False

    def fetch_positions(self, raw_store, wallet):
        return SimpleNamespace(positions=self.positions, rows_fetched=len(self.positions))

    def close(self):
        self.closed = True


class IncompletePositionsSource:
    def fetch_positions(self, raw_store, wallet):
        raise PositionsFetchIncomplete("cap reached")

    def close(self):
        pass


def _run(session, settings, positions, *, wallet=WALLET):
    return run_reconciliation(
        session,
        settings,
        wallet=wallet,
        source=FakePositionsSource(positions),
        run_ts=1_000,
    )


def _size_facts(result):
    return [fact for fact in result.facts if fact.check_type == "positions_size"]


def test_exact_local_remote_match_passes_and_trusts_wallet(settings, session):
    _insert_holding(session, "tok1", "10")

    result, trust = _run(session, settings, [_position("tok1", "10")])

    facts = _size_facts(result)
    assert len(facts) == 1
    assert facts[0].reason_code == "exact_match"
    assert facts[0].status == "pass"
    assert trust.status == "trusted"


def test_epsilon_level_dust_passes(settings, session):
    _insert_holding(session, "tok1", "10")

    result, trust = _run(session, settings, [_position("tok1", "10.00005")])

    fact = _size_facts(result)[0]
    assert fact.abs_diff <= RECONCILE_TOLERANCE
    assert fact.reason_code == "dust_only"
    assert fact.status == "pass"
    assert trust.status == "trusted"


def test_remote_present_local_missing_fails(settings, session):
    result, trust = _run(session, settings, [_position("tok_remote", "7")])

    fact = _size_facts(result)[0]
    assert fact.reason_code == "local_missing_remote_present"
    assert fact.status == "fail"
    assert trust.status == "untrusted"


def test_local_present_remote_missing_reported(settings, session):
    _insert_holding(session, "tok_local", "5")

    result, trust = _run(session, settings, [])

    fact = _size_facts(result)[0]
    assert fact.reason_code == "remote_missing_local_present"
    assert fact.status == "fail"
    assert trust.status == "untrusted"


def test_local_negative_holding_classified_without_clamping(settings, session):
    _insert_holding(session, "tok_neg", "-2")

    result, trust = _run(session, settings, [])

    fact = _size_facts(result)[0]
    assert fact.computed == -2
    assert fact.reason_code == "local_negative_holding"
    assert trust.status == "untrusted"


def test_metadata_unavailable_upstream_classified(settings, session):
    _insert_holding(session, "tok_no_meta", "3", condition_id=None)

    result, trust = _run(session, settings, [])

    fact = _size_facts(result)[0]
    assert fact.reason_code == "metadata_unavailable_upstream"
    assert fact.status == "warn"
    assert trust.status == "warn"


def test_merge_condition_scoped_size_gap_classified(settings, session):
    wallet = "0xmergegap"
    condition_id = "0xcmerge"
    _seed_conditions(session, {condition_id: ["tok_a", "tok_b"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": condition_id,
                "token_id": "tok_a",
                "delta_shares": "1",
                "delta_usdc": "-0.5",
            },
            {
                "type": "MERGE",
                "ts": 200,
                "condition_id": condition_id,
                "delta_shares": "-2",
                "delta_usdc": "2",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(session, settings, [], wallet=wallet)

    reasons = {fact.subject: fact.reason_code for fact in _size_facts(result)}
    assert reasons["tok_b"] == "merge_condition_scoped_size_gap"
    assert trust.status == "warn"


def test_same_timestamp_redeem_merge_ordering_ambiguity_classified(settings, session):
    wallet = "0xambig"
    condition_id = "0xcambig"
    _seed_conditions(session, {condition_id: ["tok_a", "tok_b"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": condition_id,
                "token_id": "tok_a",
                "delta_shares": "10",
                "delta_usdc": "-5",
            },
            {
                "type": "REDEEM",
                "ts": 200,
                "condition_id": condition_id,
                "delta_shares": "-10",
                "delta_usdc": "10",
            },
            {
                "type": "MERGE",
                "ts": 200,
                "condition_id": condition_id,
                "delta_shares": "-3",
                "delta_usdc": "3",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(session, settings, [], wallet=wallet)

    assert {
        fact.reason_code for fact in _size_facts(result)
    } == {"same_timestamp_redeem_merge_ordering_ambiguity"}
    assert trust.status == "warn"


def test_source_api_missing_fill_is_known_exception_but_fails_strict_trust(settings, session):
    wallet = "0xoversell"
    _seed_conditions(session, {"0xc1": ["tok_sell"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_sell",
                "delta_shares": "-4",
                "delta_usdc": "2",
            }
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(session, settings, [], wallet=wallet)

    fact = _size_facts(result)[0]
    assert fact.reason_code == "source_api_missing_fill"
    assert fact.status == "fail"
    assert trust.status == "untrusted"
    assert trust.reason == "fail: source_api_missing_fill=1"
    assert result.known_exceptions() == [
        {
            "token_id": "tok_sell",
            "exception_type": "source_api_missing_fill",
            "classification": "upstream_historical_gap",
            "status": "fail",
            "check_type": "positions_size",
            "expected": "0",
            "computed": "-4",
            "abs_diff": "4",
            "source": "dataapi/positions",
            "condition_id": "0xc1",
            "question": None,
            "outcome": None,
            "note": "sell-driven negative holding with no local acquisition or condition-scoped explanation",
        }
    ]


def test_closed_condition_merge_gap_can_explain_sell_triggered_negative(settings, session):
    wallet = "0xmerge-before-sell"
    condition_id = "0xcmergeclosed"
    _seed_conditions(session, {condition_id: ["tok_yes", "tok_no"]})
    session.execute(
        text("UPDATE markets SET closed = 1 WHERE condition_id = :c"),
        {"c": condition_id},
    )
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": condition_id,
                "token_id": "tok_no",
                "delta_shares": "100",
                "delta_usdc": "-50",
            },
            {
                "type": "MERGE",
                "ts": 200,
                "condition_id": condition_id,
                "delta_shares": "-20",
                "delta_usdc": "20",
            },
            {
                "type": "TRADE",
                "ts": 300,
                "condition_id": condition_id,
                "token_id": "tok_no",
                "delta_shares": "-90",
                "delta_usdc": "89",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(session, settings, [], wallet=wallet)

    fact = [fact for fact in _size_facts(result) if fact.subject == "tok_no"][0]
    assert fact.computed == -10
    assert fact.reason_code == "merge_condition_scoped_size_gap"
    assert fact.status == "warn"
    assert trust.status == "warn"


def test_json_output_schema_stable(settings, session, monkeypatch):
    _insert_holding(session, "tok1", "10")
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))

    class SourceFactory:
        def __call__(self):
            return FakePositionsSource([_position("tok1", "10")])

    monkeypatch.setattr("pmresearch.reconcile.runner.DataApiSource", SourceFactory())
    runner = CliRunner()

    result = runner.invoke(main, ["reconcile", "run", "--wallet", WALLET, "--json"])

    assert result.exit_code == 0, result.output
    payload = __import__("json").loads(result.output)
    assert list(payload) == [
        "wallet",
        "run_ts",
        "tolerance",
        "summary",
        "trust",
        "wallet_trust",
        "known_exception_count",
        "known_exceptions",
        "analytics_trust_caveat",
        "top_qty_discrepancies",
        "top_notional_discrepancies",
        "top_remote_positions",
        "negative_holdings_presence",
        "missing_token_metadata_presence",
        "facts",
    ]
    assert payload["summary"]["exact_matches"] == 1
    assert payload["trust"]["status"] == "trusted"
    assert payload["wallet_trust"]["status"] == "trusted"
    assert payload["known_exception_count"] == 0
    assert payload["known_exceptions"] == []
    assert payload["analytics_trust_caveat"] == {
        "trust_status": "trusted",
        "known_exception_count": 0,
        "known_exception_types": [],
    }


def test_incomplete_positions_fetch_persists_failure_and_untrusts(settings, session):
    result, trust = run_reconciliation(
        session,
        settings,
        wallet=WALLET,
        source=IncompletePositionsSource(),
        run_ts=2_000,
    )

    fact = _size_facts(result)[0]
    assert fact.subject == "__positions_fetch__"
    assert fact.status == "fail"
    assert trust.status == "untrusted"
    rows = session.execute(text("SELECT status, reason_code FROM reconciliation_facts")).fetchall()
    assert [(row.status, row.reason_code) for row in rows] == [("fail", "unknown")]
