"""Real-time push of new-mail events to browsers over WebSocket.

Standard library only:

* ``handshake_response`` / ``read_frame`` / ``encode_frame`` implement the
  subset of RFC 6455 a browser client needs (text, ping/pong, close).
* ``Hub`` keeps the set of connected browsers and broadcasts JSON events.
* ``MailWatcher`` is a background thread that polls every configured INBOX
  (read-only, ``BODY.PEEK``) while at least one browser is connected and
  pushes ``new_emails`` events when UIDs above the last seen one appear.

Event payloads sent to the browser::

    {"type": "hello", "poll_interval": 30}
    {"type": "new_emails", "account_id": "1a2b3c4d", "emails": [summary, ...]}
    {"type": "account_status", "account_id": "1a2b3c4d", "ok": false, "error": "..."}
    {"type": "accounts_changed"}
"""

from __future__ import annotations

import base64
import hashlib
import json
import struct
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, BinaryIO

try:
    from . import config
    from .imap_reader import InboxReader, MailboxError
except ImportError:  # Running scripts/main.py directly.
    import config
    from imap_reader import InboxReader, MailboxError


WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
MAX_CLIENT_FRAME = 64 * 1024
MAX_CLIENTS = 32


class ProtocolError(Exception):
    """The peer violated RFC 6455 or sent something we refuse to handle."""


