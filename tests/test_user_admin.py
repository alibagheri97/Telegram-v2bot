from types import SimpleNamespace

import pytest
import requests
from sqlalchemy import select
from telebot import TeleBot, types
from telebot.handler_backends import ContinueHandling
from test_catalog import shop as shop  # noqa: PLC0414 -- re-export the pytest fixture

from vpnshopbot.audit import backfill_actions, receipt_message_url, record_action
from vpnshopbot.models import (
    BotSetting,
    Order,
    OrderStatus,
    PaymentReceipt,
    ProductType,
    User,
    UserAction,
    UserStatus,
    WalletTransaction,
)


class Messages:
    def __init__(self):
        self.messages = []
        self.replies = []
        self.copies = []
        self.forwards = []
        self.alerts = []
        self.failed = False

    def send_message(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(self.messages), chat=SimpleNamespace(id=chat_id))

    def reply_to(self, message, text, **kwargs):
        self.replies.append((text, kwargs))

    def answer_callback_query(self, *args, **kwargs):
        self.alerts.append((args, kwargs))

    def copy_message(self, chat_id, from_chat_id, message_id, **kwargs):
        if self.failed:
            raise requests.ConnectionError("unreachable")
        self.copies.append((chat_id, from_chat_id, message_id, kwargs))
        return SimpleNamespace(message_id=700)

    def forward_message(self, chat_id, from_chat_id, message_id):
        self.forwards.append((chat_id, from_chat_id, message_id))
        return SimpleNamespace(message_id=701)


@pytest.fixture
def controls(shop):
    database, _panel, _service, controller = shop
    controller.settings.support_username = "@staff_support"
    controller.bot = Messages()
    controller.screens = []
    controller._edit = lambda call, text, markup: controller.screens.append((text, markup))
    return controller, database


def call(data, actor=10):
    return SimpleNamespace(
        id=f"callback-{data}",
        data=data,
        from_user=SimpleNamespace(id=actor),
        message=SimpleNamespace(chat=SimpleNamespace(id=actor, type="private")),
    )


def message(text=None, *, actor=10, message_id=500, kind="text"):
    return SimpleNamespace(
        text=text,
        caption=None,
        content_type=kind,
        message_id=message_id,
        chat=SimpleNamespace(id=actor, type="private"),
        from_user=SimpleNamespace(id=actor),
    )


def test_promote_button_and_repeated_click_keep_user_admin(controls):
    controller, database = controls
    controller._user_action(call("user:view:40"), "user:view:40")
    buttons = [button for row in controller.screens[-1][1].keyboard for button in row]
    assert any(button.callback_data == "user:promote:40" for button in buttons)
    for _ in range(2):
        controller._user_action(call("user:promote:40"), "user:promote:40")
    with database.session() as db:
        assert db.get(User, 40).permission == 1
        events = db.scalars(select(UserAction).where(UserAction.action == "user.role_changed")).all()
        assert len(events) == 1
    buttons = [button for row in controller.screens[-1][1].keyboard for button in row]
    assert any(button.callback_data == "user:promoteroot:40" for button in buttons)
    assert any(button.text == "✅ ادمین" for button in buttons)


def test_root_promotion_buttons_confirmation_and_replay(controls):
    controller, database = controls
    controller._user_action(call("user:view:40"), "user:view:40")
    assert any([button.callback_data for button in row] == ["user:promote:40", "user:promoteroot:40"]
               for row in controller.screens[-1][1].keyboard)
    controller._user_action(call("user:promoteroot:40"), "user:promoteroot:40")
    with database.session() as db:
        assert db.get(User, 40).permission == -1
    assert "دسترسی کامل" in controller.screens[-1][0]
    for _ in range(2):
        controller._user_action(call("user:confirmroot:40"), "user:confirmroot:40")
    with database.session() as db:
        assert db.get(User, 40).is_root
        assert len(db.scalars(select(UserAction).where(UserAction.action == "user.role_changed")).all()) == 1


def test_admin_cannot_promote_user_to_root(controls):
    controller, database = controls
    controller._user_action(call("user:confirmroot:40", actor=20), "user:confirmroot:40")
    with database.session() as db:
        assert db.get(User, 40).permission == -1


