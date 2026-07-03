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
    # Holdings below this many shares (absolute) count as flat — the source
    # reports 6-decimal sizes, so anything under 1e-6 is rounding residue.
    dust_epsilon: Decimal = Decimal("0.000001")

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
        dust_epsilon=Decimal(_env("PMR_DUST_EPSILON", "0.000001")),
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
