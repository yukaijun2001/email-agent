"""Read-only Outlook INBOX access through Microsoft Graph.

Accounts use the four-part import line
``email----password----client_id----refresh_token``. Graph itself exchanges
``client_id`` and ``refresh_token`` for an access token; the mailbox password
is stored with the account and is not sent to Microsoft.
"""

from __future__ import annotations

import base64
import json
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

try:
    from . import config, html_sanitizer
    from .imap_reader import MailboxError, _Body
except ImportError:  # Running scripts/main.py directly.
    import config
    import html_sanitizer
    from imap_reader import MailboxError, _Body


TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
GRAPH_ROOT = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = "offline_access https://graph.microsoft.com/Mail.Read"
REQUEST_TIMEOUT_SECONDS = 20
MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_INLINE_TOTAL_BYTES = 30 * 1024 * 1024
_UID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,800}")


def encode_message_uid(message_id: str) -> str:
    """URL-safe id that fits in the existing message path."""
    return base64.urlsafe_b64encode(message_id.encode("utf-8")).decode("ascii").rstrip("=")


def decode_message_uid(uid: str) -> str | None:
    if not _UID_PATTERN.fullmatch(uid or ""):
        return None
    padded = uid + "=" * (-len(uid) % 4)
    try:
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if not text or any(ord(char) < 32 for char in text):
        return None
    return text


def _iso_timestamp(value: str) -> float:
    if not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, IndexError):
            return 0.0
    if parsed is None:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _address(value: Any) -> str:
    if not isinstance(value, dict):
        return ""
    details = value.get("emailAddress") if isinstance(value.get("emailAddress"), dict) else value
    if not isinstance(details, dict):
        return ""
    name = str(details.get("name") or "").strip()
    address = str(details.get("address") or "").strip()
    if name and address:
        return f"{name} <{address}>"
    return name or address


def _addresses(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return ", ".join(item for item in (_address(entry) for entry in value) if item)


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB"):
        if value < 1024 or unit == "MB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} MB"


def explain_graph_error(payload: str, status: int = 0) -> str:
    """Short Chinese message for a Microsoft token or Graph error body."""
    text = payload.replace("\n", " ").strip()
    code = ""
    description = text
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        error = parsed.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or "")
            description = str(error.get("message") or description)
        else:
            code = str(error or "")
            description = str(parsed.get("error_description") or description)
    blob = f"{code} {description}"
    if "AADSTS90023" in blob:
        return "当前应用未提供可用的邮箱授权权限，请检查 Mail.Read 授权。"
    if "invalid_grant" in blob:
        return "刷新令牌无效或已过期，请重新获取 refresh_token。"
    if "invalid_client" in blob:
        return "client_id 无效，请检查导入的 client_id。"
    if "interaction_required" in blob or "consent_required" in blob:
        return "账号需要重新授权或补充权限。"
    if "ErrorInvalidUser" in blob or "MailboxNotEnabledForRESTAPI" in blob:
        return "Graph 无法访问该邮箱，请检查账号类型或 Mail.Read 权限。"
    if status == 401:
        return "Graph 登录失败，请检查 client_id 和 refresh_token。"
    if status == 403:
        return "Graph 拒绝读取邮件，请确认应用已获得 Mail.Read 权限。"
    if status == 429:
        return "Graph 请求过于频繁，请稍后重试。"
    if description and description != text:
        return description[:180]
    if text:
        return text[:180]
    return "Graph 读取失败，请稍后重试。"


