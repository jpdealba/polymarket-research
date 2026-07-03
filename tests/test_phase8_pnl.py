from decimal import Decimal
import json

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.ingest.derived import derive_redeem_payouts
from pmresearch.projections.episodes import rebuild_episodes
from pmresearch.projections.pnl_decomposition import (
    fetch_pnl_decomposition,
    rebuild_pnl_decomposition,
)

DUST = Decimal("0.000001")


def _raw_ref(session, wallet):
    return session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', :params, 'test', 200, 'none', :hash, 0) "
            "RETURNING id"
        ),
        {"params": f'{{"wallet":"{wallet}"}}', "hash": f"phase8-{wallet}"},
    ).scalar()


def _seed_market(
    session,
    condition_id="0xc8",
    tokens=("win", "lose"),
    prices=("1", "0"),
    category="Sports",
):
    session.execute(
        text(
            "INSERT INTO markets "
            "(condition_id, question, category, outcomes_json, clob_token_ids_json, "
            "closed, resolution_prices_json, structure_type, updated_at) "
            "VALUES (:condition_id, 'Question', :category, :outcomes, :tokens, "
            "1, :prices, 'binary', 'test')"
        ),
        {
            "condition_id": condition_id,
            "category": category,
            "outcomes": json.dumps(["Win", "Lose"]),
            "tokens": json.dumps(list(tokens)),
            "prices": json.dumps({token: price for token, price in zip(tokens, prices)}),
        },
    )
    for index, token_id in enumerate(tokens):
        session.execute(
            text(
                "INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
                "VALUES (:token_id, :condition_id, :outcome_index, :outcome_label)"
            ),
            {
                "token_id": token_id,
                "condition_id": condition_id,
                "outcome_index": index,
                "outcome_label": token_id,
            },
        )
    session.commit()


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
                "NULL, :delta_shares, :delta_usdc, '0', :usdc_size, 'test', 0, "
                ":raw_ref, :dedupe_key, 'test')"
            ),
            {
                "wallet": wallet,
                "event_type": event["type"],
                "ts": event["ts"],
                "tx_hash": event.get("tx_hash", f"0x{index}"),
                "condition_id": event.get("condition_id", "0xc8"),
                "token_id": event.get("token_id"),
                "delta_shares": event.get("delta_shares", "0"),
                "delta_usdc": event.get("delta_usdc", "0"),
                "usdc_size": event.get("usdc_size", event.get("delta_usdc", "0")),
                "raw_ref": raw_ref,
                "dedupe_key": f"phase8-{wallet}-{index}",
            },
        )
    session.commit()


def _episode_realized(session, wallet, token_id):
    return Decimal(
        session.execute(
            text("SELECT realized_pnl FROM episodes WHERE wallet = :wallet AND token_id = :token_id"),
            {"wallet": wallet, "token_id": token_id},
        ).scalar_one()
    )


def test_derived_redeem_payout_is_idempotent_and_marked(session):
    wallet = "0xderive"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )

    first = derive_redeem_payouts(session, wallet, dust_epsilon=DUST)
    second = derive_redeem_payouts(session, wallet, dust_epsilon=DUST)

    assert first.derived_events_inserted == 1
    assert second.derived_events_inserted == 0
    row = session.execute(
        text(
            "SELECT event_type, delta_usdc, usdc_size, is_derived, source "
            "FROM wallet_events WHERE wallet = :wallet AND event_type = 'REDEEM_PAYOUT'"
        ),
        {"wallet": wallet},
    ).fetchone()
    assert row.event_type == "REDEEM_PAYOUT"
    assert Decimal(row.delta_usdc) == Decimal("10")
    assert Decimal(row.usdc_size) == Decimal("10")
    assert row.is_derived == 1
    assert row.source == "derived/redeem_payout"


