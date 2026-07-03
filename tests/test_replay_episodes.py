import json
from decimal import Decimal

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.projections.episodes import (
    EPISODES_PROJECTION_VERSION,
    episode_stats,
    rebuild_episodes,
)
from pmresearch.projections.holdings import rebuild_holdings

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
            "VALUES ('test', 'activity', :params, 'test', 200, 'none', :h, 0) "
            "RETURNING id"
        ),
        {"params": f'{{"wallet":"{wallet}"}}', "h": f"episodes-{wallet}"},
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
                "dedupe_key": f"episodes-{wallet}-{i}",
            },
        )
    session.commit()


def _episode_rows(session, wallet):
    return session.execute(
        text(
            "SELECT token_id, open_ts, close_ts, close_reason, peak_qty, num_adds, "
            "num_partial_exits, wac_entry, realized_pnl, events_consumed, "
            "projection_version FROM episodes WHERE wallet = :w ORDER BY open_ts, id"
        ),
        {"w": wallet},
    ).fetchall()


def _seed_base(session):
    _seed_conditions(session, {"0xc1": ["101", "102"]})
    session.commit()


def test_single_round_trip_realized_pnl(session):
    _seed_base(session)
    wallet = "0xe1"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 200, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-10", "delta_usdc": "7"},
        ],
    )

    stats = rebuild_episodes(session, wallet, dust_epsilon=DUST)
    rows = _episode_rows(session, wallet)

    assert stats.episodes_written == 1
    assert rows[0].close_reason == "flat"
    assert rows[0].open_ts == 100
    assert rows[0].close_ts == 200
    assert Decimal(rows[0].wac_entry) == Decimal("0.5")
    assert Decimal(rows[0].realized_pnl) == Decimal("2")
    assert rows[0].projection_version == EPISODES_PROJECTION_VERSION


def test_scale_in_partial_exit_hand_computed_wac(session):
    _seed_base(session)
    wallet = "0xe2"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 200, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-7"},
            {"type": "TRADE", "ts": 300, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-5", "delta_usdc": "4"},
            {"type": "TRADE", "ts": 400, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-15", "delta_usdc": "12"},
        ],
    )

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    row = _episode_rows(session, wallet)[0]

    assert Decimal(row.wac_entry) == Decimal("0.6")
    assert row.num_adds == 1
    assert row.num_partial_exits == 1
    assert Decimal(row.realized_pnl) == Decimal("4")
    assert json.loads(row.events_consumed) == [1, 2, 3, 4]


def test_flat_crossing_creates_two_episodes_without_debounce(session):
    _seed_base(session)
    wallet = "0xe3"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-10", "delta_usdc": "6"},
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "5", "delta_usdc": "-3"},
        ],
    )

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    rows = _episode_rows(session, wallet)

    assert len(rows) == 2
    assert rows[0].close_reason == "flat"
    assert rows[0].open_ts == rows[0].close_ts == 100
    assert rows[1].close_reason == "open"
    assert rows[1].open_ts == 100


def test_redeem_closes_by_resolution_with_phase6_understated_pnl(session):
    _seed_base(session)
    wallet = "0xe4"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "condition_id": "0xc1", "delta_shares": "-10", "delta_usdc": "0"},
        ],
    )

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    row = _episode_rows(session, wallet)[0]

    assert row.close_reason == "resolution"
    assert row.close_ts == 200
    assert Decimal(row.realized_pnl) == Decimal("-4")


def test_open_episode_at_stream_end(session):
    _seed_base(session)
    wallet = "0xe5"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
        ],
    )

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    row = _episode_rows(session, wallet)[0]

    assert row.close_reason == "open"
    assert row.close_ts is None
    assert Decimal(row.wac_entry) == Decimal("0.5")


def test_dust_epsilon_closes_episode(session):
    _seed_base(session)
    wallet = "0xe6"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 200, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-9.9999995", "delta_usdc": "6"},
        ],
    )

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    row = _episode_rows(session, wallet)[0]

    assert row.close_reason == "flat"
    assert Decimal(row.realized_pnl) == Decimal("1")


def test_cross_projection_consistency_against_holdings(session):
    _seed_base(session)
    wallet = "0xe7"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 200, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-7"},
            {"type": "TRADE", "ts": 300, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-5", "delta_usdc": "4"},
            {"type": "SPLIT", "ts": 400, "condition_id": "0xc1", "delta_shares": "6", "delta_usdc": "-6"},
            {"type": "MERGE", "ts": 500, "condition_id": "0xc1", "delta_shares": "-2", "delta_usdc": "2"},
        ],
    )

    rebuild_holdings(session, wallet, dust_epsilon=DUST)
    rebuild_episodes(session, wallet, dust_epsilon=DUST)

    holdings = {
        row.token_id: row
        for row in session.execute(
            text("SELECT token_id, qty, wac_cost FROM holdings WHERE wallet = :w"),
            {"w": wallet},
        )
    }
    open_episodes = session.execute(
        text(
            "SELECT token_id, wac_entry FROM episodes "
            "WHERE wallet = :w AND close_reason = 'open'"
        ),
        {"w": wallet},
    ).fetchall()

    assert {row.token_id for row in open_episodes} == {
        token_id for token_id, row in holdings.items() if abs(Decimal(row.qty)) > DUST
    }
    for row in open_episodes:
        assert Decimal(row.wac_entry) == Decimal(holdings[row.token_id].wac_cost)


def test_episode_stats_and_cli_smoke(session, settings, monkeypatch):
    from pmresearch.cli import main

    _seed_base(session)
    wallet = "0xe8"
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "condition_id": "0xc1", "token_id": "101", "delta_shares": "10", "delta_usdc": "-5"},
            {"type": "TRADE", "ts": 120, "condition_id": "0xc1", "token_id": "101", "delta_shares": "-10", "delta_usdc": "6"},
        ],
    )
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    result = runner.invoke(main, ["replay", "episodes", "--wallet", wallet])
    assert result.exit_code == 0, result.output
    assert "1 episodes" in result.output
    assert "understated until Phase 8" in result.output

    result = runner.invoke(main, ["episodes", "stats", "--wallet", wallet])
    assert result.exit_code == 0, result.output
    assert "micro_episodes=1" in result.output

    stats = episode_stats(session, wallet)
    assert stats.count == 1
    assert stats.micro_episode_count == 1