# --------------------------------------------------------------------------- frames
def accept_key(client_key: str) -> str:
    digest = hashlib.sha1((client_key + WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def valid_client_key(client_key: str | None) -> bool:
    if not client_key:
        return False
    try:
        return len(base64.b64decode(client_key.strip(), validate=True)) == 16
    except (ValueError, TypeError):
        return False


def encode_frame(opcode: int, payload: bytes = b"") -> bytes:
    """Server-to-client frame: FIN set, never masked."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(length)
    elif length < 1 << 16:
        header.append(126)
        header += struct.pack("!H", length)
    else:
        header.append(127)
        header += struct.pack("!Q", length)
    return bytes(header) + payload


def _read_exact(stream: BinaryIO, size: int) -> bytes:
    data = b""
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise ConnectionError("WebSocket peer closed the connection.")
        data += chunk
    return data


def read_frame(stream: BinaryIO) -> tuple[bool, int, bytes]:
    """Read one client frame and return ``(fin, opcode, unmasked_payload)``."""
    first, second = _read_exact(stream, 2)
    fin = bool(first & 0x80)
    if first & 0x70:
        raise ProtocolError("Reserved bits are not supported.")
    opcode = first & 0x0F
    if not second & 0x80:
        raise ProtocolError("Client frames must be masked.")
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", _read_exact(stream, 2))
    elif length == 127:
        (length,) = struct.unpack("!Q", _read_exact(stream, 8))
    if opcode >= 0x8 and (length > 125 or not fin):
        raise ProtocolError("Invalid control frame.")
    if length > MAX_CLIENT_FRAME:
        raise ProtocolError("Frame too large.")
    mask = _read_exact(stream, 4)
    payload = bytearray(_read_exact(stream, length))
    for index in range(length):
        payload[index] ^= mask[index % 4]
    return fin, opcode, bytes(payload)


# --------------------------------------------------------------------------- clients
class Client:
    """One connected browser; writes are serialised by a lock."""

    def __init__(self, stream: BinaryIO, address: str = "") -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.address = address
        self.closed = False

    def send_frame(self, opcode: int, payload: bytes = b"") -> bool:
        with self._lock:
            if self.closed:
                return False
            try:
                self._stream.write(encode_frame(opcode, payload))
                self._stream.flush()
                return True
            except (OSError, ValueError):
                self.closed = True
                return False

    def send_json(self, event: dict[str, Any]) -> bool:
        return self.send_frame(OP_TEXT, json.dumps(event, ensure_ascii=False).encode("utf-8"))

    def close(self, code: int = 1000) -> None:
        self.send_frame(OP_CLOSE, struct.pack("!H", code))
        with self._lock:
            self.closed = True


class Hub:
    """Thread-safe registry of connected browsers."""

    def __init__(self) -> None:
        self._clients: set[Client] = set()
        self._lock = threading.Lock()
        self.on_first_client = None  # Callable invoked when the hub goes 0 -> 1 client.

    def register(self, client: Client) -> bool:
        with self._lock:
            if len(self._clients) >= MAX_CLIENTS:
                return False
            was_empty = not self._clients
            self._clients.add(client)
        if was_empty and self.on_first_client:
            self.on_first_client()
        return True

    def unregister(self, client: Client) -> None:
        with self._lock:
            self._clients.discard(client)

    def count(self) -> int:
        with self._lock:
            return len(self._clients)

    def _snapshot(self) -> list[Client]:
        with self._lock:
            return list(self._clients)

    def broadcast(self, event: dict[str, Any]) -> int:
        delivered = 0
        for client in self._snapshot():
            if client.send_json(event):
                delivered += 1
            else:
                self.unregister(client)
        return delivered

    def ping_all(self) -> None:
        for client in self._snapshot():
            if not client.send_frame(OP_PING, b"keepalive"):
                self.unregister(client)


def serve_client(hub: Hub, client: Client, stream: BinaryIO) -> None:
    """Run the read loop of one connection until it closes (blocks the caller)."""
    if not hub.register(client):
        client.close(1013)  # Try again later.
        return
    try:
        client.send_json({"type": "hello", "poll_interval": config.POLL_INTERVAL_SECONDS})
        while not client.closed:
            _fin, opcode, payload = read_frame(stream)
            if opcode == OP_CLOSE:
                client.close(1000)
                break
            if opcode == OP_PING:
                client.send_frame(OP_PONG, payload)
            elif opcode == OP_TEXT and payload.strip() == b'{"type":"ping"}':
                client.send_json({"type": "pong"})
            # Pongs, other text frames and continuation frames are ignored:
            # the browser does not need to send anything to receive events.
    except ProtocolError:
        client.close(1002)
    except (ConnectionError, OSError, ValueError):
        pass
    finally:
        with client._lock:
            client.closed = True
        hub.unregister(client)


# --------------------------------------------------------------------------- watcher
class MailWatcher(threading.Thread):
    """Poll all INBOXes while browsers are connected and push new mail."""

    def __init__(self, hub: Hub, reader: InboxReader | None = None, interval: float | None = None) -> None:
        super().__init__(name="mail-watcher", daemon=True)
        self.hub = hub
        self.reader = reader or InboxReader()
        self.interval = float(interval or config.POLL_INTERVAL_SECONDS)
        self._wake = threading.Event()
        self._stop_event = threading.Event()
        # account_id -> {"validity": str, "last_uid": int | None, "error": str | None, "signature": tuple}
        self._state: dict[str, dict[str, Any]] = {}
        self._state_lock = threading.Lock()

    def wake(self) -> None:
        """Run a check now (new browser connected or accounts changed)."""
        self._wake.set()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake.set()

    def _signature(self, account_id: str) -> tuple:
        try:
            account = config.Config.mailbox(account_id)
        except config.AccountNotFound:
            return ()
        if account.get("protocol") == "graph":
            return ("graph", account["account"].lower(), account.get("client_id") or "")
        return (account["imap_server"].lower(), account["imap_port"], account["account"].lower())

    def check_account(self, account_id: str) -> None:
        signature = self._signature(account_id)
        with self._state_lock:
            state = self._state.get(account_id)
            if state is None or state["signature"] != signature:
                # New account or connection target changed: start a fresh baseline.
                state = {"validity": "", "last_uid": None, "error": None, "signature": signature}
                self._state[account_id] = state
            validity, last_uid = state["validity"], state["last_uid"]
        try:
            next_validity, next_uid, emails = self.reader.check_new(account_id, validity, last_uid)
            error = None
        except MailboxError as exc:
            error = str(exc)
        except Exception as exc:  # Network errors, unexpected server replies.
            sys.stderr.write("mail-watcher: %s check failed: %r\n" % (account_id, exc))
            error = "检查新邮件失败，将在下一轮重试。"
        with self._state_lock:
            if self._state.get(account_id) is not state:
                return  # Account removed or reconfigured meanwhile.
            previous_error = state["error"]
            state["error"] = error
            if error is None:
                state["validity"], state["last_uid"] = next_validity, next_uid
        if error != previous_error:
            self.hub.broadcast(
                {"type": "account_status", "account_id": account_id, "ok": error is None, "error": error or ""}
            )
        if error is None and emails:
            self.hub.broadcast({"type": "new_emails", "account_id": account_id, "emails": emails})

    def run_once(self) -> None:
        account_ids = config.Config.account_ids()
        with self._state_lock:
            for stale in set(self._state) - set(account_ids):
                del self._state[stale]
        if not account_ids:
            return
        workers = max(1, min(InboxReader.max_parallel, len(account_ids)))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="imap-watch") as pool:
            list(pool.map(self.check_account, account_ids))

    def run(self) -> None:
        while not self._stop_event.is_set():
            if self.hub.count():
                try:
                    self.run_once()
                except Exception as exc:
                    sys.stderr.write("mail-watcher: cycle failed: %r\n" % (exc,))
                self.hub.ping_all()
            prune = getattr(self.reader, "prune", None)
            if prune:
                try:
                    prune()  # Close pooled sessions of removed accounts or idle too long.
                except Exception as exc:
                    sys.stderr.write("mail-watcher: prune failed: %r\n" % (exc,))
            # Sleep until the next cycle; a wake() cuts it short.
            self._wake.wait(self.interval)
            self._wake.clear()
