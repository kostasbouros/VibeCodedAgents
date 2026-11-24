from typer.testing import CliRunner

from agents.install_verifier.__main__ import app


def test_runs_in_mock_dry_run_mode() -> None:
    result = CliRunner().invoke(app, ["--mock", "--dry-run"])
    assert result.exit_code == 0
    assert "install_verifier" in result.output
