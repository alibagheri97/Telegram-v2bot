import pytest
from test_catalog import shop as shop  # noqa: PLC0414 -- fixture dependency
from test_expiry import callback
from test_expiry import expiry as expiry  # noqa: PLC0414 -- shared fixture

from vpnshopbot.expiry import setting
from vpnshopbot.models import User


def screen(env, data):
    env.controller._expiry_callback(callback(data), data)
    text, markup = env.controller.screens[-1]
    return text, [button.callback_data for row in markup.keyboard for button in row]


def test_service_index_is_only_a_short_list_with_status(expiry):
    text, callbacks = screen(expiry, "expiry:home")
    assert len(text.splitlines()) <= 4
    assert callbacks == [
        "expiry:service:expire",
        "expiry:service:emergency",
        "expiry:service:auto",
        "expiry:home",
        "root:home",
    ]
    assert "expiry:toggle" not in callbacks and "emergency:toggle" not in callbacks


@pytest.mark.parametrize(
    "service,action,excluded",
    [
        ("expire", "expiry:toggle", "emergency:toggle"),
        ("emergency", "emergency:toggle", "expiry:auto"),
        ("auto", "expiry:auto", "expiry:toggle"),
    ],
)
def test_service_details_have_only_their_own_controls(expiry, service, action, excluded):
    _text, callbacks = screen(expiry, f"expiry:service:{service}")
    assert action in callbacks and excluded not in callbacks
    assert f"expiry:service:{service}" in callbacks
    assert callbacks[-1] == "expiry:home"


def test_actions_return_to_the_selected_service_not_the_index(expiry):
    text, callbacks = screen(expiry, "expiry:toggle")
    assert "اعلان انقضا" in text and "expiry:service:expire" in callbacks
    expiry.configure(emergency_group_id=-100123456, emergency_topic_id=77)
    expiry.controller._emergency_callback(callback("emergency:toggle"), "emergency:toggle")
    text, markup = expiry.controller.screens[-1]
    assert "شارژ اضطراری" in text
    assert any(
        button.callback_data == "expiry:service:emergency"
        for row in markup.keyboard
        for button in row
    )


def test_browsing_and_refresh_do_not_change_service_state(expiry):
    for service in ["expire", "emergency", "auto"]:
        screen(expiry, f"expiry:service:{service}")
    with expiry.database.session() as db:
        assert setting(db, "service:expire:enabled", "0") == "0"
        assert setting(db, "service:emergency:enabled", "0") == "0"
        assert setting(db, "service:auto_renew:enabled", "1") == "1"


@pytest.mark.parametrize(
    "service,command", [("expire", "expired"), ("emergency", "emergency-trigger")]
)
def test_each_service_has_its_own_topic_guide_and_back_button(expiry, service, command):
    text, callbacks = screen(expiry, f"expiry:topic:{service}")
    assert f"/set-topic {command}" in text
    assert f"/unset-topic {command}" in text
    assert callbacks == [f"expiry:service:{service}"]


def test_service_lists_return_to_matching_detail_page(expiry):
    with expiry.database.session() as db:
        root = db.get(User, 10)
        assert (
            expiry.controller._driving_source(db, root, "expired").back == "expiry:service:expire"
        )
        assert (
            expiry.controller._driving_source(db, root, "emergency").back
            == "expiry:service:emergency"
        )


def test_service_detail_is_root_only(expiry):
    with pytest.raises(ValueError, match="مدیر اصلی"):
        expiry.controller._expiry_callback(
            callback("expiry:service:expire", actor=20), "expiry:service:expire"
        )
