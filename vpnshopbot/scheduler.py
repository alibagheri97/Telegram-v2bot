from __future__ import annotations

import logging
import time

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telebot import TeleBot

from .archive import is_archived
from .config import Settings
from .db import Database
from .emergency import EmergencyService
from .expiry import ExpiryService, setting
from .models import Product, ProductType, User, UserStatus, V2AccountLink, WalletTransaction
from .panel import PanelRepository
from .services import ShopService, toman
from .wallet import can_spend

log = logging.getLogger(__name__)


class ShopScheduler:
    def __init__(
        self, bot: TeleBot, database: Database, panel: PanelRepository, settings: Settings
    ):
        self.bot = bot
        self.database = database
        self.panel = panel
        self.service = ShopService(settings, panel)
        self.scheduler = BackgroundScheduler(timezone=settings.timezone)

    def start(self) -> None:
        self.scheduler.add_job(
            self.check_expirations,
            "interval",
            seconds=15,
            id="check_expirations",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.add_job(
            self.auto_renew,
            "interval",
            minutes=15,
            id="auto_renew",
            max_instances=1,
            coalesce=True,
        )
        self.scheduler.start()

    def shutdown(self, *, wait=False) -> None:
        self.scheduler.shutdown(wait=wait)

    def check_expirations(self) -> None:
        try:
            EmergencyService(self.bot, self.database, self.panel).run()
        except Exception:
            log.exception("emergency recharge job failed")
        try:
            ExpiryService(self.bot, self.database, self.panel).run()
        except Exception:
            log.exception("expiration notification job failed")

    def auto_renew(self) -> None:
        try:
            with self.database.session() as db:
                if setting(db, "service:auto_renew:enabled", "1") != "1":
                    return
                links = list(
                    db.scalars(
                        select(V2AccountLink)
                        .options(selectinload(V2AccountLink.user))
                        .where(
                            V2AccountLink.auto_renewal.is_(True),
                            V2AccountLink.renewal_price.is_not(None),
                        )
                    )
                )
                for link in links:
                    if is_archived(db, link.v2_user_id):
                        continue
                    owner = db.scalar(select(User).where(User.id == link.user_id).with_for_update()
                        .execution_options(populate_existing=True))
                    if not owner or owner.status != UserStatus.ACTIVE:
                        continue
                    account = self.panel.get_user(link.v2_user_id)
                    if not account:
                        continue
                    expired = int(account.get("expired_at") or 0)
                    remaining = int(account.get("transfer_enable") or 0) - int(
                        account.get("u") or 0
                    ) - int(account.get("d") or 0)
                    due = (
                        expired <= int(time.time())
                        if link.product_type == ProductType.TIME
                        else remaining <= 0
                    )
                    price = int(link.renewal_price or 0)
                    if link.admin_created:
                        product = db.scalar(select(Product).where(
                            Product.product_type == link.product_type,
                            Product.months == link.renewal_months,
                            Product.data_gb == link.renewal_data_gb,
                            Product.connections == (link.renewal_connections or 1),
                        ).order_by(Product.id))
                        if not product:
                            continue
                        try:
                            price = self.service.admin_product_price(db, link.user, product)
                        except ValueError:
                            continue
                    if not due or not can_spend(db, owner, price):
                        continue
                    if link.product_type == ProductType.TIME:
                        self.panel.add_days(link.v2_user_id, int(link.renewal_months or 1) * 30)
                    else:
                        self.panel.reset_data_subscription(
                            link.v2_user_id, int(link.renewal_data_gb or 10)
                        )
                    link.user.balance -= price
                    db.add(
                        WalletTransaction(
                            user_id=link.user_id,
                            amount=-price,
                            kind="auto_renewal",
                            reference=f"renew:{link.id}:{int(time.time())}",
                            note=link.label,
                        )
                    )
                    self.bot.send_message(
                        link.user.telegram_id,
                        f"اشتراک «{link.label}» خودکار تمدید شد و {toman(price)} از کیف پول کم شد ✅",
                    )
        except Exception:
            log.exception("auto-renewal job failed")
