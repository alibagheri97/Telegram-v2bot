from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Annotated
from zoneinfo import ZoneInfo

from pydantic import BeforeValidator, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _split_ids(value: object) -> list[int]:
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped or stripped == "[]":
            return []
        if stripped.startswith("["):
            return [int(item) for item in json.loads(stripped)]
        return [int(item.strip()) for item in stripped.split(",") if item.strip()]
    if isinstance(value, int):
        return [value]
    return list(value or [])


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    bot_token: SecretStr
    brand_name: str = "VPN Shop"
    bot_db_url: SecretStr
    panel_db_url: SecretStr
    panel_user_table: str = "v2_user"
    root_telegram_ids: Annotated[list[int], NoDecode, BeforeValidator(_split_ids)] = Field(
        default_factory=list
    )
    bootstrap_auth_hash: SecretStr | None = None
    bootstrap_auth_expires_at: int = 0
    subscription_base_url: str
    brand_subscription_base_url: str = ""
    brand_api_port: int = 24121
    trial_plan_id: int = 4
    trial_group_id: int = 2
    trial_days: int = 1
    trial_transfer_gb: int = 1
    payment_group_id: int = 0
    payment_topic_id: int = 0
    support_username: str = "@support"
    card_number: str
    card_holder: str
    timezone: str = "Asia/Tehran"
    log_level: str = "INFO"

    @field_validator("subscription_base_url")
    @classmethod
    def validate_subscription_url(cls, value: str) -> str:
        if not value.startswith("https://"):
            raise ValueError("SUBSCRIPTION_BASE_URL must use HTTPS")
        return value

    @field_validator("brand_subscription_base_url")
    @classmethod
    def validate_brand_subscription_url(cls, value: str) -> str:
        if value and (not value.startswith("https://") or not value.endswith("/")):
            raise ValueError("BRAND_SUBSCRIPTION_BASE_URL must use HTTPS and end with /")
        return value

    @field_validator("panel_user_table")
    @classmethod
    def validate_table_name(cls, value: str) -> str:
        if not value.replace("_", "").isalnum():
            raise ValueError("PANEL_USER_TABLE must contain only letters, numbers and underscores")
        return value

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


@lru_cache
def get_settings() -> Settings:
    return Settings(_env_file=os.environ.get("VPN_SHOP_ENV_FILE", ".env"))
