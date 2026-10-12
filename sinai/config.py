from pathlib import Path
import os
from typing import Literal
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_file_encoding="utf-8")
    amocrm_base_url: str = "https://sinai24.amocrm.ru"
    amocrm_access_token: SecretStr = SecretStr("")
    amocrm_refresh_token: SecretStr = SecretStr("")
    amocrm_client_id: str = ""
    amocrm_client_secret: SecretStr = SecretStr("")
    amocrm_redirect_uri: str = ""
    env_path: Path = Path(".env")
    admin_id: int = 8608404966
    max_admin_id: int = 185607445
    tg_manager_ids: str = ""
    max_manager_ids: str = ""
    tg_bot_token: SecretStr = SecretStr("")
    max_bot_token: SecretStr = SecretStr("")
    tg_bot_username: str = "sinai_news_bot"
    max_bot_username: str = ""
    tg_webhook_secret: SecretStr = SecretStr("")
    max_webhook_secret: SecretStr = SecretStr("")
    amo_webhook_secret: SecretStr = SecretStr("")
    public_base_url: str = ""
    database_path: Path = Path("data/sinai.sqlite3")
    test_mode: bool = True
    broadcasts_enabled: bool = False
    crm_notes_enabled: bool = False
    completed_pipeline_id: int = 8993570
    active_interval_months: Literal[1, 2] = 1
    completed_interval_months: Literal[1, 2] = 1
    preferred_platform: Literal["telegram", "max"] = "telegram"
    reconciliation_seconds: int = Field(default=3600, ge=60)
    contact_index_seconds: int = Field(default=86400, ge=60)
    max_contact_pages: int = Field(default=1000, ge=1)
    link_ttl_seconds: int = Field(default=604800, ge=60)
    max_ca_bundle: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production(self):
        if not self.amocrm_base_url.startswith("https://"):
            raise ValueError("AMOCRM_BASE_URL requires HTTPS")
        if not self.test_mode:
            if not self.public_base_url.startswith("https://"):
                raise ValueError("PUBLIC_BASE_URL requires HTTPS in production")
            required = [(self.tg_bot_token, self.tg_webhook_secret), (self.max_bot_token, self.max_webhook_secret)]
            if any(t.get_secret_value() and len(s.get_secret_value()) < 32 for t, s in required):
                raise ValueError("Webhook secrets must contain at least 32 characters")
            if len(self.amo_webhook_secret.get_secret_value()) < 32:
                raise ValueError("AMO_WEBHOOK_SECRET must contain at least 32 characters")
        return self

    def admin(self, platform: str, user_id: int) -> bool:
        expected = self.admin_id if platform == "telegram" else self.max_admin_id
        return bool(expected and user_id == expected)

    def manager(self, platform: str, user_id: int) -> bool:
        ids = self.tg_manager_ids if platform == "telegram" else self.max_manager_ids
        return self.admin(platform, user_id) or str(user_id) in {x.strip() for x in ids.split(",") if x.strip()}


def load_settings():
    path = os.environ.get("ENV_PATH", ".env")
    return Settings(_env_file=path, env_path=Path(path))
