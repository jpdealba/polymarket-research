"""Phase 5 acceptance criterion: reconciliation runs automatically after each
incremental sync cycle, not only when invoked manually via `pmr reconcile run`."""

from sqlalchemy import text

from pmresearch.sources.dataapi import FetchOutcome
from pmresearch.walletmanager import manager, scheduler

WALLET = "0xschedreconcileaaaaaaaaaaaaaaaaaaaaaaaaaa"


class _FakeDataApiSource:
    def __init__(self, *args, **kwargs) -> None:
        self.closed = False

    def fetch_positions(self, raw_store, wallet):
        from types import SimpleNamespace

        return SimpleNamespace(positions=(), rows_fetched=0)

    def close(self) -> None:
        self.closed = True


def test_incremental_cycle_reconciles_wallet_with_new_activity(settings, session, monkeypatch):
    manager.add_wallet(session, WALLET)
    session.commit()

    monkeypatch.setattr(scheduler, "DataApiSource", _FakeDataApiSource)
    monkeypatch.setattr(
        scheduler.sync_runner,
        "run_incremental",
        lambda session, settings, raw_store, source, address: FetchOutcome(
            (), 1, 100, 100, 1
        ),
    )

    scheduler.run_incremental_cycle(settings)

    row = session.execute(
        text("SELECT wallet, status FROM wallet_trust WHERE wallet = :w"),
        {"w": WALLET.lower()},
    ).fetchone()
    assert row is not None, "expected run_incremental_cycle to trigger reconciliation and persist wallet_trust"
    assert row.status == "trusted"


def test_incremental_cycle_skips_reconciliation_when_no_new_activity(settings, session, monkeypatch):
    manager.add_wallet(session, WALLET)
    session.commit()

    monkeypatch.setattr(scheduler, "DataApiSource", _FakeDataApiSource)
    monkeypatch.setattr(
        scheduler.sync_runner,
        "run_incremental",
        lambda session, settings, raw_store, source, address: FetchOutcome.empty(),
    )

    scheduler.run_incremental_cycle(settings)

    row = session.execute(
        text("SELECT wallet FROM wallet_trust WHERE wallet = :w"),
        {"w": WALLET.lower()},
    ).fetchone()
    assert row is None, "reconciliation should be skipped when the sync fetched no new rows"


def test_incremental_cycle_skips_reconciliation_when_sync_fails(settings, session, monkeypatch):
    manager.add_wallet(session, WALLET)
    session.commit()

    monkeypatch.setattr(scheduler, "DataApiSource", _FakeDataApiSource)

    def _boom(session, settings, raw_store, source, address):
        raise RuntimeError("simulated sync failure")

    monkeypatch.setattr(scheduler.sync_runner, "run_incremental", _boom)

    scheduler.run_incremental_cycle(settings)

    row = session.execute(
        text("SELECT wallet FROM wallet_trust WHERE wallet = :w"),
        {"w": WALLET.lower()},
    ).fetchone()
    assert row is None, "reconciliation should not run against a wallet whose sync just failed"
