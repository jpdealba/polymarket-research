"""Regression test for a real bug found while running Phase 1's backfill
against a very high-frequency live wallet: an interrupted backfill used to
restart from a brand new "now" on retry, re-walking history it had already
covered. This proves a retry resumes from the last checkpoint instead."""

import gzip
import json

import httpx
from sqlalchemy import text

from pmresearch.rawstore.store import RawStore
from pmresearch.sources.dataapi import GENESIS_TS, MAX_OFFSET, DataApiSource
from pmresearch.walletmanager import manager
from pmresearch.walletmanager import sync as sync_runner

WALLET = "0xresumeaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _make_dataset(start_ts: int, count: int) -> list[dict]:
    return [
        {
            "proxyWallet": WALLET,
            "timestamp": start_ts + i,
            "type": "TRADE",
            "transactionHash": f"0x{i:064x}",
            "size": 1.0,
            "usdcSize": 1.0,
            "price": 1.0,
            "asset": "1",
            "side": "BUY",
            "conditionId": "0xcond",
        }
        for i in range(count)
    ]


class FlakyTransport(httpx.BaseTransport):
    """Same offset-cap contract as the real Data-API, plus: raises after
    `fail_after` requests (simulating a crash mid-backfill), once."""

    def __init__(self, dataset: list[dict], *, fail_after: int) -> None:
        self.dataset = dataset
        self.fail_after = fail_after
        self.requests: list[dict] = []
        self._failed_once = False

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)

        if not self._failed_once and len(self.requests) > self.fail_after:
            self._failed_once = True
            raise RuntimeError("simulated crash")

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


def _all_persisted_tx_hashes(session) -> set[str]:
    rows = session.execute(text("SELECT file_path FROM raw_fetches")).fetchall()
    hashes: set[str] = set()
    for row in rows:
        with gzip.open(row.file_path, "rt") as fh:
            payload = json.load(fh)
        hashes.update(item["transactionHash"] for item in payload)
    return hashes


def test_interrupted_backfill_resumes_instead_of_restarting(settings, session, monkeypatch):
    # run_backfill always fetches from GENESIS_TS, so the dataset must sit
    # after it to be reachable at all.
    dataset_start = GENESIS_TS + 1_000_000
    dataset = _make_dataset(dataset_start, 4000)  # forces at least one narrowing
    # Crash right after the first window's cap-triggered checkpoint (request
    # 7 hits the cap; request 8 would start the narrowed window) — this is
    # exactly the scenario that needs to resume from a checkpoint, not from
    # a blank slate.
    transport = FlakyTransport(dataset, fail_after=7)
    client = httpx.Client(base_url="https://fake", transport=transport)
    source = DataApiSource(client=client, sleep_fn=lambda s: None)
    raw_store = RawStore(settings, session)

    fixed_now = dataset_start + 3999
    monkeypatch.setattr("pmresearch.walletmanager.sync.time.time", lambda: fixed_now)

    manager.add_wallet(session, WALLET)

    try:
        sync_runner.run_backfill(session, settings, raw_store, source, WALLET)
        assert False, "expected the simulated crash to propagate"
    except RuntimeError:
        pass

    state = manager.get_sync_state(session, WALLET)
    assert state.status == "error"
    assert state.backfill_complete is False
    assert state.last_incremental_ts == fixed_now  # original upper bound preserved
    assert state.backfill_cursor_ts is not None  # a checkpoint was recorded
    checkpoint_after_crash = state.backfill_cursor_ts
    requests_before_retry = len(transport.requests)

    # Retry: must resume from the checkpoint, not GENESIS_TS..fixed_now again.
    outcome = sync_runner.run_backfill(session, settings, raw_store, source, WALLET)

    first_retry_request = transport.requests[requests_before_retry]
    assert int(first_retry_request["end"]) == checkpoint_after_crash
    assert int(first_retry_request["end"]) < fixed_now  # did not restart from "now"

    state = manager.get_sync_state(session, WALLET)
    assert state.backfill_complete is True
    assert state.status == "complete"
    assert state.backfill_cursor_ts == GENESIS_TS
    assert state.last_incremental_ts == fixed_now

    # Full history was still eventually covered despite the interruption.
    expected_hashes = {row["transactionHash"] for row in dataset}
    assert _all_persisted_tx_hashes(session) == expected_hashes
