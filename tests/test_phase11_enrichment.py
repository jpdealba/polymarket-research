"""Phase 11 — maker/taker enrichment (subgraph + optional RPC)."""

import json
from decimal import Decimal

import httpx
from sqlalchemy import text

from pmresearch.ingest.enrichment import (
    enrichment_coverage,
    join_fills,
    max_rpc_watermark,
    run_enrichment,
)
from pmresearch.sources.rpc import (
    CTF_EXCHANGE,
    CTF_EXCHANGE_V2,
    EXCHANGE_CONTRACTS,
    ORDER_FILLED_TOPIC0,
    ORDER_FILLED_V1_TOPIC0,
    ORDER_FILLED_V2_TOPIC0,
    OrderFilledLog,
    RpcError,
    RpcFetch,
    RpcSource,
    decode_order_filled,
)
from pmresearch.sources.polygonscan import PolygonscanSource
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


def _seed_trade(
    session,
    wallet,
    *,
    tx,
    token,
    delta_shares,
    delta_usdc="0",
    usdc_size="0",
    ts=1000,
    key=None,
):
    raw_ref = _raw_ref(session, wallet)
    return session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, "
            "delta_shares, delta_usdc, price, usdc_size, source, is_derived, raw_ref, "
            "dedupe_key, ingested_at) "
            "VALUES (:w, 'TRADE', :ts, :tx, 'cond', :token, 'BUY', :ds, :du, '0', :us, "
            "'test', 0, :raw_ref, :key, 'test') RETURNING id"
        ),
        {
            "w": wallet.lower(),
            "ts": ts,
            "tx": tx,
            "token": token,
            "ds": str(delta_shares),
            "du": str(delta_usdc),
            "us": str(usdc_size),
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

    # orderHash, maker, taker are all indexed (topics[1..3]); data holds the five
    # non-indexed words in declaration order.
    order_hash = "0x" + "aa" * 32
    data = "0x" + "".join(
        word(v) for v in (maker_asset_id, taker_asset_id, maker_amount, taker_amount, fee)
    )
    return {
        "topics": [ORDER_FILLED_TOPIC0, order_hash, addr_topic(maker), addr_topic(taker)],
        "data": data,
        "transactionHash": "0xdeadbeef",
        "blockNumber": "0x64",
    }


def _encode_v2_log(*, maker, taker, side, token_id, maker_amount, taker_amount, fee):
    def word(v):
        return format(v, "064x")

    def addr_topic(a):
        return "0x" + format(int(a, 16), "064x")

    order_hash = "0x" + "bb" * 32
    data = "0x" + "".join(
        word(v) for v in (side, token_id, maker_amount, taker_amount, fee, 0, 0)
    )
    return {
        "topics": [ORDER_FILLED_V2_TOPIC0, order_hash, addr_topic(maker), addr_topic(taker)],
        "data": data,
        "transactionHash": "0xv2",
        "blockNumber": "0x65",
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


def test_decode_order_filled_v2_buy_normalizes_to_maker_pays_usdc():
    log = _encode_v2_log(
        maker="0x1111111111111111111111111111111111111111",
        taker="0x2222222222222222222222222222222222222222",
        side=0,
        token_id=999,
        maker_amount=4_000000,
        taker_amount=5_000000,
        fee=2_000,
    )

    decoded = decode_order_filled(log)

    assert decoded.maker_asset_id == 0
    assert decoded.taker_asset_id == 999
    assert decoded.traded_token_id == "999"
    assert decoded.traded_shares == Decimal("5")
    assert decoded.fee_decimal == Decimal("0.002")


def test_decode_order_filled_v2_sell_normalizes_to_maker_pays_shares():
    log = _encode_v2_log(
        maker="0x1111111111111111111111111111111111111111",
        taker="0x2222222222222222222222222222222222222222",
        side=1,
        token_id=1001,
        maker_amount=7_000000,
        taker_amount=3_000000,
        fee=0,
    )

    decoded = decode_order_filled(log)

    assert decoded.maker_asset_id == 1001
    assert decoded.taker_asset_id == 0
    assert decoded.traded_token_id == "1001"
    assert decoded.traded_shares == Decimal("7")


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


def test_same_share_candidates_disambiguated_by_usdc_amount(session):
    wallet = "0xaaa"
    tx = "0xtx2b"
    token = "222"
    id_low = _seed_trade(
        session,
        wallet,
        tx=tx,
        token=token,
        delta_shares="1022.88",
        delta_usdc="-953.32416",
        usdc_size="953.32416",
        key="a",
    )
    id_high = _seed_trade(
        session,
        wallet,
        tx=tx,
        token=token,
        delta_shares="1022.88",
        delta_usdc="-960.48432",
        usdc_size="960.48432",
        key="b",
    )
    session.commit()

    fill = OrderFill(
        order_hash="0xoh",
        maker=wallet,
        taker="0xbbb",
        maker_asset_id=int(token),
        taker_asset_id=0,
        maker_amount_filled=1022_880000,
        taker_amount_filled=960_484320,
        fee=0,
        timestamp=1000,
        transaction_hash=tx,
        subgraph_id="s-usdc",
    )
    stats = join_fills(session, wallet, [fill], source="subgraph")

    assert stats.enriched == 1
    assert stats.ambiguous == 0
    rows = {
        r.event_id: r.role
        for r in session.execute(text("SELECT event_id, role FROM fill_enrichment"))
    }
    assert rows == {id_high: "maker"}
    assert id_low not in rows


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


def test_simple_maker_match_assigns_maker(session):
    wallet, other = "0xaaa", "0xbbb"
    event_id = _seed_trade(session, wallet, tx="0xmaker", token="101", delta_shares="5")
    session.commit()

    fill = _fill(
        wallet_is_maker=True,
        tx="0xmaker",
        token="101",
        shares="5",
        maker=wallet,
        taker=other,
    )

    stats = join_fills(session, wallet, [fill], source="subgraph")

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, counterparty FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "maker"
    assert row.counterparty == other


def test_simple_taker_match_assigns_taker(session):
    wallet, other = "0xaaa", "0xbbb"
    event_id = _seed_trade(session, wallet, tx="0xtaker", token="101", delta_shares="5")
    session.commit()

    fill = _fill(
        wallet_is_maker=False,
        tx="0xtaker",
        token="101",
        shares="5",
        maker=other,
        taker=wallet,
    )

    stats = join_fills(session, wallet, [fill], source="subgraph")

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, counterparty FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "taker"
    assert row.counterparty == other


def test_enrichment_address_normalization(session):
    wallet, other = "0xaaa", "0xbbb"
    event_id = _seed_trade(session, wallet, tx="0xcase", token="101", delta_shares="5")
    session.commit()

    fill = OrderFill(
        order_hash="0xCASE",
        maker="0XAAA",
        taker="0XBBB",
        maker_asset_id=101,
        taker_asset_id=0,
        maker_amount_filled=5_000000,
        taker_amount_filled=5_000000,
        fee=0,
        timestamp=1000,
        transaction_hash="0XCASE",
        subgraph_id="case",
    )

    stats = join_fills(session, "0XAAA", [fill], source="subgraph")

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, order_hash, counterparty FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "maker"
    assert row.order_hash == "0xcase"
    assert row.counterparty == other


def test_exchange_facing_maker_fill_classifies_wallet_as_taker(session):
    wallet = "0xaaa"
    event_id = _seed_trade(session, wallet, tx="0xexchange", token="101", delta_shares="5")
    session.commit()

    fill = _fill(
        wallet_is_maker=True,
        tx="0xexchange",
        token="101",
        shares="5",
        maker=wallet,
        taker=CTF_EXCHANGE,
        order_hash="0xexchangeorder",
    )

    stats = join_fills(session, wallet, [fill], source="subgraph")

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, counterparty FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "taker"
    assert row.counterparty == CTF_EXCHANGE


def test_companion_logs_do_not_let_maker_fetch_order_win(session):
    wallet, other = "0xaaa", "0xbbb"
    event_id = _seed_trade(session, wallet, tx="0xcompanions", token="101", delta_shares="5")
    session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, 'maker', '0xstale', '0', :counterparty, 'subgraph', 'old')"
        ),
        {"event_id": event_id, "counterparty": CTF_EXCHANGE},
    )
    session.commit()

    maker_page = _fill(
        wallet_is_maker=True,
        tx="0xcompanions",
        token="101",
        shares="5",
        maker=wallet,
        taker=CTF_EXCHANGE,
        order_hash="0xmakerpage",
    )
    taker_page = _fill(
        wallet_is_maker=False,
        tx="0xcompanions",
        token="101",
        shares="5",
        maker=other,
        taker=wallet,
        order_hash="0xtakerpage",
    )

    stats = join_fills(session, wallet, [maker_page, taker_page], source="subgraph")

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT role, order_hash, counterparty FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "taker"
    assert row.order_hash == "0xtakerpage"
    assert row.counterparty == other


