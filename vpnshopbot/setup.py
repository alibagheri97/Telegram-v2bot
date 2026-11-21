from __future__ import annotations

import argparse
import getpass
import json
import re
import secrets
import string
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import quote_plus
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import bcrypt
import pymysql
import requests
from sqlalchemy.engine import make_url
from telebot import TeleBot
from telebot.apihelper import ApiTelegramException

from .config import Settings
from .privileges import privileged, service_identity

PROJECT_DIR = Path(__file__).resolve().parent.parent
IDENTIFIER = re.compile(r"^[A-Za-z0-9_]+$")


def ask(prompt: str, default: str | None = None, *, secret: bool = False) -> str:
    suffix = f" [{default}]" if default is not None else ""
    reader = getpass.getpass if secret else input
    while True:
        value = reader(f"{prompt}{suffix}: ").strip()
        if value:
            return value
        if default is not None:
            return default
        print("This value is required.")


def ask_int(prompt: str, default: int, *, minimum: int | None = None) -> int:
    while True:
        raw = ask(prompt, str(default))
        try:
            value = int(raw)
        except ValueError:
            print("Enter a whole number.")
            continue
        if minimum is not None and value < minimum:
            print(f"Value must be at least {minimum}.")
            continue
        return value


def ask_secret_optional(prompt: str, default: str = "") -> str:
    value = getpass.getpass(f"{prompt}: ").strip()
    return value or default


def identifier(prompt: str, default: str) -> str:
    while True:
        value = ask(prompt, default)
        if IDENTIFIER.fullmatch(value):
            return value
        print("Use only English letters, numbers and underscores.")


