"""Public plan management and independent reseller entitlements/prices."""

from __future__ import annotations

import html
from types import SimpleNamespace

from sqlalchemy import func, select

from . import keyboards
from .archive import is_archived
from .models import (
    AdminPrice,
    BotSetting,
    OrderStatus,
    Permission,
    Product,
    ProductType,
    User,
    V2AccountLink,
    V2AccountOrigin,
    WalletTransaction,
)
from .services import toman
from .wallet import can_spend, wallet_floor


class CatalogMixin:
    def _catalog_callback(self, call, data):
        if not self._authorized(call.from_user.id, root=True):
            return
        parts = data.split(":")
        action = parts[1]
        if action in {"plans", "admins", "assignments"}:
            key = f"ap{parts[2]}" if action == "assignments" else action
            self._driving_render(call, key)
            return
        with self.database.session() as db:
            state = self.service.get_state(db, call.from_user.id)
            if state and state.state == "catalog_price" and action not in {"publicprice", "adminprice"}:
                self.service.clear_state(db, call.from_user.id)
            rows = []
            if action in {"plan", "publicprice", "toggle"}:
                product = db.get(Product, int(parts[2]))
                if not product:
                    return
                if action == "publicprice":
                    self.service.set_state(
                        db, call.from_user.id, "catalog_price", product_id=product.id
                    )
                    self._edit(
                        call,
                        "💰 قیمت فروش به کاربران ربات را به تومان بفرستید.",
                        keyboards.inline([("❌ انصراف", f"catalog:plan:{product.id}")]),
                    )
                    return
                if action == "toggle":
                    product.enabled = not product.enabled
                details = self.service.order_summary(
                    SimpleNamespace(
                        product_type=product.product_type,
                        months=product.months,
                        data_gb=product.data_gb,
                        connections=product.connections,
                        amount=product.price,
                    )
                )
                self._edit(
                    call,
                    f"<b>📦 {html.escape(product.title)}</b>\n\n{details}\n\n"
                    f"فروش عمومی: {'🟢 فعال' if product.enabled else '🔴 غیرفعال'}\n"
                    "این قیمت مخصوص کاربران ربات است و قیمت ادمین‌ها را تغییر نمی‌دهد.",
                    keyboards.inline(
                        [("💰 تغییر قیمت کاربران", f"catalog:publicprice:{product.id}")],
                        [("🔄 فعال / غیرفعال برای کاربران", f"catalog:toggle:{product.id}")],
                        [("🔙 بسته‌ها", "catalog:plans:1")],
                    ),
                )
                return
            admin = db.get(User, int(parts[2]))
            if not admin or admin.permission != int(Permission.ADMIN):
                self._edit(
                    call, "ادمین پیدا نشد.", keyboards.inline([("🔙 ادمین‌ها", "catalog:admins:1")])
                )
                return
            if action == "admin":
                count = (
                    db.scalar(
                        select(func.count())
                        .select_from(V2AccountLink)
                        .where(
                            V2AccountLink.user_id == admin.id, V2AccountLink.admin_created.is_(True)
                        )
                    )
                    or 0
                )
                self._edit(
                    call,
                    f"<b>🧑‍💼 {html.escape(admin.name)}</b>\n"
                    f"🆔 {admin.telegram_id}\n💳 موجودی: {toman(admin.balance)}\n"
                    f"🏦 کف مجاز کیف پول: {toman(wallet_floor(db, admin))}\n"
                    f"💸 بدهی: {toman(max(0, -admin.balance))}\n"
                    f"🛒 اعتبار قابل‌استفاده: {toman(max(0, admin.balance - wallet_floor(db, admin)))}\n"
                    f"📦 اکانت‌های ساخته‌شده: {count}\n🚦 وضعیت: {admin.status.value}",
                    keyboards.inline(
                        [("📦 بسته‌ها و قیمت اختصاصی", f"catalog:assignments:{admin.id}:1")],
                        [("🛠 مدیریت حساب ادمین", f"user:view:{admin.id}")],
                        [("🏦 تنظیم حد منفی کیف پول", f"user:creditlimit:{admin.id}")],
                        [("🔙 ادمین‌ها", "catalog:admins:1")],
                    ),
                )
                return
            product = db.get(Product, int(parts[3]))
            if not product:
                return
            assignment = db.scalar(
                select(AdminPrice).where(
                    AdminPrice.admin_id == admin.id, AdminPrice.product_id == product.id
                )
            )
            if action == "adminprice":
                self.service.set_state(
                    db, call.from_user.id, "catalog_price", product_id=product.id, admin_id=admin.id
                )
                self._edit(
                    call,
                    f"💰 قیمت اختصاصی «{html.escape(product.title)}» برای {html.escape(admin.name)} را به تومان بفرستید.\n"
                    "با ذخیره قیمت، بسته برای همین ادمین فعال می‌شود.",
                    keyboards.inline(
                        [("❌ انصراف", f"catalog:assignment:{admin.id}:{product.id}")]
                    ),
                )
                return
            if action == "revoke" and assignment:
                db.delete(assignment)
                assignment = None
            status = f"✅ فعال — {toman(assignment.price)}" if assignment else "🔒 غیرفعال"
            rows = [
                [
                    (
                        "💰 فعال‌سازی / تغییر قیمت اختصاصی",
                        f"catalog:adminprice:{admin.id}:{product.id}",
                    )
                ]
            ]
            if assignment:
                rows.append(
                    [("⛔ غیرفعال‌سازی برای این ادمین", f"catalog:revoke:{admin.id}:{product.id}")]
                )
            rows.append([("🔙 بسته‌های ادمین", f"catalog:assignments:{admin.id}:1")])
            self._edit(
                call,
                f"🧑‍💼 {html.escape(admin.name)}\n📦 {html.escape(product.title)}\n"
                f"💳 قیمت عمومی: {toman(product.price)}\nقیمت ادمین: {status}\n\n"
                "این قیمت فقط برای همین ادمین اعمال می‌شود.",
                keyboards.inline(*rows),
            )

    def _catalog_price_message(self, db, message, payload):
        root = self.service.get_user(db, message.from_user.id)
        if not root or not root.is_root:
            return
        raw = message.text.strip().replace(",", "").replace("٬", "")
        if not raw.isdecimal() or not 0 <= int(raw) <= 10**12:
            self.bot.reply_to(message, "مبلغ معتبر به تومان بفرستید (صفر یا بیشتر).")
            return
        product = db.get(Product, int(payload["product_id"]))
        if not product:
            return
        price = int(raw)
        admin_id = payload.get("admin_id")
        if admin_id:
            admin = db.get(User, int(admin_id))
            if not admin or admin.permission != int(Permission.ADMIN):
                self.bot.reply_to(message, "ادمین دیگر فعال نیست.")
                return
            assignment = db.scalar(
                select(AdminPrice).where(
                    AdminPrice.admin_id == admin.id, AdminPrice.product_id == product.id
                )
            )
            if assignment:
                assignment.price = price
            else:
                db.add(AdminPrice(admin_id=admin.id, product_id=product.id, price=price))
            callback = f"catalog:assignment:{admin.id}:{product.id}"
        else:
            product.price = price
            callback = f"catalog:plan:{product.id}"
        self.service.clear_state(db, message.from_user.id)
        self.bot.reply_to(
            message,
            "✅ قیمت ذخیره شد.",
            reply_markup=keyboards.inline([("🔙 جزئیات بسته", callback)]),
        )


    def _resell_callback(self, call, data):
        if not self._authorized(call.from_user.id):
            return
        with self.database.session() as db:
            admin = db.scalar(
                select(User).where(User.telegram_id == call.from_user.id).with_for_update()
            )
            state = self.service.get_state(db, call.from_user.id)
            payload = dict(state.data or {}) if state else {}
            parts = data.split(":")
            action = parts[1]
            if action in {"manage", "ban", "credentials", "recharge"}:
                link = db.scalar(
                    select(V2AccountLink).where(
                        V2AccountLink.v2_user_id == int(parts[2]),
                        V2AccountLink.user_id == admin.id,
                        V2AccountLink.admin_created.is_(True),
                    )
                )
                if not link:
                    raise ValueError("این اکانت متعلق به شما نیست")
                account = self.panel.get_user(link.v2_user_id)
                if is_archived(db, link.v2_user_id):
                    raise ValueError("اکانت بایگانی شده؛ ابتدا از بایگانی بازیابی کنید")
                if not account:
                    raise ValueError("اکانت پیدا نشد")
                if action == "recharge":
                    self.service.set_state(
                        db,
                        admin.telegram_id,
                        "admin_pick_type",
                        username=link.label,
                        recharge_id=link.v2_user_id,
                    )
                    self._driving_render(call, "pick")
                    return
                if action == "ban":
                    self.panel.set_banned(link.v2_user_id, not bool(account.get("banned")))
                    account = self.panel.get_user(link.v2_user_id)
                elif action == "credentials":
                    self.panel.renew_credentials(link.v2_user_id)
                self._edit(
                    call,
                    f"👤 {html.escape(link.label)}\n🚦 {'🔴 مسدود' if account.get('banned') else '🟢 آزاد'}",
                    keyboards.inline(
                        [("💳 تمدید / شارژ", f"resell:recharge:{link.v2_user_id}")],
                        [("🚫 مسدود / آزاد", f"resell:ban:{link.v2_user_id}")],
                        [("🔄 توکن و UUID جدید", f"resell:credentials:{link.v2_user_id}")],
                        [("🔗 لینک و QR", f"sub:view:{link.v2_user_id}")],
                        [("🔙 لیست مشتری‌ها", "admin:users:1")],
                    ),
                )
                return
            if not state:
                return
            if action == "plan":
                if state.state != "admin_pick_type":
                    return
                product = db.get(Product, int(parts[2]))
                if not product:
                    return
                self.service.admin_product_price(db, admin, product)
                payload["product_id"] = product.id
                self.service.set_state(db, admin.telegram_id, "resell_note", **payload)
                self._edit(
                    call,
                    "📝 یادداشت مشتری را بفرستید یا رد شوید.",
                    keyboards.inline([("⏭ رد شدن", "resell:skip")], [("❌ انصراف", "admin:home")]),
                )
                return
            product = db.get(Product, int(payload.get("product_id", 0)))
            if not product:
                return
            price = self.service.admin_product_price(db, admin, product)
            if action == "skip" and state.state == "resell_note":
                payload["note"] = ""
                self._resell_preview(db, admin, payload, call=call)
                return
            if action != "confirm" or state.state != "resell_confirm":
                return
            if price != payload.get("quoted_price"):
                self._resell_preview(db, admin, payload, call=call)
                return
            if not can_spend(db, admin, price):
                self._edit(
                    call, "موجودی یا سقف اعتبار کافی نیست.", keyboards.inline([("💳 کیف پول", "wallet:show")])
                )
                return
            order = self.service.create_order(
                db, admin, product.product_type, product_id=product.id
            )
            if payload.get("recharge_id"):
                link = db.scalar(
                    select(V2AccountLink)
                    .where(
                        V2AccountLink.v2_user_id == int(payload["recharge_id"]),
                        V2AccountLink.user_id == admin.id,
                        V2AccountLink.admin_created.is_(True),
                    )
                    .with_for_update()
                )
                if not link:
                    raise ValueError("این اکانت متعلق به شما نیست")
                account = self.panel.recharge_subscription(
                    link.v2_user_id,
                    months=product.months,
                    data_gb=product.data_gb,
                    connections=product.connections,
                    time_based=product.product_type == ProductType.TIME,
                )
                order.v2_user_id = link.v2_user_id
                order.status = OrderStatus.PAID
                link.product_type = product.product_type
                link.renewal_months = product.months
                link.renewal_data_gb = product.data_gb
                link.renewal_connections = product.connections
                link.renewal_price = order.amount
            else:
                account = self.service.provision_order(db, order)
            admin.balance -= order.amount
            db.add(
                WalletTransaction(
                    user_id=admin.id,
                    amount=-order.amount,
                    kind="admin_purchase",
                    reference=f"admin-order:{order.id}",
                    note=product.title,
                )
            )
            db.flush()
            link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == account["id"]))
            link.admin_created = True
            if not payload.get("recharge_id"):
                origin = db.get(V2AccountOrigin, account["id"])
                if origin:
                    origin.kind = "managed"
            link.label = payload.get("username", product.title)[:100]
            if not payload.get("recharge_id"):
                self.panel.update_remarks(account["id"], payload.get("note", ""))
            self.service.clear_state(db, admin.telegram_id)
            brand = db.get(BotSetting, f"admin_brand:{admin.id}")
            active = db.get(BotSetting, f"admin_brand_active:{admin.id}")
            brand_url = None
            if (
                brand
                and (not active or active.value == "1")
                and self.settings.brand_subscription_base_url
            ):
                brand_url = f"{self.settings.brand_subscription_base_url}{account['token']}"
        self._send_account(
            call.message.chat.id,
            account,
            link.label,
            display_name=link.label,
            unlimited=product.product_type == ProductType.TIME,
            url_override=brand_url,
            reseller=True,
        )

    def _resell_preview(self, db, admin, payload, *, call=None, message=None):
        product = db.get(Product, int(payload["product_id"]))
        price = self.service.admin_product_price(db, admin, product)
        payload["quoted_price"] = price
        self.service.set_state(db, admin.telegram_id, "resell_confirm", **payload)
        text = (
            f"📦 {html.escape(product.title)}\n👤 {html.escape(payload['username'])}\n"
            f"📝 {html.escape(payload.get('note', ''))}\n💳 کسر از کیف پول: {toman(price)}\n"
            f"💰 موجودی پس از خرید: {toman(admin.balance - price)}\n"
            f"🏦 کف مجاز: {toman(wallet_floor(db, admin))}"
        )
        if payload.get("recharge_id"):
            text += "\n🔄 تمدید روی همین اکانت انجام و مصرف آن صفر می‌شود."
        markup = keyboards.inline(
            [("✅ تأیید و ساخت", "resell:confirm")], [("❌ انصراف", "admin:home")]
        )
        if call:
            self._edit(call, text, markup)
        else:
            self.bot.reply_to(message, text, parse_mode="HTML", reply_markup=markup)
