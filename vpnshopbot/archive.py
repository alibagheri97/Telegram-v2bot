"""Reversible, recoverable expiry archiving without deleting panel or bot records."""

from __future__ import annotations

import html
import secrets
import time
from datetime import UTC, datetime

from sqlalchemy import select

from . import keyboards
from .audit import record_action
from .models import (
    ConversationState,
    EmergencyAccount,
    ExpiryRetention,
    ExpiryWatch,
    User,
    UserStatus,
    V2AccountLink,
    V2Archive,
)
from .service_ui import render_services

ARCHIVED_STATES = ("pending", "archived")
SNAPSHOT_FIELDS = (
    "id",
    "email",
    "telegram_id",
    "u",
    "d",
    "transfer_enable",
    "expired_at",
    "device_limit",
    "banned",
    "remarks",
    "plan_id",
    "created_at",
    "updated_at",
)


def is_archived(db, v2_id):
    row = db.get(V2Archive, v2_id)
    return bool(row and row.status in ARCHIVED_STATES)


class ArchiveService:
    def __init__(self, database, panel):
        self.database, self.panel = database, panel

    def check(self, link_id, days):
        from .emergency import _operation_lock
        from .expiry import account_expiration, account_signature, root_managed

        with _operation_lock:
            now = int(time.time())
            with self.database.session() as db:
                link = db.scalar(
                    select(V2AccountLink).where(V2AccountLink.id == link_id).with_for_update()
                )
                if not link:
                    return False
                row = db.get(V2Archive, link.v2_user_id)
                if row and row.status == "archived":
                    return True
                owner = db.get(User, link.user_id)
                if (
                    not owner
                    or owner.status != UserStatus.ACTIVE
                    or not root_managed(db, link, owner)
                ):
                    return False
                pending = db.get(EmergencyAccount, link.v2_user_id)
                if pending and pending.pending_event_id:
                    return False
                account = self.panel.get_user(link.v2_user_id)
                if not account:
                    return False
                tracker = db.get(ExpiryRetention, link.v2_user_id)
                if not tracker:
                    tracker = ExpiryRetention(v2_user_id=link.v2_user_id, signature="")
                    db.add(tracker)
                reason = account_expiration(account)
                signature = account_signature(account)
                if not reason:
                    tracker.expired_since, tracker.signature = None, signature
                    if row and row.status == "pending":
                        row.status = "restored"
                    return False
                if not row or row.status != "pending":
                    expiry = int(account.get("expired_at") or 0)
                    since = expiry if expiry and expiry <= now else now
                    since = max(since, int(tracker.restored_at or 0))
                    if tracker.signature != signature or tracker.expired_since is None:
                        tracker.expired_since = since
                    tracker.signature = signature
                    if not days or now - tracker.expired_since < days * 86400:
                        return False
                    snapshot = {key: account.get(key) for key in SNAPSHOT_FIELDS}
                    if not row:
                        row = V2Archive(
                            v2_user_id=link.v2_user_id,
                            user_id=owner.id,
                            email=str(account.get("email") or ""),
                            label=link.label,
                            status="pending",
                            generation=1,
                            snapshot=snapshot,
                            expired_since=tracker.expired_since,
                        )
                        db.add(row)
                    else:
                        row.status, row.snapshot = "pending", snapshot
                        row.email, row.label = str(account.get("email") or ""), link.label
                        row.expired_since, row.generation = (
                            tracker.expired_since,
                            row.generation + 1,
                        )
                v2_id, snapshot = row.v2_user_id, dict(row.snapshot)
            # The durable marker is committed first, blocking renewals until reconciliation.
            applied = self.panel.archive_if_unchanged(v2_id, snapshot)
            with self.database.session() as db:
                row = db.scalar(
                    select(V2Archive).where(V2Archive.v2_user_id == v2_id).with_for_update()
                )
                if row.status != "pending":
                    return row.status == "archived"
                if not applied:
                    row.status = "restored"
                    tracker = db.get(ExpiryRetention, v2_id)
                    tracker.expired_since = None
                    return False
                row.status, row.archived_at = "archived", datetime.now(UTC)
                watch = db.get(ExpiryWatch, v2_id)
                if watch:
                    watch.was_expired = False
                record_action(
                    db,
                    row.user_id,
                    "v2.archived",
                    "🗃 بایگانی اشتراک تمدیدن‌شده",
                    source_key=f"archive:{v2_id}:{row.generation}",
                    details={
                        "v2_user_id": v2_id,
                        "reason": "expired_without_recharge",
                        "expired_since": row.expired_since,
                        "snapshot": snapshot,
                    },
                )
            return True


