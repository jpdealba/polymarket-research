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


def _enriched_set(session: Session, event_ids: list[int]) -> set[int]:
    if not event_ids:
        return set()
    placeholders = ",".join(f":id{i}" for i in range(len(event_ids)))
    params = {f"id{i}": eid for i, eid in enumerate(event_ids)}
    rows = session.execute(
        text(f"SELECT event_id FROM fill_enrichment WHERE event_id IN ({placeholders})"),
        params,
    )
    return {r.event_id for r in rows}


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


def _insert_enrichment(
    session: Session,
    *,
    event_id: int,
    role: str,
    order_hash: str,
    fee: str,
    counterparty: Optional[str],
    source: str,
    enriched_at: str,
) -> bool:
    result = session.execute(
        text(
            "INSERT INTO fill_enrichment "
            "(event_id, role, order_hash, fee, counterparty, source, enriched_at) "
            "VALUES (:event_id, :role, :order_hash, :fee, :counterparty, :source, :enriched_at) "
            "ON CONFLICT(event_id) DO NOTHING"
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
    return result.rowcount > 0


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

    for fill in fills:
        stats = stats._add(fills_seen=1, head_ts=getattr(fill, "timestamp", 0) or 0)

        if fill.maker == wallet:
            role, counterparty = "maker", fill.taker
        elif fill.taker == wallet:
            role, counterparty = "taker", fill.maker
        else:
            # Fill returned by the source but not actually involving this wallet.
            stats = stats._add(unmatched=1)
            continue

        candidates = _trade_candidates(session, wallet, fill.transaction_hash, fill.traded_token_id)
        if candidates:
            enriched_ids = _enriched_set(session, [c.event_id for c in candidates])
            candidates = [c for c in candidates if c.event_id not in enriched_ids]
            if not candidates:
                stats = stats._add(already_enriched=1)
                continue

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

        inserted = _insert_enrichment(
            session,
            event_id=event_id,
            role=role,
            order_hash=fill.order_hash,
            fee=str(fill.fee_decimal),
            counterparty=counterparty,
            source=source,
            enriched_at=enriched_at,
        )
        if inserted:
            stats = stats._add(enriched=1)
        else:
            stats = stats._add(already_enriched=1)

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

    enriched_ids = {
        r.event_id
        for r in session.execute(
            text("SELECT fe.event_id FROM fill_enrichment fe "
                 "JOIN wallet_events we ON we.id = fe.event_id WHERE we.wallet = :w"),
            {"w": wallet},
        )
    }

    # Group unenriched rows by (tx_hash, token, rounded |shares|, |USDC|) to find twins.
    groups: dict[tuple, list[int]] = {}
    for r in rows:
        if r.id in enriched_ids:
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
        if r.id in enriched_ids:
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
