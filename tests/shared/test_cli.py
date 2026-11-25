"""Tests for shared/cli.py."""

from typing import Any

import pytest
from typer.testing import CliRunner

from shared import cli
from shared.cli import LiveModeRefused, RunOptions, build_app, check_live_allowed
from shared.config import Settings

runner = CliRunner()


def use_settings(monkeypatch: pytest.MonkeyPatch, **values: Any) -> None:
    for name in ("USE_MOCKS", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(_env_file=None))


def recording_app() -> tuple[Any, list[RunOptions]]:
    seen: list[RunOptions] = []
    return build_app("demo_agent", seen.append), seen


def test_defaults_are_mock_no_dry_run_no_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    app, seen = recording_app()
    result = runner.invoke(app, [])
    assert result.exit_code == 0, result.output
    assert (seen[0].mock, seen[0].dry_run, seen[0].limit, seen[0].verbose) == (
        True,
        False,
        None,
        False,
    )


def test_all_flags_are_passed_to_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    app, seen = recording_app()
    result = runner.invoke(app, ["--mock", "--dry-run", "--limit", "5", "--verbose"])
    assert result.exit_code == 0, result.output
    assert (seen[0].agent, seen[0].dry_run, seen[0].limit, seen[0].verbose) == (
        "demo_agent",
        True,
        5,
        True,
    )


def test_limit_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    app, seen = recording_app()
    assert runner.invoke(app, ["--limit", "0"]).exit_code != 0
    assert seen == []


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({}, "USE_MOCKS must be set to false"),
        ({"USE_MOCKS": "true", "ENVIRONMENT": "prod"}, "USE_MOCKS must be set to false"),
        ({"USE_MOCKS": "false"}, "ENVIRONMENT must be set"),
        ({"ENVIRONMENT": "prod"}, "USE_MOCKS must be set to false"),
    ],
)
def test_live_is_refused_without_both_settings(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], message: str
) -> None:
    use_settings(monkeypatch, **env)
    app, seen = recording_app()
    result = runner.invoke(app, ["--live"])
    assert result.exit_code == 2
    assert message in result.output
    assert seen == []  # the agent never started


def test_live_runs_when_both_settings_are_explicit(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch, USE_MOCKS="false", ENVIRONMENT="production")
    app, seen = recording_app()
    result = runner.invoke(app, ["--live"])
    assert result.exit_code == 0, result.output
    assert seen[0].mock is False


def test_default_environment_value_does_not_count_as_set() -> None:
    settings = Settings(_env_file=None, use_mocks=False)
    assert settings.environment == "dev"  # default exists...
    with pytest.raises(LiveModeRefused, match="ENVIRONMENT"):
        check_live_allowed(settings)  # ...but was not chosen on purpose


def test_agent_without_run_function_prints_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    use_settings(monkeypatch)
    result = runner.invoke(build_app("demo_agent"), ["--dry-run"])
    assert result.exit_code == 0
    assert "demo_agent: not built yet" in result.output


def test_mock_flag_wins_over_use_mocks_false_everywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    # Found in review: with USE_MOCKS=false in the settings file, --mock must still keep
    # Claude in mock mode.
    use_settings(monkeypatch, USE_MOCKS="false", ENVIRONMENT="production")
    app, seen = recording_app()
    result = runner.invoke(app, ["--mock", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert seen[0].mock is True
    assert seen[0].settings.use_mocks is True
    from shared.claude_client import ClaudeClient

    assert ClaudeClient(settings=seen[0].settings).mock is True
    assert Settings(_env_file=None).use_mocks is True  # anything reading settings later agrees
