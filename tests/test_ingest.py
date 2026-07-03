from decimal import Decimal

import pytest
from sqlalchemy import text

from pmresearch.ingest import runner
from pmresearch.ingest.activity import parse_activity_row
from pmresearch.ingest.runner import reparse_wallet, run_ingest
from pmresearch.ledger.model import normalize_condition_id
from pmresearch.rawstore.store import RawStore

GOLDEN_WALLET = "0xabc0000000000000000000000000000000000a"

GOLDEN_ROWS = [
    {  # TRADE BUY
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1000,
        "conditionId": "0xcond1",
        "type": "TRADE",
        "size": 100.0,
        "usdcSize": 55.0,
        "transactionHash": "0xtx1",
        "price": 0.55,
        "asset": "1111",
        "side": "BUY",
        "outcomeIndex": 0,
    },
    {  # TRADE SELL
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1001,
        "conditionId": "0xcond1",
        "type": "TRADE",
        "size": 40.0,
        "usdcSize": 24.0,
        "transactionHash": "0xtx2",
        "price": 0.6,
        "asset": "1111",
        "side": "SELL",
        "outcomeIndex": 0,
    },
    {  # MERGE
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1002,
        "conditionId": "0xcond1",
        "type": "MERGE",
        "size": 10.0,
        "usdcSize": 10.0,
        "transactionHash": "0xtx3",
        "price": 0,
        "asset": "",
        "side": "",
        "outcomeIndex": 999,
    },
    {  # REDEEM (usdcSize is always reported as 0 by the source — verified fact)
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1003,
        "conditionId": "0xcond1",
        "type": "REDEEM",
        "size": 50.0,
        "usdcSize": 0.0,
        "transactionHash": "0xtx4",
        "price": 0,
        "asset": "",
        "side": "",
        "outcomeIndex": 999,
    },
    {  # REWARD
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1004,
        "conditionId": "",
        "type": "REWARD",
        "size": 1.23,
        "usdcSize": 1.23,
        "transactionHash": "0xtx5",
        "price": 0,
        "asset": "",
        "side": "",
        "outcomeIndex": 999,
    },
    {  # rebate income observed live in the activity feed
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1005,
        "conditionId": "",
        "type": "MAKER_REBATE",
        "size": 2.5,
        "usdcSize": 2.5,
        "transactionHash": "0xtx6",
        "price": 0,
        "asset": "",
        "side": "",
        "outcomeIndex": 999,
    },
    {
        "proxyWallet": GOLDEN_WALLET,
        "timestamp": 1006,
        "conditionId": "",
        "type": "TAKER_REBATE",
        "size": 1.5,
        "usdcSize": 1.5,
        "transactionHash": "0xtx7",
        "price": 0,
        "asset": "",
        "side": "",
        "outcomeIndex": 999,
    },
]


def _seed(raw_store, wallet, rows, **params_extra):
    return raw_store.persist(
        source="dataapi",
        endpoint="activity",
        wallet=wallet,
        params={"user": wallet, "offset": 0, **params_extra},
        payload=rows,
        http_status=200,
    )


def _ledger_rows(session, wallet):
    return session.execute(
        text("SELECT * FROM wallet_events WHERE wallet = :w ORDER BY ts"), {"w": wallet}
    ).fetchall()


