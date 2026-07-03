import json

import httpx
from sqlalchemy import text

from pmresearch.exposure.descriptors import derive_structure_type
from pmresearch.ingest.markets import (
    MarketSyncStats,
    derive_category,
    event_ids_for_conditions,
    ledger_condition_ids,
    missing_market_count,
    upsert_event_category,
    upsert_market_payloads,
)
from pmresearch.rawstore.store import RawStore
from pmresearch.cli.markets import _sync_condition_ids
from pmresearch.sources.gamma import GammaSource


COND_BINARY = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
COND_NEGRISK = "0xbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
COND_TEAM = "0xcccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"


def _market(
    condition_id,
    *,
    outcomes,
    token_ids,
    prices=("0.25", "0.75"),
    closed=False,
    neg_risk=False,
    event_id="100",
):
    return {
        "conditionId": condition_id,
        "question": "Fixture market",
        "slug": f"fixture-{condition_id[-4:]}",
        "category": "Sports",
        "events": [
            {
                "id": event_id,
                "title": "Fixture event",
                "slug": f"event-{event_id}",
                "negRisk": neg_risk,
                "tags": [{"label": "fixture"}],
            }
        ],
        "negRisk": neg_risk,
        "outcomes": json.dumps(list(outcomes)),
        "clobTokenIds": json.dumps(list(token_ids)),
        "outcomePrices": json.dumps(list(prices)),
        "startDate": "2026-01-01T00:00:00Z",
        "endDate": "2026-01-02T00:00:00Z",
        "closed": closed,
        "closedTime": "2026-01-02 01:00:00+00" if closed else None,
    }


def test_descriptor_is_label_agnostic_for_binary_team_name_market():
    descriptor = derive_structure_type(
        {
            "clob_token_ids_json": json.dumps(["team-a-token", "team-b-token"]),
            "neg_risk": False,
            "event_id": "evt",
        }
    )

    assert descriptor == "binary"


def test_upsert_market_fixtures_resolution_and_idempotency(session):
    payloads = (
        [
            _market(COND_BINARY, outcomes=("Yes", "No"), token_ids=("101", "102")),
            _market(
                COND_NEGRISK,
                outcomes=("Candidate A", "Candidate B"),
                token_ids=("201", "202"),
                neg_risk=True,
                event_id="200",
            ),
            _market(
                COND_TEAM,
                outcomes=("Mavericks", "Grizzlies"),
                token_ids=("301", "302"),
                prices=("0", "1"),
                closed=True,
                event_id="300",
            ),
        ],
    )

    first = upsert_market_payloads(session, payloads, [COND_BINARY, COND_NEGRISK, COND_TEAM])
    second = upsert_market_payloads(session, payloads, [COND_BINARY, COND_NEGRISK, COND_TEAM])

    assert first.markets_upserted == 3
    assert first.tokens_upserted == 6
    assert first.events_upserted == 3
    assert second.markets_upserted == 3

    rows = session.execute(
        text("SELECT condition_id, structure_type, resolution_prices_json FROM markets")
    ).fetchall()
    by_condition = {row.condition_id: row for row in rows}
    assert by_condition[COND_BINARY].structure_type == "binary"
    assert by_condition[COND_NEGRISK].structure_type == "negRisk-event-member"
    assert by_condition[COND_TEAM].structure_type == "binary"
    assert json.loads(by_condition[COND_TEAM].resolution_prices_json) == {"301": "0", "302": "1"}

    token_count = session.execute(text("SELECT COUNT(*) FROM tokens")).scalar()
    event_count = session.execute(text("SELECT COUNT(*) FROM pm_events")).scalar()
    assert token_count == 6
    assert event_count == 3


def test_missing_market_detection_query(session):
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count, ingested_at) "
            "VALUES ('dataapi', 'activity', '{}', 'now', 200, 'fixture', 'hash', 1, 'now')"
        )
    )
    raw_id = session.execute(text("SELECT id FROM raw_fetches")).scalar()
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, delta_shares, "
            "delta_usdc, price, usdc_size, source, is_derived, raw_ref, dedupe_key, ingested_at) "
            "VALUES ('0xwallet', 'TRADE', 1, '0xtx', :condition_id, '101', 'BUY', "
            "'1', '-0.5', '0.5', '0.5', 'dataapi', 0, :raw_id, 'dedupe', 'now')"
        ),
        {"condition_id": COND_BINARY, "raw_id": raw_id},
    )
    session.commit()

    assert ledger_condition_ids(session, missing_only=True) == [COND_BINARY]
    assert missing_market_count(session) == 1

    upsert_market_payloads(
        session,
        ([_market(COND_BINARY, outcomes=("Yes", "No"), token_ids=("101", "102"))],),
        [COND_BINARY],
    )
    assert ledger_condition_ids(session, missing_only=True) == []
    assert missing_market_count(session) == 0


