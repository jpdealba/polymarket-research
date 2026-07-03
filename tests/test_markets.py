import json

import httpx
from sqlalchemy import text

from pmresearch.exposure.descriptors import derive_structure_type
from pmresearch.ingest.markets import ledger_condition_ids, missing_market_count, upsert_market_payloads
from pmresearch.rawstore.store import RawStore
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
    raw = session.execute(
        text("SELECT source, endpoint, row_count FROM raw_fetches WHERE source = 'gamma'")
    ).fetchone()
    assert raw.source == "gamma"
    assert raw.endpoint == "markets"
    assert raw.row_count == 1