def test_golden_fixture_exact_rows_and_sign_conventions(settings, session):
    raw_store = RawStore(settings, session)
    _seed(raw_store, GOLDEN_WALLET, GOLDEN_ROWS)

    stats = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats.raw_fetches_processed == 1
    assert stats.events_seen == 7
    assert stats.events_inserted == 7

    rows = _ledger_rows(session, GOLDEN_WALLET)
    assert len(rows) == 7
    by_tx = {r.tx_hash: r for r in rows}

    trade_buy = by_tx["0xtx1"]
    assert trade_buy.event_type == "TRADE"
    assert Decimal(trade_buy.delta_shares) == Decimal("100.0")
    assert Decimal(trade_buy.delta_usdc) == Decimal("-55.0")
    assert trade_buy.token_id == "1111"
    assert trade_buy.condition_id == "0xcond1"
    assert trade_buy.raw_ref is not None

    trade_sell = by_tx["0xtx2"]
    assert Decimal(trade_sell.delta_shares) == Decimal("-40.0")
    assert Decimal(trade_sell.delta_usdc) == Decimal("24.0")

    merge = by_tx["0xtx3"]
    assert merge.event_type == "MERGE"
    assert Decimal(merge.delta_shares) == Decimal("-10.0")
    assert Decimal(merge.delta_usdc) == Decimal("10.0")
    assert merge.token_id is None

    redeem = by_tx["0xtx4"]
    assert redeem.event_type == "REDEEM"
    assert Decimal(redeem.delta_shares) == Decimal("-50.0")
    assert Decimal(redeem.delta_usdc) == Decimal("0.0")
    assert redeem.token_id is None

    reward = by_tx["0xtx5"]
    assert reward.event_type == "REWARD"
    assert Decimal(reward.delta_shares) == Decimal("0")
    assert Decimal(reward.delta_usdc) == Decimal("1.23")
    assert reward.token_id is None

    maker_rebate = by_tx["0xtx6"]
    assert maker_rebate.event_type == "MAKER_REBATE"
    assert Decimal(maker_rebate.delta_shares) == Decimal("0")
    assert Decimal(maker_rebate.delta_usdc) == Decimal("2.5")

    taker_rebate = by_tx["0xtx7"]
    assert taker_rebate.event_type == "TAKER_REBATE"
    assert Decimal(taker_rebate.delta_shares) == Decimal("0")
    assert Decimal(taker_rebate.delta_usdc) == Decimal("1.5")


def test_ingest_idempotent_zero_new_rows_on_rerun(settings, session):
    raw_store = RawStore(settings, session)
    _seed(raw_store, GOLDEN_WALLET, GOLDEN_ROWS)

    stats1 = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats1.events_inserted == 7

    stats2 = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats2.raw_fetches_processed == 0  # already-ingested raw_fetches are skipped entirely
    assert stats2.events_inserted == 0

    count = session.execute(
        text("SELECT COUNT(*) FROM wallet_events WHERE wallet = :w"), {"w": GOLDEN_WALLET}
    ).scalar()
    assert count == 7


def test_overlapping_raw_fetches_dedupe_at_ledger_level(settings, session):
    raw_store = RawStore(settings, session)
    window_a = GOLDEN_ROWS[:4]  # tx1..tx4
    window_b = GOLDEN_ROWS[2:]  # tx3..tx6 (tx3, tx4 overlap with window_a)

    _seed(raw_store, GOLDEN_WALLET, window_a, start=1000, end=1003)
    _seed(raw_store, GOLDEN_WALLET, window_b, start=1002, end=1005)

    stats = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats.raw_fetches_processed == 2
    assert stats.events_seen == 9  # 4 + 5 rows seen across both fetches
    assert stats.events_inserted == 7  # only 7 distinct events; tx3/tx4 deduped

    count = session.execute(
        text("SELECT COUNT(*) FROM wallet_events WHERE wallet = :w"), {"w": GOLDEN_WALLET}
    ).scalar()
    assert count == 7


def test_multiple_legitimate_fills_in_same_transaction_are_preserved(settings, session):
    raw_store = RawStore(settings, session)
    fill_a = {
        **GOLDEN_ROWS[0],
        "transactionHash": "0xmulti",
        "size": 10.0,
        "usdcSize": 4.0,
        "price": 0.4,
    }
    fill_b = {
        **GOLDEN_ROWS[0],
        "transactionHash": "0xmulti",
        "size": 12.0,
        "usdcSize": 6.0,
        "price": 0.5,
    }
    _seed(raw_store, GOLDEN_WALLET, [fill_a, fill_b])

    stats = run_ingest(session, wallet=GOLDEN_WALLET)

    assert stats.events_seen == 2
    assert stats.events_inserted == 2
    rows = session.execute(
        text(
            "SELECT tx_hash, token_id, delta_shares, delta_usdc, price "
            "FROM wallet_events WHERE wallet = :w ORDER BY price"
        ),
        {"w": GOLDEN_WALLET},
    ).fetchall()
    assert len(rows) == 2
    assert {row.tx_hash for row in rows} == {"0xmulti"}
    assert {Decimal(row.delta_shares) for row in rows} == {Decimal("10.0"), Decimal("12.0")}
    assert {Decimal(row.delta_usdc) for row in rows} == {Decimal("-4.0"), Decimal("-6.0")}


