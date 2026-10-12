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
    database_path: Path = Path("data/sinai.sqlite3")
    test_mode: bool = True
    broadcasts_enabled: bool = False
    crm_notes_enabled: bool = False
    completed_pipeline_id: int = 8993570
    active_interval_months: Literal[1, 2] = 1
    completed_interval_months: Literal[1, 2] = 1
    preferred_platform: Literal["telegram", "max"] = "telegram"
    reconciliation_seconds: int = Field(default=3600, ge=60)
    max_contact_pages: int = Field(default=1000, ge=1)
    polling_enabled: bool = False
    poll_timeout: int = Field(default=25, ge=1, le=60)
    max_poll_min_seconds: float = Field(default=2, ge=1)
    lead_start_limit: int = Field(default=5, ge=1)
    lead_start_global_limit: int = Field(default=100, ge=1)
    lead_start_window_seconds: int = Field(default=3600, ge=60)
    media_max_bytes: int = Field(default=20_000_000, ge=1024, le=50_000_000)
    max_ca_bundle: str = ""
    log_level: str = "INFO"

    @model_validator(mode="after")
    def validate_production(self):
        if not self.amocrm_base_url.startswith("https://"):
            raise ValueError("AMOCRM_BASE_URL requires HTTPS")
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
