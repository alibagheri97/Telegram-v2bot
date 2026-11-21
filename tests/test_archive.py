import time

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from test_catalog import shop as shop  # noqa: PLC0414 -- fixture dependency
from test_driving import table_ui as table_ui  # noqa: PLC0414 -- SQL list fixture
from test_expiry import callback
from test_expiry import expiry as expiry  # noqa: PLC0414 -- shared fixture
from test_user_admin import message

from vpnshopbot.archive import ArchiveService, is_archived
from vpnshopbot.emergency import EmergencyService
from vpnshopbot.expiry import setting
from vpnshopbot.models import (
    EmergencyAccount,
    ExpiryRetention,
    User,
    UserAction,
    V2AccountLink,
    V2Archive,
    V2ManualPayment,
)
from vpnshopbot.panel import PanelRepository
from vpnshopbot.scheduler import ShopScheduler


def old_root(env, account_id=1):
    env.link(
        account_id,
        owner=10,
        managed=True,
        creator_role=0,
        expired_at=int(time.time()) - 3 * 86400 - 1,
    )
    return ArchiveService(env.database, env.panel)


def link_id(env, account_id=1):
    with env.database.session() as db:
        return db.scalar(select(V2AccountLink.id).where(V2AccountLink.v2_user_id == account_id))


def test_three_day_boundary_preserves_rows_payments_and_audit(expiry):
    service = old_root(expiry)
    expiry.accounts[1]["password"] = "not-for-snapshots"
    expiry.accounts[1]["token"] = "not-for-logs"
    with expiry.database.session() as db:
        db.add(V2ManualPayment(v2_user_id=1, amount=100, actor_telegram_id=10))
    expiry.accounts[1]["expired_at"] = int(time.time()) - 3 * 86400 + 5
    assert not service.check(link_id(expiry), 3)
    expiry.accounts[1]["expired_at"] -= 10
    assert service.check(link_id(expiry), 3)
    assert 1 in expiry.accounts and expiry.accounts[1]["banned"] == 1
    assert service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        row = db.get(V2Archive, 1)
        assert row.status == "archived"
        assert "password" not in row.snapshot and "token" not in row.snapshot
        assert db.scalar(select(V2AccountLink.user_id)) == 10
        assert db.scalar(select(V2ManualPayment.amount)) == 100
        actions = db.scalars(select(UserAction).where(UserAction.action == "v2.archived")).all()
        assert len(actions) == 1 and actions[0].user_id == 10


def test_normal_and_reseller_accounts_are_not_auto_archived(expiry):
    for account_id, owner, managed, role, kind in [
        (1, 40, False, -1, "purchase"),
        (2, 20, True, 1, "managed"),
        (3, 10, False, 0, "purchase"),
        (4, 10, True, None, "managed"),
    ]:
        expiry.link(
            account_id,
            owner=owner,
            managed=managed,
            creator_role=role,
            kind=kind,
            expired_at=int(time.time()) - 30 * 86400,
        )
        assert not ArchiveService(expiry.database, expiry.panel).check(
            link_id(expiry, account_id), 3
        )
    with expiry.database.session() as db:
        assert not db.scalars(select(V2Archive)).all()


def test_zero_disables_new_archiving(expiry):
    service = old_root(expiry)
    assert not service.check(link_id(expiry), 0)
    assert expiry.accounts[1]["banned"] == 0


def test_data_exhaustion_counts_from_first_observation_and_resets_on_recharge(expiry, monkeypatch):
    expiry.link(
        1, owner=10, managed=True, creator_role=0, expired_at=0, transfer_enable=10, u=10, d=0
    )
    now = [int(time.time())]
    monkeypatch.setattr("vpnshopbot.archive.time.time", lambda: now[0])
    service = ArchiveService(expiry.database, expiry.panel)
    assert not service.check(link_id(expiry), 3)
    now[0] += 2 * 86400
    assert not service.check(link_id(expiry), 3)
    expiry.accounts[1].update(u=0, d=0)
    assert not service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        assert db.get(ExpiryRetention, 1).expired_since is None
    expiry.accounts[1]["u"] = 10
    assert not service.check(link_id(expiry), 3)
    now[0] += 3 * 86400
    assert service.check(link_id(expiry), 3)


