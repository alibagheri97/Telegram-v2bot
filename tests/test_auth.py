import time
from contextlib import contextmanager
from types import SimpleNamespace

import bcrypt
import requests
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from vpnshopbot.db import Base
from vpnshopbot.handlers import BotController
from vpnshopbot.models import (
    BotSetting,
    ConversationState,
    Order,
    OrderStatus,
    PaymentReceipt,
    Permission,
    ProductType,
    User,
)
from vpnshopbot.services import ShopService


class FakeDatabase:
    def __init__(self) -> None:
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)

    @contextmanager
    def session(self):
        db = self.sessions()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()


class FakeBot:
    def __init__(self) -> None:
        self.replies = []
        self.failed_chats = set()
        self.sent_receipts = []

    def reply_to(self, message, text, **kwargs) -> None:
        self.replies.append((text, kwargs))

    def get_me(self):
        return SimpleNamespace(id=777)

    def get_chat_member(self, chat_id, user_id):
        return SimpleNamespace(status="administrator")

    def send_photo(self, *, chat_id, photo, **kwargs):
        if chat_id in self.failed_chats:
            raise requests.ConnectionError("unreachable chat")
        sent = SimpleNamespace(chat=SimpleNamespace(id=chat_id), message_id=100)
        self.sent_receipts.append((chat_id, kwargs))
        return sent

    def send_document(self, *, chat_id, document, **kwargs):
        return self.send_photo(chat_id=chat_id, photo=document, **kwargs)


def controller_for(code: str = "123456") -> tuple[BotController, FakeDatabase, FakeBot]:
    settings = SimpleNamespace(
        bootstrap_auth_hash=SecretStr(
            bcrypt.hashpw(code.encode(), bcrypt.gensalt(rounds=4)).decode()
        ),
        bootstrap_auth_expires_at=int(time.time()) + 300,
        root_telegram_ids=[],
        payment_group_id=999,
        payment_topic_id=2,
    )
    database = FakeDatabase()
    bot = FakeBot()
    controller = BotController.__new__(BotController)
    controller.bot = bot
    controller.database = database
    controller.settings = settings
    controller.service = ShopService(settings, panel=SimpleNamespace())
    return controller, database, bot


def message(code: str, telegram_id: int = 42):
    return SimpleNamespace(
        text=f"/auth {code}",
        from_user=SimpleNamespace(id=telegram_id, first_name="Root"),
    )


def test_one_time_code_claims_exactly_one_root() -> None:
    controller, database, bot = controller_for()
    controller.auth_command(message("123456"))
    with Session(database.engine) as db:
        root = db.scalar(select(User).where(User.permission == int(Permission.ROOT)))
        assert root.telegram_id == 42
    controller.auth_command(message("123456", telegram_id=99))
    with Session(database.engine) as db:
        roots = list(db.scalars(select(User).where(User.permission == int(Permission.ROOT))))
        assert len(roots) == 1
    assert "قبلاً" in bot.replies[-1][0]


def test_root_can_set_payment_topic_without_a_code() -> None:
    controller, database, _bot = controller_for()
    controller.auth_command(message("123456"))
    group_message = SimpleNamespace(
        text="/set-topic recipt",
        chat=SimpleNamespace(
            id=-100123456,
            type="supergroup",
            is_forum=True,
            title="Payments",
        ),
        message_thread_id=77,
        from_user=SimpleNamespace(id=42),
    )
    controller.set_topic_command(group_message)
    with Session(database.engine) as db:
        assert db.get(BotSetting, "payment_group_id").value == "-100123456"
        assert db.get(BotSetting, "payment_topic_id").value == "77"


def test_root_can_set_expired_topic_without_changing_payment_topic() -> None:
    controller, database, _bot = controller_for()
    controller.auth_command(message("123456"))
    group_message = SimpleNamespace(
        text="/set-topic expired",
        chat=SimpleNamespace(id=-100123456, type="supergroup", is_forum=True, title="Expired"),
        message_thread_id=88, from_user=SimpleNamespace(id=42))
    controller.set_topic_command(group_message)
    with Session(database.engine) as db:
        assert db.get(BotSetting, "expired_group_id").value == "-100123456"
        assert db.get(BotSetting, "expired_topic_id").value == "88"
        assert db.get(BotSetting, "payment_group_id") is None


def test_non_root_cannot_set_expired_topic() -> None:
    controller, database, _bot = controller_for()
    group_message = SimpleNamespace(
        text="/set-topic expired",
        chat=SimpleNamespace(id=-100123456, type="supergroup", is_forum=True, title="Expired"),
        message_thread_id=88, from_user=SimpleNamespace(id=99))
    controller.set_topic_command(group_message)
    with Session(database.engine) as db:
        assert db.get(BotSetting, "expired_topic_id") is None


def test_non_root_cannot_set_payment_topic() -> None:
    controller, database, bot = controller_for()
    group_message = SimpleNamespace(
        text="/set-topic recipt",
        chat=SimpleNamespace(
            id=-100123456,
            type="supergroup",
            is_forum=True,
            title="Payments",
        ),
        message_thread_id=77,
        from_user=SimpleNamespace(id=99),
    )
    controller.set_topic_command(group_message)
    with Session(database.engine) as db:
        assert db.get(BotSetting, "payment_group_id") is None
    assert "مدیر اصلی" in bot.replies[-1][0]


def test_receipt_is_saved_and_falls_back_to_root_when_group_fails() -> None:
    controller, database, bot = controller_for()
    controller.auth_command(message("123456"))
    with Session(database.engine) as db:
        customer = User(
            telegram_id=100,
            name="Customer",
            permission=int(Permission.USER),
            referral_code="customerref",
            v2accounts=[],
        )
        db.add(customer)
        db.flush()
        order = Order(
            user_id=customer.id,
            product_type=ProductType.DATA,
            data_gb=10,
            amount=200_000,
            status=OrderStatus.AWAITING_PAYMENT,
        )
        db.add(order)
        db.flush()
        db.add(
            ConversationState(
                telegram_id=100,
                state="await_receipt",
                data={"order_id": order.id},
            )
        )
        db.commit()
    bot.failed_chats.add(999)
    receipt_message = SimpleNamespace(
        from_user=SimpleNamespace(id=100),
        photo=[SimpleNamespace(file_id="photo-file")],
        document=None,
    )
    controller.receive_receipt(receipt_message)
    with Session(database.engine) as db:
        receipt = db.scalar(select(PaymentReceipt))
        order = db.scalar(select(Order))
        assert receipt is not None
        assert receipt.review_chat_id == 42
        assert order.status == OrderStatus.PENDING_REVIEW
        assert db.get(ConversationState, 100) is None
    assert [chat_id for chat_id, _ in bot.sent_receipts] == [42]


def test_wrong_code_does_not_create_root() -> None:
    controller, database, bot = controller_for()
    controller.auth_command(message("654321"))
    with Session(database.engine) as db:
        assert db.scalar(select(User).where(User.permission == int(Permission.ROOT))) is None
    assert "نادرست" in bot.replies[-1][0]


def test_bootstrap_stays_disabled_if_root_row_is_later_removed() -> None:
    controller, database, bot = controller_for()
    controller.auth_command(message("123456"))
    with Session(database.engine) as db:
        root = db.scalar(select(User).where(User.permission == int(Permission.ROOT)))
        db.delete(root)
        db.commit()
    controller.auth_command(message("123456", telegram_id=99))
    with Session(database.engine) as db:
        assert db.scalar(select(User).where(User.permission == int(Permission.ROOT))) is None
    assert "قبلاً" in bot.replies[-1][0]
