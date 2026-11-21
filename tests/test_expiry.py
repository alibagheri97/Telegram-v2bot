import time
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from test_catalog import shop as shop  # noqa: PLC0414 -- shared database fixture

from vpnshopbot.expiry import ExpiryService, account_expiration, account_signature, save_setting
from vpnshopbot.models import (
    BotSetting,
    ExpiryWatch,
    ProductType,
    User,
    V2AccountLink,
    V2AccountOrigin,
)
from vpnshopbot.panel import GIB, PanelRepository


@pytest.fixture
def expiry(shop):
    database, panel, service, controller = shop
    accounts, messages, failures = {}, [], set()
    panel.get_user = lambda account_id: accounts.get(account_id)
    panel.subscription_url = lambda account: f"https://panel.test/subscribe?token=token{account['id']}"

    def archive_if_unchanged(account_id, snapshot):
        from vpnshopbot.expiry import account_expiration
        account = accounts.get(account_id)
        if not account or not account_expiration(account) or any(
            int(account.get(key) or 0) != int(snapshot.get(key) or 0)
            for key in ("expired_at", "transfer_enable", "u", "d")
        ):
            return False
        account["banned"] = 1
        return True

    panel.archive_if_unchanged = archive_if_unchanged

    def apply_operation(account_id, *, before, after, transfer_enable, monthly):
        account = accounts[account_id]
        if account["expired_at"] == after and before != after:
            return
        if account["expired_at"] != before or account["transfer_enable"] != transfer_enable:
            raise ValueError("panel conflict")
        account["expired_at"] = after
        if monthly:
            account.update(u=0, d=0, banned=0)

    panel.apply_emergency_operation = apply_operation

    def send(chat_id, text, **kwargs):
        if chat_id in failures:
            raise RuntimeError("test delivery failure")
        messages.append((chat_id, text, kwargs))
        return SimpleNamespace(message_id=len(messages), chat=SimpleNamespace(id=chat_id))

    controller.bot = SimpleNamespace(
        send_message=send,
        get_me=lambda: SimpleNamespace(username="test_vpn_bot"),
        answer_callback_query=lambda *args, **kwargs: None,
    )
    controller.screens = []
    controller._edit = lambda call, text, markup: controller.screens.append((text, markup))
    worker = ExpiryService(controller.bot, database, panel)

    def link(
        account_id, owner=40, *, managed=False, creator_role=None, kind="managed", **overrides
    ):
        accounts[account_id] = {
            "id": account_id,
            "email": f"person{account_id}@example.test",
            "is_admin": 0,
            "expired_at": int(time.time()) - 60,
            "transfer_enable": 10 * GIB,
            "u": GIB,
            "d": GIB,
            "banned": 0,
            **overrides,
        }
        with database.session() as db:
            db.add(
                V2AccountLink(
                    user_id=owner,
                    v2_user_id=account_id,
                    label=f"Account {account_id}",
                    product_type=ProductType.TIME,
                    admin_created=managed,
                )
            )
            if creator_role is not None:
                db.add(
                    V2AccountOrigin(
                        v2_user_id=account_id,
                        creator_user_id=owner,
                        creator_permission=creator_role,
                        kind=kind,
                    )
                )

    def configure(**values):
        with database.session() as db:
            for key, value in values.items():
                save_setting(db, key, value)

    configure(expired_group_id=-100123456, expired_topic_id=77)
    return SimpleNamespace(
        database=database,
        panel=panel,
        service=service,
        controller=controller,
        worker=worker,
        accounts=accounts,
        messages=messages,
        failures=failures,
        link=link,
        configure=configure,
    )


def callback(data, actor=10):
    return SimpleNamespace(
        id="test-callback",
        data=data,
        from_user=SimpleNamespace(id=actor),
        message=SimpleNamespace(message_id=1, chat=SimpleNamespace(id=actor, type="private")),
    )


def test_service_starts_paused_and_manual_scan_is_available(expiry):
    expiry.link(1)
    assert expiry.worker.run()["paused"]
    assert not expiry.messages
    assert expiry.worker.run(force=True)["sent"] == 1