def test_time_recharge_before_deadline_prevents_archive(expiry):
    service = old_root(expiry)
    expiry.accounts[1]["expired_at"] = int(time.time()) + 86400
    assert not service.check(link_id(expiry), 3)
    assert expiry.accounts[1]["banned"] == 0


def test_recharge_race_is_rechecked_inside_panel_transaction(expiry):
    service = old_root(expiry)
    original = expiry.panel.archive_if_unchanged

    def racing(account_id, snapshot):
        expiry.accounts[account_id]["expired_at"] = int(time.time()) + 86400
        return original(account_id, snapshot)

    expiry.panel.archive_if_unchanged = racing
    assert not service.check(link_id(expiry), 3)
    assert expiry.accounts[1]["banned"] == 0
    with expiry.database.session() as db:
        assert not is_archived(db, 1)


@pytest.mark.parametrize("apply_before_crash", [False, True])
def test_pending_archive_recovers_once_after_restart(expiry, apply_before_crash):
    service = old_root(expiry)
    original = expiry.panel.archive_if_unchanged

    def interrupted(account_id, snapshot):
        if apply_before_crash:
            original(account_id, snapshot)
        raise RuntimeError("simulated restart")

    expiry.panel.archive_if_unchanged = interrupted
    with pytest.raises(RuntimeError):
        service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        assert db.get(V2Archive, 1).status == "pending"
    expiry.panel.archive_if_unchanged = original
    assert ArchiveService(expiry.database, expiry.panel).check(link_id(expiry), 3)
    with expiry.database.session() as db:
        assert db.get(V2Archive, 1).status == "archived"
        assert (
            len(db.scalars(select(UserAction).where(UserAction.action == "v2.archived")).all()) == 1
        )


def test_emergency_advances_and_pending_operations_prevent_archiving(expiry):
    service = old_root(expiry)
    with expiry.database.session() as db:
        db.add(
            EmergencyAccount(v2_user_id=1, days_used=0, generation=0, pending_event_id="pending")
        )
    assert not service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        db.get(EmergencyAccount, 1).pending_event_id = None
    assert EmergencyService(expiry.controller.bot, expiry.database, expiry.panel).operate(1)
    assert not service.check(link_id(expiry), 3)


def test_archived_cards_are_read_only_and_emergency_recharge_is_blocked(expiry):
    service = old_root(expiry)
    service.check(link_id(expiry), 3)
    for action in ["ban", "reset", "delete", "custom", "payment", "package", "addday"]:
        with pytest.raises(ValueError, match="بایگانی"):
            expiry.controller._v2_action(callback(f"v2:{action}:1"), f"v2:{action}:1")
    with pytest.raises(ValueError, match="بایگانی"):
        EmergencyService(expiry.controller.bot, expiry.database, expiry.panel).operate(
            1, monthly=True
        )
    assert EmergencyService(expiry.controller.bot, expiry.database, expiry.panel).operate(1) is None
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    text, markup = expiry.controller.screens[-1]
    assert "فقط مشاهده" in text
    assert all("v2:ban" not in button.callback_data for row in markup.keyboard for button in row)


def test_restore_retains_history_and_grants_fresh_archive_grace_not_service_time(expiry):
    service = old_root(expiry)
    service.check(link_id(expiry), 3)
    original_expiry = expiry.accounts[1]["expired_at"]
    expiry.controller._archive_callback(callback("archive:restore:1:1"), "archive:restore:1:1")
    assert expiry.accounts[1]["expired_at"] == original_expiry
    assert expiry.accounts[1]["banned"] == 1
    assert not service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        assert not is_archived(db, 1)
        assert (
            len(
                db.scalars(
                    select(UserAction).where(UserAction.action.in_(["v2.archived", "v2.restored"]))
                ).all()
            )
            == 2
        )
    with pytest.raises(ValueError, match="قدیمی"):
        expiry.controller._archive_callback(callback("archive:restore:1:1"), "archive:restore:1:1")


