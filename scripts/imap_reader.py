"""Read-only access to the INBOX of every configured IMAP account."""

from __future__ import annotations

import base64
import email
from html import escape
import imaplib
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from email.header import decode_header
from email.utils import parsedate_to_datetime
from typing import Any

try:
    from . import config, html_sanitizer
except ImportError:  # Running scripts/main.py directly.
    import config
    import html_sanitizer


# Seconds an unused IMAP session is kept open for reuse. Servers usually keep
# idle sessions for 30 minutes; stay well below that.
IDLE_TIMEOUT_SECONDS = 240
# Concurrent sessions per mailbox (list + new-mail check + opening a message).
SESSIONS_PER_ACCOUNT = 2
SOCKET_TIMEOUT_SECONDS = 15
BODY_CACHE_SIZE = 100
# Upper bound for cached message bodies (HTML + inline images), in bytes.
BODY_CACHE_BYTES = 48 * 1024 * 1024
# Images carried inside the message are embedded up to these sizes.
MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024
MAX_INLINE_TOTAL_BYTES = 30 * 1024 * 1024
HEADER_QUERY = "(UID BODY.PEEK[HEADER.FIELDS (FROM SUBJECT DATE)])"


class MailboxError(Exception):
    """Raised when a configured inbox cannot be read."""


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    parts = []
    for chunk, charset in decode_header(value):
        if isinstance(chunk, str):
            parts.append(chunk)
            continue
        for encoding in (charset, "utf-8", "gb18030", "big5", "latin-1"):
            if not encoding:
                continue
            try:
                parts.append(chunk.decode(encoding))
                break
            except (LookupError, UnicodeDecodeError):
                continue
        else:
            parts.append(chunk.decode("utf-8", errors="replace"))
    return "".join(parts)


def _decode_payload(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset)
    except (LookupError, UnicodeDecodeError):
        return payload.decode("utf-8", errors="replace")


def _filename(part: email.message.Message) -> str:
    return _decode_header(part.get_filename() or "") or "未命名附件"


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size < 1024 or unit == "MB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} MB"  # pragma: no cover


class _Body:
    """Everything needed to render one message as a rich document."""

    __slots__ = ("parts", "images", "attachments", "_size")

    def __init__(self) -> None:
        # (kind, content) in display order; kind is "html", "text" or "raw"
        # (pre-rendered markup such as forwarded-message headers). Rendering is
        # deferred so cid: references resolve against every image in the mail.
        self.parts: list[tuple[str, str]] = []
        self.images: list[tuple[str, str, str]] = []  # (content_id, data_uri, filename)
        self.attachments: list[str] = []    # Non-image attachments: "name (size)".
        self._size = 0

    def size(self) -> int:
        return self._size

    def render(self) -> tuple[str, list[str], str]:
        """Return ``(body_markup, body_attrs, base_href)``.

        Image parts not referenced through ``cid:`` are appended at the end so
        nothing in the message is hidden.
        """
        inline = {cid: uri for cid, uri, _name in self.images if cid}
        used: set[str] = set()
        fragments: list[str] = []
        body_attrs: list[str] = []
        base_href = ""
        for kind, content in self.parts:
            if kind == "html":
                rendered = html_sanitizer.neutralize(content, inline)
                used |= rendered.used_cids
                if len(self.parts) == 1:
                    body_attrs = rendered.body_attrs
                    fragments.append(rendered.body)
                else:
                    attrs = "".join(" " + a for a in rendered.body_attrs if not a.startswith("class="))
                    fragments.append(f'<div class="mail-part"{attrs}>{rendered.body}</div>')
                base_href = base_href or rendered.base_href
            elif kind == "text":
                text = html_sanitizer.text_to_html(content)
                fragments.append(text if len(self.parts) == 1 else f'<div class="mail-part">{text}</div>')
            else:  # Pre-rendered markup (forwarded message headers).
                fragments.append(content)
        extra = [(uri, name) for cid, uri, name in self.images if not cid or cid not in used]
        if extra or self.attachments:
            items = "".join(
                f'<figure><img src="{escape(uri, quote=True)}" alt="{escape(name, quote=True)}">'
                f'<figcaption>{escape(name, quote=False)}</figcaption></figure>'
                for uri, name in extra
            )
            files = "".join(f"<li>{escape(item, quote=False)}</li>" for item in self.attachments)
            if files:
                items += f"<p>其他附件（只读模式不提供下载）：</p><ul>{files}</ul>"
            fragments.append(f'<section class="mail-attachments">{items}</section>')
        return "".join(fragments), body_attrs, base_href


