"""Phase 11 — maker/taker enrichment (subgraph + optional RPC)."""

from decimal import Decimal

import httpx
from sqlalchemy import text

from pmresearch.ingest.enrichment import (
    enrichment_coverage,
    join_fills,
    run_enrichment,
)
from pmresearch.sources.rpc import ORDER_FILLED_TOPIC0, decode_order_filled
from pmresearch.sources.subgraph import OrderFill, SubgraphSource


# --- fixtures / helpers -----------------------------------------------------


_RAW_SEQ = [0]


def _raw_ref(session, wallet):
    _RAW_SEQ[0] += 1
    seq = _RAW_SEQ[0]
    return session.execute(
        text(
            "INSERT INTO raw_fetches (source, endpoint, params_json, fetched_at, "
            "http_status, file_path, content_hash, row_count) "
            "VALUES ('test', 'activity', :p, 'test', 200, 'none', :h, 0) RETURNING id"
        ),
        {"p": f'{{"w":"{wallet}","n":{seq}}}', "h": f"p11-{wallet}-{seq}"},
    ).scalar()


def _seed_trade(session, wallet, *, tx, token, delta_shares, ts=1000, key=None):
    raw_ref = _raw_ref(session, wallet)
    return session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:w, 'TRADE', :ts, :tx, 'cond', :token, 'BUY', :ds, '0', '0', '0', "
            "'test', 0, :raw_ref, :key, 'test') RETURNING id"
        ),
        {
            "w": wallet.lower(),
            "ts": ts,
            "tx": tx,
            "token": token,
            "ds": str(delta_shares),
            "raw_ref": raw_ref,
            "key": key or f"{tx}-{token}-{delta_shares}-{ts}",
        },
    ).scalar()


def _fill(*, wallet_is_maker, tx, token, shares, maker, taker, order_hash="0xoh", fee=0):
    """Build an OrderFill where the traded token is `token` with `shares`
    (maker-pays-shares direction, makerAssetId=token != 0)."""
    raw_amount = int(Decimal(shares) * 1_000_000)
    return OrderFill(
        order_hash=order_hash,
        maker=maker.lower(),
        taker=taker.lower(),
        maker_asset_id=int(token),
        taker_asset_id=0,
        maker_amount_filled=raw_amount,
        taker_amount_filled=raw_amount,
        fee=fee,
        timestamp=1000,
        transaction_hash=tx.lower(),
        subgraph_id=f"{tx}-{token}-{shares}",
    )


def _encode_log(*, maker, taker, maker_asset_id, taker_asset_id, maker_amount, taker_amount, fee):
    def word(v):
        return format(v, "064x")

    def addr_topic(a):
        return "0x" + format(int(a, 16), "064x")

    order_hash = "aa" * 32
    data = "0x" + order_hash + "".join(
        word(v) for v in (maker_asset_id, taker_asset_id, maker_amount, taker_amount, fee)
    )
    return {
        "topics": [ORDER_FILLED_TOPIC0, addr_topic(maker), addr_topic(taker)],
        "data": data,
        "transactionHash": "0xdeadbeef",
        "blockNumber": "0x64",
    }


# --- RPC decode -------------------------------------------------------------


def test_decode_order_filled_maker_pays_usdc():
    # makerAssetId == 0 ⇒ maker paid USDC ⇒ traded token = takerAssetId.
    log = _encode_log(
        maker="0x1111111111111111111111111111111111111111",
        taker="0x2222222222222222222222222222222222222222",
        maker_asset_id=0,
        taker_asset_id=777,
        maker_amount=4_000000,
        taker_amount=5_000000,
        fee=1_000,
    )
    decoded = decode_order_filled(log)

    assert decoded.maker == "0x1111111111111111111111111111111111111111"
    assert decoded.taker == "0x2222222222222222222222222222222222222222"
    assert decoded.maker_asset_id == 0
    assert decoded.taker_asset_id == 777
    assert decoded.traded_token_id == "777"
    assert decoded.traded_shares == Decimal("5")
    assert decoded.fee_decimal == Decimal("0.001")


def test_decode_order_filled_maker_pays_shares():
    # makerAssetId != 0 ⇒ maker paid shares ⇒ traded token = makerAssetId.
    log = _encode_log(
        maker="0x1111111111111111111111111111111111111111",
        taker="0x2222222222222222222222222222222222222222",
        maker_asset_id=888,
        taker_asset_id=0,
        maker_amount=7_000000,
        taker_amount=3_000000,
        fee=0,
    )
    decoded = decode_order_filled(log)

    assert decoded.traded_token_id == "888"
    assert decoded.traded_shares == Decimal("7")
    assert decoded.block_number == 100


# --- join logic -------------------------------------------------------------


def test_multiple_fills_same_wallet_asset_matched_by_amount(session):
    wallet = "0xaaa"
    other = "0xbbb"
    tx = "0xtx1"
    token = "111"
    id_big = _seed_trade(session, wallet, tx=tx, token=token, delta_shares="10", key="a")
    id_small = _seed_trade(session, wallet, tx=tx, token=token, delta_shares="3", key="b")
    session.commit()

    fills = [
        _fill(wallet_is_maker=True, tx=tx, token=token, shares="10", maker=wallet, taker=other),
        _fill(wallet_is_maker=True, tx=tx, token=token, shares="3", maker=wallet, taker=other),
    ]
    stats = join_fills(session, wallet, fills, source="subgraph")

    assert stats.enriched == 2
    assert stats.ambiguous == 0
    rows = {
        r.event_id: r
        for r in session.execute(text("SELECT event_id, role, counterparty FROM fill_enrichment"))
    }
    assert set(rows) == {id_big, id_small}
    assert rows[id_big].role == "maker"
    assert rows[id_big].counterparty == other


