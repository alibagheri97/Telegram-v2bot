from io import StringIO

import pytest
from dotenv import dotenv_values

from vpnshopbot.config import Settings
from vpnshopbot.setup import (
    dotenv_line,
    render_env,
    render_service_unit,
    service_slug,
    validate_https,
)


def test_service_name_is_safe_and_brand_based() -> None:
    assert service_slug("Star Light VPN") == "star_light_vpn"
    assert service_slug("Starlight") == "starlight"
    assert service_slug("ستاره") == "vpn_shop"


def test_subscription_url_must_be_real_https_endpoint() -> None:
    assert (
        validate_https("https://panel.brand.test/api/v1/client/subscribe?token=")
        == "https://panel.brand.test/api/v1/client/subscribe?token="
    )
    with pytest.raises(ValueError):
        validate_https("http://panel.brand.test/subscribe?token=")
    with pytest.raises(ValueError):
        validate_https("https://panel.example.com/subscribe?token=")


def test_dotenv_values_are_quoted() -> None:
    assert dotenv_line("BRAND_NAME", "ستاره روشن") == 'BRAND_NAME="ستاره روشن"'


def test_rendered_bootstrap_hash_round_trips() -> None:
    config = {
        "db_user": "brand_bot",
        "db_password": "safePassword123",
        "db_host": "127.0.0.1",
        "db_port": 3306,
        "bot_db": "brand_bot",
        "panel_db": "panel",
        "panel_table": "v2_user",
        "bot_token": "123:token",
        "brand": "ستاره",
        "subscription_url": "https://panel.brand.test/subscribe?token=",
        "brand_subscription_url": "https://brand.test/allbatech/",
        "brand_api_port": 24121,
        "trial_plan_id": 4,
        "trial_group_id": 2,
        "trial_days": 1,
        "trial_transfer_gb": 1,
        "payment_group_id": 0,
        "payment_topic_id": 0,
        "support_username": "@support",
        "card_number": "1234",
        "card_holder": "Test",
        "timezone": "Asia/Tehran",
    }
    auth_hash = "$2b$12$abcdefghijklmnopqrstuv1234567890123456789012345678901"
    parsed = dotenv_values(stream=StringIO(render_env(config, auth_hash, 123456)))
    assert parsed["BOOTSTRAP_AUTH_HASH"] == auth_hash
    assert parsed["ROOT_TELEGRAM_IDS"] == "[]"


@pytest.mark.parametrize("raw", ["", "[]"])
def test_empty_root_admin_list_is_accepted(raw: str) -> None:
    settings = Settings(
        _env_file=None,
        bot_token="123:token",
        bot_db_url="sqlite:///bot.db",
        panel_db_url="sqlite:///panel.db",
        root_telegram_ids=raw,
        subscription_base_url="https://panel.brand.test/subscribe?token=",
        card_number="1234",
        card_holder="Test",
    )
    assert settings.root_telegram_ids == []


def test_service_unit_uses_brand_name_and_clone_path() -> None:
    unit = render_service_unit("starlight_bot.service", "alice", "alice", "/srv/Starlight Bot")
    assert "WorkingDirectory=/srv/Starlight\\x20Bot" in unit
    assert "ExecStart=/srv/Starlight\\x20Bot/.venv/bin/python -m vpnshopbot.app" in unit
    assert "User=alice" in unit
