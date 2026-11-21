"""Persistent user events, including references to actual receipts and orders."""

from sqlalchemy import select

from .models import Order, OrderStatus, PaymentReceipt, UserAction, WalletTransaction


def record_action(
    db, user_id, action, title, *, details=None, actor_id=None, source_key=None, created_at=None
):
    if source_key:
        existing = db.scalar(select(UserAction).where(UserAction.source_key == source_key))
        if existing:
            return existing
    event = UserAction(
        user_id=user_id,
        actor_telegram_id=actor_id,
        action=action,
        title=title,
        details=details or {},
        source_key=source_key,
    )
    if created_at:
        event.created_at = created_at
    db.add(event)
    return event


def backfill_actions(db, user_id):
    """Reconstruct only events evidenced by existing DB records, never old clicks."""
    keys = set(db.scalars(select(UserAction.source_key).where(UserAction.user_id == user_id)))
    for order in db.scalars(select(Order).where(Order.user_id == user_id)):
        key = f"order:created:{order.id}"
        if key not in keys:
            record_action(
                db,
                user_id,
                "order.created",
                "🛒 ثبت سفارش",
                source_key=key,
                created_at=order.created_at,
                details={
                    "order_id": order.id,
                    "amount": order.amount,
                    "type": order.product_type.value,
                    "historical": True,
                },
            )
        if order.status == OrderStatus.PAID and f"order:paid:{order.id}" not in keys:
            record_action(
                db,
                user_id,
                "order.paid",
                "✅ پرداخت تأییدشده",
                source_key=f"order:paid:{order.id}",
                created_at=order.updated_at,
                details={
                    "order_id": order.id,
                    "amount": order.amount,
                    "v2_user_id": order.v2_user_id,
                    "historical": True,
                },
            )
    for receipt in db.scalars(select(PaymentReceipt).where(PaymentReceipt.user_id == user_id)):
        key = f"receipt:submitted:{receipt.id}"
        if key not in keys:
            record_action(
                db,
                user_id,
                "receipt.submitted",
                "📎 ارسال رسید پرداخت",
                source_key=key,
                created_at=receipt.created_at,
                details={
                    "receipt_id": receipt.id,
                    "order_id": receipt.order_id,
                    "historical": True,
                },
            )
        key = f"receipt:decision:{receipt.id}"
        if receipt.decision and key not in keys:
            record_action(
                db,
                user_id,
                "receipt.reviewed",
                "🧾 نتیجه بررسی رسید",
                source_key=key,
                created_at=receipt.decision_at or receipt.updated_at,
                details={
                    "receipt_id": receipt.id,
                    "order_id": receipt.order_id,
                    "decision": receipt.decision,
                    "historical": True,
                },
            )
    for txn in db.scalars(select(WalletTransaction).where(WalletTransaction.user_id == user_id)):
        key = f"wallet:{txn.id}"
        if key not in keys:
            record_action(
                db,
                user_id,
                "wallet.transaction",
                "💳 تراکنش کیف پول",
                source_key=key,
                created_at=txn.created_at,
                details={
                    "amount": txn.amount,
                    "kind": txn.kind,
                    "note": txn.note,
                    "historical": True,
                },
            )


def receipt_message_url(chat_id, message_id, topic_id=None):
    chat = str(chat_id)
    if not chat.startswith("-100") or not message_id:
        return None
    url = f"https://t.me/c/{chat[4:]}/{int(message_id)}"
    return f"{url}?thread={int(topic_id)}" if topic_id else url