def _collect(part: email.message.Message, body: _Body, inline_total: list[int]) -> None:
    """Walk the MIME tree in display order and fill ``body``."""
    content_type = part.get_content_type()
    if content_type == "message/rfc822":
        payload = part.get_payload()
        inner = payload[0] if isinstance(payload, list) and payload else None
        if isinstance(inner, email.message.Message):
            head = "".join(
                f"<div><b>{label}：</b>{escape(_decode_header(inner.get(field)), quote=False)}</div>"
                for label, field in (("发件人", "From"), ("收件人", "To"), ("日期", "Date"), ("主题", "Subject"))
                if inner.get(field)
            )
            body.parts.append(("raw", f'<div class="mail-forward"><div class="mail-forward-head">{head}</div>'))
            _collect(inner, body, inline_total)
            body.parts.append(("raw", "</div>"))
            body._size += len(head)
        return
    if part.is_multipart():
        children = [c for c in part.get_payload() if isinstance(c, email.message.Message)]
        if content_type == "multipart/alternative":
            # Show the richest version only (HTML over plain text), like any mail client.
            def rank(child: email.message.Message) -> int:
                kind = child.get_content_type()
                if kind == "text/html" or (child.is_multipart() and any(
                        p.get_content_type() == "text/html" for p in child.walk())):
                    return 2
                return 1 if kind == "text/plain" or child.is_multipart() else 0

            if children:
                _collect(max(reversed(children), key=rank), body, inline_total)
            return
        for child in children:
            _collect(child, body, inline_total)
        return
    disposition = part.get_content_disposition()
    if content_type.startswith("image/"):
        data = part.get_payload(decode=True) or b""
        name = _filename(part) if part.get_filename() else "图片"
        if data and len(data) <= MAX_INLINE_IMAGE_BYTES and inline_total[0] + len(data) <= MAX_INLINE_TOTAL_BYTES:
            mime = "image/jpeg" if content_type == "image/jpg" else content_type
            cid = (part.get("Content-ID") or "").strip().strip("<>").strip().lower()
            uri = f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
            body.images.append((cid, uri, name))
            inline_total[0] += len(data)
            body._size += len(uri)
        elif data:
            body.attachments.append(f"{name}（{_human_size(len(data))}，图片过大未显示）")
        return
    if disposition != "attachment" and content_type in {"text/html", "text/plain"}:
        text = _decode_payload(part)
        body.parts.append(("html" if content_type == "text/html" else "text", text))
        body._size += len(text)
        return
    if disposition == "attachment" or part.get_filename():
        data = part.get_payload(decode=True) or b""
        body.attachments.append(f"{_filename(part)}（{_human_size(len(data))}）")
        return
    if content_type.startswith("text/"):
        # Other inline text (text/enriched, text/calendar...): show it as text.
        text = _decode_payload(part)
        body.parts.append(("text", text))
        body._size += len(text)


def _extract_body(message: email.message.Message) -> _Body:
    body = _Body()
    _collect(message, body, [0])
    return body


def _fetch_payloads(data: list[Any] | None) -> list[tuple[bytes, bytes]]:
    return [item for item in (data or []) if isinstance(item, tuple) and len(item) > 1]


