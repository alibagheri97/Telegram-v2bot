import time

from vpnshopbot.handlers import (
    _account_caption,
    _remaining,
    _status_icon,
    _v2_page_targets,
    _v2_usage_metrics,
)
from vpnshopbot.panel import GIB


def account(**overrides):
    result = {
        "banned": 0,
        "expired_at": int(time.time()) + 10 * 86400,
        "transfer_enable": 10 * GIB,
        "u": 1 * GIB,
        "d": 0,
    }
    result.update(overrides)
    return result


def test_active_is_green() -> None:
    assert _status_icon(account()) == "🟢"


def test_near_expiration_is_yellow() -> None:
    assert _status_icon(account(expired_at=int(time.time()) + 3600)) == "🟡"


def test_expired_is_red() -> None:
    item = account(expired_at=int(time.time()) - 60)
    assert _status_icon(item) == "🔴"
    assert "منقضی" in _remaining(item)


def test_unlimited_account_caption_uses_name_instead_of_panel_username() -> None:
    item = account(email="internal@panel.local", device_limit=2)
    caption = _account_caption(
        item,
        label="اشتراک سه‌ماهه",
        display_name="علی رضایی",
        auto_renewal=True,
        unlimited=True,
        url="https://panel.test/subscribe?token=abc",
    )
    assert "علی رضایی" in caption
    assert "internal@panel.local" not in caption
    assert "📈 مصرف‌شده: 1.00 GB" in caption
    assert "📊 باقی‌مانده: ♾️ نامحدود" in caption
    assert "🔌 نوع اکانت: 👥 دوکاربره" in caption
    assert all(icon in caption for icon in ["🏷️", "👤", "🧩", "📊", "⏳", "🔌", "🚦", "🔁", "🔗"])


def test_v2_metrics_show_download_and_status() -> None:
    item = account(
        id=17,
        email="customer@example.com",
        transfer_enable=20 * GIB,
        u=8 * GIB,
        d=2 * GIB,
        expired_at=0,
    )
    download, status = _v2_usage_metrics(item)
    assert download == 2.0
    assert status == "🟢"


def test_v2_unlimited_metrics_still_show_real_download() -> None:
    item = account(
        id=18,
        email="unlimited@example.com",
        transfer_enable=1000 * GIB,
        u=8 * GIB,
        d=2 * GIB,
        expired_at=0,
    )
    download, _status = _v2_usage_metrics(item)
    assert download == 2.0


def test_v2_pagination_supports_one_and_three_page_steps() -> None:
    assert _v2_page_targets(5, 10) == (2, 4, 6, 8)
    assert _v2_page_targets(1, 10) == (1, 1, 2, 4)
    assert _v2_page_targets(10, 10) == (7, 9, 10, 10)
