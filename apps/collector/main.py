"""Thin collector entrypoint — delegates to pmresearch.walletmanager.scheduler.
No logic lives here; apps/* are thin shells over the core library (ADR 0004)."""

from __future__ import annotations

from pmresearch.walletmanager.scheduler import run_forever

if __name__ == "__main__":
    run_forever()
