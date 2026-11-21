"""A compact services index and independent detail pages (no job mutations here)."""

import html
from datetime import datetime
from zoneinfo import ZoneInfo

from sqlalchemy import func, select

from . import keyboards
from .models import EmergencyAccount, ExpiryWatch, V2AccountLink, V2Archive

SERVICES = {
    "expire": ("⏳ اعلان انقضا", "0"),
    "emergency": ("🆘 شارژ اضطراری", "0"),
    "auto": ("🔁 تمدید خودکار کیف پول", "1"),
}


def render_services(controller, call, selected=None):
    from .expiry import setting

    if selected is not None and selected not in SERVICES:
        raise ValueError("سرویس پیدا نشد")
    with controller.database.session() as db:
        flags = {
            key: setting(db, f"service:{'auto_renew' if key == 'auto' else key}:enabled", default)
            == "1"
            for key, (_, default) in SERVICES.items()
        }
        if selected is None:
            rows = [
                [(f"{title} · {'🟢 فعال' if flags[key] else '⏸ متوقف'}", f"expiry:service:{key}")]
                for key, (title, _) in SERVICES.items()
            ]
            rows.append([("🔃 تازه‌سازی", "expiry:home"), ("🔙 داشبورد", "root:home")])
            controller._edit(
                call,
                "<b>⚙️ مدیریت سرویس‌ها</b>\n\nسرویس موردنظر را انتخاب کنید:",
                keyboards.inline(*rows),
            )
            return
        title, _ = SERVICES[selected]
        enabled = flags[selected]
        lines = [f"<b>{title}</b>", f"🚦 وضعیت: {'🟢 فعال' if enabled else '⏸ متوقف'}"]
        rows = []
        if selected == "auto":
            count = (
                db.scalar(
                    select(func.count())
                    .select_from(V2AccountLink)
                    .where(V2AccountLink.auto_renewal.is_(True))
                )
                or 0
            )
            lines += [
                "⏱ فاصله بررسی: ۱۵ دقیقه",
                f"📦 اشتراک‌های دارای تمدید خودکار: {count}",
                "",
                "تمدید با کسر از کیف پول انجام می‌شود؛ قیمت اختصاصی ادمین و حد منفی مجاز او رعایت می‌شود.",
                "فقط اشتراک‌هایی تمدید می‌شوند که مالکشان تمدید خودکار را فعال کرده باشد.",
            ]
            rows.append([("⏸ توقف سرویس" if enabled else "▶️ فعال‌سازی سرویس", "expiry:auto")])
        else:
            prefix = "expired" if selected == "expire" else "emergency"
            group, topic = (
                setting(db, f"{prefix}_group_id", "0"),
                setting(db, f"{prefix}_topic_id", "0"),
            )
            ready = bool(int(group) and int(topic))
            lines += [
                "⏱ فاصله بررسی: ۱۵ ثانیه",
                "",
                f"📍 گروه: <code>{int(group)}</code> | تاپیک: <code>{int(topic)}</code>"
                if ready
                else "📍 تاپیک هنوز متصل نشده است.",
            ]
            stamp = int(setting(db, f"service:{selected}:last_run", "0"))
            if stamp:
                zone = ZoneInfo(getattr(controller.settings, "timezone", "Asia/Tehran"))
                lines.append(
                    f"🕒 آخرین بررسی: {datetime.fromtimestamp(stamp, zone).strftime('%Y/%m/%d %H:%M:%S')}"
                )
            lines.append(
                "📊 "
                + html.escape(setting(db, f"service:{selected}:last_result", "هنوز اجرا نشده"))
            )
            if selected == "expire":
                archive_days = int(setting(db, "service:expire:archive_after_days", "3"))
                archives = db.scalar(select(func.count()).select_from(V2Archive).where(V2Archive.status.in_(["pending", "archived"]))) or 0
                count = (
                    db.scalar(
                        select(func.count())
                        .select_from(ExpiryWatch)
                        .where(ExpiryWatch.was_expired.is_(True))
                    )
                    or 0
                )
                lines += [
                    f"🔴 اشتراک‌های پایان‌یافته: {count}",
                    "",
                    "فقط اکانت‌های ساخته‌شده توسط روت به تاپیک می‌روند؛ ادمین‌ها و خریداران اعلان خصوصی می‌گیرند.",
                    f"🗃 بایگانی خودکار: {str(archive_days) + ' روز پس از انقضا' if archive_days else 'غیرفعال'}",
                    f"📚 اکانت‌های بایگانی: {archives}",
                    "بایگانی فقط برای اکانت‌های ساخته‌شده توسط روت است؛ اطلاعات و تاریخچه حذف نمی‌شوند.",
                    "انقضای زمانی از تاریخ پایان اعتبار و اتمام حجم از اولین مشاهده محاسبه می‌شود.",
                ]
                if not ready:
                    lines.append(
                        "⚠️ بدون تاپیک، اعلان‌های روت ارسال نمی‌شوند؛ اعلان خصوصی همچنان ممکن است."
                    )
                rows += [
                    [("⏸ توقف سرویس" if enabled else "▶️ فعال‌سازی سرویس", "expiry:toggle")],
                    [("🔎 یک بار بررسی اکنون", "expiry:run")],
                    [("📋 اشتراک‌های پایان‌یافته", "dl:expired:show")],
                    [("🗓 تغییر مهلت بایگانی", "archive:grace")],
                    [("🗃 فهرست بایگانی‌ها", "dl:archives:show")],
                ]
            else:
                debt = db.scalar(select(func.sum(EmergencyAccount.days_used))) or 0
                lines += [
                    f"🆘 روزهای اضطراری تسویه‌نشده: {debt}",
                    "",
                    "فقط اکانت‌های ساخته‌شده توسط روت: یک روز پیش‌پرداخت پس از انقضای زمانی.",
                    "تمدید ماهانه = ۳۱ منهای روزهای اضطراری؛ سپس شمارنده صفر می‌شود. سقف پیش‌پرداخت: ۳۱ روز.",
                ]
                if enabled and not ready:
                    lines.append("⚠️ سرویس تا اتصال تاپیک، شارژ جدید انجام نمی‌دهد.")
                rows += [
                    [("⏸ توقف سرویس" if enabled else "▶️ فعال‌سازی سرویس", "emergency:toggle")],
                    [("🔎 بررسی اکنون", "emergency:run")],
                    [("📋 شمارنده‌های اضطراری", "dl:emergency:show")],
                ]
            rows.append([("📍 راهنمای تنظیم تاپیک", f"expiry:topic:{selected}")])
        rows += [
            [("🔃 تازه‌سازی", f"expiry:service:{selected}")],
            [("🔙 فهرست سرویس‌ها", "expiry:home")],
        ]
        controller._edit(call, "\n".join(lines), keyboards.inline(*rows))


def render_service_topic(controller, call, service):
    if service not in {"expire", "emergency"}:
        raise ValueError("این سرویس تاپیک ندارد")
    name = "expired" if service == "expire" else "emergency-trigger"
    controller._edit(
        call,
        f"<b>📍 تنظیم تاپیک {SERVICES[service][0]}</b>\n\n"
        "ربات و حساب روت باید مدیر گروه باشند. داخل تاپیک موردنظر بفرستید:\n"
        f"<code>/set-topic {name}</code>\n\n"
        f"حذف اتصال: <code>/unset-topic {name}</code>\n"
        "فهرست اتصال‌ها: <code>/topics</code>\nراهنمای گروه: <code>/help</code>",
        keyboards.inline([("🔙 جزئیات سرویس", f"expiry:service:{service}")]),
    )