def test_conflicting_maker_and_taker_evidence_classifies_ambiguous(session):
    wallet = "0xaaa"
    event_id = _seed_trade(session, wallet, tx="0xconflict", token="101", delta_shares="5")
    session.commit()

    maker_fill = _fill(
        wallet_is_maker=True,
        tx="0xconflict",
        token="101",
        shares="5",
        maker=wallet,
        taker="0xbbb",
        order_hash="0xmakerrole",
    )
    taker_fill = _fill(
        wallet_is_maker=False,
        tx="0xconflict",
        token="101",
        shares="5",
        maker="0xccc",
        taker=wallet,
        order_hash="0xtakerrole",
    )

    stats = join_fills(session, wallet, [maker_fill, taker_fill], source="subgraph")

    assert stats.enriched == 0
    assert stats.ambiguous == 1
    row = session.execute(
        text("SELECT role FROM fill_enrichment WHERE event_id = :id"),
        {"id": event_id},
    ).fetchone()
    assert row.role == "ambiguous"
    cov = enrichment_coverage(session, wallet, now_ts=3000, head_ts=2000)
    assert cov.ambiguous == 1
    assert cov.enriched == 0


def test_subgraph_and_polygonscan_shapes_resolve_exchange_roles_consistently(session):
    wallet = "0xaaa"
    subgraph_id = _seed_trade(
        session, wallet, tx="0xsubgraphex", token="101", delta_shares="5", key="sg"
    )
    polygonscan_id = _seed_trade(
        session, wallet, tx="0xpolygonex", token="102", delta_shares="7", key="poly"
    )
    session.commit()

    subgraph_fill = _fill(
        wallet_is_maker=True,
        tx="0xsubgraphex",
        token="101",
        shares="5",
        maker=wallet,
        taker=CTF_EXCHANGE,
    )
    polygonscan_log = _rpc_log(
        maker=wallet,
        taker=CTF_EXCHANGE,
        token="102",
        shares="7",
        tx="0xpolygonex",
        block=10,
    )

    subgraph_stats = join_fills(session, wallet, [subgraph_fill], source="subgraph")
    polygonscan_stats = join_fills(session, wallet, [polygonscan_log], source="polygonscan")

    assert subgraph_stats.enriched == 1
    assert polygonscan_stats.enriched == 1
    rows = {
        r.event_id: r
        for r in session.execute(text("SELECT event_id, role, source FROM fill_enrichment"))
    }
    assert rows[subgraph_id].role == "taker"
    assert rows[subgraph_id].source == "subgraph"
    assert rows[polygonscan_id].role == "taker"
    assert rows[polygonscan_id].source == "polygonscan"


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

    fill_row = {
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

    def handler(request: httpx.Request) -> httpx.Response:
        # The adapter runs one query per role; the wallet is the maker here, so
        # only the maker query returns the fill (the taker query is empty).
        query = request.read().decode()
        rows = [fill_row] if "maker: $wallet" in query else []
        return httpx.Response(200, json={"data": {"orderFilledEvents": rows}})

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


# --- RPC chunked driver -----------------------------------------------------


def _rpc_log(*, maker, taker, token, shares, tx, block):
    raw = int(Decimal(shares) * 1_000_000)
    return OrderFilledLog(
        order_hash="0xoh", maker=maker.lower(), taker=taker.lower(),
        maker_asset_id=int(token), taker_asset_id=0,
        maker_amount_filled=raw, taker_amount_filled=raw, fee=0,
        transaction_hash=tx.lower(), block_number=block,
    )


class _FakeRpc:
    """Records requested block ranges; returns the logs that fall in each range.
    `fail_ranges` raises RpcError to exercise the auto-halving path."""

    def __init__(self, logs, *, fail_ranges=()):
        self._logs = list(logs)
        self.calls = []
        self._fail = set(fail_ranges)

    def fetch_order_filled_logs(self, raw_store, *, wallet, from_block, to_block):
        self.calls.append((from_block, to_block))
        if (from_block, to_block) in self._fail:
            raise RpcError("range too large")
        sel = [lg for lg in self._logs if from_block <= lg.block_number <= to_block]
        head = max([from_block] + [lg.block_number for lg in sel])
        return RpcFetch(tuple(sel), head, 1)


def _set_rpc_watermark(session, wallet, block):
    session.execute(
        text(
            "INSERT INTO enrichment_watermarks (wallet, subgraph_synced_to_ts, rpc_synced_to_block) "
            "VALUES (:w, NULL, :b)"
        ),
        {"w": wallet.lower(), "b": block},
    )
    session.commit()


def test_max_rpc_watermark_across_wallets(settings, session):
    _set_rpc_watermark(session, "0xaaa", 100)
    _set_rpc_watermark(session, "0xbbb", 250)

    assert max_rpc_watermark(session, ["0xaaa", "0xbbb"]) == 250
    assert max_rpc_watermark(session, ["0xaaa"]) == 100
    assert max_rpc_watermark(session, ["0xccc"]) == 0
    assert max_rpc_watermark(session, []) == 0


def test_rpc_chunked_driver_enriches_and_advances_watermark(settings, session):
    wallet, other = "0xaaa", "0xbbb"
    id1 = _seed_trade(session, wallet, tx="0xt1", token="1", delta_shares="10", key="a")
    id2 = _seed_trade(session, wallet, tx="0xt2", token="2", delta_shares="20", key="b")
    session.commit()

    logs = [
        _rpc_log(maker=wallet, taker=other, token="1", shares="10", tx="0xt1", block=5),
        _rpc_log(maker=other, taker=wallet, token="2", shares="20", tx="0xt2", block=2500),
    ]
    rpc = _FakeRpc(logs)
    stats = run_enrichment(
        session, settings, wallet, source="rpc", rpc=rpc,
        from_block=0, to_block=3000, chunk_blocks=2000,
    )

    assert stats.enriched == 2
    assert rpc.calls == [(0, 1999), (2000, 3000)]  # two chunks, no re-scan
    wm = session.execute(
        text("SELECT rpc_synced_to_block FROM enrichment_watermarks WHERE wallet = :w"),
        {"w": wallet},
    ).fetchone()
    assert wm.rpc_synced_to_block == 3000
    roles = {
        r.event_id: r.role
        for r in session.execute(text("SELECT event_id, role FROM fill_enrichment"))
    }
    assert roles[id1] == "maker"
    assert roles[id2] == "taker"


def test_rpc_resume_skips_covered_blocks(settings, session):
    wallet, other = "0xaaa", "0xbbb"
    _seed_trade(session, wallet, tx="0xt1", token="1", delta_shares="10", key="a")
    session.commit()
    _set_rpc_watermark(session, wallet, 1000)

    logs = [_rpc_log(maker=wallet, taker=other, token="1", shares="10", tx="0xt1", block=1200)]
    rpc = _FakeRpc(logs)
    stats = run_enrichment(
        session, settings, wallet, source="rpc", rpc=rpc,
        from_block=0, to_block=1500, chunk_blocks=2000,
    )

    assert rpc.calls == [(1001, 1500)]  # resumed past the watermark, no re-scan of [0,1000]
    assert stats.enriched == 1


def test_block_driver_can_ignore_watermark_for_rescan(settings, session):
    wallet, other = "0xaaa", "0xbbb"
    _seed_trade(session, wallet, tx="0xt1", token="1", delta_shares="10", key="a")
    session.commit()
    _set_rpc_watermark(session, wallet, 1000)

    logs = [_rpc_log(maker=wallet, taker=other, token="1", shares="10", tx="0xt1", block=500)]
    rpc = _FakeRpc(logs)
    stats = run_enrichment(
        session, settings, wallet, source="rpc", rpc=rpc,
        from_block=0, to_block=600, chunk_blocks=1000, ignore_watermark=True,
    )

    assert rpc.calls == [(0, 600)]
    assert stats.enriched == 1


def test_rpc_halves_chunk_on_provider_limit(settings, session):
    wallet, other = "0xaaa", "0xbbb"
    _seed_trade(session, wallet, tx="0xt1", token="1", delta_shares="10", key="a")
    session.commit()

    logs = [_rpc_log(maker=wallet, taker=other, token="1", shares="10", tx="0xt1", block=1500)]
    rpc = _FakeRpc(logs, fail_ranges={(0, 1999)})
    stats = run_enrichment(
        session, settings, wallet, source="rpc", rpc=rpc,
        from_block=0, to_block=1999, chunk_blocks=2000,
    )

    assert stats.enriched == 1
    assert rpc.calls == [(0, 1999), (0, 999), (1000, 1999)]  # full range failed, halved
    wm = session.execute(
        text("SELECT rpc_synced_to_block FROM enrichment_watermarks WHERE wallet = :w"),
        {"w": wallet},
    ).fetchone()
    assert wm.rpc_synced_to_block == 1999


def test_polygonscan_uses_block_driver_and_source_label(settings, session):
    wallet, other = "0xaaa", "0xbbb"
    event_id = _seed_trade(session, wallet, tx="0xt1", token="1", delta_shares="10", key="a")
    session.commit()

    logs = [_rpc_log(maker=wallet, taker=other, token="1", shares="10", tx="0xt1", block=1500)]
    source = _FakeRpc(logs)
    stats = run_enrichment(
        session, settings, wallet, source="polygonscan", rpc=source,
        from_block=0, to_block=1999, chunk_blocks=2000,
    )

    assert stats.enriched == 1
    row = session.execute(
        text("SELECT source FROM fill_enrichment WHERE event_id = :e"),
        {"e": event_id},
    ).fetchone()
    assert row.source == "polygonscan"
    wm = session.execute(
        text("SELECT rpc_synced_to_block FROM enrichment_watermarks WHERE wallet = :w"),
        {"w": wallet},
    ).fetchone()
    assert wm.rpc_synced_to_block == 1999


def test_rpc_http_400_range_cap_becomes_rpcerror(settings, session):
    """Alchemy signals its getLogs range/size cap as HTTP 400 with an error
    body. It must surface as RpcError (so the driver halves), not a raw
    HTTPStatusError traceback."""
    import pytest

    from pmresearch.rawstore.store import RawStore

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"error": {"code": -32602, "message": "Log response size exceeded."}},
        )

    client = httpx.Client(base_url="https://fake-rpc", transport=httpx.MockTransport(handler))
    rpc = RpcSource("https://fake-rpc", client=client, sleep_fn=lambda s: None)

    with pytest.raises(RpcError):
        rpc.fetch_order_filled_logs(
            RawStore(settings, session), wallet="0xaaa", from_block=0, to_block=2000
        )


