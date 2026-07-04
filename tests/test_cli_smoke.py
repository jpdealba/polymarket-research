from click.testing import CliRunner

from pmresearch.cli import main


def test_version_exit_zero():
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()


def test_worldcup_wallets_cli(settings, monkeypatch):
    monkeypatch.setenv("PMR_DATA_DIR", str(settings.data_dir))
    runner = CliRunner()

    result = runner.invoke(main, ["worldcup", "wallets", "set", "0xAAA", "0xBBB"])
    assert result.exit_code == 0
    assert "tracking=0xaaa,0xbbb" in result.output

    result = runner.invoke(main, ["worldcup", "wallets", "list"])
    assert result.exit_code == 0
    assert "wallet=0xaaa" in result.output
    assert "wallet=0xbbb" in result.output

    result = runner.invoke(main, ["worldcup", "wallets", "set", "0x1", "0x2", "0x3"])
    assert result.exit_code != 0
    assert "at most 2" in result.output

    result = runner.invoke(main, ["worldcup", "wallets", "clear"])
    assert result.exit_code == 0
    assert result.output.strip() == "tracking="
