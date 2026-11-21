"""Per-admin overdraft authorization; normal users and roots have a zero floor."""

from .models import BotSetting, Permission


def credit_key(user_id):
    return f"admin_wallet_floor:{user_id}"


def wallet_floor(db, user):
    if user.permission != int(Permission.ADMIN):
        return 0
    row = db.get(BotSetting, credit_key(user.id))
    value = int(row.value) if row else 0
    return min(0, max(-(10**12), value))


def can_spend(db, user, amount):
    return amount >= 0 and user.balance - amount >= wallet_floor(db, user)