def test_gamma_source_batches_and_raw_stores(settings, session):
    seen_params = []

    def handler(request):
        seen_params.append(dict(request.url.params.multi_items()))
        return httpx.Response(
            200,
            json=[_market(COND_BINARY, outcomes=("Yes", "No"), token_ids=("101", "102"))],
        )

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=1)
    raw_store = RawStore(settings, session)

    result = source.fetch_markets_by_condition_ids(raw_store, [COND_BINARY])

    assert result.requests_made == 1
    assert result.rows_fetched == 1
    assert seen_params[0]["condition_ids"] == COND_BINARY
    assert seen_params[0]["closed"] == "false"
    raw = session.execute(
        text("SELECT source, endpoint, row_count FROM raw_fetches WHERE source = 'gamma'")
    ).fetchone()
    assert raw.source == "gamma"
    assert raw.endpoint == "markets"
    assert raw.row_count == 1


def test_gamma_closed_false_returns_no_historical_markets_but_closed_true_does(
    settings, session
):
    seen_closed = []

    def handler(request):
        closed = request.url.params["closed"]
        seen_closed.append(closed)
        ids = request.url.params.get_list("condition_ids")
        rows = (
            [
                _market(
                    condition_id,
                    outcomes=("Yes", "No"),
                    token_ids=(f"{condition_id[-4:]}01", f"{condition_id[-4:]}02"),
                    closed=True,
                )
                for condition_id in ids
            ]
            if closed == "true"
            else []
        )
        return httpx.Response(200, json=rows)

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=10)
    raw_store = RawStore(settings, session)

    open_result = source.fetch_markets_by_condition_ids(
        raw_store, [COND_BINARY], closed=False
    )
    closed_result = source.fetch_markets_by_condition_ids(
        raw_store, [COND_BINARY], closed=True
    )

    assert open_result.rows_fetched == 0
    assert closed_result.rows_fetched == 1
    assert seen_closed == ["false", "true"]


def test_markets_sync_falls_back_to_closed_true_per_batch(settings, session):
    seen_closed = []

    def handler(request):
        if request.url.path == "/events":
            ids = request.url.params.get_list("id")
            return httpx.Response(
                200,
                json=[
                    {
                        "id": event_id,
                        "title": "Fixture event",
                        "slug": f"event-{event_id}",
                        "negRisk": False,
                        "tags": [{"label": "Sports"}],
                    }
                    for event_id in ids
                ],
            )
        closed = request.url.params["closed"]
        seen_closed.append(closed)
        ids = request.url.params.get_list("condition_ids")
        rows = (
            [
                _market(
                    condition_id,
                    outcomes=("Yes", "No"),
                    token_ids=(f"{condition_id[-4:]}01", f"{condition_id[-4:]}02"),
                    closed=True,
                )
                for condition_id in ids
            ]
            if closed == "true"
            else []
        )
        return httpx.Response(200, json=rows)

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=1)
    raw_store = RawStore(settings, session)

    stats = _sync_condition_ids(session, source, raw_store, [COND_BINARY, COND_NEGRISK])

    assert seen_closed == ["false", "true", "false", "true"]
    assert stats.requested_conditions == 2
    assert stats.markets_upserted == 2
    assert stats.missing_conditions == 0
    assert session.execute(text("SELECT COUNT(*) FROM markets")).scalar() == 2
    categories = session.execute(text("SELECT category FROM markets")).fetchall()
    assert [row.category for row in categories] == ["Sports", "Sports"]


def test_gamma_uses_repeated_plain_condition_ids(settings, session):
    condition_ids = [COND_BINARY, COND_NEGRISK]
    seen_query = []
    seen_ids = []

    def handler(request):
        query = request.url.query
        if isinstance(query, bytes):
            query = query.decode("ascii")
        seen_query.append(query)
        ids = request.url.params.get_list("condition_ids")
        seen_ids.append(ids)
        return httpx.Response(
            200,
            json=[
                _market(condition_id, outcomes=("Yes", "No"), token_ids=("101", "102"))
                for condition_id in ids
            ],
        )

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=10)
    raw_store = RawStore(settings, session)

    result = source.fetch_markets_by_condition_ids(raw_store, condition_ids)

    assert result.rows_fetched == 2
    assert seen_ids == [sorted(condition_id.lower() for condition_id in condition_ids)]
    assert "condition_ids%5B%5D" not in seen_query[0]
    assert "condition_ids[]=" not in seen_query[0]
    assert seen_query[0].count("condition_ids=") == 2
    assert "closed=false" in seen_query[0]