def test_deactivation_reason_blocks_commands_and_stale_buttons_until_reactivation(controls):
    controller, database = controls
    controller._user_action(call("user:block:40"), "user:block:40")
    controller._observe_message(message("نیاز به بررسی پرداخت"))
    with database.session() as db:
        assert db.get(User, 40).status == UserStatus.BLOCKED
        assert db.get(BotSetting, "user_deactivation_reason:40").value == "نیاز به بررسی پرداخت"
    assert controller._observe_message(message("/start", actor=40)) is None
    assert controller._observe_callback(call("buy:start", actor=40)) is None
    assert "نیاز به بررسی پرداخت" in controller.bot.messages[-1][1]
    assert (
        controller.bot.messages[-1][2]["reply_markup"].keyboard[0][0].url
        == "https://t.me/staff_support"
    )
    controller._user_action(call("user:block:40"), "user:block:40")
    with database.session() as db:
        assert db.get(User, 40).status == UserStatus.ACTIVE
    assert isinstance(
        controller._observe_message(message("/start", actor=40, message_id=501)), ContinueHandling
    )


def test_wallet_change_is_confirmed_audited_and_cannot_replay(controls):
    controller, database = controls
    controller._user_action(call("user:wallet:40"), "user:wallet:40")
    controller._observe_message(message("+50000"))
    with database.session() as db:
        assert db.get(User, 40).balance == 1_000_000
    controller._user_action(call("user:walletapply:40"), "user:walletapply:40")
    with database.session() as db:
        assert db.get(User, 40).balance == 1_050_000
        transactions = list(db.scalars(select(WalletTransaction)))
        assert len(transactions) == 1 and transactions[0].amount == 50_000
        event = db.scalar(select(UserAction).where(UserAction.action == "wallet.adjusted"))
        assert event.actor_telegram_id == 10
        assert event.details == {"before": 1_000_000, "after": 1_050_000, "amount": 50_000}
    with pytest.raises(ValueError, match="قبلاً"):
        controller._user_action(call("user:walletapply:40"), "user:walletapply:40")


def test_wallet_confirmation_detects_concurrent_balance_change(controls):
    controller, database = controls
    controller._user_action(call("user:wallet:40"), "user:wallet:40")
    controller._observe_message(message("200000"))
    with database.session() as db:
        db.get(User, 40).balance += 1
    with pytest.raises(ValueError, match="موجودی کاربر تغییر کرده"):
        controller._user_action(call("user:walletapply:40"), "user:walletapply:40")
    with database.session() as db:
        assert db.get(User, 40).balance == 1_000_001
        assert db.scalar(select(WalletTransaction)) is None


def test_negative_wallet_result_is_rejected_without_mutation(controls):
    controller, database = controls
    controller._user_action(call("user:wallet:40"), "user:wallet:40")
    controller._observe_message(message("-1000001"))
    with database.session() as db:
        assert db.get(User, 40).balance == 1_000_000
        assert controller.service.get_state(db, 10).state == "root_user_wallet"


def test_admin_cannot_change_wallet_or_read_user_history(controls):
    controller, database = controls
    controller._user_action(call("user:wallet:40", actor=20), "user:wallet:40")
    with database.session() as db:
        assert controller.service.get_state(db, 20) is None
        assert db.get(User, 40).balance == 1_000_000
    with pytest.raises(ValueError, match="دسترسی مدیر اصلی"):
        controller._driving_render(call("", actor=20), "events40")
    with pytest.raises(ValueError, match="دسترسی مدیر اصلی"):
        controller._driving_render(call("", actor=20), "uv40")


def test_root_media_copy_includes_direct_support_button_and_history(controls):
    controller, database = controls
    controller._user_action(call("user:sendcopy:40"), "user:sendcopy:40")
    assert controller._observe_message(message(kind="photo")) is None
    assert controller.bot.copies[0][:3] == (40, 10, 500)
    markup = controller.bot.copies[0][3]["reply_markup"]
    assert markup.keyboard[0][0].url == "https://t.me/staff_support"
    with database.session() as db:
        assert controller.service.get_state(db, 10) is None
        event = db.scalar(select(UserAction).where(UserAction.action == "root.message_sent"))
        assert event.user_id == 40 and event.details["content_type"] == "photo"


def test_root_forward_is_preserved_and_support_is_attached_as_reply(controls):
    controller, _ = controls
    controller._user_action(call("user:sendforward:40"), "user:sendforward:40")
    controller._observe_message(message("فوروارد این پیام"))
    assert controller.bot.forwards == [(40, 10, 500)]
    _, _, kwargs = controller.bot.messages[-1]
    assert kwargs["reply_to_message_id"] == 701
    assert kwargs["reply_markup"].keyboard[0][0].url.startswith("https://t.me/")


