from __future__ import annotations

import argparse
import logging
import os
import signal

from telebot import TeleBot

from .config import get_settings
from .db import Database
from .handlers import BotController
from .panel import PanelRepository
from .runtime import (
    HandlerDrain,
    exec_current_release,
    join_polling_threads,
    publish_health,
    runtime_directory,
)
from .scheduler import ShopScheduler
from .services import ShopService

log = logging.getLogger(__name__)


class RedactingFormatter(logging.Formatter):
    def __init__(self, secret: str):
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self.secret = secret

    def format(self, record: logging.LogRecord) -> str:
        return super().format(record).replace(self.secret, "<redacted>")


def configure_logging(level: str, secret: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(secret))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))


def build_application() -> tuple[TeleBot, Database, PanelRepository, ShopService]:
    settings = get_settings()
    database = Database(settings)
    # Importing models registers all tables on Base.metadata.
    from . import models  # noqa: F401

    database.create_schema()
    panel = PanelRepository(settings)
    service = ShopService(settings, panel)
    with database.session() as db:
        service.seed_defaults(db)
    bot = TeleBot(settings.bot_token.get_secret_value(), parse_mode="HTML", threaded=True)
    BotController(bot, database, panel, service, settings)
    return bot, database, panel, service


def main() -> None:
    parser = argparse.ArgumentParser(description="Persian VPN shop Telegram bot")
    parser.add_argument(
        "--init-db", action="store_true", help="create/upgrade initial tables and exit"
    )
    parser.add_argument(
        "--check", action="store_true", help="validate config, DB and Telegram token"
    )
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.log_level, settings.bot_token.get_secret_value())
    bot, database, panel, _ = build_application()
    if args.init_db:
        log.info("database schema and defaults are ready")
        return
    if args.check:
        me = bot.get_me()
        panel.list_users(page_size=1)
        log.info("configuration OK; connected as @%s", me.username)
        if ".invalid" in settings.subscription_base_url:
            log.warning("SUBSCRIPTION_BASE_URL is still a placeholder; do not start production")
        if settings.card_number == "CHANGE_ME":
            log.warning("CARD_NUMBER is still a placeholder")
        if not settings.payment_group_id:
            log.warning("PAYMENT_GROUP_ID is unset; receipts will be sent directly to root admins")
        return
    scheduler = ShopScheduler(bot, database, panel, settings)
    bot.last_update_id = int(os.environ.pop("VPN_SHOP_LAST_UPDATE_ID", "0"))
    drain = HandlerDrain(bot)
    scheduler.start()
    reload_requested = False

    def stop(signum, *_args) -> None:
        nonlocal reload_requested
        reload_requested = (
            signum == getattr(signal, "SIGHUP", None) and runtime_directory() is not None
        )
        bot.stop_polling()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, stop)
    publish_health()
    log.info("bot is running")
    # skip_pending performs an unguarded request before infinity_polling starts its retry loop.
    # Keeping it false lets the service survive temporary Telegram/network outages.
    bot.infinity_polling(skip_pending=False, timeout=30, long_polling_timeout=30)
    join_polling_threads()
    scheduler.shutdown(wait=True)
    drain.wait()
    bot.stop_bot()
    database.engine.dispose()
    panel.engine.dispose()
    if reload_requested:
        exec_current_release(bot.last_update_id)


if __name__ == "__main__":
    main()
