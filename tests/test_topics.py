from types import SimpleNamespace

import pytest
from telebot import TeleBot, types
from test_auth import controller_for, message

from vpnshopbot.expiry import setting


def group(text, actor=42, chat_id=-100123456, topic=77):
    return SimpleNamespace(
        text=text,
        from_user=SimpleNamespace(id=actor),
        chat=SimpleNamespace(id=chat_id, type="supergroup", is_forum=True, title="Test"),
        message_thread_id=topic,
    )


def root_controller():
    controller, database, bot = controller_for()
    controller.auth_command(message("123456"))
    return controller, database, bot


def test_group_can_set_all_three_topics_independently():
    controller, database, _ = root_controller()
    for key, topic in [("recipt", 10), ("expired", 20), ("emergency-trigger", 30)]:
        controller.set_topic_command(group(f"/set-topic {key}", topic=topic))
    with database.session() as db:
        assert setting(db, "payment_topic_id") == "10"
        assert setting(db, "expired_topic_id") == "20"
        assert setting(db, "emergency_topic_id") == "30"


def test_unset_only_selected_topic_and_list_remaining_connections():
    controller, database, bot = root_controller()
    controller.set_topic_command(group("/set-topic expired", topic=10))
    controller.set_topic_command(group("/set-topic emergency-trigger", topic=20))
    controller.unset_topic_command(group("/unset-topic emergency-trigger"))
    with database.session() as db:
        assert setting(db, "expired_topic_id") == "10"
        assert setting(db, "emergency_topic_id") == "0"
        assert setting(db, "emergency_group_id") == "0"
    controller.topics_command(group("/topics"))
    assert "expired" in bot.replies[-1][0]
    assert "emergency-trigger" in bot.replies[-1][0]
    assert "تنظیم نشده" in bot.replies[-1][0]


def test_cannot_unset_connection_from_another_group():
    controller, database, bot = root_controller()
    controller.set_topic_command(group("/set-topic emergency-trigger"))
    controller.unset_topic_command(group("/unset-topic emergency-trigger", chat_id=-100999999))
    with database.session() as db:
        assert setting(db, "emergency_group_id") == "-100123456"
    assert "گروه دیگری" in bot.replies[-1][0]


def test_nonroot_cannot_change_or_list_topics_but_can_read_help():
    controller, database, bot = root_controller()
    controller.set_topic_command(group("/set-topic emergency-trigger", actor=99))
    controller.unset_topic_command(group("/unset-topic expired", actor=99))
    controller.topics_command(group("/topics", actor=99))
    with database.session() as db:
        assert setting(db, "emergency_group_id") == ""
    assert "مدیر اصلی" in bot.replies[-1][0]
    controller.help_command(group("/help", actor=99))
    for command in [
        "/set-topic expired",
        "/set-topic emergency-trigger",
        "/unset-topic",
        "/topics",
    ]:
        assert command in bot.replies[-1][0]


@pytest.mark.parametrize("who", [42, 777])
def test_both_root_sender_and_bot_must_be_group_admins(who):
    controller, database, bot = root_controller()
    bot.get_chat_member = lambda chat, uid: SimpleNamespace(
        status="member" if uid == who else "administrator"
    )
    controller.set_topic_command(group("/set-topic emergency-trigger"))
    with database.session() as db:
        assert setting(db, "emergency_group_id") == ""


def test_topic_set_requires_actual_forum_thread():
    controller, database, _ = root_controller()
    controller.set_topic_command(group("/set-topic emergency-trigger", topic=None))
    with database.session() as db:
        assert setting(db, "emergency_topic_id") == ""


@pytest.mark.parametrize("command", ["/set-topic", "/set_topic", "/set_topic@test_bot"])
def test_real_telebot_dispatch_routes_topic_commands(command):
    controller, database, _ = root_controller()
    bot = TeleBot("123456:test-token", threaded=False)
    replies = []
    bot.reply_to = lambda message, text, **kwargs: replies.append(text)
    bot.get_me = lambda: SimpleNamespace(id=777, username="test_bot")
    bot.get_chat_member = lambda *args: SimpleNamespace(status="administrator")
    controller.bot = bot
    controller.register_handlers()
    update = types.Message.de_json(
        {
            "message_id": 123,
            "date": 1,
            "from": {"id": 42, "first_name": "Root", "is_bot": False},
            "chat": {"id": -100123456, "type": "supergroup", "title": "Test", "is_forum": True},
            "message_thread_id": 66,
            "text": f"{command} emergency-trigger",
        }
    )
    bot.process_new_messages([update])
    with database.session() as db:
        assert setting(db, "emergency_topic_id") == "66"
    assert "ثبت شد" in replies[-1]
