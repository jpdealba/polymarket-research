import httpx
from sqlalchemy import text

from pmresearch.ingest.runner import run_ingest
from pmresearch.rawstore.store import RawStore
from pmresearch.sources.dataapi import DataApiSource
from pmresearch.walletmanager import manager
from pmresearch.walletmanager import sync as sync_runner


WALLET = "0xlateaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"


def _activity_row(
    *,
    tx_hash: str,
    timestamp: int,
    event_type: str = "TRADE",
    size: float = 1.0,
    usdc_size: float = 1.0,
) -> dict:
    return {
        "proxyWallet": WALLET,
        "timestamp": timestamp,
        "type": event_type,
        "transactionHash": tx_hash,
        "size": size,
        "usdcSize": usdc_size,
        "price": 1.0 if event_type == "TRADE" else 0,
        "asset": "123" if event_type == "TRADE" else "",
        "side": "BUY" if event_type == "TRADE" else "",
        "conditionId": "0xcondlate",
        "outcomeIndex": 0,
    }


class StagedActivityTransport(httpx.BaseTransport):
    def __init__(self, stages: list[list[dict]]) -> None:
        self.stages = stages
        self.requests: list[dict[str, str]] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        self.requests.append(params)

        stage_index = min(len(self.requests) - 1, len(self.stages) - 1)
        start = int(params["start"])
        end = int(params["end"])
        rows = [
            row
            for row in self.stages[stage_index]
            if start <= int(row["timestamp"]) <= end
        ]
        rows.sort(key=lambda row: int(row["timestamp"]), reverse=True)
        return httpx.Response(200, json=rows)


def test_incremental_overlap_catches_late_arrival_without_duplicate_ledger_rows(
    settings, session, monkeypatch
):
    already_seen = _activity_row(tx_hash="0xalreadyseen", timestamp=1880)
    late_redeem = _activity_row(
        tx_hash="0xlatearrival",
        timestamp=1910,
        event_type="REDEEM",
        size=92363.0,
        usdc_size=92363.0,
    )
    transport = StagedActivityTransport(
        stages=[
            [already_seen],
            [already_seen, late_redeem],
        ]
    )
    source = DataApiSource(
        client=httpx.Client(base_url="https://fake", transport=transport),
        sleep_fn=lambda _: None,
    )
    raw_store = RawStore(settings, session)

    manager.add_wallet(session, WALLET)
    manager.complete_backfill(session, WALLET, cursor_ts=0, up_to_ts=1700)

    monkeypatch.setattr("pmresearch.walletmanager.sync.time.time", lambda: 2000)
    first_outcome = sync_runner.run_incremental(
        session, settings, raw_store, source, WALLET
    )
    first_ingest = run_ingest(session, wallet=WALLET)

    assert first_outcome.rows_fetched == 1
    assert first_ingest.events_seen == 1
    assert first_ingest.events_inserted == 1
    assert manager.get_sync_state(session, WALLET).last_incremental_ts == 1940

    monkeypatch.setattr("pmresearch.walletmanager.sync.time.time", lambda: 2060)
    second_outcome = sync_runner.run_incremental(
        session, settings, raw_store, source, WALLET
    )
    second_ingest = run_ingest(session, wallet=WALLET)

    assert second_outcome.rows_fetched == 2
    assert second_ingest.events_seen == 2
    assert second_ingest.events_inserted == 1
    assert manager.get_sync_state(session, WALLET).last_incremental_ts == 2000

    assert int(transport.requests[0]["start"]) == 1400
    assert int(transport.requests[0]["end"]) == 1940
    assert int(transport.requests[1]["start"]) == 1640
    assert int(transport.requests[1]["end"]) == 2000

    rows = session.execute(
        text(
            "SELECT tx_hash, event_type, ts FROM wallet_events "
            "WHERE wallet = :w ORDER BY tx_hash"
        ),
        {"w": WALLET},
    ).fetchall()
    assert [(row.tx_hash, row.event_type, row.ts) for row in rows] == [
        ("0xalreadyseen", "TRADE", 1880),
        ("0xlatearrival", "REDEEM", 1910),
    ]
