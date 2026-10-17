"""Persistent runtime configuration for the local inbox reader.

Docker deployments use PostgreSQL through ``INBOX_DATABASE_URL``. When that
variable is absent, the JSON file backend remains available for local
development and the test suite. The legacy single-mailbox JSON format is
accepted by both backends and imported into PostgreSQL on first startup.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import tempfile
import time
from pathlib import Path
from threading import RLock


ROOT_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = Path(os.environ.get("INBOX_CONFIG_PATH", str(ROOT_DIR / "inbox_config.json"))).expanduser()
DATABASE_URL = os.environ.get("INBOX_DATABASE_URL", "").strip()
DATABASE_CONNECT_RETRIES = 20
APP_HOST = os.environ.get("INBOX_APP_HOST", "127.0.0.1").strip() or "127.0.0.1"
try:
    APP_PORT = int(os.environ.get("INBOX_APP_PORT", "8000"))
except ValueError:
    APP_PORT = 8000
# Seconds between IMAP checks for new mail (per account) used by the push watcher.
POLL_INTERVAL_SECONDS = 30

ACCOUNT_ID_PATTERN = re.compile(r"[0-9a-f]{8}")
_PUBLIC_FIELDS = ("id", "imap_server", "imap_port", "account")


def _clean_account(raw: dict) -> dict | None:
    """Normalise one stored account; returns None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        port = int(raw.get("imap_port", 993))
    except (TypeError, ValueError):
        port = 993
    account = {
        "id": str(raw.get("id") or ""),
        "imap_server": str(raw.get("imap_server") or "").strip(),
        "imap_port": port,
        "account": str(raw.get("account") or "").strip(),
        "password": str(raw.get("password") or ""),
    }
    if not ACCOUNT_ID_PATTERN.fullmatch(account["id"]):
        account["id"] = secrets.token_hex(4)
    if not account["imap_server"] and not account["account"]:
        return None
    return account


def _public(account: dict) -> dict:
    value = {key: account[key] for key in _PUBLIC_FIELDS}
    value["has_password"] = bool(account["password"])
    value["configured"] = bool(account["imap_server"] and account["account"] and account["password"])
    return value


def _validate(imap_server, imap_port, account) -> tuple[str, int, str]:
    server = str(imap_server or "").strip()
    user = str(account or "").strip()
    try:
        port = int(imap_port)
    except (TypeError, ValueError) as exc:
        raise ValueError("IMAP 端口必须是数字。") from exc
    if not server:
        raise ValueError("请填写 IMAP 服务器地址。")
    if len(server) > 255 or any(ch.isspace() for ch in server) or "/" in server:
        raise ValueError("IMAP 服务器地址格式无效，只需填写主机名。")
    if not 1 <= port <= 65535:
        raise ValueError("IMAP 端口必须在 1 到 65535 之间。")
    if not user:
        raise ValueError("请填写邮箱账号。")
    if len(user) > 320:
        raise ValueError("邮箱账号过长。")
    return server, port, user


class AccountNotFound(KeyError):
    """Raised when an account id does not exist."""