def test_ambiguous_identical_candidates_left_unenriched(session):
    wallet = "0xaaa"
    tx = "0xtx2"
    token = "222"
    _seed_trade(session, wallet, tx=tx, token=token, delta_shares="5", key="a")
    _seed_trade(session, wallet, tx=tx, token=token, delta_shares="5", key="b")
    session.commit()

    fills = [
        _fill(wallet_is_maker=True, tx=tx, token=token, shares="5", maker=wallet, taker="0xbbb"),
    ]
    stats = join_fills(session, wallet, fills, source="subgraph")

    assert stats.enriched == 0
    assert stats.ambiguous == 1
    count = session.execute(text("SELECT COUNT(*) FROM fill_enrichment")).scalar()
    assert count == 0


def test_idempotent_enrichment(session):
    wallet = "0xaaa"
    tx = "0xtx3"
    token = "333"
    event_id = _seed_trade(session, wallet, tx=tx, token=token, delta_shares="8", key="a")
    session.commit()
    fills = [_fill(wallet_is_maker=True, tx=tx, token=token, shares="8", maker=wallet, taker="0xbbb")]

    first = join_fills(session, wallet, fills, source="subgraph")
    second = join_fills(session, wallet, fills, source="subgraph")

    assert first.enriched == 1
    assert second.enriched == 0
    assert second.already_enriched == 1
    count = session.execute(
        text("SELECT COUNT(*) FROM fill_enrichment WHERE event_id = :e"), {"e": event_id}
    ).scalar()
    assert count == 1


def test_amount_conversion_matches_delta_shares(session):
    # 6-decimal integer 5_000000 must convert to Decimal('5') and match a
    # ledger delta_shares of "5".
    wallet = "0xaaa"
    tx = "0xtx4"
    token = "444"
    _seed_trade(session, wallet, tx=tx, token=token, delta_shares="5", key="a")
    session.commit()
    fill = OrderFill(
        order_hash="0xoh", maker=wallet, taker="0xbbb",
        maker_asset_id=int(token), taker_asset_id=0,
        maker_amount_filled=5_000000, taker_amount_filled=5_000000,
        fee=0, timestamp=1000, transaction_hash=tx, subgraph_id="s1",
    )
    assert fill.traded_shares == Decimal("5")
    stats = join_fills(session, wallet, [fill], source="subgraph")
    assert stats.enriched == 1


# --- coverage / lag awareness -----------------------------------------------


def test_coverage_lag_awareness(session):
    wallet = "0xaaa"
    # head_ts = 1000: an event at ts=2000 is newer than the subgraph head
    # (pending), one at ts=500 is older and unenriched (missing).
    _seed_trade(session, wallet, tx="0xr", token="1", delta_shares="1", ts=2000, key="recent")
    _seed_trade(session, wallet, tx="0xo", token="2", delta_shares="1", ts=500, key="old")
    session.commit()

    cov = enrichment_coverage(session, wallet, now_ts=3000, head_ts=1000)

    assert cov.total == 2
    assert cov.pending == 1
    assert cov.missing == 1
    assert cov.enriched == 0
    assert cov.ambiguous == 0


def test_coverage_counts_ambiguous_twins(session):
    wallet = "0xaaa"
    _seed_trade(session, wallet, tx="0xt", token="9", delta_shares="4", ts=500, key="a")
    _seed_trade(session, wallet, tx="0xt", token="9", delta_shares="4", ts=500, key="b")
    session.commit()

    cov = enrichment_coverage(session, wallet, now_ts=3000, head_ts=1000)

    assert cov.ambiguous == 2
    assert cov.missing == 0


# --- subgraph-only end to end (RPC disabled) --------------------------------


def test_subgraph_only_end_to_end(settings, session):
    assert settings.rpc_url == ""  # RPC off
    wallet = "0xaaa"
    other = "0xbbb"
    tx = "0xchain"
    token = "111"
    event_id = _seed_trade(session, wallet, tx=tx, token=token, delta_shares="10", key="a")
    session.commit()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "orderFilledEvents": [
                        {
                            "id": "fill-1",
                            "transactionHash": tx,
                            "timestamp": "1700000000",
                            "orderHash": "0xorder",
                            "maker": wallet,
                            "taker": other,
                            "makerAssetId": token,
                            "takerAssetId": "0",
                            "makerAmountFilled": "10000000",
                            "takerAmountFilled": "10000000",
                            "fee": "50000",
                        }
                    ]
                }
            },
        )

    client = httpx.Client(base_url="https://fake-subgraph", transport=httpx.MockTransport(handler))
    subgraph = SubgraphSource("https://fake-subgraph", client=client, sleep_fn=lambda s: None)

    stats = run_enrichment(session, settings, wallet, source="subgraph", subgraph=subgraph)

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, order_hash, fee, source FROM fill_enrichment WHERE event_id = :e"),
        {"e": event_id},
    ).fetchone()
    assert row.role == "maker"
    assert row.order_hash == "0xorder"
    assert row.fee == "0.05"
    assert row.source == "subgraph"

    wm = session.execute(
        text("SELECT subgraph_synced_to_ts FROM enrichment_watermarks WHERE wallet = :w"),
        {"w": wallet},
    ).fetchone()
    assert wm.subgraph_synced_to_ts == 1700000000