def test_failed_message_delivery_keeps_compose_state_without_false_success(controls):
    controller, database = controls
    controller._user_action(call("user:sendcopy:40"), "user:sendcopy:40")
    controller.bot.failed = True
    controller._observe_message(message("Hello"))
    with database.session() as db:
        assert controller.service.get_state(db, 10).state == "root_user_send"
        assert db.scalar(select(UserAction).where(UserAction.action == "root.message_sent")) is None


def test_receipt_event_has_link_to_exact_group_message(controls):
    controller, database = controls
    with database.session() as db:
        order = Order(
            user_id=40,
            product_type=ProductType.DATA,
            data_gb=10,
            amount=50_000,
            status=OrderStatus.PENDING_REVIEW,
        )
        db.add(order)
        db.flush()
        receipt = PaymentReceipt(
            user_id=40,
            order_id=order.id,
            telegram_file_id="file",
            media_type="photo",
            review_chat_id=-100987654,
            review_message_id=123,
        )
        db.add(receipt)
        db.flush()
        event = record_action(
            db,
            40,
            "receipt.submitted",
            "📎 ارسال رسید",
            details={"receipt_id": receipt.id, "order_id": order.id, "review_topic_id": 2},
        )
        db.flush()
        event_id = event.id
    controller._user_action(call(f"user:event:{event_id}"), f"user:event:{event_id}")
    urls = [
        button.url for row in controller.screens[-1][1].keyboard for button in row if button.url
    ]
    assert urls == ["https://t.me/c/987654/123?thread=2"]


def test_historical_backfill_is_idempotent_and_does_not_invent_clicks(controls):
    _, database = controls
    with database.session() as db:
        db.add(
            Order(
                user_id=40,
                product_type=ProductType.DATA,
                data_gb=10,
                amount=50_000,
                status=OrderStatus.PAID,
            )
        )
        db.flush()
        backfill_actions(db, 40)
        db.flush()
        backfill_actions(db, 40)
        actions = list(db.scalars(select(UserAction)))
        assert len(actions) == 2
        assert {a.action for a in actions} == {"order.created", "order.paid"}


def test_private_receipt_does_not_generate_invalid_group_url():
    assert receipt_message_url(1234, 5) is None


def test_real_telebot_dispatch_stops_blocked_commands_before_business_handlers(controls):
    controller, database = controls
    with database.session() as db:
        db.get(User, 40).status = UserStatus.BLOCKED
        db.add(BotSetting(key="user_deactivation_reason:40", value="پرداخت نامشخص"))
    delivery = controller.bot
    controller.bot = TeleBot("123:ABC", threaded=False)
    controller.bot.send_message = delivery.send_message
    controller.bot.answer_callback_query = delivery.answer_callback_query

    def forbidden(*_args):
        pytest.fail("Blocked update reached business handler")

    controller.start = forbidden
    controller._buy_callback = forbidden
    controller.register_handlers()
    incoming = {
        "message_id": 900,
        "date": 100,
        "chat": {"id": 40, "type": "private"},
        "from": {"id": 40, "is_bot": False, "first_name": "User"},
        "text": "/start",
    }
    controller.bot.process_new_messages([types.Message.de_json(incoming)])
    query = types.CallbackQuery.de_json(
        {
            "id": "stale-button",
            "from": incoming["from"],
            "chat_instance": "test",
            "data": "buy:start",
            "message": incoming,
        }
    )
    controller.bot.process_new_callback_query([query])
    assert len(delivery.messages) == 2
    assert all("پرداخت نامشخص" in text for _, text, _ in delivery.messages)


def test_authentication_code_is_not_stored_in_audit_history(controls):
    controller, database = controls
    controller._observe_message(message("/auth 123456"))
    with database.session() as db:
        event = db.scalar(select(UserAction).where(UserAction.action == "interaction.received"))
        assert event.details["text"] == "/auth"
        assert "123456" not in str(event.details)


def test_blocked_account_cannot_create_order_via_service(controls):
    controller, database = controls
    with database.session() as db:
        user = db.get(User, 40)
        user.status = UserStatus.BLOCKED
        with pytest.raises(ValueError, match="غیرفعال"):
            controller.service.create_order(db, user, ProductType.DATA, data_gb=10)
        assert db.scalar(select(Order)) is None
