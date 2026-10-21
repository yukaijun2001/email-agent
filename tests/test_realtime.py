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
        self.patches = [
            patch("scripts.config.CONFIG_PATH", Path(self._dir.name) / "inbox_config.json"),
            patch.object(config, "APP_HOST", "127.0.0.1"),
        ]
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

    def _open_ws(self, origin=None, host=None):
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        origin = origin or f"http://127.0.0.1:{self.port}"
        sock.sendall((
            f"GET /ws HTTP/1.1\r\nHost: {host or f'127.0.0.1:{self.port}'}\r\nUpgrade: websocket\r\n"
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

    def _request(self, method, path, body=None, headers=None):
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        request_headers = {"Content-Type": "application/json"} if body is not None else {}
        request_headers.update(headers or {})
        conn.request(method, path, body=json.dumps(body) if body is not None else None, headers=request_headers)
        response = conn.getresponse()
        data = json.loads(response.read() or b"{}")
        conn.close()
        return response.status, data

    def test_server_deployment_can_add_mailbox_using_public_host(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            status, result = self._request("POST", "/api/accounts", {
                "imap_server": "imap.test", "imap_port": 993,
                "account": "deployed@test", "password": "test-app-password",
            }, headers={"Host": "203.0.113.10:8080", "Origin": "http://203.0.113.10:8080"})
        self.assertEqual(status, 201, result)
        self.assertNotIn("password", result)
        self.assertEqual(Config.connection_values(result["id"])[2], "deployed@test")

    def test_outlook_paste_preview_and_import(self):
        payload = {"text": "user@outlook.com----secret-password----clientid01----secret-token\nbad-row"}
        status, preview = self._request("POST", "/api/outlook/preview", payload)
        self.assertEqual(status, 200)
        self.assertEqual(preview["accounts"][0]["account"], "user@outlook.com")
        self.assertEqual(Config.snapshot()["accounts"], [])
        status, result = self._request("POST", "/api/outlook/import", payload)
        self.assertEqual(status, 201)
        self.assertEqual(result["added_lines"], [1])
        self.assertEqual(result["errors"][0]["line"], 2)
        self.assertEqual(result["added"][0]["protocol"], "outlook_imap")
        self.assertNotIn("secret-password", json.dumps(result))
        self.assertNotIn("secret-token", json.dumps(result))
        status, duplicate = self._request("POST", "/api/outlook/import", payload)
        self.assertEqual(status, 200)
        self.assertEqual(duplicate["added"], [])
        self.assertEqual(len(duplicate["errors"]), 2)

    def test_server_deployment_supports_account_crud_at_detected_addresses(self):
        cases = (
            ("203.0.113.10:8080", "http://203.0.113.10:8080"),
            ("inbox.example.test", "https://inbox.example.test"),
            ("inbox.example.test:443", "https://inbox.example.test"),
            ("inbox.example.test", "http://inbox.example.test:80"),
            ("localhost:18000", "http://localhost:18000"),
            ("[2001:db8::10]:8080", "http://[2001:db8::10]:8080"),
        )
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            for host, origin in cases:
                with self.subTest(host=host, origin=origin):
                    headers = {"Host": host, "Origin": origin}
                    payload = {"imap_server": "imap.test", "imap_port": 993,
                               "account": "deployed@test", "password": "fixture-only"}
                    status, created = self._request("POST", "/api/accounts", payload, headers=headers)
                    self.assertEqual(status, 201, created)
                    account_path = f"/api/accounts/{created['id']}"
                    status, snapshot = self._request("GET", "/api/accounts", headers=headers)
                    self.assertEqual(status, 200, snapshot)
                    self.assertNotIn("password", snapshot["accounts"][0])
                    payload.update(imap_port=995, password="")
                    status, edited = self._request("PUT", account_path, payload, headers=headers)
                    self.assertEqual((status, edited["imap_port"]), (200, 995))
                    self.assertEqual(Config.connection_values(created["id"])[3], "fixture-only")
                    status, snapshot = self._request("DELETE", account_path, headers=headers)
                    self.assertEqual((status, snapshot["accounts"]), (200, []))

    def test_server_deployment_rejects_cross_origin_account_changes(self):
        account_id = Config.add_account("imap.test", 993, "deployed@test", "fixture-only")["id"]
        payload = {"imap_server": "imap.test", "imap_port": 993,
                   "account": "foreign@test", "password": "fixture-only"}
        origins = ("https://evil.example", "https://inbox.example.test:444", "null",
                   "https://inbox.example.test/path", "https://inbox.example.test@evil.example")
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            for origin in origins:
                for method, path in (("POST", "/api/accounts"), ("PUT", f"/api/accounts/{account_id}"),
                                     ("DELETE", f"/api/accounts/{account_id}")):
                    with self.subTest(origin=origin, method=method):
                        status, _ = self._request(method, path, payload,
                                                  headers={"Host": "inbox.example.test", "Origin": origin})
                        self.assertEqual(status, 403)
        self.assertEqual(Config.account_ids(), [account_id])

    def test_server_deployment_rejects_cross_site_write_without_origin(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            status, _ = self._request("DELETE", "/api/accounts/aaaaaaaa",
                                      headers={"Host": "inbox.example.test", "Sec-Fetch-Site": "cross-site"})
        self.assertEqual(status, 403)

    def test_server_deployment_keeps_cli_requests_without_origin_working(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            status, result = self._request("POST", "/api/accounts", {
                "imap_server": "imap.test", "imap_port": 993,
                "account": "cli@test", "password": "fixture-only",
            }, headers={"Host": "203.0.113.10:8080"})
        self.assertEqual(status, 201, result)

    def test_server_deployment_rejects_invalid_hosts(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            for host in ("", "https://inbox.example.test", "user@inbox.example.test", "inbox.example.test/path",
                         "inbox.example.test:65536", "inbox.example.test:0", "inbox.example.test:",
                         "inbox.example.test,evil.test", "bad host", "bad..test", "[not-an-ip]"):
                with self.subTest(host=host):
                    status, _ = self._request("GET", "/api/accounts", headers={"Host": host})
                    self.assertEqual(status, 403)

    def test_server_deployment_rejects_duplicate_hosts(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
            try:
                conn.putrequest("GET", "/api/accounts", skip_host=True)
                conn.putheader("Host", "inbox.example.test")
                conn.putheader("Host", "evil.example")
                conn.endheaders()
                self.assertEqual(conn.getresponse().status, 403)
            finally:
                conn.close()

    def test_server_deployment_websocket_accepts_public_same_origin(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            sock, stream, status, _headers, _key = self._open_ws(
                host="inbox.example.test", origin="https://inbox.example.test",
            )
            try:
                self.assertIn("101", status)
            finally:
                sock.close()
                stream.close()

    def test_server_deployment_websocket_rejects_other_origins(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            for origin in ("https://evil.example", "https://inbox.example.test:444", "null"):
                with self.subTest(origin=origin):
                    sock, stream, status, _headers, _key = self._open_ws(
                        host="inbox.example.test", origin=origin,
                    )
                    try:
                        self.assertIn("403", status)
                    finally:
                        sock.close()
                        stream.close()

    def test_server_deployment_does_not_trust_forwarded_host(self):
        with patch.object(config, "APP_HOST", "0.0.0.0"):
            status, _ = self._request("DELETE", "/api/accounts/aaaaaaaa", headers={
                "Host": "inbox.example.test", "Origin": "https://evil.example",
                "X-Forwarded-Host": "evil.example", "Forwarded": "host=evil.example;proto=https",
            })
        self.assertEqual(status, 403)

    def test_server_deployment_csp_uses_actual_public_websocket_address(self):
        for host in ("203.0.113.10:8080", "inbox.example.test"):
            with self.subTest(host=host):
                conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
                try:
                    conn.request("GET", "/", headers={"Host": host})
                    response = conn.getresponse()
                    csp = response.getheader("Content-Security-Policy")
                    response.read()
                    self.assertIn(f"ws://{host}", csp)
                    self.assertIn(f"wss://{host}", csp)
                    self.assertNotIn("ws://127.0.0.1:8000", csp)
                finally:
                    conn.close()

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
