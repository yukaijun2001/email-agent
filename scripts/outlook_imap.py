"""Outlook IMAP authentication through Microsoft OAuth2 and SASL XOAUTH2.

The module owns two short-lived caches:

* access tokens refreshed from the account's persisted refresh token;
* device-code authorization attempts started by the web UI.

Only refresh tokens are persisted. Access tokens and device codes stay in
memory and are never returned by the account APIs.
"""

from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

try:
    from . import config
    from .imap_reader import MailboxError
except ImportError:  # Running scripts/main.py directly.
    import config
    from imap_reader import MailboxError


AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
DEVICE_CODE_URL = f"{AUTHORITY}/devicecode"
TOKEN_URL = f"{AUTHORITY}/token"
IMAP_SCOPE = "offline_access https://outlook.office.com/IMAP.AccessAsUser.All"
REQUEST_TIMEOUT_SECONDS = 20
MIN_POLL_INTERVAL_SECONDS = 5


def _json_object(payload: bytes) -> dict[str, Any]:
    try:
        value = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MailboxError("Microsoft OAuth2 返回了无法解析的内容。") from exc
    if not isinstance(value, dict):
        raise MailboxError("Microsoft OAuth2 返回了无法解析的内容。")
    return value


def _oauth_error(payload: dict[str, Any], status: int = 0) -> str:
    code = str(payload.get("error") or "")
    description = str(payload.get("error_description") or "")
    blob = f"{code} {description}".lower()
    if code == "authorization_pending":
        return "pending"
    if code == "slow_down":
        return "slow_down"
    if code in {"authorization_declined", "access_denied"}:
        return "Microsoft 授权已被拒绝，请重新开始授权。"
    if code in {"expired_token", "code_expired"}:
        return "Microsoft 设备授权已过期，请重新开始授权。"
    if code == "invalid_grant":
        return "Outlook refresh_token 已失效，请重新完成 Microsoft 授权。"
    if code in {"invalid_client", "unauthorized_client"} or "aadsts700016" in blob:
        return "Client ID 无效，或应用未启用公共客户端/设备代码登录。"
    if code == "invalid_scope" or "aadsts70011" in blob:
        return "Microsoft 应用未允许 Outlook IMAP OAuth2 权限。"
    if "aadsts50020" in blob or "personal microsoft accounts" in blob:
        return "该应用未允许个人 Microsoft 账号，请修改应用的受支持账号类型。"
    if description:
        return description[:220]
    if status:
        return f"Microsoft OAuth2 请求失败（HTTP {status}）。"
    return "Microsoft OAuth2 请求失败。"


