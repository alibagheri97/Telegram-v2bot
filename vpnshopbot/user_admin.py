"""Root-only bot-user actions and access guards for Telegram updates."""

from __future__ import annotations

import html
import logging
import re
import secrets

import requests
from sqlalchemy import func, select
from telebot import types, util
from telebot.apihelper import ApiTelegramException
from telebot.handler_backends import ContinueHandling

from . import keyboards
from .archive import is_archived
from .audit import backfill_actions, receipt_message_url, record_action
from .emergency import monthly_info
from .models import (
    BotSetting,
    ConversationState,
    PaymentReceipt,
    Permission,
    User,
    UserAction,
    UserStatus,
    V2AccountLink,
    WalletTransaction,
)
from .services import toman
from .v2_admin import customer_owner
from .wallet import credit_key, wallet_floor

log = logging.getLogger(__name__)


def support_url(value: str) -> str:
    value = value.strip()
    if value.isdecimal():
        return f"tg://user?id={int(value)}"
    if value.startswith("https://t.me/"):
        value = value.removeprefix("https://t.me/")
    username = value.lstrip("@")
    if not re.fullmatch(r"[a-zA-Z0-9_]{5,32}", username):
        raise ValueError("شناسه پشتیبانی معتبر نیست؛ از @username یا شناسه عددی استفاده کنید")
    return f"https://t.me/{username}"


