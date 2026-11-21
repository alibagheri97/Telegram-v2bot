"""Group topic discovery and root-only configuration commands."""

from __future__ import annotations

import logging

import requests
from telebot.apihelper import ApiTelegramException

from .expiry import save_setting, setting

log = logging.getLogger(__name__)
TOPICS = {
    "recipt": ("payment", "رسیدهای پرداخت"),
    "expired": ("expired", "اشتراک‌های منقضی"),
    "emergency-trigger": ("emergency", "شارژ اضطراری"),
}
ALIASES = {
    "receipt": "recipt",
    "expire": "expired",
    "expiration": "expired",
    "emergency": "emergency-trigger",
    "emergency_trigger": "emergency-trigger",
}


def topic_help(controller, message):
    controller.bot.reply_to(
        message,
        "<b>📚 راهنمای تاپیک‌های ربات</b>\n"
        "ربات را مدیر گروه کنید. مدیر اصلی ربات که مدیر گروه هم هست، داخل تاپیک موردنظر بفرستد:\n\n"
        "🧾 رسیدها: <code>/set-topic recipt</code>\n"
        "⏳ انقضا: <code>/set-topic expired</code>\n"
        "🆘 شارژ اضطراری: <code>/set-topic emergency-trigger</code>\n\n"
        "📋 فهرست اتصال‌ها: <code>/topics</code> یا <code>/list-topics</code>\n"
        "🗑 حذف اتصال: <code>/unset-topic recipt</code>، <code>/unset-topic expired</code>، "
        "<code>/unset-topic emergency-trigger</code>\n"
        "حذف اتصال فقط از همان گروه ممکن است. نام تاپیک مهم نیست؛ شناسه همان تاپیک ثبت می‌شود.\n"
        "نسخه با زیرخط نیز قابل استفاده است: <code>/set_topic</code>، "
        "<code>/unset_topic</code>، <code>/list_topics</code>.\n"
        "فعال/متوقف کردن سرویس‌ها از داشبورد مدیر اصلی ← مدیریت سرویس‌ها انجام می‌شود.",
        parse_mode="HTML",
    )


def configure_topic(controller, message, action="set"):
    if not controller._authorized(message.from_user.id, root=True):
        controller.bot.reply_to(message, "فقط مدیر اصلی ربات می‌تواند topic را تنظیم یا مشاهده کند.")
        return
    if message.chat.type not in {"group", "supergroup"}:
        controller.bot.reply_to(message, "این دستور باید داخل گروه اجرا شود؛ راهنما: /help")
        return
    try:
        bot_member = controller.bot.get_chat_member(message.chat.id, controller.bot.get_me().id)
        sender = controller.bot.get_chat_member(message.chat.id, message.from_user.id)
    except (ApiTelegramException, requests.RequestException):
        log.warning("could not verify group administrators for topic configuration")
        controller.bot.reply_to(message, "امکان بررسی مدیران گروه وجود ندارد؛ دوباره تلاش کنید.")
        return
    if bot_member.status not in {"administrator", "creator"} or sender.status not in {
        "administrator",
        "creator",
    }:
        controller.bot.reply_to(message, "فرستنده و ربات باید مدیر گروه باشند.")
        return
    if action == "list":
        lines = ["<b>📋 تاپیک‌های ثبت‌شده</b>"]
        with controller.database.session() as db:
            for name, (prefix, title) in TOPICS.items():
                group = setting(db, f"{prefix}_group_id", "0")
                topic = setting(db, f"{prefix}_topic_id", "0")
                state = (
                    f"گروه <code>{int(group)}</code> | تاپیک <code>{int(topic)}</code>"
                    if int(group) and int(topic)
                    else "➖ تنظیم نشده"
                )
                lines.append(f"{title} ({name}): {state}")
        controller.bot.reply_to(message, "\n".join(lines), parse_mode="HTML")
        return
    parts = message.text.split()
    key = ALIASES.get(parts[1].lower(), parts[1].lower()) if len(parts) == 2 else ""
    if key not in TOPICS:
        topic_help(controller, message)
        return
    prefix, title = TOPICS[key]
    if action == "set":
        topic_id = getattr(message, "message_thread_id", None)
        if not getattr(message.chat, "is_forum", False) or not topic_id:
            controller.bot.reply_to(
                message, "این دستور را داخل تاپیک موردنظر یک گروه Forum اجرا کنید."
            )
            return
        with controller.database.session() as db:
            save_setting(db, f"{prefix}_group_id", message.chat.id)
            save_setting(db, f"{prefix}_topic_id", topic_id)
            save_setting(db, f"{prefix}_group_title", message.chat.title or "")
        controller.bot.reply_to(message, f"✅ این تاپیک برای {title} ثبت شد.")
    elif action == "unset":
        with controller.database.session() as db:
            group = int(setting(db, f"{prefix}_group_id", "0"))
            if group and group != message.chat.id:
                controller.bot.reply_to(
                    message, "این اتصال مربوط به گروه دیگری است؛ از همان گروه حذف کنید."
                )
                return
            for suffix, value in [("group_id", 0), ("topic_id", 0), ("group_title", "")]:
                save_setting(db, f"{prefix}_{suffix}", value)
        controller.bot.reply_to(message, f"✅ اتصال تاپیک {title} حذف شد.")
