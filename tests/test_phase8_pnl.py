from decimal import Decimal
import json

from click.testing import CliRunner
from sqlalchemy import text

from pmresearch.cli import main
from pmresearch.ingest.derived import derive_redeem_payouts, derive_resolution_settlements
from pmresearch.projections.episodes import rebuild_episodes
from pmresearch.projections.holdings import fetch_holdings, rebuild_holdings
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
    assert "gross_base_total=6" in result.output
    assert "net_after_blended_fees=6" in result.output


def _holding_qty(session, wallet, token_id):
    rebuild_holdings(session, wallet, dust_epsilon=DUST)
    rows = fetch_holdings(session, wallet)
    matches = [row for row in rows if row.token_id == token_id]
    return Decimal(matches[0].qty) if matches else Decimal("0")


def test_resolution_settlement_losing_token_without_redeem_realizes_loss(session):
    wallet = "0xlosernoredeem"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "lose", "delta_shares": "10", "delta_usdc": "-4"},
        ],
    )

    stats = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)
    assert stats.resolved_open_tokens_seen == 1
    assert stats.derived_events_inserted == 1

    row = session.execute(
        text(
            "SELECT event_type, token_id, delta_shares, delta_usdc, is_derived, source "
            "FROM wallet_events WHERE wallet = :wallet AND event_type = 'RESOLUTION_SETTLEMENT'"
        ),
        {"wallet": wallet},
    ).fetchone()
    assert row.event_type == "RESOLUTION_SETTLEMENT"
    assert row.token_id == "lose"
    assert Decimal(row.delta_shares) == Decimal("-10")
    assert Decimal(row.delta_usdc) == Decimal("0")
    assert row.is_derived == 1
    assert row.source == "derived/resolution_settlement"

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    assert _episode_realized(session, wallet, "lose") == Decimal("-4")
    assert _holding_qty(session, wallet, "lose") == Decimal("0")


def test_resolution_settlement_winning_token_without_redeem_realizes_payout(session):
    wallet = "0xwinnernoredeem"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
        ],
    )

    stats = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)
    assert stats.derived_events_inserted == 1

    row = session.execute(
        text(
            "SELECT token_id, delta_shares, delta_usdc "
            "FROM wallet_events WHERE wallet = :wallet AND event_type = 'RESOLUTION_SETTLEMENT'"
        ),
        {"wallet": wallet},
    ).fetchone()
    assert row.token_id == "win"
    assert Decimal(row.delta_shares) == Decimal("-10")
    assert Decimal(row.delta_usdc) == Decimal("10")

    rebuild_episodes(session, wallet, dust_epsilon=DUST)
    assert _episode_realized(session, wallet, "win") == Decimal("6")

    rebuild_pnl_decomposition(session, wallet, dust_epsilon=DUST)
    row = fetch_pnl_decomposition(session, wallet)[0]
    assert row.redemption_pnl == Decimal("6")
    assert row.total_pnl == Decimal("6")


def test_resolution_settlement_skips_position_already_closed_by_redeem(session):
    wallet = "0xalreadyredeemed"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "win", "delta_shares": "10", "delta_usdc": "-4"},
            {"type": "REDEEM", "ts": 200, "delta_shares": "-10", "delta_usdc": "0", "usdc_size": "0"},
        ],
    )

    derive_redeem_payouts(session, wallet, dust_epsilon=DUST)
    stats = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)

    assert stats.resolved_open_tokens_seen == 0
    assert stats.derived_events_inserted == 0
    count = session.execute(
        text(
            "SELECT COUNT(*) FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'RESOLUTION_SETTLEMENT'"
        ),
        {"wallet": wallet},
    ).scalar_one()
    assert count == 0


def test_resolution_settlement_is_idempotent(session):
    wallet = "0xsettleidempotent"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {"type": "TRADE", "ts": 100, "token_id": "lose", "delta_shares": "10", "delta_usdc": "-4"},
        ],
    )

    first = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)
    second = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)

    assert first.derived_events_inserted == 1
    assert second.derived_events_inserted == 0
    count = session.execute(
        text(
            "SELECT COUNT(*) FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'RESOLUTION_SETTLEMENT'"
        ),
        {"wallet": wallet},
    ).scalar_one()
    assert count == 1


def test_resolution_settlement_ignores_unresolved_market(session):
    wallet = "0xunresolvedsplit"
    _seed_market(session, condition_id="0xunresolved", prices=(None, None))
    # Overwrite the seeded market to be unresolved (no resolution_prices_json).
    session.execute(
        text(
            "UPDATE markets SET closed = 0, resolution_prices_json = NULL "
            "WHERE condition_id = '0xunresolved'"
        )
    )
    session.commit()
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "SPLIT",
                "ts": 100,
                "condition_id": "0xunresolved",
                "delta_shares": "10",
                "delta_usdc": "-10",
            },
        ],
    )

    stats = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)

    assert stats.resolved_open_tokens_seen == 0
    assert stats.derived_events_inserted == 0


def test_resolution_settlement_ignores_dust_holding(session):
    wallet = "0xdustholder"
    _seed_market(session)
    _seed_ledger(
        session,
        wallet,
        [
            {
                "type": "TRADE",
                "ts": 100,
                "token_id": "lose",
                "delta_shares": "0.0000001",
                "delta_usdc": "-0.00000004",
            },
        ],
    )

    stats = derive_resolution_settlements(session, wallet, dust_epsilon=DUST)

    assert stats.resolved_open_tokens_seen == 0
    assert stats.derived_events_inserted == 0
    assert stats.dust_skipped == 1
