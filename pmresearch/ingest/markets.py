"""Upsert mutable Gamma market/event dimensions."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from ..exposure.descriptors import derive_structure_type


@dataclass(frozen=True)
class MarketSyncStats:
    requested_conditions: int
    markets_upserted: int
    tokens_upserted: int
    events_upserted: int
    missing_conditions: int

    @staticmethod
    def empty() -> "MarketSyncStats":
        return MarketSyncStats(0, 0, 0, 0, 0)

    def merge(self, other: "MarketSyncStats") -> "MarketSyncStats":
        return MarketSyncStats(
            requested_conditions=self.requested_conditions + other.requested_conditions,
            markets_upserted=self.markets_upserted + other.markets_upserted,
            tokens_upserted=self.tokens_upserted + other.tokens_upserted,
            events_upserted=self.events_upserted + other.events_upserted,
            missing_conditions=self.missing_conditions + other.missing_conditions,
        )


def parse_jsonish_list(value: object) -> list:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return []
        return decoded if isinstance(decoded, list) else []
    return []


def _json_dumps(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _bool_int(value: object) -> int:
    return 1 if bool(value) else 0


def _decimal_string(value: object) -> str:
    try:
        return str(Decimal(str(value)))
    except (InvalidOperation, ValueError):
        return str(value)


def _event_from_market(market: dict) -> dict | None:
    events = market.get("events")
    if isinstance(events, list) and events:
        return events[0]
    event_id = market.get("eventId") or market.get("event_id")
    if event_id:
        return {"id": event_id, "title": None, "slug": None, "negRisk": market.get("negRisk")}
    return None


def _event_id(market: dict) -> str | None:
    event = _event_from_market(market)
    if event is None:
        return None
    event_id = event.get("id")
    return str(event_id) if event_id is not None else None


def _resolution_prices_json(market: dict, token_ids: list, outcome_prices: list) -> str | None:
    if not bool(market.get("closed")):
        return None
    if not token_ids or len(token_ids) != len(outcome_prices):
        return None
    return _json_dumps(
        {str(token_id): _decimal_string(price) for token_id, price in zip(token_ids, outcome_prices)}
    )


def _upsert_event(session: Session, event: dict | None, *, fallback_neg_risk: bool = False) -> bool:
    if event is None or event.get("id") is None:
        return False

    tags = event.get("tags") or event.get("tagsJson") or []
    session.execute(
        text(
            "INSERT INTO pm_events (event_id, title, slug, neg_risk, tags_json) "
            "VALUES (:event_id, :title, :slug, :neg_risk, :tags_json) "
            "ON CONFLICT(event_id) DO UPDATE SET "
            "title = excluded.title, slug = excluded.slug, neg_risk = excluded.neg_risk, "
            "tags_json = excluded.tags_json"
        ),
        {
            "event_id": str(event["id"]),
            "title": event.get("title"),
            "slug": event.get("slug"),
            "neg_risk": _bool_int(event.get("negRisk") or fallback_neg_risk),
            "tags_json": _json_dumps(tags),
        },
    )
    return True


# Canonical top-level categories, matched in the order Gamma lists an event's
# tags. Gamma tags mix broad categories with narrow topical tags (e.g. a
# Kraken IPO event carries ["Crypto", "exchange", "Finance", "Business", ...])
# with no field marking which tag is the "primary" one, so we pick the first
# tag that matches this allowlist.
_CANONICAL_CATEGORIES = [
    "Politics",
    "Sports",
    "Crypto",
    "Business",
    "Culture",
    "Science",
    "World",
    "Elections",
    "Tech",
    "Entertainment",
    "Economy",
    "Finance",
]
_CANONICAL_CATEGORY_LOOKUP = {label.lower(): label for label in _CANONICAL_CATEGORIES}


def derive_category(tags: list[dict]) -> str | None:
    for tag in tags:
        label = tag.get("label") if isinstance(tag, dict) else None
        if not label:
            continue
        canonical = _CANONICAL_CATEGORY_LOOKUP.get(str(label).lower())
        if canonical:
            return canonical
    return None


def upsert_event_category(session: Session, event: dict) -> int:
    """Upsert a fully-populated Gamma event (with tags) and propagate its
    derived category to every market that belongs to it. Returns the number
    of markets updated."""
    if not _upsert_event(session, event):
        return 0

    event_id = str(event["id"])
    category = derive_category(event.get("tags") or [])
    result = session.execute(
        text(
            "UPDATE markets SET category = :category, updated_at = :updated_at "
            "WHERE event_id = :event_id"
        ),
        {
            "category": category,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "event_id": event_id,
        },
    )
    return result.rowcount or 0


def upsert_market(session: Session, market: dict) -> tuple[int, int, int]:
    condition_id = market.get("conditionId") or market.get("condition_id")
    if not condition_id:
        return 0, 0, 0
    condition_id = str(condition_id).lower()
    outcomes = parse_jsonish_list(market.get("outcomes"))
    token_ids = parse_jsonish_list(market.get("clobTokenIds") or market.get("clob_token_ids_json"))
    outcome_prices = parse_jsonish_list(market.get("outcomePrices"))
    event = _event_from_market(market)
    event_id = _event_id(market)
    now = datetime.now(timezone.utc).isoformat()
    neg_risk = _bool_int(market.get("negRisk"))
    market_row = {
        "condition_id": condition_id,
        "question": market.get("question"),
        "slug": market.get("slug"),
        "category": market.get("category"),
        "event_id": event_id,
        "neg_risk": neg_risk,
        "outcomes_json": _json_dumps(outcomes),
        "clob_token_ids_json": _json_dumps([str(token_id) for token_id in token_ids]),
        "start_date": market.get("startDate") or market.get("start_date"),
        "end_date": market.get("endDate") or market.get("end_date"),
        "closed": _bool_int(market.get("closed")),
        "resolution_prices_json": _resolution_prices_json(market, token_ids, outcome_prices),
        "closed_time": market.get("closedTime") or market.get("closed_time"),
        "structure_type": derive_structure_type(
            {
                "clob_token_ids_json": _json_dumps([str(token_id) for token_id in token_ids]),
                "neg_risk": bool(neg_risk),
                "event_id": event_id,
            }
        ),
        "updated_at": now,
    }

    events_upserted = 1 if _upsert_event(session, event, fallback_neg_risk=bool(neg_risk)) else 0

    session.execute(
        text(
            "INSERT INTO markets "
            "(condition_id, question, slug, category, event_id, neg_risk, outcomes_json, "
            "clob_token_ids_json, start_date, end_date, closed, resolution_prices_json, "
            "closed_time, structure_type, updated_at) "
            "VALUES (:condition_id, :question, :slug, :category, :event_id, :neg_risk, "
            ":outcomes_json, :clob_token_ids_json, :start_date, :end_date, :closed, "
            ":resolution_prices_json, :closed_time, :structure_type, :updated_at) "
            "ON CONFLICT(condition_id) DO UPDATE SET "
            "question = excluded.question, slug = excluded.slug, "
            "category = COALESCE(excluded.category, markets.category), "
            "event_id = excluded.event_id, neg_risk = excluded.neg_risk, "
            "outcomes_json = excluded.outcomes_json, "
            "clob_token_ids_json = excluded.clob_token_ids_json, "
            "start_date = excluded.start_date, end_date = excluded.end_date, "
            "closed = excluded.closed, resolution_prices_json = excluded.resolution_prices_json, "
            "closed_time = excluded.closed_time, structure_type = excluded.structure_type, "
            "updated_at = excluded.updated_at"
        ),
        market_row,
    )

    tokens_upserted = 0
    for index, token_id in enumerate(token_ids):
        token_id = str(token_id)
        label = outcomes[index] if index < len(outcomes) else None
        session.execute(
            text(
                "INSERT INTO tokens (token_id, condition_id, outcome_index, outcome_label) "
                "VALUES (:token_id, :condition_id, :outcome_index, :outcome_label) "
                "ON CONFLICT(token_id) DO UPDATE SET "
                "condition_id = excluded.condition_id, outcome_index = excluded.outcome_index, "
                "outcome_label = excluded.outcome_label"
            ),
            {
                "token_id": token_id,
                "condition_id": condition_id,
                "outcome_index": index,
                "outcome_label": str(label) if label is not None else None,
            },
        )
        tokens_upserted += 1

    return 1, tokens_upserted, events_upserted


def ledger_condition_ids(session: Session, *, missing_only: bool = False) -> list[str]:
    query = (
        "SELECT DISTINCT lower(we.condition_id) AS condition_id "
        "FROM wallet_events we "
        "WHERE we.condition_id IS NOT NULL AND we.condition_id != ''"
    )
    if missing_only:
        query += (
            " AND NOT EXISTS ("
            "SELECT 1 FROM markets m WHERE m.condition_id = lower(we.condition_id)"
            ")"
        )
    query += " ORDER BY condition_id"
    return [row.condition_id for row in session.execute(text(query)).fetchall()]


def incremental_market_condition_ids(session: Session) -> list[str]:
    """Conditions whose Gamma metadata can affect current projections.

    This is the fast path for routine syncs after new wallet activity lands:
    fetch markets/tokens that are missing locally, plus markets we still
    believe are open. Closed markets with missing resolution prices are
    included because that metadata affects valuation/settlement projections.
    Other closed markets should not be re-fetched unless the caller explicitly
    asks for a full refresh.
    """
    rows = session.execute(
        text(
            "SELECT DISTINCT lower(we.condition_id) AS condition_id "
            "FROM wallet_events we "
            "LEFT JOIN markets m ON m.condition_id = lower(we.condition_id) "
            "WHERE we.condition_id IS NOT NULL AND we.condition_id != '' "
            "AND ("
            "m.condition_id IS NULL "
            "OR NOT EXISTS ("
            "SELECT 1 FROM tokens t WHERE t.condition_id = lower(we.condition_id)"
            ") "
            "OR COALESCE(m.closed, 0) = 0 "
            "OR (m.closed = 1 AND m.resolution_prices_json IS NULL)"
            ") "
            "ORDER BY condition_id"
        )
    ).fetchall()
    return [row.condition_id for row in rows]


def event_ids_for_conditions(session: Session, condition_ids: list[str]) -> list[str]:
    normalized = sorted({condition_id.lower() for condition_id in condition_ids if condition_id})
    if not normalized:
        return []
    rows = session.execute(
        text(
            "SELECT DISTINCT event_id FROM markets "
            "WHERE condition_id IN :condition_ids AND event_id IS NOT NULL"
        ).bindparams(bindparam("condition_ids", expanding=True)),
        {"condition_ids": normalized},
    ).fetchall()
    return [row.event_id for row in rows]


def missing_market_count(session: Session) -> int:
    return int(
        session.execute(
            text(
                "SELECT COUNT(*) FROM ("
                "SELECT DISTINCT lower(we.condition_id) AS condition_id "
                "FROM wallet_events we "
                "LEFT JOIN markets m ON m.condition_id = lower(we.condition_id) "
                "WHERE we.condition_id IS NOT NULL AND we.condition_id != '' "
                "AND m.condition_id IS NULL"
                ") missing"
            )
        ).scalar()
        or 0
    )


def upsert_market_payloads(
    session: Session, payloads: tuple[list[dict], ...], requested_conditions: list[str]
) -> MarketSyncStats:
    markets_upserted = 0
    tokens_upserted = 0
    events_upserted = 0
    seen_conditions: set[str] = set()

    for payload in payloads:
        for market in payload:
            market_count, token_count, event_count = upsert_market(session, market)
            markets_upserted += market_count
            tokens_upserted += token_count
            events_upserted += event_count
            condition_id = market.get("conditionId") or market.get("condition_id")
            if condition_id:
                seen_conditions.add(str(condition_id).lower())

    requested = {condition_id.lower() for condition_id in requested_conditions if condition_id}
    session.commit()
    return MarketSyncStats(
        requested_conditions=len(requested),
        markets_upserted=markets_upserted,
        tokens_upserted=tokens_upserted,
        events_upserted=events_upserted,
        missing_conditions=len(requested - seen_conditions),
    )
