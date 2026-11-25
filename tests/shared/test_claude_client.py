"""Tests for shared/claude_client.py. A fake Anthropic client stands in for the API."""

from types import SimpleNamespace
from typing import Any, Literal

import pytest
from pydantic import BaseModel

from shared import claude_client as cc
from shared.claude_client import ClaudeClient, ClaudeRefused, InvalidModelOutput
from shared.config import Settings
from shared.redact import RedactionError

VALID = '{"category": "device_offline", "priority": "high", "summary": "Tracker offline."}'
INVALID = '{"category": "device_offline", "priority": "urgent"}'  # bad priority, no summary


class Triage(BaseModel):
    category: str
    priority: Literal["low", "normal", "high"]
    summary: str


class FakeAnthropic:
    """Returns the given reply texts in order and records every request."""

    def __init__(self, *replies: str, stop_reason: str = "end_turn") -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, Any]] = []
        self.stop_reason = stop_reason
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        self.calls.append(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="thinking", thinking=""),  # Opus 5.5 always thinks
                SimpleNamespace(type="text", text=self.replies.pop(0)),
            ],
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber"),
            model=kwargs["model"],
            usage=SimpleNamespace(
                input_tokens=1000,
                output_tokens=200,
                cache_read_input_tokens=3000,
                cache_creation_input_tokens=0,
            ),
        )


def settings(**overrides: Any) -> Settings:
    values = {"_env_file": None, "use_mocks": False, "claude_model": "claude-opus-5-5"}
    return Settings(**(values | overrides))


def live(fake: FakeAnthropic, **settings_overrides: Any) -> ClaudeClient:
    return ClaudeClient(mock=False, settings=settings(**settings_overrides), anthropic_client=fake)


def ask(client: ClaudeClient, user: str = "Ticket: tracker [IMEI_1] is offline.", **kw: Any):
    return client.ask(
        system="You triage support tickets.",
        user=user,
        schema=Triage,
        agent="desk_triage",
        prompt_version="v1",
        **kw,
    )


# --- The three requested cases --------------------------------------------------------------


def test_valid_output() -> None:
    fake = FakeAnthropic(VALID)
    result, usage = ask(live(fake))
    assert result == Triage(category="device_offline", priority="high", summary="Tracker offline.")
    assert len(fake.calls) == 1
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (1000, 200, 3000)
    assert usage.attempts == 1
    # 1000 x $4 + 200 x $20 + 3000 x $0.20, per million tokens
    assert usage.estimated_cost_usd == pytest.approx(0.0086)


def test_invalid_then_valid_retries_once_with_the_error() -> None:
    fake = FakeAnthropic(INVALID, VALID)
    result, usage = ask(live(fake))
    assert result.priority == "high"
    assert len(fake.calls) == 2
    retry_messages = fake.calls[1]["messages"]
    assert [m["role"] for m in retry_messages] == ["user", "assistant", "user"]
    assert "<validation_error>" in retry_messages[2]["content"]
    assert "priority" in retry_messages[2]["content"]
    assert usage.attempts == 2
    assert usage.input_tokens == 2000  # both attempts are counted


def test_two_invalid_replies_raise_invalid_model_output() -> None:
    fake = FakeAnthropic(INVALID, INVALID)
    with pytest.raises(InvalidModelOutput) as err:
        ask(live(fake))
    assert err.value.raw_output == INVALID
    assert len(fake.calls) == 2  # exactly one retry, no more


# --- Request shape ----------------------------------------------------------------------------


def test_system_prompt_first_and_cached_with_schema() -> None:
    fake = FakeAnthropic(VALID)
    ask(live(fake))
    system = fake.calls[0]["system"]
    assert system[0] == {"type": "text", "text": "You triage support tickets."}
    assert system[-1]["cache_control"] == {"type": "ephemeral"}
    assert '"priority"' in system[-1]["text"]
    assert "JSON" in system[-1]["text"]


def test_customer_text_is_sent_only_as_the_user_message() -> None:
    fake = FakeAnthropic(VALID)
    ask(live(fake), user="Ticket text here")
    assert fake.calls[0]["messages"] == [{"role": "user", "content": "Ticket text here"}]


def test_temperature_is_not_sent_and_fallback_is_on() -> None:
    fake = FakeAnthropic(VALID)
    ask(live(fake), temperature=0.7)
    call = fake.calls[0]
    assert "temperature" not in call
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]


