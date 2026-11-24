from typer.testing import CliRunner

from agents.account_signals.__main__ import app


def test_runs_in_mock_dry_run_mode() -> None:
    result = CliRunner().invoke(app, ["--mock", "--dry-run"])
    assert result.exit_code == 0
    assert "account_signals" in result.output
