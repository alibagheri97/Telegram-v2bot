"""Root V2 account cards and explicitly confirmed manual administration."""

from __future__ import annotations

import html
import secrets
import time
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import keyboards
from .audit import record_action
from .emergency import EmergencyService, monthly_info
from .models import (
    BotSetting,
    ConversationState,
    Product,
    ProductType,
    User,
    UserStatus,
    V2AccountLink,
    V2AccountOrigin,
    V2ManualPayment,
)
from .panel import GIB


def customer_owner(db, v2_id):
    link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
    origin = db.get(V2AccountOrigin, v2_id)
    if (
        not link
        or link.admin_created
        or (
            origin
            and (origin.kind in {"managed", "root_registered"} or origin.creator_permission != -1)
        )
    ):
        return None
    owner = db.get(User, link.user_id)
    if owner and not origin and owner.is_admin:
        return None
    return owner


class V2AdminMixin:
    def _v2_card(self, db, account):
        v2_id = int(account["id"])
        zone = ZoneInfo(getattr(self.settings, "timezone", "Asia/Tehran"))

        def date(value):
            if not value:
                return "—"
            try:
                return datetime.fromtimestamp(int(value), zone).strftime("%Y/%m/%d — %H:%M:%S")
            except (ValueError, TypeError, OverflowError, OSError):
                return "—"

        link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
        owner = db.get(User, link.user_id) if link else None
        origin = db.get(V2AccountOrigin, v2_id)
        monthly = monthly_info(db, v2_id)
        payments = (
            db.scalar(
                select(func.sum(V2ManualPayment.amount)).where(V2ManualPayment.v2_user_id == v2_id)
            )
            or 0
        )
        used = int(account.get("u") or 0) + int(account.get("d") or 0)
        total = int(account.get("transfer_enable") or 0)
        unlimited = not total or total >= 1_000_000 * GIB
        expiry = int(account.get("expired_at") or 0)
        if account.get("banned"):
            status = "🚫 مسدود"
        elif (expiry and expiry <= time.time()) or (not unlimited and used >= total):
            status = "🔴 پایان‌یافته"
        else:
            status = "✅ فعال"
        label = link.label if link else f"پلن پنل #{account.get('plan_id') or '—'}"
        if link and link.admin_created:
            label = (
                f"نامحدود {link.renewal_months or '—'} ماهه"
                if unlimited
                else f"حجمی {total / GIB:.1f} GB"
            )
        selected = db.get(BotSetting, f"v2_selected_product:{v2_id}")
        product = (
            db.get(Product, int(selected.value))
            if selected and selected.value.isdecimal()
            else None
        )
        if product:
            label = product.title
        role = (
            "👑 مدیر اصلی"
            if account.get("is_admin")
            else "🛡 مدیر پنل"
            if account.get("is_staff")
            else "👤 کاربر"
        )
        if link and link.admin_created:
            creator = (
                "👑 ساخته‌شده توسط مدیر اصلی"
                if origin and origin.creator_permission == 0
                else "🧑‍💼 ساخته‌شده توسط ادمین"
            )
        elif owner:
            creator = "🤖 حساب شخصی کاربر ربات"
        else:
            creator = "❔ سابقه سازنده ثبت نشده"
        seconds = expiry - int(time.time()) if expiry else None
        if seconds is None:
            remaining_time = "♾️ بدون انقضا"
        else:
            duration = abs(seconds)
            remaining_time = f"{duration // 86400} روز و {duration % 86400 // 3600} ساعت و {duration % 3600 // 60} دقیقه"
            remaining_time = (
                "⏳ باقی‌مانده: " if seconds > 0 else "🔴 گذشته از انقضا: "
            ) + remaining_time
        referrer = (
            self.panel.get_user(int(account["invite_user_id"]))
            if account.get("invite_user_id")
            else None
        )
        text = (
            f"<b>👤 اکانت #{v2_id}</b>\n📧 <code>{html.escape(str(account.get('email') or '—'))}</code>\n\n"
            "🕒 آخرین اتصال VPN: داده معتبر در پنل موجود نیست\n"
            f"🔑 آخرین ورود به پنل: {date(account.get('last_login_at'))}\n"
            f"💠 وضعیت اکانت: {status}\n📦 نوع بسته: {html.escape(label)}\n"
            f"👥 اتصال هم‌زمان: {account.get('device_limit') or '♾️ نامحدود'}\n"
            f"📈 حجم مصرفی: {used / GIB:.2f} GB\n"
            f"⬇️ دانلود: {int(account.get('d') or 0) / GIB:.2f} GB | ⬆️ آپلود: {int(account.get('u') or 0) / GIB:.2f} GB\n"
            f"📊 حجم باقی‌مانده: {'♾️ نامحدود' if unlimited else f'{max(0, total - used) / GIB:.2f} GB'}\n"
            f"📅 تاریخ انقضا: {date(expiry) if expiry else '♾️ بدون انقضا'}\n{remaining_time}\n\n"
            f"🔐 مجوز: {role}\n🏷️ مالکیت: {creator}\n"
            f"🧑‍🤝‍🧑 دعوت‌کننده: {html.escape(str(referrer.get('email'))) if referrer else '—'}\n"
            f"🛠 تاریخ ساخت: {date(account.get('created_at'))}\n"
            f"📝 یادداشت: {html.escape(str(account.get('remarks') or '—')[:700])}\n"
            f"💵 جمع پرداخت‌های دستی ثبت‌شده: {payments:,} تومان"
        )
        if monthly:
            text += f"\n🆘 روزهای اضطراری این دوره: {monthly['used']}\n🗓 مانده تمدید ماهانه: {max(0, 31 - monthly['used'])} روز"
            if monthly["pending"]:
                text += "\n⚠️ عملیات معلق در حال بازیابی است."
        text += f"\n\n🔗 کلید اتصال هوشمند:\n<code>{html.escape(self.panel.subscription_url(account))}</code>"
        return text

    def _v2_extra_action(self, call, action, v2_id, parts):
        if not self._authorized(call.from_user.id, root=True):
            raise ValueError("دسترسی مدیر اصلی لازم است")
        if action == "owner":
            with self.database.session() as db:
                owner = customer_owner(db, v2_id)
                if not owner:
                    raise ValueError("این اکانت، حساب شخصی کاربر ربات نیست")
                self._user_detail(call, db, owner)
            return
        if action == "emergency":
            result = EmergencyService(self.bot, self.database, self.panel).operate(
                v2_id, actor_id=call.from_user.id
            )
            if not result:
                raise ValueError("شارژ اضطراری فقط برای اکانت روتِ منقضی و زیر سقف ۳۱ روز مجاز است")
            self._v2_action(call, f"v2:view:{v2_id}")
            return
        if action in {"resetask", "renewask"}:
            operation = "reset" if action == "resetask" else "renew"
            text = (
                "مصرف این اکانت صفر شود؟"
                if operation == "reset"
                else "توکن و UUID عوض شوند؟ لینک قبلی از کار می‌افتد."
            )
            self._edit(
                call,
                f"⚠️ {text}",
                keyboards.inline(
                    [("✅ تأیید", f"v2:{operation}:{v2_id}")], [("❌ انصراف", f"v2:view:{v2_id}")]
                ),
            )
            return
        if action == "package":
            self._driving_render(call, f"vp{v2_id}")
            return
        if action == "paymentdetail":
            with self.database.session() as db:
                payment = db.get(V2ManualPayment, int(parts[3]))
                if not payment or payment.v2_user_id != v2_id:
                    raise ValueError("پرداخت پیدا نشد")
                self._edit(
                    call,
                    f"💵 پرداخت دستی #{payment.id}\n🆔 اکانت #{v2_id}\n"
                    f"💰 {payment.amount:,} تومان\n👤 ثبت‌کننده: {payment.actor_telegram_id}\n"
                    "این سابقه به‌تنهایی موجودی یا اشتراک را تغییر نمی‌دهد.",
                    keyboards.inline([("🔙 پرداخت‌ها", f"dl:vpay{v2_id}:show")]),
                )
            return
        with self.database.session() as db:
            if action in {"custom", "payment"}:
                self.service.set_state(
                    db,
                    call.from_user.id,
                    "root_v2_input",
                    action=action,
                    v2_id=v2_id,
                    chat_id=call.message.chat.id,
                    message_id=call.message.message_id,
                    nonce=secrets.token_hex(4),
                )
                prompt = (
                    "✏️ تعداد روزهای تمدید دلخواه را بفرستید (۱ تا ۳۶۵۰). این تمدید دستی است و شمارنده اضطراری را پاک نمی‌کند."
                    if action == "custom"
                    else "💵 مبلغ پرداخت دریافت‌شده را به تومان بفرستید. فقط ثبت حسابداری است؛ موجودی یا اعتبار تغییر نمی‌کند."
                )
                self._edit(call, prompt, keyboards.inline([("❌ انصراف", f"v2:view:{v2_id}")]))
                return
            if action == "pickplan":
                nonce = secrets.token_hex(4)
                product = db.get(Product, int(parts[3]))
                if not product:
                    raise ValueError("بسته پیدا نشد")
                self.service.set_state(
                    db,
                    call.from_user.id,
                    "root_v2_confirm",
                    action="package",
                    v2_id=v2_id,
                    product_id=product.id,
                    nonce=nonce,
                    chat_id=call.message.chat.id,
                )
                self._edit(
                    call,
                    f"📦 {html.escape(product.title)}\n"
                    f"📊 حجم: {'♾️ نامحدود' if product.product_type == ProductType.TIME else str(product.data_gb) + ' GB'}\n"
                    f"👥 اتصال: {product.connections}\n"
                    "فقط الگوی حجم و اتصال تغییر می‌کند. زمان انقضا، مصرف فعلی، شناسه پلن پنل و کیف پول دست‌نخورده می‌مانند.",
                    keyboards.inline(
                        [("✅ اعمال الگوی بسته", f"v2:applyextra:{v2_id}:{nonce}")],
                        [("❌ انصراف", f"v2:view:{v2_id}")],
                    ),
                )
                return
            state = db.scalar(
                select(ConversationState)
                .where(ConversationState.telegram_id == call.from_user.id)
                .with_for_update()
            )
            if (
                action != "applyextra"
                or not state
                or state.state != "root_v2_confirm"
                or state.data.get("v2_id") != v2_id
                or state.data.get("chat_id") != call.message.chat.id
                or len(parts) != 4
                or state.data.get("nonce") != parts[3]
            ):
                raise ValueError("این تأیید قدیمی یا نامعتبر است")
            payload = dict(state.data)
            link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
            subject = link.user_id if link else self.service.get_user(db, call.from_user.id).id
            if payload["action"] == "payment":
                db.add(
                    V2ManualPayment(
                        v2_user_id=v2_id,
                        amount=payload["value"],
                        actor_telegram_id=call.from_user.id,
                    )
                )
            elif payload["action"] == "custom":
                self.panel.apply_emergency_operation(
                    v2_id,
                    before=payload["before"],
                    after=payload["after"],
                    transfer_enable=payload["transfer_enable"],
                    monthly=False,
                )
            elif payload["action"] == "package":
                product = db.get(Product, payload["product_id"])
                if not product:
                    raise ValueError("بسته پیدا نشد")
                self.panel.set_package_limits(
                    v2_id,
                    transfer_gb=1_000_000
                    if product.product_type == ProductType.TIME
                    else int(product.data_gb or 0),
                    device_limit=product.connections
                    if product.product_type == ProductType.TIME
                    else None,
                )
                self._save_setting(db, f"v2_selected_product:{v2_id}", product.id)
                if link:
                    link.product_type = product.product_type
            else:
                raise ValueError("عملیات نامعتبر است")
            record_action(
                db,
                subject,
                f"v2.manual_{payload['action']}",
                "🛠 عملیات مدیریتی اکانت V2",
                actor_id=call.from_user.id,
                details=payload,
            )
            self.service.clear_state(db, call.from_user.id)
        self._v2_action(call, f"v2:view:{v2_id}")

    def _v2_input_message(self, db, message, payload):
        root = self.service.get_user(db, message.from_user.id)
        if (
            not root
            or not root.is_root
            or root.status != UserStatus.ACTIVE
            or message.chat.id != payload["chat_id"]
        ):
            return
        raw = message.text.strip().replace(",", "").replace("٬", "")
        ceiling = 3650 if payload["action"] == "custom" else 10**12
        if not raw.isdecimal() or not 1 <= int(raw) <= ceiling:
            self.bot.reply_to(message, "عدد معتبر در محدوده اعلام‌شده بفرستید.")
            return
        payload["value"] = int(raw)
        if payload["action"] == "custom":
            account = self.panel.get_user(int(payload["v2_id"]))
            if not account:
                raise ValueError("اکانت پیدا نشد")
            payload.update(
                before=int(account.get("expired_at") or 0),
                after=max(int(account.get("expired_at") or 0), int(time.time())) + int(raw) * 86400,
                transfer_enable=int(account.get("transfer_enable") or 0),
            )
        self.service.set_state(db, root.telegram_id, "root_v2_confirm", **payload)
        text = f"✅ {payload['value']:,} {'روز تمدید دستی' if payload['action'] == 'custom' else 'تومان پرداخت دریافتی'} برای اکانت #{payload['v2_id']} تأیید شود؟"
        self._edit(
            SimpleNamespace(
                message=SimpleNamespace(chat=message.chat, message_id=payload["message_id"])
            ),
            text,
            keyboards.inline(
                [("✅ تأیید", f"v2:applyextra:{payload['v2_id']}:{payload['nonce']}")],
                [("❌ انصراف", f"v2:view:{payload['v2_id']}")],
            ),
        )
