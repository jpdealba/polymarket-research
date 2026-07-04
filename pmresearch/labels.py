"""Label resolution for condition/event/token IDs. Pure SQL lookups against
the markets/pm_events/tokens tables — no new computation, just metadata fetches."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text

_CHUNK_SIZE = 500


@dataclass(frozen=True)
class MarketLabel:
    condition_id: str
    question: str
    category: Optional[str]
    outcomes: list[str]
    event_id: Optional[str]


@dataclass(frozen=True)
class EventLabel:
    event_id: str
    title: str
    slug: Optional[str]


def _chunked_select(
    session,
    base_query: str,
    ids: list[str],
    col: str,
    prefix: str,
) -> list:
    """Run ``SELECT ... WHERE col IN (...)`` in chunks to stay within
    SQLite's 999-bound-parameter limit."""
    all_rows: list = []
    for start in range(0, len(ids), _CHUNK_SIZE):
        chunk = ids[start : start + _CHUNK_SIZE]
        placeholders = ", ".join(f":{prefix}{i}" for i in range(len(chunk)))
        params = {f"{prefix}{i}": v for i, v in enumerate(chunk)}
        rows = session.execute(
            text(f"{base_query} WHERE {col} IN ({placeholders})"), params
        ).fetchall()
        all_rows.extend(rows)
    return all_rows


def resolve_market_labels(session, condition_ids: list[str]) -> dict[str, MarketLabel]:
    """Batch-resolve condition_id -> MarketLabel."""
    if not condition_ids:
        return {}
    rows = _chunked_select(
        session,
        "SELECT condition_id, question, category, outcomes_json, event_id FROM markets",
        condition_ids,
        "condition_id",
        "c",
    )
    result = {}
    for r in rows:
        outcomes = json.loads(r.outcomes_json) if r.outcomes_json else []
        result[r.condition_id] = MarketLabel(
            condition_id=r.condition_id,
            question=r.question or r.condition_id,
            category=r.category,
            outcomes=outcomes,
            event_id=r.event_id,
        )
    return result


def resolve_event_labels(session, event_ids: list[str]) -> dict[str, EventLabel]:
    """Batch-resolve event_id -> EventLabel."""
    if not event_ids:
        return {}
    rows = _chunked_select(
        session,
        "SELECT event_id, title, slug FROM pm_events",
        event_ids,
        "event_id",
        "e",
    )
    return {
        r.event_id: EventLabel(
            event_id=r.event_id,
            title=r.title or r.event_id,
            slug=r.slug,
        )
        for r in rows
    }


def get_market_question(session, condition_id: str) -> str:
    """Single condition_id -> question text."""
    row = session.execute(
        text("SELECT question FROM markets WHERE condition_id = :c"),
        {"c": condition_id},
    ).fetchone()
    return row.question if row else condition_id


def get_event_title(session, event_id: str) -> str:
    """Single event_id -> title text."""
    row = session.execute(
        text("SELECT title FROM pm_events WHERE event_id = :e"),
        {"e": event_id},
    ).fetchone()
    return row.title if row else event_id
