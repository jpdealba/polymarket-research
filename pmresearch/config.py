"""Env-first configuration. PMR_DATA_DIR (default /data) is the root for all
persistent state: db/, raw/, backups/, exports/, logs/."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_level: str
    rpc_url: str
    rclone_remote: str
    # Goldsky orderbook subgraph endpoint (Phase 11 enrichment). Empty = the
    # subgraph is not configured, so enrichment is skipped / errors clearly.
    subgraph_url: str = ""
    # Etherscan V2 (PolygonScan) API key — free-tier maker/taker source that
    # paginates by results, immune to RPC block-range caps. Empty = off.
    polygonscan_api_key: str = ""
    # Holdings below this many shares (absolute) count as flat — the source
    # reports 6-decimal sizes, so anything under 1e-6 is rounding residue.
    dust_epsilon: Decimal = Decimal("0.000001")
    # Book sampler interval in seconds (Phase 12). 0 = disabled.
    book_sample_interval_s: int = 300
    # Days to retain raw book snapshots before pruning (Phase 12).
    # Summary rows (best_bid/ask/spread/mid) are kept indefinitely.
    book_retention_raw_days: int = 30

    @property
    def db_dir(self) -> Path:
        return self.data_dir / "db"

    @property
    def db_path(self) -> Path:
        return self.db_dir / "pmresearch.db"

    @property
    def raw_dir(self) -> Path:
        return self.data_dir / "raw"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def exports_dir(self) -> Path:
        return self.data_dir / "exports"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite:///{self.db_path.as_posix()}"


def get_settings() -> Settings:
    return Settings(
        data_dir=Path(_env("PMR_DATA_DIR", "/data")),
        log_level=_env("PMR_LOG_LEVEL", "INFO"),
        rpc_url=_env("PMR_RPC_URL", ""),
        rclone_remote=_env("PMR_RCLONE_REMOTE", ""),
        subgraph_url=_env("PMR_SUBGRAPH_URL", ""),
        polygonscan_api_key=_env("PMR_POLYGONSCAN_API_KEY", ""),
        dust_epsilon=Decimal(_env("PMR_DUST_EPSILON", "0.000001")),
        book_sample_interval_s=int(_env("PMR_BOOK_SAMPLE_INTERVAL_S", "300")),
        book_retention_raw_days=int(_env("PMR_BOOK_RETENTION_RAW_DAYS", "30")),
    )


def ensure_data_dirs(settings: Settings) -> None:
    for d in (
        settings.db_dir,
        settings.raw_dir,
        settings.backups_dir,
        settings.exports_dir,
        settings.logs_dir,
    ):
        d.mkdir(parents=True, exist_ok=True)
