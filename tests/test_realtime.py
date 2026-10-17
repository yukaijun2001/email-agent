import base64
import io
import json
import os
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from scripts import config, main, realtime
from scripts.config import Config
from scripts.imap_reader import MailboxError


def client_frame(opcode, payload=b"", mask=b"\x01\x02\x03\x04"):
    """Build a masked client->server frame."""
    header = bytearray([0x80 | opcode])
    length = len(payload)
    if length < 126:
        header.append(0x80 | length)
    else:
        header.append(0x80 | 126)
        header += struct.pack("!H", length)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return bytes(header) + mask + masked


def read_server_frame(stream):
    first, second = stream.read(2)
    length = second & 0x7F
    if length == 126:
        (length,) = struct.unpack("!H", stream.read(2))
    elif length == 127:
        (length,) = struct.unpack("!Q", stream.read(8))
    return first & 0x0F, stream.read(length)


class FrameTests(unittest.TestCase):
    def test_accept_key_matches_rfc_example(self):
        self.assertEqual(realtime.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")
        self.assertTrue(realtime.valid_client_key("dGhlIHNhbXBsZSBub25jZQ=="))
        self.assertFalse(realtime.valid_client_key("short"))
        self.assertFalse(realtime.valid_client_key(None))

    def test_round_trip_lengths(self):
        for size in (0, 125, 126, 70000):
            payload = os.urandom(size)
            opcode, data = read_server_frame(io.BytesIO(realtime.encode_frame(realtime.OP_TEXT, payload)))
            self.assertEqual((opcode, data), (realtime.OP_TEXT, payload))

    def test_read_masked_client_frame(self):
        fin, opcode, payload = realtime.read_frame(io.BytesIO(client_frame(realtime.OP_TEXT, b"hello")))
        self.assertEqual((fin, opcode, payload), (True, realtime.OP_TEXT, b"hello"))

    def test_unmasked_client_frame_is_rejected(self):
        with self.assertRaises(realtime.ProtocolError):
            realtime.read_frame(io.BytesIO(bytes([0x81, 0x02]) + b"hi"))


class FakeReader:
    def __init__(self):
        self.responses = {}

    def check_new(self, account_id, validity, last_uid, limit=20):
        result = self.responses[account_id]
        if isinstance(result, Exception):
            raise result
        return result


class RecordingHub(realtime.Hub):
    def __init__(self):
        super().__init__()
        self.events = []

    def broadcast(self, event):
        self.events.append(event)
        return 1


class WatcherTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self._patch = patch("scripts.config.CONFIG_PATH", Path(self._dir.name) / "inbox_config.json")
        self._patch.start()
        Config.reset_for_tests()
        self.account = Config.add_account("imap.test", 993, "user@test", "secret")["id"]

    def tearDown(self):
        self._patch.stop()
        Config.reset_for_tests()
        self._dir.cleanup()

    def test_baseline_then_push_then_error_recovery(self):
        hub, reader = RecordingHub(), FakeReader()
        watcher = realtime.MailWatcher(hub, reader, interval=60)
        reader.responses[self.account] = ("1", 9, [])
        watcher.run_once()
        self.assertEqual(hub.events, [])

        mail = {"account_id": self.account, "uid": "10", "subject": "Hi"}
        reader.responses[self.account] = ("1", 10, [mail])
        watcher.run_once()
        self.assertEqual(hub.events, [{"type": "new_emails", "account_id": self.account, "emails": [mail]}])

        hub.events.clear()
        reader.responses[self.account] = MailboxError("登录失败")
        watcher.run_once()
        watcher.run_once()  # Same error twice is only announced once.
        self.assertEqual(hub.events, [
            {"type": "account_status", "account_id": self.account, "ok": False, "error": "登录失败"}
        ])
        self.assertEqual(watcher._state[self.account]["last_uid"], 10)

        hub.events.clear()
        reader.responses[self.account] = ("1", 10, [])
        watcher.run_once()
        self.assertEqual(hub.events, [{"type": "account_status", "account_id": self.account, "ok": True, "error": ""}])

    def test_removed_accounts_are_forgotten(self):
        hub, reader = RecordingHub(), FakeReader()
        watcher = realtime.MailWatcher(hub, reader, interval=60)
        reader.responses[self.account] = ("1", 9, [])
        watcher.run_once()
        Config.remove_account(self.account)
        watcher.run_once()
        self.assertEqual(watcher._state, {})


class StubReader:
    """Reader used by the end-to-end test: one new mail appears after the baseline."""

    def __init__(self):
        self.calls = 0

    def check_new(self, account_id, validity, last_uid, limit=20):
        self.calls += 1
        if last_uid is None:
            return "1", 5, []
        if last_uid == 5:
            return "1", 6, [{"account_id": account_id, "uid": "6", "subject": "实时", "from": "a@b", "date": "", "timestamp": 0}]
        return "1", last_uid, []

    def list_all(self, account_ids, limit):
        return [], []

    def get_message_html(self, account_id, uid):
        if uid == "404":
            return None
        return "<!doctype html><p>rich body</p>"


class EndToEndTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.patches = [patch("scripts.config.CONFIG_PATH", Path(self._dir.name) / "inbox_config.json")]
        for p in self.patches:
            p.start()
        Config.reset_for_tests()
        self.hub = realtime.Hub()
        self.reader = StubReader()
        self.watcher = realtime.MailWatcher(self.hub, self.reader, interval=0.2)
        self.hub.on_first_client = self.watcher.wake
        handler = type("Handler", (main.InboxHandler,), {
            "hub": self.hub, "reader": self.reader, "watcher": self.watcher,
            "log_message": lambda *a: None,
        })
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.patches.append(patch.object(config, "APP_PORT", self.port))
        self.patches[-1].start()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.watcher.start()

    def tearDown(self):
        self.watcher.stop()
        self.server.shutdown()
        self.server.server_close()
        for p in reversed(self.patches):
            p.stop()
        Config.reset_for_tests()
        self._dir.cleanup()

    def _open_ws(self, origin=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        origin = origin or f"http://127.0.0.1:{self.port}"
        sock.sendall((
            f"GET /ws HTTP/1.1\r\nHost: 127.0.0.1:{self.port}\r\nUpgrade: websocket\r\n"
            f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
            f"Origin: {origin}\r\n\r\n"
        ).encode())
        stream = sock.makefile("rb")
        status = stream.readline().decode()
        headers = {}
        while True:
            line = stream.readline().decode().strip()
            if not line:
                break
            name, _, value = line.partition(":")
            headers[name.lower()] = value.strip()
        return sock, stream, status, headers, key

    def _request(self, method, path, body=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_accounts_api_and_new_mail_push(self):
        status, created = self._request("POST", "/api/accounts", {
            "imap_server": "imap.test", "imap_port": 993, "account": "u@test", "password": "pw",
        })
        self.assertEqual(status, 201)
        self.assertNotIn("password", created)

        sock, stream, status, headers, key = self._open_ws()
        try:
            self.assertIn("101", status)
            self.assertEqual(headers["sec-websocket-accept"], realtime.accept_key(key))
            opcode, payload = read_server_frame(stream)
            self.assertEqual(json.loads(payload)["type"], "hello")

            deadline = time.time() + 5
            event = None
            while time.time() < deadline:
                opcode, payload = read_server_frame(stream)
                if opcode == realtime.OP_TEXT:
                    event = json.loads(payload)
                    if event["type"] == "new_emails":
                        break
            self.assertIsNotNone(event)
            self.assertEqual(event["type"], "new_emails")
            self.assertEqual(event["account_id"], created["id"])
            self.assertEqual(event["emails"][0]["subject"], "实时")

            # Editing accounts is broadcast so other tabs refresh their list.
            status, _ = self._request("PUT", f"/api/accounts/{created['id']}", {
                "imap_server": "imap.test", "imap_port": 995, "account": "u@test", "password": "",
            })
            self.assertEqual(status, 200)
            seen = []
            while time.time() < deadline and "accounts_changed" not in seen:
                opcode, payload = read_server_frame(stream)
                if opcode == realtime.OP_TEXT:
                    seen.append(json.loads(payload)["type"])
            self.assertIn("accounts_changed", seen)

            sock.sendall(client_frame(realtime.OP_CLOSE, struct.pack("!H", 1000)))
            while True:
                opcode, _ = read_server_frame(stream)
                if opcode == realtime.OP_CLOSE:
                    break
        finally:
            stream.close()
            sock.close()

        status, snapshot = self._request("DELETE", f"/api/accounts/{created['id']}")
        self.assertEqual((status, snapshot["accounts"]), (200, []))

    def test_cross_origin_websocket_is_rejected(self):
        sock, stream, status, _headers, _key = self._open_ws(origin="http://evil.example")
        stream.close()
        sock.close()
        self.assertIn("403", status)

    def test_foreign_host_header_is_rejected(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/accounts", headers={"Host": "attacker.example"})
        self.assertEqual(conn.getresponse().status, 403)
        conn.close()

    def test_html_body_is_served_with_strict_csp(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/accounts/aaaaaaaa/emails/9/html")
        response = conn.getresponse()
        page = response.read().decode()
        csp = response.getheader("Content-Security-Policy")
        conn.close()
        self.assertEqual(response.status, 200)
        self.assertTrue(response.getheader("Content-Type").startswith("text/html"))
        self.assertIn("rich body", page)
        self.assertIn("default-src * data: blob:", csp)  # Remote content loads.
        self.assertIn("script-src 'none'", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("form-action 'none'", csp)
        self.assertIn("frame-ancestors 'self'", csp)

        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/accounts/aaaaaaaa/emails/404/html")
        self.assertEqual(conn.getresponse().status, 404)
        conn.close()

    def test_app_page_allows_same_origin_frames_only(self):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/")
        csp = conn.getresponse().getheader("Content-Security-Policy")
        conn.close()
        self.assertIn("frame-src 'self'", csp)
        self.assertIn("frame-ancestors 'none'", csp)

    def test_unknown_account_filter_returns_404(self):
        status, _ = self._request("GET", "/api/emails?account=deadbeef")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