def test_rpc_fetch_is_wallet_filtered_and_dedupes(settings, session):
    """Each getLogs is filtered to the wallet via the maker/taker topics (one
    query per role, both contracts in the address array); a log hit by both
    role queries (self-trade) is deduped by (txHash, logIndex)."""
    from pmresearch.rawstore.store import RawStore
    from pmresearch.sources.rpc import _wallet_topic

    wallet = "0x000000000000000000000000000000000000aaaa"
    captured_topics = []
    captured_address = []

    def handler(request: httpx.Request) -> httpx.Response:
        p = json.loads(request.read())["params"][0]
        captured_topics.append(p["topics"])
        captured_address.append(p["address"])
        if p["topics"][0] != ORDER_FILLED_V1_TOPIC0:
            return httpx.Response(200, json={"result": []})
        log = {
            "topics": [
                ORDER_FILLED_V1_TOPIC0, "0x" + "bb" * 32,
                _wallet_topic(wallet), _wallet_topic("0x" + "11" * 20),
            ],
            "data": "0x" + "".join(
                format(v, "064x") for v in (888, 0, 7_000000, 3_000000, 0)
            ),
            "transactionHash": "0xfeed",
            "logIndex": "0x1",
            "blockNumber": "0x5",
        }
        return httpx.Response(200, json={"result": [log]})

    client = httpx.Client(base_url="https://fake-rpc", transport=httpx.MockTransport(handler))
    rpc = RpcSource("https://fake-rpc", client=client, sleep_fn=lambda s: None)

    fetch = rpc.fetch_order_filled_logs(
        RawStore(settings, session), wallet=wallet, from_block=0, to_block=100
    )

    assert fetch.requests_made == 4  # maker/taker for V1 and V2 topics
    assert len(fetch.logs) == 1      # same log from both roles, deduped
    wtopic = _wallet_topic(wallet)
    assert captured_topics[0] == [ORDER_FILLED_V1_TOPIC0, None, wtopic, None]
    assert captured_topics[1] == [ORDER_FILLED_V2_TOPIC0, None, wtopic, None]
    assert captured_topics[2] == [ORDER_FILLED_V1_TOPIC0, None, None, wtopic]
    assert captured_topics[3] == [ORDER_FILLED_V2_TOPIC0, None, None, wtopic]
    assert captured_address[0] == [addr for addr, topic in EXCHANGE_CONTRACTS if topic == ORDER_FILLED_V1_TOPIC0]
    assert captured_address[1] == [addr for addr, topic in EXCHANGE_CONTRACTS if topic == ORDER_FILLED_V2_TOPIC0]


