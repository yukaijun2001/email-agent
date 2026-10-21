import json
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from scripts import graph_reader
from scripts.config import Config
from scripts.imap_reader import MailboxError


class _Response:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload if isinstance(payload, bytes) else json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class GraphReaderTests(unittest.TestCase):
    def setUp(self):
        self._dir = __import__("tempfile").TemporaryDirectory()
        self.path = __import__("pathlib").Path(self._dir.name) / "inbox_config.json"
        self._patch = patch("scripts.config.CONFIG_PATH", self.path)
        self._patch.start()
        Config.reset_for_tests()
        created = Config.add_graph_account(
            "user@outlook.com", "mailbox-secret", "client-id-1", "refresh-old",
        )
        self.account_id = created["id"]
        self.calls = []

    def tearDown(self):
        self._patch.stop()
        Config.reset_for_tests()
        self._dir.cleanup()

    def _route(self, request, timeout=None):
        del timeout
        body = request.data.decode() if request.data else ""
        self.calls.append((request.full_url, body))
        if request.full_url.endswith("/token"):
            self.assertIn("client_id=client-id-1", body)
            self.assertNotIn("mailbox-secret", body)
            rotated = "refresh_token=refresh-old" in body
            return _Response(200, {
                "access_token": "access-1",
                "refresh_token": "refresh-new" if rotated else "refresh-new",
                "expires_in": 3600,
            })
        if "/messages/delta" in request.full_url:
            return _Response(200, {"value": [], "@odata.deltaLink": "https://graph.test/delta?token=2"})
        if request.full_url.endswith("/messages") or "/inbox/messages?" in request.full_url:
            return _Response(200, {"value": [{
                "id": "AAMkAGI=",
                "subject": "你好",
                "from": {"emailAddress": {"name": "Ann", "address": "ann@example.com"}},
                "receivedDateTime": "2026-10-20T08:00:00Z",
            }]})
        if "/attachments" in request.full_url:
            return _Response(200, {"value": []})
        return _Response(200, {
            "id": "AAMkAGI=",
            "subject": "你好",
            "from": {"emailAddress": {"name": "Ann", "address": "ann@example.com"}},
            "toRecipients": [{"emailAddress": {"address": "user@outlook.com"}}],
            "receivedDateTime": "2026-10-20T08:00:00Z",
            "hasAttachments": False,
            "body": {"contentType": "html", "content": "<p>正文</p>"},
        })

    def test_lists_inbox_and_stores_rotated_refresh_token(self):
        client = graph_reader.GraphClient()
        with patch.object(graph_reader.urllib.request, "urlopen", side_effect=self._route):
            messages = client.list_messages(self.account_id, 10)
        self.assertEqual(messages[0]["subject"], "你好")
        self.assertEqual(messages[0]["from"], "Ann <ann@example.com>")
        self.assertEqual(Config.mailbox(self.account_id)["refresh_token"], "refresh-new")
        self.assertEqual(Config.mailbox(self.account_id)["password"], "mailbox-secret")
        uid = messages[0]["uid"]
        self.assertEqual(graph_reader.decode_message_uid(uid), "AAMkAGI=")

    def test_message_body_and_new_mail_baseline(self):
        client = graph_reader.GraphClient()
        with patch.object(graph_reader.urllib.request, "urlopen", side_effect=self._route):
            messages = client.list_messages(self.account_id, 10)
            loaded = client.load_message(self.account_id, messages[0]["uid"])
            validity, last_uid, fresh = client.check_new(self.account_id, None, None)
        self.assertIn("<p>正文</p>", loaded[1].parts[0][1])
        self.assertEqual(loaded[0]["to"], "user@outlook.com")
        self.assertEqual((validity, last_uid, fresh), ("graph", 1, []))

    def test_invalid_grant_is_explained(self):
        def fail(request, timeout=None):
            del timeout
            error = HTTPError(request.full_url, 400, "bad", hdrs=None, fp=None)
            payload = json.dumps({"error": "invalid_grant", "error_description": "AADSTS70000"}).encode()
            error.read = lambda: payload
            raise error

        client = graph_reader.GraphClient()
        with patch.object(graph_reader.urllib.request, "urlopen", side_effect=fail):
            with self.assertRaisesRegex(MailboxError, "refresh_token"):
                client.list_messages(self.account_id, 5)


if __name__ == "__main__":
    unittest.main()
