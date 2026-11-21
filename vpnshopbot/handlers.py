from __future__ import annotations

import html
import logging
import math
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import requests
from sqlalchemy import func, select
from telebot import TeleBot, types
from telebot.apihelper import ApiTelegramException

from . import keyboards
from .archive import ArchiveMixin, is_archived
from .audit import record_action
from .catalog import CatalogMixin
from .config import Settings
from .db import Database
from .driving import DrivingListMixin
from .emergency import monthly_info
from .expiry import ExpiryMixin
from .models import (
    AdminPrice,
    BotSetting,
    Discount,
    Order,
    OrderStatus,
    PaymentReceipt,
    Permission,
    Product,
    ProductType,
    User,
    UserStatus,
    V2AccountLink,
)
from .panel import GIB, PanelRepository
from .services import ShopService, generate_qr, toman
from .topics import configure_topic, topic_help
from .user_admin import UserAdminMixin
from .v2_admin import V2AdminMixin
from .wallet import wallet_floor

log = logging.getLogger(__name__)


def _h(value: Any) -> str:
    return html.escape(str(value or "—"))


def _status_icon(account: dict[str, Any]) -> str:
    if account.get("banned"):
        return "🔴"
    expired = int(account.get("expired_at") or 0)
    remaining = int(account.get("transfer_enable") or 0) - int(account.get("u") or 0) - int(
        account.get("d") or 0
    )
    if (expired and expired <= time.time()) or remaining <= 0:
        return "🔴"
    if (expired and expired - time.time() < 3 * 86400) or remaining < 2 * GIB:
        return "🟡"
    return "🟢"


def _remaining(account: dict[str, Any]) -> str:
    expired = int(account.get("expired_at") or 0)
    if not expired:
        return "بدون انقضا"
    seconds = expired - int(time.time())
    if seconds <= 0:
        return f"منقضی ({abs(seconds) // 86400} روز قبل)"
    if seconds < 86400:
        return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}"
    return f"{seconds // 86400} روز"


def _account_caption(
    account: dict[str, Any],
    *,
    label: str,
    display_name: str,
    auto_renewal: bool,
    unlimited: bool,
    url: str,
) -> str:
    used = int(account.get("u") or 0) + int(account.get("d") or 0)
    total = int(account.get("transfer_enable") or 0)
    if unlimited:
        traffic_details = (
            f"📈 مصرف‌شده: {used / GIB:.2f} GB\n"
            "📊 باقی‌مانده: ♾️ نامحدود"
        )
        subscription_type = "♾️ نامحدود"
    else:
        remaining = max(total - used, 0)
        traffic_details = (
            f"📦 حجم کل: {total / GIB:.2f} GB\n"
            f"📈 مصرف‌شده: {used / GIB:.2f} GB\n"
            f"📊 باقی‌مانده: {remaining / GIB:.2f} GB"
        )
        subscription_type = "📦 حجمی"
    device_limit = account.get("device_limit")
    account_type = {
        1: "👤 یک‌کاربره",
        2: "👥 دوکاربره",
    }.get(device_limit, "♾️ بدون محدودیت کاربر")
    status_icon = _status_icon(account)
    status_text = {"🟢": "فعال", "🟡": "نزدیک پایان", "🔴": "پایان‌یافته یا مسدود"}[status_icon]
    renewal = "🟢 روشن" if auto_renewal else "🔴 خاموش"
    return (
        f"🏷️ <b>{_h(label)}</b>\n\n"
        f"👤 نام: <b>{_h(display_name)}</b>\n"
        f"🧩 نوع اشتراک: {subscription_type}\n"
        f"{traffic_details}\n"
        f"⏳ انقضا: {_remaining(account)}\n"
        f"🔌 نوع اکانت: {account_type}\n"
        f"🚦 وضعیت: {status_icon} {status_text}\n"
        f"🔁 تمدید خودکار: {renewal}\n\n"
        f"🔗 لینک اشتراک:\n<code>{_h(url)}</code>"
    )


def _v2_usage_metrics(account: dict[str, Any]) -> tuple[float, str]:
    download = int(account.get("d") or 0)
    return download / GIB, _status_icon(account)


def _v2_page_targets(page: int, pages: int) -> tuple[int, int, int, int]:
    return (
        max(1, page - 3),
        max(1, page - 1),
        min(pages, page + 1),
        min(pages, page + 3),
    )