def test_polygonscan_fetch_pages_filters_wallet_and_raw_stores(settings, session):
    from pmresearch.rawstore.store import RawStore
    from pmresearch.sources.rpc import _wallet_topic

    wallet = "0x000000000000000000000000000000000000aaaa"
    other = "0x1111111111111111111111111111111111111111"
    captured = []

    log1 = _encode_log(
        maker=wallet,
        taker=other,
        maker_asset_id=888,
        taker_asset_id=0,
        maker_amount=7_000000,
        taker_amount=3_000000,
        fee=0,
    )
    log1.update({"transactionHash": "0xfeed1", "logIndex": "0x1", "blockNumber": "0x65"})
    log2 = _encode_log(
        maker=wallet,
        taker=other,
        maker_asset_id=999,
        taker_asset_id=0,
        maker_amount=9_000000,
        taker_amount=4_000000,
        fee=0,
    )
    log2.update({"transactionHash": "0xfeed2", "logIndex": "0x2", "blockNumber": "0x66"})
    log3 = _encode_v2_log(
        maker=wallet,
        taker=other,
        side=0,
        token_id=1002,
        maker_amount=5_000000,
        taker_amount=6_000000,
        fee=0,
    )
    log3.update({"transactionHash": "0xfeed3", "logIndex": "0x3", "blockNumber": "0x67"})

    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        captured.append(params)
        if params["module"] == "logs" and params["address"] == CTF_EXCHANGE:
            if params.get("topic2") == _wallet_topic(wallet):
                if params["page"] == "1":
                    return httpx.Response(
                        200, json={"status": "1", "message": "OK", "result": [log1]}
                    )
                if params["page"] == "2":
                    return httpx.Response(
                        200, json={"status": "1", "message": "OK", "result": [log2]}
                    )
        if params["module"] == "logs" and params["address"] == CTF_EXCHANGE_V2:
            if params.get("topic2") == _wallet_topic(wallet) and params["topic0"] == ORDER_FILLED_V2_TOPIC0:
                if params["page"] == "1":
                    return httpx.Response(
                        200, json={"status": "1", "message": "OK", "result": [log3]}
                    )
        return httpx.Response(
            200, json={"status": "0", "message": "No records found", "result": []}
        )

    client = httpx.Client(base_url="https://api.etherscan.io", transport=httpx.MockTransport(handler))
    source = PolygonscanSource("key", client=client, sleep_fn=lambda s: None, page_size=1)

    fetch = source.fetch_order_filled_logs(
        RawStore(settings, session), wallet=wallet, from_block=100, to_block=200
    )

    assert fetch.requests_made == (len(EXCHANGE_CONTRACTS) * 2) + 3
    assert [log.traded_token_id for log in fetch.logs] == ["888", "999", "1002"]
    assert fetch.head_block == 103
    assert captured[0]["topic0"] == ORDER_FILLED_V1_TOPIC0
    assert captured[0]["topic2"] == _wallet_topic(wallet)
    assert captured[0]["topic0_2_opr"] == "and"
    assert captured[0]["page"] == "1"
    assert captured[1]["page"] == "2"
    assert any(
        row["address"] == CTF_EXCHANGE_V2 and row["topic0"] == ORDER_FILLED_V2_TOPIC0
        for row in captured
    )
    raw_count = session.execute(
        text("SELECT COUNT(*) FROM raw_fetches WHERE source = 'polygonscan'")
    ).scalar()
    assert raw_count == (len(EXCHANGE_CONTRACTS) * 2) + 3


