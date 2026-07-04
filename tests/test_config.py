from pathlib import Path

from pmresearch.config import get_settings


def test_defaults(monkeypatch):
    monkeypatch.delenv("PMR_DATA_DIR", raising=False)
    monkeypatch.delenv("PMR_LOG_LEVEL", raising=False)
    monkeypatch.delenv("PMR_RPC_URL", raising=False)
    monkeypatch.delenv("PMR_POLYGONSCAN_API_KEY", raising=False)
    monkeypatch.delenv("PMR_RCLONE_REMOTE", raising=False)

    settings = get_settings()

    assert settings.data_dir == Path("/data")
    assert settings.log_level == "INFO"
    assert settings.rpc_url == ""
    assert settings.polygonscan_api_key == ""
    assert settings.rclone_remote == ""
    assert settings.db_path == Path("/data/db/pmresearch.db")


def test_env_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("PMR_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("PMR_LOG_LEVEL", "DEBUG")
    monkeypatch.setenv("PMR_RPC_URL", "https://example.org")
    monkeypatch.setenv("PMR_POLYGONSCAN_API_KEY", "poly-key")
    monkeypatch.setenv("PMR_RCLONE_REMOTE", "remote:bucket")

    settings = get_settings()

    assert settings.data_dir == tmp_path
    assert settings.log_level == "DEBUG"
    assert settings.rpc_url == "https://example.org"
    assert settings.polygonscan_api_key == "poly-key"
    assert settings.rclone_remote == "remote:bucket"
    assert settings.db_path == tmp_path / "db" / "pmresearch.db"
    assert settings.raw_dir == tmp_path / "raw"
    assert settings.backups_dir == tmp_path / "backups"
    assert settings.exports_dir == tmp_path / "exports"
    assert settings.logs_dir == tmp_path / "logs"
