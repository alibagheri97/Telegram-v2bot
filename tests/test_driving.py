from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import sessionmaker

from vpnshopbot.db import Base
from vpnshopbot.handlers import BotController
from vpnshopbot.models import AdminPrice, Product, ProductType, User, UserStatus, V2AccountLink
from vpnshopbot.panel import GIB
from vpnshopbot.services import ShopService


class MemoryDatabase:
    def __init__(self):
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        with self.sessions() as db, db.begin():
            yield db


@pytest.fixture
def table_ui():
    database = MemoryDatabase()
    panel = Table(
        "panel_users",
        MetaData(),
        Column("id", Integer, primary_key=True),
        Column("email", String),
        *[
            Column(name, Integer)
            for name in [
                "d",
                "u",
                "transfer_enable",
                "expired_at",
                "banned",
                "telegram_id",
                "device_limit",
                "created_at",
            ]
        ],
        *[Column(name, String) for name in ["remarks", "token", "uuid"]],
    )
    panel.create(database.engine)
    with database.engine.begin() as connection:
        connection.execute(
            panel.insert(),
            [
                {
                    "id": n,
                    "email": f"person{n}@example.test",
                    "d": n * GIB,
                    "u": GIB,
                    "transfer_enable": 100 * GIB,
                    "expired_at": 0,
                    "banned": 0,
                    "telegram_id": 500 + n,
                    "device_limit": 1,
                    "created_at": 100,
                    "remarks": f"customer{n}",
                    "token": f"token{n}",
                    "uuid": f"uuid{n}",
                }
                for n in range(1, 21)
            ],
        )
    with database.session() as db:
        for uid, role in [(1, 0), (2, 1), (3, 1), (4, -1)]:
            db.add(
                User(
                    id=uid,
                    telegram_id=uid,
                    name=f"Person {uid}",
                    permission=role,
                    status=UserStatus.ACTIVE,
                    referral_code=f"ref{uid}",
                    balance=uid * 1000,
                    v2accounts=[],
                )
            )
        for n in range(1, 21):
            db.add(
                Product(
                    id=n,
                    code=f"plan{n}",
                    title=f"Plan {n}",
                    product_type=ProductType.DATA,
                    data_gb=10,
                    connections=1,
                    price=n * 10000,
                    enabled=True,
                )
            )
        db.flush()
        db.add(AdminPrice(admin_id=2, product_id=2, price=500))
        db.add(AdminPrice(admin_id=3, product_id=3, price=900))
        db.add(
            V2AccountLink(
                user_id=2,
                v2_user_id=2,
                label="Mine",
                product_type=ProductType.DATA,
                admin_created=True,
            )
        )
        db.add(
            V2AccountLink(
                user_id=3,
                v2_user_id=3,
                label="Foreign",
                product_type=ProductType.DATA,
                admin_created=True,
            )
        )
    controller = BotController.__new__(BotController)
    controller.database = database
    controller.panel = SimpleNamespace(engine=database.engine, users=panel)
    controller.service = ShopService(SimpleNamespace(), controller.panel)
    controller.screens = []
    controller.replies = []
    controller._edit = lambda call, text, markup: controller.screens.append((text, markup))
    controller.bot = SimpleNamespace(
        reply_to=lambda *args, **kwargs: controller.replies.append((args, kwargs))
    )
    return controller, database


def call(actor=1, data=""):
    return SimpleNamespace(from_user=SimpleNamespace(id=actor), data=data)


def data_rows(controller):
    markup = controller.screens[-1][1]
    return [
        row for row in markup.keyboard if any(":open:" in button.callback_data for button in row)
    ]


def test_screenshot_layout_has_column_headers_cells_and_page_metadata(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(), "v2")
    text, markup = controller.screens[-1]
    assert "20" in text and "1/3" in text
    assert [b.text for b in markup.keyboard[0]] == ["ID ↑", "ایمیل", "انقضا"]
    rows = data_rows(controller)
    assert len(rows) == 8 and all(len(row) == 3 for row in rows)
    assert {b.callback_data for b in rows[0]} == {"dl:v2:open:1"}
    callbacks = {b.callback_data for row in markup.keyboard for b in row}
    assert {
        "dl:v2:add",
        "dl:v2:remove",
        "dl:v2:search",
        "dl:v2:sortmenu",
        "dl:v2:show",
    } <= callbacks


def test_sort_and_column_selection_persist_per_list(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(), "v2", "sort", "id")
    assert data_rows(controller)[0][0].text == "#20"
    controller._driving_render(call(), "v2", "addcol", "d")
    controller._driving_render(call(), "v2", "rmcol", "email")
    controller._driving_render(call(), "v2")
    assert [b.text for b in controller.screens[-1][1].keyboard[0]] == ["ID ↓", "انقضا", "دانلود"]
    controller._driving_render(call(), "users")
    assert [b.text for b in controller.screens[-1][1].keyboard[0]] == ["ID ↑", "نام", "کیف پول"]