def test_gamma_source_splits_long_condition_queries(settings, session):
    max_query_chars = 360
    seen_query_lengths = []

    condition_ids = [f"0x{i:064x}" for i in range(10)]

    def handler(request):
        query = request.url.query
        if isinstance(query, bytes):
            query = query.decode("ascii")
        seen_query_lengths.append(len(query))
        ids = request.url.params.get_list("condition_ids")
        return httpx.Response(
            200,
            json=[
                _market(condition_id, outcomes=("Yes", "No"), token_ids=("101", "102"))
                for condition_id in ids
            ],
        )

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=100, max_query_chars=max_query_chars)
    raw_store = RawStore(settings, session)

    result = source.fetch_markets_by_condition_ids(raw_store, condition_ids)

    assert result.requests_made > 1
    assert result.rows_fetched == len(condition_ids)
    assert max(seen_query_lengths) <= max_query_chars


def test_gamma_rejects_unrelated_condition_ids(settings, session, caplog):
    def handler(request):
        return httpx.Response(
            200,
            json=[_market(COND_TEAM, outcomes=("Yes", "No"), token_ids=("101", "102"))],
        )

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=10)
    raw_store = RawStore(settings, session)

    with caplog.at_level("WARNING", logger="pmresearch.sources.gamma"):
        result = source.fetch_markets_by_condition_ids(raw_store, [COND_BINARY])

    assert result.rows_fetched == 0
    assert "Ignoring Gamma market" in caplog.text
    stats = upsert_market_payloads(session, result.payloads, [COND_BINARY])
    assert stats.markets_upserted == 0
    assert stats.missing_conditions == 1
    assert session.execute(text("SELECT COUNT(*) FROM markets")).scalar() == 0


def test_gamma_batches_can_be_upserted_incrementally_and_idempotently(settings, session):
    condition_ids = [COND_BINARY, COND_NEGRISK]

    def handler(request):
        ids = request.url.params.get_list("condition_ids")
        return httpx.Response(
            200,
            json=[
                _market(
                    condition_id,
                    outcomes=("Yes", "No"),
                    token_ids=(f"{condition_id[-4:]}01", f"{condition_id[-4:]}02"),
                )
                for condition_id in ids
            ],
        )

    client = httpx.Client(
        base_url="https://gamma-api.polymarket.test",
        transport=httpx.MockTransport(handler),
    )
    source = GammaSource(client=client, batch_size=1)
    raw_store = RawStore(settings, session)

    def run_once():
        stats = MarketSyncStats.empty()
        for batch in source.fetch_market_batches_by_condition_ids(raw_store, condition_ids):
            batch_stats = upsert_market_payloads(
                session, (batch.payload,), list(batch.requested_ids)
            )
            stats = stats.merge(batch_stats)
        return stats

    stats = run_once()
    second = run_once()
    assert stats.requested_conditions == 2
    assert stats.markets_upserted == 2
    assert stats.tokens_upserted == 4
    assert stats.missing_conditions == 0
    assert second.requested_conditions == 2
    assert second.markets_upserted == 2
    assert second.tokens_upserted == 4
    assert second.missing_conditions == 0
    assert session.execute(text("SELECT COUNT(*) FROM markets")).scalar() == 2
    raw_count = session.execute(
        text("SELECT COUNT(*) FROM raw_fetches WHERE source = 'gamma'")
    ).scalar()
    assert raw_count == 2


def test_derive_category_skips_non_canonical_tags_and_keeps_order():
    assert derive_category(
        [{"label": "exchange"}, {"label": "Crypto"}, {"label": "Featured"}]
    ) == "Crypto"
    assert derive_category([{"label": "Featured"}, {"label": "exchange"}]) is None
    assert derive_category([]) is None


def test_upsert_event_category_propagates_to_markets(session):
    upsert_market_payloads(
        session,
        ([_market(COND_BINARY, outcomes=("Yes", "No"), token_ids=("101", "102"))],),
        [COND_BINARY],
    )
    assert event_ids_for_conditions(session, [COND_BINARY]) == ["100"]

    updated = upsert_event_category(
        session,
        {
            "id": "100",
            "title": "Fixture event",
            "slug": "event-100",
            "negRisk": False,
            "tags": [{"label": "France"}, {"label": "Politics"}],
        },
    )
    session.commit()

    assert updated == 1
    row = session.execute(
        text("SELECT category FROM markets WHERE condition_id = :cid"), {"cid": COND_BINARY}
    ).fetchone()
    assert row.category == "Politics"