def test_get_block_number_retries_below_floor():
    # First attempt's two reads both land on stale nodes (min=100), below
    # the floor of 150 — should retry rather than returning the stale value.
    # Second attempt clears the floor (min=160).
    responses = iter([100, 100, 160, 170])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": hex(next(responses))})

    client = httpx.Client(base_url="https://api.etherscan.io", transport=httpx.MockTransport(handler))
    sleeps: list[float] = []
    source = PolygonscanSource("key", client=client, sleep_fn=sleeps.append)

    assert source.get_block_number(floor=150) == 160
    assert sleeps == [1.0]


def test_get_block_number_gives_up_after_max_attempts():
    # Every attempt stays below the floor — after exhausting retries, return
    # the best (highest) minimum seen rather than looping forever.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"status": "1", "message": "OK", "result": hex(50)})

    client = httpx.Client(base_url="https://api.etherscan.io", transport=httpx.MockTransport(handler))
    source = PolygonscanSource("key", client=client, sleep_fn=lambda s: None)

    assert source.get_block_number(floor=1000, max_attempts=3) == 50


def test_find_block_by_timestamp_binary_search():
    # A toy chain of 10 blocks where timestamp == block_number * 100.
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.read())
        if body["method"] == "eth_blockNumber":
            return httpx.Response(200, json={"result": hex(9)})
        n = int(body["params"][0], 16)
        return httpx.Response(200, json={"result": {"timestamp": hex(n * 100)}})

    client = httpx.Client(base_url="https://fake-rpc", transport=httpx.MockTransport(handler))
    rpc = RpcSource("https://fake-rpc", client=client, sleep_fn=lambda s: None)

    assert rpc.get_block_number() == 9
    # Lowest block with ts >= 450 is block 5 (ts 500); block 4 is ts 400.
    assert rpc.find_block_by_timestamp(450) == 5
    assert rpc.find_block_by_timestamp(500) == 5
    assert rpc.find_block_by_timestamp(0) == 0


def test_subgraph_graphql_error_raises_not_silent_zero(settings, session):
    """A GraphQL `errors` payload (HTTP 200) must raise, never be swallowed as
    zero fills — regression for the live 'or'-filter rejection that reported
    fills=0 while the query was actually invalid."""
    import pytest

    from pmresearch.rawstore.store import RawStore
    from pmresearch.sources.subgraph import SubgraphError

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"errors": [{"message": "Cannot mix column filters with 'or' operator"}]},
        )

    client = httpx.Client(base_url="https://fake-subgraph", transport=httpx.MockTransport(handler))
    subgraph = SubgraphSource("https://fake-subgraph", client=client, sleep_fn=lambda s: None)

    with pytest.raises(SubgraphError):
        subgraph.fetch_order_fills(RawStore(settings, session), "0xaaa")
