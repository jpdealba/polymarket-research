"""Phase 11 enrichment join: attach subgraph/RPC OrderFilled fills onto ledger
`wallet_events` TRADE rows.

Matching key is **(tx_hash, wallet, asset token_id, amount)** — never tx_hash
alone, because one transaction legitimately contains many fills. Subgraph
amounts are 6-decimal integers; we convert them to Decimal shares and compare
against the ledger's `abs(delta_shares)` within a rounding tolerance.

If a single fill maps to more than one candidate ledger row (e.g. two
identical fills in the same tx for the same wallet+asset), those rows are left
UNENRICHED and counted as ambiguous — never force-matched (ADR 0006).

Enrichment NEVER creates or deletes ledger events; it only inserts into
`fill_enrichment` (idempotently) and advances `enrichment_watermarks`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ..config import Settings
from ..rawstore.store import RawStore

logger = logging.getLogger(__name__)

# Shares agree to 6 decimals on both sides; allow one unit of rounding slack.
MATCH_TOLERANCE = Decimal("0.000001")

EXCHANGE_COUNTERPARTIES = frozenset(
    {
        "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
        "0xc5d563a36ae78145c45a50134d48a1215220f80a",
        "0xe111180000d2663c0091e4f400237545b87b996b",
        "0xe2222d279d744050d28e00520010520000310f59",
    }
)


@dataclass(frozen=True)
class EnrichmentStats:
    fills_seen: int = 0
    enriched: int = 0
    ambiguous: int = 0
    unmatched: int = 0
    already_enriched: int = 0
    head_ts: int = 0

    def _add(self, **kw) -> "EnrichmentStats":
        return EnrichmentStats(
            fills_seen=self.fills_seen + kw.get("fills_seen", 0),
            enriched=self.enriched + kw.get("enriched", 0),
            ambiguous=self.ambiguous + kw.get("ambiguous", 0),
            unmatched=self.unmatched + kw.get("unmatched", 0),
            already_enriched=self.already_enriched + kw.get("already_enriched", 0),
            head_ts=max(self.head_ts, kw.get("head_ts", 0)),
        )


@dataclass(frozen=True)
class _Candidate:
    event_id: int
    abs_shares: Decimal
    abs_usdc: Decimal


@dataclass(frozen=True)
class _FillCandidate:
    event_id: int
    role: str
    order_hash: str
    fee: str
    counterparty: Optional[str]
    source: str
    enriched_at: str
    provenance: str


@dataclass(frozen=True)
class _ResolvedEnrichment:
    event_id: int
    role: str
    order_hash: str
    fee: str
    counterparty: Optional[str]
    source: str
    enriched_at: str


def _trade_candidates(
    session: Session, wallet: str, tx_hash: str, token_id: str
) -> list[_Candidate]:
    rows = session.execute(
        text(
            "SELECT id, delta_shares, delta_usdc, usdc_size FROM wallet_events "
            "WHERE wallet = :wallet AND event_type = 'TRADE' "
            "AND lower(tx_hash) = :tx AND token_id = :token"
        ),
        {"wallet": wallet.lower(), "tx": tx_hash.lower(), "token": token_id},
    ).fetchall()
    return [
        _Candidate(
            row.id,
            abs(Decimal(row.delta_shares)),
            _ledger_abs_usdc(row.delta_usdc, row.usdc_size),
        )
        for row in rows
    ]


def _ledger_abs_usdc(delta_usdc: object, usdc_size: object) -> Decimal:
    size = abs(Decimal(str(usdc_size or "0")))
    if size != 0:
        return size
    return abs(Decimal(str(delta_usdc or "0")))


def _match(
    candidates: list[_Candidate], fill_shares: Decimal, fill_usdc: Optional[Decimal] = None
) -> tuple[Optional[int], bool]:
    """Return (event_id, ambiguous). event_id is None when no candidate matches
    the amount; ambiguous=True when more than one does."""
    hits = [c for c in candidates if abs(c.abs_shares - fill_shares) <= MATCH_TOLERANCE]
    if len(hits) == 1:
        return hits[0].event_id, False
    if len(hits) > 1:
        if fill_usdc is not None and fill_usdc != 0 and any(c.abs_usdc != 0 for c in hits):
            usdc_hits = [
                c for c in hits if abs(c.abs_usdc - fill_usdc) <= MATCH_TOLERANCE
            ]
            if len(usdc_hits) == 1:
                return usdc_hits[0].event_id, False
            if len(usdc_hits) == 0:
                return None, False
        return None, True
    return None, False


def _classify_fill_role(fill, wallet: str) -> tuple[Optional[str], Optional[str], str]:
    maker = str(fill.maker).lower()
    taker = str(fill.taker).lower()
    evidence: list[tuple[str, Optional[str], str]] = []

    if maker == wallet:
        if taker in EXCHANGE_COUNTERPARTIES:
            evidence.append(("taker", taker, "maker_exchange_taker_order"))
        else:
            evidence.append(("maker", taker, "maker"))

    if taker == wallet:
        if maker in EXCHANGE_COUNTERPARTIES:
            evidence.append(("ambiguous", maker, "taker_exchange_counterparty"))
        else:
            evidence.append(("taker", maker, "taker"))

    if not evidence:
        return None, None, "unmatched"

    roles = {role for role, _, _ in evidence}
    if len(roles) == 1 and "ambiguous" not in roles:
        return evidence[0]

    counterparty = next((cp for _, cp, _ in evidence if cp is not None), None)
    provenance = "+".join(sorted({prov for _, _, prov in evidence}))
    return "ambiguous", counterparty, provenance


def _candidate_sort_key(candidate: _FillCandidate) -> tuple[int, int, str, str, str]:
    provenance_priority = {
        "taker": 0,
        "maker": 1,
        "maker_exchange_taker_order": 2,
    }.get(candidate.provenance, 9)
    exchange_counterparty = (
        1 if candidate.counterparty in EXCHANGE_COUNTERPARTIES else 0
    )
    return (
        exchange_counterparty,
        provenance_priority,
        candidate.source,
        candidate.order_hash,
        candidate.counterparty or "",
    )


def _resolve_event_candidates(
    event_id: int, candidates: list[_FillCandidate]
) -> _ResolvedEnrichment:
    roles = {candidate.role for candidate in candidates}
    if len(roles) == 1 and "ambiguous" not in roles:
        role = next(iter(roles))
        selectable = [candidate for candidate in candidates if candidate.role == role]
    else:
        role = "ambiguous"
        selectable = candidates

    chosen = sorted(selectable, key=_candidate_sort_key)[0]
    return _ResolvedEnrichment(
        event_id=event_id,
        role=role,
        order_hash=chosen.order_hash,
        fee=chosen.fee,
        counterparty=chosen.counterparty,
        source=chosen.source,
        enriched_at=chosen.enriched_at,
    )


def _upsert_enrichment(
    session: Session,
    *,
    event_id: int,
    role: str,
    order_hash: str,
    fee: str,
    counterparty: Optional[str],
    source: str,
    enriched_at: str,
) -> str:
    existing = session.execute(
        text(
            "SELECT role, order_hash, fee, counterparty, source "
            "FROM fill_enrichment WHERE event_id = :event_id"
        ),
        {"event_id": event_id},
    ).fetchone()
    if existing is not None:
        unchanged = (
            existing.role == role
            and existing.order_hash == order_hash
            and existing.fee == fee
            and existing.counterparty == counterparty
            and existing.source == source
        )
        if unchanged:
            return "unchanged"
        session.execute(
            text(
                "UPDATE fill_enrichment SET role = :role, order_hash = :order_hash, "
                "fee = :fee, counterparty = :counterparty, source = :source, "
                "enriched_at = :enriched_at WHERE event_id = :event_id"
            ),
            {
                "event_id": event_id,
                "role": role,
                "order_hash": order_hash,
                "fee": fee,
                "counterparty": counterparty,
                "source": source,
                "enriched_at": enriched_at,
            },
        )
        return "updated"

    session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, :role, :order_hash, :fee, :counterparty, :source, :enriched_at)"
        ),
        {
            "event_id": event_id,
            "role": role,
            "order_hash": order_hash,
            "fee": fee,
            "counterparty": counterparty,
            "source": source,
            "enriched_at": enriched_at,
        },
    )
    return "inserted"


def _prepare_candidate_store(session: Session) -> None:
    # Keep large replay runs out of Python heap; SQLite may spill temp tables to disk.
    session.execute(text("PRAGMA temp_store = FILE"))
    session.execute(text("DROP TABLE IF EXISTS temp.fill_enrichment_candidates"))
    session.execute(
        text(
            "CREATE TEMP TABLE fill_enrichment_candidates ("
            "event_id INTEGER NOT NULL, "
            "role TEXT NOT NULL, "
            "order_hash TEXT NOT NULL, "
            "fee TEXT, "
            "counterparty TEXT, "
            "source TEXT NOT NULL, "
            "enriched_at TEXT NOT NULL, "
            "provenance TEXT NOT NULL"
            ")"
        )
    )


def _flush_candidate_batch(session: Session, batch: list[dict[str, object]]) -> None:
    if not batch:
        return
    session.execute(
        text(
            "INSERT INTO temp.fill_enrichment_candidates "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at, provenance) "
            "VALUES (:event_id, :role, :order_hash, :fee, :counterparty, :source, "
            ":enriched_at, :provenance)"
        ),
        batch,
    )
    batch.clear()


def _candidate_from_row(row) -> _FillCandidate:
    return _FillCandidate(
        event_id=int(row.event_id),
        role=row.role,
        order_hash=row.order_hash,
        fee=row.fee,
        counterparty=row.counterparty,
        source=row.source,
        enriched_at=row.enriched_at,
        provenance=row.provenance,
    )


def _iter_resolved_enrichments(session: Session):
    rows = session.execute(
        text(
            "SELECT event_id, role, order_hash, fee, counterparty, source, enriched_at, provenance "
            "FROM temp.fill_enrichment_candidates "
            "ORDER BY event_id, role, counterparty, provenance, source, order_hash"
        )
    )
    current_event_id: Optional[int] = None
    current_candidates: list[_FillCandidate] = []
    for row in rows:
        event_id = int(row.event_id)
        if current_event_id is not None and event_id != current_event_id:
            yield _resolve_event_candidates(current_event_id, current_candidates)
            current_candidates = []
        current_event_id = event_id
        current_candidates.append(_candidate_from_row(row))
    if current_event_id is not None:
        yield _resolve_event_candidates(current_event_id, current_candidates)


def join_fills(
    session: Session,
    wallet: str,
    fills: Iterable,
    *,
    source: str,
    enriched_at: Optional[str] = None,
) -> EnrichmentStats:
    """Match `fills` (subgraph OrderFill or rpc OrderFilledLog) onto TRADE rows
    for `wallet` and insert enrichment rows. Idempotent."""
    wallet = wallet.lower()
    enriched_at = enriched_at or datetime.now(timezone.utc).isoformat()
    stats = EnrichmentStats()
    _prepare_candidate_store(session)
    candidate_batch: list[dict[str, object]] = []

    for fill in fills:
        stats = stats._add(fills_seen=1, head_ts=getattr(fill, "timestamp", 0) or 0)

        role, counterparty, provenance = _classify_fill_role(fill, wallet)
        if role is None:
            # Fill returned by the source but not actually involving this wallet.
            stats = stats._add(unmatched=1)
            continue

        candidates = _trade_candidates(session, wallet, fill.transaction_hash, fill.traded_token_id)
        event_id, ambiguous = _match(
            candidates,
            fill.traded_shares,
            getattr(fill, "collateral_usdc", None),
        )
        if ambiguous:
            logger.warning(
                "Ambiguous enrichment for wallet %s tx %s token %s amount %s usdc %s: "
                "multiple ledger candidates match; leaving unenriched.",
                wallet,
                fill.transaction_hash,
                fill.traded_token_id,
                fill.traded_shares,
                getattr(fill, "collateral_usdc", None),
            )
            stats = stats._add(ambiguous=1)
            continue
        if event_id is None:
            stats = stats._add(unmatched=1)
            continue

        candidate_batch.append(
            {
                "event_id": event_id,
                "role": role,
                "order_hash": str(fill.order_hash).lower(),
                "fee": str(fill.fee_decimal),
                "counterparty": counterparty.lower() if counterparty else None,
                "source": source,
                "enriched_at": enriched_at,
                "provenance": provenance,
            }
        )
        if len(candidate_batch) >= 5000:
            _flush_candidate_batch(session, candidate_batch)

    _flush_candidate_batch(session, candidate_batch)
    session.execute(
        text(
            "CREATE INDEX IF NOT EXISTS temp.ix_fill_enrichment_candidates_event_id "
            "ON fill_enrichment_candidates (event_id)"
        )
    )

    for resolved in _iter_resolved_enrichments(session):
        outcome = _upsert_enrichment(
            session,
            event_id=resolved.event_id,
            role=resolved.role,
            order_hash=resolved.order_hash,
            fee=resolved.fee,
            counterparty=resolved.counterparty,
            source=resolved.source,
            enriched_at=resolved.enriched_at,
        )
        if resolved.role == "ambiguous":
            if outcome == "unchanged":
                stats = stats._add(already_enriched=1)
            else:
                stats = stats._add(ambiguous=1)
        elif outcome in ("inserted", "updated"):
            stats = stats._add(enriched=1)
        else:
            stats = stats._add(already_enriched=1)

    session.execute(text("DROP TABLE IF EXISTS temp.fill_enrichment_candidates"))
    session.commit()
    return stats


def _update_watermark(
    session: Session, wallet: str, *, subgraph_ts: Optional[int] = None, rpc_block: Optional[int] = None
) -> None:
    wallet = wallet.lower()
    row = session.execute(
        text("SELECT wallet FROM enrichment_watermarks WHERE wallet = :w"), {"w": wallet}
    ).fetchone()
    if row is None:
        session.execute(
            text(
                "INSERT INTO enrichment_watermarks (wallet, subgraph_synced_to_ts, rpc_synced_to_block) "
                "VALUES (:w, :ts, :block)"
            ),
            {"w": wallet, "ts": subgraph_ts, "block": rpc_block},
        )
    else:
        if subgraph_ts is not None:
            session.execute(
                text(
                    "UPDATE enrichment_watermarks SET subgraph_synced_to_ts = "
                    "MAX(COALESCE(subgraph_synced_to_ts, 0), :ts) WHERE wallet = :w"
                ),
                {"w": wallet, "ts": subgraph_ts},
            )
        if rpc_block is not None:
            session.execute(
                text(
                    "UPDATE enrichment_watermarks SET rpc_synced_to_block = "
                    "MAX(COALESCE(rpc_synced_to_block, 0), :block) WHERE wallet = :w"
                ),
                {"w": wallet, "block": rpc_block},
            )
    session.commit()


def run_enrichment(
    session: Session,
    settings: Settings,
    wallet: str,
    *,
    source: str = "subgraph",
    subgraph=None,
    rpc=None,
    from_block: int = 0,
    to_block: Optional[int] = None,
    chunk_blocks: int = 2000,
    ignore_watermark: bool = False,
) -> EnrichmentStats:
    """Fetch fills for `wallet` from the chosen source and join them onto the
    ledger. `subgraph`/`rpc` may be injected for tests; otherwise they are
    constructed from settings (and error clearly when unconfigured)."""
    raw_store = RawStore(settings, session)

    if source == "subgraph":
        if subgraph is None:
            if not settings.subgraph_url:
                raise RuntimeError(
                    "Subgraph enrichment requested but PMR_SUBGRAPH_URL is not set."
                )
            from ..sources.subgraph import SubgraphSource

            subgraph = SubgraphSource(settings.subgraph_url)
        since_ts = _current_subgraph_ts(session, wallet)
        fetch = subgraph.fetch_order_fills(raw_store, wallet, since_ts=since_ts)
        stats = join_fills(session, wallet, fetch.fills, source="subgraph")
        stats = stats._add(head_ts=fetch.head_ts)
        _update_watermark(session, wallet, subgraph_ts=max(fetch.head_ts, since_ts))
        return stats

    if source in ("rpc", "polygonscan"):
        from ..sources.rpc import RpcError

        fetcher = rpc if rpc is not None else _build_block_fetcher(source, settings)
        if to_block is None:
            raise ValueError(f"{source} enrichment requires an explicit to_block.")

        # Resume: never re-scan blocks already covered by a prior run. The
        # watermark advances per chunk, so an interrupted run picks up where it
        # stopped (enrichment is idempotent, so overlap would be harmless too).
        resume_block = None if ignore_watermark else _current_rpc_block(session, wallet)
        start = from_block if resume_block is None else max(from_block, resume_block + 1)

        stats = EnrichmentStats()
        size = max(1, chunk_blocks)
        b = start
        while b <= to_block:
            end = min(b + size - 1, to_block)
            try:
                fetch = fetcher.fetch_order_filled_logs(
                    raw_store, wallet=wallet, from_block=b, to_block=end
                )
            except RpcError:
                # Provider capped the range/response. Halve and retry the same
                # start; give up only when a single block still fails.
                if size <= 1:
                    raise
                size = max(1, size // 2)
                logger.warning(
                    "getLogs [%d,%d] over provider limit; halving chunk to %d blocks.",
                    b, end, size,
                )
                continue
            chunk = join_fills(session, wallet, fetch.logs, source=source)
            stats = stats._add(
                fills_seen=chunk.fills_seen,
                enriched=chunk.enriched,
                ambiguous=chunk.ambiguous,
                unmatched=chunk.unmatched,
                already_enriched=chunk.already_enriched,
            )
            _update_watermark(session, wallet, rpc_block=end)
            b = end + 1
            if size < chunk_blocks:  # recover after a dense/capped patch
                size = min(chunk_blocks, size * 2)
        return stats

    raise ValueError(f"Unknown enrichment source: {source!r}")


def _build_block_fetcher(source: str, settings: Settings):
    if source == "rpc":
        if not settings.rpc_url:
            raise RuntimeError("RPC enrichment requested but PMR_RPC_URL is not set.")
        from ..sources.rpc import RpcSource

        return RpcSource(settings.rpc_url)

    if source == "polygonscan":
        if not settings.polygonscan_api_key:
            raise RuntimeError(
                "PolygonScan enrichment requested but PMR_POLYGONSCAN_API_KEY is not set."
            )
        from ..sources.polygonscan import PolygonscanSource

        return PolygonscanSource(settings.polygonscan_api_key)

    raise ValueError(f"Unknown block enrichment source: {source!r}")


def _current_rpc_block(session: Session, wallet: str) -> Optional[int]:
    row = session.execute(
        text("SELECT rpc_synced_to_block FROM enrichment_watermarks WHERE wallet = :w"),
        {"w": wallet.lower()},
    ).fetchone()
    if row is None or row.rpc_synced_to_block is None:
        return None
    return int(row.rpc_synced_to_block)


def max_rpc_watermark(session: Session, wallets: Iterable[str]) -> int:
    """Highest `rpc_synced_to_block` already recorded across `wallets`, or 0.

    Used as a floor for `get_block_number()`: the chain can't move
    backwards, so a fresh head reading below this floor means the read hit
    stale/inconsistent nodes, not that the chain regressed.
    """
    best = 0
    for wallet in wallets:
        block = _current_rpc_block(session, wallet)
        if block is not None:
            best = max(best, block)
    return best


def _current_subgraph_ts(session: Session, wallet: str) -> int:
    row = session.execute(
        text(
            "SELECT subgraph_synced_to_ts FROM enrichment_watermarks WHERE wallet = :w"
        ),
        {"w": wallet.lower()},
    ).fetchone()
    if row is None or row.subgraph_synced_to_ts is None:
        return 0
    return int(row.subgraph_synced_to_ts)


# --- coverage ---------------------------------------------------------------


@dataclass(frozen=True)
class CoverageBucket:
    label: str
    total: int = 0
    enriched: int = 0
    pending: int = 0
    ambiguous: int = 0
    missing: int = 0


@dataclass(frozen=True)
class Coverage:
    wallet: str
    head_ts: int
    total: int
    enriched: int
    pending: int
    ambiguous: int
    missing: int
    buckets: tuple[CoverageBucket, ...]

    @property
    def enriched_share(self) -> Decimal:
        if self.total == 0:
            return Decimal(0)
        return Decimal(self.enriched) / Decimal(self.total)


# Recency buckets by age (seconds) relative to now, most-recent first.
_RECENCY_EDGES = [
    ("<=1d", 24 * 3600),
    ("1-7d", 7 * 24 * 3600),
    ("7-30d", 30 * 24 * 3600),
    (">30d", None),
]


def _recency_label(age_s: int) -> str:
    for label, edge in _RECENCY_EDGES:
        if edge is None or age_s <= edge:
            return label
    return ">30d"


def classify_trade_events(
    session: Session, wallet: str, *, now_ts: int, head_ts: Optional[int] = None
) -> list[tuple[int, str]]:
    """Classify each TRADE event for `wallet` as enriched/pending/ambiguous/
    missing. `pending` = unenriched but newer than the subgraph head (the
    subgraph simply hasn't reported it yet — NOT a genuine gap). `ambiguous` =
    old, unenriched, and sharing (tx_hash, token, |amount|) with another
    unenriched TRADE row (can never be disambiguated by amount). `missing` =
    old, unenriched, no such twin."""
    wallet = wallet.lower()
    if head_ts is None:
        head_ts = _current_subgraph_ts(session, wallet)

    rows = session.execute(
        text(
            "SELECT id, ts, tx_hash, token_id, delta_shares, delta_usdc, usdc_size "
            "FROM wallet_events "
            "WHERE wallet = :w AND event_type = 'TRADE' ORDER BY ts, id"
        ),
        {"w": wallet},
    ).fetchall()
    if not rows:
        return []

    enrichment_status = {
        r.event_id: r.role
        for r in session.execute(
            text(
                "SELECT fe.event_id, fe.role FROM fill_enrichment fe "
                "JOIN wallet_events we ON we.id = fe.event_id WHERE we.wallet = :w"
            ),
            {"w": wallet},
        )
    }

    # Group unenriched rows by (tx_hash, token, rounded |shares|, |USDC|) to find twins.
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        if r.id in enrichment_status:
            continue
        key = (
            (r.tx_hash or "").lower(),
            r.token_id,
            abs(Decimal(r.delta_shares)).quantize(MATCH_TOLERANCE),
            _ledger_abs_usdc(r.delta_usdc, getattr(r, "usdc_size", 0)).quantize(MATCH_TOLERANCE),
        )
        groups.setdefault(key, []).append(r.id)

    ambiguous_ids = {
        eid for ids in groups.values() if len(ids) > 1 for eid in ids
    }

    result: list[tuple[int, str]] = []
    for r in rows:
        if enrichment_status.get(r.id) == "ambiguous":
            status = "ambiguous"
        elif r.id in enrichment_status:
            status = "enriched"
        elif r.ts > head_ts:
            status = "pending"
        elif r.id in ambiguous_ids:
            status = "ambiguous"
        else:
            status = "missing"
        result.append((r.id, status))
    return result


def enrichment_coverage(
    session: Session, wallet: str, *, now_ts: int, head_ts: Optional[int] = None
) -> Coverage:
    wallet = wallet.lower()
    if head_ts is None:
        head_ts = _current_subgraph_ts(session, wallet)

    rows = {
        r.id: r
        for r in session.execute(
            text(
                "SELECT id, ts FROM wallet_events "
                "WHERE wallet = :w AND event_type = 'TRADE'"
            ),
            {"w": wallet},
        )
    }
    classified = classify_trade_events(session, wallet, now_ts=now_ts, head_ts=head_ts)

    totals = {"enriched": 0, "pending": 0, "ambiguous": 0, "missing": 0}
    buckets: dict[str, dict[str, int]] = {}
    for event_id, status in classified:
        totals[status] += 1
        age = max(0, now_ts - int(rows[event_id].ts))
        label = _recency_label(age)
        bucket = buckets.setdefault(
            label, {"total": 0, "enriched": 0, "pending": 0, "ambiguous": 0, "missing": 0}
        )
        bucket["total"] += 1
        bucket[status] += 1

    ordered = [label for label, _ in _RECENCY_EDGES if label in buckets]
    bucket_objs = tuple(
        CoverageBucket(label=label, **buckets[label]) for label in ordered
    )
    total = sum(totals.values())
    return Coverage(
        wallet=wallet,
        head_ts=head_ts,
        total=total,
        enriched=totals["enriched"],
        pending=totals["pending"],
        ambiguous=totals["ambiguous"],
        missing=totals["missing"],
        buckets=bucket_objs,
    )
