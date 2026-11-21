from __future__ import annotations

import io
import math
from datetime import UTC, datetime
from typing import Any

import qrcode
from sqlalchemy import select
from sqlalchemy.orm import Session

from .audit import record_action
from .config import Settings
from .models import (
    AdminPrice,
    BotSetting,
    ConversationState,
    Discount,
    Order,
    OrderStatus,
    Permission,
    Product,
    ProductType,
    User,
    UserStatus,
    V2AccountLink,
    WalletTransaction,
)
from .panel import PanelRepository

TIME_PRICES = {
    (1, 1): 450_000,
    (1, 3): 1_000_000,
    (1, 6): 1_500_000,
    (2, 1): 650_000,
    (2, 3): 1_400_000,
    (2, 6): 2_000_000,
}
DATA_PRICE_PER_GB = 20_000
UNRESOLVED_ORDER_STATUSES = (
    OrderStatus.AWAITING_PAYMENT,
    OrderStatus.PENDING_REVIEW,
    OrderStatus.REJECTED,
)


def toman(value: int) -> str:
    return f"{value:,} تومان"


def price_for_time(connections: int, months: int) -> int:
    try:
        return TIME_PRICES[(connections, months)]
    except KeyError as exc:
        raise ValueError("پلن زمانی نامعتبر است") from exc


def price_for_data(gigabytes: int) -> int:
    if gigabytes < 10 or gigabytes > 50 or gigabytes % 5:
        raise ValueError("حجم باید بین ۱۰ تا ۵۰ گیگابایت و مضرب ۵ باشد")
    return gigabytes * DATA_PRICE_PER_GB


def generate_qr(url: str) -> io.BytesIO:
    image = qrcode.make(url)
    output = io.BytesIO()
    output.name = "subscription.png"
    image.save(output, format="PNG")
    output.seek(0)
    return output


