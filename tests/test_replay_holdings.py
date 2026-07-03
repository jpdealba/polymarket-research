import json
import logging
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import text

from pmresearch.projections.holdings import (
    HOLDINGS_PROJECTION_VERSION,
    fetch_holdings,
    rebuild_holdings,
)

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "holdings_golden.json").read_text(encoding="utf-8")
)
SCENARIOS = {scenario["name"]: scenario for scenario in FIXTURE["scenarios"]}
DUST = Decimal("0.000001")


def _seed_conditions(session, conditions):
    for condition_id, token_ids in conditions.items():
        session.execute(
            text(
                "INSERT INTO markets (condition_id, outcomes_json, clob_token_ids_json, "
                "structure_type, updated_at) VALUES (:c, '[]', '[]', 'binary', 'test')"
            ),
            {"c": condition_id},
        )
        for index, token_id in enumerate(token_ids):
            session.execute(
                text(
                    "INSERT INTO tokens (token_id, condition_id, outcome_index) "
                    "VALUES (:t, :c, :i)"
                ),
                {"t": token_id, "c": condition_id, "i": index},
            )


def _seed_ledger(session, wallet, events):
    raw_ref = session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', '{}', 'test', 200, 'none', :h, 0) RETURNING id"
        ),
        {"h": f"golden-{wallet}"},
    ).scalar()
    for i, event in enumerate(events):
        session.execute(
            text(
                "INSERT INTO wallet_events "
                "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
                "delta_shares, delta_usdc, price, usdc_size, source, is_derived, "
                "raw_ref, dedupe_key, ingested_at) "
                "VALUES (:wallet, :event_type, :ts, :tx_hash, :condition_id, :token_id, "
                "NULL, :delta_shares, :delta_usdc, '0', '0', 'test', 0, :raw_ref, "
                ":dedupe_key, 'test')"
            ),
            {
                "wallet": wallet,
                "event_type": event["type"],
                "ts": event["ts"],
                "tx_hash": f"0xtx{i}",
                "condition_id": event.get("condition_id"),
                "token_id": event.get("token_id"),
                "delta_shares": event["delta_shares"],
                "delta_usdc": event["delta_usdc"],
                "raw_ref": raw_ref,
                "dedupe_key": f"golden-{wallet}-{i}",
            },
        )
    session.commit()


def _holdings_rows(session, wallet):
    return session.execute(
        text(
            "SELECT token_id, qty, wac_cost, as_of_ts, projection_version "
            "FROM holdings WHERE wallet = :w ORDER BY token_id"
        ),
        {"w": wallet},
    ).fetchall()


def _run_scenario(session, name):
    scenario = SCENARIOS[name]
    _seed_ledger(session, scenario["wallet"], scenario["events"])
    return scenario, rebuild_holdings(session, scenario["wallet"], dust_epsilon=DUST)


@pytest.fixture
def golden(session):
    _seed_conditions(session, FIXTURE["conditions"])
    session.commit()
    return session


@pytest.mark.parametrize("name", sorted(SCENARIOS))
def test_golden_scenarios_hand_computed(golden, name):
    scenario, stats = _run_scenario(golden, name)

    rows = {row.token_id: row for row in _holdings_rows(golden, scenario["wallet"])}
    assert set(rows) == set(scenario["expected"])
    for token_id, expected in scenario["expected"].items():
        row = rows[token_id]
        assert Decimal(row.qty) == Decimal(expected["qty"]), (name, token_id)
        assert Decimal(row.wac_cost) == Decimal(expected["wac"]), (name, token_id)
        assert row.as_of_ts == expected["as_of_ts"], (name, token_id)
        assert row.projection_version == HOLDINGS_PROJECTION_VERSION

    if "expected_negative_qty_events" in scenario:
        assert stats.negative_qty_events == scenario["expected_negative_qty_events"]
    if "expected_negative_qty_tokens" in scenario:
        assert stats.negative_qty_tokens == scenario["expected_negative_qty_tokens"]


def test_negative_holdings_logged_not_clamped(golden, caplog):
    with caplog.at_level(logging.WARNING):
        scenario, stats = _run_scenario(golden, "negative_not_clamped")

    assert stats.negative_qty_tokens == 1
    assert any("went negative" in record.message for record in caplog.records)
    row = _holdings_rows(golden, scenario["wallet"])[0]
    assert Decimal(row.qty) == Decimal("-10")  # preserved, not clamped to zero


def test_rebuild_is_deterministic(golden):
    scenario, _ = _run_scenario(golden, "merge_reduces_both")
    first = _holdings_rows(golden, scenario["wallet"])
    rebuild_holdings(golden, scenario["wallet"], dust_epsilon=DUST)
    second = _holdings_rows(golden, scenario["wallet"])
    assert first == second


def test_unmapped_condition_events_skipped_and_counted(golden):
    wallet = "0xwx"
    events = [
        {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101",
         "delta_shares": "10", "delta_usdc": "-5"},
        {"type": "MERGE", "ts": 200, "condition_id": "0xunknown",
         "delta_shares": "-5", "delta_usdc": "5"},
    ]
    _seed_ledger(golden, wallet, events)
    stats = rebuild_holdings(golden, wallet, dust_epsilon=DUST)

    assert stats.unmapped_condition_events == 1
    assert stats.unmapped_condition_ids == 1
    row = _holdings_rows(golden, wallet)[0]
    assert Decimal(row.qty) == Decimal("10")  # the unmapped MERGE touched nothing


def test_nonzero_filter_excludes_dust(golden):
    scenario, _ = _run_scenario(golden, "dust_flat")
    all_rows = fetch_holdings(golden, scenario["wallet"])
    nonzero = fetch_holdings(golden, scenario["wallet"], nonzero=True, dust_epsilon=DUST)
    assert len(all_rows) == 1
    assert nonzero == []


def test_cli_replay_and_show_smoke(golden, settings, monkeypatch):
    from click.testing import CliRunner

    from pmresearch.cli import main

    scenario, _ = _run_scenario(golden, "buy_sell_wac")
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    result = runner.invoke(main, ["replay", "holdings", "--wallet", scenario["wallet"]])
    assert result.exit_code == 0, result.output
    assert "1 nonzero" in result.output

    result = runner.invoke(main, ["holdings", "show", "--wallet", scenario["wallet"], "--nonzero"])
    assert result.exit_code == 0, result.output
    assert "150" in result.output