def _request_form(url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode("utf-8"),
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return int(response.status), _json_object(response.read())
    except urllib.error.HTTPError as exc:
        return int(exc.code), _json_object(exc.read())
    except urllib.error.URLError as exc:
        raise MailboxError("无法连接 Microsoft OAuth2 服务，请检查服务器网络。") from exc
    except TimeoutError as exc:
        raise MailboxError("连接 Microsoft OAuth2 服务超时，请稍后重试。") from exc


class OutlookOAuth:
    """Refresh Outlook access tokens and manage device-code authorization."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}
        # account_id -> (refresh_token, access_token, usable_until_epoch)
        self._tokens: dict[str, tuple[str, str, float]] = {}
        # opaque flow id -> server-only device-code state
        self._flows: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._flows.clear()

    def prune(self, keep_ids: set[str]) -> None:
        with self._lock:
            for account_id in list(self._tokens):
                if account_id not in keep_ids:
                    self._tokens.pop(account_id, None)
                    self._account_locks.pop(account_id, None)
            now = time.time()
            for flow_id in list(self._flows):
                if self._flows[flow_id]["expires_at"] <= now:
                    self._flows.pop(flow_id, None)

    def _account_lock(self, account_id: str) -> threading.Lock:
        with self._lock:
            lock = self._account_locks.get(account_id)
            if lock is None:
                lock = threading.Lock()
                self._account_locks[account_id] = lock
            return lock

    @staticmethod
    def _credentials(account_id: str) -> dict:
        try:
            account = config.Config.mailbox(account_id)
        except config.AccountNotFound as exc:
            raise MailboxError("邮箱不存在或已被删除。") from exc
        if account.get("protocol") != "outlook_imap":
            raise MailboxError("这个邮箱不是 Outlook IMAP OAuth2 账号。")
        if not account.get("account") or not account.get("client_id") or not account.get("refresh_token"):
            raise MailboxError("请先完成 Outlook IMAP OAuth2 授权。")
        return account

    def access_token(self, account_id: str, *, force_refresh: bool = False) -> str:
        """Return a valid Outlook IMAP access token and persist token rotation."""
        with self._account_lock(account_id):
            account = self._credentials(account_id)
            refresh_token = account["refresh_token"]
            now = time.time()
            cached = self._tokens.get(account_id)
            if not force_refresh and cached and cached[0] == refresh_token and cached[2] > now:
                return cached[1]
            token, new_refresh, expires_in = self._refresh(account["client_id"], refresh_token)
            if new_refresh and new_refresh != refresh_token:
                config.Config.replace_refresh_token(account_id, refresh_token, new_refresh)
                refresh_token = new_refresh
            usable_until = now + max(30, expires_in - 120)
            self._tokens[account_id] = (refresh_token, token, usable_until)
            return token

    @staticmethod
    def _refresh(client_id: str, refresh_token: str) -> tuple[str, str, int]:
        status, payload = _request_form(TOKEN_URL, {
            "client_id": client_id,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "scope": IMAP_SCOPE,
        })
        if status != 200 or not payload.get("access_token"):
            raise MailboxError(_oauth_error(payload, status))
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        return (
            str(payload["access_token"]),
            str(payload.get("refresh_token") or ""),
            expires_in,
        )

    def start_device_flow(
        self, account: str, client_id: str, account_id: str | None = None
    ) -> dict[str, Any]:
        """Start a Microsoft device-code flow and return only public instructions."""
        user = str(account or "").strip()
        app_id = str(client_id or "").strip()
        if "@" not in user or user.startswith("@") or user.endswith("@") or any(ch.isspace() for ch in user):
            raise ValueError("请填写有效的 Outlook 邮箱。")
        if not app_id or len(app_id) < 8 or len(app_id) > 128 or any(ch.isspace() for ch in app_id):
            raise ValueError("请填写有效的 client_id。")
        if account_id:
            try:
                existing = config.Config.mailbox(account_id)
            except config.AccountNotFound as exc:
                raise ValueError("邮箱不存在或已被删除。") from exc
            if existing.get("protocol") != "outlook_imap":
                raise ValueError("只能为 Outlook IMAP 账号重新授权。")
        status, payload = _request_form(DEVICE_CODE_URL, {"client_id": app_id, "scope": IMAP_SCOPE})
        if status != 200 or not all(payload.get(key) for key in ("device_code", "user_code")):
            raise MailboxError(_oauth_error(payload, status))
        try:
            expires_in = max(60, int(payload.get("expires_in") or 900))
            interval = max(MIN_POLL_INTERVAL_SECONDS, int(payload.get("interval") or 5))
        except (TypeError, ValueError):
            expires_in, interval = 900, MIN_POLL_INTERVAL_SECONDS
        flow_id = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            self._flows[flow_id] = {
                "account": user,
                "account_id": account_id or "",
                "client_id": app_id,
                "device_code": str(payload["device_code"]),
                "expires_at": now + expires_in,
                "interval": interval,
                "next_poll": now,
            }
        verification_uri = str(
            payload.get("verification_uri") or payload.get("verification_url")
            or "https://microsoft.com/devicelogin"
        )
        return {
            "flow_id": flow_id,
            "user_code": str(payload["user_code"]),
            "verification_uri": verification_uri,
            "message": str(payload.get("message") or ""),
            "expires_in": expires_in,
            "interval": interval,
        }

    def poll_device_flow(self, flow_id: str) -> dict[str, Any]:
        """Poll one device-code flow and save the mailbox after authorization."""
        key = str(flow_id or "").strip()
        with self._lock:
            flow = self._flows.get(key)
            if flow is None:
                raise ValueError("授权会话不存在或已过期，请重新开始授权。")
            now = time.time()
            if flow["expires_at"] <= now:
                self._flows.pop(key, None)
                raise ValueError("Microsoft 设备授权已过期，请重新开始授权。")
            if flow["next_poll"] > now:
                return {"status": "pending", "retry_after": max(1, int(flow["next_poll"] - now + 0.999))}
            flow["next_poll"] = now + flow["interval"]
            request_values = {
                "client_id": flow["client_id"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "device_code": flow["device_code"],
            }
        status, payload = _request_form(TOKEN_URL, request_values)
        if status != 200 or not payload.get("access_token"):
            error = _oauth_error(payload, status)
            if error in {"pending", "slow_down"}:
                with self._lock:
                    current = self._flows.get(key)
                    if current is flow and error == "slow_down":
                        current["interval"] += 5
                        current["next_poll"] = time.time() + current["interval"]
                    retry_after = current["interval"] if current is not None else MIN_POLL_INTERVAL_SECONDS
                return {"status": "pending", "retry_after": retry_after}
            with self._lock:
                self._flows.pop(key, None)
            raise MailboxError(error)
        refresh_token = str(payload.get("refresh_token") or "")
        if not refresh_token:
            with self._lock:
                self._flows.pop(key, None)
            raise MailboxError("Microsoft 未返回 refresh_token，请确认授权包含 offline_access。")
        account_id = str(flow.get("account_id") or "")
        if account_id:
            saved = config.Config.update_account(
                account_id,
                config.OUTLOOK_IMAP_SERVER,
                config.OUTLOOK_IMAP_PORT,
                flow["account"],
                None,
                "outlook_imap",
                flow["client_id"],
                refresh_token,
            )
        else:
            saved = config.Config.add_outlook_imap_account(
                flow["account"], flow["client_id"], refresh_token,
            )
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        with self._lock:
            self._flows.pop(key, None)
            self._tokens[saved["id"]] = (
                refresh_token,
                str(payload["access_token"]),
                time.time() + max(30, expires_in - 120),
            )
        return {"status": "complete", "account": saved}


def xoauth2_bytes(username: str, access_token: str) -> bytes:
    """Raw SASL payload; ``imaplib.authenticate`` performs base64 encoding."""
    return f"user={username}\x01auth=Bearer {access_token}\x01\x01".encode("utf-8")