def test_pagination_clamps_before_querying_and_shows_last_rows(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(), "v2", "page", "999")
    assert "3/3" in controller.screens[-1][0]
    assert len(data_rows(controller)) == 4
    assert data_rows(controller)[0][0].text == "#17"


def test_nested_search_and_undo_keep_result_scope(table_ui):
    controller, database = table_ui

    def search(query):
        with database.session() as db:
            controller._driving_search_message(
                db, SimpleNamespace(text=query, from_user=SimpleNamespace(id=1)), {"list_key": "v2"}
            )
        controller._driving_render(call(), "v2")

    search("id?1")
    assert "تعداد داده‌ها: 11" in controller.screens[-1][0]
    search("id=12")
    assert len(data_rows(controller)) == 1
    assert data_rows(controller)[0][0].text == "#12"
    controller._driving_render(call(), "v2", "undo")
    assert "تعداد داده‌ها: 11" in controller.screens[-1][0]
    controller._driving_render(call(), "v2", "reset")
    assert "تعداد داده‌ها: 20" in controller.screens[-1][0]


def test_administrative_datasets_all_use_the_shared_cells(table_ui):
    controller, _ = table_ui
    for key in ["users", "admins", "plans", "ap2"]:
        controller._driving_render(call(), key)
        rows = data_rows(controller)
        assert rows and all(len(row) >= 3 for row in rows)
    controller._driving_render(call(2), "pick")
    assert len(data_rows(controller)) == 1
    assert data_rows(controller)[0][0].text == "#2"


def test_admin_lists_and_forged_callbacks_cannot_access_other_owners(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(2), "customers")
    assert len(data_rows(controller)) == 1
    assert data_rows(controller)[0][0].text == "#2"
    with pytest.raises(ValueError, match="دسترسی مدیر اصلی"):
        controller._driving_render(call(2), "v2")
    with pytest.raises(ValueError, match="دسترسی مدیر اصلی"):
        controller._driving_render(call(2), "ap3")
    with pytest.raises(ValueError, match="دسترسی ندارید"):
        controller._driving_callback(call(2, "dl:customers:open:3"))


def test_search_in_plan_picker_preserves_username_workflow(table_ui):
    controller, database = table_ui
    with database.session() as db:
        controller.service.set_state(db, 2, "admin_pick_type", username="Customer")
    controller._driving_render(call(2), "pick", "search")
    with database.session() as db:
        state = controller.service.get_state(db, 2)
        payload = dict(state.data)
        controller._driving_search_message(
            db, SimpleNamespace(text="title?Plan", from_user=SimpleNamespace(id=2)), payload
        )
    with database.session() as db:
        state = controller.service.get_state(db, 2)
        assert state.state == "admin_pick_type"
        assert state.data == {"username": "Customer"}


def test_search_payload_is_bound_and_cannot_expand_results(table_ui):
    controller, database = table_ui
    with database.session() as db:
        actor = db.get(User, 2)
        source = controller._driving_source(db, actor, "customers")
        view = controller._driving_load(db, actor, "customers", source)
        view["filters"] = ["' OR 1=1 --"]
        rows, total, _, _ = controller._driving_query(db, source, view)
        assert rows == [] and total == 0
        assert len(list(db.scalars(select(User)))) == 4


def test_quick_actions_use_existing_cells_and_toggle_back(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(), "v2", "quick")
    rows = [row for row in controller.screens[-1][1].keyboard
            if any(":cell:" in button.callback_data for button in row)]
    assert len(rows) == 8
    assert all(len(row) == 3 for row in rows)
    assert rows[0][2].callback_data == "dl:v2:cell:1:expired_at"
    controller._driving_render(call(), "v2", "quick")
    assert all(len(row) == 3 for row in data_rows(controller))


def test_admin_list_excludes_roots_and_newly_promoted_roots(table_ui):
    controller, database = table_ui
    with database.session() as db:
        actor = db.get(User, 1)
        source = controller._driving_source(db, actor, "admins")
        view = controller._driving_load(db, actor, "admins", source)
        rows, total, _, _ = controller._driving_query(db, source, view)
        assert total == 2
        assert {row["id"] for row in rows} == {2, 3}
        db.get(User, 2).permission = 0
        db.flush()
        rows, total, _, _ = controller._driving_query(db, source, view)
        assert total == 1 and rows[0]["id"] == 3