class Config:
    """Thread-safe multi-account store used by HTTP, IMAP and watcher threads."""

    _lock = RLock()
    _accounts: list[dict] | None = None
    _postgres_ready = False

    @classmethod
    def _uses_postgres(cls) -> bool:
        return bool(DATABASE_URL)

    @staticmethod
    def _pg_connection():
        try:
            import psycopg
        except ImportError as exc:  # pragma: no cover - exercised in Docker.
            raise RuntimeError("PostgreSQL 存储需要安装 psycopg。") from exc
        return psycopg.connect(DATABASE_URL, connect_timeout=5)

    @staticmethod
    def _account_from_row(row) -> dict:
        return {
            "id": row[0],
            "imap_server": row[1],
            "imap_port": int(row[2]),
            "account": row[3],
            "password": row[4],
        }

    @classmethod
    def initialize(cls) -> None:
        """Create the PostgreSQL schema and import a legacy JSON config once."""
        if not cls._uses_postgres():
            return
        if cls._postgres_ready:
            return
        last_error: Exception | None = None
        for attempt in range(DATABASE_CONNECT_RETRIES):
            try:
                with cls._lock:
                    if cls._postgres_ready:
                        return
                    cls._initialize_postgres_locked()
                    cls._postgres_ready = True
                return
            except Exception as exc:  # Database may still be starting in Compose.
                last_error = exc
                if attempt + 1 < DATABASE_CONNECT_RETRIES:
                    time.sleep(min(2, 0.5 + attempt * 0.1))
        raise RuntimeError("无法连接 PostgreSQL，请检查数据库容器和连接配置。") from last_error

    @classmethod
    def _initialize_postgres_locked(cls) -> None:
        with cls._pg_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS mail_accounts (
                        id VARCHAR(8) PRIMARY KEY,
                        imap_server VARCHAR(255) NOT NULL,
                        imap_port INTEGER NOT NULL CHECK (imap_port BETWEEN 1 AND 65535),
                        account VARCHAR(320) NOT NULL,
                        password TEXT NOT NULL,
                        created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                cursor.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS mail_accounts_server_account_idx
                    ON mail_accounts (LOWER(imap_server), LOWER(account))
                    """
                )
                cursor.execute("SELECT COUNT(*) FROM mail_accounts")
                is_empty = cursor.fetchone()[0] == 0
                if is_empty:
                    legacy_accounts = cls._read_json_locked()
                    for account in legacy_accounts:
                        cursor.execute(
                            """
                            INSERT INTO mail_accounts
                                (id, imap_server, imap_port, account, password)
                            VALUES (%s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                account["id"],
                                account["imap_server"],
                                account["imap_port"],
                                account["account"],
                                account["password"],
                            ),
                        )

    # ------------------------------------------------------------------ storage
    @classmethod
    def _load_locked(cls) -> list[dict]:
        if cls._uses_postgres():
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, imap_server, imap_port, account, password
                        FROM mail_accounts
                        ORDER BY created_at, id
                        """
                    )
                    return [cls._account_from_row(row) for row in cursor.fetchall()]
        if cls._accounts is not None:
            return cls._accounts
        accounts = cls._read_json_locked()
        cls._accounts = accounts
        return accounts

    @classmethod
    def _read_json_locked(cls) -> list[dict]:
        saved: object = {}
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            saved = {}
        accounts: list[dict] = []
        migrated = False
        if isinstance(saved, dict):
            if isinstance(saved.get("accounts"), list):
                raw_accounts = saved["accounts"]
            elif saved.get("imap_server") or saved.get("account"):
                raw_accounts = [saved]  # Legacy single-mailbox file.
                migrated = True
            else:
                raw_accounts = []
            seen: set[str] = set()
            for raw in raw_accounts:
                cleaned = _clean_account(raw)
                if not cleaned:
                    continue
                while cleaned["id"] in seen:
                    cleaned["id"] = secrets.token_hex(4)
                seen.add(cleaned["id"])
                accounts.append(cleaned)
        if migrated:
            try:
                cls._write_locked(accounts)
            except OSError:
                pass  # Keep the in-memory migration; the next save will retry.
        return accounts

    @classmethod
    def _write_locked(cls, accounts: list[dict]) -> None:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".inbox-config-", dir=CONFIG_PATH.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"accounts": accounts}, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.chmod(temporary, 0o600)
            os.replace(temporary, CONFIG_PATH)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @classmethod
    def _find_locked(cls, account_id: str) -> dict:
        if cls._uses_postgres():
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        SELECT id, imap_server, imap_port, account, password
                        FROM mail_accounts WHERE id = %s
                        """,
                        (account_id,),
                    )
                    row = cursor.fetchone()
                    if row:
                        return cls._account_from_row(row)
            raise AccountNotFound(account_id)
        for account in cls._load_locked():
            if account["id"] == account_id:
                return account
        raise AccountNotFound(account_id)

    @classmethod
    def _ensure_unique_locked(cls, server: str, user: str, skip_id: str | None) -> None:
        if cls._uses_postgres():
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    if skip_id:
                        cursor.execute(
                            """
                            SELECT 1 FROM mail_accounts
                            WHERE LOWER(imap_server) = LOWER(%s)
                              AND LOWER(account) = LOWER(%s)
                              AND id <> %s
                            LIMIT 1
                            """,
                            (server, user, skip_id),
                        )
                    else:
                        cursor.execute(
                            """
                            SELECT 1 FROM mail_accounts
                            WHERE LOWER(imap_server) = LOWER(%s)
                              AND LOWER(account) = LOWER(%s)
                            LIMIT 1
                            """,
                            (server, user),
                        )
                    if cursor.fetchone():
                        raise ValueError("该邮箱已添加，请勿重复配置。")
            return
        for other in cls._load_locked():
            if other["id"] == skip_id:
                continue
            if other["imap_server"].lower() == server.lower() and other["account"].lower() == user.lower():
                raise ValueError("该邮箱已添加，请勿重复配置。")

    # ------------------------------------------------------------------ reads
    @classmethod
    def snapshot(cls) -> dict:
        """Return public settings for every account, never exposing passwords."""
        with cls._lock:
            accounts = [_public(account) for account in cls._load_locked()]
        return {"configured": any(a["configured"] for a in accounts), "accounts": accounts}

    @classmethod
    def account_ids(cls) -> list[str]:
        with cls._lock:
            return [account["id"] for account in cls._load_locked()]

    @classmethod
    def public_account(cls, account_id: str) -> dict:
        with cls._lock:
            return _public(cls._find_locked(account_id))

    @classmethod
    def connection_values(cls, account_id: str) -> tuple[str, int, str, str]:
        """Read the latest connection values of one account for an IMAP request."""
        with cls._lock:
            account = cls._find_locked(account_id)
            return account["imap_server"], account["imap_port"], account["account"], account["password"]

    # ------------------------------------------------------------------ writes
    @classmethod
    def add_account(cls, imap_server, imap_port, account, password) -> dict:
        server, port, user = _validate(imap_server, imap_port, account)
        secret = "" if password is None else str(password)
        if not secret:
            raise ValueError("请填写授权码或应用密码。")
        with cls._lock:
            if cls._uses_postgres():
                return cls._add_postgres_locked(server, port, user, secret)
            accounts = cls._load_locked()
            cls._ensure_unique_locked(server, user, None)
            existing = {a["id"] for a in accounts}
            new_id = secrets.token_hex(4)
            while new_id in existing:
                new_id = secrets.token_hex(4)
            entry = {
                "id": new_id,
                "imap_server": server,
                "imap_port": port,
                "account": user,
                "password": secret,
            }
            next_accounts = [*accounts, entry]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts
            return _public(entry)

    @classmethod
    def update_account(cls, account_id, imap_server, imap_port, account, password) -> dict:
        """Update an account, preserving the stored password when ``password`` is blank."""
        server, port, user = _validate(imap_server, imap_port, account)
        with cls._lock:
            if cls._uses_postgres():
                return cls._update_postgres_locked(account_id, server, port, user, password)
            current = cls._find_locked(account_id)
            cls._ensure_unique_locked(server, user, account_id)
            secret = current["password"] if password in (None, "") else str(password)
            if not secret:
                raise ValueError("请填写授权码或应用密码。")
            entry = {
                "id": account_id,
                "imap_server": server,
                "imap_port": port,
                "account": user,
                "password": secret,
            }
            next_accounts = [entry if a["id"] == account_id else a for a in cls._load_locked()]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts
            return _public(entry)

    @classmethod
    def remove_account(cls, account_id: str) -> None:
        with cls._lock:
            if cls._uses_postgres():
                cls._remove_postgres_locked(account_id)
                return
            cls._find_locked(account_id)
            next_accounts = [a for a in cls._load_locked() if a["id"] != account_id]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts

    @classmethod
    def reset_for_tests(cls) -> None:
        with cls._lock:
            cls._accounts = None
            cls._postgres_ready = False

    @classmethod
    def _add_postgres_locked(cls, server: str, port: int, user: str, secret: str) -> dict:
        cls.initialize()
        cls._ensure_unique_locked(server, user, None)
        entry = {
            "id": secrets.token_hex(4),
            "imap_server": server,
            "imap_port": port,
            "account": user,
            "password": secret,
        }
        try:
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO mail_accounts (id, imap_server, imap_port, account, password)
                        VALUES (%s, %s, %s, %s, %s)
                        """,
                        (entry["id"], server, port, user, secret),
                    )
        except Exception as exc:
            if exc.__class__.__name__ == "UniqueViolation":
                raise ValueError("该邮箱已添加，请勿重复配置。") from exc
            raise
        return _public(entry)

    @classmethod
    def _update_postgres_locked(cls, account_id: str, server: str, port: int, user: str, password) -> dict:
        cls.initialize()
        current = cls._find_locked(account_id)
        cls._ensure_unique_locked(server, user, account_id)
        secret = current["password"] if password in (None, "") else str(password)
        if not secret:
            raise ValueError("请填写授权码或应用密码。")
        entry = {
            "id": account_id,
            "imap_server": server,
            "imap_port": port,
            "account": user,
            "password": secret,
        }
        try:
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE mail_accounts
                        SET imap_server = %s, imap_port = %s, account = %s,
                            password = %s, updated_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                        """,
                        (server, port, user, secret, account_id),
                    )
                    if cursor.rowcount != 1:
                        raise AccountNotFound(account_id)
        except Exception as exc:
            if exc.__class__.__name__ == "UniqueViolation":
                raise ValueError("该邮箱已添加，请勿重复配置。") from exc
            raise
        return _public(entry)

    @classmethod
    def _remove_postgres_locked(cls, account_id: str) -> None:
        cls.initialize()
        with cls._pg_connection() as conn:
            with conn.cursor() as cursor:
                cursor.execute("DELETE FROM mail_accounts WHERE id = %s", (account_id,))
                if cursor.rowcount != 1:
                    raise AccountNotFound(account_id)
