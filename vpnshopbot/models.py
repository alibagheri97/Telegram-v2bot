from __future__ import annotations

import enum
import secrets
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


class Permission(enum.IntEnum):
    USER = -1
    ROOT = 0
    ADMIN = 1


class UserStatus(str, enum.Enum):
    ACTIVE = "active"
    PENDING = "pending"
    BLOCKED = "blocked"


class OrderStatus(str, enum.Enum):
    DRAFT = "draft"
    AWAITING_PAYMENT = "awaiting_payment"
    PENDING_REVIEW = "pending_review"
    PAID = "paid"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class ProductType(str, enum.Enum):
    TIME = "time"
    DATA = "data"
    WALLET = "wallet"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, server_default=func.now(), onupdate=func.now()
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    # Compatibility snapshot requested by the owner; links are the source of truth.
    v2accounts: Mapped[list[int]] = mapped_column(JSON, default=list)
    permission: Mapped[int] = mapped_column(Integer, default=int(Permission.USER), index=True)
    balance: Mapped[int] = mapped_column(BigInteger, default=0)
    referral_code: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    referred_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus), default=UserStatus.ACTIVE, index=True
    )
    trial_notified_at: Mapped[datetime | None] = mapped_column(DateTime)

    accounts: Mapped[list[V2AccountLink]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    referred_by: Mapped[User | None] = relationship(remote_side="User.id")

    @staticmethod
    def new_referral_code() -> str:
        return secrets.token_urlsafe(8).replace("-", "").replace("_", "")[:10]

    @property
    def is_root(self) -> bool:
        return self.permission == int(Permission.ROOT)

    @property
    def is_admin(self) -> bool:
        return self.permission in (int(Permission.ROOT), int(Permission.ADMIN))


class V2AccountLink(TimestampMixin, Base):
    __tablename__ = "v2_account_links"
    __table_args__ = (UniqueConstraint("v2_user_id", name="uq_v2_link_v2_user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    v2_user_id: Mapped[int] = mapped_column(Integer, index=True)
    label: Mapped[str] = mapped_column(String(100), default="اشتراک")
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    auto_renewal: Mapped[bool] = mapped_column(Boolean, default=False)
    admin_created: Mapped[bool] = mapped_column(Boolean, default=False)
    renewal_months: Mapped[int | None] = mapped_column(Integer)
    renewal_data_gb: Mapped[int | None] = mapped_column(Integer)
    renewal_connections: Mapped[int | None] = mapped_column(Integer)
    renewal_price: Mapped[int | None] = mapped_column(BigInteger)

    user: Mapped[User] = relationship(back_populates="accounts")


class V2AccountOrigin(TimestampMixin, Base):
    """Immutable creator role snapshot: a later promotion must not change provenance."""
    __tablename__ = "v2_account_origins"

    v2_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    creator_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    creator_permission: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(32))


class ExpiryWatch(TimestampMixin, Base):
    __tablename__ = "expiry_watches"

    v2_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(String(120), default="")
    episode: Mapped[int] = mapped_column(Integer, default=0)
    was_expired: Mapped[bool] = mapped_column(Boolean, default=False)
    notified_episode: Mapped[int] = mapped_column(Integer, default=0)
    destination: Mapped[str] = mapped_column(String(120), default="")
    message_id: Mapped[int | None] = mapped_column(Integer)
    last_error: Mapped[str | None] = mapped_column(String(500))


class ExpiryRetention(TimestampMixin, Base):
    __tablename__ = "expiry_retention"

    v2_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signature: Mapped[str] = mapped_column(String(120), default="")
    expired_since: Mapped[int | None] = mapped_column(BigInteger)
    restored_at: Mapped[int | None] = mapped_column(BigInteger)


class V2Archive(TimestampMixin, Base):
    __tablename__ = "v2_archives"

    v2_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    email: Mapped[str] = mapped_column(String(255))
    label: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), index=True)
    generation: Mapped[int] = mapped_column(Integer, default=1)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    expired_since: Mapped[int] = mapped_column(BigInteger)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime)
    restored_at: Mapped[datetime | None] = mapped_column(DateTime)