def test_admin_plan_cells_toggle_only_selected_admin(table_ui):
    controller, database = table_ui
    controller._driving_render(call(), "ap2", "quick")
    controller._driving_callback(call(data="dl:ap2:cell:3:assigned"))
    with database.session() as db:
        assert db.scalar(select(AdminPrice.price).where(AdminPrice.admin_id == 2, AdminPrice.product_id == 3)) == 30000
        assert db.scalar(select(AdminPrice.price).where(AdminPrice.admin_id == 3, AdminPrice.product_id == 3)) == 900
    controller._driving_callback(call(data="dl:ap2:cell:3:assigned"))
    with database.session() as db:
        assert db.scalar(select(AdminPrice).where(AdminPrice.admin_id == 2, AdminPrice.product_id == 3)) is None


def test_quick_price_input_returns_to_same_list_and_keeps_other_admin_price(table_ui):
    controller, database = table_ui
    controller._driving_render(call(), "ap2", "page", "2")
    controller._driving_render(call(), "ap2", "quick")
    request = call(data="dl:ap2:cell:10:admin_price")
    request.message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=123)
    controller._driving_callback(request)
    with database.session() as db:
        state = controller.service.get_state(db, 1)
        assert state.state == "driving_edit"
        payload = dict(state.data)
        message = SimpleNamespace(text="۱۲٬۳۴۵", from_user=request.from_user, chat=request.message.chat)
        controller._driving_edit_message(db, message, payload)
        assert db.scalar(select(AdminPrice.price).where(AdminPrice.admin_id == 2, AdminPrice.product_id == 10)) == 12345
        assert controller.service.get_state(db, 1) is None
    assert "صفحه: 2/3" in controller.screens[-1][0]
    assert any(button.callback_data == "dl:ap2:cell:10:admin_price"
               for row in controller.screens[-1][1].keyboard for button in row)


def test_quick_off_and_readonly_cells_open_details(table_ui):
    controller, _ = table_ui
    dispatched = []
    controller._dispatch_callback_with_data = lambda _, target: dispatched.append(target)
    controller._driving_callback(call(data="dl:ap2:cell:2:assigned"))
    assert dispatched == ["catalog:assignment:2:2"]
    controller._driving_render(call(), "ap2", "quick")
    controller._driving_callback(call(data="dl:ap2:cell:2:title"))
    assert dispatched[-1] == "catalog:assignment:2:2"


def test_quick_admin_cells_reject_nonroot(table_ui):
    controller, _ = table_ui
    with pytest.raises(ValueError, match="مدیر اصلی"):
        controller._driving_callback(call(actor=2, data="dl:ap2:cell:2:assigned"))


def test_v2_quick_cells_toggle_ban_and_prompt_expiry(table_ui):
    controller, database = table_ui
    changed = []
    controller.panel.get_user = lambda item: {"banned": 0}
    controller.panel.set_banned = lambda item, value: changed.append((item, value))
    controller.panel.add_days = lambda item, days: changed.append((item, days))
    controller._driving_render(call(), "v2", "quick")
    controller._driving_callback(call(data="dl:v2:cell:2:banned"))
    assert changed == [(2, True)]
    request = call(data="dl:v2:cell:2:expired_at")
    request.message = SimpleNamespace(chat=SimpleNamespace(id=1), message_id=456)
    controller._driving_callback(request)
    with database.session() as db:
        payload = dict(controller.service.get_state(db, 1).data)
        message = SimpleNamespace(text="30", from_user=request.from_user, chat=request.message.chat)
        controller._driving_edit_message(db, message, payload)
    assert changed[-1] == (2, 30)
    assert "دیتابیس V2" in controller.screens[-1][0]


def test_settings_list_excludes_internal_and_authentication_values(table_ui):
    from vpnshopbot.models import BotSetting

    controller, database = table_ui
    with database.session() as db:
        db.add(BotSetting(key="card_holder", value="Ali"))
        db.add(BotSetting(key="topic_auth_hash", value="secret-hash"))
        db.add(BotSetting(key="bootstrap_claimed", value="private"))
    controller._driving_render(call(), "settings")
    rows = data_rows(controller)
    assert len(rows) == 1
    assert rows[0][0].text == "card_holder"
    assert rows[0][1].text == "Ali"
    with pytest.raises(ValueError, match="دسترسی مدیر اصلی"):
        controller._driving_render(call(2), "settings")


def test_stale_callbacks_recheck_revoked_roles(table_ui):
    controller, database = table_ui
    controller._driving_render(call(2), "customers")
    with database.session() as db:
        db.get(User, 2).permission = -1
    with pytest.raises(ValueError, match="دسترسی مدیریت"):
        controller._driving_callback(call(2, "dl:customers:open:2"))


def test_root_selected_users_v2_accounts_open_root_v2_actions(table_ui):
    controller, _ = table_ui
    controller._driving_render(call(), "uv2")
    assert len(data_rows(controller)) == 1
    assert data_rows(controller)[0][0].text == "#2"
    targets = []
    controller._dispatch_callback_with_data = lambda query, target: targets.append(target)
    controller._driving_callback(call(1, "dl:uv2:open:2"))
    assert targets == ["v2:view:2"]
