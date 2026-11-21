"""One persistent, server-paginated table UI for every administrative list."""

from __future__ import annotations

import enum
import html
import json
import math
import re
import time
from contextlib import nullcontext
from dataclasses import dataclass
from datetime import datetime
from types import SimpleNamespace

from sqlalchemy import String, and_, cast, func, or_, select

from . import keyboards
from .models import (
    AdminPrice,
    BotSetting,
    EmergencyAccount,
    ExpiryWatch,
    Order,
    Permission,
    Product,
    User,
    UserAction,
    UserStatus,
    V2AccountLink,
    V2Archive,
    V2ManualPayment,
)
from .panel import GIB

PAGE_SIZE = 8
PUBLIC_SETTING_KEYS = {
    "referral_reward",
    "data_price_per_gb",
    "support_username",
    "payment_group_id",
    "payment_topic_id",
    "expired_group_id",
    "expired_topic_id",
    "emergency_group_id",
    "emergency_topic_id",
    "card_number",
    "card_holder",
    "guide_android_file_id",
    "guide_iphone_file_id",
    "guide_windows_file_id",
    "guide_mac_file_id",
    "guide_linux_file_id",
}


@dataclass
class DrivingSource:
    title: str
    fields: dict
    labels: dict
    defaults: list[str]
    source: object
    scope: object
    detail: str
    back: str = "root:home"
    panel: bool = False
    quick: bool = False


def search_predicate(fields: dict, query: str):
    """Identifiers come exclusively from the source whitelist; values stay bound."""
    match = re.fullmatch(r"([a-zA-Z_]+)\s*([=?])\s*(.+)", query.strip())
    if match:
        key, operator, value = match.groups()
        if key not in fields:
            raise ValueError("ستون جست‌وجو معتبر نیست")
        targets = [fields[key]]
    else:
        operator, value, targets = "?", query.strip(), list(fields.values())
    value = (
        value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        if operator == "?"
        else value
    )
    return or_(
        *[
            cast(column, String).like(f"%{value}%", escape="\\")
            if operator == "?"
            else func.lower(cast(column, String)) == value.lower()
            for column in targets
        ]
    )


def query_page(connection, source: DrivingSource, view: dict):
    conditions = [source.scope]
    conditions.extend(search_predicate(source.fields, value) for value in view["filters"])
    predicate = and_(*conditions)
    total = int(
        connection.scalar(select(func.count()).select_from(source.source).where(predicate)) or 0
    )
    pages = max(1, math.ceil(total / PAGE_SIZE))
    page = min(max(1, int(view["page"])), pages)
    column = source.fields[view["sort"]]
    ordering = column.desc() if view["desc"] else column.asc()
    query = (
        select(*[expr.label(key) for key, expr in source.fields.items()])
        .select_from(source.source)
        .where(predicate)
        .order_by(ordering, source.fields["id"].asc())
        .limit(PAGE_SIZE)
        .offset((page - 1) * PAGE_SIZE)
    )
    return [dict(row) for row in connection.execute(query).mappings()], total, page, pages


def cell_text(key: str, value, row: dict) -> str:
    if value is None:
        return "—"
    if isinstance(value, enum.Enum):
        value = value.value
    if key == "id":
        return f"#{value}" if str(value).isdigit() else str(value)
    if key == "product_type":
        return {"time": "♾ زمانی", "data": "📦 حجمی", "wallet": "💳 کیف پول"}.get(
            str(value), str(value)
        )
    if key in {"u", "d", "transfer_enable", "used"}:
        return f"{int(value) / GIB:.1f} GB"
    if key == "expired_at":
        if not value:
            return "♾️ بدون انقضا"
        seconds = int(value) - int(time.time())
        if seconds <= 0:
            return f"🔴 منقضی ({abs(seconds) // 86400} روز)"
        if seconds < 86400:
            return f"🟡 {seconds // 3600:02}:{seconds % 3600 // 60:02}"
        return f"{seconds // 86400} روز"
    if key in {"price", "balance", "amount", "admin_price"}:
        return f"{int(value):,} ت"
    if key in {"enabled", "assigned", "banned", "auto_renewal"}:
        return "✅ بله" if value else "➖ خیر"
    if key == "permission":
        return {0: "👑 روت", 1: "🧑‍💼 ادمین", -1: "👤 کاربر"}.get(int(value), str(value))
    if key == "status":
        return {
            "active": "🟢 فعال",
            "blocked": "🔴 مسدود",
            "pending": "🟡 انتظار",
            "awaiting_payment": "🟠 منتظر پرداخت",
            "pending_review": "🟡 بررسی رسید",
            "paid": "🟢 پرداخت‌شده",
            "rejected": "🔴 ردشده",
            "cancelled": "⚫ لغوشده",
            "archived": "🗃 بایگانی",
            "restored": "♻️ بازیابی‌شده",
        }.get(str(value), str(value))
    if isinstance(value, datetime):
        return value.strftime("%Y/%m/%d %H:%M")
    text = str(value).replace("\n", " ")
    return text if len(text) <= 23 else text[:22] + "…"


