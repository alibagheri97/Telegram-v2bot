from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from vpnshopbot.db import Base
from vpnshopbot.models import (
    AdminPrice,
    Discount,
    OrderStatus,
    Permission,
    Product,
    ProductType,
    User,
)
from vpnshopbot.services import ShopService


def make_service() -> ShopService:
    settings = SimpleNamespace(root_telegram_ids=[], trial_days=1)
    return ShopService(settings, panel=SimpleNamespace())


def make_user(telegram_id: int = 100, permission: int = -1) -> User:
    return User(
        telegram_id=telegram_id,
        name="کاربر تست",
        referral_code=f"ref{telegram_id}",
        permission=permission,
        v2accounts=[],
    )


def test_seed_and_dynamic_product_price() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = make_service()
    with Session(engine) as db:
        service.seed_defaults(db)
        user = make_user()
        db.add(user)
        db.commit()
        product = db.scalar(select(Product).where(Product.code == "time-1c-1m"))
        assert product is not None
        product.price = 475_000
        assert service.quote_time(db, user, 1, 1) == 475_000
        assert db.scalar(select(Product).where(Product.code == "data-50gb")) is not None


def test_admin_specific_price_wins() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = make_service()
    with Session(engine) as db:
        service.seed_defaults(db)
        admin = make_user(permission=int(Permission.ADMIN))
        db.add(admin)
        db.flush()
        product = db.scalar(select(Product).where(Product.code == "time-1c-1m"))
        db.add(AdminPrice(admin_id=admin.id, product_id=product.id, price=300_000))
        db.flush()
        assert service.quote_time(db, admin, 1, 1) == 300_000
        order = service.create_order(
            db,
            admin,
            ProductType.TIME,
            months=1,
            connections=1,
        )
        assert order.amount == 300_000


def test_active_discount_is_applied() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = make_service()
    with Session(engine) as db:
        service.seed_defaults(db)
        user = make_user()
        db.add(user)
        db.flush()
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        now = datetime.now(UTC)
        db.add(
            Discount(
                title="جشنواره",
                product_id=product.id,
                percent=25,
                starts_at=now - timedelta(minutes=1),
                ends_at=now + timedelta(hours=1),
            )
        )
        db.flush()
        assert service.quote_data(db, user, 10) == 150_000


def test_state_survives_new_session() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = make_service()
    with Session(engine) as db:
        service.set_state(db, 123, "await_name", referral="abc")
        db.commit()
    with Session(engine) as db:
        state = service.get_state(db, 123)
        assert state.state == "await_name"
        assert state.data == {"referral": "abc"}


def test_user_cannot_create_second_order_until_first_is_resolved() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    service = make_service()
    with Session(engine) as db:
        service.seed_defaults(db)
        user = make_user()
        db.add(user)
        db.flush()
        first = service.create_order(db, user, ProductType.DATA, data_gb=10)
        with pytest.raises(ValueError, match="تعیین تکلیف نشده"):
            service.create_order(db, user, ProductType.TIME, months=1, connections=1)
        first.status = OrderStatus.CANCELLED
        second = service.create_order(db, user, ProductType.TIME, months=1, connections=1)
        assert second.id != first.id


def test_final_invoice_uses_icons_for_every_field() -> None:
    service = make_service()
    order = SimpleNamespace(
        product_type=ProductType.TIME,
        months=3,
        connections=2,
        amount=1_400_000,
    )
    summary = service.order_summary(order)
    assert all(icon in summary for icon in ["♾️", "🗓", "👥", "📊", "💰"])