def service_slug(brand: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", brand.lower()).strip("_")
    return value or "vpn_shop"


def random_password(length: int = 28) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def dotenv_line(key: str, value: object) -> str:
    return f"{key}={json.dumps(str(value), ensure_ascii=False)}"


def validate_https(value: str) -> str:
    if not value.startswith("https://") or not value.endswith("token="):
        raise ValueError(
            "The subscription URL must be HTTPS and end at the token value, e.g. ?token="
        )
    if "example.com" in value or ".invalid" in value:
        raise ValueError("Enter the real panel subscription domain, not the example value.")
    return value


def validate_bot_token(token: str) -> str | None:
    try:
        return TeleBot(token).get_me().username
    except ApiTelegramException as exc:
        raise SystemExit(f"Telegram rejected the bot token: {exc}") from exc
    except requests.RequestException as exc:
        print(
            f"Warning: Telegram is temporarily unreachable; service installation will continue: {exc}"
        )
        return None


def configure_database(config: dict[str, object], admin_password: str) -> None:
    connection = pymysql.connect(
        host=str(config["db_host"]),
        port=int(config["db_port"]),
        user=str(config["db_admin_user"]),
        password=admin_password,
        charset="utf8mb4",
        autocommit=True,
    )
    bot_db = str(config["bot_db"])
    panel_db = str(config["panel_db"])
    panel_table = str(config["panel_table"])
    db_user = str(config["db_user"])
    db_password = str(config["db_password"])
    allowed_host = str(config["db_allowed_host"])
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{bot_db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
            cursor.execute(
                "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                (db_user, allowed_host, db_password),
            )
            cursor.execute(
                "ALTER USER %s@%s IDENTIFIED BY %s",
                (db_user, allowed_host, db_password),
            )
            cursor.execute(
                f"GRANT ALL PRIVILEGES ON `{bot_db}`.* TO %s@%s",
                (db_user, allowed_host),
            )
            cursor.execute(
                f"GRANT SELECT, INSERT, UPDATE, DELETE ON `{panel_db}`.`{panel_table}` TO %s@%s",
                (db_user, allowed_host),
            )
            cursor.execute(f"SELECT 1 FROM `{panel_db}`.`{panel_table}` LIMIT 1")
            cursor.execute("FLUSH PRIVILEGES")
    finally:
        connection.close()


def render_env(config: dict[str, object], auth_hash: str, auth_expiry: int) -> str:
    user = quote_plus(str(config["db_user"]))
    password = quote_plus(str(config["db_password"]))
    host = str(config["db_host"])
    port = int(config["db_port"])
    bot_db = str(config["bot_db"])
    panel_db = str(config["panel_db"])
    values = {
        "BOT_TOKEN": config["bot_token"],
        "BRAND_NAME": config["brand"],
        "BOT_DB_URL": f"mysql+pymysql://{user}:{password}@{host}:{port}/{bot_db}?charset=utf8mb4",
        "PANEL_DB_URL": f"mysql+pymysql://{user}:{password}@{host}:{port}/{panel_db}?charset=utf8mb4",
        "PANEL_USER_TABLE": config["panel_table"],
        "ROOT_TELEGRAM_IDS": "[]",
        "BOOTSTRAP_AUTH_HASH": auth_hash,
        "BOOTSTRAP_AUTH_EXPIRES_AT": auth_expiry,
        "SUBSCRIPTION_BASE_URL": config["subscription_url"],
        "BRAND_SUBSCRIPTION_BASE_URL": config["brand_subscription_url"],
        "BRAND_API_PORT": config["brand_api_port"],
        "TRIAL_PLAN_ID": config["trial_plan_id"],
        "TRIAL_GROUP_ID": config["trial_group_id"],
        "TRIAL_DAYS": config["trial_days"],
        "TRIAL_TRANSFER_GB": config["trial_transfer_gb"],
        "PAYMENT_GROUP_ID": config["payment_group_id"],
        "PAYMENT_TOPIC_ID": config["payment_topic_id"],
        "SUPPORT_USERNAME": config["support_username"],
        "CARD_NUMBER": config["card_number"],
        "CARD_HOLDER": config["card_holder"],
        "TIMEZONE": config["timezone"],
        "LOG_LEVEL": "INFO",
    }
    return "\n".join(dotenv_line(key, value) for key, value in values.items()) + "\n"


def update_env_values(path: Path, updates: dict[str, object]) -> None:
    remaining = dict(updates)
    output: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in remaining:
            output.append(dotenv_line(key, remaining.pop(key)))
        else:
            output.append(line)
    output.extend(dotenv_line(key, value) for key, value in remaining.items())
    temporary = path.with_suffix(".tmp")
    temporary.write_text("\n".join(output) + "\n", encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def escape_unit_path(path: str) -> str:
    return path.replace("\\", "\\x5c").replace(" ", "\\x20").replace("\t", "\\x09")


def render_service_unit(service_name: str, user: str, group: str, project: str, *, managed=False) -> str:
    escaped_project = escape_unit_path(project)
    executable = f"{escaped_project}/deploy/run.sh" if managed else f"{escaped_project}/.venv/bin/python -m vpnshopbot.app"
    return f"""[Unit]
Description={service_name} Telegram VPN Shop
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={escaped_project}
EnvironmentFile={escaped_project}/.env
ExecStart={executable}
ExecReload=/bin/kill -HUP $MAINPID
Restart=on-failure
RestartSec=5
TimeoutStopSec=180
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={escaped_project}

[Install]
WantedBy=multi-user.target
"""


def render_brand_service_unit(service_name: str, user: str, group: str, project: str) -> str:
    escaped_project = escape_unit_path(project)
    return f"""[Unit]
Description={service_name} branded subscription API
After=network-online.target mariadb.service
Wants=network-online.target

[Service]
Type=simple
User={user}
Group={group}
WorkingDirectory={escaped_project}
EnvironmentFile={escaped_project}/.env
ExecStart={escaped_project}/.venv/bin/gunicorn --workers 2 --bind 127.0.0.1:${{BRAND_API_PORT}} brand_api:app
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={escaped_project}

[Install]
WantedBy=multi-user.target
"""


def install_service(service_name: str, *, enable_brand_api=False, project_dir: Path | None = None) -> None:
    project_path = Path(project_dir or PROJECT_DIR)
    current_user, current_group = service_identity(service_name, project_path)
    project = str(project_path)
    unit = render_service_unit(service_name, current_user, current_group, project, managed=True)
    brand_service_name = service_name.removesuffix("_bot.service") + "_brand_api.service"
    brand_unit = render_brand_service_unit(brand_service_name, current_user, current_group, project)
    temporary_paths = []
    try:
        for name, content in ((service_name, unit), (brand_service_name, brand_unit)):
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
                handle.write(content)
                temporary_paths.append(handle.name)
            target = f"/etc/systemd/system/{name}"
            subprocess.run(
                privileged(["install", "-m", "0644", temporary_paths[-1], target]),
                check=True,
            )
        subprocess.run(privileged(["systemctl", "daemon-reload"]), check=True)
        for name in ((service_name, brand_service_name) if enable_brand_api else (service_name,)):
            subprocess.run(privileged(["systemctl", "enable", name]), check=True)
            subprocess.run(privileged(["systemctl", "restart", name]), check=True)
    finally:
        for temporary_path in temporary_paths:
            Path(temporary_path).unlink(missing_ok=True)


def wait_for_root(config: dict[str, object], service_name: str, code: str, expiry: int) -> None:
    print("\n" + "=" * 64)
    print(f"ONE-TIME ROOT COMMAND:  /auth {code}")
    print("Send this command to the bot from the Telegram account that must become root.")
    print("The code expires in 30 minutes and stops working after the first root claim.")
    print("=" * 64)
    print(f"Service: {service_name}")
    print(f"Logs: sudo journalctl -u {service_name} -f")
    print(
        "Waiting for the first root admin. Press Ctrl+C to stop waiting; the service keeps running.\n"
    )
    time.sleep(2)
    while time.time() < expiry:
        status = subprocess.run(
            ["systemctl", "is-active", service_name], capture_output=True, text=True, check=False
        )
        if status.stdout.strip() not in {"active", "activating"}:
            print(
                f"Service is not active. Inspect it with: sudo journalctl -u {service_name} -n 100"
            )
            return
        try:
            connection = pymysql.connect(
                host=str(config["db_host"]),
                port=int(config["db_port"]),
                user=str(config["db_user"]),
                password=str(config["db_password"]),
                database=str(config["bot_db"]),
                charset="utf8mb4",
            )
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT telegram_id, name FROM users WHERE permission = 0 LIMIT 1"
                    )
                    root = cursor.fetchone()
                if root:
                    print(f"Root admin authenticated successfully: {root[1]} ({root[0]})")
                    return
            finally:
                connection.close()
        except pymysql.MySQLError:
            pass
        time.sleep(2)
    print("The one-time code expired. Run ./start.sh again to generate a new code.")


def prepare_bootstrap(config: dict[str, object]) -> bool:
    connection = pymysql.connect(
        host=str(config["db_host"]),
        port=int(config["db_port"]),
        user=str(config["db_user"]),
        password=str(config["db_password"]),
        database=str(config["bot_db"]),
        charset="utf8mb4",
        autocommit=True,
    )
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT COUNT(*) FROM users WHERE permission = 0")
            root_exists = bool(cursor.fetchone()[0])
            cursor.execute("SELECT COUNT(*) FROM bot_settings WHERE `key` = 'bootstrap_claimed'")
            bootstrap_claimed = bool(cursor.fetchone()[0])
            if not root_exists and not bootstrap_claimed:
                cursor.execute("DELETE FROM bot_settings WHERE `key` = 'bootstrap_failed_attempts'")
            return root_exists or bootstrap_claimed
    finally:
        connection.close()


def collect_config() -> dict[str, object]:
    print("\nVPN Shop interactive setup\n")
    brand = ask("Brand name", "Starlight")
    slug = service_slug(brand)
    token = ask("Telegram bot token", secret=True)
    db_host = ask("MariaDB host", "127.0.0.1")
    db_port = ask_int("MariaDB port", 3306, minimum=1)
    admin_user = ask("MariaDB administrative user", "root")
    admin_password = ask_secret_optional(
        "MariaDB administrative password (press Enter for an empty password)"
    )
    bot_db = identifier("New bot database name", f"{slug}_bot")
    panel_db = identifier("Existing VPN panel database", "sql_panel_allbatech_net")
    panel_table = identifier("VPN users table", "v2_user")
    db_user = identifier("Limited database user", f"{slug}_bot")
    generated_password = random_password()
    db_password = ask_secret_optional(
        "Limited database password (press Enter to generate a strong password)", generated_password
    )
    allowed_host = ask("Database GRANT host for the bot", "127.0.0.1")
    while True:
        try:
            subscription_url = validate_https(
                ask(
                    "Subscription URL prefix",
                    "https://panel.example.com/api/v1/client/subscribe?token=",
                )
            )
            break
        except ValueError as exc:
            print(exc)
    brand_subscription_url = ask(
        "Public branded subscription URL prefix (blank keeps original links)", ""
    )
    config: dict[str, object] = {
        "brand": brand,
        "service_name": f"{slug}_bot.service",
        "bot_token": token,
        "db_host": db_host,
        "db_port": db_port,
        "db_admin_user": admin_user,
        "db_admin_password": admin_password,
        "bot_db": bot_db,
        "panel_db": panel_db,
        "panel_table": panel_table,
        "db_user": db_user,
        "db_password": db_password,
        "db_allowed_host": allowed_host,
        "subscription_url": subscription_url,
        "brand_subscription_url": brand_subscription_url,
        "brand_api_port": ask_int("Local brand API port", 24121, minimum=1),
        "trial_plan_id": ask_int("Trial plan ID", 4, minimum=1),
        "trial_group_id": ask_int("Trial group ID", 2, minimum=1),
        "trial_days": ask_int("Trial duration in days", 1, minimum=1),
        "trial_transfer_gb": ask_int("Trial traffic in GB", 1, minimum=1),
        "payment_group_id": ask_int("Payment receipt Telegram group ID (0 = root private chat)", 0),
        "payment_topic_id": ask_int("Payment receipt topic ID (0 = no topic)", 0),
        "support_username": ask("Support Telegram username", "@support"),
        "card_number": ask("Card number"),
        "card_holder": ask("Card holder name"),
    }
    while True:
        timezone = ask("Timezone", "Asia/Tehran")
        try:
            ZoneInfo(timezone)
            config["timezone"] = timezone
            break
        except ZoneInfoNotFoundError:
            print("Enter a valid IANA timezone, such as Asia/Tehran or UTC.")
    return config


def resume_existing_setup(env_path: Path) -> None:
    if not env_path.exists():
        raise SystemExit("No .env file exists to resume. Run ./start.sh without --resume.")
    auth_code = f"{secrets.randbelow(900_000) + 100_000:06d}"
    auth_hash = bcrypt.hashpw(auth_code.encode(), bcrypt.gensalt()).decode()
    auth_expiry = int(time.time()) + 30 * 60
    update_env_values(
        env_path,
        {
            "ROOT_TELEGRAM_IDS": "[]",
            "BOOTSTRAP_AUTH_HASH": auth_hash,
            "BOOTSTRAP_AUTH_EXPIRES_AT": auth_expiry,
        },
    )
    settings = Settings(_env_file=env_path)
    bot_url = make_url(settings.bot_db_url.get_secret_value())
    config: dict[str, object] = {
        "brand": settings.brand_name,
        "service_name": f"{service_slug(settings.brand_name)}_bot.service",
        "bot_token": settings.bot_token.get_secret_value(),
        "db_host": bot_url.host or "127.0.0.1",
        "db_port": bot_url.port or 3306,
        "bot_db": bot_url.database or "",
        "db_user": bot_url.username or "",
        "db_password": bot_url.password or "",
    }
    username = validate_bot_token(str(config["bot_token"]))
    identity = f" (@{username})" if username else ""
    print(f"Resuming setup for {settings.brand_name}{identity}...")
    subprocess.run(
        [sys.executable, "-m", "vpnshopbot.app", "--init-db"], cwd=PROJECT_DIR, check=True
    )
    bootstrap_already_claimed = prepare_bootstrap(config)
    from .release import prepare_initial

    prepare_initial(PROJECT_DIR)
    service_name = str(config["service_name"])
    print(f"Installing and starting {service_name}; sudo may ask for your system password...")
    install_service(service_name)
    if bootstrap_already_claimed:
        print(
            f"{service_name} is active. This database already has a root admin or a recorded "
            "bootstrap claim; /auth is disabled."
        )
    else:
        wait_for_root(config, service_name, auth_code, auth_expiry)


def main() -> None:
    parser = argparse.ArgumentParser(description="Configure and install the VPN shop bot")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume from the existing .env after an interrupted setup",
    )
    args = parser.parse_args()
    env_path = PROJECT_DIR / ".env"
    if args.resume:
        resume_existing_setup(env_path)
        return
    if env_path.exists():
        choice = ask("Existing configuration: resume, reconfigure, or cancel", "resume").lower()
        if choice in {"resume", "r"}:
            resume_existing_setup(env_path)
            return
        if choice not in {"reconfigure", "c", "configure"}:
            print("Setup cancelled; no changes were made.")
            return
    config = collect_config()
    admin_password = str(config.pop("db_admin_password"))
    username = validate_bot_token(str(config["bot_token"]))
    if username:
        print(f"Connected to Telegram as @{username}.")
    print("\nCreating the isolated database and limited database account...")
    configure_database(config, admin_password)
    auth_code = f"{secrets.randbelow(900_000) + 100_000:06d}"
    auth_hash = bcrypt.hashpw(auth_code.encode(), bcrypt.gensalt()).decode()
    auth_expiry = int(time.time()) + 30 * 60
    env_path.write_text(render_env(config, auth_hash, auth_expiry), encoding="utf-8")
    env_path.chmod(0o600)
    subprocess.run(
        [sys.executable, "-m", "vpnshopbot.app", "--init-db"], cwd=PROJECT_DIR, check=True
    )
    bootstrap_already_claimed = prepare_bootstrap(config)
    from .release import prepare_initial

    prepare_initial(PROJECT_DIR)
    service_name = str(config["service_name"])
    print(f"Installing and starting {service_name}; sudo may ask for your system password...")
    install_service(service_name)
    if bootstrap_already_claimed:
        print(
            f"{service_name} is active. This database already has a root admin or a recorded "
            "bootstrap claim; /auth is disabled."
        )
    else:
        wait_for_root(config, service_name, auth_code, auth_expiry)


if __name__ == "__main__":
    main()
