"""Tests for shared/limits.py and config/limits.yaml."""

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from shared import claude_client, http, redact, state, trace
from shared.limits import LIMITS_FILE, limits, load_limits


def write_limits(tmp_path: Path, change: dict) -> Path:
    data = yaml.safe_load(LIMITS_FILE.read_text(encoding="utf-8"))
    for section, values in change.items():
        data[section].update(values)
    path = tmp_path / "limits.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_project_limits_file_is_valid_and_unchanged() -> None:
    lim = load_limits()
    assert lim.http.max_attempts == 4
    assert lim.http.base_wait_seconds == 1.0
    assert lim.http.max_jitter_seconds == 0.5
    assert lim.http.max_retry_after_seconds == 120
    assert lim.claude.max_tokens == 1500
    assert lim.claude.max_validation_error_chars == 2000
    assert lim.state.claim_stale_after_minutes == 60
    assert lim.state.max_error_chars == 500
    assert lim.trace.max_note_chars == 200
    assert lim.redact.signoff_search_lines == 8


def test_code_uses_the_values_from_the_file() -> None:
    lim = limits()
    assert http.MAX_ATTEMPTS == lim.http.max_attempts
    assert http.BASE_WAIT_SECONDS == lim.http.base_wait_seconds
    assert http.MAX_JITTER_SECONDS == lim.http.max_jitter_seconds
    assert http.MAX_RETRY_AFTER_SECONDS == lim.http.max_retry_after_seconds
    assert claude_client._MAX_ERROR_CHARS == lim.claude.max_validation_error_chars
    assert state._MAX_ERROR_CHARS == lim.state.max_error_chars
    assert trace._MAX_NOTE_CHARS == lim.trace.max_note_chars
    assert redact._SIGNOFF_SEARCH_LINES == lim.redact.signoff_search_lines


@pytest.mark.parametrize(
    "change",
    [
        {"http": {"max_attempts": 0}},
        {"http": {"max_retry_after_seconds": -5}},
        {"claude": {"max_tokens": "lots"}},
        {"state": {"claim_stale_after_minutes": 0}},
    ],
)
def test_bad_values_stop_the_program(tmp_path: Path, change: dict) -> None:
    with pytest.raises(ValidationError):
        load_limits(write_limits(tmp_path, change))


def test_misspelled_name_is_an_error(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="max_atempts"):
        load_limits(write_limits(tmp_path, {"http": {"max_atempts": 3}}))


def test_missing_section_is_an_error(tmp_path: Path) -> None:
    path = tmp_path / "limits.yaml"
    path.write_text("http:\n  max_attempts: 4\n", encoding="utf-8")
    with pytest.raises(ValidationError):
        load_limits(path)


def test_zero_jitter_is_allowed(tmp_path: Path) -> None:
    assert load_limits(write_limits(tmp_path, {"http": {"max_jitter_seconds": 0}})).http


def test_claude_max_tokens_default_comes_from_limits() -> None:
    from types import SimpleNamespace

    from shared.config import Settings

    calls: list[dict] = []

    def create(**kwargs: object) -> SimpleNamespace:
        calls.append(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text='{"ok": true}')],
            stop_reason="end_turn",
            model="claude-opus-5-5",
            usage=SimpleNamespace(
                input_tokens=1,
                output_tokens=1,
                cache_read_input_tokens=0,
                cache_creation_input_tokens=0,
            ),
        )

    from pydantic import BaseModel

    class Reply(BaseModel):
        ok: bool

    fake = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
    client = claude_client.ClaudeClient(
        mock=False, settings=Settings(_env_file=None), anthropic_client=fake
    )
    client.ask(system="s", user="u", schema=Reply, agent="demo", prompt_version="v1")
    assert calls[0]["max_tokens"] == limits().claude.max_tokens
