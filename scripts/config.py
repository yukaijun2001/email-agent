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
GRAPH_SERVER = "graph.microsoft.com"
_PUBLIC_FIELDS = ("id", "imap_server", "imap_port", "account")


def _clean_account(raw: dict) -> dict | None:
    """Normalise one stored account; returns None when it is unusable."""
    if not isinstance(raw, dict):
        return None
    try:
        port = int(raw.get("imap_port", 993))
    except (TypeError, ValueError):
        port = 993
    protocol = str(raw.get("protocol") or "imap").strip().lower()
    if protocol == "outlook_imap":
        protocol = "graph"
    if protocol not in {"imap", "graph"}:
        protocol = "imap"
    account = {
        "id": str(raw.get("id") or ""),
        "protocol": protocol,
        "imap_server": str(raw.get("imap_server") or "").strip(),
        "imap_port": port,
        "account": str(raw.get("account") or "").strip(),
        "password": str(raw.get("password") or ""),
        "client_id": str(raw.get("client_id") or "").strip(),
        "refresh_token": str(raw.get("refresh_token") or "").strip(),
    }
    if protocol == "graph":
        account["imap_server"] = GRAPH_SERVER
        account["imap_port"] = 443
    if not ACCOUNT_ID_PATTERN.fullmatch(account["id"]):
        account["id"] = secrets.token_hex(4)
    if not account["imap_server"] and not account["account"]:
        return None
    return account


def _public(account: dict) -> dict:
    value = {key: account[key] for key in _PUBLIC_FIELDS}
    protocol = account.get("protocol") or "imap"
    value["protocol"] = protocol
    value["client_id"] = account.get("client_id") or ""
    value["has_password"] = bool(account.get("password"))
    value["has_refresh_token"] = bool(account.get("refresh_token"))
    if protocol == "graph":
        value["configured"] = bool(
            account.get("account") and account.get("client_id") and account.get("refresh_token")
        )
    else:
        value["configured"] = bool(account["imap_server"] and account["account"] and account["password"])
    return value


def parse_graph_import(text: str) -> tuple[list[dict], list[str]]:
    """Parse ``email----password----client_id----refresh_token`` lines.

    Separators of one to four dashes are accepted. Four dashes are preferred
    because a client id is usually a UUID.
    """
    accounts: list[dict] = []
    errors: list[str] = []
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        trimmed = line.strip()
        if not trimmed:
            continue
        parts = _split_graph_line(trimmed)
        if not parts:
            errors.append(f"第 {index} 行格式错误，请使用 邮箱----密码----client_id----refresh_token。")
            continue
        email, password, client_id, refresh_token = parts
        if "@" not in email or email.startswith("@") or email.endswith("@") or any(ch.isspace() for ch in email):
            errors.append(f"第 {index} 行邮箱格式无效。")
            continue
        accounts.append({
            "account": email,
            "password": password,
            "client_id": client_id,
            "refresh_token": refresh_token,
        })
    return accounts, errors


def _split_graph_line(trimmed: str) -> list[str] | None:
    # A UUID contains single dashes; match it as a field before splitting.
    single_dash_uuid = re.fullmatch(
        r"(.+?@[^\s@]+?)(?<!-)-(?!-)(.+?)(?<!-)-(?!-)"
        r"([0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})-(?!-)(.+)",
        trimmed,
    )
    if single_dash_uuid:
        return [part.strip() for part in single_dash_uuid.groups()]
    for dash_count in range(4, 0, -1):
        separator = rf"(?<!-)-{{{dash_count}}}(?!-)"
        if len(re.findall(separator, trimmed)) >= 3:
            cleaned = [part.strip() for part in re.split(separator, trimmed, maxsplit=3)]
            return cleaned if len(cleaned) == 4 and all(cleaned) else None
    return None


def _parse_outlook_import(text: str) -> tuple[list[dict], list[dict]]:
    accounts, errors = [], []
    for index, line in enumerate(str(text or "").splitlines(), start=1):
        if not line.strip():
            continue
        parsed, issues = parse_graph_import(line)
        if issues:
            errors.append({"line": index, "message": issues[0].split("行", 1)[1]})
            continue
        item = parsed[0]
        try:
            _validate_outlook_oauth(item["account"], item["client_id"], item["refresh_token"])
        except ValueError as exc:
            errors.append({"line": index, "message": str(exc)})
        else:
            accounts.append({**item, "line": index})
    return accounts, errors


