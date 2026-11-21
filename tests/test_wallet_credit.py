import pytest
from sqlalchemy import select
from test_catalog import shop as shop  # noqa: PLC0414 -- shared fixture dependency
from test_user_admin import call, message
from test_user_admin import controls as controls  # noqa: PLC0414 -- shared fixture

from vpnshopbot.models import (
    AdminPrice,
    BotSetting,
    Order,
    Product,
    ProductType,
    User,
    V2AccountLink,
    WalletTransaction,
)
from vpnshopbot.scheduler import ShopScheduler
from vpnshopbot.wallet import can_spend, credit_key, wallet_floor


def set_credit(database, user_id=20, floor=-5000000, balance=0):
    with database.session() as db:
        db.get(User, user_id).balance = balance
        row = db.get(BotSetting, credit_key(user_id))
        if row:
            row.value = str(floor)
        else:
            db.add(BotSetting(key=credit_key(user_id), value=str(floor)))


def test_floor_applies_only_to_exact_admin_role(controls):
    _controller, database = controls
    for user_id in [10, 20, 40]:
        set_credit(database, user_id)
    set_credit(database, 30, floor=0)
    with database.session() as db:
        assert wallet_floor(db, db.get(User, 20)) == -5000000
        assert wallet_floor(db, db.get(User, 10)) == wallet_floor(db, db.get(User, 40)) == 0
        assert can_spend(db, db.get(User, 20), 5000000)
        assert not can_spend(db, db.get(User, 20), 5000001)
        assert not can_spend(db, db.get(User, 40), 1)
        assert not can_spend(db, db.get(User, 30), 1)


def test_root_sets_limit_with_confirmation_without_changing_balance(controls):
    controller, database = controls
    controller._user_action(call("user:creditlimit:20"), "user:creditlimit:20")
    controller._observe_message(message("-5000000"))
    with database.session() as db:
        assert wallet_floor(db, db.get(User, 20)) == 0
        state = controller.service.get_state(db, 10)
        nonce = state.data["nonce"]
    data = f"user:creditlimitapply:20:{nonce}"
    controller._user_action(call(data), data)
    with database.session() as db:
        assert wallet_floor(db, db.get(User, 20)) == -5000000
        assert db.get(User, 20).balance == 1000000
    with pytest.raises(ValueError, match="قدیمی"):
        controller._user_action(call(data), data)


def test_regular_user_has_no_limit_button_and_rejects_limit_callback(controls):
    controller, _ = controls
    controller._user_action(call("user:view:40"), "user:view:40")
    assert not any(
        "creditlimit" in button.callback_data
        for row in controller.screens[-1][1].keyboard
        for button in row
    )
    with pytest.raises(ValueError, match="فقط برای ادمین"):
        controller._user_action(call("user:creditlimit:40"), "user:creditlimit:40")


@pytest.mark.parametrize("starting_balance,allowed", [(-4915000, True), (-4915001, False)])
def test_reseller_purchase_honors_exact_floor_and_cannot_double_spend(
    controls, starting_balance, allowed
):
    controller, database = controls
    set_credit(database, balance=starting_balance)
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-20gb"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=85000))
        controller.service.set_state(
            db,
            20,
            "resell_confirm",
            product_id=product.id,
            quoted_price=85000,
            username="Credit customer",
            note="",
        )
    request = call("resell:confirm", actor=20)
    controller._resell_callback(request, "resell:confirm")
    controller._resell_callback(request, "resell:confirm")
    with database.session() as db:
        assert db.get(User, 20).balance == (
            starting_balance - 85000 if allowed else starting_balance
        )
        assert len(db.scalars(select(Order)).all()) == int(allowed)


def test_recharge_reduces_debt_and_can_restore_positive_balance(controls):
    controller, database = controls
    set_credit(database, balance=-1000000)
    with database.session() as db:
        admin = db.get(User, 20)
        order = controller.service.create_order(db, admin, ProductType.WALLET, amount=1500000)
        controller.service.provision_order(db, order)
    with database.session() as db:
        assert db.get(User, 20).balance == 500000
        assert db.scalar(select(WalletTransaction.amount)) == 1500000


def test_root_can_set_negative_admin_balance_only_within_floor(controls):
    controller, database = controls
    set_credit(database)
    controller._user_action(call("user:wallet:20"), "user:wallet:20")
    controller._observe_message(message("=-5000000"))
    controller._user_action(call("user:walletapply:20"), "user:walletapply:20")
    with database.session() as db:
        assert db.get(User, 20).balance == -5000000


def test_lowering_credit_requires_existing_debt_to_fit(controls):
    controller, database = controls
    set_credit(database, balance=-2000000)
    controller._user_action(call("user:creditlimit:20"), "user:creditlimit:20")
    controller._observe_message(message("0"))
    with database.session() as db:
        assert wallet_floor(db, db.get(User, 20)) == -5000000
        assert controller.service.get_state(db, 10).state == "root_user_creditlimit"


def test_wallet_confirmation_rechecks_reduced_floor(controls):
    controller, database = controls
    set_credit(database)
    controller._user_action(call("user:wallet:20"), "user:wallet:20")
    controller._observe_message(message("=-1000000"))
    set_credit(database, floor=0)
    with pytest.raises(ValueError, match="خارج از حد"):
        controller._user_action(call("user:walletapply:20"), "user:walletapply:20")


def test_indebted_admin_cannot_become_user_or_root(controls):
    controller, database = controls
    set_credit(database, balance=-1000)
    for data in ["user:role:20", "user:confirmroot:20"]:
        with pytest.raises(ValueError, match="تسویه"):
            controller._user_action(call(data), data)
    with database.session() as db:
        assert db.get(User, 20).permission == 1


@pytest.mark.parametrize("owner_id,allowed", [(20, True), (40, False)])
def test_auto_renewal_respects_admin_credit_and_stops_at_floor(controls, owner_id, allowed):
    controller, database = controls
    set_credit(database, user_id=owner_id, floor=-100000)
    with database.session() as db:
        product = db.scalar(select(Product).where(Product.code == "data-10gb"))
        db.add(AdminPrice(admin_id=20, product_id=product.id, price=85000))
        db.add(
            V2AccountLink(
                user_id=owner_id,
                v2_user_id=99,
                label="Credit customer",
                product_type=ProductType.DATA,
                admin_created=allowed,
                auto_renewal=True,
                renewal_price=85000,
                renewal_data_gb=10,
                renewal_connections=1,
            )
        )
    resets = []
    controller.panel.get_user = lambda _: {"expired_at": 0, "transfer_enable": 10, "u": 10, "d": 0}
    controller.panel.reset_data_subscription = lambda account_id, gb: resets.append(
        (account_id, gb)
    )
    scheduler = ShopScheduler.__new__(ShopScheduler)
    scheduler.database, scheduler.panel = database, controller.panel
    scheduler.service, scheduler.bot = controller.service, controller.bot
    scheduler.auto_renew()
    scheduler.auto_renew()
    assert resets == ([(99, 10)] if allowed else [])
    with database.session() as db:
        assert db.get(User, owner_id).balance == (-85000 if allowed else 0)
        assert len(db.scalars(select(WalletTransaction)).all()) == int(allowed)
