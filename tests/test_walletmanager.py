from pmresearch.walletmanager import manager


def test_wallet_add_remove_idempotent(session):
    assert manager.add_wallet(session, "0xAAA") is True
    assert manager.add_wallet(session, "0xaaa") is False  # idempotent, case-insensitive

    rows = manager.list_wallets(session)
    assert [r.address for r in rows] == ["0xaaa"]

    assert manager.remove_wallet(session, "0xaaa") is True
    assert manager.remove_wallet(session, "0xaaa") is False
    assert manager.list_wallets(session) == []

    # Re-adding after removal reactivates the same watchlist row.
    assert manager.add_wallet(session, "0xaaa") is True
    assert [r.address for r in manager.list_wallets(session)] == ["0xaaa"]


def test_sync_state_transitions(session):
    manager.add_wallet(session, "0xbbb")
    state = manager.get_sync_state(session, "0xbbb")
    assert state.status == "new"
    assert state.backfill_complete is False

    manager.start_backfill(session, "0xbbb")
    assert manager.get_sync_state(session, "0xbbb").status == "backfilling"

    manager.complete_backfill(session, "0xbbb", cursor_ts=100, up_to_ts=200)
    state = manager.get_sync_state(session, "0xbbb")
    assert state.status == "complete"
    assert state.backfill_complete is True
    assert state.backfill_cursor_ts == 100
    assert state.last_incremental_ts == 200

    manager.record_incremental_success(session, "0xbbb", up_to_ts=300)
    state = manager.get_sync_state(session, "0xbbb")
    assert state.status == "incremental"
    assert state.last_incremental_ts == 300
    assert state.consecutive_failures == 0


def test_backfill_checkpoint_is_monotonic_when_backfill_walks_backwards(session):
    manager.add_wallet(session, "0xbeef")
    manager.start_backfill(session, "0xbeef", high_bound=1_000)

    manager.checkpoint_backfill(session, "0xbeef", cursor_ts=900)
    assert manager.get_sync_state(session, "0xbeef").backfill_cursor_ts == 900

    manager.checkpoint_backfill(session, "0xbeef", cursor_ts=950)
    assert manager.get_sync_state(session, "0xbeef").backfill_cursor_ts == 900

    manager.checkpoint_backfill(session, "0xbeef", cursor_ts=800)
    assert manager.get_sync_state(session, "0xbeef").backfill_cursor_ts == 800


def test_failure_increments_counter(session):
    manager.add_wallet(session, "0xccc")

    manager.record_failure(session, "0xccc", "boom")
    state = manager.get_sync_state(session, "0xccc")
    assert state.consecutive_failures == 1
    assert state.last_error == "boom"
    assert state.status == "error"

    manager.record_failure(session, "0xccc", "boom again")
    assert manager.get_sync_state(session, "0xccc").consecutive_failures == 2

    manager.record_incremental_success(session, "0xccc", up_to_ts=42)
    state = manager.get_sync_state(session, "0xccc")
    assert state.consecutive_failures == 0
    assert state.last_error is None


def test_next_action(session):
    from pmresearch.walletmanager.manager import SyncAction

    manager.add_wallet(session, "0xddd")
    assert manager.next_action(session, "0xddd") is SyncAction.BACKFILL

    manager.complete_backfill(session, "0xddd", cursor_ts=1, up_to_ts=2)
    assert manager.next_action(session, "0xddd") is SyncAction.INCREMENTAL