class ArchiveMixin:
    def _archive_callback(self, call, data):
        from .emergency import _operation_lock
        from .expiry import save_setting

        if not self._authorized(call.from_user.id, root=True):
            raise ValueError("دسترسی مدیر اصلی لازم است")
        parts, action = data.split(":"), data.split(":")[1]
        if action in {"grace", "gracesave", "cancel"}:
            with self.database.session() as db:
                if action == "grace":
                    self.service.set_state(
                        db, call.from_user.id, "expiry_archive_days", chat_id=call.message.chat.id
                    )
                    self._edit(
                        call,
                        "🗓 مهلت بایگانی خودکار را به روز بفرستید (۱ تا ۳۶۵۰).\n"
                        "۰ = غیرفعال کردن بایگانی خودکار. حساب‌ها و سوابق به‌صورت دائمی حذف نمی‌شوند.",
                        keyboards.inline([("❌ انصراف", "archive:cancel")]),
                    )
                    return
                if action == "gracesave":
                    state = db.scalar(
                        select(ConversationState)
                        .where(ConversationState.telegram_id == call.from_user.id)
                        .with_for_update()
                    )
                    if (
                        not state
                        or state.state != "expiry_archive_confirm"
                        or state.data.get("nonce") != parts[2]
                        or state.data.get("chat_id") != call.message.chat.id
                    ):
                        raise ValueError("تأیید قدیمی یا نامعتبر است")
                    save_setting(db, "service:expire:archive_after_days", state.data["days"])
                self.service.clear_state(db, call.from_user.id)
            render_services(self, call, "expire")
            return
        v2_id = int(parts[2])
        with _operation_lock, self.database.session() as db:
            row = db.scalar(
                select(V2Archive).where(V2Archive.v2_user_id == v2_id).with_for_update()
            )
            if not row:
                raise ValueError("سابقه بایگانی پیدا نشد")
            if action == "restoreask":
                self._edit(
                    call,
                    "♻️ اکانت از بایگانی به فهرست اصلی برگردد؟\n"
                    "اعتبار یا کیف پول تغییر نمی‌کند و بلاک تا تمدید/آزادسازی دستی باقی می‌ماند.\n"
                    "برای تمدیدنشده‌ها مهلت بایگانی از زمان بازیابی دوباره محاسبه می‌شود.",
                    keyboards.inline(
                        [("✅ تأیید بازیابی", f"archive:restore:{v2_id}:{row.generation}")],
                        [("❌ انصراف", f"archive:view:{v2_id}")],
                    ),
                )
                return
            if action == "restore":
                if row.status != "archived" or len(parts) != 4 or int(parts[3]) != row.generation:
                    raise ValueError("این درخواست بازیابی قدیمی یا نامعتبر است")
                if not self.panel.get_user(v2_id):
                    raise ValueError(
                        "رکورد پنل خارج از ربات حذف شده؛ سابقه محفوظ است اما بازیابی خودکار ممکن نیست"
                    )
                row.status, row.restored_at = "restored", datetime.now(UTC)
                tracker = db.get(ExpiryRetention, v2_id)
                if tracker:
                    tracker.restored_at, tracker.expired_since = int(time.time()), None
                record_action(
                    db,
                    row.user_id,
                    "v2.restored",
                    "♻️ بازیابی اکانت بایگانی‌شده",
                    actor_id=call.from_user.id,
                    source_key=f"restore:{v2_id}:{row.generation}",
                    details={"v2_user_id": v2_id},
                )
            elif action != "view":
                raise ValueError("عملیات بایگانی معتبر نیست")
            snapshot = row.snapshot or {}
            text = (
                f"<b>🗃 سابقه بایگانی #{v2_id}</b>\n"
                f"📧 {html.escape(row.email)}\n🏷 {html.escape(row.label)}\n"
                f"🚦 {'بایگانی‌شده' if row.status == 'archived' else 'در حال بایگانی' if row.status == 'pending' else 'بازیابی‌شده'}\n"
                f"📅 زمان بایگانی (UTC): {row.archived_at or '—'}\n"
                f"📝 {html.escape(str(snapshot.get('remarks') or '—')[:700])}\n"
                "اطلاعات اکانت، ارتباط مالک و سوابق پرداخت/فعالیت حفظ شده‌اند."
            )
            rows = [
                [("📜 تاریخچه مالک", f"user:history:{row.user_id}")],
                [("💵 سوابق پرداخت", f"dl:vpay{v2_id}:show")],
            ]
            panel_exists = self.panel.get_user(v2_id) is not None
            if panel_exists:
                rows.insert(0, [("👁 اطلاعات اکانت", f"v2:view:{v2_id}")])
            if row.status == "archived" and panel_exists:
                rows.append([("♻️ بازیابی از بایگانی", f"archive:restoreask:{v2_id}")])
            rows.append([("🔙 بایگانی‌ها", "dl:archives:show")])
            self._edit(call, text, keyboards.inline(*rows))

    def _archive_config_message(self, db, message, payload):
        if (
            not self._authorized(message.from_user.id, root=True)
            or payload.get("chat_id") != message.chat.id
        ):
            return
        raw = message.text.strip()
        if not raw.isdecimal() or not 0 <= int(raw) <= 3650:
            self.bot.reply_to(message, "عدد بین صفر و ۳۶۵۰ بفرستید.")
            return
        nonce, days = secrets.token_hex(4), int(raw)
        self.service.set_state(
            db,
            message.from_user.id,
            "expiry_archive_confirm",
            chat_id=message.chat.id,
            nonce=nonce,
            days=days,
        )
        self.bot.reply_to(
            message,
            f"🗓 مهلت بایگانی: {days} روز {'(غیرفعال)' if not days else ''}\n"
            "کاهش مهلت می‌تواند در بررسی بعدی حساب‌های روتِ ازقبلمنقضی را بایگانی کند. تأیید می‌کنید؟",
            reply_markup=keyboards.inline(
                [("✅ تأیید", f"archive:gracesave:{nonce}")], [("❌ انصراف", "archive:cancel")]
            ),
        )
