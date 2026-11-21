from types import SimpleNamespace

import pytest
from sqlalchemy import select
from test_catalog import shop as shop  # noqa: PLC0414 -- shared fixture dependency
from test_expiry import callback
from test_expiry import expiry as expiry  # noqa: PLC0414 -- shared fixture

from vpnshopbot.models import Product, User, V2ManualPayment
from vpnshopbot.panel import GIB


def buttons(env):
    return [button for row in env.controller.screens[-1][1].keyboard for button in row]


def test_card_has_screenshot_details_and_personal_bot_user_button(expiry):
    expiry.link(
        1,
        creator_role=-1,
        kind="purchase",
        remarks="<hello>",
        last_login_at=1000,
        created_at=1000,
        transfer_enable=1_000_000 * GIB,
    )
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    text = expiry.controller.screens[-1][0]
    for label in [
        "وضعیت اکانت",
        "نوع بسته",
        "دانلود",
        "آپلود",
        "حجم باقی‌مانده",
        "تاریخ انقضا",
        "مجوز",
        "دعوت‌کننده",
        "تاریخ ساخت",
        "کلید اتصال هوشمند",
    ]:
        assert label in text
    assert "♾️ نامحدود" in text
    assert "&lt;hello&gt;" in text
    assert "داده معتبر در پنل موجود نیست" in text
    assert any(button.callback_data == "v2:owner:1" for button in buttons(expiry))


@pytest.mark.parametrize(
    "owner,managed,role,kind",
    [
        (20, True, 1, "managed"),
        (10, True, 0, "root_registered"),
        (20, False, 1, "purchase"),
        (10, False, 0, "purchase"),
    ],
)
def test_creator_admin_or_root_has_no_customer_button_even_with_stale_callback(
    expiry, owner, managed, role, kind
):
    expiry.link(1, owner=owner, managed=managed, creator_role=role, kind=kind)
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    assert all(button.callback_data != "v2:owner:1" for button in buttons(expiry))
    with pytest.raises(ValueError, match="حساب شخصی"):
        expiry.controller._v2_action(callback("v2:owner:1"), "v2:owner:1")


def test_customer_button_opens_correct_bot_user(expiry):
    expiry.link(1, creator_role=-1, kind="purchase")
    expiry.controller._v2_action(callback("v2:owner:1"), "v2:owner:1")
    assert "User 40" in expiry.controller.screens[-1][0]


def test_screenshot_actions_keep_extra_existing_controls(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    callbacks = {button.callback_data for button in buttons(expiry)}
    for action in [
        "custom",
        "payment",
        "package",
        "remarks",
        "ban",
        "resetask",
        "renewask",
        "deleteask",
        "emergency",
        "qr",
    ]:
        assert f"v2:{action}:1" in callbacks
    assert "emergency:renew:1:0" in callbacks


def enter(env, action, value):
    env.controller._v2_action(callback(f"v2:{action}:1"), f"v2:{action}:1")
    with env.database.session() as db:
        state = env.service.get_state(db, 10)
        request = callback("")
        env.controller._v2_input_message(
            db,
            SimpleNamespace(text=value, from_user=request.from_user, chat=request.message.chat),
            dict(state.data),
        )


def apply_confirmation(env):
    data = next(
        button.callback_data
        for button in buttons(env)
        if button.callback_data.startswith("v2:applyextra:")
    )
    env.controller._v2_action(callback(data), data)


def test_manual_payment_needs_confirmation_and_never_changes_wallet_or_expiry(expiry):
    expiry.link(1)
    before = dict(expiry.accounts[1])
    enter(expiry, "payment", "450000")
    with expiry.database.session() as db:
        assert not db.scalars(select(V2ManualPayment)).all()
    apply_confirmation(expiry)
    with expiry.database.session() as db:
        assert db.scalar(select(V2ManualPayment.amount)) == 450000
        assert db.get(User, 40).balance == 1000000
    assert expiry.accounts[1] == before
    with pytest.raises(ValueError, match="قدیمی"):
        expiry.controller._v2_action(callback("v2:applyextra:1"), "v2:applyextra:1")


def test_back_to_account_cancels_payment_confirmation(expiry):
    expiry.link(1)
    enter(expiry, "payment", "450000")
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    with pytest.raises(ValueError, match="قدیمی"):
        expiry.controller._v2_action(callback("v2:applyextra:1"), "v2:applyextra:1")


def test_custom_days_extend_only_time_and_require_confirmation(expiry):
    expiry.link(1)
    enter(expiry, "custom", "12")
    with expiry.database.session() as db:
        target = expiry.service.get_state(db, 10).data["after"]
    assert expiry.accounts[1]["expired_at"] != target
    apply_confirmation(expiry)
    assert expiry.accounts[1]["expired_at"] == target
    assert expiry.accounts[1]["u"] == expiry.accounts[1]["d"] == GIB


def test_package_preview_does_not_reset_time_usage_or_wallet(expiry):
    expiry.link(1)
    with expiry.database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        product_id = product.id
    original = dict(expiry.accounts[1])
    expiry.panel.set_package_limits = lambda account_id, **kwargs: expiry.accounts[
        account_id
    ].update(transfer_enable=kwargs["transfer_gb"] * GIB, device_limit=kwargs["device_limit"])
    data = f"v2:pickplan:1:{product_id}"
    expiry.controller._v2_action(callback(data), data)
    assert expiry.accounts[1] == original
    apply_confirmation(expiry)
    for field in ["expired_at", "u", "d"]:
        assert expiry.accounts[1][field] == original[field]
    with expiry.database.session() as db:
        assert db.get(User, 40).balance == 1000000


def test_payment_history_is_scoped_and_root_only(expiry):
    expiry.link(1)
    with expiry.database.session() as db:
        db.add(V2ManualPayment(v2_user_id=1, amount=100, actor_telegram_id=10))
        db.add(V2ManualPayment(v2_user_id=2, amount=200, actor_telegram_id=10))
        db.flush()
        actor = db.get(User, 10)
        source = expiry.controller._driving_source(db, actor, "vpay1")
        view = expiry.controller._driving_load(db, actor, "vpay1", source)
        rows, total, _, _ = expiry.controller._driving_query(db, source, view)
        assert total == 1 and rows[0]["amount"] == 100
        with pytest.raises(ValueError, match="مدیر اصلی"):
            expiry.controller._driving_source(db, db.get(User, 20), "vpay1")
