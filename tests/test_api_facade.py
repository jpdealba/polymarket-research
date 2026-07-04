"""Unit tests proving `pmresearch.api` functions are thin pass-throughs over
the library, using the same `settings`/`session` fixtures as the rest of the
suite (tests/conftest.py)."""

from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import text

from pmresearch import api
from pmresearch.walletmanager.manager import add_wallet


def _insert_event(session, *, wallet, event_id_seed, event_type="TRADE", ts=1000):
    session.execute(
        text(
            "INSERT INTO raw_fetches "
            "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count, ingested_at) "
            f"VALUES ('fixture', 'activity', '{{}}', 'now', 200, 'fixture', 'hash-{event_id_seed}', 1, 'now')"
        )
    )
    raw_id = session.execute(text("SELECT max(id) FROM raw_fetches")).scalar()
    session.execute(
        text(
            "INSERT INTO wallet_events "
            "(wallet, event_type, ts, tx_hash, condition_id, token_id, side, delta_shares, "
            "delta_usdc, price, usdc_size, source, is_derived, raw_ref, dedupe_key, ingested_at) "
            "VALUES (:wallet, :event_type, :ts, :tx_hash, NULL, '101', 'BUY', "
            "'10', '-5', '0.5', '5', 'fixture', 0, :raw_id, :dedupe_key, 'now')"
        ),
        {
            "wallet": wallet,
            "event_type": event_type,
            "ts": ts,
            "tx_hash": f"0xtx{event_id_seed}",
            "raw_id": raw_id,
            "dedupe_key": f"dedupe-{event_id_seed}",
        },
    )
    session.commit()


def test_list_wallets_reflects_added_wallet(session):
    add_wallet(session, "0xFacadeWallet", display_name="Facade Test")

    wallets = api.list_wallets(session, active_only=False)

    addresses = [w.address for w in wallets]
    assert "0xfacadewallet" in addresses


def test_ledger_event_counts(session):
    wallet = "0xledgereventcounts"
    _insert_event(session, wallet=wallet, event_id_seed=1, event_type="TRADE", ts=100)
    _insert_event(session, wallet=wallet, event_id_seed=2, event_type="TRADE", ts=200)
    _insert_event(session, wallet=wallet, event_id_seed=3, event_type="REWARD", ts=300)

    counts = api.ledger_event_counts(session, wallet)

    by_type = {row.event_type: row.cnt for row in counts}
    assert by_type["TRADE"] == 2
    assert by_type["REWARD"] == 1


def test_list_wallet_events_pagination_and_filter(session):
    wallet = "0xlistwalletevents"
    for i in range(5):
        _insert_event(session, wallet=wallet, event_id_seed=10 + i, event_type="TRADE", ts=1000 + i)
    _insert_event(session, wallet=wallet, event_id_seed=99, event_type="REWARD", ts=2000)

    page1 = api.list_wallet_events(session, wallet, limit=2, offset=0)
    page2 = api.list_wallet_events(session, wallet, limit=2, offset=2)

    assert len(page1) == 2
    assert len(page2) == 2
    # Ordered ts DESC, id DESC: most recent event (REWARD, ts=2000) comes first.
    assert page1[0].event_type == "REWARD"
    assert page1[0].id != page2[0].id

    trades_only = api.list_wallet_events(session, wallet, limit=100, offset=0, event_type="TRADE")
    assert len(trades_only) == 5
    assert all(row.event_type == "TRADE" for row in trades_only)


def test_open_session_closes_and_is_usable(settings):
    patcher = None
    close_spy = None
    with api.open_session(settings) as session:
        result = session.execute(text("SELECT 1")).scalar()
        assert result == 1
        patcher = patch.object(session, "close", wraps=session.close)
        close_spy = patcher.start()

    patcher.stop()
    close_spy.assert_called_once()