class DrivingListMixin:
    def _driving_source(self, db, actor, key: str) -> DrivingSource:
        if actor is None:
            raise ValueError("ابتدا وارد ربات شوید")
        roots = {"v2", "users", "plans", "admins", "settings", "expired", "emergency", "archives"}
        if (key in roots or key.startswith(("ap", "uv", "events", "vp"))) and not actor.is_root:
            raise ValueError("دسترسی مدیر اصلی لازم است")
        if not actor.is_admin or actor.status != UserStatus.ACTIVE:
            raise ValueError("دسترسی مدیریت ندارید")
        if key == "archives":
            table = V2Archive.__table__
            return DrivingSource("🗃 بایگانی اشتراک‌ها",
                {"id": table.c.v2_user_id, "email": table.c.email, "label": table.c.label,
                 "status": table.c.status, "archived_at": table.c.archived_at, "user_id": table.c.user_id},
                {"id": "ID", "email": "ایمیل", "label": "نام", "status": "وضعیت",
                 "archived_at": "زمان بایگانی", "user_id": "مالک"},
                ["id", "email", "archived_at"], table, table.c.status.in_(["pending", "archived"]),
                "archive:view:{id}", "expiry:service:expire")
        if re.fullmatch(r"vpay\d+", key):
            v2_id, table = int(key[4:]), V2ManualPayment.__table__
            return DrivingSource("💵 پرداخت‌های دستی اکانت",
                {name: table.c[name] for name in ["id", "amount", "actor_telegram_id", "created_at"]},
                {"id": "ID", "amount": "مبلغ", "actor_telegram_id": "ثبت‌کننده", "created_at": "تاریخ"},
                ["id", "amount", "created_at"], table, table.c.v2_user_id == v2_id,
                f"v2:paymentdetail:{v2_id}:{{id}}", f"v2:view:{v2_id}")
        if key == "emergency":
            balances, links = EmergencyAccount.__table__, V2AccountLink.__table__
            return DrivingSource("🆘 شمارنده‌های شارژ اضطراری",
                {"id": balances.c.v2_user_id, "label": links.c.label,
                 "days_used": balances.c.days_used, "generation": balances.c.generation,
                 "pending": balances.c.pending_event_id, "updated_at": balances.c.updated_at},
                {"id": "ID", "label": "اکانت", "days_used": "روز اضطراری",
                 "generation": "دوره", "pending": "عملیات معلق", "updated_at": "آخرین تغییر"},
                ["id", "label", "days_used"], balances.join(links, links.c.v2_user_id == balances.c.v2_user_id),
                True, "v2:view:{id}", "expiry:service:emergency")
        if key == "expired":
            watch, links, users = ExpiryWatch.__table__, V2AccountLink.__table__, User.__table__
            return DrivingSource("🔴 اشتراک‌های پایان‌یافته",
                {"id": watch.c.v2_user_id, "label": links.c.label, "name": users.c.name,
                 "destination": watch.c.destination, "updated_at": watch.c.updated_at},
                {"id": "ID", "label": "اشتراک", "name": "مالک", "destination": "مقصد اعلان",
                 "updated_at": "آخرین بررسی"}, ["id", "label", "name"],
                watch.join(links, links.c.v2_user_id == watch.c.v2_user_id).join(users, users.c.id == links.c.user_id),
                watch.c.was_expired.is_(True), "v2:view:{id}", "expiry:service:expire")
        if re.fullmatch(r"events\d+", key):
            owner_id = int(key[6:])
            owner = db.get(User, owner_id)
            if not owner:
                raise ValueError("کاربر پیدا نشد")
            table = UserAction.__table__
            fields = {name: table.c[name] for name in ["id", "title", "action", "actor_telegram_id", "created_at"]}
            return DrivingSource(f"📜 تاریخچه {owner.name}", fields,
                {"id": "ID", "title": "فعالیت", "action": "نوع رویداد", "actor_telegram_id": "انجام‌دهنده", "created_at": "زمان UTC"},
                ["id", "title", "created_at"], table, table.c.user_id == owner_id,
                "user:event:{id}", f"user:view:{owner_id}")
        if key == "settings":
            table = BotSetting.__table__
            return DrivingSource(
                "⚙ تنظیمات",
                {"id": table.c.key, "value": table.c.value, "updated_at": table.c.updated_at},
                {"id": "پارامتر", "value": "مقدار", "updated_at": "بروزرسانی"},
                ["id", "value"],
                table,
                table.c.key.in_(PUBLIC_SETTING_KEYS),
                "root:settings",
            )
        if key in {"v2", "customers", "subs"} or re.fullmatch(r"uv\d+", key):
            owner_id = int(key[2:]) if key.startswith("uv") else actor.id
            owner = db.get(User, owner_id)
            if not owner:
                raise ValueError("کاربر پیدا نشد")
            table = self.panel.users
            names = [
                "id",
                "email",
                "d",
                "u",
                "transfer_enable",
                "expired_at",
                "banned",
                "telegram_id",
                "remarks",
                "device_limit",
                "token",
                "uuid",
                "created_at",
            ]
            fields = {name: table.c[name] for name in names}
            fields["used"] = table.c.u + table.c.d
            archived_ids = list(db.scalars(select(V2Archive.v2_user_id).where(V2Archive.status.in_(["pending", "archived"]))))
            scope = table.c.id.not_in(archived_ids) if archived_ids else True
            if key != "v2":
                query = select(V2AccountLink.v2_user_id).where(V2AccountLink.user_id == owner_id)
                if key == "customers":
                    query = query.where(V2AccountLink.admin_created.is_(True))
                scope = and_(scope, table.c.id.in_(list(db.scalars(query))))
                for hidden in ("token", "uuid"):
                    fields.pop(hidden)
            return DrivingSource(
                "🗄 دیتابیس V2" if key == "v2" else f"📋 اکانت‌های {owner.name}",
                fields,
                {
                    "id": "ID",
                    "email": "ایمیل",
                    "d": "دانلود",
                    "u": "آپلود",
                    "transfer_enable": "حجم کل",
                    "expired_at": "انقضا",
                    "banned": "مسدود",
                    "telegram_id": "تلگرام",
                    "remarks": "یادداشت",
                    "device_limit": "اتصال",
                    "used": "مصرف کل",
                    "token": "توکن",
                    "uuid": "UUID",
                    "created_at": "ایجاد",
                },
                ["id", "email", "expired_at"],
                table,
                scope,
                "v2:view:{id}" if key == "v2" or key.startswith("uv") else "sub:view:{id}",
                f"user:view:{owner_id}" if key.startswith("uv") else "root:home" if key == "v2" else "admin:home",
                panel=True,
                quick=key != "subs",
            )
        if key in {"users", "admins", "friends"}:
            table = User.__table__
            names = [
                "id",
                "name",
                "telegram_id",
                "permission",
                "balance",
                "status",
                "referral_code",
                "created_at",
                "updated_at",
            ]
            result = DrivingSource(
                "👥 کاربران ربات" if key == "users" else "🧑‍💼 ادمین‌ها",
                {name: table.c[name] for name in names},
                {
                    "id": "ID",
                    "name": "نام",
                    "telegram_id": "تلگرام",
                    "permission": "سطح",
                    "balance": "کیف پول",
                    "status": "وضعیت",
                    "referral_code": "کد دعوت",
                    "created_at": "ایجاد",
                    "updated_at": "بروزرسانی",
                },
                ["id", "name", "balance"],
                table,
                User.permission == int(Permission.ADMIN) if key == "admins" else True,
                "catalog:admin:{id}" if key == "admins" else "user:view:{id}",
                quick=True,
            )
            if key == "friends":
                result.title = "🎁 دوستان دعوت‌شده"
                result.scope = User.referred_by_id == actor.id
                result.fields = {name: table.c[name] for name in ["id", "name", "created_at"]}
                result.defaults = ["id", "name", "created_at"]
                result.detail, result.back, result.quick = "dl:friends:show", "home:user", False
            return result
        if key in {"plans", "pick", "purchase"} or re.fullmatch(r"(?:ap|vp)\d+", key):
            table = Product.__table__
            names = [
                "id",
                "title",
                "price",
                "product_type",
                "months",
                "data_gb",
                "connections",
                "enabled",
                "code",
            ]
            fields = {name: table.c[name] for name in names}
            labels = {
                "id": "ID",
                "title": "بسته",
                "price": "قیمت کاربران",
                "product_type": "نوع",
                "months": "ماه",
                "data_gb": "گیگابایت",
                "connections": "کاربر",
                "enabled": "فروش عمومی",
                "code": "کد",
            }
            source, scope = table, True
            defaults = ["id", "title", "price"]
            detail, title, back = "catalog:plan:{id}", "📦 مدیریت بسته‌ها", "root:home"
            if key.startswith("vp"):
                return DrivingSource("📦 انتخاب الگوی حجم و اتصال", fields, labels, defaults,
                    source, scope, f"v2:pickplan:{int(key[2:])}:{{id}}", f"v2:view:{int(key[2:])}")
            if key != "plans":
                owner_id = int(key[2:]) if key.startswith("ap") else actor.id
                owner = db.get(User, owner_id)
                if not owner or not owner.is_admin:
                    raise ValueError("ادمین پیدا نشد")
                if key.startswith("ap") and owner.permission != int(Permission.ADMIN):
                    raise ValueError("این کاربر دیگر ادمین نیست")
                prices = AdminPrice.__table__
                source = table.outerjoin(
                    prices, and_(prices.c.product_id == table.c.id, prices.c.admin_id == owner_id)
                )
                fields["admin_price"] = prices.c.price
                fields["assigned"] = prices.c.id.is_not(None)
                labels.update(admin_price="قیمت ادمین", assigned="دسترسی")
                defaults = ["id", "title", "admin_price", "assigned"]
                title = f"📦 بسته‌های {owner.name}"
                detail, back = f"catalog:assignment:{owner_id}:{{id}}", f"catalog:admin:{owner_id}"
                if key in {"pick", "purchase"}:
                    scope = prices.c.id.is_not(None)
                    detail, back = (
                        ("resell:plan:{id}" if key == "pick" else "buy:product:{id}"),
                        "admin:home",
                    )
            return DrivingSource(title, fields, labels, defaults, source, scope, detail, back,
                                 quick=key.startswith("ap"))
        if key == "orders":
            table = Order.__table__
            names = [
                "id",
                "user_id",
                "product_type",
                "amount",
                "status",
                "months",
                "data_gb",
                "created_at",
            ]
            return DrivingSource(
                "🧾 سفارش‌ها",
                {n: table.c[n] for n in names},
                {
                    "id": "ID",
                    "user_id": "کاربر",
                    "product_type": "نوع",
                    "amount": "مبلغ",
                    "status": "وضعیت",
                    "months": "ماه",
                    "data_gb": "گیگابایت",
                    "created_at": "ایجاد",
                },
                ["id", "amount", "status"],
                table,
                True if actor.is_root else Order.user_id == actor.id,
                "orders:view:{id}",
                "root:finance" if actor.is_root else "admin:home",
            )
        raise ValueError("فهرست معتبر نیست")

    @staticmethod
    def _driving_state_key(actor_id, key):
        return f"driving:{actor_id}:{key}"

    def _driving_load(self, db, actor, key, source):
        saved = db.get(BotSetting, self._driving_state_key(actor.id, key))
        view = json.loads(saved.value) if saved else {}
        columns = [c for c in view.get("columns", source.defaults) if c in source.fields]
        return {
            "columns": columns or source.defaults[:],
            "sort": view.get("sort") if view.get("sort") in source.fields else "id",
            "desc": bool(view.get("desc", key.startswith("events"))),
            "page": max(1, int(view.get("page", 1))),
            "filters": view.get("filters", []),
            "quick": bool(view.get("quick", False)),
        }

    def _driving_save(self, db, actor, key, view):
        setting_key = self._driving_state_key(actor.id, key)
        saved = db.get(BotSetting, setting_key)
        if saved:
            saved.value = json.dumps(view, ensure_ascii=False)
        else:
            db.add(BotSetting(key=setting_key, value=json.dumps(view, ensure_ascii=False)))

    def _driving_query(self, db, source, view):
        if source.panel:
            with self.panel.engine.connect() as connection:
                return query_page(connection, source, view)
        return query_page(db, source, view)

    def _driving_render(self, call, key, action="show", value=None, *, db_override=None):
        with (nullcontext(db_override) if db_override is not None else self.database.session()) as db:
            actor = self.service.get_user(db, call.from_user.id)
            if not actor:
                raise ValueError("ابتدا وارد ربات شوید")
            source = self._driving_source(db, actor, key)
            conversation = self.service.get_state(db, actor.telegram_id)
            if conversation and conversation.state in {"catalog_price", "driving_edit"}:
                self.service.clear_state(db, actor.telegram_id)
                conversation = None
            if conversation and conversation.state == "driving_search" and action != "search":
                previous = dict(conversation.data or {})
                if previous.get("restore_state"):
                    self.service.set_state(
                        db,
                        actor.telegram_id,
                        previous["restore_state"],
                        **previous.get("restore_data", {}),
                    )
                else:
                    self.service.clear_state(db, actor.telegram_id)
            view = self._driving_load(db, actor, key, source)
            if action == "page":
                view["page"] = max(1, int(value))
            elif action == "sort":
                if value not in source.fields:
                    raise ValueError("ستون معتبر نیست")
                view["desc"] = not view["desc"] if view["sort"] == value else False
                view["sort"], view["page"] = value, 1
            elif action == "quick":
                view["quick"] = not view["quick"] if source.quick else False
            elif action == "reset":
                view["filters"], view["page"] = [], 1
            elif action == "undo":
                view["filters"] = view["filters"][:-1]
                view["page"] = 1
            elif action in {"add", "remove", "sortmenu"}:
                candidates = (
                    source.fields
                    if action == "sortmenu"
                    else [k for k in source.fields if k not in view["columns"]]
                    if action == "add"
                    else view["columns"]
                )
                operation = {"add": "addcol", "remove": "rmcol", "sortmenu": "sort"}[action]
                rows = [[(source.labels[c], f"dl:{key}:{operation}:{c}")] for c in candidates]
                rows.append([("🔙 فهرست", f"dl:{key}:show")])
                self._edit(call, "ستون موردنظر را انتخاب کنید:", keyboards.inline(*rows))
                return
            elif action in {"addcol", "rmcol"}:
                if value not in source.fields:
                    raise ValueError("ستون معتبر نیست")
                if action == "addcol" and value not in view["columns"]:
                    if len(view["columns"]) >= 5:
                        raise ValueError(
                            "حداکثر پنج ستون قابل نمایش است؛ ابتدا یک ستون را حذف کنید"
                        )
                    view["columns"].append(value)
                if action == "rmcol" and value in view["columns"] and len(view["columns"]) > 1:
                    view["columns"].remove(value)
            elif action == "search":
                restore_state = (
                    conversation.state
                    if conversation and conversation.state != "driving_search"
                    else None
                )
                restore_data = dict(conversation.data or {}) if restore_state else {}
                self.service.set_state(
                    db,
                    actor.telegram_id,
                    "driving_search",
                    list_key=key,
                    restore_state=restore_state,
                    restore_data=restore_data,
                )
                self._edit(
                    call,
                    "🔎 عبارت جست‌وجو را بفرستید.\n"
                    "مثال: <code>id=32</code> یا <code>name?ali</code> یا عبارت عمومی.\n"
                    "جست‌وجوی جدید داخل نتایج فعلی انجام می‌شود.",
                    keyboards.inline([("❌ انصراف", f"dl:{key}:show")]),
                )
                return
            rows, total, page, pages = self._driving_query(db, source, view)
            view["page"] = page
            self._driving_save(db, actor, key, view)
            prefix = f"dl:{key}"
            headers = [
                (
                    source.labels[c] + (" ↓" if view["desc"] else " ↑")
                    if c == view["sort"]
                    else source.labels[c],
                    f"{prefix}:sort:{c}",
                )
                for c in view["columns"]
            ]
            keyboard = [
                headers,
                [("➕ افزودن ستون", f"{prefix}:add"), ("➖ حذف ستون", f"{prefix}:remove")],
            ]
            actions = []
            if view["quick"] and source.quick and key in {"users", "admins"}:
                actions = (
                    [("🗓 شارژ", "recharge"), ("🚫 مسدود/آزاد", "ban"), ("🔄 توکن", "credentials")]
                    if key == "customers"
                    else [
                        ("➕ یک روز", "addday"),
                        ("🚫 مسدود/آزاد", "ban"),
                        ("🔄 توکن", "renew"),
                        ("🧹 مصرف صفر", "reset"),
                    ]
                    if key == "v2" or key.startswith("uv")
                    else [("🚫 مسدود/فعال", "block")]
                )
                name_field = "email" if source.panel else "name"
                keyboard[0] = [(source.labels[name_field], f"{prefix}:sort:{name_field}")] + [
                    (label, "noop") for label, _ in actions
                ]
            for row in rows:
                if actions:
                    keyboard.append(
                        [
                            (
                                cell_text(name_field, row[name_field], row),
                                f"{prefix}:open:{row['id']}",
                            )
                        ]
                        + [(label, f"{prefix}:qa:{row['id']}:{op}") for label, op in actions]
                    )
                    continue
                keyboard.append(
                    [
                        (cell_text(c, row[c], row),
                         f"{prefix}:cell:{row['id']}:{c}"
                         if view["quick"] and source.quick
                         else f"{prefix}:open:{row['id']}")
                        for c in view["columns"]
                    ]
                )
            keyboard.append(
                [
                    ("⏪ ۳", f"{prefix}:page:{max(1, page - 3)}"),
                    ("◀ ۱", f"{prefix}:page:{max(1, page - 1)}"),
                    (f"📄 {page}/{pages}", "noop"),
                    ("۱ ▶", f"{prefix}:page:{min(pages, page + 1)}"),
                    ("۳ ⏩", f"{prefix}:page:{min(pages, page + 3)}"),
                ]
            )
            keyboard.extend(
                [
                    [("🔎 جست‌وجو", f"{prefix}:search")],
                    [("↕ مرتب‌سازی", f"{prefix}:sortmenu"), ("🔃 تازه‌سازی", f"{prefix}:show")],
                ]
            )
            if source.quick:
                keyboard.append(
                    [("⚡ عملیات سریع " + ("✅" if view["quick"] else ""), f"{prefix}:quick")]
                )
            if view["filters"]:
                keyboard.append(
                    [("↩ جست‌وجوی قبلی", f"{prefix}:undo"), ("✖ پاک‌کردن جست‌وجو", f"{prefix}:reset")]
                )
            if key == "admins":
                keyboard.append([("➕ انتخاب کاربر برای ادمین‌شدن", "root:users:1")])
            if key == "friends":
                keyboard.append([("🔗 لینک دعوت من", "referral:share")])
            keyboard.append([("🔙 بازگشت", source.back)])
            search_text = " ← ".join(view["filters"]) or "همه داده‌ها"
            text = (
                f"<b>{html.escape(source.title)}</b>\n🔎 جست‌وجو: {html.escape(search_text)}\n"
                f"📊 تعداد داده‌ها: {total}\n📄 صفحه: {page}/{pages}"
            )
            if not rows:
                text += "\nنتیجه‌ای یافت نشد."
            if view["quick"] and source.quick:
                text += "\n⚡ عملیات سریع روشن است؛ روی مقدار ستون بزنید."
                if key.startswith("ap"):
                    text += "\n🔑 دسترسی: فعال/غیرفعال؛ فعال‌سازی اولیه با قیمت عمومی.\n💰 قیمت ادمین: ورود قیمت اختصاصی."
                elif source.panel:
                    text += "\n🚫 مسدود: تغییر وضعیت؛ مصرف: صفرکردن؛ انقضا: افزایش روز."
            self._edit(call, text, keyboards.inline(*keyboard))

    def _driving_send(self, message, key):
        sent = self.bot.send_message(message.chat.id, "📋 در حال نمایش فهرست…")
        self._driving_render(SimpleNamespace(message=sent, from_user=message.from_user), key)

    def _driving_callback(self, call):
        parts = call.data.split(":")
        key, action = parts[1:3]
        value = parts[3] if len(parts) > 3 else None
        if action not in {"open", "qa", "cell"}:
            self._driving_render(call, key, action, value)
            return
        with self.database.session() as db:
            actor = self.service.get_user(db, call.from_user.id)
            if not actor:
                raise ValueError("ابتدا وارد ربات شوید")
            source = self._driving_source(db, actor, key)
            item_id = value if key == "settings" else int(value)
            query = (
                select(source.fields["id"])
                .select_from(source.source)
                .where(source.scope, source.fields["id"] == item_id)
            )
            if source.panel:
                with self.panel.engine.connect() as connection:
                    found = connection.scalar(query)
            else:
                found = db.scalar(query)
            if found is None:
                raise ValueError("ردیف پیدا نشد یا به آن دسترسی ندارید")
            if key == "settings":
                setting = db.get(BotSetting, item_id)
                self._edit(
                    call,
                    f"⚙ <code>{html.escape(item_id)}</code>\n\n"
                    f"{html.escape(setting.value)}\n\n"
                    f"ویرایش: <code>/set {html.escape(item_id)} مقدار</code>",
                    keyboards.inline([("🔙 تنظیمات", "dl:settings:show")]),
                )
                return
            target = source.detail.format(id=item_id)
            self._save_setting(db, f"driving_origin:{actor.id}", key)
            result = None
            if action == "cell":
                view = self._driving_load(db, actor, key, source)
                if source.quick and view["quick"]:
                    field = parts[4]
                    if field not in source.fields:
                        raise ValueError("ستون معتبر نیست")
                    result = self._driving_cell(call, db, actor, key, item_id, field)
                    if key == "customers":
                        operation = {"banned": "ban", "token": "credentials", "uuid": "credentials",
                                     "expired_at": "recharge", "transfer_enable": "recharge"}.get(field)
                        if operation:
                            target = f"resell:{operation}:{item_id}"
        if result == "prompt":
            return
        if result == "changed":
            self._driving_render(call, key)
            return
        if action == "qa":
            op = parts[4]
            allowed = (
                {
                    "v2": {"addday", "ban", "renew", "reset"},
                    "users": {"block"},
                    "admins": {"block"},
                    "customers": {"recharge", "ban", "credentials"},
                    "subs": {"recharge", "ban", "credentials"},
                }
            ).get("v2" if key.startswith("uv") else key, set())
            if op not in allowed:
                raise ValueError("عملیات مجاز نیست")
            target = f"{'v2' if key == 'v2' or key.startswith('uv') else 'user' if key in {'users', 'admins'} else 'resell'}:{op}:{item_id}"
        self._dispatch_callback_with_data(call, target)
        if action == "qa" and op not in {"recharge", "block"}:
            self._driving_render(call, key)

    def _driving_cell(self, call, db, actor, key, item_id, field):
        prompt = None
        if re.fullmatch(r"ap\d+", key) and field in {"assigned", "admin_price"}:
            admin_id = int(key[2:])
            assignment = db.scalar(select(AdminPrice).where(
                AdminPrice.admin_id == admin_id, AdminPrice.product_id == item_id
            ).with_for_update())
            if field == "assigned":
                if assignment:
                    db.delete(assignment)
                else:
                    product = db.get(Product, item_id)
                    db.add(AdminPrice(admin_id=admin_id, product_id=item_id, price=product.price))
                return "changed"
            prompt = "💰 قیمت اختصاصی این بسته را به تومان بفرستید (صفر یا بیشتر)."
        elif key == "v2" or re.fullmatch(r"uv\d+", key):
            if field == "banned":
                account = self.panel.get_user(item_id)
                if not account:
                    raise ValueError("اکانت پیدا نشد")
                self.panel.set_banned(item_id, not bool(account.get("banned")))
                return "changed"
            if field in {"u", "d", "used"}:
                self.panel.reset_usage(item_id)
                return "changed"
            if field in {"token", "uuid"}:
                self.panel.renew_credentials(item_id)
                return "changed"
            prompt = {
                "expired_at": "🗓 چند روز به اعتبار اضافه شود؟ عددی از ۱ تا ۳۶۵۰ بفرستید.",
                "transfer_enable": "📦 چند گیگابایت به حجم اضافه شود؟ عددی از ۱ تا ۱۰۰۰۰۰ بفرستید.",
                "device_limit": "👥 تعداد اتصال را بفرستید (۰ = نامحدود، حداکثر ۱۰۰۰).",
                "remarks": "📝 یادداشت جدید را بفرستید (حداکثر ۱۰۰۰ نویسه).",
            }.get(field)
        if not prompt:
            return None
        self.service.set_state(db, actor.telegram_id, "driving_edit", list_key=key,
                               item_id=item_id, field=field,
                               chat_id=call.message.chat.id, message_id=call.message.message_id)
        self._edit(call, prompt, keyboards.inline([("❌ انصراف", f"dl:{key}:show")]))
        return "prompt"

    def _driving_edit_message(self, db, message, payload):
        actor = self.service.get_user(db, message.from_user.id)
        key, item_id, field = payload["list_key"], int(payload["item_id"]), payload["field"]
        source = self._driving_source(db, actor, key)
        if not source.quick or not self._driving_load(db, actor, key, source)["quick"]:
            raise ValueError("عملیات سریع خاموش است؛ دوباره از فهرست انتخاب کنید")
        if message.chat.id != payload["chat_id"]:
            raise ValueError("پاسخ را در همان گفت‌وگو بفرستید")
        query = select(source.fields["id"]).select_from(source.source).where(
            source.scope, source.fields["id"] == item_id)
        if source.panel:
            with self.panel.engine.connect() as connection:
                found = connection.scalar(query)
        else:
            found = db.scalar(query)
        if found is None:
            raise ValueError("ردیف دیگر در دسترس نیست")
        raw = message.text.strip().replace(",", "").replace("٬", "")
        limits = {"admin_price": (0, 10**12), "expired_at": (1, 3650),
                  "transfer_enable": (1, 100000), "device_limit": (0, 1000)}
        if field == "remarks":
            if not 1 <= len(message.text.strip()) <= 1000:
                self.bot.reply_to(message, "یادداشت باید بین ۱ تا ۱۰۰۰ نویسه باشد.")
                return
        elif field not in limits or not raw.isdecimal() or not limits[field][0] <= int(raw) <= limits[field][1]:
            self.bot.reply_to(message, "عدد معتبر در محدوده اعلام‌شده بفرستید.")
            return
        if re.fullmatch(r"ap\d+", key) and field == "admin_price":
            admin_id = int(key[2:])
            assignment = db.scalar(select(AdminPrice).where(
                AdminPrice.admin_id == admin_id, AdminPrice.product_id == item_id
            ).with_for_update())
            if assignment:
                assignment.price = int(raw)
            else:
                db.add(AdminPrice(admin_id=admin_id, product_id=item_id, price=int(raw)))
        elif key == "v2" or re.fullmatch(r"uv\d+", key):
            if field == "remarks":
                self.panel.update_remarks(item_id, message.text.strip())
            elif field == "expired_at":
                self.panel.add_days(item_id, int(raw))
            elif field == "transfer_enable":
                self.panel.add_transfer(item_id, int(raw))
            elif field == "device_limit":
                self.panel.set_device_limit(item_id, int(raw) or None)
            else:
                raise ValueError("ویرایش این ستون مجاز نیست")
        else:
            raise ValueError("ویرایش این ستون مجاز نیست")
        self.service.clear_state(db, actor.telegram_id)
        db.flush()
        origin = SimpleNamespace(from_user=message.from_user, message=SimpleNamespace(
            chat=message.chat, message_id=payload["message_id"]))
        self._driving_render(origin, key, db_override=db)

    def _dispatch_callback_with_data(self, call, target):
        original = call.data
        try:
            call.data = target
            self._dispatch_callback(call)
        finally:
            call.data = original

    def _driving_search_message(self, db, message, payload):
        actor = self.service.get_user(db, message.from_user.id)
        key = payload["list_key"]
        source = self._driving_source(db, actor, key)
        view = self._driving_load(db, actor, key, source)
        query = message.text.strip()[:100]
        if not query or len(view["filters"]) >= 8:
            self.bot.reply_to(message, "حداکثر هشت فیلتر مجاز است؛ جست‌وجو را پاک کنید.")
            return
        try:
            search_predicate(source.fields, query)
        except ValueError as exc:
            self.bot.reply_to(message, str(exc))
            return
        view["filters"].append(query)
        view["page"] = 1
        self._driving_save(db, actor, key, view)
        if payload.get("restore_state"):
            self.service.set_state(
                db, actor.telegram_id, payload["restore_state"], **payload.get("restore_data", {})
            )
        else:
            self.service.clear_state(db, actor.telegram_id)
        self.bot.reply_to(
            message,
            "✅ فیلتر به نتایج فعلی اضافه شد.",
            reply_markup=keyboards.inline([("📋 نمایش نتایج", f"dl:{key}:show")]),
        )
