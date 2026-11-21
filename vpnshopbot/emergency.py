"""One-day advances and durable, debt-adjusted 31-day renewal for root accounts."""

from __future__ import annotations

import html
import logging
import secrets
import threading
import time

from sqlalchemy import select
from telebot import types

from . import keyboards
from .archive import is_archived
from .audit import record_action
from .expiry import account_signature, root_managed, save_setting, setting
from .models import EmergencyAccount, EmergencyEvent, User, UserStatus, V2AccountLink

log = logging.getLogger(__name__)
_operation_lock = threading.RLock()
_scan_lock = threading.Lock()


def monthly_info(db, v2_id):
    link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
    owner = db.get(User, link.user_id) if link else None
    if not owner or not root_managed(db, link, owner):
        return None
    balance = db.get(EmergencyAccount, v2_id)
    return {
        "used": balance.days_used if balance else 0,
        "generation": balance.generation if balance else 0,
        "pending": bool(balance and balance.pending_event_id),
    }


class EmergencyService:
    def __init__(self, bot, database, panel):
        self.bot, self.database, self.panel = bot, database, panel

    def _recover(self, v2_id):
        with self.database.session() as db:
            balance = db.get(EmergencyAccount, v2_id)
            if not balance or not balance.pending_event_id:
                return
            event = db.get(EmergencyEvent, balance.pending_event_id)
            if not event:
                raise ValueError("سند عملیات معلق پیدا نشد؛ بررسی مدیر لازم است")
            event_id = event.id
            operation = {
                "before": event.expiry_before,
                "after": event.expiry_after,
                "transfer_enable": event.transfer_enable,
                "monthly": event.kind == "monthly",
            }
        self.panel.apply_emergency_operation(v2_id, **operation)
        with self.database.session() as db:
            balance = db.scalar(
                select(EmergencyAccount)
                .where(EmergencyAccount.v2_user_id == v2_id)
                .with_for_update()
            )
            event = db.get(EmergencyEvent, event_id)
            if balance.pending_event_id != event_id:
                return
            if (
                balance.days_used != event.debt_before
                or balance.generation != event.generation_before
            ):
                raise ValueError("تعارض شمارنده اضطراری؛ بررسی مدیر لازم است")
            balance.days_used = event.debt_after
            if event.kind == "monthly":
                balance.generation += 1
            balance.pending_event_id, event.status = None, "applied"
            link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
            if link:
                record_action(
                    db,
                    link.user_id,
                    "v2.emergency_granted" if event.kind == "emergency" else "v2.monthly_renewed",
                    "🆘 شارژ اضطراری یک‌روزه"
                    if event.kind == "emergency"
                    else "🗓 تمدید ماهانه با تسویه اضطراری",
                    actor_id=event.actor_telegram_id,
                    source_key=f"emergency:{event.id}",
                    details={
                        "v2_user_id": v2_id,
                        "days_added": event.days_added,
                        "emergency_before": event.debt_before,
                        "emergency_after": event.debt_after,
                    },
                )

    def operate(
        self,
        v2_id,
        *,
        monthly=False,
        generation=None,
        actor_id=None,
        expected_signature=None,
        require_expired=False,
    ):
        with _operation_lock:
            with self.database.session() as db:
                if is_archived(db, v2_id):
                    if monthly:
                        raise ValueError("اکانت بایگانی شده؛ ابتدا آن را بازیابی کنید")
                    return None
                link = db.scalar(select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id))
                owner = db.get(User, link.user_id) if link else None
                if (
                    not owner
                    or owner.status != UserStatus.ACTIVE
                    or not root_managed(db, link, owner)
                ):
                    if monthly:
                        raise ValueError("عملیات فقط برای اکانت ساخته‌شده توسط مدیر اصلی مجاز است")
                    return None
            self._recover(v2_id)
            with self.database.session() as db:
                link = db.scalar(
                    select(V2AccountLink).where(V2AccountLink.v2_user_id == v2_id).with_for_update()
                )
                owner = db.get(User, link.user_id) if link else None
                if is_archived(db, v2_id):
                    if monthly:
                        raise ValueError("اکانت بایگانی شده؛ ابتدا آن را بازیابی کنید")
                    return None
                if (
                    not owner
                    or owner.status != UserStatus.ACTIVE
                    or not root_managed(db, link, owner)
                ):
                    if monthly:
                        raise ValueError("عملیات فقط برای اکانت ساخته‌شده توسط مدیر اصلی مجاز است")
                    return None
                account = self.panel.get_user(v2_id)
                if not account:
                    raise ValueError("اکانت پیدا نشد")
                balance = db.get(EmergencyAccount, v2_id)
                if not balance:
                    balance = EmergencyAccount(v2_user_id=v2_id, days_used=0, generation=0)
                    db.add(balance)
                if balance.pending_event_id:
                    raise ValueError("عملیات قبلی هنوز معلق است")
                if generation is not None and balance.generation != generation:
                    raise ValueError("این دکمه مربوط به دوره قبلی است")
                expiry = int(account.get("expired_at") or 0)
                now = int(time.time())
                if not monthly:
                    if not expiry or expiry > now or balance.days_used >= 31:
                        return None
                elif require_expired and not balance.days_used:
                    from .expiry import account_expiration

                    if not account_expiration(account) or (
                        expected_signature is not None
                        and account_signature(account) != expected_signature
                    ):
                        raise ValueError("اکانت قبلاً تمدید شده یا وضعیتش تغییر کرده است")
                days = max(0, 31 - balance.days_used) if monthly else 1
                event_id = secrets.token_hex(16)
                event = EmergencyEvent(
                    id=event_id,
                    v2_user_id=v2_id,
                    kind="monthly" if monthly else "emergency",
                    status="pending",
                    expiry_before=expiry,
                    expiry_after=max(expiry, now) + days * 86400,
                    transfer_enable=int(account.get("transfer_enable") or 0),
                    debt_before=balance.days_used,
                    debt_after=0 if monthly else balance.days_used + 1,
                    generation_before=balance.generation,
                    days_added=days,
                    actor_telegram_id=actor_id,
                    announced=False,
                )
                db.add(event)
                balance.pending_event_id = event_id
                result = {
                    "days": days,
                    "used": balance.days_used,
                    "generation": balance.generation,
                    "event_id": event_id,
                }
            # Intent commits before the panel write. Recovery replays its exact target, never another day.
            self._recover(v2_id)
            return result

    def run(self):
        counts = {"granted": 0, "sent": 0, "errors": 0}
        if not _scan_lock.acquire(blocking=False):
            return {**counts, "busy": True}
        try:
            with self.database.session() as db:
                if setting(db, "service:emergency:enabled", "0") != "1":
                    return {**counts, "paused": True}
                chat, topic = (
                    int(setting(db, "emergency_group_id", "0")),
                    int(setting(db, "emergency_topic_id", "0")),
                )
                if not chat or not topic:
                    return {**counts, "topic_pending": True}
                ids = list(db.scalars(select(V2AccountLink.v2_user_id)))
            for v2_id in ids:
                try:
                    result = self.operate(v2_id)
                    counts["granted"] += int(result is not None)
                except Exception:
                    counts["errors"] += 1
                    log.exception("emergency advance failed for account %s", v2_id)
            with self.database.session() as db:
                events = list(
                    db.scalars(
                        select(EmergencyEvent.id)
                        .where(
                            EmergencyEvent.kind == "emergency",
                            EmergencyEvent.status == "applied",
                            EmergencyEvent.announced.is_(False),
                        )
                        .order_by(EmergencyEvent.created_at, EmergencyEvent.id)
                    )
                )
            for event_id in events:
                if counts["sent"] >= 2:
                    break
                try:
                    with self.database.session() as db:
                        event = db.get(EmergencyEvent, event_id)
                        link = db.scalar(
                            select(V2AccountLink).where(
                                V2AccountLink.v2_user_id == event.v2_user_id
                            )
                        )
                        owner = db.get(User, link.user_id) if link else None
                        if is_archived(db, event.v2_user_id):
                            continue
                        if (
                            not owner
                            or owner.status != UserStatus.ACTIVE
                            or not root_managed(db, link, owner)
                        ):
                            continue
                        balance = db.get(EmergencyAccount, event.v2_user_id)
                        if event.generation_before != balance.generation:
                            event.announced = True
                            continue
                        url = f"https://t.me/{self.bot.get_me().username}?start=account_{event.v2_user_id}"
                        markup = keyboards.inline(
                            [
                                (
                                    "🗓 تمدید ماهانه ۳۱ روزه با کسر اضطراری",
                                    f"emergency:renew:{event.v2_user_id}:{balance.generation}",
                                )
                            ]
                        )
                        markup.row(types.InlineKeyboardButton("👁 وضعیت و مدیریت اکانت", url=url))
                        sent = self.bot.send_message(
                            chat,
                            f"🆘 <b>شارژ اضطراری یک‌روزه انجام شد</b>\n🆔 #{event.v2_user_id}\n"
                            f"👤 {html.escape(link.label)}\n"
                            f"📊 مجموع روزهای پیش‌پرداخت این دوره: {balance.days_used}\n"
                            f"🗓 تمدید ماهانه بعدی: {max(0, 31 - balance.days_used)} روز\n"
                            "این شارژ فقط اعتبار زمانی را اضافه می‌کند؛ مصرف و مسدودی تغییر نمی‌کند.",
                            message_thread_id=topic,
                            parse_mode="HTML",
                            reply_markup=markup,
                        )
                        event.announced, event.message_id = True, sent.message_id
                        counts["sent"] += 1
                except Exception:
                    counts["errors"] += 1
                    log.exception("emergency announcement failed for event %s", event_id)
            with self.database.session() as db:
                save_setting(db, "service:emergency:last_run", int(time.time()))
                save_setting(
                    db,
                    "service:emergency:last_result",
                    f"شارژ: {counts['granted']} | اعلان: {counts['sent']} | خطا: {counts['errors']}",
                )
            return counts
        finally:
            _scan_lock.release()
