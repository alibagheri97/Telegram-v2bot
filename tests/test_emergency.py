import time

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, select
from test_catalog import shop as shop  # noqa: PLC0414 -- dependency of shared fixture
from test_expiry import callback
from test_expiry import expiry as expiry  # noqa: PLC0414 -- shared fixture

from vpnshopbot.emergency import EmergencyService
from vpnshopbot.models import EmergencyAccount, EmergencyEvent, User
from vpnshopbot.panel import GIB, PanelRepository


def enable(env):
    env.configure(
        **{
            "service:emergency:enabled": "1",
            "emergency_group_id": -100123456,
            "emergency_topic_id": 99,
        }
    )
    return EmergencyService(env.controller.bot, env.database, env.panel)


def balance(env, v2_id=1):
    with env.database.session() as db:
        row = db.get(EmergencyAccount, v2_id)
        return (row.days_used, row.generation, row.pending_event_id) if row else None


def test_emergency_starts_paused_and_requires_topic(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = EmergencyService(expiry.controller.bot, expiry.database, expiry.panel)
    original = expiry.accounts[1]["expired_at"]
    assert service.run()["paused"]
    expiry.configure(**{"service:emergency:enabled": "1"})
    assert service.run()["topic_pending"]
    assert expiry.accounts[1]["expired_at"] == original


def test_only_true_root_created_accounts_receive_time_credit(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.link(2, owner=20, managed=True, creator_role=1)
    expiry.link(3, owner=40)
    expiry.link(4, owner=10, managed=True)
    expiry.link(5, owner=10, managed=False, creator_role=0, kind="purchase")
    with expiry.database.session() as db:
        db.get(User, 20).permission = 0
    service = enable(expiry)
    assert service.run()["granted"] == 1
    assert balance(expiry) == (1, 0, None)
    assert all(balance(expiry, account_id) is None for account_id in range(2, 6))
    assert [message[0] for message in expiry.messages] == [-100123456]


def test_one_day_is_granted_once_and_keeps_traffic_and_ban(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0, banned=1)
    service = enable(expiry)
    now = int(time.time())
    assert service.run()["granted"] == 1
    account = expiry.accounts[1]
    assert now + 86400 <= account["expired_at"] <= int(time.time()) + 86400
    assert account["u"] == account["d"] == GIB
    assert account["banned"] == 1
    assert service.run()["granted"] == 0
    assert service.run()["sent"] == 0
    assert balance(expiry)[0] == 1
    _, text, kwargs = expiry.messages[0]
    assert kwargs["message_thread_id"] == 99
    buttons = [button for row in kwargs["reply_markup"].keyboard for button in row]
    assert buttons[0].callback_data == "emergency:renew:1:0"
    assert buttons[1].url == "https://t.me/test_vpn_bot?start=account_1"
    assert "30" in text


def test_three_advances_monthly_adds_28_days_and_resets_counter(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    for _ in range(3):
        expiry.accounts[1]["expired_at"] = int(time.time()) - 1
        assert service.run()["granted"] == 1
    before = expiry.accounts[1]["expired_at"]
    result = service.operate(1, monthly=True, generation=0, actor_id=10)
    assert result["days"] == 28 and result["used"] == 3
    assert expiry.accounts[1]["expired_at"] == before + 28 * 86400
    assert balance(expiry) == (0, 1, None)
    with expiry.database.session() as db:
        events = db.scalars(select(EmergencyEvent).where(EmergencyEvent.v2_user_id == 1)).all()
        assert sorted(event.days_added for event in events) == [1, 1, 1, 28]
    with pytest.raises(ValueError, match="دوره قبلی"):
        service.operate(1, monthly=True, generation=0, actor_id=10)


@pytest.mark.parametrize("apply_before_failure", [False, True])
def test_restart_recovers_exactly_one_advance(expiry, apply_before_failure):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    original = expiry.panel.apply_emergency_operation

    def fail(account_id, **kwargs):
        if apply_before_failure:
            original(account_id, **kwargs)
        raise RuntimeError("simulated process interruption")

    expiry.panel.apply_emergency_operation = fail
    with pytest.raises(RuntimeError):
        service.operate(1)
    assert balance(expiry)[0] == 0 and balance(expiry)[2]
    expiry.panel.apply_emergency_operation = original
    restarted = EmergencyService(expiry.controller.bot, expiry.database, expiry.panel)
    assert restarted.operate(1) is None
    assert balance(expiry) == (1, 0, None)
    with expiry.database.session() as db:
        assert len(db.scalars(select(EmergencyEvent)).all()) == 1


def test_monthly_recovery_cannot_charge_a_second_month(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    service.operate(1)
    original = expiry.panel.apply_emergency_operation

    def fail(account_id, **kwargs):
        original(account_id, **kwargs)
        raise RuntimeError("lost acknowledgement")

    expiry.panel.apply_emergency_operation = fail
    with pytest.raises(RuntimeError):
        service.operate(1, monthly=True, generation=0, actor_id=10)
    expected_expiry = expiry.accounts[1]["expired_at"]
    expiry.panel.apply_emergency_operation = original
    with pytest.raises(ValueError, match="دوره قبلی"):
        service.operate(1, monthly=True, generation=0, actor_id=10)
    assert expiry.accounts[1]["expired_at"] == expected_expiry
    assert balance(expiry) == (0, 1, None)


def test_failed_announcement_retries_without_another_credit(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    expiry.failures.add(-100123456)
    assert service.run()["granted"] == 1
    expected = expiry.accounts[1]["expired_at"]
    expiry.failures.clear()
    assert service.run()["sent"] == 1
    assert expiry.accounts[1]["expired_at"] == expected
    assert balance(expiry)[0] == 1


def test_monthly_settlement_discards_outdated_unsent_emergency_notice(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    service.operate(1)
    service.operate(1, monthly=True, generation=0, actor_id=10)
    assert service.run()["sent"] == 0
    assert not expiry.messages


def test_volume_exhaustion_or_unlimited_time_does_not_grant_time_repeatedly(expiry):
    expiry.link(
        1, owner=10, managed=True, creator_role=0, expired_at=int(time.time()) + 86400, u=10 * GIB
    )
    expiry.link(2, owner=10, managed=True, creator_role=0, expired_at=0, u=10 * GIB)
    service = enable(expiry)
    assert service.run()["granted"] == 0


def test_outstanding_advances_stop_at_31_and_monthly_never_subtracts_time(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    with expiry.database.session() as db:
        db.add(EmergencyAccount(v2_user_id=1, days_used=31, generation=0))
    assert service.run()["granted"] == 0
    result = service.operate(1, monthly=True, generation=0, actor_id=10)
    assert result["days"] == 0
    assert balance(expiry) == (0, 1, None)


def test_old_expiry_topic_button_deducts_emergency_debt_even_when_now_active(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.worker.run(force=True)
    service = enable(expiry)
    service.run()
    expiry.worker.run(force=True)
    before = expiry.accounts[1]["expired_at"]
    expiry.controller._expiry_callback(callback("expiry:renew:1:1"), "expiry:renew:1:1")
    assert expiry.accounts[1]["expired_at"] == before + 30 * 86400
    assert balance(expiry) == (0, 1, None)


def test_emergency_monthly_callback_is_root_only_and_one_use(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    service.run()
    with pytest.raises(ValueError, match="مدیر اصلی"):
        expiry.controller._emergency_callback(
            callback("emergency:renew:1:0", actor=20), "emergency:renew:1:0"
        )
    expiry.controller._emergency_callback(callback("emergency:renew:1:0"), "emergency:renew:1:0")
    assert "30" in expiry.controller.screens[-1][0]
    with pytest.raises(ValueError, match="دوره قبلی"):
        expiry.controller._emergency_callback(
            callback("emergency:renew:1:0"), "emergency:renew:1:0"
        )


def test_old_expiry_notification_cannot_renew_a_later_emergency_cycle(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    expiry.worker.run(force=True)
    service = enable(expiry)
    service.operate(1)
    service.operate(1, monthly=True, generation=0, actor_id=10)
    expiry.accounts[1]["expired_at"] = int(time.time()) - 1
    service.operate(1)
    for data in ["expiry:renew:1:1", "expiry:renew:1:1:0"]:
        with pytest.raises(ValueError, match="دوره قبلی"):
            expiry.controller._expiry_callback(callback(data), data)
    assert balance(expiry) == (1, 1, None)


def test_emergency_toggle_persists_and_requires_topic(expiry):
    with pytest.raises(ValueError, match="تاپیک"):
        expiry.controller._emergency_callback(callback("emergency:toggle"), "emergency:toggle")
    expiry.configure(emergency_group_id=-100123456, emergency_topic_id=99)
    expiry.controller._emergency_callback(callback("emergency:toggle"), "emergency:toggle")
    assert (
        not EmergencyService(expiry.controller.bot, expiry.database, expiry.panel)
        .run()
        .get("paused")
    )
    expiry.controller._emergency_callback(callback("emergency:toggle"), "emergency:toggle")
    assert EmergencyService(expiry.controller.bot, expiry.database, expiry.panel).run()["paused"]


def test_emergency_driving_list_is_root_only(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    enable(expiry).operate(1)
    with expiry.database.session() as db:
        actor = db.get(User, 10)
        source = expiry.controller._driving_source(db, actor, "emergency")
        view = expiry.controller._driving_load(db, actor, "emergency", source)
        rows, _, _, _ = expiry.controller._driving_query(db, source, view)
        assert rows[0]["days_used"] == 1
        with pytest.raises(ValueError, match="مدیر اصلی"):
            expiry.controller._driving_source(db, db.get(User, 20), "emergency")


def test_panel_compare_and_set_replay_preserves_emergency_usage_and_monthly_resets_it():
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
    before, after = 100, int(time.time()) + 86400
    with panel.engine.begin() as connection:
        connection.execute(
            panel.users.insert().values(
                id=1,
                expired_at=before,
                transfer_enable=10 * GIB,
                u=GIB,
                d=GIB,
                banned=1,
                updated_at=before,
            )
        )
    operation = {"before": before, "after": after, "transfer_enable": 10 * GIB, "monthly": False}
    panel.apply_emergency_operation(1, **operation)
    panel.apply_emergency_operation(1, **operation)
    account = panel.get_user(1)
    assert account["expired_at"] == after
    assert account["u"] == account["d"] == GIB and account["banned"] == 1
    with pytest.raises(ValueError, match="تعارض"):
        panel.apply_emergency_operation(
            1, before=100, after=after + 1, transfer_enable=10 * GIB, monthly=False
        )
    panel.apply_emergency_operation(
        1, before=after, after=after + 30 * 86400, transfer_enable=10 * GIB, monthly=True
    )
    account = panel.get_user(1)
    assert account["u"] == account["d"] == account["banned"] == 0


def test_legacy_monthly_button_does_not_bypass_emergency_debt(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    service = enable(expiry)
    service.operate(1)
    before = expiry.accounts[1]["expired_at"]
    expiry.controller._v2_action(callback("v2:addmonth:1"), "v2:addmonth:1")
    assert expiry.accounts[1]["expired_at"] == before
    button = expiry.controller.screens[-1][1].keyboard[0][0]
    assert button.callback_data == "emergency:renew:1:0"


def test_counter_is_visible_in_account_details_and_monthly_button(expiry):
    expiry.link(1, owner=10, managed=True, creator_role=0)
    enable(expiry).operate(1)
    expiry.controller._v2_action(callback("v2:view:1"), "v2:view:1")
    text, markup = expiry.controller.screens[-1]
    assert "روزهای اضطراری این دوره: 1" in text
    assert "تمدید ماهانه: 30" in text
    assert any(
        button.callback_data == "emergency:renew:1:0" for row in markup.keyboard for button in row
    )
