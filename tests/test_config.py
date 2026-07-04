from pathlib import Path

from pmresearch.config import get_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("PMR_DATA_DIR", raising=False)
    monkeypatch.delenv("PMR_LOG_LEVEL", raising=False)
    monkeypatch.delenv("PMR_RPC_URL", raising=False)
    monkeypatch.delenv("PMR_POLYGONSCAN_API_KEY", raising=False)
    monkeypatch.delenv("PMR_RCLONE_REMOTE", raising=False)
    monkeypatch.delenv("PMR_WORLDCUP_WATCH_ENABLED", raising=False)
    monkeypatch.delenv("PMR_WORLDCUP_WALLET", raising=False)

    settings = get_settings()

    assert settings.data_dir == Path("/data")
    assert settings.log_level == "INFO"
    assert settings.rpc_url == ""
    assert settings.polygonscan_api_key == ""
    assert settings.rclone_remote == ""
    assert settings.db_path == Path("/data/db/pmresearch.db")
    assert settings.worldcup_watch_enabled is False
    assert settings.worldcup_wallet == ""
    assert settings.worldcup_watchlist_name == "world_cup_2026"
    assert settings.worldcup_sample_limit == 200


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PMR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PMR_RPC_URL", "https://example.org")
    monkeypatch.setenv("PMR_POLYGONSCAN_API_KEY", "poly-key")
    monkeypatch.setenv("PMR_RCLONE_REMOTE", "remote:bucket")
    monkeypatch.setenv("PMR_WORLDCUP_WATCH_ENABLED", "true")
    monkeypatch.setenv("PMR_WORLDCUP_WALLET", "0xABC")
    monkeypatch.setenv("PMR_WORLDCUP_SAMPLE_LIMIT", "123")

    settings = get_settings()

    assert settings.data_dir == tmp_path
    assert settings.log_level == "DEBUG"
    assert settings.rpc_url == "https://example.org"
    assert settings.polygonscan_api_key == "poly-key"
    assert settings.rclone_remote == "remote:bucket"
    assert settings.worldcup_watch_enabled is True
    assert settings.worldcup_wallet == "0xabc"
    assert settings.worldcup_sample_limit == 123
    assert settings.db_path == tmp_path / "db" / "pmresearch.db"
    assert settings.raw_dir == tmp_path / "raw"
    assert settings.backups_dir == tmp_path / "backups"
    assert settings.exports_dir == tmp_path / "exports"
    assert settings.logs_dir == tmp_path / "logs"
