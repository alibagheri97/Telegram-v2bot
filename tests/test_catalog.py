from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from vpnshopbot.db import Base
from vpnshopbot.handlers import BotController
from vpnshopbot.models import (
    AdminPrice,
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
from vpnshopbot.services import ShopService


class MemoryDB:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions() as db, db.begin():
            yield db


class Panel:
    def __init__(self):
        self.created = []
        self.recharged = []

    def create_account(self, **kwargs):
        self.created.append(kwargs)
        return {"id": 500 + len(self.created), "token": "a" * 32}

    def update_remarks(self, *_args):
        return True

    def recharge_subscription(self, account_id, **kwargs):
        self.recharged.append((account_id, kwargs))
        return {"id": account_id, "token": "a" * 32}


@pytest.fixture
def shop():
    database = MemoryDB()
    panel = Panel()
    settings = SimpleNamespace(trial_group_id=2, brand_subscription_base_url="")
    service = ShopService(settings, panel)
    with database.session() as db:
        service.seed_defaults(db)
        for uid, permission in [
            (10, Permission.ROOT),
            (20, Permission.ADMIN),
            (30, Permission.ADMIN),
            (40, Permission.USER),
        ]:
            db.add(
                User(
                    id=uid,
                    telegram_id=uid,
                    name=f"User {uid}",
                    permission=int(permission),
                    referral_code=f"ref{uid}",
                    balance=1_000_000,
                    status=UserStatus.ACTIVE,
                    v2accounts=[],
                )
            )
    controller = BotController.__new__(BotController)
    controller.database, controller.service = database, service
    controller.settings, controller.panel = settings, panel
    controller.bot = SimpleNamespace(reply_to=lambda *args, **kwargs: None)
    controller._send_account = lambda *args, **kwargs: None
    controller._edit = lambda *args, **kwargs: None
    return database, panel, service, controller


def test_admin_plan_is_opt_in_and_prices_are_independent(shop):
    database, _panel, service, _controller = shop
    with database.session() as db:
        first, second, public = db.get(User, 20), db.get(User, 30), db.get(User, 40)
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        with pytest.raises(ValueError, match="فعال نشده"):
            service.admin_product_price(db, first, product)
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=70_000))
        db.add(AdminPrice(admin_id=30, product_id=product.id, price=90_000))
        db.flush()
        product.price = 250_000
        assert service.quote_data(db, first, 10) == 70_000
        assert service.quote_data(db, second, 10) == 90_000
        assert service.quote_data(db, public, 10) == 250_000


def test_revocation_removes_product_and_prevents_order(shop):
    database, panel, service, _controller = shop
    with database.session() as db:
        admin = db.get(User, 20)
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        assignment = AdminPrice(admin_id=20, product_id=product.id, price=50_000)
        db.add(assignment)
        db.flush()
        assert service.enabled_admin_products(db, 20) == [product]
        db.delete(assignment)
        db.flush()
        assert service.enabled_admin_products(db, 20) == []
        with pytest.raises(ValueError):
            service.create_order(db, admin, ProductType.DATA, product_id=product.id)
        assert admin.balance == 1_000_000
        assert panel.created == []


def test_reseller_confirmation_charges_exact_assigned_price_only_once(shop):
    database, panel, service, controller = shop
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-20gb"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=85_000))
        service.set_state(
            db,
            20,
            "resell_confirm",
            product_id=product.id,
            quoted_price=85_000,
            username="Customer",
            note="Customer note",
        )
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=20), message=SimpleNamespace(chat=SimpleNamespace(id=20))
    )
    controller._resell_callback(call, "resell:confirm")
    controller._resell_callback(call, "resell:confirm")
    with database.session() as db:
        assert db.get(User, 20).balance == 915_000
        assert db.get(User, 30).balance == 1_000_000
        orders = list(db.scalars(select(Order)))
        assert len(orders) == 1
        assert orders[0].status == OrderStatus.PAID
        assert orders[0].amount == 85_000
        assert db.scalar(select(WalletTransaction)).amount == -85_000
        link = db.scalar(select(V2AccountLink))
        assert link.user_id == 20 and link.admin_created
    assert len(panel.created) == 1
    assert panel.created[0]["transfer_gb"] == 20


def test_public_price_edit_does_not_change_admin_assignment(shop):
    database, _panel, _service, controller = shop
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "time-1c-1m"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=100_000))
        db.flush()
        controller._catalog_price_message(
            db,
            SimpleNamespace(text="600000", from_user=SimpleNamespace(id=10)),
            {"product_id": product.id},
        )
        assert product.price == 600_000
        assert db.scalar(select(AdminPrice)).price == 100_000


def test_changed_admin_price_requires_confirmation_again(shop):
    database, panel, service, controller = shop
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-20gb"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=95_000))
        service.set_state(
            db,
            20,
            "resell_confirm",
            product_id=product.id,
            quoted_price=85_000,
            username="Customer",
            note="",
        )
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=20), message=SimpleNamespace(chat=SimpleNamespace(id=20))
    )
    controller._resell_callback(call, "resell:confirm")
    with database.session() as db:
        assert db.get(User, 20).balance == 1_000_000
        assert db.scalar(select(Order)) is None
        assert service.get_state(db, 20).data["quoted_price"] == 95_000
    assert panel.created == []


def test_admin_cannot_manage_another_admins_account(shop):
    database, panel, _service, controller = shop
    with database.session() as db:
        db.add(
            V2AccountLink(
                user_id=30,
                v2_user_id=123,
                label="Foreign",
                product_type=ProductType.DATA,
                admin_created=True,
            )
        )
    call = SimpleNamespace(from_user=SimpleNamespace(id=20))
    with pytest.raises(ValueError, match="متعلق به شما نیست"):
        controller._resell_callback(call, "resell:ban:123")
    assert panel.created == []


def test_admin_price_edit_is_scoped_to_selected_admin(shop):
    database, _panel, _service, controller = shop
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        db.add(AdminPrice(admin_id=30, product_id=product.id, price=90_000))
        db.flush()
        controller._catalog_price_message(
            db,
            SimpleNamespace(text="60000", from_user=SimpleNamespace(id=10)),
            {"product_id": product.id, "admin_id": 20},
        )
        db.flush()
        prices = {p.admin_id: p.price for p in db.scalars(select(AdminPrice))}
        assert prices == {20: 60_000, 30: 90_000}
        assert product.price == 200_000


def test_recharge_keeps_same_account_and_charges_assigned_price(shop):
    database, panel, service, controller = shop
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "time-2c-3m"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=320_000))
        db.add(
            V2AccountLink(
                user_id=20,
                v2_user_id=123,
                label="Customer",
                product_type=ProductType.DATA,
                admin_created=True,
            )
        )
        service.set_state(
            db,
            20,
            "resell_confirm",
            product_id=product.id,
            quoted_price=320_000,
            username="Customer",
            note="",
            recharge_id=123,
        )
    call = SimpleNamespace(
        from_user=SimpleNamespace(id=20), message=SimpleNamespace(chat=SimpleNamespace(id=20))
    )
    controller._resell_callback(call, "resell:confirm")
    with database.session() as db:
        assert db.get(User, 20).balance == 680_000
        order = db.scalar(select(Order))
        assert order.v2_user_id == 123 and order.status == OrderStatus.PAID
        assert len(list(db.scalars(select(V2AccountLink)))) == 1
        assert db.scalar(select(V2AccountLink)).renewal_price == 320_000
    assert panel.created == []
    assert panel.recharged == [
        (
            123,
            {
                "months": 3,
                "data_gb": None,
                "connections": 2,
                "time_based": True,
            },
        )
    ]
