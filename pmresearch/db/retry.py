"""App-level retry for SQLite writes under sustained external write pressure.

`busy_timeout` (see `engine.py`) has SQLite itself keep retrying lock
acquisition for a bounded window, but when a competing writer (the
always-on collector's book sampler, ticking every few seconds) is writing
continuously, a single long busy-wait can still lose every race. This wraps
a write in an outer retry loop with backoff + jitter so a `database is
locked` failure is retried against a fresh attempt instead of aborting a
multi-minute rebuild outright.
"""

from __future__ import annotations

import logging
import random
import time
from typing import Callable, TypeVar

from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

T = TypeVar("T")


def retry_locked(
    session: Session,
    fn: Callable[[], T],
    *,
    max_attempts: int = 12,
    base_delay: float = 1.0,
    max_delay: float = 15.0,
) -> T:
    """Run `fn` (a closure that writes via `session`), retrying on
    'database is locked'. Rolls back the session before each retry since a
    failed statement leaves the transaction unusable."""
    attempt = 0
    while True:
        try:
            return fn()
        except OperationalError as exc:
            if "database is locked" not in str(exc).lower() or attempt >= max_attempts - 1:
                raise
            session.rollback()
            delay = min(max_delay, base_delay * (2**attempt)) * (0.5 + random.random())
            logger.warning(
                "database is locked, retrying in %.1fs (attempt %d/%d)",
                delay, attempt + 1, max_attempts,
            )
            time.sleep(delay)
            attempt += 1
