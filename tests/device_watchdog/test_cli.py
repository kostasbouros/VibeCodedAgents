from pathlib import Path

import pytest
from typer.testing import CliRunner

from agents.device_watchdog.__main__ import app
from shared.state import StateStore
from tests.device_watchdog.conftest import FakeMobility


def test_runs_in_mock_dry_run_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("connectors.mobility.get_mobility_client", FakeMobility)
    monkeypatch.setattr("agents.device_watchdog.agent.default_store",
                        lambda: StateStore(tmp_path / "state.db"))  # fmt: skip
    monkeypatch.setattr("agents.device_watchdog.agent.CACHE_FILE", tmp_path / "cache.json")
    monkeypatch.setattr("shared.trace.TRACES_DIR", tmp_path / "traces")
    result = CliRunner().invoke(app, ["--mock", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "device_watchdog" in result.output
    assert "# Device health report" in result.output
