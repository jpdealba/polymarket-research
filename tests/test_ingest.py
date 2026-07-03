from decimal import Decimal

from sqlalchemy import text

from pmresearch.ingest.runner import reparse_wallet, run_ingest
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
    {  # unknown type (MAKER_REBATE is real, observed live, and not in the documented enum)
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
    assert stats.events_seen == 6
    assert stats.events_inserted == 6

    rows = _ledger_rows(session, GOLDEN_WALLET)
    assert len(rows) == 6
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

    unknown = by_tx["0xtx6"]
    assert unknown.event_type == "MAKER_REBATE"  # preserved as-is, never dropped
    assert Decimal(unknown.delta_shares) == Decimal("0")
    assert Decimal(unknown.delta_usdc) == Decimal("0")  # never guessed


def test_ingest_idempotent_zero_new_rows_on_rerun(settings, session):
    raw_store = RawStore(settings, session)
    _seed(raw_store, GOLDEN_WALLET, GOLDEN_ROWS)

    stats1 = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats1.events_inserted == 6

    stats2 = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats2.raw_fetches_processed == 0  # already-ingested raw_fetches are skipped entirely
    assert stats2.events_inserted == 0

    count = session.execute(
        text("SELECT COUNT(*) FROM wallet_events WHERE wallet = :w"), {"w": GOLDEN_WALLET}
    ).scalar()
    assert count == 6


def test_overlapping_raw_fetches_dedupe_at_ledger_level(settings, session):
    raw_store = RawStore(settings, session)
    window_a = GOLDEN_ROWS[:4]  # tx1..tx4
    window_b = GOLDEN_ROWS[2:]  # tx3..tx6 (tx3, tx4 overlap with window_a)

    _seed(raw_store, GOLDEN_WALLET, window_a, start=1000, end=1003)
    _seed(raw_store, GOLDEN_WALLET, window_b, start=1002, end=1005)

    stats = run_ingest(session, wallet=GOLDEN_WALLET)
    assert stats.raw_fetches_processed == 2
    assert stats.events_seen == 8  # 4 + 4 rows seen across both fetches
    assert stats.events_inserted == 6  # only 6 distinct events; tx3/tx4 deduped

    count = session.execute(
        text("SELECT COUNT(*) FROM wallet_events WHERE wallet = :w"), {"w": GOLDEN_WALLET}
    ).scalar()
    assert count == 6


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
    assert stats.events_inserted == 6

    after = session.execute(
        text(f"SELECT {cols} FROM wallet_events WHERE wallet = :w ORDER BY tx_hash"),
        {"w": GOLDEN_WALLET},
    ).fetchall()

    assert before == after