class ShopService:
    def __init__(self, settings: Settings, panel: PanelRepository):
        self.settings = settings
        self.panel = panel

    def seed_defaults(self, db: Session) -> None:
        for (connections, months), price in TIME_PRICES.items():
            code = f"time-{connections}c-{months}m"
            if not db.scalar(select(Product).where(Product.code == code)):
                db.add(
                    Product(
                        code=code,
                        title=f"نامحدود {months} ماهه - {connections} اتصال",
                        product_type=ProductType.TIME,
                        months=months,
                        data_gb=None,
                        connections=connections,
                        price=price,
                    )
                )
        for gigabytes in range(10, 51, 5):
            code = f"data-{gigabytes}gb"
            if not db.scalar(select(Product).where(Product.code == code)):
                db.add(
                    Product(
                        code=code,
                        title=f"حجمی {gigabytes} گیگابایت",
                        product_type=ProductType.DATA,
                        months=None,
                        data_gb=gigabytes,
                        connections=1,
                        price=gigabytes * DATA_PRICE_PER_GB,
                    )
                )
        for key, value in {
            "referral_reward": "50000",
            "data_price_per_gb": str(DATA_PRICE_PER_GB),
        }.items():
            if not db.get(BotSetting, key):
                db.add(BotSetting(key=key, value=value))

    def get_user(self, db: Session, telegram_id: int) -> User | None:
        return db.scalar(select(User).where(User.telegram_id == telegram_id))

    def ensure_root(self, db: Session, telegram_id: int, name: str) -> User | None:
        if telegram_id not in self.settings.root_telegram_ids:
            return self.get_user(db, telegram_id)
        user = self.get_user(db, telegram_id)
        if user:
            user.permission = int(Permission.ROOT)
            user.status = UserStatus.ACTIVE
            return user
        user = User(
            telegram_id=telegram_id,
            name=name[:100] or "مدیر اصلی",
            permission=int(Permission.ROOT),
            referral_code=User.new_referral_code(),
            status=UserStatus.ACTIVE,
            v2accounts=[],
        )
        db.add(user)
        db.flush()
        return user

    def set_state(self, db: Session, telegram_id: int, state: str, **data: Any) -> None:
        record = db.get(ConversationState, telegram_id)
        if record:
            record.state = state
            record.data = data
        else:
            db.add(ConversationState(telegram_id=telegram_id, state=state, data=data))

    def get_state(self, db: Session, telegram_id: int) -> ConversationState | None:
        return db.get(ConversationState, telegram_id)

    def clear_state(self, db: Session, telegram_id: int) -> None:
        record = db.get(ConversationState, telegram_id)
        if record:
            db.delete(record)

    def get_unresolved_order(self, db: Session, user_id: int) -> Order | None:
        return db.scalar(
            select(Order)
            .where(
                Order.user_id == user_id,
                Order.status.in_(UNRESOLVED_ORDER_STATUSES),
            )
            .order_by(Order.id.asc())
        )

    def register_user(
        self,
        db: Session,
        *,
        telegram_id: int,
        name: str,
        referral_code: str | None,
    ) -> tuple[User, dict[str, Any]]:
        if self.get_user(db, telegram_id):
            raise ValueError("این حساب قبلاً ساخته شده است")
        referrer = None
        if referral_code:
            referrer = db.scalar(
                select(User).where(User.referral_code == referral_code).with_for_update()
            )
        account = self.panel.create_account(
            email_prefix=f"tg{telegram_id}",
            telegram_id=telegram_id,
            transfer_gb=self.settings.trial_transfer_gb,
            days=self.settings.trial_days,
            device_limit=1,
            plan_id=self.settings.trial_plan_id,
            group_id=self.settings.trial_group_id,
            remarks=f"Telegram trial for {name[:80]}",
        )
        user = User(
            telegram_id=telegram_id,
            name=name.strip()[:100],
            referral_code=User.new_referral_code(),
            referred_by_id=referrer.id if referrer else None,
            status=UserStatus.ACTIVE,
            v2accounts=[account["id"]],
        )
        db.add(user)
        db.flush()
        db.add(
            V2AccountLink(
                user_id=user.id,
                v2_user_id=account["id"],
                label="اشتراک آزمایشی یک‌روزه",
                product_type=ProductType.TIME,
                renewal_months=None,
                renewal_data_gb=None,
                renewal_connections=1,
                renewal_price=None,
            )
        )
        if referrer:
            reward_setting = db.get(BotSetting, "referral_reward")
            reward = int(reward_setting.value if reward_setting else 0)
            referrer.balance += reward
            db.add(
                WalletTransaction(
                    user_id=referrer.id,
                    amount=reward,
                    kind="referral",
                    reference=f"referral:{user.id}",
                    note=f"دعوت {user.name}",
                )
            )
        self.clear_state(db, telegram_id)
        record_action(
            db,
            user.id,
            "user.registered",
            "🎁 ثبت‌نام و اشتراک آزمایشی",
            actor_id=telegram_id,
            source_key=f"user:registered:{user.id}",
            details={"name": user.name, "v2_user_id": account["id"]},
        )
        return user, account

    def create_order(
        self,
        db: Session,
        user: User,
        product_type: ProductType,
        *,
        months: int | None = None,
        data_gb: int | None = None,
        connections: int | None = None,
        amount: int | None = None,
        product_id: int | None = None,
    ) -> Order:
        # Lock the owner row so two simultaneous callbacks cannot create two open orders.
        locked_user = db.scalar(
            select(User)
            .where(User.id == user.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not locked_user or locked_user.status != UserStatus.ACTIVE:
            raise ValueError("حساب شما غیرفعال است")
        unresolved = self.get_unresolved_order(db, user.id)
        if unresolved:
            raise ValueError(f"سفارش شماره {unresolved.id} هنوز از نظر مالی تعیین تکلیف نشده است")
        if product_id is not None:
            product = db.get(Product, product_id)
            if not product or product.product_type == ProductType.WALLET:
                raise ValueError("بسته معتبر نیست")
            product_type = product.product_type
            months, data_gb, connections = product.months, product.data_gb, product.connections
            amount = self._product_price(db, user, product.code, product.price)
        elif product_type == ProductType.TIME:
            amount = self.quote_time(db, user, int(connections or 0), int(months or 0))
        elif product_type == ProductType.DATA:
            amount = self.quote_data(db, user, int(data_gb or 0))
        elif not amount or amount < 10_000:
            raise ValueError("حداقل افزایش موجودی ۱۰٬۰۰۰ تومان است")
        order = Order(
            user_id=user.id,
            product_type=product_type,
            months=months,
            data_gb=data_gb,
            connections=connections,
            amount=int(amount),
            status=OrderStatus.AWAITING_PAYMENT,
        )
        db.add(order)
        db.flush()
        record_action(
            db,
            user.id,
            "order.created",
            "🛒 ثبت سفارش",
            actor_id=user.telegram_id,
            source_key=f"order:created:{order.id}",
            details={
                "order_id": order.id,
                "amount": order.amount,
                "type": order.product_type.value,
                "months": months,
                "data_gb": data_gb,
                "connections": connections,
            },
        )
        return order

    def _product_price(self, db: Session, user: User, code: str, fallback: int) -> int:
        product = db.scalar(select(Product).where(Product.code == code))
        if not product:
            raise ValueError("بسته پیدا نشد")
        if user.permission == int(Permission.ADMIN):
            return self.admin_product_price(db, user, product)
        if not product.enabled:
            raise ValueError("فروش این بسته غیرفعال است")
        price = product.price
        now = datetime.now(UTC)
        discount = db.scalar(
            select(Discount)
            .where(
                Discount.enabled.is_(True),
                Discount.starts_at <= now,
                Discount.ends_at >= now,
                (Discount.product_id == product.id) | Discount.product_id.is_(None),
            )
            .order_by(Discount.percent.desc())
        )
        return price * (100 - discount.percent) // 100 if discount else price

    def admin_product_price(self, db: Session, admin: User, product: Product) -> int:
        if not admin.is_admin or admin.status != UserStatus.ACTIVE:
            raise ValueError("نمایندگی فعال نیست")
        assignment = db.scalar(
            select(AdminPrice)
            .where(AdminPrice.admin_id == admin.id, AdminPrice.product_id == product.id)
            .with_for_update()
        )
        if not assignment:
            raise ValueError("این بسته برای شما فعال نشده است")
        # Reseller prices are independent of public prices and public discount events.
        return assignment.price

    def enabled_admin_products(self, db: Session, admin_id: int) -> list[Product]:
        return list(
            db.scalars(
                select(Product)
                .join(AdminPrice, AdminPrice.product_id == Product.id)
                .where(AdminPrice.admin_id == admin_id)
                .order_by(Product.id)
            )
        )

    def quote_time(self, db: Session, user: User, connections: int, months: int) -> int:
        fallback = price_for_time(connections, months)
        return self._product_price(db, user, f"time-{connections}c-{months}m", fallback)

    def quote_data(self, db: Session, user: User, gigabytes: int) -> int:
        fallback = price_for_data(gigabytes)
        return self._product_price(db, user, f"data-{gigabytes}gb", fallback)

    def provision_order(self, db: Session, order: Order) -> dict[str, Any] | None:
        if order.status == OrderStatus.PAID:
            return self.panel.get_user(order.v2_user_id) if order.v2_user_id else None
        user = db.scalar(
            select(User)
            .where(User.id == order.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if not user:
            raise ValueError("کاربر سفارش پیدا نشد")
        if order.product_type == ProductType.WALLET:
            user.balance += order.amount
            db.add(
                WalletTransaction(
                    user_id=user.id,
                    amount=order.amount,
                    kind="deposit",
                    reference=f"order:{order.id}",
                    note="افزایش موجودی با کارت‌به‌کارت",
                )
            )
            order.status = OrderStatus.PAID
            record_action(
                db,
                user.id,
                "order.paid",
                "✅ تأیید پرداخت و شارژ کیف پول",
                source_key=f"order:paid:{order.id}",
                details={"order_id": order.id, "amount": order.amount},
            )
            return None
        is_time = order.product_type == ProductType.TIME
        account = self.panel.create_account(
            email_prefix=f"tg{user.telegram_id}",
            telegram_id=user.telegram_id,
            transfer_gb=1_000_000 if is_time else int(order.data_gb or 0),
            days=(int(order.months or 0) * 30) if is_time else None,
            device_limit=order.connections if is_time else None,
            plan_id=None,
            group_id=self.settings.trial_group_id,
            remarks=f"Telegram order #{order.id} - {user.name}",
        )
        from .models import V2AccountOrigin

        db.add(V2AccountOrigin(v2_user_id=account["id"], creator_user_id=user.id,
                               creator_permission=user.permission, kind="purchase"))
        order.v2_user_id = account["id"]
        order.status = OrderStatus.PAID
        user.v2accounts = [*list(user.v2accounts or []), account["id"]]
        db.add(
            V2AccountLink(
                user_id=user.id,
                v2_user_id=account["id"],
                label=(
                    f"نامحدود {order.months} ماهه - {order.connections} اتصال"
                    if is_time
                    else f"حجمی {order.data_gb} گیگابایت"
                ),
                product_type=order.product_type,
                renewal_months=order.months,
                renewal_data_gb=order.data_gb,
                renewal_connections=order.connections,
                renewal_price=order.amount,
            )
        )
        record_action(
            db,
            user.id,
            "order.paid",
            "✅ تأیید پرداخت و تحویل اشتراک",
            source_key=f"order:paid:{order.id}",
            details={"order_id": order.id, "amount": order.amount, "v2_user_id": account["id"]},
        )
        return account

    def order_summary(self, order: Order) -> str:
        if order.product_type == ProductType.TIME:
            details = (
                "♾️ نوع اشتراک: نامحدود زمانی\n"
                f"🗓 مدت: {order.months} ماه\n"
                f"👥 نوع اکانت: {order.connections} کاربره\n"
                "📊 محدودیت حجم: نامحدود"
            )
        elif order.product_type == ProductType.DATA:
            details = (
                f"📦 نوع اشتراک: حجمی\n⏳ مدت: بدون محدودیت زمانی\n📊 حجم: {order.data_gb} گیگابایت"
            )
        else:
            details = "💳 نوع سفارش: افزایش موجودی کیف پول"
        return f"{details}\n\n💰 مبلغ: {toman(order.amount)}"

    @staticmethod
    def total_pages(total: int, page_size: int = 8) -> int:
        return max(1, math.ceil(total / page_size))
