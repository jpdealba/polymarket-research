import sqlite3

from pmresearch.backup import create_backup, restore_backup
from pmresearch.config import Settings, ensure_data_dirs
from pmresearch.db.engine import get_engine
from pmresearch.db.migrations import downgrade, upgrade_to_head


def _settings(tmp_path) -> Settings:
    return Settings(data_dir=tmp_path, log_level="INFO", rpc_url="", rclone_remote="")


def _dump(path) -> str:
    conn = sqlite3.connect(path)
    try:
        return "\n".join(conn.iterdump())
    finally:
        conn.close()


def test_wal_pragma_active(tmp_path):
    settings = _settings(tmp_path)
    ensure_data_dirs(settings)
    engine = get_engine(settings)
    with engine.connect() as conn:
        mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar()
    assert mode.lower() == "wal"


def test_migration_round_trip(tmp_path):
    settings = _settings(tmp_path)
    ensure_data_dirs(settings)

    upgrade_to_head(settings)
    conn = sqlite3.connect(settings.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "alembic_version" in tables

    downgrade(settings, "base")
    upgrade_to_head(settings)

    conn = sqlite3.connect(settings.db_path)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    conn.close()
    assert "alembic_version" in tables


def test_backup_restore_round_trip(tmp_path):
    settings = _settings(tmp_path)
    ensure_data_dirs(settings)
    upgrade_to_head(settings)

    original_dump = _dump(settings.db_path)

    backup_path = create_backup(settings)
    assert backup_path.exists()

    restore_backup(settings, backup_path)

    restored_dump = _dump(settings.db_path)
    assert restored_dump == original_dump
