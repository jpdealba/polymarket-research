"""Projection contract (CONTEXT.md "Projection", ADR 0002).

A projection is derived state computed by replaying the ledger. Disposable by
contract: written only by rebuild, droppable and rebuildable at any time,
never a source of truth. MVP projections rebuild from scratch — rebuilds are
cheap; incremental apply can be added later without changing the contract.

Each projection stamps its rows with `version`; bumping the version after a
semantics change marks all previously-built rows as stale-by-inspection.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from sqlalchemy.orm import Session


class Projection(ABC):
    name: str
    version: int

    @abstractmethod
    def rebuild(self, session: Session, wallet: str):
        """Drop and rebuild this projection's rows for one wallet.

        Returns projection-specific rebuild stats."""
