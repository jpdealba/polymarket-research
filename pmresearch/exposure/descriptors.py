"""Market Structure Descriptors.

Descriptors are deliberately label-agnostic: never infer structure from
outcome text such as "Yes", "No", team names, or candidate names. Use only
machine structure: token count and explicit Gamma flags.
"""

from __future__ import annotations

import json


STRUCTURE_BINARY = "binary"
STRUCTURE_NEG_RISK_EVENT_MEMBER = "negRisk-event-member"
STRUCTURE_UNCLASSIFIED = "unclassified"


def _as_list(value: object) -> list:
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


def derive_structure_type(market: dict) -> str:
    token_ids = _as_list(market.get("clob_token_ids_json") or market.get("clobTokenIds"))
    neg_risk = bool(market.get("neg_risk") or market.get("negRisk"))
    event_id = market.get("event_id") or market.get("eventId")

    if len(token_ids) == 2 and neg_risk and event_id:
        return STRUCTURE_NEG_RISK_EVENT_MEMBER
    if len(token_ids) == 2:
        return STRUCTURE_BINARY
    return STRUCTURE_UNCLASSIFIED