class UserAdminMixin:
    def _support_markup(self):
        with self.database.session() as db:
            setting = db.get(BotSetting, "support_username")
            value = setting.value if setting else self.settings.support_username
        markup = types.InlineKeyboardMarkup()
        markup.row(types.InlineKeyboardButton("💬 تماس با پشتیبانی", url=support_url(value)))
        return markup

    def _safe_notify_user(self, telegram_id, text):
        try:
            markup = self._support_markup()
        except ValueError:
            log.warning("invalid support configuration; sending account notice without a link")
            markup = None
        try:
            self.bot.send_message(telegram_id, text, parse_mode="HTML", reply_markup=markup)
            return True
        except (ApiTelegramException, requests.RequestException, ValueError):
            log.warning("could not notify bot user %s", telegram_id)
            return False

    def _observe_update(self, update, *, callback=False):
        sender = getattr(update, "from_user", None)
        if not sender:
            return ContinueHandling()
        blocked_reason = None
        with self.database.session() as db:
            user = self.service.get_user(db, sender.id)
            if user:
                blocked = user.status == UserStatus.BLOCKED
                if blocked:
                    reason = db.get(BotSetting, f"user_deactivation_reason:{user.id}")
                    blocked_reason = (
                        reason.value if reason else "حساب شما توسط مدیر غیرفعال شده است."
                    )
                if callback:
                    details = {"callback": str(update.data)[:100], "blocked": blocked}
                    source = f"callback:{update.id}"
                    title = "🚫 درخواست از حساب غیرفعال" if blocked else "🔘 انتخاب دکمه"
                else:
                    kind = getattr(update, "content_type", "text")
                    text = getattr(update, "text", None) or getattr(update, "caption", None) or ""
                    # Authentication codes and root command arguments must not enter history.
                    if text.startswith("/"):
                        text = text.split()[0]
                    elif user.is_root:
                        text = "[ورودی مدیر اصلی]"
                    details = {
                        "content_type": kind,
                        "text": text[:500],
                        "blocked": blocked,
                        "chat_id": update.chat.id,
                        "message_id": update.message_id,
                    }
                    source = f"message:{update.chat.id}:{update.message_id}"
                    title = "🚫 پیام از حساب غیرفعال" if blocked else "📨 پیام ورودی"
                record_action(
                    db,
                    user.id,
                    "access.denied" if blocked else "interaction.received",
                    title,
                    details=details,
                    actor_id=sender.id,
                    source_key=source,
                )
        if blocked_reason is not None:
            if callback:
                self.bot.answer_callback_query(update.id, "حساب شما غیرفعال است.", show_alert=True)
                chat_id = sender.id
            else:
                chat_id = update.chat.id if update.chat.type == "private" else sender.id
            self._safe_notify_user(
                chat_id,
                "🚫 حساب شما غیرفعال است.\n\n📣 دلیل اعلام‌شده:\n" + html.escape(blocked_reason),
            )
            return None
        return ContinueHandling()

    def _observe_message(self, message):
        result = self._observe_update(message)
        if result is None:
            return None
        with self.database.session() as db:
            state = self.service.get_state(db, message.from_user.id) if message.from_user else None
            same_chat = state and state.data.get("chat_id", message.chat.id) == message.chat.id
            pending = state and state.state == "root_user_send" and same_chat
            control = (
                state
                and state.state in {"root_user_wallet", "root_user_deactivate", "root_user_rename", "root_user_creditlimit"}
                and same_chat
            )
            command = (getattr(message, "text", "") or "").split(maxsplit=1)[0:1]
            if (
                state
                and state.state.startswith("root_user_")
                and command
                and command[0].split("@")[0] in {"/start", "/cancel", "/root"}
            ):
                self.service.clear_state(db, message.from_user.id)
                pending, control = False, False
                if command[0].split("@")[0] == "/cancel":
                    self.bot.reply_to(message, "عملیات لغو شد.")
                    return None
            result_data = None
            if control and getattr(message, "text", None) and not message.text.startswith("/"):
                result_data = self._root_user_text(db, message, state)
        if control and getattr(message, "text", None) and not message.text.startswith("/"):
            if result_data:
                target_id, notification, back = result_data
                delivered = (
                    self._safe_notify_user(target_id, notification) if notification else True
                )
                self.bot.reply_to(
                    message,
                    "✅ تغییر ذخیره شد." + (" اعلان به کاربر ارسال نشد." if not delivered else ""),
                    reply_markup=back,
                )
            return None
        if pending and not (getattr(message, "text", "") or "").startswith("/"):
            try:
                self._send_user_message(message)
            except ValueError as exc:
                self.bot.reply_to(message, str(exc))
            return None
        return result

    def _observe_callback(self, call):
        return self._observe_update(call, callback=True)

    def _register_user_guards(self):
        self.bot.register_message_handler(
            self._observe_message, content_types=list(util.content_type_media), func=lambda _: True
        )
        self.bot.register_callback_query_handler(self._observe_callback, func=lambda _: True)

    def _user_detail(self, call, db, target):
        count = (
            db.scalar(
                select(func.count())
                .select_from(V2AccountLink)
                .where(V2AccountLink.user_id == target.id)
            )
            or 0
        )
        reason = db.get(BotSetting, f"user_deactivation_reason:{target.id}")
        role = {0: "👑 مدیر اصلی", 1: "🧑‍💼 ادمین", -1: "👤 کاربر"}.get(target.permission, "—")
        text = (
            f"<b>👤 {html.escape(target.name)}</b>\n🆔 کاربر #{target.id}\n"
            f"📱 Telegram ID: <code>{target.telegram_id}</code>\n"
            f"🔑 سطح: {role}\n🚦 وضعیت: {'🔴 غیرفعال' if target.status == UserStatus.BLOCKED else '🟢 فعال'}\n"
            f"💳 موجودی: {toman(target.balance)}\n📦 اکانت‌ها: {count}"
        )
        if target.status == UserStatus.BLOCKED and reason:
            text += f"\n📣 دلیل: {html.escape(reason.value)}"
        if target.permission == int(Permission.ADMIN):
            floor = wallet_floor(db, target)
            text += (f"\n🏦 کف مجاز کیف پول: {toman(floor)}\n💸 بدهی: {toman(max(0, -target.balance))}\n"
                     f"🛒 اعتبار قابل‌استفاده: {toman(max(0, target.balance - floor))}")
        status_label = "✅ فعال‌سازی" if target.status == UserStatus.BLOCKED else "🚫 غیرفعال‌سازی"
        self._edit(
            call,
            text,
            keyboards.inline(
                [
                    (status_label, f"user:block:{target.id}"),
                    ("💳 تغییر کیف پول", f"user:wallet:{target.id}"),
                ],
                [(f"📦 اکانت‌های V2 ({count})", f"dl:uv{target.id}:show")],
                [("📨 ارسال پیام / رسانه", f"user:send:{target.id}")],
                [("📜 تاریخچه فعالیت‌ها", f"user:history:{target.id}")],
                *([[("🏦 تنظیم حد منفی کیف پول", f"user:creditlimit:{target.id}")]]
                  if target.permission == int(Permission.ADMIN) else []),
                *(
                    [[
                        ("✅ ادمین" if target.permission == int(Permission.ADMIN) else "🧑‍💼 ارتقا به ادمین",
                         f"user:promote:{target.id}"),
                        ("👑 ارتقا به مدیر اصلی", f"user:promoteroot:{target.id}"),
                    ]]
                    if not target.is_root
                    else []
                ),
                [("✏ تغییر نام", f"user:rename:{target.id}")],
                [("🔙 کاربران", "dl:users:show")],
            ),
        )

    def _user_action(self, call, data):
        if not self._authorized(call.from_user.id, root=True):
            return
        parts = data.split(":")
        action = parts[1]
        if action == "search":
            self._driving_render(call, "users", "search")
            return
        if action == "event":
            self._show_user_event(call, int(parts[2]))
            return
        if action == "receipt":
            self._show_saved_receipt(call, int(parts[2]))
            return
        if action == "history":
            with self.database.session() as db:
                backfill_actions(db, int(parts[2]))
            self._driving_render(call, f"events{int(parts[2])}")
            return
        notify = None
        with self.database.session() as db:
            target = db.scalar(select(User).where(User.id == int(parts[2])).with_for_update())
            if not target:
                raise ValueError("کاربر پیدا نشد")
            cancel = keyboards.inline([("❌ انصراف", f"user:view:{target.id}")])
            if action == "view":
                state = self.service.get_state(db, call.from_user.id)
                if state and state.state.startswith("root_user_"):
                    self.service.clear_state(db, call.from_user.id)
                self._user_detail(call, db, target)
                return
            if action == "block":
                if target.is_root:
                    raise ValueError("مدیر اصلی قابل غیرفعال‌سازی نیست")
                if target.status != UserStatus.BLOCKED:
                    self.service.set_state(
                        db,
                        call.from_user.id,
                        "root_user_deactivate",
                        user_id=target.id,
                        chat_id=call.message.chat.id,
                    )
                    self._edit(
                        call,
                        "📣 دلیل غیرفعال‌سازی را بفرستید؛ همین متن به کاربر اعلام می‌شود.",
                        cancel,
                    )
                    return
                target.status = UserStatus.ACTIVE
                record_action(
                    db, target.id, "user.activated", "✅ فعال‌سازی حساب", actor_id=call.from_user.id
                )
                notify = (target.telegram_id, "✅ حساب شما توسط مدیر دوباره فعال شد.")
            elif action == "creditlimit":
                if target.permission != int(Permission.ADMIN):
                    raise ValueError("حد منفی فقط برای ادمین‌ها قابل تنظیم است")
                self.service.set_state(db, call.from_user.id, "root_user_creditlimit",
                    user_id=target.id, chat_id=call.message.chat.id)
                self._edit(call, f"🏦 کف فعلی: {toman(wallet_floor(db, target))}\n"
                    "کف مجاز را به تومان بفرستید؛ مثال: -5000000\n"
                    "صفر = بدون اعتبار منفی. این تنظیم موجودی را تغییر نمی‌دهد.", cancel)
                return
            elif action == "creditlimitapply":
                state = db.scalar(select(ConversationState).where(
                    ConversationState.telegram_id == call.from_user.id).with_for_update())
                if not state or state.state != "root_user_creditlimit_confirm" or state.data.get("user_id") != target.id or state.data.get("chat_id") != call.message.chat.id or len(parts) != 4 or state.data.get("nonce") != parts[3]:
                    raise ValueError("تأیید قدیمی یا نامعتبر است")
                if target.permission != int(Permission.ADMIN):
                    raise ValueError("این کاربر دیگر ادمین نیست")
                before, after = wallet_floor(db, target), int(state.data["after"])
                if before != state.data["before"] or not -10**12 <= after <= 0 or target.balance < after:
                    raise ValueError("وضعیت اعتبار یا بدهی تغییر کرده؛ ابتدا بدهی را تسویه یا درخواست را تکرار کنید")
                self._save_setting(db, credit_key(target.id), after)
                record_action(db, target.id, "wallet.credit_limit_changed", "🏦 تغییر حد منفی کیف پول",
                    actor_id=call.from_user.id, details={"before": before, "after": after})
                self.service.clear_state(db, call.from_user.id)
                notify = (target.telegram_id, f"🏦 کف مجاز کیف پول شما تغییر کرد: {toman(after)}")
            elif action in {"wallet", "credit", "debit"}:
                self.service.set_state(
                    db,
                    call.from_user.id,
                    "root_user_wallet",
                    user_id=target.id,
                    chat_id=call.message.chat.id,
                )
                self._edit(
                    call,
                    f"💳 موجودی فعلی: {toman(target.balance)}\n\n"
                    "موجودی جدید را به تومان بفرستید.\n"
                    "100000 = تنظیم موجودی\n+50000 = افزایش\n-50000 = کاهش\n"
                    "=-1000000 = تنظیم دقیق موجودی منفی (فقط ادمین و در محدوده اعتبار)",
                    cancel,
                )
                return
            elif action == "walletapply":
                state = db.scalar(
                    select(ConversationState)
                    .where(ConversationState.telegram_id == call.from_user.id)
                    .with_for_update()
                )
                if (
                    not state
                    or state.state != "root_user_wallet_confirm"
                    or state.data.get("user_id") != target.id
                ):
                    raise ValueError("درخواست منقضی شده یا قبلاً انجام شده است")
                payload = state.data
                if payload.get("chat_id", call.message.chat.id) != call.message.chat.id:
                    raise ValueError("تأیید را در همان گفت‌وگوی اولیه انجام دهید")
                if target.balance != payload["before"]:
                    raise ValueError("موجودی کاربر تغییر کرده؛ تغییر کیف پول را دوباره آغاز کنید")
                before, after = target.balance, int(payload["after"])
                if not wallet_floor(db, target) <= after <= 10**12:
                    raise ValueError("موجودی جدید خارج از حد مجاز فعلی کیف پول است")
                target.balance = after
                txn = WalletTransaction(
                    user_id=target.id,
                    amount=after - before,
                    kind="root_adjustment",
                    reference=f"root-wallet:{payload['request_id']}",
                    note=f"Root Telegram ID: {call.from_user.id}",
                )
                db.add(txn)
                db.flush()
                record_action(
                    db,
                    target.id,
                    "wallet.adjusted",
                    "💳 تغییر موجودی توسط مدیر",
                    actor_id=call.from_user.id,
                    source_key=f"wallet:{txn.id}",
                    details={"before": before, "after": after, "amount": after - before},
                )
                self.service.clear_state(db, call.from_user.id)
                notify = (
                    target.telegram_id,
                    f"💳 موجودی کیف پول شما توسط مدیر تغییر کرد.\nموجودی جدید: {toman(after)}",
                )
            elif action == "promoteroot":
                if target.is_root:
                    self._user_detail(call, db, target)
                    return
                self._edit(
                    call,
                    f"👑 آیا {html.escape(target.name)} به مدیر اصلی ارتقا یابد؟\n"
                    "⚠️ مدیر اصلی به تمام کاربران، کیف پول‌ها و تنظیمات ربات دسترسی کامل دارد.",
                    keyboards.inline(
                        [("👑 تأیید ارتقا به مدیر اصلی", f"user:confirmroot:{target.id}")],
                        [("❌ انصراف", f"user:view:{target.id}")],
                    ),
                )
                return
            elif action in {"role", "promote", "confirmroot"}:
                if action == "confirmroot" and target.is_root:
                    self._user_detail(call, db, target)
                    return
                if target.is_root:
                    raise ValueError("سطح مدیر اصلی از این بخش قابل تغییر نیست")
                if action == "promote" and target.permission == int(Permission.ADMIN):
                    self._user_detail(call, db, target)
                    return
                before = target.permission
                if target.balance < 0 and action in {"role", "confirmroot"}:
                    raise ValueError("ابتدا بدهی ادمین را تسویه کنید؛ تغییر سطح با موجودی منفی مجاز نیست")
                target.permission = (
                    int(Permission.ROOT) if action == "confirmroot" else int(Permission.USER)
                    if action == "role" and target.permission == int(Permission.ADMIN)
                    else int(Permission.ADMIN)
                )
                record_action(
                    db,
                    target.id,
                    "user.role_changed",
                    "🔑 تغییر سطح دسترسی",
                    actor_id=call.from_user.id,
                    details={"before": before, "after": target.permission},
                )
                if before == int(Permission.ADMIN) and target.permission != int(Permission.ADMIN):
                    self._save_setting(db, credit_key(target.id), 0)
            elif action == "rename":
                self.service.set_state(
                    db,
                    call.from_user.id,
                    "root_user_rename",
                    user_id=target.id,
                    chat_id=call.message.chat.id,
                )
                self._edit(call, "✏ نام جدید کاربر را بفرستید.", cancel)
                return
            elif action == "send":
                self._edit(
                    call,
                    "📨 روش ارسال پیام به کاربر را انتخاب کنید:",
                    keyboards.inline(
                        [("📋 پیام / رسانه بدون نام فرستنده", f"user:sendcopy:{target.id}")],
                        [("↪ فوروارد با نام فرستنده", f"user:sendforward:{target.id}")],
                        [("🔙 جزئیات کاربر", f"user:view:{target.id}")],
                    ),
                )
                return
            elif action in {"sendcopy", "sendforward"}:
                self.service.set_state(
                    db,
                    call.from_user.id,
                    "root_user_send",
                    user_id=target.id,
                    mode="forward" if action == "sendforward" else "copy",
                    chat_id=call.message.chat.id,
                )
                self._edit(
                    call,
                    "📨 پیام، تصویر، ویدیو، فایل، صدا یا پیام فورواردشده را همین‌جا بفرستید.\n"
                    "دکمه تماس مستقیم با پشتیبانی برای کاربر اضافه می‌شود.",
                    cancel,
                )
                return
            else:
                raise ValueError("عملیات معتبر نیست")
            self._user_detail(call, db, target)
        if notify and not self._safe_notify_user(*notify):
            self.bot.send_message(
                call.message.chat.id, "تغییر ذخیره شد، اما ارسال اعلان به کاربر ممکن نشد."
            )

    def _root_user_text(self, db, message, state):
        root = self.service.get_user(db, message.from_user.id)
        if not root or not root.is_root or root.status != UserStatus.ACTIVE:
            return
        target = db.scalar(
            select(User).where(User.id == int(state.data["user_id"])).with_for_update()
        )
        if not target:
            raise ValueError("کاربر پیدا نشد")
        value = message.text.strip()
        back = keyboards.inline([("🔙 جزئیات کاربر", f"user:view:{target.id}")])
        if state.state == "root_user_creditlimit":
            raw = value.replace(",", "").replace("٬", "").replace(" ", "").replace("−", "-")
            if target.permission != int(Permission.ADMIN):
                raise ValueError("حد منفی فقط برای ادمین‌ها مجاز است")
            if not re.fullmatch(r"-?\d+", raw) or not -10**12 <= int(raw) <= 0:
                self.bot.reply_to(message, "عددی بین منفی ۱٬۰۰۰٬۰۰۰٬۰۰۰٬۰۰۰ و صفر بفرستید.")
                return
            after = int(raw)
            if target.balance < after:
                self.bot.reply_to(message, "کف جدید از بدهی فعلی بالاتر است؛ ابتدا بدهی را تسویه کنید.")
                return
            nonce = secrets.token_hex(4)
            self.service.set_state(db, root.telegram_id, "root_user_creditlimit_confirm",
                user_id=target.id, chat_id=message.chat.id, before=wallet_floor(db, target), after=after, nonce=nonce)
            self.bot.reply_to(message, f"🏦 کف مجاز کیف پول به {toman(after)} تغییر کند؟\nموجودی تغییری نمی‌کند.",
                reply_markup=keyboards.inline([("✅ تأیید", f"user:creditlimitapply:{target.id}:{nonce}")],
                                             [("❌ انصراف", f"user:view:{target.id}")]))
            return
        if state.state == "root_user_wallet":
            raw = value.replace(",", "").replace("٬", "").replace(" ", "")
            if not re.fullmatch(r"=?[+-]?\d+", raw):
                self.bot.reply_to(message, "مبلغ معتبر به تومان بفرستید.")
                return
            after = int(raw[1:]) if raw.startswith("=") else target.balance + int(raw) if raw[:1] in {"+", "-"} else int(raw)
            if not wallet_floor(db, target) <= after <= 10**12:
                self.bot.reply_to(
                    message, f"موجودی نهایی باید بین {toman(wallet_floor(db, target))} و ۱٬۰۰۰٬۰۰۰٬۰۰۰٬۰۰۰ تومان باشد."
                )
                return
            self.service.set_state(
                db,
                root.telegram_id,
                "root_user_wallet_confirm",
                user_id=target.id,
                before=target.balance,
                after=after,
                request_id=secrets.token_hex(12),
                chat_id=message.chat.id,
            )
            self.bot.reply_to(
                message,
                f"💳 موجودی قبلی: {toman(target.balance)}\n"
                f"💰 موجودی جدید: {toman(after)}\nتغییر را تأیید می‌کنید؟",
                reply_markup=keyboards.inline(
                    [("✅ تأیید تغییر", f"user:walletapply:{target.id}")],
                    [("❌ انصراف", f"user:view:{target.id}")],
                ),
            )
            return
        if state.state == "root_user_deactivate":
            if target.is_root or not 3 <= len(value) <= 1000:
                self.bot.reply_to(
                    message, "دلیل باید بین ۳ تا ۱۰۰۰ نویسه باشد؛ مدیر اصلی قابل غیرفعال‌سازی نیست."
                )
                return
            target.status = UserStatus.BLOCKED
            self._save_setting(db, f"user_deactivation_reason:{target.id}", value)
            record_action(
                db,
                target.id,
                "user.deactivated",
                "🚫 غیرفعال‌سازی حساب",
                actor_id=root.telegram_id,
                details={"reason": value},
            )
            notification = "🚫 حساب شما توسط مدیر غیرفعال شد.\n\n📣 دلیل:\n" + html.escape(value)
        elif state.state == "root_user_rename":
            if not 2 <= len(value) <= 100:
                self.bot.reply_to(message, "نام باید بین ۲ تا ۱۰۰ نویسه باشد.")
                return
            before = target.name
            target.name = value
            record_action(
                db,
                target.id,
                "user.renamed",
                "✏ تغییر نام کاربر",
                actor_id=root.telegram_id,
                details={"before": before, "after": value},
            )
            notification = None
        else:
            return
        self.service.clear_state(db, root.telegram_id)
        # Notifications are sent only after the caller commits the mutation.
        return target.telegram_id, notification, back

    def _send_user_message(self, message):
        if not self._authorized(message.from_user.id, root=True):
            return
        with self.database.session() as db:
            state = self.service.get_state(db, message.from_user.id)
            if (
                not state
                or state.state != "root_user_send"
                or state.data["chat_id"] != message.chat.id
            ):
                return
            target = db.get(User, int(state.data["user_id"]))
            mode = state.data["mode"]
            if not target:
                return
            markup = self._support_markup()
            try:
                if mode == "copy":
                    sent = self.bot.copy_message(
                        target.telegram_id, message.chat.id, message.message_id, reply_markup=markup
                    )
                else:
                    sent = self.bot.forward_message(
                        target.telegram_id, message.chat.id, message.message_id
                    )
            except (ApiTelegramException, requests.RequestException) as exc:
                log.warning(
                    "root message delivery failed for bot user %s: %s",
                    target.id,
                    type(exc).__name__,
                )
                self.bot.reply_to(
                    message,
                    "ارسال انجام نشد. اگر پیام محافظت‌شده است روش کپی را امتحان کنید؛ یا کاربر ربات را بلاک کرده است.",
                )
                return
            if mode == "forward":
                support_delivered = True
                try:
                    self.bot.send_message(
                        target.telegram_id,
                        "💬 برای پاسخ با پشتیبانی تماس بگیرید.",
                        reply_markup=markup,
                        reply_to_message_id=sent.message_id,
                    )
                except (ApiTelegramException, requests.RequestException):
                    support_delivered = False
                    log.warning("forward delivered, but support button failed for %s", target.id)
            else:
                support_delivered = True
            record_action(
                db,
                target.id,
                "root.message_sent",
                "📨 پیام مدیر به کاربر",
                actor_id=message.from_user.id,
                source_key=f"root-message:{message.chat.id}:{message.message_id}",
                details={
                    "mode": mode,
                    "content_type": message.content_type,
                    "source_chat_id": message.chat.id,
                    "source_message_id": message.message_id,
                    "delivered_message_id": sent.message_id,
                    "support_button_delivered": support_delivered,
                    "text": (
                        getattr(message, "text", None) or getattr(message, "caption", None) or ""
                    )[:1000],
                },
            )
            self.service.clear_state(db, message.from_user.id)
        self.bot.reply_to(
            message,
            "✅ پیام به کاربر ارسال شد."
            + (" دکمه پشتیبانی ارسال نشد." if not support_delivered else ""),
            reply_markup=keyboards.inline([("🔙 جزئیات کاربر", f"user:view:{target.id}")]),
        )

    def _show_user_event(self, call, event_id):
        with self.database.session() as db:
            event = db.get(UserAction, event_id)
            if not event:
                raise ValueError("رویداد پیدا نشد")
            details = dict(event.details or {})
            labels = {
                "order_id": "سفارش",
                "receipt_id": "رسید",
                "amount": "مبلغ / تغییر موجودی",
                "before": "قبل",
                "after": "بعد",
                "reason": "دلیل",
                "decision": "نتیجه",
                "text": "متن",
                "callback": "دکمه انتخاب‌شده",
                "content_type": "نوع پیام",
                "v2_user_id": "اکانت V2",
                "mode": "روش ارسال",
                "historical": "بازسازی از سوابق مالی",
            }
            lines = [
                f"<b>{html.escape(event.title)}</b>",
                f"👤 کاربر #{event.user_id}",
                f"🕒 {event.created_at:%Y/%m/%d %H:%M} UTC",
                f"🆔 انجام‌دهنده: {event.actor_telegram_id or 'سیستم / سابقه قدیمی'}",
            ]
            lines.extend(
                f"{labels.get(k, k)}: {html.escape(str(v)[:400])}"
                for k, v in details.items()
                if not k.endswith("file_id")
            )
            rows = [
                [("🔙 تاریخچه", f"dl:events{event.user_id}:show")],
                [("👤 جزئیات کاربر", f"user:view:{event.user_id}")],
            ]
            markup = keyboards.inline(*rows)
            if event.action in {"v2.archived", "v2.restored"} and details.get("v2_user_id"):
                markup.row(types.InlineKeyboardButton("🗃 مشاهده سابقه بایگانی",
                    callback_data=f"archive:view:{int(details['v2_user_id'])}"))
            receipt_id = details.get("receipt_id")
            if receipt_id:
                receipt = db.get(PaymentReceipt, int(receipt_id))
                if receipt and receipt.user_id == event.user_id:
                    url = receipt_message_url(
                        receipt.review_chat_id,
                        receipt.review_message_id,
                        details.get("review_topic_id"),
                    )
                    if url:
                        markup.row(types.InlineKeyboardButton("🧾 رفتن به رسید در گروه", url=url))
                    markup.row(
                        types.InlineKeyboardButton(
                            "📎 نمایش نسخه رسید", callback_data=f"user:receipt:{receipt.id}"
                        )
                    )
            while sum(len(line) + 1 for line in lines) > 3900 and len(lines) > 4:
                lines.pop()
            self._edit(call, "\n".join(lines), markup)

    def _show_saved_receipt(self, call, receipt_id):
        with self.database.session() as db:
            receipt = db.get(PaymentReceipt, receipt_id)
            if not receipt:
                raise ValueError("رسید پیدا نشد")
            markup = keyboards.inline([("🔙 تاریخچه", f"dl:events{receipt.user_id}:show")])
            url = receipt_message_url(receipt.review_chat_id, receipt.review_message_id)
            if url:
                markup.row(types.InlineKeyboardButton("🧾 رسید در گروه", url=url))
            kwargs = {
                "caption": f"🧾 رسید #{receipt.id} | سفارش #{receipt.order_id}",
                "reply_markup": markup,
            }
            if receipt.media_type == "photo":
                self.bot.send_photo(call.message.chat.id, receipt.telegram_file_id, **kwargs)
            else:
                self.bot.send_document(call.message.chat.id, receipt.telegram_file_id, **kwargs)

    def _root_v2_markup(self, v2_id, telegram_id):
        with self.database.session() as db:
            if is_archived(db, v2_id):
                return keyboards.inline([("🗃 سابقه و بازیابی", f"archive:view:{v2_id}")],
                    [("🔗 QR و لینک", f"v2:qr:{v2_id}")], [("🔙 بایگانی‌ها", "dl:archives:show")])
            root = self.service.get_user(db, telegram_id)
            origin = db.get(BotSetting, f"driving_origin:{root.id}") if root else None
            back = (
                f"dl:{origin.value}:show"
                if origin and re.fullmatch(r"uv\d+", origin.value)
                else None
            )
            linked = db.scalar(select(V2AccountLink.id).where(V2AccountLink.v2_user_id == v2_id))
            monthly = monthly_info(db, v2_id)
            customer = customer_owner(db, v2_id)
        markup = keyboards.account_actions(v2_id, root=True, list_back=back)
        markup.row(types.InlineKeyboardButton("📜 سوابق پرداخت دستی", callback_data=f"dl:vpay{v2_id}:show"))
        if monthly:
            markup.row(types.InlineKeyboardButton("🆘 تمدید اضطراری یک‌روزه",
                                                  callback_data=f"v2:emergency:{v2_id}"))
            markup.row(types.InlineKeyboardButton(
                f"🗓 تمدید ماهانه ({max(0, 31 - monthly['used'])} روز)",
                callback_data=f"emergency:renew:{v2_id}:{monthly['generation']}"))
        if customer:
            markup.row(types.InlineKeyboardButton("🤖 مشاهده کاربر ربات", callback_data=f"v2:owner:{v2_id}"))
        if linked is None:
            markup.row(types.InlineKeyboardButton("👑 ثبت اکانت ساخته‌شده توسط من",
                                                  callback_data=f"expiry:claim:{v2_id}"))
        return markup
