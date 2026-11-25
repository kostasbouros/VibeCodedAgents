"""Tests for shared/config.py. They never read the real .env file."""

from pathlib import Path

import pytest

from shared.config import Settings, get_settings

# The five secret settings, each with a fake value that is easy to spot if it leaks.
SECRETS = {
    "ANTHROPIC_API_KEY": "FAKE-SECRET-anthropic",
    "MOBILITY_PASSWORD": "FAKE-SECRET-mobility",
    "ZOHO_CLIENT_SECRET": "FAKE-SECRET-zoho-client",
    "ZOHO_DESK_REFRESH_TOKEN": "FAKE-SECRET-desk-token",
    "ZOHO_CRM_REFRESH_TOKEN": "FAKE-SECRET-crm-token",
}


@pytest.fixture
def settings_with_secrets(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for name, value in SECRETS.items():
        monkeypatch.setenv(name, value)
    return Settings(_env_file=None)


def test_printing_settings_never_shows_a_secret(
    settings_with_secrets: Settings, capsys: pytest.CaptureFixture[str]
) -> None:
    print(settings_with_secrets)
    print(repr(settings_with_secrets))
    printed = capsys.readouterr().out
    for value in SECRETS.values():
        assert value not in printed
    assert "**********" in printed


def test_str_repr_and_json_never_show_a_secret(settings_with_secrets: Settings) -> None:
    outputs = [
        str(settings_with_secrets),
        repr(settings_with_secrets),
        settings_with_secrets.model_dump_json(),
        str(settings_with_secrets.model_dump()),
    ]
    for text in outputs:
        for value in SECRETS.values():
            assert value not in text


def test_each_secret_field_is_hidden_on_its_own(settings_with_secrets: Settings) -> None:
    for name, value in SECRETS.items():
        field = getattr(settings_with_secrets, name.lower())
        assert value not in str(field)
        assert value not in repr(field)


def test_code_can_still_get_the_real_value_on_purpose(settings_with_secrets: Settings) -> None:
    for name, value in SECRETS.items():
        assert getattr(settings_with_secrets, name.lower()).get_secret_value() == value


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("USE_MOCKS", raising=False)
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    settings = Settings(_env_file=None)
    assert settings.use_mocks is True
    assert settings.environment == "dev"


def test_mocks_off_only_when_set_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USE_MOCKS", "false")
    assert Settings(_env_file=None).use_mocks is False


def test_reads_every_variable_in_env_example() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    names = [line.split("=")[0].lower() for line in example.read_text().splitlines() if line]
    missing = [n for n in names if n not in Settings.model_fields]
    assert missing == []


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.chdir(tmp_path)  # empty folder: no .env is read
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()
