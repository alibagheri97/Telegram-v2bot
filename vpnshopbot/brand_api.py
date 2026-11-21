"""Serve branded subscription labels for accounts created by bot admins."""

from __future__ import annotations

import base64
import json
import math
import re
import time
from functools import lru_cache
from urllib.parse import quote, urlsplit, urlunsplit

import requests
from flask import Flask, Response
from sqlalchemy import MetaData, Table, create_engine, select

from vpnshopbot.config import Settings, get_settings
from vpnshopbot.db import Database
from vpnshopbot.models import BotSetting, Permission, User, V2AccountLink

TOKEN_RE = re.compile(r"^[a-fA-F0-9]{32,128}$")
app = Flask(__name__)


@lru_cache
def settings() -> Settings:
    return get_settings()


@lru_cache
def bot_database() -> Database:
    return Database(settings())


@lru_cache
def panel_users() -> tuple[object, Table]:
    cfg = settings()
    engine = create_engine(
        cfg.panel_db_url.get_secret_value(),
        pool_pre_ping=True,
        pool_recycle=1800,
    )
    table = Table(cfg.panel_user_table, MetaData(), autoload_with=engine)
    return engine, table


def subscription_profile(token: str):
    """Return the owning admin's active brand and live panel counters."""
    engine, users = panel_users()
    with engine.connect() as connection:
        row = connection.execute(
            select(
                users.c.id.label("v2_user_id"),
                users.c.uuid.label("subscriber_uuid"),
                users.c.u.label("uploaded_bytes"),
                users.c.d.label("downloaded_bytes"),
                users.c.transfer_enable.label("transfer_limit_bytes"),
                users.c.expired_at.label("expires_at"),
            ).where(users.c.token == token)
        ).mappings().first()
    if not row:
        return None
    subscriber = dict(row)
    with bot_database().session() as db:
        link = db.scalar(
            select(V2AccountLink).where(
                V2AccountLink.v2_user_id == subscriber["v2_user_id"],
                V2AccountLink.admin_created.is_(True),
            )
        )
        if not link:
            return None
        owner = db.get(User, link.user_id)
        if not owner or owner.permission not in {
            int(Permission.ROOT),
            int(Permission.ADMIN),
        }:
            return None
        brand = db.get(BotSetting, f"admin_brand:{owner.id}")
        active = db.get(BotSetting, f"admin_brand_active:{owner.id}")
        if not brand or not brand.value.strip() or (active and active.value != "1"):
            return None
        return brand.value.strip(), subscriber


def subscription_status_nodes(subscriber, now=None):
    """Build local-only nodes that show live expiry and traffic information."""
    gib = 1024**3
    now = time.time() if now is None else now
    expiry = subscriber["expires_at"]
    if not expiry:
        days = "♾️ نامحدود"
    else:
        remaining_days = max(0, math.ceil((float(expiry) - now) / 86400))
        days = f"{remaining_days} روز"

    used_bytes = max(0, int(subscriber["uploaded_bytes"] or 0)) + max(
        0, int(subscriber["downloaded_bytes"] or 0)
    )
    limit = subscriber["transfer_limit_bytes"]
    if limit is None or int(limit) > 100 * gib:
        remaining = "♾️ نامحدود"
    else:
        remaining = f"{max(0, int(limit) - used_bytes) / gib:.1f} GB"
    labels = (
        f"⏳ روزهای باقی‌مانده: {days}",
        f"📊 استفاده شما: {used_bytes / gib:.1f} GB | باقی‌مانده: {remaining}",
    )
    password = subscriber["subscriber_uuid"] or "subscription-info"
    credentials = base64.urlsafe_b64encode(
        f"chacha20-ietf-poly1305:{password}".encode()
    ).decode().rstrip("=")
    return [
        f"ss://{credentials}@127.0.0.1:1111#{quote(label, safe='')}"
        for label in labels
    ]


def relabel_subscription(source: str, brand: str, subscriber) -> bytes:
    """Prepend status nodes and preserve every real node's connection data."""
    decoded = base64.b64decode("".join(source.split()), validate=True).decode()
    lines = subscription_status_nodes(subscriber)
    nodes = [line for line in decoded.splitlines() if line.strip()]
    if not nodes:
        raise ValueError("Empty subscription")
    for index, line in enumerate(nodes, 1):
        label = brand + (f" {index}" if len(nodes) > 1 else "")
        if line.startswith("vmess://"):
            payload = json.loads(base64.b64decode(line[8:] + "===").decode())
            payload["ps"] = label
            line = "vmess://" + base64.b64encode(
                json.dumps(payload, ensure_ascii=False).encode()
            ).decode()
        else:
            parsed = urlsplit(line)
            if parsed.scheme not in {
                "vless",
                "trojan",
                "ss",
                "hysteria",
                "hysteria2",
                "tuic",
            }:
                raise ValueError("Unsupported subscription node")
            line = urlunsplit(
                (parsed.scheme, parsed.netloc, parsed.path, parsed.query, quote(label, safe=""))
            )
        lines.append(line)
    return base64.b64encode(("\n".join(lines) + "\n").encode())


@app.get("/health")
def health():
    return {"status": "ok", "service": "vpn-shop-brand-api"}


@app.get("/allbatech/<token>")
def subscription(token):
    if not TOKEN_RE.fullmatch(token):
        return Response("Not found", status=404)
    try:
        profile = subscription_profile(token)
        if profile is None:
            return Response("Not found", status=404)
        brand, subscriber = profile
        upstream = requests.get(
            f"{settings().subscription_base_url}{token}",
            timeout=12,
        )
        if upstream.status_code != 200:
            return Response("Not found", status=404)
        body = relabel_subscription(upstream.text, brand, subscriber)
        response = Response(body, mimetype="text/plain")
        for header in (
            "subscription-userinfo",
            "profile-update-interval",
            "profile-web-page-url",
        ):
            if header in upstream.headers:
                response.headers[header] = upstream.headers[header]
        response.headers["Cache-Control"] = "no-store"
        return response
    except (ValueError, json.JSONDecodeError):
        return Response("Invalid subscription", status=502)
    except Exception:
        app.logger.exception("Brand subscription failed")
        return Response("Subscription unavailable", status=502)


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=settings().brand_api_port)