def test_model_defaults_to_claude_model_setting() -> None:
    fake = FakeAnthropic(VALID)
    ask(live(fake, claude_model="claude-sonnet-5-5"))
    assert fake.calls[0]["model"] == "claude-sonnet-5-5"


def test_model_falls_back_when_setting_is_empty() -> None:
    fake = FakeAnthropic(VALID)
    ask(live(fake, claude_model=""))
    assert fake.calls[0]["model"] == cc.DEFAULT_MODEL


def test_code_fenced_reply_is_accepted() -> None:
    result, _ = ask(live(FakeAnthropic(f"```json\n{VALID}\n```")))
    assert result.category == "device_offline"


# --- Safety -------------------------------------------------------------------------------------


def test_personal_data_in_user_text_is_never_sent() -> None:
    fake = FakeAnthropic(VALID)
    with pytest.raises(RedactionError, match="PHONE"):
        ask(live(fake), user="Customer phone 6912345678")
    assert fake.calls == []


def test_refusal_raises_and_is_not_retried() -> None:
    fake = FakeAnthropic(VALID, stop_reason="refusal")
    with pytest.raises(ClaudeRefused, match="cyber"):
        ask(live(fake))
    assert len(fake.calls) == 1


def test_bad_agent_name_cannot_escape_fixture_folder() -> None:
    with pytest.raises(ValueError, match="agent name"):
        ClaudeClient(mock=True, settings=settings()).ask(
            system="s", user="u", schema=Triage, agent="../secrets", prompt_version="v1"
        )


def test_live_mode_without_api_key_fails_clearly() -> None:
    client = ClaudeClient(mock=False, settings=settings(anthropic_api_key=None))
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        ask(client)


# --- Mock mode ----------------------------------------------------------------------------------


def test_mock_mode_returns_fixture_without_calling_api() -> None:
    client = ClaudeClient(mock=True, settings=settings())
    result, usage = client.ask(
        system="s", user="u", schema=Triage, agent="example_agent", prompt_version="v1"
    )
    assert result.priority == "high"
    assert (usage.attempts, usage.estimated_cost_usd) == (0, 0.0)


def test_mock_mode_follows_use_mocks_setting() -> None:
    assert ClaudeClient(settings=settings(use_mocks=True)).mock is True
    assert ClaudeClient(settings=settings(use_mocks=False)).mock is False


def test_mock_fixture_that_breaks_the_schema_is_reported(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cc, "FIXTURES_DIR", tmp_path)
    (tmp_path / "desk_triage.json").write_text(INVALID, encoding="utf-8")
    with pytest.raises(InvalidModelOutput, match="fixture"):
        ask(ClaudeClient(mock=True, settings=settings()))


def test_missing_fixture_is_reported(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cc, "FIXTURES_DIR", tmp_path)
    with pytest.raises(FileNotFoundError, match="desk_triage.json"):
        ask(ClaudeClient(mock=True, settings=settings()))


# --- Cost ---------------------------------------------------------------------------------------


def test_unknown_model_cost_is_none() -> None:
    usage = cc.Usage(model="claude-unknown", input_tokens=10)
    assert cc.estimate_cost("claude-unknown", usage) is None


# --- Found in review ----------------------------------------------------------------------------


def test_personal_data_in_system_text_is_never_sent() -> None:
    fake = FakeAnthropic(VALID)
    with pytest.raises(RedactionError, match="system text"):
        live(fake).ask(
            system="Customer email: maria@example.gr",
            user="ok",
            schema=Triage,
            agent="desk_triage",
            prompt_version="v1",
        )
    assert fake.calls == []


def test_refusal_still_reports_the_tokens_spent() -> None:
    with pytest.raises(ClaudeRefused) as err:
        ask(live(FakeAnthropic(VALID, stop_reason="refusal")))
    assert err.value.usage is not None
    assert (err.value.usage.input_tokens, err.value.usage.attempts) == (1000, 1)


def test_invalid_output_still_reports_the_tokens_spent() -> None:
    with pytest.raises(InvalidModelOutput) as err:
        ask(live(FakeAnthropic(INVALID, INVALID)))
    assert (err.value.usage.input_tokens, err.value.usage.attempts) == (2000, 2)
