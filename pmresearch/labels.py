"""Label resolution for condition/event/token IDs. Pure SQL lookups against
the markets/pm_events/tokens tables — no new computation, just metadata fetches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text


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


def resolve_market_labels(session, condition_ids: list[str]) -> dict[str, MarketLabel]:
    """Batch-resolve condition_id -> MarketLabel."""
    if not condition_ids:
        return {}
    placeholders = ", ".join(f":c{i}" for i in range(len(condition_ids)))
    rows = session.execute(
        text(
            f"SELECT condition_id, question, category, outcomes_json, event_id "
            f"FROM markets WHERE condition_id IN ({placeholders})"
        ),
        {f"c{i}": cid for i, cid in enumerate(condition_ids)},
    ).fetchall()
    result = {}
    for r in rows:
        import json

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
    placeholders = ", ".join(f":e{i}" for i in range(len(event_ids)))
    rows = session.execute(
        text(
            f"SELECT event_id, title, slug "
            f"FROM pm_events WHERE event_id IN ({placeholders})"
        ),
        {f"e{i}": eid for i, eid in enumerate(event_ids)},
    ).fetchall()
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
