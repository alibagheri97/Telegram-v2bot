from __future__ import annotations

import re
import secrets
import time
import uuid as uuid_lib
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import bcrypt
from sqlalchemy import MetaData, Table, and_, create_engine, delete, func, or_, select, update
from sqlalchemy.engine import Engine

from .config import Settings

GIB = 1024**3
SEARCH_PATTERN = re.compile(r"^(id|email|telegram_id|token|uuid|remarks)\s*(=|\?)\s*(.+)$", re.IGNORECASE)
ALLOWED_SORTS = {"id", "email", "d", "u", "transfer_enable", "expired_at", "banned", "created_at"}


@dataclass(frozen=True)
class SearchExpression:
    field: str | None
    operator: str
    value: str


def parse_search(raw: str) -> SearchExpression:
    value = raw.strip()
    match = SEARCH_PATTERN.fullmatch(value)
    if not match:
        return SearchExpression(None, "?", value)
    return SearchExpression(match.group(1).lower(), match.group(2), match.group(3).strip())


class PanelRepository:
    """Small, explicit gateway to the panel DB; it never mutates its schema."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine: Engine = create_engine(
            settings.panel_db_url.get_secret_value(),
            pool_pre_ping=True,
            pool_recycle=1800,
            isolation_level="READ COMMITTED",
        )
        metadata = MetaData()
        self.users = Table(settings.panel_user_table, metadata, autoload_with=self.engine)

    def _new_email(self, prefix: str) -> str:
        safe = re.sub(r"[^a-zA-Z0-9_.-]", "", prefix)[:36] or "user"
        return f"{safe}.{secrets.token_hex(3)}@telegram.local"

    def create_account(
        self,
        *,
        email_prefix: str,
        telegram_id: int | None,
        transfer_gb: int,
        days: int | None,
        device_limit: int | None,
        plan_id: int | None,
        group_id: int,
        remarks: str,
    ) -> dict[str, Any]:
        now = int(time.time())
        raw_password = secrets.token_urlsafe(24).encode()
        values = {
            "invite_user_id": None,
            "telegram_id": telegram_id,
            "email": self._new_email(email_prefix),
            "password": bcrypt.hashpw(raw_password, bcrypt.gensalt()).decode(),
            "balance": 0,
            "commission_type": 0,
            "commission_balance": 0,
            "t": 0,
            "u": 0,
            "d": 0,
            "transfer_enable": transfer_gb * GIB,
            "device_limit": device_limit,
            "banned": 0,
            "is_admin": 0,
            "uuid": str(uuid_lib.uuid4()),
            "group_id": group_id,
            "plan_id": plan_id,
            "auto_renewal": 0,
            "remind_expire": 1,
            "remind_traffic": 1,
            "token": secrets.token_hex(16),
            "expired_at": now + int(timedelta(days=days).total_seconds()) if days else 0,
            "remarks": remarks,
            "created_at": now,
            "updated_at": now,
        }
        with self.engine.begin() as connection:
            result = connection.execute(self.users.insert().values(**values))
            account_id = int(result.inserted_primary_key[0])
            row = connection.execute(select(self.users).where(self.users.c.id == account_id)).mappings().one()
        return dict(row)

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.engine.connect() as connection:
            row = connection.execute(
                select(self.users).where(self.users.c.id == user_id)
            ).mappings().first()
        return dict(row) if row else None

    def list_users(
        self,
        *,
        page: int = 1,
        page_size: int = 8,
        sort: str = "id",
        descending: bool = True,
        search: str = "",
        ids: list[int] | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        sort = sort if sort in ALLOWED_SORTS else "id"
        where = []
        if ids is not None:
            where.append(self.users.c.id.in_(ids or [-1]))
        if search.strip():
            expression = parse_search(search)
            if expression.field:
                column = self.users.c[expression.field]
                if expression.operator == "=" and expression.field in {"id", "telegram_id"}:
                    try:
                        where.append(column == int(expression.value))
                    except ValueError:
                        where.append(column == -1)
                elif expression.operator == "=":
                    where.append(column == expression.value)
                else:
                    where.append(column.like(f"%{expression.value}%"))
            else:
                term = f"%{expression.value}%"
                where.append(
                    or_(
                        self.users.c.email.like(term),
                        self.users.c.token.like(term),
                        self.users.c.uuid.like(term),
                        self.users.c.remarks.like(term),
                    )
                )
        condition = and_(*where) if where else None
        count_query = select(func.count()).select_from(self.users)
        query = select(self.users)
        if condition is not None:
            count_query = count_query.where(condition)
            query = query.where(condition)
        ordering = self.users.c[sort].desc() if descending else self.users.c[sort].asc()
        query = query.order_by(ordering).limit(page_size).offset(max(page - 1, 0) * page_size)
        with self.engine.connect() as connection:
            total = int(connection.scalar(count_query) or 0)
            rows = [dict(row) for row in connection.execute(query).mappings()]
        return rows, total

    def set_banned(self, user_id: int, banned: bool) -> bool:
        return self._update(user_id, banned=int(banned))

    def reset_usage(self, user_id: int) -> bool:
        return self._update(user_id, u=0, d=0)

    def reset_data_subscription(self, user_id: int, transfer_gb: int) -> bool:
        return self._update(user_id, u=0, d=0, transfer_enable=transfer_gb * GIB)

    def recharge_subscription(
        self, user_id: int, *, months: int | None, data_gb: int | None,
        connections: int, time_based: bool,
    ) -> dict[str, Any]:
        with self.engine.begin() as connection:
            account = connection.execute(
                select(self.users).where(self.users.c.id == user_id).with_for_update()
            ).mappings().first()
            if not account:
                raise ValueError("اکانت پنل پیدا نشد")
            now = int(time.time())
            expiry = max(int(account.get("expired_at") or 0), now) + int(months or 1) * 30 * 86400
            connection.execute(update(self.users).where(self.users.c.id == user_id).values(
                u=0, d=0, banned=0,
                transfer_enable=(1_000_000 if time_based else int(data_gb or 0)) * GIB,
                device_limit=connections if time_based else None,
                expired_at=expiry if time_based else 0, updated_at=now,
            ))
            return dict(connection.execute(
                select(self.users).where(self.users.c.id == user_id)
            ).mappings().one())

    def add_days(self, user_id: int, days: int) -> bool:
        account = self.get_user(user_id)
        if not account:
            return False
        now = int(time.time())
        base = max(int(account.get("expired_at") or 0), now)
        return self._update(user_id, expired_at=base + days * 86400)

    def renew_expired(self, user_id: int, *, signature: str, days: int = 31) -> bool:
        """Lock and recheck the expired cycle, then renew/reset in one panel transaction."""
        from .expiry import account_expiration, account_signature

        with self.engine.begin() as connection:
            row = connection.execute(select(self.users).where(
                self.users.c.id == user_id).with_for_update()).mappings().first()
            if not row or account_signature(row) != signature or not account_expiration(row):
                return False
            now = int(time.time())
            connection.execute(update(self.users).where(self.users.c.id == user_id).values(
                expired_at=max(int(row.get("expired_at") or 0), now) + days * 86400,
                u=0, d=0, banned=0, updated_at=now))
            return True

    def apply_emergency_operation(self, user_id: int, *, before: int, after: int,
                                  transfer_enable: int, monthly: bool) -> None:
        """Compare-and-set an extension; replay of an already applied intent is safe."""
        with self.engine.begin() as connection:
            row = connection.execute(select(self.users).where(
                self.users.c.id == user_id).with_for_update()).mappings().first()
            if not row:
                raise ValueError("اکانت پنل پیدا نشد")
            current = int(row.get("expired_at") or 0)
            if current == after and before != after:
                return
            if current != before or int(row.get("transfer_enable") or 0) != transfer_enable:
                raise ValueError("تعارض وضعیت پنل؛ عملیات معلق نیاز به بررسی مدیر دارد")
            values = {"expired_at": after, "updated_at": int(time.time())}
            if monthly:
                values.update(u=0, d=0, banned=0)
            connection.execute(update(self.users).where(self.users.c.id == user_id).values(**values))

    def add_transfer(self, user_id: int, gigabytes: int) -> bool:
        account = self.get_user(user_id)
        if not account:
            return False
        total = int(account.get("transfer_enable") or 0) + gigabytes * GIB
        return self._update(user_id, transfer_enable=total)

    def set_device_limit(self, user_id: int, device_limit: int | None) -> bool:
        return self._update(user_id, device_limit=device_limit)

    def set_package_limits(self, user_id: int, *, transfer_gb: int, device_limit: int | None) -> bool:
        return self._update(user_id, transfer_enable=transfer_gb * GIB, device_limit=device_limit)

    def archive_if_unchanged(self, user_id: int, snapshot: dict) -> bool:
        """Soft archive: retain the panel row and recheck expiry under its write lock."""
        from .expiry import account_expiration

        with self.engine.begin() as connection:
            row = connection.execute(select(self.users).where(
                self.users.c.id == user_id).with_for_update()).mappings().first()
            if not row or not account_expiration(row):
                return False
            if any(int(row.get(key) or 0) != int(snapshot.get(key) or 0)
                   for key in ("expired_at", "transfer_enable", "u", "d")):
                return False
            connection.execute(update(self.users).where(self.users.c.id == user_id).values(
                banned=1, updated_at=int(time.time())))
            return True

    def renew_credentials(self, user_id: int) -> bool:
        return self._update(user_id, token=secrets.token_hex(16), uuid=str(uuid_lib.uuid4()))

    def update_remarks(self, user_id: int, remarks: str) -> bool:
        return self._update(user_id, remarks=remarks[:1000])

    def delete_user(self, user_id: int) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(delete(self.users).where(self.users.c.id == user_id))
        return result.rowcount == 1

    def _update(self, user_id: int, **values: Any) -> bool:
        values["updated_at"] = int(time.time())
        with self.engine.begin() as connection:
            result = connection.execute(
                update(self.users).where(self.users.c.id == user_id).values(**values)
            )
        return result.rowcount == 1

    def subscription_url(self, account: dict[str, Any]) -> str:
        return f"{self.settings.subscription_base_url}{account['token']}"