def test_derivation_only_fills_api_reported_zero_redeems(session):
    wallet = "0xnonzero"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "10", "usdc_size": "10"},
        ],
    )

    stats = derive_redeem_payouts(session, wallet, dust_epsilon=DUST)

    assert stats.derived_events_inserted == 0
    assert stats.nonzero_redeems_skipped == 1


def test_winner_and_loser_resolution_episode_pnl_after_derivation(session):
    _seed_market(session)
    winner = "0xwinner"
    loser = "0xloser"
    _seed_ledger(
        session,
        winner,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )
    _seed_ledger(
        session,
        loser,
        [
            {"type": "TRADE", "ts": 100, "token_id": "lose", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )

    derive_redeem_payouts(session, winner, dust_epsilon=DUST)
    derive_redeem_payouts(session, loser, dust_epsilon=DUST)
    rebuild_episodes(session, winner, dust_epsilon=DUST)
    rebuild_episodes(session, loser, dust_epsilon=DUST)

    assert _episode_realized(session, winner, "win") == Decimal("6")
    assert _episode_realized(session, loser, "lose") == Decimal("-4")


def test_merge_round_trip_decomposes_to_zero(session):
    wallet = "0xmergezero"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "SPLIT", "ts": 100, "delta_shares": "10", "delta_usdc": "-10"},
            {"type": "MERGE", "ts": 200, "delta_shares": "-10", "delta_usdc": "10"},
        ],
    )

    stats = rebuild_pnl_decomposition(session, wallet, dust_epsilon=DUST)
    row = fetch_pnl_decomposition(session, wallet)[0]

    assert stats.total_pnl == Decimal("0")
    assert row.bond_merge_pnl == Decimal("0")
    assert row.total_pnl == Decimal("0")


def test_decomposition_components_and_categories_sum_to_total_realized_pnl(session):
    wallet = "0xsum"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "TRADE", "ts": 150, "token_id": "win", "delta_shares": "-4", "delta_usdc": "3.2"},
            {"type": "REWARD", "ts": 175, "condition_id": None, "delta_usdc": "0.5"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-6", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )
    derive_redeem_payouts(session, wallet, dust_epsilon=DUST)
    rebuild_episodes(session, wallet, dust_epsilon=DUST)

    rebuild_pnl_decomposition(session, wallet, dust_epsilon=DUST)
    all_row = fetch_pnl_decomposition(session, wallet)[0]
    category_rows = fetch_pnl_decomposition(session, wallet, by_category=True)
    episode_realized = session.execute(
        text(
            "SELECT SUM(CAST(realized_pnl AS TEXT)) "
            "FROM episodes WHERE wallet = :wallet"
        ),
        {"wallet": wallet},
    ).scalar()
    ledger_rewards = session.execute(
        text(
            "SELECT SUM(CAST(delta_usdc AS TEXT)) FROM wallet_events "
            "WHERE wallet = :wallet AND event_type IN ('REWARD', 'MAKER_REBATE', 'TAKER_REBATE')"
        ),
        {"wallet": wallet},
    ).scalar()
    independently_computed_total = Decimal(str(episode_realized or 0)) + Decimal(
        str(ledger_rewards or 0)
    )

    assert all_row.directional_pnl == Decimal("1.6")
    assert all_row.redemption_pnl == Decimal("3.6")
    assert all_row.reward_income == Decimal("0.5")
    assert all_row.total_pnl == Decimal("5.7")
    assert all_row.total_pnl == independently_computed_total
    assert sum((row.total_pnl for row in category_rows), Decimal("0")) == all_row.total_pnl


def test_phase8_cli_smoke(session, settings, monkeypatch):
    wallet = "0xcli8"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))

    runner = CliRunner()
    result = runner.invoke(main, ["derive", "run", "--wallet", wallet])
    assert result.exit_code == 0, result.output
    assert "derived_inserted=1" in result.output

    result = runner.invoke(main, ["pnl", "show", "--wallet", wallet, "--by-category"])
    assert result.exit_code == 0, result.output
    assert "Sports:" in result.output
    assert "total=6" in result.output
