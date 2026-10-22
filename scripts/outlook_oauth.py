"""Microsoft device-code authorization used by Outlook Graph accounts.

Device-code authorization attempts stay in memory until completion or expiry.

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
    from .microsoft_oauth import oauth_error_message
except ImportError:  # Running scripts/main.py directly.
    import config
    from imap_reader import MailboxError
    from microsoft_oauth import oauth_error_message


AUTHORITY = "https://login.microsoftonline.com/common/oauth2/v2.0"
DEVICE_CODE_URL = f"{AUTHORITY}/devicecode"
TOKEN_URL = f"{AUTHORITY}/token"
GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
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
    return oauth_error_message(payload, status)


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
    """Manage device-code authorization for Graph Mail.Read."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # opaque flow id -> server-only device-code state
        self._flows: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        with self._lock:
            self._flows.clear()

    def prune(self, keep_ids: set[str]) -> None:
        with self._lock:
            now = time.time()
            for flow_id in list(self._flows):
                flow = self._flows[flow_id]
                if flow["expires_at"] <= now or (flow["account_id"] and flow["account_id"] not in keep_ids):
                    self._flows.pop(flow_id, None)

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
            if existing.get("protocol") != "graph":
                raise ValueError("只能为 Outlook Graph 账号重新授权。")
        status, payload = _request_form(DEVICE_CODE_URL, {"client_id": app_id, "scope": GRAPH_SCOPE})
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
                config.GRAPH_SERVER,
                443,
                flow["account"],
                None,
                "graph",
                flow["client_id"],
                refresh_token,
            )
        else:
            saved = config.Config.add_outlook_account(
                flow["account"], flow["client_id"], refresh_token,
            )
        with self._lock:
            self._flows.pop(key, None)
        return {"status": "complete", "account": saved}

