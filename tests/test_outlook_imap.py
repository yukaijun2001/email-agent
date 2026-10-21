import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import outlook_imap
from scripts.config import Config
from scripts.imap_reader import InboxReader


class _ImapConnection:
    def __init__(self):
        self.calls = []
        self.untagged = {"UIDVALIDITY": [b"42"], "UIDNEXT": [b"1"]}

    def authenticate(self, mechanism, callback):
        self.calls.append(("authenticate", mechanism, callback(b"")))
        return "OK", [b"AUTHENTICATE completed"]

    def login(self, *_args):
        raise AssertionError("Outlook OAuth accounts must not use IMAP LOGIN")

    def select(self, folder, readonly=False):
        self.calls.append(("select", folder, readonly))
        return "OK", [b"0"]

    def response(self, code):
        return code, self.untagged.get(code, [None])

    def logout(self):
        self.calls.append(("logout",))

    def shutdown(self):
        self.calls.append(("shutdown",))


class OutlookImapTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "inbox_config.json"
        self._patch = patch("scripts.config.CONFIG_PATH", self.path)
        self._patch.start()
        Config.reset_for_tests()

    def tearDown(self):
        self._patch.stop()
        Config.reset_for_tests()
        self._dir.cleanup()

    def test_refreshes_token_with_imap_scope_and_persists_rotation(self):
        account = Config.add_outlook_imap_account("user@outlook.com", "client-id-1", "refresh-old")
        calls = []

        def request(url, form):
            calls.append((url, form))
            return 200, {
                "access_token": "access-1",
                "refresh_token": "refresh-new",
                "expires_in": 3600,
            }

        oauth = outlook_imap.OutlookOAuth()
        with patch.object(outlook_imap, "_request_form", side_effect=request):
            self.assertEqual(oauth.access_token(account["id"]), "access-1")
            self.assertEqual(oauth.access_token(account["id"]), "access-1")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][1]["scope"], outlook_imap.IMAP_SCOPE)
        self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "refresh-new")
        self.assertFalse(Config.mailbox(account["id"])["password"])

    def test_device_flow_keeps_device_code_private_and_creates_account(self):
        responses = [
            (200, {
                "device_code": "private-device-code",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://microsoft.com/devicelogin",
                "expires_in": 900,
                "interval": 5,
            }),
            (400, {"error": "authorization_pending"}),
            (200, {
                "access_token": "access-1",
                "refresh_token": "refresh-1",
                "expires_in": 3600,
            }),
        ]
        oauth = outlook_imap.OutlookOAuth()
        with patch.object(outlook_imap, "_request_form", side_effect=lambda *_args: responses.pop(0)):
            started = oauth.start_device_flow("user@outlook.com", "client-id-1")
            self.assertNotIn("device_code", started)
            self.assertNotIn("private-device-code", str(started))
            oauth._flows[started["flow_id"]]["next_poll"] = 0
            pending = oauth.poll_device_flow(started["flow_id"])
            self.assertEqual(pending["status"], "pending")
            oauth._flows[started["flow_id"]]["next_poll"] = 0
            complete = oauth.poll_device_flow(started["flow_id"])
        self.assertEqual(complete["status"], "complete")
        saved = Config.mailbox(complete["account"]["id"])
        self.assertEqual(saved["protocol"], "outlook_imap")
        self.assertEqual(saved["imap_server"], "outlook.office365.com")
        self.assertEqual(saved["refresh_token"], "refresh-1")

    def test_imap_reader_uses_xoauth2_and_never_basic_login(self):
        account = Config.add_outlook_imap_account("user@outlook.com", "client-id-1", "refresh-1")
        connection = _ImapConnection()
        reader = InboxReader()
        oauth = reader.pool.oauth()
        with patch.object(oauth, "access_token", return_value="access-1"), \
             patch("scripts.imap_reader.imaplib.IMAP4_SSL", return_value=connection):
            self.assertEqual(reader.list_messages(account["id"]), [])
        auth = connection.calls[0]
        self.assertEqual(auth[:2], ("authenticate", "XOAUTH2"))
        self.assertEqual(
            auth[2],
            b"user=user@outlook.com\x01auth=Bearer access-1\x01\x01",
        )
        self.assertIn(("select", "INBOX", True), connection.calls)

    def test_outlook_account_public_shape_never_exposes_tokens(self):
        account = Config.add_outlook_imap_account("user@outlook.com", "client-id-1", "refresh-1")
        self.assertEqual(account["protocol"], "outlook_imap")
        self.assertTrue(account["has_refresh_token"])
        self.assertTrue(account["configured"])
        self.assertNotIn("refresh_token", account)
        self.assertNotIn("password", account)


if __name__ == "__main__":
    unittest.main()
