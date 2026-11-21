"""Managed expiration monitoring, creator provenance, and root service controls."""

from __future__ import annotations

import html
import logging
import threading
import time
from types import SimpleNamespace

from sqlalchemy import select
from telebot import types

from . import keyboards
from .archive import ArchiveService, is_archived
from .audit import record_action
from .models import (
    BotSetting,
    EmergencyAccount,
    ExpiryWatch,
    Permission,
    ProductType,
    User,
    UserStatus,
    V2AccountLink,
    V2AccountOrigin,
)
from .panel import GIB
from .service_ui import render_service_topic, render_services

log = logging.getLogger(__name__)
_run_lock = threading.Lock()


def account_signature(account) -> str:
    return f"{int(account.get('expired_at') or 0)}:{int(account.get('transfer_enable') or 0)}"


def account_expiration(account, now=None) -> str | None:
    now = int(time.time()) if now is None else now
    expiry = int(account.get("expired_at") or 0)
    limit = int(account.get("transfer_enable") or 0)
    used = int(account.get("u") or 0) + int(account.get("d") or 0)
    if expiry > 0 and expiry <= now:
        return "پایان اعتبار زمانی"
    if limit > 0 and used >= limit:
        return "اتمام حجم"
    return None


def setting(db, key, default=""):
    row = db.get(BotSetting, key)
    return row.value if row else default


def save_setting(db, key, value):
    row = db.get(BotSetting, key)
    if row:
        row.value = str(value)
    else:
        db.add(BotSetting(key=key, value=str(value)))


def root_managed(db, link, owner) -> bool:
    origin = db.get(V2AccountOrigin, link.v2_user_id)
    return bool(
        owner.is_root
        and link.admin_created
        and origin
        and origin.creator_user_id == owner.id
        and origin.creator_permission == int(Permission.ROOT)
        and origin.kind in {"managed", "root_registered"}
    )