class GraphClient:
    """Microsoft Graph INBOX reader with a cached access token per account."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._account_locks: dict[str, threading.Lock] = {}
        self._tokens: dict[str, tuple[str, str, float]] = {}
        # account_id -> {"signature": tuple, "link": str, "seen": list[str]}
        self._delta: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        with self._lock:
            self._tokens.clear()
            self._delta.clear()

    def prune(self, keep_ids: set[str]) -> None:
        with self._lock:
            for account_id in [key for key in self._tokens if key not in keep_ids]:
                self._tokens.pop(account_id, None)
                self._account_locks.pop(account_id, None)
            for account_id in [key for key in self._delta if key not in keep_ids]:
                self._delta.pop(account_id, None)

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
        if account.get("protocol") != "graph":
            raise MailboxError("这个邮箱不是 Outlook Graph 账号。")
        if not account.get("account") or not account.get("client_id") or not account.get("refresh_token"):
            raise MailboxError("请填写 Outlook 邮箱、client_id 和 refresh_token。")
        return account

    def access_token(self, account_id: str) -> str:
        """Return a Graph access token, persisting a rotated refresh token."""
        with self._account_lock(account_id):
            account = self._credentials(account_id)
            refresh_token = account["refresh_token"]
            cached = self._tokens.get(account_id)
            now = datetime.now(timezone.utc).timestamp()
            if cached and cached[0] == refresh_token and cached[2] > now:
                return cached[1]
            token, new_refresh, expires_in = self._refresh(account["client_id"], refresh_token)
            if new_refresh and new_refresh != refresh_token:
                config.Config.replace_refresh_token(account_id, refresh_token, new_refresh)
                refresh_token = new_refresh
            self._tokens[account_id] = (refresh_token, token, now + max(30, expires_in - 120))
            return token

    @staticmethod
    def _refresh(client_id: str, refresh_token: str) -> tuple[str, str, int]:
        scopes: tuple[str | None, ...] = (GRAPH_SCOPE, None)
        last_error = "刷新令牌失败。"
        for scope in scopes:
            form = {
                "client_id": client_id,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            }
            if scope:
                form["scope"] = scope
            status, payload = _request(
                "POST",
                TOKEN_URL,
                {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                urllib.parse.urlencode(form).encode("utf-8"),
            )
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = {}
            if status == 200 and isinstance(parsed, dict) and parsed.get("access_token"):
                try:
                    expires_in = int(parsed.get("expires_in") or 3600)
                except (TypeError, ValueError):
                    expires_in = 3600
                return str(parsed["access_token"]), str(parsed.get("refresh_token") or ""), expires_in
            raw = payload.decode("utf-8", errors="replace")
            last_error = explain_graph_error(raw, status)
            if scope and "invalid_scope" not in raw.lower() and "AADSTS70011" not in raw:
                break
        raise MailboxError(last_error)

    def list_messages(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 100))
        query = urllib.parse.urlencode({
            "$top": str(limit),
            "$orderby": "receivedDateTime desc",
            "$select": "id,subject,from,receivedDateTime,bodyPreview",
        })
        messages = self._graph_json(
            account_id,
            f"{GRAPH_ROOT}/me/mailFolders/inbox/messages?{query}",
        )
        summaries = []
        for item in messages.get("value") or []:
            summary = self._summary(account_id, item)
            if summary:
                summaries.append(summary)
        summaries.sort(key=lambda item: (item["timestamp"], item["uid"]), reverse=True)
        return summaries[:limit]

    def load_message(self, account_id: str, uid: str) -> tuple[dict[str, Any], _Body] | None:
        message_id = decode_message_uid(uid)
        if not message_id:
            return None
        quoted = urllib.parse.quote(message_id, safe="")
        query = urllib.parse.urlencode({
            "$select": "id,subject,from,toRecipients,ccRecipients,receivedDateTime,body,hasAttachments",
        })
        try:
            message = self._graph_json(
                account_id,
                f"{GRAPH_ROOT}/me/messages/{quoted}?{query}",
                headers={"Prefer": 'outlook.body-content-type="html"'},
                allow_not_found=True,
            )
        except MailboxError:
            raise
        if message is None:
            return None
        public = {
            "account_id": account_id,
            "uid": uid,
            "from": _address(message.get("from")),
            "to": _addresses(message.get("toRecipients")),
            "cc": _addresses(message.get("ccRecipients")),
            "subject": str(message.get("subject") or ""),
            "date": str(message.get("receivedDateTime") or ""),
        }
        body = _Body()
        content = message.get("body") if isinstance(message.get("body"), dict) else {}
        text = str(content.get("content") or "")
        kind = "html" if str(content.get("contentType") or "").lower() == "html" else "text"
        if text:
            body.parts.append((kind, text))
            body._size += len(text)
        if message.get("hasAttachments"):
            self._collect_attachments(account_id, quoted, body)
        return public, body

    def check_new(
        self, account_id: str, known_validity: str | None, last_uid: int | None
    ) -> tuple[str, int, list[dict[str, Any]]]:
        """Baseline on the first poll, then return messages from the delta link.

        ``last_uid`` stays a small integer so the existing watcher can keep its
        state. The Graph cursor itself lives in this client.
        """
        account = self._credentials(account_id)
        signature = (account["account"].lower(), account["client_id"])
        with self._lock:
            state = self._delta.get(account_id)
        if (
            last_uid is None
            or known_validity != "graph"
            or state is None
            or state.get("signature") != signature
            or not state.get("link")
        ):
            link = self._latest_delta(account_id)
            with self._lock:
                self._delta[account_id] = {"signature": signature, "link": link, "seen": []}
            return "graph", 1, []
        try:
            pages, link = self._follow_delta(account_id, state["link"])
        except MailboxError as exc:
            if "410" not in str(exc):
                raise
            link = self._latest_delta(account_id)
            with self._lock:
                self._delta[account_id] = {"signature": signature, "link": link, "seen": []}
            return "graph", 1, []
        seen = list(state.get("seen") or [])
        known = set(seen)
        fresh: list[dict[str, Any]] = []
        for item in pages:
            if not isinstance(item, dict) or item.get("@removed"):
                continue
            summary = self._summary(account_id, item)
            if not summary or summary["uid"] in known:
                continue
            known.add(summary["uid"])
            seen.append(summary["uid"])
            fresh.append(summary)
        with self._lock:
            current = self._delta.get(account_id)
            if current is state:
                state["link"] = link or state["link"]
                state["seen"] = seen[-2000:]
        fresh.sort(key=lambda item: item["timestamp"])
        return "graph", 1, fresh

    def _latest_delta(self, account_id: str) -> str:
        query = urllib.parse.urlencode({
            "$deltatoken": "latest",
            "$select": "id,subject,from,receivedDateTime",
        })
        _pages, link = self._follow_delta(
            account_id,
            f"{GRAPH_ROOT}/me/mailFolders/inbox/messages/delta?{query}",
        )
        if not link:
            raise MailboxError("Graph 没有返回增量同步链接。")
        return link

    def _follow_delta(self, account_id: str, url: str) -> tuple[list[Any], str]:
        pages: list[Any] = []
        link = ""
        for _ in range(5):
            payload = self._graph_json(account_id, url)
            pages.extend(payload.get("value") or [])
            link = str(payload.get("@odata.deltaLink") or link)
            url = str(payload.get("@odata.nextLink") or "")
            if not url:
                break
        return pages, link

    def _collect_attachments(self, account_id: str, quoted_id: str, body: _Body) -> None:
        query = urllib.parse.urlencode({
            "$select": "name,contentType,size,isInline,contentId,contentBytes",
        })
        payload = self._graph_json(
            account_id,
            f"{GRAPH_ROOT}/me/messages/{quoted_id}/attachments?{query}",
        )
        inline_total = 0
        for item in payload.get("value") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "附件")
            try:
                size = int(item.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            content_type = str(item.get("contentType") or "")
            raw = item.get("contentBytes")
            data = b""
            if isinstance(raw, str) and raw:
                try:
                    data = base64.b64decode(raw, validate=False)
                except ValueError:
                    data = b""
            if not size and data:
                size = len(data)
            if content_type.startswith("image/") and data and size <= MAX_INLINE_IMAGE_BYTES \
                    and inline_total + len(data) <= MAX_INLINE_TOTAL_BYTES:
                mime = "image/jpeg" if content_type == "image/jpg" else content_type
                cid = str(item.get("contentId") or "").strip().strip("<>").lower()
                uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
                body.images.append((cid, uri, name))
                inline_total += len(data)
                body._size += len(uri)
                continue
            body.attachments.append(f"{name}（{_human_size(size)}）")

    def _graph_json(
        self,
        account_id: str,
        url: str,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> Any:
        token = self.access_token(account_id)
        request_headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        if headers:
            request_headers.update(headers)
        status, payload = _request("GET", url, request_headers, None)
        if status == 404 and allow_not_found:
            return None
        if status != 200:
            detail = payload.decode("utf-8", errors="replace")
            message = explain_graph_error(detail, status)
            if status == 410:
                raise MailboxError(f"410 {message}")
            raise MailboxError(message)
        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MailboxError("Graph 返回了无法解析的内容。") from exc
        if not isinstance(parsed, dict):
            raise MailboxError("Graph 返回了无法解析的内容。")
        return parsed

    @staticmethod
    def _summary(account_id: str, item: dict) -> dict[str, Any] | None:
        message_id = str(item.get("id") or "")
        if not message_id:
            return None
        date = str(item.get("receivedDateTime") or "")
        return {
            "account_id": account_id,
            "uid": encode_message_uid(message_id),
            "from": _address(item.get("from")),
            "subject": str(item.get("subject") or ""),
            "date": date,
            "timestamp": _iso_timestamp(date),
        }


def _request(method: str, url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except urllib.error.URLError as exc:
        raise MailboxError("无法连接 Microsoft Graph，请检查服务器网络。") from exc
    except TimeoutError as exc:
        raise MailboxError("连接 Microsoft Graph 超时，请稍后重试。") from exc
