from click.testing import CliRunner

from pmresearch.cli import main


def test_version_exit_zero():
    runner = CliRunner()
    result = runner.invoke(main, ["version"])
    assert result.exit_code == 0
    assert result.output.strip()
