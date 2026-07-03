import gzip
import json

import httpx
from sqlalchemy import text

from pmresearch.rawstore.store import RawStore
from pmresearch.sources.base import SourceAdapter
from pmresearch.sources.dataapi import MAX_OFFSET, DataApiSource, PositionsFetchIncomplete


def _make_dataset(start_ts: int, count: int) -> list[dict]:
    return [
        {
            "timestamp": start_ts + i,
            "type": "TRADE",
            "transactionHash": f"0x{i:064x}",
            "size": 1.0,
        }
        for i in range(count)
    ]


class RecordingTransport(httpx.BaseTransport):
    """Emulates the real Data-API /activity contract: offset > 3000 -> 400."""

    def __init__(self, dataset: list[dict]) -> None:
        self.dataset = dataset
        self.requests: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        start = int(params["start"])
        end = int(params["end"])
        offset = int(params["offset"])
        limit = int(params["limit"])

        if offset > MAX_OFFSET:
            return httpx.Response(
                400, json={"error": "max historical activity offset of 3000 exceeded"}
            )

        window = [row for row in self.dataset if start <= row["timestamp"] <= end]
        window.sort(key=lambda r: r["timestamp"], reverse=True)
        page = window[offset : offset + limit]
        return httpx.Response(200, json=page)


class PositionsTransport(httpx.BaseTransport):
    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.requests: list[dict] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)
        offset = int(params["offset"])
        limit = int(params["limit"])
        page = self.rows[offset : offset + limit]
        return httpx.Response(200, json=page)


def _build_source(transport: httpx.BaseTransport) -> DataApiSource:
    client = httpx.Client(base_url="https://fake", transport=transport)
    return DataApiSource(client=client, sleep_fn=lambda s: None)


def _all_persisted_tx_hashes(session, settings) -> set[str]:
    rows = session.execute(text("SELECT file_path FROM raw_fetches")).fetchall()
    hashes: set[str] = set()
    for row in rows:
        with gzip.open(row.file_path, "rt") as fh:
            payload = json.load(fh)
        hashes.update(item["transactionHash"] for item in payload)
    return hashes


def test_window_splitting_full_coverage_no_gaps(settings, session):
    dataset = _make_dataset(1_000_000, 4000)  # forces at least one offset-cap split
    transport = RecordingTransport(dataset)
    source = _build_source(transport)
    raw_store = RawStore(settings, session)

    outcome = source.fetch_activity_range(raw_store, "0xabc", 1_000_000, 1_003_999)

    expected_hashes = {row["transactionHash"] for row in dataset}
    assert _all_persisted_tx_hashes(session, settings) == expected_hashes  # no gaps

    # Overlap can only happen at a split's boundary second, so it's bounded —
    # nowhere near a full re-fetch of the window.
    assert len(dataset) <= outcome.rows_fetched < len(dataset) * 1.1
    assert len(transport.requests) < 100  # sane bound; proves no runaway recursion


def test_window_splitting_no_split_needed_for_small_window(settings, session):
    dataset = _make_dataset(2_000_000, 10)
    transport = RecordingTransport(dataset)
    source = _build_source(transport)
    raw_store = RawStore(settings, session)

    outcome = source.fetch_activity_range(raw_store, "0xabc", 2_000_000, 2_000_009)

    assert outcome.rows_fetched == 10
    assert len(transport.requests) == 1


def test_window_splitting_saturated_second_truncates_without_hanging(settings, session):
    # >3500 events crammed into a single second: no timestamp narrowing is
    # possible. Must truncate with a warning, not recurse forever.
    dataset = [
        {
            "timestamp": 5_000_000,
            "type": "TRADE",
            "transactionHash": f"0x{i:064x}",
            "size": 1.0,
        }
        for i in range(4000)
    ]
    transport = RecordingTransport(dataset)
    source = _build_source(transport)
    raw_store = RawStore(settings, session)

    outcome = source.fetch_activity_range(raw_store, "0xabc", 5_000_000, 5_000_000)

    # Truncated (not all 4000 rows reachable), but terminates and keeps what
    # it could reach via plain paging (up to offset 3000 + a page = 3500).
    assert 0 < outcome.rows_fetched <= 3500
    assert len(transport.requests) < 20


def test_backoff_on_429_then_success():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429)
        return httpx.Response(200, json=[])

    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="https://fake", transport=transport)
    sleeps: list[float] = []
    adapter = SourceAdapter("https://fake", client=client, sleep_fn=sleeps.append)

    response, payload = adapter.get_json("/activity", {"user": "0xabc"})

    assert response.status_code == 200
    assert payload == []
    assert calls["n"] == 3
    assert len(sleeps) == 2


def test_backoff_gives_up_after_max_retries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="https://fake", transport=transport)
    from pmresearch.sources.base import RetryConfig

    adapter = SourceAdapter(
        "https://fake", client=client, retry=RetryConfig(max_retries=2), sleep_fn=lambda s: None
    )

    try:
        adapter.get_json("/activity", {"user": "0xabc"})
        assert False, "expected HTTPStatusError"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 503


def test_non_json_http_error_body_returns_response_for_caller_to_raise():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(414, text="URI Too Long")

    transport = httpx.MockTransport(handler)
    client = httpx.Client(base_url="https://fake", transport=transport)
    adapter = SourceAdapter("https://fake", client=client, sleep_fn=lambda s: None)

    response, payload = adapter.get_json("/markets", {"condition_ids": ["0xabc"]})

    assert response.status_code == 414
    assert payload is None
    try:
        response.raise_for_status()
        assert False, "expected HTTPStatusError"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 414


def test_fetch_positions_pages_and_raw_stores(settings, session):
    rows = [
        {
            "asset": f"tok{i}",
            "conditionId": "0xabc",
            "size": i + 1,
            "avgPrice": "0.5",
            "curPrice": "0.6",
            "currentValue": str((i + 1) * 0.6),
            "title": "Question",
            "outcome": "Yes",
        }
        for i in range(501)
    ]
    transport = PositionsTransport(rows)
    source = _build_source(transport)
    raw_store = RawStore(settings, session)

    outcome = source.fetch_positions(raw_store, "0xabc")

    assert outcome.rows_fetched == 501
    assert outcome.positions[0].token_id == "tok0"
    assert outcome.positions[0].size == 1
    assert [int(req["offset"]) for req in transport.requests] == [0, 500]
    persisted = session.execute(
        text("SELECT endpoint, row_count, params_json FROM raw_fetches ORDER BY id")
    ).fetchall()
    assert [row.endpoint for row in persisted] == ["positions", "positions"]
    assert [row.row_count for row in persisted] == [500, 1]
    assert '"sizeThreshold":0' in persisted[0].params_json


def test_fetch_positions_incomplete_at_offset_cap_fails(settings, session):
    rows = [{"asset": f"tok{i}", "size": 1} for i in range(10_500)]
    transport = PositionsTransport(rows)
    source = _build_source(transport)
    raw_store = RawStore(settings, session)

    try:
        source.fetch_positions(raw_store, "0xabc")
        assert False, "expected PositionsFetchIncomplete"
    except PositionsFetchIncomplete:
        pass

    assert int(transport.requests[-1]["offset"]) == 10_000
