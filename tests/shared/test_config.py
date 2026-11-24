from shared.config import Settings


def test_mocks_are_on_by_default(monkeypatch) -> None:
    monkeypatch.delenv("USE_MOCKS", raising=False)
    assert Settings(_env_file=None).use_mocks is True


def test_mocks_off_only_when_set_explicitly(monkeypatch) -> None:
    monkeypatch.setenv("USE_MOCKS", "false")
    assert Settings(_env_file=None).use_mocks is False


def test_secrets_are_hidden_when_printed(monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-real-value")
    settings = Settings(_env_file=None)
    assert "sk-real-value" not in repr(settings)
    assert "sk-real-value" not in str(settings.anthropic_api_key)