def _timestamp(value: str) -> float:
    """Epoch seconds for a Date header, used to merge mailboxes chronologically."""
    if not value:
        return 0.0
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return 0.0
    if parsed is None:
        return 0.0
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return 0.0


def _parse_fetch(data: list[Any] | None) -> list[tuple[str, bytes]]:
    """Return ``(uid, literal)`` pairs from an imaplib FETCH response.

    Servers may put ``UID n`` before or after the literal, so the trailing
    bytes item that follows a literal is inspected too.
    """
    items = list(data or [])
    results: list[tuple[str, bytes]] = []
    for index, item in enumerate(items):
        if not (isinstance(item, tuple) and len(item) > 1):
            continue
        match = re.search(rb"\bUID (\d+)", item[0])
        if not match and index + 1 < len(items) and isinstance(items[index + 1], bytes):
            match = re.search(rb"\bUID (\d+)", items[index + 1])
        if match:
            results.append((match.group(1).decode("ascii"), item[1]))
    return results


def _last_number(data: list[Any] | None) -> int | None:
    """Last numeric item of an untagged response (EXISTS may be repeated)."""
    value = None
    for item in data or []:
        if isinstance(item, bytes) and item.strip().isdigit():
            value = int(item.strip())
    return value


class _Session:
    """One logged-in IMAP connection with INBOX opened read-only."""

    def __init__(self, signature: tuple, connection: Any) -> None:
        self.signature = signature
        self.connection = connection
        self.last_used = time.monotonic()
        self.reused = False

    def examine(self) -> tuple[int, str, int | None]:
        """(Re)open INBOX read-only and return ``(exists, uidvalidity, uidnext)``.

        One round trip; it also refreshes the message count on reused sessions.
        """
        status, data = self.connection.select("INBOX", readonly=True)
        if status != "OK":
            raise MailboxError("无法只读打开 INBOX。")
        exists = _last_number(data) or 0
        _name, validity = self.connection.response("UIDVALIDITY")
        _name, uidnext = self.connection.response("UIDNEXT")
        number = _last_number(validity)
        return exists, "" if number is None else str(number), _last_number(uidnext)

    def close(self, graceful: bool = True) -> None:
        connection = self.connection
        try:
            if graceful:
                sock = getattr(connection, "sock", None)
                if sock is not None:
                    sock.settimeout(3)
                connection.logout()
            else:
                connection.shutdown()
        except Exception:
            pass