def _validate_graph(account, password, client_id, refresh_token) -> tuple[str, str, str, str]:
    user = str(account or "").strip()
    secret = "" if password is None else str(password)
    app_id = str(client_id or "").strip()
    token = "" if refresh_token is None else str(refresh_token).strip()
    if "@" not in user or user.startswith("@") or user.endswith("@") or any(ch.isspace() for ch in user):
        raise ValueError("请填写有效的 Outlook 邮箱。")
    if len(user) > 320:
        raise ValueError("邮箱账号过长。")
    if len(secret) > 1024:
        raise ValueError("邮箱密码过长。")
    if not app_id or len(app_id) > 128 or any(ch.isspace() for ch in app_id):
        raise ValueError("请填写有效的 client_id。")
    if len(app_id) < 8:
        raise ValueError("请填写有效的 client_id。")
    if not token:
        raise ValueError("请填写 refresh_token。")
    if len(token) > 8192 or any(ch.isspace() for ch in token):
        raise ValueError("refresh_token 格式无效。")
    return user, secret, app_id, token


def _validate_outlook_oauth(account, client_id, refresh_token) -> tuple[str, str, str]:
    """Validate Outlook Graph OAuth credentials."""
    user = str(account or "").strip()
    app_id = str(client_id or "").strip()
    token = "" if refresh_token is None else str(refresh_token).strip()
    if "@" not in user or user.startswith("@") or user.endswith("@") or any(ch.isspace() for ch in user):
        raise ValueError("请填写有效的 Outlook 邮箱。")
    if len(user) > 320:
        raise ValueError("邮箱账号过长。")
    if not app_id or len(app_id) > 128 or any(ch.isspace() for ch in app_id):
        raise ValueError("请填写有效的 client_id。")
    if len(app_id) < 8:
        raise ValueError("请填写有效的 client_id。")
    if not token:
        raise ValueError("请先完成 Microsoft OAuth2 授权，或填写 refresh_token。")
    if len(token) > 8192 or any(ch.isspace() for ch in token):
        raise ValueError("refresh_token 格式无效。")
    return user, app_id, token


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
        protocol = "graph" if row[1] == "outlook_imap" else row[1] or "imap"
        server = row[2]
        port = int(row[3])
        if protocol == "graph":
            server, port = GRAPH_SERVER, 443
        return {
            "id": row[0],
            "protocol": protocol,
            "imap_server": server,
            "imap_port": port,
            "account": row[4],
            "password": row[5],
            "client_id": row[6] or "",
            "refresh_token": row[7] or "",
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
                    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS protocol VARCHAR(16) NOT NULL DEFAULT 'imap'"
                )
                cursor.execute(
                    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS client_id VARCHAR(128) NOT NULL DEFAULT ''"
                )
                cursor.execute(
                    "ALTER TABLE mail_accounts ADD COLUMN IF NOT EXISTS refresh_token TEXT NOT NULL DEFAULT ''"
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
                                (id, protocol, imap_server, imap_port, account, password, client_id, refresh_token)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING
                            """,
                            (
                                account["id"],
                                account.get("protocol") or "imap",
                                account["imap_server"],
                                account["imap_port"],
                                account["account"],
                                account["password"],
                                account.get("client_id") or "",
                                account.get("refresh_token") or "",
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
                        SELECT id, protocol, imap_server, imap_port, account, password, client_id, refresh_token
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
                if isinstance(raw, dict) and raw.get("protocol") == "outlook_imap":
                    migrated = True
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
                        SELECT id, protocol, imap_server, imap_port, account, password, client_id, refresh_token
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
                            WHERE LOWER(CASE WHEN protocol = 'outlook_imap'
                                       THEN 'graph.microsoft.com' ELSE imap_server END) = LOWER(%s)
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
                            WHERE LOWER(CASE WHEN protocol = 'outlook_imap'
                                       THEN 'graph.microsoft.com' ELSE imap_server END) = LOWER(%s)
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

    @classmethod
    def mailbox(cls, account_id: str) -> dict:
        """Return one stored account, including secrets, for a reader thread."""
        with cls._lock:
            return dict(cls._find_locked(account_id))

    @classmethod
    def is_graph(cls, account_id: str) -> bool:
        return cls.mailbox(account_id).get("protocol") == "graph"

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
                "protocol": "imap",
                "imap_server": server,
                "imap_port": port,
                "account": user,
                "password": secret,
                "client_id": "",
                "refresh_token": "",
            }
            next_accounts = [*accounts, entry]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts
            return _public(entry)

    @classmethod
    def update_account(cls, account_id, imap_server, imap_port, account, password, protocol=None, client_id=None, refresh_token=None) -> dict:
        """Update an account, preserving stored secrets when the new values are blank."""
        with cls._lock:
            current = cls._find_locked(account_id)
            kind = str(protocol or current.get("protocol") or "imap").strip().lower()
            if kind == "outlook_imap":
                kind = "graph"
            if kind == "graph":
                secret = current.get("password") if password in (None, "") else str(password)
                token = current.get("refresh_token") if refresh_token in (None, "") else refresh_token
                app_id = current.get("client_id") if client_id is None else client_id
                return cls._write_graph_locked(account_id, account, secret, app_id, token, current)
            server, port, user = _validate(imap_server, imap_port, account)
            if cls._uses_postgres():
                return cls._update_postgres_locked(account_id, server, port, user, password)
            cls._ensure_unique_locked(server, user, account_id)
            secret = current["password"] if password in (None, "") else str(password)
            if not secret:
                raise ValueError("请填写授权码或应用密码。")
            entry = {
                "id": account_id,
                "protocol": "imap",
                "imap_server": server,
                "imap_port": port,
                "account": user,
                "password": secret,
                "client_id": "",
                "refresh_token": "",
            }
            next_accounts = [entry if a["id"] == account_id else a for a in cls._load_locked()]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts
            return _public(entry)

    @classmethod
    def add_graph_account(cls, account, password, client_id, refresh_token) -> dict:
        user, secret, app_id, token = _validate_graph(account, password, client_id, refresh_token)
        with cls._lock:
            return cls._write_graph_locked(None, user, secret, app_id, token, None)

    @classmethod
    def add_outlook_account(cls, account, client_id, refresh_token) -> dict:
        user, app_id, token = _validate_outlook_oauth(account, client_id, refresh_token)
        with cls._lock:
            return cls._write_outlook_locked(None, user, app_id, token, None)

    @classmethod
    def import_graph_accounts(cls, text: str) -> dict:
        """Import Outlook lines. One bad line does not roll back lines already saved."""
        parsed, errors = parse_graph_import(text)
        if not parsed and not errors:
            raise ValueError("请粘贴至少一行 Outlook 账号。")
        added: list[dict] = []
        with cls._lock:
            for item in parsed:
                try:
                    added.append(cls._write_graph_locked(
                        None, item["account"], item["password"], item["client_id"], item["refresh_token"], None,
                    ))
                except ValueError as exc:
                    errors.append(f"{item['account']}：{exc}")
        return {"added": added, "errors": errors}

    @classmethod
    def preview_outlook_accounts(cls, text: str) -> dict:
        parsed, errors = _parse_outlook_import(text)
        return {
            "accounts": [{key: item[key] for key in ("line", "account", "client_id")} for item in parsed],
            "errors": errors,
        }

    @classmethod
    def import_outlook_accounts(cls, text: str) -> dict:
        parsed, errors = _parse_outlook_import(text)
        if not parsed and not errors:
            raise ValueError("请粘贴至少一行 Outlook 账号。")
        added, added_lines = [], []
        with cls._lock:
            for item in parsed:
                try:
                    saved = cls._write_outlook_locked(
                        None, item["account"], item["client_id"], item["refresh_token"], None,
                    )
                except ValueError as exc:
                    errors.append({"line": item["line"], "message": str(exc)})
                else:
                    added.append(saved)
                    added_lines.append(item["line"])
        return {"added": added, "added_lines": added_lines, "errors": errors}

    @classmethod
    def replace_refresh_token(cls, account_id: str, previous: str, new_token: str) -> None:
        """Persist a rotated OAuth refresh token when it still matches ``previous``."""
        token = str(new_token or "").strip()
        if not token or token == previous or len(token) > 8192 or any(ch.isspace() for ch in token):
            return
        with cls._lock:
            current = cls._find_locked(account_id)
            if current.get("protocol") != "graph" \
                    or current.get("refresh_token") != previous:
                return
            current["refresh_token"] = token
            if cls._uses_postgres():
                with cls._pg_connection() as conn:
                    with conn.cursor() as cursor:
                        cursor.execute(
                            """
                            UPDATE mail_accounts
                            SET refresh_token = %s, updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s AND refresh_token = %s
                            """,
                            (token, account_id, previous),
                        )
                return
            next_accounts = [current if a["id"] == account_id else a for a in cls._load_locked()]
            cls._write_locked(next_accounts)
            cls._accounts = next_accounts

    @classmethod
    def _write_graph_locked(cls, account_id: str | None, account, password, client_id, refresh_token, current: dict | None) -> dict:
        user, secret, app_id, token = _validate_graph(account, password, client_id, refresh_token)
        cls._ensure_unique_locked(GRAPH_SERVER, user, account_id)
        if account_id is None:
            accounts = [] if cls._uses_postgres() else cls._load_locked()
            existing = {item["id"] for item in accounts}
            new_id = secrets.token_hex(4)
            while new_id in existing:
                new_id = secrets.token_hex(4)
            account_id = new_id
        entry = {
            "id": account_id,
            "protocol": "graph",
            "imap_server": GRAPH_SERVER,
            "imap_port": 443,
            "account": user,
            "password": secret,
            "client_id": app_id,
            "refresh_token": token,
        }
        if cls._uses_postgres():
            return cls._upsert_graph_postgres_locked(entry, current is not None)
        if current is None:
            next_accounts = [*cls._load_locked(), entry]
        else:
            next_accounts = [entry if item["id"] == account_id else item for item in cls._load_locked()]
        cls._write_locked(next_accounts)
        cls._accounts = next_accounts
        return _public(entry)

    @classmethod
    def _write_outlook_locked(cls, account_id, account, client_id, refresh_token, current):
        user, app_id, token = _validate_outlook_oauth(account, client_id, refresh_token)
        return cls._write_graph_locked(account_id, user, "", app_id, token, current)

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
            "protocol": "imap",
            "imap_server": server,
            "imap_port": port,
            "account": user,
            "password": secret,
            "client_id": "",
            "refresh_token": "",
        }
        try:
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        INSERT INTO mail_accounts
                            (id, protocol, imap_server, imap_port, account, password, client_id, refresh_token)
                        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                        """,
                        (entry["id"], "imap", server, port, user, secret, "", ""),
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
            "protocol": "imap",
            "imap_server": server,
            "imap_port": port,
            "account": user,
            "password": secret,
            "client_id": "",
            "refresh_token": "",
        }
        try:
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE mail_accounts
                        SET protocol = 'imap', imap_server = %s, imap_port = %s, account = %s,
                            password = %s, client_id = '', refresh_token = '',
                            updated_at = CURRENT_TIMESTAMP
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
    def _upsert_graph_postgres_locked(cls, entry: dict, updating: bool) -> dict:
        cls.initialize()
        try:
            with cls._pg_connection() as conn:
                with conn.cursor() as cursor:
                    if updating:
                        cursor.execute(
                            """
                            UPDATE mail_accounts
                            SET protocol = 'graph', imap_server = %s, imap_port = %s, account = %s,
                                password = %s, client_id = %s, refresh_token = %s,
                                updated_at = CURRENT_TIMESTAMP
                            WHERE id = %s
                            """,
                            (
                                entry["imap_server"], entry["imap_port"], entry["account"], entry["password"],
                                entry["client_id"], entry["refresh_token"], entry["id"],
                            ),
                        )
                        if cursor.rowcount != 1:
                            raise AccountNotFound(entry["id"])
                    else:
                        cursor.execute(
                            """
                            INSERT INTO mail_accounts
                                (id, protocol, imap_server, imap_port, account, password, client_id, refresh_token)
                            VALUES (%s, 'graph', %s, %s, %s, %s, %s, %s)
                            """,
                            (
                                entry["id"], entry["imap_server"], entry["imap_port"], entry["account"],
                                entry["password"], entry["client_id"], entry["refresh_token"],
                            ),
                        )
        except AccountNotFound:
            raise
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
