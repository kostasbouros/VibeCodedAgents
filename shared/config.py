"""Settings loaded from environment variables (and .env). The only place secrets are read."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """All settings. Secrets are SecretStr so they print as '**********', never the real value."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    anthropic_api_key: SecretStr | None = None
    claude_model: str = ""
    claude_model_small: str = ""

    mobility_base_url: str = ""
    mobility_username: str = ""
    mobility_password: SecretStr | None = None

    zoho_accounts_url: str = ""
    zoho_client_id: str = ""
    zoho_client_secret: SecretStr | None = None
    zoho_desk_refresh_token: SecretStr | None = None
    zoho_desk_org_id: str = ""
    zoho_desk_base_url: str = ""
    zoho_desk_department_id: str = ""
    zoho_crm_refresh_token: SecretStr | None = None
    zoho_crm_base_url: str = ""

    # Mock data is the default; real systems only when USE_MOCKS=false is set explicitly.
    use_mocks: bool = True
    state_db_path: str = "data/state.db"  # relative paths are from the project folder
    log_level: str = "INFO"
    environment: str = "dev"


@lru_cache
def get_settings() -> Settings:
    """Load settings once and reuse them."""
    return Settings()