class BotController(UserAdminMixin, DrivingListMixin, CatalogMixin, ExpiryMixin, V2AdminMixin, ArchiveMixin):
    def __init__(
        self,
        bot: TeleBot,
        database: Database,
        panel: PanelRepository,
        service: ShopService,
        settings: Settings,
    ):
        self.bot = bot
        self.database = database
        self.panel = panel
        self.service = service
        self.settings = settings
        self.register_handlers()

    def register_handlers(self) -> None:
        self._register_user_guards()
        self.bot.register_message_handler(self.start, commands=["start"])
        self.bot.register_message_handler(self.auth_command, commands=["auth"])
        self.bot.register_message_handler(self.set_topic_command, commands=["set_topic"])
        self.bot.register_message_handler(self.unset_topic_command, commands=["unset_topic"])
        self.bot.register_message_handler(self.topics_command, commands=["topics", "list_topics"])
        self.bot.register_message_handler(self.help_command, commands=["help"])
        self.bot.register_message_handler(self.unset_topic_command, content_types=["text"],
            func=lambda message: bool(message.text) and message.text.split()[0].split("@")[0].lower() == "/unset-topic")
        self.bot.register_message_handler(self.topics_command, content_types=["text"],
            func=lambda message: bool(message.text) and message.text.split()[0].split("@")[0].lower() == "/list-topics")
        self.bot.register_message_handler(
            self.set_topic_command,
            content_types=["text"],
            func=lambda message: bool(message.text)
            and message.text.lower().startswith("/set-topic"),
        )
        self.bot.register_message_handler(self.root_command, commands=["root"])
        self.bot.register_message_handler(self.admin_command, commands=["admin"])
        self.bot.register_message_handler(self.set_command, commands=["set"])
        self.bot.register_message_handler(self.price_command, commands=["price"])
        self.bot.register_message_handler(self.admin_price_command, commands=["adminprice"])
        self.bot.register_message_handler(self.discount_command, commands=["discount"])
        self.bot.register_message_handler(self.discount_off_command, commands=["discountoff"])
        self.bot.register_message_handler(
            self.receive_receipt, content_types=["photo", "document"], func=lambda _: True
        )
        self.bot.register_callback_query_handler(self.callback, func=lambda _: True)
        self.bot.register_message_handler(self.text_message, content_types=["text"], func=lambda _: True)

    def _edit(self, call: types.CallbackQuery, text: str, markup=None) -> None:
        try:
            self.bot.edit_message_text(
                text,
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=markup,
                disable_web_page_preview=True,
            )
        except (ApiTelegramException, requests.RequestException) as exc:
            if "message is not modified" not in str(exc):
                self.bot.send_message(
                    call.message.chat.id,
                    text,
                    parse_mode="HTML",
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )

    def _home(self, chat_id: int, user: User, *, edit_call=None) -> None:
        text = (
            f"سلام <b>{_h(user.name)}</b> عزیز 👋\n"
            f"به فروشگاه امن <b>{_h(self.settings.brand_name)}</b> خوش آمدی."
        )
        markup = keyboards.user_home()
        if edit_call:
            self._edit(edit_call, text, markup)
        else:
            self.bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)

    def _authorized(self, telegram_id: int, *, root: bool = False) -> User | None:
        with self.database.session() as db:
            user = self.service.get_user(db, telegram_id)
            if not user or user.status == UserStatus.BLOCKED:
                return None
            if root and not user.is_root:
                return None
            if not root and not user.is_admin:
                return None
            db.expunge(user)
            return user

    def _show_unresolved_order(self, call: types.CallbackQuery, order: Order) -> None:
        summary = self.service.order_summary(order)
        if order.status == OrderStatus.PENDING_REVIEW:
            status_text = "رسید این سفارش در حال بررسی مدیر است."
            markup = keyboards.inline([("🏠 بازگشت به خانه", "home:user")])
        elif order.status == OrderStatus.REJECTED:
            status_text = "رسید این سفارش رد شده است؛ رسید صحیح را دوباره بفرستید یا سفارش را لغو کنید."
            markup = keyboards.inline(
                [("📎 ارسال مجدد رسید", f"pay:receipt:{order.id}")],
                [("❌ لغو سفارش", f"order:cancel:{order.id}"), ("🏠 خانه", "home:user")],
            )
        else:
            status_text = "این سفارش هنوز پرداخت یا لغو نشده است."
            markup = keyboards.inline(
                [("💳 ادامه پرداخت", f"order:confirm:{order.id}")],
                [("❌ لغو سفارش", f"order:cancel:{order.id}"), ("🏠 خانه", "home:user")],
            )
        self._edit(
            call,
            f"<b>سفارش مالی باز #{order.id}</b>\n\n{status_text}\n\n{summary}\n\n"
            "تا تعیین تکلیف این سفارش، امکان خرید یا پرداخت جدید وجود ندارد.",
            markup,
        )

    def _render_order_history(
        self, call: types.CallbackQuery, db, user: User, page: int = 1
    ) -> None:
        page_size = 8
        total = db.scalar(
            select(func.count()).select_from(Order).where(Order.user_id == user.id)
        ) or 0
        pages = max(1, math.ceil(total / page_size))
        page = min(max(1, page), pages)
        orders = list(
            db.scalars(
                select(Order)
                .where(Order.user_id == user.id)
                .order_by(Order.id.desc())
                .limit(page_size)
                .offset((page - 1) * page_size)
            )
        )
        if not orders:
            self._edit(
                call,
                "هنوز سفارشی ثبت نکرده‌اید.",
                keyboards.inline(
                    [("🛒 خرید اشتراک", "buy:start")], [("🏠 بازگشت به خانه", "home:user")]
                ),
            )
            return
        status_labels = {
            OrderStatus.DRAFT: ("⚪️", "پیش‌نویس"),
            OrderStatus.AWAITING_PAYMENT: ("🟠", "منتظر پرداخت"),
            OrderStatus.PENDING_REVIEW: ("🟡", "در حال بررسی رسید"),
            OrderStatus.PAID: ("🟢", "تأیید و تحویل‌شده"),
            OrderStatus.REJECTED: ("🔴", "رسید ردشده"),
            OrderStatus.CANCELLED: ("⚫️", "لغوشده"),
        }
        type_labels = {
            ProductType.TIME: "نامحدود",
            ProductType.DATA: "حجمی",
            ProductType.WALLET: "کیف پول",
        }
        rows = []
        for order in orders:
            icon, status = status_labels[order.status]
            rows.append(
                [
                    (
                        f"{icon} #{order.id} | {type_labels[order.product_type]} | {status}",
                        f"orders:view:{order.id}",
                    )
                ]
            )
        if pages > 1:
            rows.append(
                [
                    ("◀", f"orders:list:{max(1, page - 1)}"),
                    (f"{page}/{pages}", "noop"),
                    ("▶", f"orders:list:{min(pages, page + 1)}"),
                ]
            )
        rows.append([("🏠 بازگشت به خانه", "home:user")])
        self._edit(
            call,
            "<b>سفارش‌های قبلی من</b>\n\n"
            "برای مشاهده جزئیات و عملیات ممکن، یک سفارش را انتخاب کنید.",
            keyboards.inline(*rows),
        )

    def _show_order_detail(self, call: types.CallbackQuery, order: Order) -> None:
        if order.status in {
            OrderStatus.AWAITING_PAYMENT,
            OrderStatus.PENDING_REVIEW,
            OrderStatus.REJECTED,
        }:
            self._show_unresolved_order(call, order)
            return
        summary = self.service.order_summary(order)
        if order.status == OrderStatus.PAID:
            rows = []
            if order.v2_user_id:
                rows.append([("🔗 مشاهده اشتراک و QR", f"sub:view:{order.v2_user_id}")])
            elif order.product_type == ProductType.WALLET:
                rows.append([("💳 مشاهده کیف پول", "wallet:show")])
            rows.extend(
                [
                    [("💬 درخواست لغو / بازپرداخت", "support:show")],
                    [("🔙 بازگشت به سفارش‌ها", "orders:list:1"), ("🏠 خانه", "home:user")],
                ]
            )
            self._edit(
                call,
                f"<b>سفارش #{order.id}</b>\n\n"
                "وضعیت: 🟢 تأیید و تحویل‌شده\n\n"
                f"{summary}\n\n"
                "سفارش تحویل‌شده خودکار لغو نمی‌شود؛ برای بررسی بازپرداخت با پشتیبانی تماس بگیرید.",
                keyboards.inline(*rows),
            )
            return
        self._edit(
            call,
            f"<b>سفارش #{order.id}</b>\n\nوضعیت: ⚫️ لغوشده\n\n{summary}",
            keyboards.inline(
                [("🛒 خرید مجدد", "buy:start")],
                [("🔙 بازگشت به سفارش‌ها", "orders:list:1"), ("🏠 خانه", "home:user")],
            ),
        )

    def start(self, message: types.Message) -> None:
        payload = message.text.split(maxsplit=1)[1].strip() if " " in message.text else ""
        with self.database.session() as db:
            user = self.service.ensure_root(
                db, message.from_user.id, message.from_user.first_name or "مدیر اصلی"
            )
            user = user or self.service.get_user(db, message.from_user.id)
            if user:
                if user.status == UserStatus.BLOCKED:
                    self.bot.send_message(message.chat.id, "حساب شما مسدود است. با پشتیبانی تماس بگیرید.")
                    return
                if self._expiry_deeplink(message, user, payload):
                    return
                if user.is_root:
                    self.bot.send_message(
                        message.chat.id,
                        "<b>داشبورد مدیر اصلی</b>",
                        parse_mode="HTML",
                        reply_markup=keyboards.root_home(),
                    )
                elif user.permission == int(Permission.ADMIN):
                    self.bot.send_message(
                        message.chat.id,
                        "<b>داشبورد نمایندگی</b>",
                        parse_mode="HTML",
                        reply_markup=keyboards.admin_home(),
                    )
                else:
                    self._home(message.chat.id, user)
                return
            self.service.set_state(db, message.from_user.id, "new", referral=payload)
        self.bot.send_message(
            message.chat.id,
            f"سلام دوست من، به {_h(self.settings.brand_name)} خوش آمدی 🌱\n"
            "هنوز حسابی نداری؛ با دکمه زیر حساب و اشتراک آزمایشی یک‌روزه‌ات را بساز.",
            parse_mode="HTML",
            reply_markup=keyboards.create_account(),
        )

    def auth_command(self, message: types.Message) -> None:
        parts = message.text.split()
        if len(parts) != 2 or len(parts[1]) != 6 or not parts[1].isdigit():
            self.bot.reply_to(message, "فرمت صحیح: <code>/auth 123456</code>", parse_mode="HTML")
            return
        with self.database.session() as db:
            root_exists = db.scalar(
                select(func.count()).select_from(User).where(User.permission == int(Permission.ROOT))
            )
            bootstrap_claimed = db.get(BotSetting, "bootstrap_claimed")
            if root_exists or bootstrap_claimed:
                self.bot.reply_to(message, "مدیر اصلی قبلاً ثبت شده است.")
                return
            auth_hash = self.settings.bootstrap_auth_hash
            if not auth_hash or self.settings.bootstrap_auth_expires_at < int(time.time()):
                self.bot.reply_to(message, "کد راه‌اندازی وجود ندارد یا منقضی شده است؛ نصب‌کننده را دوباره اجرا کنید.")
                return
            state = self.service.get_state(db, message.from_user.id)
            attempts = int((state.data or {}).get("attempts", 0)) if state and state.state == "auth_failed" else 0
            global_attempts_setting = db.get(BotSetting, "bootstrap_failed_attempts")
            global_attempts = int(global_attempts_setting.value) if global_attempts_setting else 0
            if attempts >= 5 or global_attempts >= 30:
                self.bot.reply_to(message, "تعداد تلاش مجاز تمام شده است؛ نصب‌کننده را دوباره اجرا کنید.")
                return
            valid = bcrypt.checkpw(
                parts[1].encode(), auth_hash.get_secret_value().encode()
            )
            if not valid:
                self.service.set_state(
                    db, message.from_user.id, "auth_failed", attempts=attempts + 1
                )
                if global_attempts_setting:
                    global_attempts_setting.value = str(global_attempts + 1)
                else:
                    db.add(BotSetting(key="bootstrap_failed_attempts", value="1"))
                self.bot.reply_to(message, "رمز راه‌اندازی نادرست است.")
                return
            user = self.service.get_user(db, message.from_user.id)
            if user:
                user.permission = int(Permission.ROOT)
                user.status = UserStatus.ACTIVE
                if message.from_user.first_name:
                    user.name = message.from_user.first_name[:100]
            else:
                user = User(
                    telegram_id=message.from_user.id,
                    name=(message.from_user.first_name or "مدیر اصلی")[:100],
                    permission=int(Permission.ROOT),
                    referral_code=User.new_referral_code(),
                    status=UserStatus.ACTIVE,
                    v2accounts=[],
                )
                db.add(user)
                db.flush()
            self.service.clear_state(db, message.from_user.id)
            db.add(
                BotSetting(
                    key="bootstrap_claimed",
                    value=f"{message.from_user.id}:{int(time.time())}",
                )
            )
        self.bot.reply_to(
            message,
            "احراز هویت انجام شد؛ شما مدیر اصلی هستید ✅",
            reply_markup=keyboards.root_home(),
        )

    @staticmethod
    def _save_setting(db, key: str, value: object) -> None:
        setting = db.get(BotSetting, key)
        if setting:
            setting.value = str(value)
        else:
            db.add(BotSetting(key=key, value=str(value)))

    def set_topic_command(self, message: types.Message) -> None:
        configure_topic(self, message, "set")

    def unset_topic_command(self, message):
        configure_topic(self, message, "unset")

    def topics_command(self, message):
        configure_topic(self, message, "list")

    def help_command(self, message):
        topic_help(self, message)

    def root_command(self, message: types.Message) -> None:
        user = self._authorized(message.from_user.id, root=True)
        if not user:
            self.bot.reply_to(message, "دسترسی مدیر اصلی لازم است.")
            return
        self.bot.send_message(
            message.chat.id, "<b>داشبورد مدیر اصلی</b>", parse_mode="HTML", reply_markup=keyboards.root_home()
        )

    def admin_command(self, message: types.Message) -> None:
        user = self._authorized(message.from_user.id)
        if not user:
            self.bot.reply_to(message, "دسترسی نمایندگی لازم است.")
            return
        self.bot.send_message(
            message.chat.id, "<b>داشبورد نمایندگی</b>", parse_mode="HTML", reply_markup=keyboards.admin_home()
        )

    def set_command(self, message: types.Message) -> None:
        if not self._authorized(message.from_user.id, root=True):
            return
        parts = message.text.split(maxsplit=2)
        if len(parts) != 3:
            self.bot.reply_to(message, "نمونه: <code>/set referral_reward 50000</code>", parse_mode="HTML")
            return
        key, value = parts[1], parts[2]
        allowed = {
            "referral_reward",
            "data_price_per_gb",
            "support_username",
            "payment_group_id",
            "payment_topic_id",
            "expired_group_id",
            "expired_topic_id",
            "emergency_group_id",
            "emergency_topic_id",
            "card_number",
            "card_holder",
            "guide_android_file_id",
            "guide_iphone_file_id",
            "guide_windows_file_id",
            "guide_mac_file_id",
            "guide_linux_file_id",
        }
        if key not in allowed:
            self.bot.reply_to(message, "کلید تنظیمات مجاز نیست.")
            return
        with self.database.session() as db:
            setting = db.get(BotSetting, key)
            if setting:
                setting.value = value
            else:
                db.add(BotSetting(key=key, value=value))
        self.bot.reply_to(message, f"تنظیم <code>{_h(key)}</code> ذخیره شد.", parse_mode="HTML")

    def price_command(self, message: types.Message) -> None:
        if not self._authorized(message.from_user.id, root=True):
            return
        parts = message.text.split()
        if len(parts) != 3 or not parts[2].isdigit():
            self.bot.reply_to(message, "نمونه: <code>/price time-1c-1m 450000</code>", parse_mode="HTML")
            return
        with self.database.session() as db:
            product = db.scalar(select(Product).where(Product.code == parts[1]))
            if not product:
                self.bot.reply_to(message, "کد اشتراک پیدا نشد.")
                return
            product.price = int(parts[2])
        self.bot.reply_to(message, "قیمت ذخیره شد.")

    def admin_price_command(self, message: types.Message) -> None:
        if not self._authorized(message.from_user.id, root=True):
            return
        parts = message.text.split()
        if len(parts) != 4 or not parts[1].lstrip("-").isdigit() or not parts[3].isdigit():
            self.bot.reply_to(
                message,
                "نمونه: <code>/adminprice 123456789 time-1c-1m 350000</code>",
                parse_mode="HTML",
            )
            return
        with self.database.session() as db:
            admin = self.service.get_user(db, int(parts[1]))
            product = db.scalar(select(Product).where(Product.code == parts[2]))
            if not admin or admin.permission != int(Permission.ADMIN) or not product:
                self.bot.reply_to(message, "نماینده یا کد محصول پیدا نشد.")
                return
            price = db.scalar(
                select(AdminPrice).where(
                    AdminPrice.admin_id == admin.id, AdminPrice.product_id == product.id
                )
            )
            if price:
                price.price = int(parts[3])
            else:
                db.add(
                    AdminPrice(
                        admin_id=admin.id, product_id=product.id, price=int(parts[3])
                    )
                )
        self.bot.reply_to(message, "قیمت اختصاصی نماینده ذخیره شد.")

    def discount_command(self, message: types.Message) -> None:
        if not self._authorized(message.from_user.id, root=True):
            return
        parts = message.text.split()
        if (
            len(parts) != 4
            or not parts[2].isdigit()
            or not parts[3].isdigit()
            or not 1 <= int(parts[2]) <= 90
            or not 1 <= int(parts[3]) <= 8760
        ):
            self.bot.reply_to(
                message,
                "نمونه: <code>/discount time-1c-1m 20 48</code>\n"
                "یعنی ۲۰٪ تخفیف به مدت ۴۸ ساعت. به‌جای کد محصول می‌توان <code>all</code> نوشت.",
                parse_mode="HTML",
            )
            return
        with self.database.session() as db:
            product = None
            if parts[1] != "all":
                product = db.scalar(select(Product).where(Product.code == parts[1]))
                if not product:
                    self.bot.reply_to(message, "کد محصول پیدا نشد.")
                    return
            now = datetime.now(UTC)
            discount = Discount(
                title=f"تخفیف {parts[2]} درصدی",
                product_id=product.id if product else None,
                percent=int(parts[2]),
                starts_at=now,
                ends_at=now + timedelta(hours=int(parts[3])),
                enabled=True,
            )
            db.add(discount)
            db.flush()
            discount_id = discount.id
        self.bot.reply_to(message, f"رویداد تخفیف #{discount_id} فعال شد.")

    def discount_off_command(self, message: types.Message) -> None:
        if not self._authorized(message.from_user.id, root=True):
            return
        parts = message.text.split()
        if len(parts) != 2 or not parts[1].isdigit():
            self.bot.reply_to(message, "نمونه: <code>/discountoff 3</code>", parse_mode="HTML")
            return
        with self.database.session() as db:
            discount = db.get(Discount, int(parts[1]))
            if not discount:
                self.bot.reply_to(message, "رویداد پیدا نشد.")
                return
            discount.enabled = False
        self.bot.reply_to(message, "رویداد تخفیف غیرفعال شد.")

    def callback(self, call: types.CallbackQuery) -> None:
        try:
            self.bot.answer_callback_query(call.id)
            self._dispatch_callback(call)
        except ValueError as exc:
            self._edit(call, _h(exc), keyboards.inline([("🏠 خانه", "home:user")]))
        except Exception:
            log.exception("callback failed: %s", call.data)
            self.bot.answer_callback_query(call.id, "خطایی رخ داد؛ دوباره تلاش کنید.", show_alert=True)

    def _dispatch_callback(self, call: types.CallbackQuery) -> None:
        data = call.data
        if data.startswith("archive:"):
            self._archive_callback(call, data)
            return
        if data.startswith("emergency:"):
            self._emergency_callback(call, data)
            return
        if data.startswith("expiry:"):
            self._expiry_callback(call, data)
            return
        if data.startswith("dl:"):
            self._driving_callback(call)
            return
        if data.startswith("catalog:"):
            self._catalog_callback(call, data)
            return
        if data.startswith("resell:"):
            self._resell_callback(call, data)
            return
        if data == "noop":
            return
        if data == "register:start":
            with self.database.session() as db:
                state = self.service.get_state(db, call.from_user.id)
                referral = (state.data or {}).get("referral", "") if state else ""
                self.service.set_state(db, call.from_user.id, "await_name", referral=referral)
            self._edit(call, "دوست خوبم، چه صدایت کنیم؟\nنامت را در یک پیام بفرست.")
            return
        if data in {
            "home:user",
            "buy:start",
            "guide:start",
            "wallet:show",
            "referral:show",
            "referral:share",
            "support:show",
            "orders:open",
        }:
            self._basic_callback(call, data)
            return
        if data == "wallet:add":
            with self.database.session() as db:
                user = self.service.get_user(db, call.from_user.id)
                unresolved = self.service.get_unresolved_order(db, user.id) if user else None
                if unresolved:
                    self._show_unresolved_order(call, unresolved)
                    return
                self.service.set_state(db, call.from_user.id, "wallet_amount")
            self._edit(call, "مبلغ افزایش موجودی را به تومان بفرست (حداقل ۱۰٬۰۰۰).")
            return
        if data.startswith("guide:"):
            self._show_guide(call, data.split(":", 1)[1])
            return
        if data.startswith("buy:"):
            self._buy_callback(call, data)
            return
        if data.startswith(("order:", "pay:")):
            self._payment_callback(call, data)
            return
        if data.startswith("orders:"):
            self._order_history_callback(call, data)
            return
        if data.startswith("receipt:"):
            self._review_callback(call, data)
            return
        if data.startswith(("subs:", "sub:")):
            self._subscription_callback(call, data)
            return
        if data.startswith(("root:", "v2:", "user:")):
            self._root_callback(call, data)
            return
        if data.startswith("admin:"):
            self._admin_callback(call, data)
            return
        if data.startswith("notice:"):
            self._notice_callback(call, data)

    def _basic_callback(self, call: types.CallbackQuery, data: str) -> None:
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            if not user:
                self._edit(call, "ابتدا /start را بزنید.")
                return
            if data == "home:user":
                if user.is_root:
                    self._edit(call, "<b>داشبورد مدیر اصلی</b>", keyboards.root_home())
                else:
                    self._home(call.message.chat.id, user, edit_call=call)
            elif data == "buy:start":
                unresolved = self.service.get_unresolved_order(db, user.id)
                if unresolved:
                    self._show_unresolved_order(call, unresolved)
                elif user.is_admin:
                    self._driving_render(call, "purchase")
                else:
                    self._edit(call, "چه نوع اشتراکی می‌خواهی؟", keyboards.buy_types())
            elif data == "guide:start":
                self._edit(call, "پلتفرمت را انتخاب کن:", keyboards.guide_platforms())
            elif data == "wallet:show":
                markup = keyboards.inline(
                    [("➕ افزایش موجودی", "wallet:add")], [("🏠 خانه", "home:user")]
                )
                text = f"موجودی کیف پول: <b>{toman(user.balance)}</b>"
                if user.permission == int(Permission.ADMIN):
                    floor = wallet_floor(db, user)
                    text += (f"\n🏦 کف مجاز: {toman(floor)}\n💸 بدهی: {toman(max(0, -user.balance))}\n"
                             f"🛒 اعتبار قابل‌استفاده: {toman(max(0, user.balance - floor))}")
                self._edit(call, text, markup)
            elif data in {"referral:show", "referral:share"}:
                if user.is_admin and data == "referral:show":
                    self._driving_render(call, "friends")
                    return
                invited = list(
                    db.scalars(
                        select(User)
                        .where(User.referred_by_id == user.id)
                        .order_by(User.created_at.desc())
                        .limit(20)
                    )
                )
                count = db.scalar(
                    select(func.count()).select_from(User).where(User.referred_by_id == user.id)
                ) or 0
                me = self.bot.get_me()
                link = f"https://t.me/{me.username}?start={user.referral_code}"
                reward = db.get(BotSetting, "referral_reward")
                names = "\n".join(f"• {_h(item.name)}" for item in invited) or "هنوز کسی نپیوسته است."
                friends_text = "" if user.is_admin else f"<b>آخرین دوستان:</b>\n{names}\n\n"
                text = (
                    f"دوستان دعوت‌شده: <b>{count}</b> نفر\n"
                    f"پاداش هر دعوت: <b>{toman(int(reward.value if reward else 0))}</b>\n\n"
                    f"{friends_text}"
                    f"لینک دعوت شما:\n<code>{_h(link)}</code>"
                )
                self._edit(call, text, keyboards.home_button())
            elif data == "support:show":
                support = db.get(BotSetting, "support_username")
                self._edit(
                    call,
                    f"پشتیبانی: <code>{_h(support.value if support else self.settings.support_username)}</code>",
                    keyboards.home_button(),
                )
            elif data == "orders:open":
                if user.is_admin:
                    self._driving_render(call, "orders")
                else:
                    self._render_order_history(call, db, user)

    def _order_history_callback(self, call: types.CallbackQuery, data: str) -> None:
        _, action, value = data.split(":")
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            if not user:
                self._edit(call, "ابتدا /start را بزنید.")
                return
            if action == "list":
                if user.is_admin:
                    self._driving_render(call, "orders")
                else:
                    self._render_order_history(call, db, user, int(value))
                return
            if action == "view":
                order = db.get(Order, int(value))
                if order and user.is_root and order.user_id != user.id:
                    self._edit(call,
                        f"🧾 سفارش #{order.id}\n👤 کاربر #{order.user_id}\n"
                        f"🚦 {order.status.value}\n\n{self.service.order_summary(order)}",
                        keyboards.inline([("🔙 سفارش‌ها", "dl:orders:show")]))
                    return
                if not order or order.user_id != user.id:
                    self.bot.answer_callback_query(
                        call.id, "سفارش متعلق به شما نیست.", show_alert=True
                    )
                    return
                self._show_order_detail(call, order)

    def _show_guide(self, call: types.CallbackQuery, platform: str) -> None:
        guides = {
            "android": "۱) برنامه v2rayNG را نصب کن.\n۲) لینک را کپی کن.\n۳) از + گزینه Import from clipboard را بزن.\n۴) اتصال را روشن کن.",
            "iphone": "۱) برنامه Streisand یا V2Box را نصب کن.\n۲) لینک اشتراک را کپی کن.\n۳) Add Subscription را بزن و لینک را وارد کن.\n۴) Update و سپس Connect.",
            "windows": "۱) برنامه v2rayN را نصب کن.\n۲) لینک را کپی کن.\n۳) از Subscription، اشتراک جدید بساز.\n۴) Update و Set system proxy را فعال کن.",
            "mac": "۱) برنامه Streisand یا FoXray را نصب کن.\n۲) لینک را به‌عنوان Subscription اضافه کن.\n۳) بروزرسانی و اتصال را بزن.",
            "linux": "۱) HiddifyNext را نصب کن.\n۲) Add profile from clipboard را بزن.\n۳) پروفایل را بروزرسانی و متصل شو.",
        }
        if platform not in guides:
            return
        with self.database.session() as db:
            setting = db.get(BotSetting, f"guide_{platform}_file_id")
        if setting and setting.value:
            self.bot.send_video(
                call.message.chat.id,
                setting.value,
                caption=guides[platform],
                reply_markup=keyboards.home_button(),
            )
        else:
            self._edit(call, guides[platform], keyboards.home_button())

    def _buy_callback(self, call: types.CallbackQuery, data: str) -> None:
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            unresolved = self.service.get_unresolved_order(db, user.id) if user else None
            if unresolved:
                self._show_unresolved_order(call, unresolved)
                return
            if user and user.is_admin and not data.startswith("buy:product:"):
                self._driving_render(call, "purchase")
                return
        if data == "buy:type:time":
            self._edit(call, "نوع اکانت را انتخاب کنید:", keyboards.connections())
            return
        if data.startswith("buy:conn:"):
            connections = int(data.rsplit(":", 1)[1])
            with self.database.session() as db:
                user = self.service.get_user(db, call.from_user.id)
                rows = [
                    [
                        (
                            f"🗓️ {months} ماهه — {toman(self.service.quote_time(db, user, connections, months))}",
                            f"buy:time:{connections}:{months}",
                        )
                    ]
                    for months in (1, 3, 6)
                ]
            rows.append([("🔙 بازگشت", "buy:type:time")])
            self._edit(call, "مدت اشتراک را انتخاب کن:", keyboards.inline(*rows))
            return
        if data == "buy:type:data":
            with self.database.session() as db:
                user = self.service.get_user(db, call.from_user.id)
                price = self.service.quote_data(db, user, 10)
                self.service.set_state(db, call.from_user.id, "pick_data", gigabytes=10)
            self._edit(
                call,
                f"قیمت ۱۰ گیگابایت: {toman(price)}\nحجم دلخواه را انتخاب کن:",
                keyboards.data_picker(10),
            )
            return
        if data in {"buy:data:+", "buy:data:-"}:
            with self.database.session() as db:
                state = self.service.get_state(db, call.from_user.id)
                gigabytes = int((state.data or {}).get("gigabytes", 10)) if state else 10
                gigabytes += 5 if data.endswith("+") else -5
                gigabytes = min(50, max(10, gigabytes))
                user = self.service.get_user(db, call.from_user.id)
                price = self.service.quote_data(db, user, gigabytes)
                self.service.set_state(db, call.from_user.id, "pick_data", gigabytes=gigabytes)
            self._edit(
                call,
                f"حجم انتخابی: {gigabytes}GB\nقیمت: <b>{toman(price)}</b>",
                keyboards.data_picker(gigabytes),
            )
            return
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            if not user:
                return
            if data.startswith("buy:product:"):
                product = db.get(Product, int(data.rsplit(":", 1)[1]))
                if not product:
                    raise ValueError("بسته پیدا نشد")
                order = self.service.create_order(db, user, product.product_type, product_id=product.id)
            elif data.startswith("buy:time:"):
                _, _, connection_text, month_text = data.split(":")
                order = self.service.create_order(
                    db,
                    user,
                    ProductType.TIME,
                    connections=int(connection_text),
                    months=int(month_text),
                )
            elif data.startswith("buy:data:confirm:"):
                gigabytes = int(data.rsplit(":", 1)[1])
                order = self.service.create_order(db, user, ProductType.DATA, data_gb=gigabytes)
            else:
                return
            summary = self.service.order_summary(order)
            self.service.clear_state(db, call.from_user.id)
            self._edit(call, f"<b>پیش‌فاکتور نهایی</b>\n\n{summary}", keyboards.order_confirm(order.id))

    def _payment_callback(self, call: types.CallbackQuery, data: str) -> None:
        action, _, order_text = data.partition(":")
        if action == "order":
            subaction, order_id_text = order_text.split(":", 1)
        else:
            subaction, order_id_text = order_text.split(":", 1)
        order_id = int(order_id_text)
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            order = db.get(Order, order_id)
            if not user or not order or order.user_id != user.id:
                self.bot.answer_callback_query(call.id, "سفارش معتبر نیست.", show_alert=True)
                return
            if action == "order" and subaction == "cancel":
                if order.status == OrderStatus.PENDING_REVIEW:
                    self.bot.answer_callback_query(
                        call.id,
                        "رسید در حال بررسی است و فعلاً امکان لغو وجود ندارد.",
                        show_alert=True,
                    )
                    return
                if order.status == OrderStatus.PAID:
                    self.bot.answer_callback_query(
                        call.id, "سفارش پرداخت شده قابل لغو نیست.", show_alert=True
                    )
                    return
                order.status = OrderStatus.CANCELLED
                record_action(db, user.id, "order.cancelled", "❌ لغو سفارش", actor_id=user.telegram_id,
                    source_key=f"order:cancelled:{order.id}", details={"order_id": order.id})
                state = self.service.get_state(db, call.from_user.id)
                if state and int((state.data or {}).get("order_id", 0)) == order.id:
                    self.service.clear_state(db, call.from_user.id)
                self._home(call.message.chat.id, user, edit_call=call)
                return
            if action == "order" and subaction == "confirm":
                self._edit(call, "روش پرداخت را انتخاب کن:", keyboards.payment(order.id))
                return
            if action == "pay" and subaction == "card":
                card = db.get(BotSetting, "card_number")
                holder = db.get(BotSetting, "card_holder")
                text = (
                    f"لطفاً مبلغ <b>{toman(order.amount)}</b> را به کارت زیر واریز کن:\n\n"
                    f"<code>{_h(card.value if card else self.settings.card_number)}</code>\n"
                    f"به نام: <b>{_h(holder.value if holder else self.settings.card_holder)}</b>\n\n"
                    "سپس دکمه ارسال رسید را بزن."
                )
                self._edit(call, text, keyboards.send_receipt(order.id))
                return
            if action == "pay" and subaction == "receipt":
                self.service.set_state(db, call.from_user.id, "await_receipt", order_id=order.id)
                self._edit(
                    call,
                    "تصویر یا فایل رسید پرداخت را همین‌جا ارسال کن.\nپس از ارسال، سفارش وارد صف بررسی می‌شود.",
                    keyboards.inline([("❌ انصراف", f"order:cancel:{order.id}")]),
                )

    def receive_receipt(self, message: types.Message) -> None:
        receipt_id = 0
        caption = ""
        file_id = ""
        media_type = "photo"
        group_id = 0
        topic_id = 0
        root_destinations: list[int] = []
        with self.database.session() as db:
            state = self.service.get_state(db, message.from_user.id)
            user = self.service.get_user(db, message.from_user.id)
            if not state or state.state != "await_receipt" or not user:
                self.bot.reply_to(message, "برای ارسال رسید، ابتدا از بخش خرید یک سفارش بسازید.")
                return
            order = db.get(Order, int(state.data["order_id"]))
            if not order or order.user_id != user.id or order.status == OrderStatus.PAID:
                self.bot.reply_to(message, "این سفارش دیگر قابل پرداخت نیست.")
                return
            if message.photo:
                file_id, media_type = message.photo[-1].file_id, "photo"
            else:
                file_id, media_type = message.document.file_id, "document"
            receipt = PaymentReceipt(
                order_id=order.id,
                user_id=user.id,
                telegram_file_id=file_id,
                media_type=media_type,
            )
            db.add(receipt)
            db.flush()
            receipt_id = receipt.id
            record_action(db, user.id, "receipt.submitted", "📎 ارسال رسید پرداخت",
                actor_id=user.telegram_id, source_key=f"receipt:submitted:{receipt.id}",
                details={"receipt_id": receipt.id, "order_id": order.id, "amount": order.amount,
                    "content_type": media_type})
            order.status = OrderStatus.PENDING_REVIEW
            self.service.clear_state(db, message.from_user.id)
            caption = (
                f"<b>رسید جدید #{receipt.id}</b>\n"
                f"سفارش: #{order.id}\n"
                f"کاربر: {_h(user.name)}\n"
                f"Telegram ID: <code>{user.telegram_id}</code>\n"
                f"مبلغ: <b>{toman(order.amount)}</b>\n\n"
                f"{self.service.order_summary(order)}"
            )
            group_setting = db.get(BotSetting, "payment_group_id")
            topic_setting = db.get(BotSetting, "payment_topic_id")
            group_id = int(group_setting.value) if group_setting else self.settings.payment_group_id
            topic_id = int(topic_setting.value) if topic_setting else self.settings.payment_topic_id
            root_destinations = list(
                db.scalars(
                    select(User.telegram_id).where(
                        User.permission == int(Permission.ROOT),
                        User.status == UserStatus.ACTIVE,
                    )
                )
            )
            root_destinations.extend(
                item
                for item in self.settings.root_telegram_ids
                if item not in root_destinations
            )

        sent_messages = []
        if group_id:
            sent = self._deliver_receipt(
                chat_id=group_id,
                topic_id=topic_id,
                receipt_id=receipt_id,
                caption=caption,
                file_id=file_id,
                media_type=media_type,
            )
            if sent:
                sent_messages.append(sent)
            else:
                log.warning(
                    "receipt %s could not be delivered to configured group %s topic %s; falling back to roots",
                    receipt_id,
                    group_id,
                    topic_id,
                )
        if not sent_messages:
            for destination in root_destinations:
                sent = self._deliver_receipt(
                    chat_id=destination,
                    topic_id=0,
                    receipt_id=receipt_id,
                    caption=caption,
                    file_id=file_id,
                    media_type=media_type,
                )
                if sent:
                    sent_messages.append(sent)
        if sent_messages:
            latest = sent_messages[-1]
            with self.database.session() as db:
                saved_receipt = db.get(PaymentReceipt, receipt_id)
                if saved_receipt:
                    saved_receipt.review_chat_id = latest.chat.id
                    saved_receipt.review_message_id = latest.message_id
                    event = record_action(db, saved_receipt.user_id, "receipt.submitted", "📎 ارسال رسید پرداخت",
                        source_key=f"receipt:submitted:{receipt_id}", details={"receipt_id": receipt_id})
                    event.details = {**event.details, "review_chat_id": latest.chat.id,
                        "review_message_id": latest.message_id,
                        "review_topic_id": topic_id if latest.chat.id == group_id else 0}
            self.bot.reply_to(message, "رسید دریافت شد ✅\nپس از بررسی نتیجه را همین‌جا اعلام می‌کنیم.")
        else:
            log.error("receipt %s was saved but no review destination was reachable", receipt_id)
            self.bot.reply_to(
                message,
                "رسید ذخیره شد، اما مسیر ارسال به مدیر در دسترس نبود. لطفاً با پشتیبانی تماس بگیرید.",
            )

    def _deliver_receipt(
        self,
        *,
        chat_id: int,
        topic_id: int,
        receipt_id: int,
        caption: str,
        file_id: str,
        media_type: str,
    ):
        kwargs = {
            "chat_id": chat_id,
            "caption": caption,
            "parse_mode": "HTML",
            "reply_markup": keyboards.review_receipt(receipt_id),
        }
        if topic_id:
            kwargs["message_thread_id"] = topic_id
        try:
            if media_type == "photo":
                return self.bot.send_photo(photo=file_id, **kwargs)
            return self.bot.send_document(document=file_id, **kwargs)
        except (ApiTelegramException, requests.RequestException) as exc:
            log.warning(
                "Telegram rejected receipt delivery to chat=%s topic=%s: %s",
                chat_id,
                topic_id,
                exc,
            )
            return None

    def _review_callback(self, call: types.CallbackQuery, data: str) -> None:
        reviewer = self._authorized(call.from_user.id)
        if not reviewer:
            self.bot.answer_callback_query(call.id, "دسترسی بررسی رسید ندارید.", show_alert=True)
            return
        _, decision, receipt_text = data.split(":")
        account = None
        with self.database.session() as db:
            receipt = db.scalar(
                select(PaymentReceipt)
                .where(PaymentReceipt.id == int(receipt_text))
                .with_for_update()
            )
            if not receipt or receipt.decision:
                self.bot.answer_callback_query(call.id, "این رسید قبلاً بررسی شده است.", show_alert=True)
                return
            order = db.get(Order, receipt.order_id)
            user = db.get(User, receipt.user_id)
            if not order or not user:
                return
            receipt.reviewed_by_id = reviewer.id
            receipt.decision_at = datetime.now(UTC)
            record_action(db, user.id, "receipt.reviewed", "🧾 نتیجه بررسی رسید",
                actor_id=call.from_user.id, source_key=f"receipt:decision:{receipt.id}",
                details={"receipt_id": receipt.id, "order_id": order.id,
                    "decision": "approved" if decision == "approve" else "denied"})
            if decision == "approve":
                account = self.service.provision_order(db, order)
                receipt.decision = "approved"
                result_text = "✅ تأیید شد"
            else:
                receipt.decision = "denied"
                order.status = OrderStatus.REJECTED
                self.service.set_state(db, user.telegram_id, "await_receipt", order_id=order.id)
                result_text = "❌ رد شد"
            user_telegram_id = user.telegram_id
            order_id = order.id
        try:
            old_caption = call.message.caption or "رسید پرداخت"
            self.bot.edit_message_caption(
                f"{old_caption}\n\n<b>{result_text}</b> توسط {_h(reviewer.name)}",
                call.message.chat.id,
                call.message.message_id,
                parse_mode="HTML",
                reply_markup=None,
            )
        except (ApiTelegramException, requests.RequestException):
            pass
        if decision == "approve":
            if account:
                url = self.panel.subscription_url(account)
                self.bot.send_photo(
                    user_telegram_id,
                    generate_qr(url),
                    caption=(
                        "پرداخت تأیید و اشتراک ساخته شد ✅\n\n"
                        f"لینک اشتراک:\n<code>{_h(url)}</code>"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboards.inline(
                        [("📚 راهنمای اتصال", "guide:start")], [("🏠 خانه", "home:user")]
                    ),
                )
            else:
                self.bot.send_message(user_telegram_id, "پرداخت تأیید و کیف پول شارژ شد ✅")
        else:
            self.bot.send_message(
                user_telegram_id,
                "رسید قابل تأیید نبود ❌\n"
                "تا ارسال رسید صحیح یا لغو این سفارش، امکان خرید جدید وجود ندارد.",
                reply_markup=keyboards.inline(
                    [("📎 ارسال مجدد رسید", f"pay:receipt:{order_id}")],
                    [("❌ لغو سفارش", f"order:cancel:{order_id}")],
                ),
            )

    def _subscription_callback(self, call: types.CallbackQuery, data: str) -> None:
        with self.database.session() as db:
            user = self.service.get_user(db, call.from_user.id)
            if not user:
                return
            if data.startswith("subs:list:"):
                if user.is_admin:
                    self._driving_render(call, "subs")
                    return
                links = list(
                    db.scalars(
                        select(V2AccountLink)
                        .where(V2AccountLink.user_id == user.id)
                        .order_by(V2AccountLink.id.desc())
                    )
                )
                rows = []
                for index, link in enumerate(links, 1):
                    account = self.panel.get_user(link.v2_user_id)
                    if account:
                        rows.append(
                            [(f"{_status_icon(account)} #{index} | 📦 {link.label}", f"sub:view:{link.v2_user_id}")]
                        )
                rows.append([("🏠 خانه", "home:user")])
                self._edit(
                    call,
                    "<b>اشتراک‌های من</b>\n🟢 فعال  🟡 نزدیک پایان  🔴 پایان‌یافته",
                    keyboards.inline(*rows),
                )
                return
            _, action, v2_text = data.split(":")
            v2_id = int(v2_text)
            link = db.scalar(
                select(V2AccountLink).where(
                    V2AccountLink.user_id == user.id, V2AccountLink.v2_user_id == v2_id
                )
            )
            if not link:
                self.bot.answer_callback_query(call.id, "این اشتراک متعلق به شما نیست.", show_alert=True)
                return
            if is_archived(db, v2_id):
                self._edit(call, "🗃 این اکانت بایگانی شده است.",
                    keyboards.inline([("🗃 مشاهده سابقه", f"archive:view:{v2_id}")]))
                return
            account = self.panel.get_user(v2_id)
            if not account:
                self._edit(call, "اکانت در پنل پیدا نشد.", keyboards.home_button())
                return
            if action == "auto":
                link.auto_renewal = not link.auto_renewal
                state = "فعال" if link.auto_renewal else "غیرفعال"
                self.bot.answer_callback_query(call.id, f"تمدید خودکار {state} شد.", show_alert=True)
            display_name = link.label if link.admin_created else user.name
            list_back = None
            if user.is_admin:
                origin = db.get(BotSetting, f"driving_origin:{user.id}")
                if origin and origin.value in {"customers", "subs"}:
                    list_back = f"dl:{origin.value}:show"
            brand_url = None
            if link.admin_created and self.settings.brand_subscription_base_url:
                brand = db.get(BotSetting, f"admin_brand:{user.id}")
                brand_active = db.get(BotSetting, f"admin_brand_active:{user.id}")
                if brand and brand.value.strip() and (
                    not brand_active or brand_active.value == "1"
                ):
                    brand_url = (
                        f"{self.settings.brand_subscription_base_url}{account['token']}"
                    )
            self._send_account(
                call.message.chat.id,
                account,
                link.label,
                display_name=display_name,
                auto_renewal=link.auto_renewal,
                unlimited=link.product_type == ProductType.TIME,
                url_override=brand_url,
                reseller=link.admin_created and user.is_admin,
                list_back=list_back,
            )

    def _send_account(
        self,
        chat_id: int,
        account: dict[str, Any],
        label: str,
        *,
        display_name: str,
        auto_renewal: bool = False,
        unlimited: bool = False,
        url_override: str | None = None,
        reseller: bool = False,
        list_back: str | None = None,
    ) -> None:
        url = url_override or self.panel.subscription_url(account)
        caption = _account_caption(
            account,
            label=label,
            display_name=display_name,
            auto_renewal=auto_renewal,
            unlimited=unlimited,
            url=url,
        )
        self.bot.send_photo(
            chat_id,
            generate_qr(url),
            caption=caption,
            parse_mode="HTML",
            reply_markup=keyboards.account_actions(int(account["id"]), reseller=reseller, list_back=list_back),
        )

    def _root_callback(self, call: types.CallbackQuery, data: str) -> None:
        if not self._authorized(call.from_user.id, root=True):
            self.bot.answer_callback_query(call.id, "دسترسی مدیر اصلی لازم است.", show_alert=True)
            return
        if data == "root:products":
            self._catalog_callback(call, "catalog:plans:1")
            return
        if data.startswith("root:v2:"):
            page = int(data.rsplit(":", 1)[1])
            self._render_v2_list(call, page)
            return
        if data.startswith("root:users:"):
            page = int(data.rsplit(":", 1)[1])
            self._render_bot_users(call, page)
            return
        if data == "root:settings":
            self._driving_render(call, "settings")
            return
        if data == "root:notices":
            self._edit(
                call,
                "اعلان برای چه گروهی ارسال شود؟",
                keyboards.inline(
                    [("📣 همه کاربران", "notice:all")],
                    [("👤 فقط کاربران", "notice:users"), ("🧑‍💼 فقط نماینده‌ها", "notice:admins")],
                    [("🔙 بازگشت", "root:home")],
                ),
            )
            return
        if data == "root:home":
            self._edit(call, "<b>داشبورد مدیر اصلی</b>", keyboards.root_home())
            return
        if data == "root:finance":
            with self.database.session() as db:
                paid_count = db.scalar(select(func.count()).select_from(Order).where(Order.status == OrderStatus.PAID)) or 0
                revenue = db.scalar(select(func.sum(Order.amount)).where(Order.status == OrderStatus.PAID)) or 0
                pending = db.scalar(select(func.count()).select_from(Order).where(Order.status == OrderStatus.PENDING_REVIEW)) or 0
            self._edit(
                call,
                f"<b>گزارش مالی</b>\nفروش تأییدشده: {paid_count}\nدرآمد: {toman(revenue)}\nدر انتظار بررسی: {pending}",
                keyboards.inline([("🧾 لیست سفارش‌ها", "dl:orders:show")], [("🔙 بازگشت", "root:home")]),
            )
            return
        if data.startswith("v2:"):
            self._v2_action(call, data)
            return
        if data.startswith("user:"):
            self._user_action(call, data)

    def _render_v2_list(self, call, page=1):
        self._driving_render(call, "v2", "page" if page > 1 else "show", str(page))

    def _render_bot_users(self, call, page=1):
        self._driving_render(call, "users", "page" if page > 1 else "show", str(page))

    def _v2_action(self, call: types.CallbackQuery, data: str) -> None:
        parts = data.split(":")
        action = parts[1]
        if action in {"search", "sort", "quick", "col"}:
            operation = {"search": "search", "sort": "sortmenu", "quick": "quick"}.get(action)
            if action == "col":
                operation = "add" if parts[2] == "+" else "remove"
            self._driving_render(call, "v2", operation)
            return
        v2_id = int(parts[2])
        account = self.panel.get_user(v2_id)
        if not account:
            self._edit(call, "اکانت پیدا نشد.", keyboards.inline([("🔙 بازگشت", "root:v2:1")]))
            return
        with self.database.session() as db:
            monthly = monthly_info(db, v2_id)
            archived = is_archived(db, v2_id)
        if archived and action not in {"view", "qr", "owner", "paymentdetail"}:
            raise ValueError("اکانت بایگانی شده؛ ابتدا از فهرست بایگانی بازیابی کنید")
        if action in {"owner", "emergency", "custom", "payment", "package", "pickplan", "applyextra", "resetask", "renewask", "paymentdetail"}:
            self._v2_extra_action(call, action, v2_id, parts)
            return
        if action == "view":
            with self.database.session() as db:
                state = self.service.get_state(db, call.from_user.id)
                if state and state.state in {"root_v2_input", "root_v2_confirm", "v2_remarks"}:
                    self.service.clear_state(db, call.from_user.id)
                text = self._v2_card(db, account)
                if archived:
                    text = "🗃 <b>اکانت بایگانی‌شده — فقط مشاهده</b>\n\n" + text
            self._edit(call, text, self._root_v2_markup(v2_id, call.from_user.id))
        elif action == "qr":
            url = self.panel.subscription_url(account)
            self.bot.send_photo(
                call.message.chat.id,
                generate_qr(url),
                caption=f"اکانت #{v2_id}\n<code>{_h(url)}</code>",
                parse_mode="HTML",
                reply_markup=self._root_v2_markup(v2_id, call.from_user.id),
            )
        elif action == "update":
            self._edit(
                call,
                f"چه تغییری روی اشتراک #{v2_id} اعمال شود؟",
                keyboards.inline(
                    [("📊 +۱۰GB", f"v2:traffic:{v2_id}:10"), ("📊 +۵۰GB", f"v2:traffic:{v2_id}:50")],
                    [("🗓 تمدید ماهانه با کسر اضطراری" if monthly else "🗓️ +۳۰ روز",
                      f"emergency:renew:{v2_id}:{monthly['generation']}" if monthly else f"v2:addmonth:{v2_id}"),
                     ("👤 یک‌کاربره", f"v2:device:{v2_id}:1")],
                    [("👥 دوکاربره", f"v2:device:{v2_id}:2"), ("♾️ کاربر نامحدود", f"v2:device:{v2_id}:0")],
                    [("🔙 بازگشت", f"v2:view:{v2_id}")],
                ),
            )
        elif action == "traffic":
            self.panel.add_transfer(v2_id, int(parts[3]))
            self.bot.answer_callback_query(call.id, f"{parts[3]} گیگابایت اضافه شد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "addmonth":
            if monthly:
                self._edit(call, f"🗓 {max(0, 31 - monthly['used'])} روز پس از کسر {monthly['used']} روز اضطراری اضافه شود؟",
                    keyboards.inline([("✅ تمدید ماهانه", f"emergency:renew:{v2_id}:{monthly['generation']}")],
                                     [("❌ انصراف", f"v2:view:{v2_id}")]))
                return
            self.panel.add_days(v2_id, 30)
            self.bot.answer_callback_query(call.id, "۳۰ روز اضافه شد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "device":
            limit = int(parts[3])
            self.panel.set_device_limit(v2_id, limit or None)
            self.bot.answer_callback_query(call.id, "تعداد اتصال تغییر کرد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "remarks":
            with self.database.session() as db:
                self.service.set_state(db, call.from_user.id, "v2_remarks", v2_id=v2_id)
            self._edit(call, "یادداشت جدید را بفرست.")
        elif action == "addday":
            self.panel.add_days(v2_id, 1)
            self.bot.answer_callback_query(call.id, "یک روز اضافه شد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "ban":
            self.panel.set_banned(v2_id, not bool(account.get("banned")))
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "reset":
            self.panel.reset_usage(v2_id)
            self.bot.answer_callback_query(call.id, "مصرف صفر شد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "renew":
            self.panel.renew_credentials(v2_id)
            self.bot.answer_callback_query(call.id, "توکن و UUID تغییر کرد.", show_alert=True)
            self._v2_action(call, f"v2:view:{v2_id}")
        elif action == "deleteask":
            self._edit(
                call,
                f"حذف اکانت #{v2_id} غیرقابل‌بازگشت است. مطمئنی؟",
                keyboards.delete_confirm(v2_id),
            )
        elif action == "delete":
            old_markup = self._root_v2_markup(v2_id, call.from_user.id)
            back = next((button.callback_data for row in old_markup.keyboard for button in row
                         if button.callback_data == "root:v2:1" or button.callback_data.startswith("dl:uv")), "root:v2:1")
            with self.database.session() as db:
                link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
                if link:
                    user = db.get(User, link.user_id)
                    if user:
                        user.v2accounts = [item for item in (user.v2accounts or []) if item != v2_id]
                    db.delete(link)
            self.panel.delete_user(v2_id)
            self._edit(call, "اکانت حذف شد.", keyboards.inline([("🔙 بازگشت به لیست", back)]))


    def _admin_callback(self, call: types.CallbackQuery, data: str) -> None:
        admin = self._authorized(call.from_user.id)
        if not admin:
            return
        if data == "admin:create":
            with self.database.session() as db:
                self.service.set_state(db, call.from_user.id, "admin_username")
            self._edit(
                call,
                "نام کاربری مشتری را بفرست یا ساخت خودکار را بزن:",
                keyboards.inline([("🎲 ساخت خودکار", "admin:username:auto")], [("🔙 بازگشت", "admin:home")]),
            )
            return
        if data == "admin:username:auto":
            with self.database.session() as db:
                self.service.set_state(db, call.from_user.id, "admin_pick_type", username=f"customer{int(time.time())}")
            self._driving_render(call, "pick")
            return
        if data.startswith(("admin:type:", "admin:confirm:")) or data == "admin:note:skip":
            self._edit(call, "این دکمه قدیمی است؛ از ساخت اکانت دوباره شروع کنید.", keyboards.admin_home())
            return
        if data.startswith("admin:users:"):
            self._driving_render(call, "customers")
            return
        if data == "admin:brand":
            with self.database.session() as db:
                admin_user = self.service.get_user(db, call.from_user.id)
                brand = db.get(BotSetting, f"admin_brand:{admin_user.id}")
                active = db.get(BotSetting, f"admin_brand_active:{admin_user.id}")
                brand_name = brand.value if brand else "تنظیم نشده"
                is_active = bool(brand and (not active or active.value == "1"))
            public_status = (
                "🟢 آماده"
                if self.settings.brand_subscription_base_url
                else "🟡 آدرس عمومی تنظیم نشده؛ لینک اصلی استفاده می‌شود"
            )
            self._edit(
                call,
                "<b>🎨 برند شخصی</b>\n\n"
                f"🏷️ نام برند: <b>{_h(brand_name)}</b>\n"
                f"🚦 وضعیت: {'🟢 فعال' if is_active else '🔴 غیرفعال'}\n"
                f"🌐 API برند: {public_status}",
                keyboards.inline(
                    [("✏️ تغییر نام برند", "admin:brand:set")],
                    [
                        (
                            "⏸️ غیرفعال‌سازی" if is_active else "▶️ فعال‌سازی",
                            "admin:brand:disable" if is_active else "admin:brand:enable",
                        )
                    ],
                    [("🔙 بازگشت", "admin:home")],
                ),
            )
            return
        if data == "admin:brand:set":
            with self.database.session() as db:
                self.service.set_state(db, call.from_user.id, "admin_brand")
            self._edit(call, "🎨 نام برند شخصی را بفرست (۲ تا ۵۰ نویسه).")
            return
        if data in {"admin:brand:disable", "admin:brand:enable"}:
            with self.database.session() as db:
                admin_user = self.service.get_user(db, call.from_user.id)
                brand = db.get(BotSetting, f"admin_brand:{admin_user.id}")
                if not brand:
                    self.bot.answer_callback_query(
                        call.id, "ابتدا نام برند را تنظیم کنید.", show_alert=True
                    )
                    return
                self._save_setting(
                    db,
                    f"admin_brand_active:{admin_user.id}",
                    0 if data.endswith("disable") else 1,
                )
            self.bot.answer_callback_query(call.id, "وضعیت برند تغییر کرد ✅", show_alert=True)
            self._admin_callback(call, "admin:brand")
            return
        if data == "admin:home":
            with self.database.session() as db:
                self.service.clear_state(db, call.from_user.id)
            self._edit(call, "<b>داشبورد نمایندگی</b>", keyboards.admin_home())

    def _notice_callback(self, call: types.CallbackQuery, data: str) -> None:
        if not self._authorized(call.from_user.id, root=True):
            return
        target = data.split(":", 1)[1]
        if target not in {"all", "users", "admins"}:
            return
        with self.database.session() as db:
            self.service.set_state(db, call.from_user.id, "await_notice", target=target)
        self._edit(
            call,
            "متن اعلان را در یک پیام بفرست. ارسال پس از دریافت متن بلافاصله آغاز می‌شود.",
            keyboards.inline([("❌ انصراف", "root:home")]),
        )

    def text_message(self, message: types.Message) -> None:
        if message.text.startswith("/"):
            return
        with self.database.session() as db:
            state = self.service.get_state(db, message.from_user.id)
            if not state:
                self.bot.reply_to(message, "برای نمایش منو /start را بزنید.")
                return
            state_name = state.state
            payload = dict(state.data or {})
            if state_name == "expiry_archive_days":
                self._archive_config_message(db, message, payload)
                return
            if state_name == "root_v2_input":
                self._v2_input_message(db, message, payload)
                return
            if state_name == "driving_edit":
                self._driving_edit_message(db, message, payload)
                return
            if state_name == "driving_search":
                self._driving_search_message(db, message, payload)
                return
            if state_name == "catalog_price":
                self._catalog_price_message(db, message, payload)
                return
            if state_name == "resell_note":
                admin = self.service.get_user(db, message.from_user.id)
                if not admin or not admin.is_admin:
                    return
                payload["note"] = message.text.strip()[:200]
                self._resell_preview(db, admin, payload, message=message)
                return
            if state_name == "await_name":
                name = message.text.strip()
                if len(name) < 2 or len(name) > 100:
                    self.bot.reply_to(message, "نام باید بین ۲ تا ۱۰۰ نویسه باشد.")
                    return
                try:
                    user, account = self.service.register_user(
                        db,
                        telegram_id=message.from_user.id,
                        name=name,
                        referral_code=payload.get("referral") or None,
                    )
                except ValueError as exc:
                    self.bot.reply_to(message, _h(exc))
                    return
                url = self.panel.subscription_url(account)
                self.bot.send_photo(
                    message.chat.id,
                    generate_qr(url),
                    caption=(
                        f"{_h(user.name)} عزیز، حسابت ساخته شد ✅\n"
                        "یک اشتراک آزمایشی یک‌روزه برایت فعال کردیم.\n\n"
                        f"لینک اشتراک:\n<code>{_h(url)}</code>"
                    ),
                    parse_mode="HTML",
                    reply_markup=keyboards.inline(
                        [("📚 راهنمای اتصال", "guide:start")], [("🏠 خانه", "home:user")]
                    ),
                )
                return
            if state_name == "wallet_amount":
                normalized = (
                    message.text.replace(",", "").replace("٬", "").replace(" ", "")
                )
                if not normalized.isdigit():
                    self.bot.reply_to(message, "مبلغ را فقط به‌صورت عددی و به تومان بفرست.")
                    return
                user = self.service.get_user(db, message.from_user.id)
                try:
                    order = self.service.create_order(
                        db, user, ProductType.WALLET, amount=int(normalized)
                    )
                except ValueError as exc:
                    self.bot.reply_to(message, str(exc))
                    return
                self.service.clear_state(db, message.from_user.id)
                self.bot.send_message(
                    message.chat.id,
                    f"<b>پیش‌فاکتور</b>\n\n{self.service.order_summary(order)}",
                    parse_mode="HTML",
                    reply_markup=keyboards.order_confirm(order.id),
                )
                return
            if state_name == "v2_search":
                query = message.text.strip()[:100]
                self.service.set_state(
                    db, message.from_user.id, "v2_list", search=query, sort="id", desc=True
                )
                self.bot.send_message(
                    message.chat.id,
                    f"جست‌وجوی <code>{_h(query)}</code> آماده است.",
                    parse_mode="HTML",
                    reply_markup=keyboards.inline([("🔎 نمایش نتایج", "root:v2:1")]),
                )
                return
            if state_name == "bot_user_search":
                query = message.text.strip()[:100]
                self.service.set_state(db, message.from_user.id, "bot_users", search=query)
                self.bot.send_message(
                    message.chat.id,
                    f"جست‌وجوی <code>{_h(query)}</code> آماده است.",
                    parse_mode="HTML",
                    reply_markup=keyboards.inline([("🔎 نمایش نتایج", "root:users:1")]),
                )
                return
            if state_name == "v2_remarks":
                user = self.service.get_user(db, message.from_user.id)
                if not user or not user.is_root:
                    self.service.clear_state(db, message.from_user.id)
                    return
                v2_id = int(payload["v2_id"])
                self.panel.update_remarks(v2_id, message.text.strip())
                self.service.clear_state(db, message.from_user.id)
                self.bot.send_message(
                    message.chat.id,
                    "یادداشت ذخیره شد.",
                    reply_markup=keyboards.inline([("👁️ نمایش اکانت", f"v2:view:{v2_id}")]),
                )
                return
            if state_name == "bot_user_rename":
                root = self.service.get_user(db, message.from_user.id)
                target = db.get(User, int(payload["user_id"]))
                name = message.text.strip()
                if not root or not root.is_root or not target or not 2 <= len(name) <= 100:
                    self.bot.reply_to(message, "نام معتبر نیست.")
                    return
                target.name = name
                self.service.clear_state(db, message.from_user.id)
                self.bot.reply_to(message, "نام کاربر تغییر کرد.")
                return
            if state_name == "await_notice":
                sender = self.service.get_user(db, message.from_user.id)
                if not sender or not sender.is_root:
                    self.service.clear_state(db, message.from_user.id)
                    return
                target = payload.get("target", "all")
                query = select(User.telegram_id).where(User.status == UserStatus.ACTIVE)
                if target == "users":
                    query = query.where(User.permission == int(Permission.USER))
                elif target == "admins":
                    query = query.where(User.permission == int(Permission.ADMIN))
                recipients = list(db.scalars(query))
                self.service.clear_state(db, message.from_user.id)
                notice_text = f"📣 <b>اطلاعیه</b>\n\n{_h(message.text.strip())}"
                sent = 0
                for telegram_id in recipients:
                    try:
                        self.bot.send_message(telegram_id, notice_text, parse_mode="HTML")
                        sent += 1
                    except (ApiTelegramException, requests.RequestException):
                        log.warning("could not deliver notice to %s", telegram_id)
                self.bot.reply_to(message, f"اعلان برای {sent} نفر ارسال شد.")
                return
            if state_name == "admin_brand":
                admin = self.service.get_user(db, message.from_user.id)
                brand = message.text.strip()
                if not admin or not admin.is_admin or not 2 <= len(brand) <= 50:
                    self.bot.reply_to(message, "نام برند باید بین ۲ تا ۵۰ نویسه باشد.")
                    return
                key = f"admin_brand:{admin.id}"
                setting = db.get(BotSetting, key)
                if setting:
                    setting.value = brand
                else:
                    db.add(BotSetting(key=key, value=brand))
                self._save_setting(db, f"admin_brand_active:{admin.id}", 1)
                self.service.clear_state(db, message.from_user.id)
                self.bot.reply_to(
                    message,
                    "برند شخصی ذخیره و فعال شد ✅",
                    reply_markup=keyboards.inline(
                        [("🎨 مدیریت برند", "admin:brand")],
                        [("🔙 داشبورد نمایندگی", "admin:home")],
                    ),
                )
                return
            if state_name == "admin_username":
                admin = self.service.get_user(db, message.from_user.id)
                if not admin or not admin.is_admin:
                    return
                username = message.text.strip()
                if not username or len(username) > 64:
                    self.bot.reply_to(message, "نام کاربری باید حداکثر ۶۴ نویسه باشد.")
                    return
                self.service.set_state(
                    db, message.from_user.id, "admin_pick_type", username=username
                )
                self._driving_send(message, "pick")
                return
        self.bot.reply_to(message, "این پیام در مرحله فعلی قابل پردازش نیست؛ /start را بزنید.")