def test_only_root_created_accounts_go_to_expired_topic(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.link(2, owner=20, managed=True, creator_role=1)
    expiry.link(3, owner=40)
    expiry.link(4, owner=10, managed=False, creator_role=0, kind="purchase")
    result = expiry.worker.run(force=True)
    assert result["sent"] == 4
    assert [message[0] for message in expiry.messages] == [-100123456, 20, 40, 10]
    root, admin, user, _ = expiry.messages
    assert root[2]["message_thread_id"] == 77
    root_buttons = [button for row in root[2]["reply_markup"].keyboard for button in row]
    assert root_buttons[0].callback_data == "expiry:renew:1:1:0"
    assert root_buttons[1].url == "https://t.me/test_vpn_bot?start=account_1"
    assert admin[2]["reply_markup"].keyboard[0][0].callback_data == "resell:recharge:2"
    assert user[2]["reply_markup"].keyboard[0][0].callback_data == "buy:start"
    assert "message_thread_id" not in user[2]


def test_admin_promotion_does_not_reclassify_old_accounts_as_root_created(expiry):
    expiry.link(1, owner=20, managed=True, creator_role=1)
    with expiry.database.session() as db:
        db.get(User, 20).permission = 0
    expiry.worker.run(force=True)
    assert [message[0] for message in expiry.messages] == [20]


def test_unknown_provenance_and_unlinked_accounts_never_go_to_topic(expiry):
    expiry.link(1, owner=10, managed=True)
    expiry.accounts[2] = dict(expiry.accounts[1], id=2)
    expiry.worker.run(force=True)
    assert [message[0] for message in expiry.messages] == [10]


def test_root_topic_is_required_but_customers_still_get_private_notices(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.link(2)
    expiry.configure(expired_group_id=0, expired_topic_id=0)
    result = expiry.worker.run(force=True)
    assert result["topic_pending"] == 1
    assert [message[0] for message in expiry.messages] == [40]


def test_dedup_survives_worker_recreation_and_renews_for_new_cycle(expiry):
    expiry.link(1)
    expiry.worker.run(force=True)
    ExpiryService(expiry.controller.bot, expiry.database, expiry.panel).run(force=True)
    assert len(expiry.messages) == 1
    expiry.accounts[1]["expired_at"] += 1
    expiry.worker.run(force=True)
    assert len(expiry.messages) == 2
    with expiry.database.session() as db:
        assert db.get(ExpiryWatch, 1).episode == 2


def test_data_reset_and_active_observation_rearm_notifications(expiry):
    expiry.link(1, expired_at=0, u=5 * GIB, d=5 * GIB)
    expiry.worker.run(force=True)
    expiry.accounts[1].update(u=0, d=0)
    expiry.worker.run(force=True)
    expiry.accounts[1].update(d=10 * GIB)
    expiry.worker.run(force=True)
    assert len(expiry.messages) == 2


@pytest.mark.parametrize(
    "account,expected",
    [
        ({"expired_at": 0, "transfer_enable": 0, "u": 100}, None),
        ({"expired_at": None, "transfer_enable": 0}, None),
        ({"expired_at": 100}, "پایان اعتبار زمانی"),
        ({"expired_at": 300, "transfer_enable": 10, "u": 5, "d": 5}, "اتمام حجم"),
        ({"expired_at": 300, "transfer_enable": 10, "u": 1, "d": 1}, None),
    ],
)
def test_expiration_handles_unlimited_and_data_exhaustion(account, expected):
    assert account_expiration(account, now=200) == expected


def test_delivery_failure_isolated_and_retried_without_resending_success(expiry):
    expiry.link(1, owner=20, managed=True, creator_role=1)
    expiry.link(2)
    expiry.failures.add(20)
    assert expiry.worker.run(force=True)["errors"] == 1
    assert [message[0] for message in expiry.messages] == [40]
    expiry.failures.clear()
    expiry.worker.run(force=True)
    assert [message[0] for message in expiry.messages] == [40, 20]


def test_blocked_owner_is_not_notified(expiry):
    from vpnshopbot.models import UserStatus

    expiry.link(1)
    with expiry.database.session() as db:
        db.get(User, 40).status = UserStatus.BLOCKED
    assert expiry.worker.run(force=True)["sent"] == 0


def test_topic_backlog_is_rate_limited_and_delivered_on_later_scan(expiry):
    for account_id in range(1, 7):
        expiry.link(account_id, owner=10, managed=True, creator_role=0)
    assert expiry.worker.run(force=True)["sent"] == 2
    assert expiry.worker.run(force=True)["sent"] == 2
    assert expiry.worker.run(force=True)["sent"] == 2
    assert expiry.worker.run(force=True)["sent"] == 0


def test_auto_renew_pause_prevents_wallet_and_panel_changes(expiry):
    from vpnshopbot.scheduler import ShopScheduler

    expiry.configure(**{"service:auto_renew:enabled": "0"})
    scheduler = ShopScheduler.__new__(ShopScheduler)
    scheduler.database, scheduler.panel = expiry.database, expiry.panel
    scheduler.auto_renew()
    assert not expiry.messages


def test_expired_list_is_root_only_and_opens_panel_account(expiry):
    expiry.link(1)
    expiry.worker.run(force=True)
    with expiry.database.session() as db:
        source = expiry.controller._driving_source(db, db.get(User, 10), "expired")
        view = expiry.controller._driving_load(db, db.get(User, 10), "expired", source)
        rows, total, _, _ = expiry.controller._driving_query(db, source, view)
        assert total == 1 and rows[0]["id"] == 1
        assert source.detail == "v2:view:{id}"
        with pytest.raises(ValueError, match="مدیر اصلی"):
            expiry.controller._driving_source(db, db.get(User, 20), "expired")


def test_service_controls_require_root_and_persist_pause(expiry):
    controller = expiry.controller
    with pytest.raises(ValueError, match="مدیر اصلی"):
        controller._expiry_callback(callback("expiry:toggle", actor=20), "expiry:toggle")
    controller._expiry_callback(callback("expiry:toggle"), "expiry:toggle")
    with expiry.database.session() as db:
        assert db.get(BotSetting, "service:expire:enabled").value == "1"
    controller._expiry_callback(callback("expiry:toggle"), "expiry:toggle")
    assert expiry.worker.run()["paused"]


def test_existing_customer_cannot_be_claimed_by_root(expiry):
    expiry.link(1)
    with pytest.raises(ValueError, match="از قبل ثبت"):
        expiry.controller._expiry_callback(
            callback("expiry:confirmclaim:1"), "expiry:confirmclaim:1"
        )
    with expiry.database.session() as db:
        assert db.scalar(select(V2AccountLink.user_id).where(V2AccountLink.v2_user_id == 1)) == 40
        assert db.get(V2AccountOrigin, 1) is None


def test_unlinked_panel_account_can_be_explicitly_registered_by_root(expiry):
    expiry.accounts[1] = {"id": 1, "email": "root-created@example.test", "expired_at": int(time.time()) - 60}
    expiry.controller._v2_action = lambda *args: None
    expiry.controller._expiry_callback(callback("expiry:confirmclaim:1"), "expiry:confirmclaim:1")
    with expiry.database.session() as db:
        assert db.get(V2AccountOrigin, 1).kind == "root_registered"
        assert db.get(V2AccountOrigin, 1).creator_permission == 0
    expiry.worker.run(force=True)
    assert [message[0] for message in expiry.messages] == [-100123456]


def test_renewal_is_root_only_and_rejects_customer_accounts(expiry):
    expiry.link(1)
    expiry.worker.run(force=True)
    with pytest.raises(ValueError, match="مدیر اصلی"):
        expiry.controller._expiry_callback(
            callback("expiry:renew:1:1", actor=20), "expiry:renew:1:1"
        )
    with pytest.raises(ValueError, match="فقط برای اکانت"):
        expiry.controller._expiry_callback(callback("expiry:renew:1:1"), "expiry:renew:1:1")


def test_root_renewal_cannot_replay_or_use_old_episode(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.worker.run(force=True)
    renewed = []

    original = expiry.panel.apply_emergency_operation

    def renew(account_id, **kwargs):
        renewed.append((kwargs["after"] - max(kwargs["before"], int(time.time()))) // 86400)
        original(account_id, **kwargs)

    expiry.panel.apply_emergency_operation = renew
    with pytest.raises(ValueError, match="قدیمی"):
        expiry.controller._expiry_callback(callback("expiry:renew:1:9"), "expiry:renew:1:9")
    expiry.controller._expiry_callback(callback("expiry:renew:1:1"), "expiry:renew:1:1")
    with pytest.raises(ValueError, match="قدیمی"):
        expiry.controller._expiry_callback(callback("expiry:renew:1:1"), "expiry:renew:1:1")
    assert renewed == [31]


def test_account_deeplink_checks_root_and_private_chat(expiry):
    controller = expiry.controller
    routed = []
    controller._v2_action = lambda call, data: routed.append(data)
    request = callback("")
    message = SimpleNamespace(from_user=request.from_user, chat=request.message.chat)
    with expiry.database.session() as db:
        assert controller._expiry_deeplink(message, db.get(User, 10), "account_17")
        assert controller._expiry_deeplink(message, db.get(User, 40), "account_17")
    assert routed == ["v2:view:17"]


def test_panel_renewal_is_atomic_31_days_and_refuses_active_or_stale_account():
    panel = PanelRepository.__new__(PanelRepository)
    panel.engine = create_engine("sqlite://")
    panel.users = Table(
        "vpn_users",
        MetaData(),
        Column("id", Integer, primary_key=True),
        *[
            Column(name, Integer)
            for name in ["expired_at", "transfer_enable", "u", "d", "banned", "updated_at"]
        ],
    )
    panel.users.create(panel.engine)
    now = int(time.time())
    with panel.engine.begin() as connection:
        connection.execute(
            panel.users.insert().values(
                id=1,
                expired_at=now - 10,
                transfer_enable=10 * GIB,
                u=GIB,
                d=GIB,
                banned=1,
                updated_at=now,
            )
        )
    signature = account_signature(panel.get_user(1))
    assert not panel.renew_expired(1, signature="stale")
    assert panel.renew_expired(1, signature=signature, days=31)
    account = panel.get_user(1)
    assert now + 31 * 86400 <= account["expired_at"] <= int(time.time()) + 31 * 86400
    assert account["u"] == account["d"] == account["banned"] == 0
    assert not panel.renew_expired(1, signature=account_signature(account), days=31)
