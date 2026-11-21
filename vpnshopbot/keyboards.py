from telebot import types


def inline(*rows: list[tuple[str, str]]) -> types.InlineKeyboardMarkup:
    keyboard = types.InlineKeyboardMarkup()
    for row in rows:
        keyboard.row(*(types.InlineKeyboardButton(text, callback_data=data) for text, data in row))
    return keyboard


def create_account() -> types.InlineKeyboardMarkup:
    return inline([("🎁 ساخت حساب رایگان", "register:start")])


def user_home() -> types.InlineKeyboardMarkup:
    return inline(
        [("🛒 خرید اشتراک جدید", "buy:start")],
        [("📦 اشتراک‌های من", "subs:list:1"), ("💳 کیف پول", "wallet:show")],
        [("📚 راهنمای اتصال", "guide:start"), ("🎁 دعوت دوستان", "referral:show")],
        [("💬 پشتیبانی", "support:show")],
        [("🧾 پیگیری و لغو سفارش‌ها", "orders:open")],
    )


def root_home() -> types.InlineKeyboardMarkup:
    return inline(
        [("🗄️ دیتابیس V2", "root:v2:1"), ("👥 کاربران ربات", "root:users:1")],
        [("⚙️ تنظیمات", "root:settings"), ("📦 مدیریت بسته‌ها", "root:products")],
        [("🧑‍💼 مدیریت ادمین‌ها", "catalog:admins:1")],
        [("⚙️ مدیریت سرویس‌ها", "expiry:home")],
        [("📣 اعلان‌ها", "root:notices"), ("📊 گزارش مالی", "root:finance")],
    )


def admin_home() -> types.InlineKeyboardMarkup:
    return inline(
        [("➕ ساخت اکانت", "admin:create"), ("📋 لیست کاربران", "admin:users:1")],
        [("💳 کیف پول", "wallet:show"), ("🎨 برند شخصی", "admin:brand")],
        [("🏠 داشبورد کاربری", "home:user")],
    )


def buy_types() -> types.InlineKeyboardMarkup:
    return inline(
        [("♾️ نامحدود زمانی", "buy:type:time")],
        [("📊 حجمی (گیگابایت)", "buy:type:data")],
        [("❌ انصراف", "home:user")],
    )


def connections() -> types.InlineKeyboardMarkup:
    return inline(
        [("👤 یک‌کاربره", "buy:conn:1"), ("👥 دوکاربره", "buy:conn:2")],
        [("🔙 بازگشت", "buy:start")],
    )


def durations(connections_count: int) -> types.InlineKeyboardMarkup:
    suffix = str(connections_count)
    if connections_count == 1:
        labels = ((1, "یک ماهه — ۴۵۰٬۰۰۰"), (3, "سه ماهه — ۱٬۰۰۰٬۰۰۰"), (6, "شش ماهه — ۱٬۵۰۰٬۰۰۰"))
    else:
        labels = ((1, "یک ماهه — ۶۵۰٬۰۰۰"), (3, "سه ماهه — ۱٬۴۰۰٬۰۰۰"), (6, "شش ماهه — ۲٬۰۰۰٬۰۰۰"))
    return inline(
        *[[(f"🗓️ {label}", f"buy:time:{suffix}:{months}")] for months, label in labels],
        [("🔙 بازگشت", "buy:type:time")],
    )


def data_picker(gigabytes: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("➖ کم", "buy:data:-"), (f"📊 {gigabytes} GB", "noop"), ("➕ زیاد", "buy:data:+")],
        [("✅ تأیید حجم", f"buy:data:confirm:{gigabytes}")],
        [("🔙 بازگشت", "buy:start")],
    )


def order_confirm(order_id: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("✅ تأیید و انتخاب پرداخت", f"order:confirm:{order_id}")],
        [("❌ انصراف", f"order:cancel:{order_id}")],
    )


def payment(order_id: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("💳 کارت‌به‌کارت", f"pay:card:{order_id}")],
        [("❌ انصراف", f"order:cancel:{order_id}")],
    )


def send_receipt(order_id: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("📎 ارسال رسید پرداخت", f"pay:receipt:{order_id}")],
        [("❌ انصراف", f"order:cancel:{order_id}")],
    )


def review_receipt(receipt_id: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("✅ تأیید رسید", f"receipt:approve:{receipt_id}"), ("❌ رد رسید", f"receipt:deny:{receipt_id}")]
    )


def guide_platforms() -> types.InlineKeyboardMarkup:
    return inline(
        [("🤖 Android", "guide:android"), ("🍎 iPhone", "guide:iphone")],
        [("🪟 Windows", "guide:windows"), ("💻 macOS", "guide:mac")],
        [("🐧 Linux", "guide:linux")],
        [("🏠 خانه", "home:user")],
    )


def home_button() -> types.InlineKeyboardMarkup:
    return inline([("🏠 بازگشت به خانه", "home:user")])


def account_actions(
    v2_id: int, root: bool = False, reseller: bool = False, list_back: str | None = None
) -> types.InlineKeyboardMarkup:
    rows = [
        [("🔗 QR و لینک", f"sub:qr:{v2_id}"), ("🔁 تمدید خودکار", f"sub:auto:{v2_id}")],
        [("📚 راهنمای اتصال", "guide:start"), ("🔙 بازگشت به اشتراک‌ها", list_back or "subs:list:1")],
    ]
    if root:
        rows = [
            [("🔗 QR و لینک", f"v2:qr:{v2_id}")],
            [("🗓 تمدید اشتراک", f"v2:update:{v2_id}"), ("✏️ تمدید دلخواه", f"v2:custom:{v2_id}")],
            [("🚫 بلاک / آزاد", f"v2:ban:{v2_id}"), ("➕ یک روز", f"v2:addday:{v2_id}")],
            [("💵 ثبت پرداخت", f"v2:payment:{v2_id}"), ("📦 تغییر بسته", f"v2:package:{v2_id}")],
            [("📝 یادداشت", f"v2:remarks:{v2_id}"), ("♨️ ریست ترافیک", f"v2:resetask:{v2_id}")],
            [("🔄 تعویض TOKEN و UUID", f"v2:renewask:{v2_id}")],
            [("🗑️ حذف اکانت", f"v2:deleteask:{v2_id}"), ("🔃 تازه‌سازی", f"v2:view:{v2_id}")],
            [("🔙 بازگشت", list_back or "root:v2:1")],
        ]
    elif reseller:
        rows.insert(1, [("🛠 مدیریت اکانت مشتری", f"resell:manage:{v2_id}")])
    return inline(*rows)


def delete_confirm(v2_id: int) -> types.InlineKeyboardMarkup:
    return inline(
        [("🗑️ بله، حذف شود", f"v2:delete:{v2_id}")],
        [("🛡️ خیر، نگه‌دار", f"v2:view:{v2_id}")],
    )
