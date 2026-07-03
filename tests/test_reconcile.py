from types import SimpleNamespace
from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.projections.episodes import rebuild_episodes
from pmresearch.projections.holdings import rebuild_holdings
from pmresearch.reconcile.checks import (
    REALIZED_PNL_TOLERANCE,
    RECONCILE_TOLERANCE,
    WAC_TOLERANCE,
    build_reconciliation_result,
)
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


def _oracle_position(
    token_id,
    size,
    *,
    avg_price,
    realized_pnl="0",
    condition_id="0xc1",
    price="0.5",
    value=None,
):
    return PositionRow(
        token_id=token_id,
        size=Decimal(str(size)),
        avg_price=Decimal(str(avg_price)),
        cur_price=Decimal(str(price)),
        current_value=Decimal(str(value if value is not None else size)),
        condition_id=condition_id,
        title="Question",
        outcome="Yes",
        raw={"avgPrice": str(avg_price), "realizedPnl": str(realized_pnl)},
        realized_pnl=Decimal(str(realized_pnl)),
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


def _facts(result, check_type):
    return [fact for fact in result.facts if fact.check_type == check_type]


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


def test_wac_avgprice_matches_current_open_episode_not_lifetime(settings, session):
    wallet = "0xwacopen"
    _seed_conditions(session, {"0xc1": ["tok_wac", "tok_other"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_wac",
                "delta_shares": "10",
                "delta_usdc": "-2",
            },
            {
                "type": "TRADE",
                "ts": 200,
                "condition_id": "0xc1",
                "token_id": "tok_wac",
                "delta_shares": "-10",
                "delta_usdc": "3",
            },
            {
                "type": "TRADE",
                "ts": 300,
                "condition_id": "0xc1",
                "token_id": "tok_wac",
                "delta_shares": "5",
                "delta_usdc": "-4",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)
    rebuild_episodes(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(
        session,
        settings,
        [_oracle_position("tok_wac", "5", avg_price="0.8", realized_pnl="0")],
        wallet=wallet,
    )

    wac_fact = _facts(result, "positions_wac_avg_price")[0]
    assert wac_fact.computed == Decimal("0.8")
    assert wac_fact.expected == Decimal("0.8")
    assert wac_fact.status == "pass"
    assert wac_fact.notes["comparison_scope"] == "current_open_episode"
    assert wac_fact.notes["open_episode_ts"] == 300
    assert trust.status == "trusted"


def test_wac_avgprice_drift_fails_without_widening_tolerance(settings, session):
    wallet = "0xwacbug"
    _seed_conditions(session, {"0xc1": ["tok_bug", "tok_other"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_bug",
                "delta_shares": "10",
                "delta_usdc": "-5",
            }
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)
    rebuild_episodes(session, wallet, dust_epsilon=settings.dust_epsilon)
    session.execute(
        text("UPDATE episodes SET wac_entry = '0.7' WHERE wallet = :w AND token_id = 'tok_bug'"),
        {"w": wallet},
    )
    session.commit()

    result, trust = _run(
        session,
        settings,
        [_oracle_position("tok_bug", "10", avg_price="0.5", realized_pnl="0")],
        wallet=wallet,
    )

    wac_fact = _facts(result, "positions_wac_avg_price")[0]
    assert wac_fact.abs_diff == Decimal("0.2")
    assert wac_fact.tolerance == WAC_TOLERANCE
    assert wac_fact.reason_code == "wac_drift"
    assert wac_fact.status == "fail"
    assert trust.status == "untrusted"


def test_realized_pnl_within_band_passes(settings, session):
    wallet = "0xrealizedband"
    _seed_conditions(session, {"0xc1": ["tok_pnl", "tok_other"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_pnl",
                "delta_shares": "10",
                "delta_usdc": "-5",
            },
            {
                "type": "TRADE",
                "ts": 200,
                "condition_id": "0xc1",
                "token_id": "tok_pnl",
                "delta_shares": "-4",
                "delta_usdc": "3.2",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)
    rebuild_episodes(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(
        session,
        settings,
        [_oracle_position("tok_pnl", "6", avg_price="0.5", realized_pnl="1.205")],
        wallet=wallet,
    )

    fact = _facts(result, "positions_realized_pnl")[0]
    assert fact.computed == Decimal("1.2")
    assert fact.abs_diff == Decimal("0.005")
    assert fact.tolerance == REALIZED_PNL_TOLERANCE
    assert fact.reason_code == "within_realized_pnl_band"
    assert fact.status == "pass"
    assert trust.status == "trusted"


def test_realized_pnl_outside_band_warns_with_categorized_note(settings, session):
    wallet = "0xrealizeddrift"
    _seed_conditions(session, {"0xc1": ["tok_pnl", "tok_other"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_pnl",
                "delta_shares": "10",
                "delta_usdc": "-5",
            },
            {
                "type": "TRADE",
                "ts": 200,
                "condition_id": "0xc1",
                "token_id": "tok_pnl",
                "delta_shares": "-4",
                "delta_usdc": "3.2",
            },
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)
    rebuild_episodes(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(
        session,
        settings,
        [_oracle_position("tok_pnl", "6", avg_price="0.5", realized_pnl="1.5")],
        wallet=wallet,
    )

    fact = _facts(result, "positions_realized_pnl")[0]
    assert fact.abs_diff == Decimal("0.3")
    assert fact.status == "warn"
    assert fact.reason_code == "realized_pnl_trade_accounting_drift"
    assert fact.notes["classification"] == "trade_accounting_or_oracle_semantics_drift"
    assert trust.status == "warn"


def test_missing_oracle_avgprice_and_realized_pnl_are_skipped(settings, session):
    wallet = "0xoracleskip"
    _seed_conditions(session, {"0xc1": ["tok_skip", "tok_other"]})
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "condition_id": "0xc1",
                "token_id": "tok_skip",
                "delta_shares": "10",
                "delta_usdc": "-5",
            }
        ],
    )
    rebuild_holdings(session, wallet, dust_epsilon=settings.dust_epsilon)
    rebuild_episodes(session, wallet, dust_epsilon=settings.dust_epsilon)

    result, trust = _run(
        session,
        settings,
        [_position("tok_skip", "10")],
        wallet=wallet,
    )

    wac_fact = _facts(result, "positions_wac_avg_price")[0]
    realized_fact = _facts(result, "positions_realized_pnl")[0]
    assert wac_fact.status == "skip"
    assert wac_fact.reason_code == "oracle_field_missing"
    assert realized_fact.status == "skip"
    assert realized_fact.reason_code == "oracle_field_missing"
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
        "check_status",
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


def test_status_labels_deprecated_avg_price_info(settings, session, monkeypatch):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    facts = [
        {
            "wallet": WALLET,
            "ts": 123,
            "check_type": "positions_size",
            "subject": "tok1",
            "expected": "10",
            "computed": "10",
            "abs_diff": "0",
            "pct_diff": "0",
            "tolerance": "0.0001",
            "status": "pass",
            "source": "test",
            "reason_code": "exact_match",
            "notes": '{"local_present":true,"remote_present":true,"local_qty":"10"}',
        },
        {
            "wallet": WALLET,
            "ts": 123,
            "check_type": "positions_avg_price_info",
            "subject": "tok1",
            "expected": "0.5",
            "computed": "0.5",
            "abs_diff": "0",
            "pct_diff": "0",
            "tolerance": "0.0001",
            "status": "pass",
            "source": "test",
            "reason_code": "exact_match",
            "notes": "{}",
        },
    ]
    session.execute(
        text(
            "INSERT INTO reconciliation_facts "
            "(wallet, ts, check_type, subject, expected, computed, abs_diff, pct_diff, "
            "tolerance, status, source, reason_code, notes) "
            "VALUES (:wallet, :ts, :check_type, :subject, :expected, :computed, :abs_diff, "
            ":pct_diff, :tolerance, :status, :source, :reason_code, :notes)"
        ),
        facts,
    )
    session.commit()

    result = CliRunner().invoke(main, ["reconcile", "status", "--wallet", WALLET])

    assert result.exit_code == 0, result.output
    assert "positions_avg_price_info (deprecated; use positions_wac_avg_price)" in result.output
    assert "positions_avg_price_info:" not in result.output


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
