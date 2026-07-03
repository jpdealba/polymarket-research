"""Backup/restore via sqlite3 VACUUM INTO — never copy the live WAL-mode file
directly (that risks copying a torn/inconsistent snapshot)."""

from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import Settings


def create_backup(settings: Settings) -> Path:
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = settings.backups_dir / f"pmresearch_{ts}.db"

    conn = sqlite3.connect(settings.db_path)
    try:
        conn.execute("VACUUM INTO ?", (str(backup_path),))
    finally:
        conn.close()

    return backup_path


def restore_backup(settings: Settings, backup_file: Path) -> Path:
    conn = sqlite3.connect(backup_file)
    try:
        (result,) = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    if result != "ok":
        raise ValueError(f"Backup file failed integrity check: {result}")

    settings.db_dir.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(f"{settings.db_path}{suffix}")
        if stale.exists():
            stale.unlink()

    shutil.copy2(backup_file, settings.db_path)
    return settings.db_path