class ExpiryService:
    def __init__(self, bot, database, panel):
        self.bot, self.database, self.panel = bot, database, panel

    def run(self, *, force=False):
        counts = {"checked": 0, "sent": 0, "errors": 0, "topic_pending": 0, "archived": 0}
        if not _run_lock.acquire(blocking=False):
            return {**counts, "busy": True}
        try:
            with self.database.session() as db:
                if not force and setting(db, "service:expire:enabled", "0") != "1":
                    return {**counts, "paused": True}
                ids = list(db.scalars(select(V2AccountLink.id).order_by(V2AccountLink.id)))
                archive_days = int(setting(db, "service:expire:archive_after_days", "3"))
            topic_sent = 0
            for link_id in ids:
                try:
                    if ArchiveService(self.database, self.panel).check(link_id, archive_days):
                        counts["archived"] += 1
                        continue
                    with self.database.session() as db:
                        link = db.get(V2AccountLink, link_id)
                        if not link:
                            continue
                        owner = db.get(User, link.user_id)
                        if not owner or owner.status != UserStatus.ACTIVE:
                            continue
                        account = self.panel.get_user(link.v2_user_id)
                        if not account:
                            continue
                        counts["checked"] += 1
                        watch = db.get(ExpiryWatch, link.v2_user_id)
                        if not watch:
                            watch = ExpiryWatch(
                                v2_user_id=link.v2_user_id,
                                signature="",
                                episode=0,
                                notified_episode=0,
                                was_expired=False,
                            )
                            db.add(watch)
                        reason = account_expiration(account)
                        signature = account_signature(account)
                        if not reason:
                            watch.was_expired, watch.signature = False, signature
                            watch.last_error = None
                            continue
                        if not watch.was_expired or watch.signature != signature:
                            watch.episode += 1
                        watch.was_expired, watch.signature = True, signature
                        is_root_account = root_managed(db, link, owner)
                        if is_root_account:
                            chat_id = int(setting(db, "expired_group_id", "0"))
                            topic_id = int(setting(db, "expired_topic_id", "0"))
                            if not chat_id or not topic_id:
                                counts["topic_pending"] += 1
                                continue
                        else:
                            chat_id, topic_id = owner.telegram_id, 0
                        destination = f"{chat_id}:{topic_id}"
                        if (
                            watch.notified_episode == watch.episode
                            and watch.destination == destination
                        ):
                            continue
                        # Respect Telegram's group flood limits; remaining notices retry next scan.
                        if counts["sent"] >= 20 or (is_root_account and topic_sent >= 2):
                            continue
                        used = (int(account.get("u") or 0) + int(account.get("d") or 0)) / GIB
                        text = (
                            f"🔴 <b>اشتراک پایان یافته</b>\n🆔 #{link.v2_user_id}\n"
                            f"👤 {html.escape(link.label)}\n"
                            f"📧 {html.escape(str(account.get('email') or '—'))}\n"
                            f"⏳ {reason}\n📊 مصرف: {used:.2f} GB"
                        )
                        if is_root_account:
                            username = self.bot.get_me().username
                            balance = db.get(EmergencyAccount, link.v2_user_id)
                            generation = balance.generation if balance else 0
                            markup = keyboards.inline(
                                [
                                    (
                                        "⚡ تمدید ۳۱ روزه و صفرکردن مصرف",
                                        f"expiry:renew:{link.v2_user_id}:{watch.episode}:{generation}",
                                    )
                                ]
                            )
                            markup.row(
                                types.InlineKeyboardButton(
                                    "👁 وضعیت و مدیریت اکانت",
                                    url=f"https://t.me/{username}?start=account_{link.v2_user_id}",
                                )
                            )
                        elif owner.is_root:
                            markup = keyboards.inline(
                                [("👁 وضعیت اکانت", f"v2:view:{link.v2_user_id}")]
                            )
                        elif owner.permission == int(Permission.ADMIN) and link.admin_created:
                            markup = keyboards.inline(
                                [
                                    (
                                        "💳 تمدید با قیمت اختصاصی",
                                        f"resell:recharge:{link.v2_user_id}",
                                    )
                                ],
                                [("👁 وضعیت و مدیریت", f"resell:manage:{link.v2_user_id}")],
                            )
                        else:
                            markup = keyboards.inline(
                                [("🛒 خرید اشتراک", "buy:start")],
                                [("👁 مشاهده اشتراک", f"sub:view:{link.v2_user_id}")],
                            )
                        kwargs = {"reply_markup": markup, "parse_mode": "HTML"}
                        if topic_id:
                            kwargs["message_thread_id"] = topic_id
                        sent = self.bot.send_message(chat_id, text, **kwargs)
                        watch.notified_episode, watch.destination = watch.episode, destination
                        watch.message_id, watch.last_error = sent.message_id, None
                        counts["sent"] += 1
                        topic_sent += int(is_root_account)
                except Exception:
                    counts["errors"] += 1
                    log.exception(
                        "expiry scan failed for link %s; continuing with other accounts", link_id
                    )
            with self.database.session() as db:
                save_setting(db, "service:expire:last_run", int(time.time()))
                save_setting(
                    db,
                    "service:expire:last_result",
                    f"بررسی: {counts['checked']} | ارسال: {counts['sent']} | بایگانی: {counts['archived']} | خطا: {counts['errors']} | منتظر تاپیک: {counts['topic_pending']}",
                )
            return counts
        finally:
            _run_lock.release()


