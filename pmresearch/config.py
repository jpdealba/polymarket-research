"""Env-first configuration. PMR_DATA_DIR (default /data) is the root for all
persistent state: db/, raw/, backups/, exports/, logs/."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    log_level: str
    rpc_url: str
    rclone_remote: str

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
