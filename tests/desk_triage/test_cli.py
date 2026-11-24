from typer.testing import CliRunner

from agents.desk_triage.__main__ import app


def test_runs_in_mock_dry_run_mode() -> None:
    result = CliRunner().invoke(app, ["--mock", "--dry-run"])
    assert result.exit_code == 0
    assert "desk_triage" in result.output