def test_reparse_is_row_for_row_identical(settings, session):
    raw_store = RawStore(settings, session)
    _seed(raw_store, GOLDEN_WALLET, GOLDEN_ROWS)
    run_ingest(session, wallet=GOLDEN_WALLET)

    cols = (
        "event_type, ts, tx_hash, condition_id, token_id, side, "
        "delta_shares, delta_usdc, price, usdc_size, dedupe_key"
    )
    before = session.execute(
        text(f"SELECT {cols} FROM wallet_events WHERE wallet = :w ORDER BY tx_hash"),
        {"w": GOLDEN_WALLET},
    ).fetchall()

    stats = reparse_wallet(session, GOLDEN_WALLET)
    assert stats.events_inserted == 7

    after = session.execute(
        text(f"SELECT {cols} FROM wallet_events WHERE wallet = :w ORDER BY tx_hash"),
        {"w": GOLDEN_WALLET},
    ).fetchall()

    assert before == after


def test_ingest_commits_progress_per_raw_fetch(settings, session, monkeypatch):
    raw_store = RawStore(settings, session)
    first = _seed(raw_store, GOLDEN_WALLET, [GOLDEN_ROWS[0]], start=1000, end=1000)
    second = _seed(raw_store, GOLDEN_WALLET, [GOLDEN_ROWS[1]], start=1001, end=1001)

    original_parse = runner.parse_activity_row

    def parse_or_raise(row, **kwargs):
        if row["transactionHash"] == "0xtx2":
            raise RuntimeError("boom")
        return original_parse(row, **kwargs)

    monkeypatch.setattr(runner, "parse_activity_row", parse_or_raise)

    with pytest.raises(RuntimeError, match="boom"):
        run_ingest(session, wallet=GOLDEN_WALLET)
    session.rollback()

    rows = session.execute(
        text("SELECT id, ingested_at FROM raw_fetches ORDER BY id")
    ).fetchall()
    by_id = {row.id: row.ingested_at for row in rows}
    assert by_id[first.raw_fetch_id] is not None
    assert by_id[second.raw_fetch_id] is None

    count = session.execute(
        text("SELECT COUNT(*) FROM wallet_events WHERE wallet = :w"), {"w": GOLDEN_WALLET}
    ).scalar()
    assert count == 1


def test_normalize_condition_id_bytea_prefix_to_0x():
    assert normalize_condition_id("\\x" + "ab" * 32) == "0x" + "ab" * 32


def test_normalize_condition_id_lowercases_and_is_idempotent():
    mixed = "0x" + "AbCd" * 16
    normalized = normalize_condition_id(mixed)
    assert normalized == mixed.lower()
    assert normalize_condition_id(normalized) == normalized


def test_normalize_condition_id_none_passthrough():
    assert normalize_condition_id(None) is None


def test_normalize_condition_id_malformed_preserved_not_dropped():
    # Doesn't match the recognized 0x/\x + hex shapes — preserved as-is
    # rather than nulled or invented (same policy as unrecognized event
    # types in ledger/model.py).
    assert normalize_condition_id("0xcond1") == "0xcond1"


def test_parse_activity_row_trade_0x_condition_id_stays_0x():
    row = dict(GOLDEN_ROWS[0])
    row["conditionId"] = "0x" + "cd" * 32
    event = parse_activity_row(row, wallet=GOLDEN_WALLET, raw_fetch_id=1)
    assert event.condition_id == "0x" + "cd" * 32


def test_parse_activity_row_merge_bytea_condition_id_becomes_0x():
    row = dict(GOLDEN_ROWS[2])  # MERGE
    row["conditionId"] = "\\x" + "de" * 32
    event = parse_activity_row(row, wallet=GOLDEN_WALLET, raw_fetch_id=1)
    assert event.condition_id == "0x" + "de" * 32


def test_parse_activity_row_redeem_bytea_condition_id_becomes_0x():
    row = dict(GOLDEN_ROWS[3])  # REDEEM
    row["conditionId"] = "\\x" + "fa" * 32
    event = parse_activity_row(row, wallet=GOLDEN_WALLET, raw_fetch_id=1)
    assert event.condition_id == "0x" + "fa" * 32