class EmergencyAccount(TimestampMixin, Base):
    __tablename__ = "emergency_accounts"

    v2_user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    days_used: Mapped[int] = mapped_column(Integer, default=0)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    pending_event_id: Mapped[str | None] = mapped_column(String(32))


class EmergencyEvent(TimestampMixin, Base):
    """Durable intent allows recovery across the bot/panel database boundary."""
    __tablename__ = "emergency_events"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    v2_user_id: Mapped[int] = mapped_column(Integer, index=True)
    kind: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    expiry_before: Mapped[int] = mapped_column(BigInteger)
    expiry_after: Mapped[int] = mapped_column(BigInteger)
    transfer_enable: Mapped[int] = mapped_column(BigInteger)
    debt_before: Mapped[int] = mapped_column(Integer)
    debt_after: Mapped[int] = mapped_column(Integer)
    generation_before: Mapped[int] = mapped_column(Integer)
    days_added: Mapped[int] = mapped_column(Integer)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    announced: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    message_id: Mapped[int | None] = mapped_column(Integer)


class V2ManualPayment(TimestampMixin, Base):
    __tablename__ = "v2_manual_payments"

    id: Mapped[int] = mapped_column(primary_key=True)
    v2_user_id: Mapped[int] = mapped_column(Integer, index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    actor_telegram_id: Mapped[int] = mapped_column(BigInteger)


class ConversationState(TimestampMixin, Base):
    __tablename__ = "conversation_states"

    telegram_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    state: Mapped[str] = mapped_column(String(64), index=True)
    data: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Product(TimestampMixin, Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    title: Mapped[str] = mapped_column(String(100))
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    months: Mapped[int | None] = mapped_column(Integer)
    data_gb: Mapped[int | None] = mapped_column(Integer)
    connections: Mapped[int] = mapped_column(Integer, default=1)
    price: Mapped[int] = mapped_column(BigInteger)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AdminPrice(TimestampMixin, Base):
    __tablename__ = "admin_prices"
    __table_args__ = (UniqueConstraint("admin_id", "product_id", name="uq_admin_product"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    price: Mapped[int] = mapped_column(BigInteger)


class Discount(TimestampMixin, Base):
    __tablename__ = "discounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(100))
    product_id: Mapped[int | None] = mapped_column(ForeignKey("products.id"), index=True)
    percent: Mapped[int] = mapped_column(Integer)
    starts_at: Mapped[datetime] = mapped_column(DateTime)
    ends_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Order(TimestampMixin, Base):
    __tablename__ = "orders"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_type: Mapped[ProductType] = mapped_column(Enum(ProductType))
    months: Mapped[int | None] = mapped_column(Integer)
    data_gb: Mapped[int | None] = mapped_column(Integer)
    connections: Mapped[int | None] = mapped_column(Integer)
    amount: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[OrderStatus] = mapped_column(
        Enum(OrderStatus), default=OrderStatus.DRAFT, index=True
    )
    v2_user_id: Mapped[int | None] = mapped_column(Integer)
    description: Mapped[str | None] = mapped_column(String(255))


class PaymentReceipt(TimestampMixin, Base):
    __tablename__ = "payment_receipts"

    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    telegram_file_id: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(20), default="photo")
    review_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    review_message_id: Mapped[int | None] = mapped_column(BigInteger)
    reviewed_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    decision: Mapped[str | None] = mapped_column(String(20))
    decision_at: Mapped[datetime | None] = mapped_column(DateTime)


class WalletTransaction(TimestampMixin, Base):
    __tablename__ = "wallet_transactions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[int] = mapped_column(BigInteger)
    kind: Mapped[str] = mapped_column(String(40))
    reference: Mapped[str | None] = mapped_column(String(100), unique=True)
    note: Mapped[str | None] = mapped_column(String(255))


class BotSetting(TimestampMixin, Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class UserAction(Base):
    __tablename__ = "user_actions"
    __table_args__ = (Index("ix_user_actions_user_time", "user_id", "created_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    actor_telegram_id: Mapped[int | None] = mapped_column(BigInteger)
    action: Mapped[str] = mapped_column(String(80))
    title: Mapped[str] = mapped_column(String(160))
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_key: Mapped[str | None] = mapped_column(String(120), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


Index("ix_orders_user_status", Order.user_id, Order.status)
