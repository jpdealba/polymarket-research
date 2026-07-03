"""Pluggable mark source interface."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol

from sqlalchemy.orm import Session


@dataclass(frozen=True)
class Mark:
    token_id: str
    ts: int
    price: Decimal
    source: str
    mark_age_s: int
    stale: bool
    meta: dict


class MarkSource(Protocol):
    name: str

    def get_mark(self, session: Session, token_id: str, ts: int) -> Mark | None:
        """Return a mark for token_id at target unix timestamp ts, or None."""