class _SessionPool:
    """Keeps a few idle logged-in sessions per account so TLS + LOGIN is not
    repeated on every request. Sessions are checked out exclusively (imaplib is
    not thread-safe) and dropped when the account settings change."""

    def __init__(self) -> None:
        self._idle: dict[str, list[_Session]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _settings(account_id: str) -> tuple:
        try:
            return config.Config.connection_values(account_id)
        except config.AccountNotFound as exc:
            raise MailboxError("邮箱不存在或已被删除。") from exc

    def acquire(self, account_id: str, fresh: bool = False) -> _Session:
        signature = self._settings(account_id)
        stale: list[_Session] = []
        session = None
        now = time.monotonic()
        with self._lock:
            idle = self._idle.get(account_id, [])
            while idle and not fresh:
                candidate = idle.pop()
                if candidate.signature == signature and now - candidate.last_used < IDLE_TIMEOUT_SECONDS:
                    session = candidate
                    break
                stale.append(candidate)
        for old in stale:
            old.close()
        if session:
            session.reused = True
            return session
        return _Session(signature, self._connect(signature))

    @staticmethod
    def _connect(signature: tuple) -> Any:
        server, port, account, password = signature
        if not server or not account or not password:
            raise MailboxError("请先在浏览器中完成邮箱连接设置。")
        try:
            connection = imaplib.IMAP4_SSL(server, port, timeout=SOCKET_TIMEOUT_SECONDS)
        except Exception as exc:
            raise MailboxError(f"无法连接 {server}:{port}，请检查服务器地址和网络。") from exc
        try:
            connection.login(account, password)
        except Exception as exc:
            try:
                connection.shutdown()
            except Exception:
                pass
            raise MailboxError("IMAP 登录失败，请检查服务器、账号和授权码。") from exc
        return connection

    def release(self, account_id: str, session: _Session) -> None:
        session.last_used = time.monotonic()
        with self._lock:
            idle = self._idle.setdefault(account_id, [])
            if len(idle) < SESSIONS_PER_ACCOUNT:
                idle.append(session)
                return
        session.close()

    def prune(self, keep_ids: set[str] | None = None) -> None:
        """Close expired sessions and sessions of removed accounts."""
        now = time.monotonic()
        expired: list[_Session] = []
        with self._lock:
            for account_id in list(self._idle):
                sessions = self._idle[account_id]
                if keep_ids is not None and account_id not in keep_ids:
                    expired.extend(sessions)
                    del self._idle[account_id]
                    continue
                alive = [s for s in sessions if now - s.last_used < IDLE_TIMEOUT_SECONDS]
                expired.extend(s for s in sessions if s not in alive)
                self._idle[account_id] = alive
        for session in expired:
            session.close()

    def close_all(self) -> None:
        with self._lock:
            sessions = [s for group in self._idle.values() for s in group]
            self._idle.clear()
        for session in sessions:
            session.close()


# Errors that mean the TCP/TLS session is unusable (server timeout, network drop).
_CONNECTION_ERRORS = (imaplib.IMAP4.abort, OSError, EOFError)


class InboxReader:
    """Read-only INBOX access with pooled sessions.

    Cost per request on a warm session: listing = 2 round trips (EXAMINE +
    FETCH), new-mail check = 1 round trip when nothing arrived, opening a
    message = 2 round trips (none when cached).
    """

    max_parallel = 16

    def __init__(self) -> None:
        self.pool = _SessionPool()
        # (account_id, uidvalidity, uid) -> (public message, _Body)
        self._bodies: OrderedDict[tuple, tuple[dict[str, Any], _Body]] = OrderedDict()
        self._body_bytes = 0
        self._validity: dict[str, str] = {}
        self._cache_lock = threading.Lock()

    def close(self) -> None:
        self.pool.close_all()

    def prune(self) -> None:
        self.pool.prune(set(config.Config.account_ids()))
        with self._cache_lock:
            known = set(config.Config.account_ids())
            for key in [k for k in self._bodies if k[0] not in known]:
                self._evict(key)

    def _run(self, account_id: str, action):
        """Run ``action(session)`` on a pooled session, retrying once on a fresh
        connection when a reused one turns out to be dead."""
        for attempt in (0, 1):
            session = self.pool.acquire(account_id, fresh=attempt == 1)
            try:
                result = action(session)
            except MailboxError:
                session.close(graceful=False)
                raise
            except _CONNECTION_ERRORS as exc:
                session.close(graceful=False)
                if session.reused and attempt == 0:
                    continue
                raise MailboxError("与邮箱服务器的连接中断，请稍后重试。") from exc
            except imaplib.IMAP4.error as exc:
                session.close(graceful=False)
                raise MailboxError("邮箱服务器拒绝了读取请求。") from exc
            except BaseException:
                session.close(graceful=False)
                raise
            self.pool.release(account_id, session)
            return result
        raise MailboxError("与邮箱服务器的连接中断，请稍后重试。")  # pragma: no cover

    def _examine(self, account_id: str, session: _Session) -> tuple[int, str, int | None]:
        exists, validity, uidnext = session.examine()
        with self._cache_lock:
            previous = self._validity.get(account_id)
            self._validity[account_id] = validity
            if previous is not None and previous != validity:
                for key in [k for k in self._bodies if k[0] == account_id]:
                    self._evict(key)
        return exists, validity, uidnext

    @staticmethod
    def _summary(account_id: str, uid: str, raw_header: bytes) -> dict[str, Any]:
        message = email.message_from_bytes(raw_header)
        date = _decode_header(message.get("Date"))
        return {
            "account_id": account_id,
            "uid": uid,
            "from": _decode_header(message.get("From")),
            "subject": _decode_header(message.get("Subject")),
            "date": date,
            "timestamp": _timestamp(date),
        }

    def _summaries(self, account_id: str, data: list[Any] | None) -> list[dict[str, Any]]:
        summaries = [self._summary(account_id, uid, header) for uid, header in _parse_fetch(data)]
        summaries.sort(key=lambda item: int(item["uid"]), reverse=True)
        return summaries

    # ------------------------------------------------------------------ public API
    def list_messages(self, account_id: str, limit: int = 50) -> list[dict[str, Any]]:
        """Most recent ``limit`` INBOX messages of one account, newest first."""
        limit = max(1, min(int(limit), 100))

        def action(session: _Session):
            exists, _validity, _uidnext = self._examine(account_id, session)
            if not exists:
                return []
            # Sequence numbers are dense, so the newest N messages are simply
            # the last N: no need to SEARCH ALL and download every UID.
            start = max(1, exists - limit + 1)
            status, data = session.connection.fetch(f"{start}:{exists}", HEADER_QUERY)
            if status != "OK":
                raise MailboxError("无法读取 INBOX 邮件标题。")
            return self._summaries(account_id, data)

        return self._run(account_id, action)

    def list_all(
        self, account_ids: list[str], limit: int = 50
    ) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
        """Read several inboxes in parallel and merge them by date.

        One failing mailbox does not break the others: its error is reported in
        the second element of the result as ``{"account_id", "error"}``.
        """
        limit = max(1, min(int(limit), 100))
        if not account_ids:
            return [], []

        def read(account_id: str):
            try:
                return account_id, self.list_messages(account_id, limit), None
            except MailboxError as exc:
                return account_id, [], str(exc)
            except Exception:
                return account_id, [], "读取收件箱失败，请检查 IMAP 配置与邮箱服务状态。"

        if len(account_ids) == 1:
            results = [read(account_ids[0])]
        else:
            workers = min(self.max_parallel, len(account_ids))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imap-list") as pool:
                results = [future.result() for future in as_completed(pool.submit(read, a) for a in account_ids)]
        order = {account_id: index for index, account_id in enumerate(account_ids)}
        merged: list[dict[str, Any]] = []
        errors: list[dict[str, str]] = []
        for account_id, messages, error in sorted(results, key=lambda r: order[r[0]]):
            merged.extend(messages)
            if error:
                errors.append({"account_id": account_id, "error": error})
        merged.sort(key=lambda item: (item["timestamp"], int(item["uid"])), reverse=True)
        return merged[:limit], errors

    def check_new(
        self, account_id: str, known_validity: str | None, last_uid: int | None, limit: int = 20
    ) -> tuple[str, int, list[dict[str, Any]]]:
        """Return ``(uid_validity, highest_uid, new_messages)`` for the watcher.

        When ``last_uid`` is None (first check) or the mailbox UIDVALIDITY
        changed, only the baseline is recorded and no messages are reported.
        A check that finds nothing new costs a single EXAMINE when the server
        reports UIDNEXT.
        """

        def action(session: _Session):
            connection = session.connection
            exists, validity, uidnext = self._examine(account_id, session)
            if last_uid is None or (known_validity and validity and validity != known_validity):
                if uidnext is not None:
                    return validity, max(0, uidnext - 1), []
                if not exists:
                    return validity, 0, []
                status, data = connection.fetch("*", "(UID)")
                found = [int(m) for item in data or [] if isinstance(item, bytes)
                         for m in re.findall(rb"\bUID (\d+)", item)] if status == "OK" else []
                return validity, max(found or [0]), []
            if uidnext is not None and uidnext <= last_uid + 1:
                return validity, last_uid, []
            if not exists:
                return validity, last_uid, []
            status, data = connection.uid("search", None, f"UID {last_uid + 1}:*")
            if status != "OK":
                raise MailboxError("无法读取 INBOX 邮件列表。")
            values = (data[0] or b"").decode("ascii", errors="ignore").split() if data else []
            # "n:*" always returns the highest UID even when it is below n, so filter.
            uids = sorted(int(v) for v in values if v.isdigit() and int(v) > last_uid)
            if not uids:
                return validity, last_uid, []
            wanted = [str(uid) for uid in uids[-limit:]]
            status, data = connection.uid("fetch", ",".join(wanted), HEADER_QUERY)
            if status != "OK":
                raise MailboxError("无法读取 INBOX 邮件标题。")
            return validity, uids[-1], self._summaries(account_id, data)

        return self._run(account_id, action)

    # ------------------------------------------------------------------ message bodies
    def _evict(self, key: tuple) -> None:
        """Remove one cache entry; caller holds ``_cache_lock``."""
        entry = self._bodies.pop(key, None)
        if entry:
            self._body_bytes -= entry[1].size()

    def _cached(self, account_id: str, uid: str) -> tuple[dict[str, Any], _Body] | None:
        with self._cache_lock:
            validity = self._validity.get(account_id)
            key = (account_id, validity, uid)
            entry = self._bodies.get(key) if validity is not None else None
            if entry:
                self._bodies.move_to_end(key)
            return entry

    def _store(self, account_id: str, uid: str, entry: tuple[dict[str, Any], _Body]) -> None:
        size = entry[1].size()
        if size > BODY_CACHE_BYTES:
            return
        with self._cache_lock:
            key = (account_id, self._validity.get(account_id), uid)
            self._evict(key)
            self._bodies[key] = entry
            self._body_bytes += size
            while self._bodies and (len(self._bodies) > BODY_CACHE_SIZE or self._body_bytes > BODY_CACHE_BYTES):
                self._evict(next(iter(self._bodies)))

    def _load(self, account_id: str, uid: str) -> tuple[dict[str, Any], _Body] | None:
        if not re.fullmatch(r"[0-9]{1,20}", uid):
            return None
        cached = self._cached(account_id, uid)
        if cached:
            return cached

        def action(session: _Session):
            # Re-EXAMINE so a reused session sees messages that arrived since it was idle.
            self._examine(account_id, session)
            status, data = session.connection.uid("fetch", uid, "(UID BODY.PEEK[])")
            if status != "OK":
                raise MailboxError("无法读取这封邮件。")
            for _uid, raw_message in _parse_fetch(data):
                return email.message_from_bytes(raw_message)
            return None

        message = self._run(account_id, action)
        if message is None:
            return None
        body = _extract_body(message)
        public = {
            "account_id": account_id,
            "uid": uid,
            "from": _decode_header(message.get("From")),
            "to": _decode_header(message.get("To")),
            "cc": _decode_header(message.get("Cc")),
            "subject": _decode_header(message.get("Subject")),
            "date": _decode_header(message.get("Date")),
        }
        entry = (public, body)
        self._store(account_id, uid, entry)
        return entry

    def get_message(self, account_id: str, uid: str) -> dict[str, Any] | None:
        """Message headers; the body is served by ``get_message_html``."""
        entry = self._load(account_id, uid)
        return dict(entry[0]) if entry else None

    def get_message_html(self, account_id: str, uid: str) -> str | None:
        """The complete message as a standalone rich HTML document."""
        entry = self._load(account_id, uid)
        if not entry:
            return None
        markup, body_attrs, base_href = entry[1].render()
        if not markup.strip():
            markup = '<p style="color:#758078">这封邮件没有正文。</p>'
        return html_sanitizer.document(markup, body_attrs, base_href)
