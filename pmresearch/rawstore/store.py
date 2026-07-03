"""Raw Store: every external API response is persisted verbatim (gzipped)
before anything parses it, indexed in `raw_fetches`. Recovery tier 1 (ADR
0002): the DB can be rebuilt from raw snapshots without re-fetching.

Dedupe: if an identical (source, endpoint, params) request already produced
byte-identical content, skip the write — this is what makes re-running a
backfill or incremental sync a near no-op.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..config import Settings


def _content_hash(payload_bytes: bytes) -> str:
    return hashlib.sha256(payload_bytes).hexdigest()


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(frozen=True)
class RawFetchResult:
    raw_fetch_id: int
    file_path: Path
    content_hash: str
    row_count: int
    deduped: bool


class RawStore:
    def __init__(self, settings: Settings, session: Session) -> None:
        self.settings = settings
        self.session = session

    def persist(
        self,
        *,
        source: str,
        endpoint: str,
        wallet: str,
        params: object,
        payload: object,
        http_status: int,
        fetched_at: datetime | None = None,
    ) -> RawFetchResult:
        endpoint = endpoint.strip("/")
        fetched_at = fetched_at or datetime.now(timezone.utc)
        body = _canonical_json(payload)
        content_hash = _content_hash(body)
        row_count = len(payload) if isinstance(payload, list) else 1
        params_json = _canonical_json(params).decode("utf-8")

        existing = self.session.execute(
            text(
                "SELECT id, file_path FROM raw_fetches "
                "WHERE source = :source AND endpoint = :endpoint "
                "AND params_json = :params_json AND content_hash = :content_hash"
            ),
            {
                "source": source,
                "endpoint": endpoint,
                "params_json": params_json,
                "content_hash": content_hash,
            },
        ).fetchone()
        if existing is not None:
            return RawFetchResult(
                raw_fetch_id=existing.id,
                file_path=Path(existing.file_path),
                content_hash=content_hash,
                row_count=row_count,
                deduped=True,
            )

        ts = fetched_at.strftime("%Y%m%dT%H%M%S%f") + "Z"
        content_hash8 = content_hash[:8]
        file_dir = self.settings.raw_dir / source / endpoint / wallet
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / f"{ts}_{content_hash8}.json.gz"
        suffix = 0
        while True:
            candidate = (
                file_path
                if suffix == 0
                else file_dir / f"{ts}_{content_hash8}_{suffix}.json.gz"
            )
            try:
                with gzip.open(candidate, "xb") as fh:
                    fh.write(body)
            except FileExistsError:
                suffix += 1
                continue
            file_path = candidate
            break

        try:
            result = self.session.execute(
                text(
                    "INSERT INTO raw_fetches "
                    "(source, endpoint, params_json, fetched_at, http_status, file_path, content_hash, row_count) "
                    "VALUES (:source, :endpoint, :params_json, :fetched_at, :http_status, :file_path, :content_hash, :row_count)"
                ),
                {
                    "source": source,
                    "endpoint": endpoint,
                    "params_json": params_json,
                    "fetched_at": fetched_at.isoformat(),
                    "http_status": http_status,
                    "file_path": str(file_path),
                    "content_hash": content_hash,
                    "row_count": row_count,
                },
            )
            self.session.commit()
        except IntegrityError:
            self.session.rollback()
            file_path.unlink(missing_ok=True)
            existing = self.session.execute(
                text(
                    "SELECT id, file_path FROM raw_fetches "
                    "WHERE source = :source AND endpoint = :endpoint "
                    "AND params_json = :params_json AND content_hash = :content_hash"
                ),
                {
                    "source": source,
                    "endpoint": endpoint,
                    "params_json": params_json,
                    "content_hash": content_hash,
                },
            ).fetchone()
            if existing is None:
                raise
            return RawFetchResult(
                raw_fetch_id=existing.id,
                file_path=Path(existing.file_path),
                content_hash=content_hash,
                row_count=row_count,
                deduped=True,
            )

        return RawFetchResult(
            raw_fetch_id=result.lastrowid,
            file_path=file_path,
            content_hash=content_hash,
            row_count=row_count,
            deduped=False,
        )