def test_archive_list_and_active_v2_list_are_separate_and_scoped(table_ui):
    controller, database = table_ui
    with database.session() as db:
        db.add(
            V2Archive(
                v2_user_id=1,
                user_id=1,
                email="archived@example.test",
                label="Old account",
                status="archived",
                generation=1,
                snapshot={},
                expired_since=1,
            )
        )
        db.flush()
    with database.session() as db:
        root = db.get(User, 1)
        source = controller._driving_source(db, root, "v2")
        view = controller._driving_load(db, root, "v2", source)
        rows, total, _, _ = controller._driving_query(db, source, view)
        assert total == 19 and all(row["id"] != 1 for row in rows)
        archive = controller._driving_source(db, root, "archives")
        view = controller._driving_load(db, root, "archives", archive)
        rows, total, _, _ = controller._driving_query(db, archive, view)
        assert total == 1 and rows[0]["id"] == 1
        with pytest.raises(ValueError, match="مدیر اصلی"):
            controller._driving_source(db, db.get(User, 2), "archives")


def test_service_archives_aged_accounts_even_without_a_topic(expiry):
    old_root(expiry)
    expiry.configure(expired_group_id=0, expired_topic_id=0)
    result = expiry.worker.run(force=True)
    assert result["archived"] == 1
    assert not expiry.messages


def test_grace_setting_is_root_only_and_confirmation_based(expiry):
    controller = expiry.controller
    controller.bot.reply_to = lambda *args, **kwargs: None
    with pytest.raises(ValueError, match="مدیر اصلی"):
        controller._archive_callback(callback("archive:grace", actor=20), "archive:grace")
    controller._archive_callback(callback("archive:grace"), "archive:grace")
    controller.text_message(message("7"))
    with expiry.database.session() as db:
        assert setting(db, "service:expire:archive_after_days", "3") == "3"
        nonce = controller.service.get_state(db, 10).data["nonce"]
    data = f"archive:gracesave:{nonce}"
    controller._archive_callback(callback(data), data)
    with expiry.database.session() as db:
        assert setting(db, "service:expire:archive_after_days") == "7"
    assert "7 روز" in controller.screens[-1][0]


def test_auto_renew_skips_archives_before_touching_panel(expiry):
    service = old_root(expiry)
    service.check(link_id(expiry), 3)
    with expiry.database.session() as db:
        link = db.scalar(select(V2AccountLink))
        link.auto_renewal, link.renewal_price = True, 100
    expiry.panel.get_user = lambda _: pytest.fail("archived accounts must not be renewed")
    scheduler = ShopScheduler.__new__(ShopScheduler)
    scheduler.database, scheduler.panel = expiry.database, expiry.panel
    scheduler.auto_renew()


def test_panel_soft_archive_blocks_without_deleting_and_rechecks_recharge():
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
    with panel.engine.begin() as connection:
        connection.execute(
            panel.users.insert().values(
                id=1, expired_at=1, transfer_enable=10, u=1, d=1, banned=0, updated_at=1
            )
        )
    snapshot = panel.get_user(1)
    assert panel.archive_if_unchanged(1, snapshot)
    assert panel.get_user(1)["banned"] == 1
    assert panel.get_user(1)["expired_at"] == 1
    with panel.engine.begin() as connection:
        connection.execute(
            panel.users.update()
            .where(panel.users.c.id == 1)
            .values(expired_at=int(time.time()) + 86400, banned=0)
        )
    assert not panel.archive_if_unchanged(1, snapshot)
    assert panel.get_user(1)["banned"] == 0
