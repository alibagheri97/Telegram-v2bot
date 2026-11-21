from vpnshopbot.keyboards import account_actions, connections, root_home, user_home


def test_user_dashboard_contains_previous_order_button_below_support() -> None:
    rows = user_home().keyboard
    assert rows[-2][0].callback_data == "support:show"
    assert rows[-1][0].callback_data == "orders:open"


def test_subscription_actions_have_clear_icons() -> None:
    buttons = [button for row in account_actions(12).keyboard for button in row]
    assert [button.text.split()[0] for button in buttons] == ["🔗", "🔁", "📚", "🔙"]


def test_connection_options_use_user_friendly_account_names() -> None:
    buttons = connections().keyboard[0]
    assert [button.text for button in buttons] == ["👤 یک‌کاربره", "👥 دوکاربره"]


def test_root_dashboard_does_not_expose_user_dashboard() -> None:
    callbacks = [button.callback_data for row in root_home().keyboard for button in row]
    assert "home:user" not in callbacks