class ExpiryMixin:
    def _expiry_callback(self, call, data):
        if not self._authorized(call.from_user.id, root=True):
            raise ValueError("دسترسی مدیر اصلی لازم است")
        parts = data.split(":")
        action = parts[1]
        if action in {"home", "service", "topic"}:
            with self.database.session() as db:
                state = self.service.get_state(db, call.from_user.id)
                if state and state.state in {"expiry_archive_days", "expiry_archive_confirm"}:
                    self.service.clear_state(db, call.from_user.id)
        if action in {"service", "topic"}:
            if len(parts) != 3:
                raise ValueError("سرویس معتبر نیست")
            if action == "service":
                render_services(self, call, parts[2])
            else:
                render_service_topic(self, call, parts[2])
            return
        if action == "run":
            result = ExpiryService(self.bot, self.database, self.panel).run(force=True)
            if result.get("busy"):
                self.bot.answer_callback_query(call.id, "بررسی قبلی هنوز در حال اجراست.")
        elif action in {"claim", "confirmclaim"}:
            v2_id = int(parts[2])
            account = self.panel.get_user(v2_id)
            if not account or account.get("is_admin"):
                raise ValueError("اکانت معتبر کاربر پنل پیدا نشد")
            if action == "claim":
                self._edit(
                    call,
                    f"👑 اکانت #{v2_id} را خودتان خارج از ربات ساخته‌اید؟\n"
                    "فقط اکانت واقعاً ساخته‌شده توسط خودتان را ثبت کنید.\n"
                    "اکانت‌های خریداران ربات و مشتریان سایر ادمین‌ها قابل ثبت نیستند.",
                    keyboards.inline(
                        [("✅ تأیید ثبت اکانت خودم", f"expiry:confirmclaim:{v2_id}")],
                        [("❌ انصراف", f"v2:view:{v2_id}")],
                    ),
                )
                return
            with self.database.session() as db:
                root = db.scalar(
                    select(User).where(User.telegram_id == call.from_user.id).with_for_update()
                )
                if not root or not root.is_root or root.status != UserStatus.ACTIVE:
                    raise ValueError("دسترسی مدیر اصلی لازم است")
                if db.get(V2AccountOrigin, v2_id) or db.scalar(
                    select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id)
                ):
                    raise ValueError("این اکانت از قبل ثبت شده یا متعلق به کاربر دیگری است")
                db.add(
                    V2AccountLink(
                        user_id=root.id,
                        v2_user_id=v2_id,
                        label=str(account.get("email") or f"Account {v2_id}")[:100],
                        product_type=ProductType.TIME
                        if account.get("expired_at")
                        else ProductType.DATA,
                        admin_created=True,
                    )
                )
                db.add(
                    V2AccountOrigin(
                        v2_user_id=v2_id,
                        creator_user_id=root.id,
                        creator_permission=int(Permission.ROOT),
                        kind="root_registered",
                    )
                )
                root.v2accounts = [*list(root.v2accounts or []), v2_id]
                record_action(
                    db,
                    root.id,
                    "v2.root_registered",
                    "👑 ثبت اکانت ساخته‌شده توسط مدیر اصلی",
                    actor_id=root.telegram_id,
                    details={"v2_user_id": v2_id},
                )
            self._v2_action(call, f"v2:view:{v2_id}")
            return
        elif action == "renew":
            from .emergency import EmergencyService

            v2_id, episode = int(parts[2]), int(parts[3])
            with self.database.session() as db:
                link = db.scalar(
                    select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id).with_for_update()
                )
                owner = db.get(User, link.user_id) if link else None
                watch = db.get(ExpiryWatch, v2_id)
                if is_archived(db, v2_id):
                    raise ValueError("اکانت بایگانی شده؛ ابتدا از بایگانی بازیابی کنید")
                balance = db.get(EmergencyAccount, v2_id)
                used = balance.days_used if balance else 0
                generation = balance.generation if balance else 0
                expected_generation = int(parts[4]) if len(parts) > 4 else 0
                if generation != expected_generation:
                    raise ValueError("این اعلان قدیمی و مربوط به دوره قبلی است")
                if not link or not owner or not root_managed(db, link, owner):
                    raise ValueError("تمدید سریع فقط برای اکانت ساخته‌شده توسط مدیر اصلی مجاز است")
                if not watch or watch.episode != episode or (not watch.was_expired and not used):
                    raise ValueError("این اعلان قدیمی است؛ وضعیت اکانت را بررسی کنید")
                signature = watch.signature
            result = EmergencyService(self.bot, self.database, self.panel).operate(
                v2_id,
                monthly=True,
                generation=generation,
                actor_id=call.from_user.id,
                expected_signature=signature,
                require_expired=True,
            )
            with self.database.session() as db:
                watch = db.get(ExpiryWatch, v2_id)
                watch.was_expired = False
                watch.signature = account_signature(self.panel.get_user(v2_id))
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "👁 وضعیت و مدیریت در ربات",
                    url=f"https://t.me/{self.bot.get_me().username}?start=account_{v2_id}",
                )
            )
            self._edit(
                call,
                f"✅ اکانت #{v2_id} برای {result['days']} روز تمدید، مصرف صفر و مسدودی رفع شد.\n"
                f"🆘 {result['used']} روز اضطراری کسر و شمارنده صفر شد.",
                markup,
            )
            return
        elif action in {"toggle", "auto"}:
            key = "service:expire:enabled" if action == "toggle" else "service:auto_renew:enabled"
            with self.database.session() as db:
                save_setting(
                    db,
                    key,
                    "0" if setting(db, key, "1" if action == "auto" else "0") == "1" else "1",
                )
        elif action != "home":
            raise ValueError("عملیات سرویس معتبر نیست")
        render_services(
            self,
            call,
            "auto" if action == "auto" else "expire" if action in {"toggle", "run"} else None,
        )

    def _emergency_callback(self, call, data):
        from .emergency import EmergencyService

        if not self._authorized(call.from_user.id, root=True):
            raise ValueError("دسترسی مدیر اصلی لازم است")
        parts = data.split(":")
        action = parts[1]
        if action == "renew":
            v2_id, generation = int(parts[2]), int(parts[3])
            result = EmergencyService(self.bot, self.database, self.panel).operate(
                v2_id, monthly=True, generation=generation, actor_id=call.from_user.id
            )
            with self.database.session() as db:
                watch = db.get(ExpiryWatch, v2_id)
                if watch:
                    watch.was_expired = False
                    watch.signature = account_signature(self.panel.get_user(v2_id))
            markup = types.InlineKeyboardMarkup()
            markup.row(
                types.InlineKeyboardButton(
                    "👁 وضعیت و مدیریت در ربات",
                    url=f"https://t.me/{self.bot.get_me().username}?start=account_{v2_id}",
                )
            )
            self._edit(
                call,
                f"✅ تمدید ماهانه انجام شد: {result['days']} روز اضافه شد.\n"
                f"🆘 {result['used']} روز اضطراری از ۳۱ روز کسر و شمارنده صفر شد.",
                markup,
            )
            return
        if action == "toggle":
            with self.database.session() as db:
                enabled = setting(db, "service:emergency:enabled", "0") == "1"
                if not enabled and (
                    not int(setting(db, "emergency_group_id", "0"))
                    or not int(setting(db, "emergency_topic_id", "0"))
                ):
                    raise ValueError(
                        "ابتدا در تاپیک موردنظر /set-topic emergency-trigger را بفرستید"
                    )
                save_setting(db, "service:emergency:enabled", "0" if enabled else "1")
        elif action == "run":
            result = EmergencyService(self.bot, self.database, self.panel).run()
            if result.get("busy"):
                self.bot.answer_callback_query(call.id, "بررسی قبلی هنوز تمام نشده است.")
        else:
            raise ValueError("عملیات سرویس معتبر نیست")
        render_services(self, call, "emergency")

    def _expiry_deeplink(self, message, user, payload):
        if not payload.startswith("account_"):
            return False
        if not user.is_root or not payload[8:].isdigit() or message.chat.type != "private":
            self.bot.send_message(message.chat.id, "دسترسی خصوصی مدیر اصلی لازم است.")
            return True
        sent = self.bot.send_message(message.chat.id, "🔎 در حال دریافت وضعیت اکانت…")
        call = SimpleNamespace(message=sent, from_user=message.from_user)
        self._v2_action(call, f"v2:view:{int(payload[8:])}")
        return True
