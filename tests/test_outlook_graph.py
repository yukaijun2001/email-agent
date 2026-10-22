import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from scripts import outlook_oauth, graph_reader
from scripts.config import Config
from scripts.imap_reader import InboxReader, MailboxError


class OutlookGraphTests(unittest.TestCase):
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

    def test_scope_rejection_is_not_reported_as_expired_token(self):
        for description in (
            "AADSTS70000: One or more scopes requested are unauthorized or expired.",
            "AADSTS65001: The user has not consented to use the application.",
        ):
            with self.subTest(description=description):
                message = outlook_oauth._oauth_error({
                    "error": "invalid_grant", "error_description": description,
                }, 400)
                self.assertIn("权限", message)
                self.assertNotIn("已失效", message)
                self.assertIn(description.split(":")[0], message)

    def test_generic_oauth_rejection_does_not_claim_expiry_or_echo_secrets(self):
        payload = {"error": "invalid_grant", "error_description": "Request contained private-refresh-token"}
        for message in (outlook_oauth._oauth_error(payload, 400),
                        graph_reader.explain_graph_error(json.dumps(payload), 400)):
            self.assertNotIn("private-refresh-token", message)
            self.assertNotIn("失效", message)
            self.assertNotIn("过期", message)
            self.assertIn("授权范围", message)

    def test_graph_only_four_part_account_reads_without_imap_and_keeps_rotation(self):
        account = Config.import_outlook_accounts(
            "user@outlook.com----unused-password----client-id-1----refresh-old"
        )["added"][0]
        token_forms = []

        class Response:
            status = 200

            def __init__(self, payload):
                self.payload = json.dumps(payload).encode()

            def read(self):
                return self.payload

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

        def request(req, timeout=None):
            if req.full_url.endswith("/token"):
                form = parse_qs(req.data.decode())
                token_forms.append(form)
                self.assertNotIn("unused-password", req.data.decode())
                self.assertEqual(form["scope"], [graph_reader.GRAPH_SCOPE])
                return Response({"access_token": "graph-access", "refresh_token": "refresh-new",
                                 "expires_in": 3600, "scope": "https://graph.microsoft.com/Mail.Read"})
            self.assertEqual(req.get_header("Authorization"), "Bearer graph-access")
            if "/messages/delta" in req.full_url:
                return Response({"value": [], "@odata.deltaLink":
                    "https://graph.microsoft.com/v1.0/me/mailFolders/inbox/messages/delta?token=2"})
            if "/inbox/messages?" in req.full_url:
                return Response({"value": [{"id": "message-id", "subject": "Graph inbox"}]})
            return Response({"id": "message-id", "subject": "Graph inbox",
                             "body": {"contentType": "html", "content": "<p>Graph body</p>"}})

        reader = InboxReader()
        try:
            with patch("urllib.request.urlopen", side_effect=request), \
                 patch("scripts.imap_reader.imaplib.IMAP4_SSL", side_effect=AssertionError("No IMAP")):
                messages = reader.list_messages(account["id"])
                self.assertEqual(messages[0]["subject"], "Graph inbox")
                self.assertIn("Graph body", reader.get_message_html(account["id"], messages[0]["uid"]))
                self.assertEqual(reader.check_new(account["id"], None, None), ("graph", 1, []))
                reader.list_messages(account["id"])
                self.assertEqual(len(token_forms), 1)
                self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "refresh-new")
                self.assertFalse(Config.mailbox(account["id"])["password"])
                # Replacing credentials must invalidate the cached access token.
                Config.update_account(account["id"], "", 993, "user@outlook.com", "",
                                      refresh_token="replacement-token")
                reader.list_messages(account["id"])
                self.assertEqual(len(token_forms), 2)
        finally:
            reader.close()

    def test_expired_refresh_token_does_not_open_imap(self):
        account = Config.add_outlook_account("user@outlook.com", "client-id-1", "refresh-old")
        reader = InboxReader()
        try:
            error = json.dumps({"error": "invalid_grant", "error_description":
                                "AADSTS700082: Refresh token expired."}).encode()
            with patch.object(graph_reader, "_request", return_value=(400, error)), \
                 patch("scripts.imap_reader.imaplib.IMAP4_SSL") as imap:
                with self.assertRaisesRegex(MailboxError, "过期"):
                    reader.list_messages(account["id"])
                imap.assert_not_called()
        finally:
            reader.close()

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
        oauth = outlook_oauth.OutlookOAuth()
        with patch.object(outlook_oauth, "_request_form", side_effect=lambda *_args: responses.pop(0)) as request:
            started = oauth.start_device_flow("user@outlook.com", "client-id-1")
            self.assertEqual(request.call_args.args[1]["scope"], graph_reader.GRAPH_SCOPE)
            self.assertNotIn("device_code", started)
            self.assertNotIn("private-device-code", str(started))
            oauth._flows[started["flow_id"]]["next_poll"] = 0
            pending = oauth.poll_device_flow(started["flow_id"])
            self.assertEqual(pending["status"], "pending")
            oauth._flows[started["flow_id"]]["next_poll"] = 0
            complete = oauth.poll_device_flow(started["flow_id"])
        self.assertEqual(complete["status"], "complete")
        saved = Config.mailbox(complete["account"]["id"])
        self.assertEqual(saved["protocol"], "graph")
        self.assertEqual(saved["imap_server"], "graph.microsoft.com")
        self.assertEqual(saved["refresh_token"], "refresh-1")
        self.assertTrue(complete["account"]["configured"])
        with patch.object(outlook_oauth, "_request_form", return_value=(200, {
            "device_code": "new-code", "user_code": "NEW-CODE",
        })):
            self.assertIn("flow_id", oauth.start_device_flow(
                "user@outlook.com", "client-id-1", saved["id"]))

    def test_outlook_account_public_shape_never_exposes_tokens(self):
        account = Config.add_outlook_account("user@outlook.com", "client-id-1", "refresh-1")
        self.assertEqual(account["protocol"], "graph")
        self.assertTrue(account["has_refresh_token"])
        self.assertTrue(account["configured"])
        self.assertNotIn("refresh_token", account)
        self.assertNotIn("password", account)


if __name__ == "__main__":
    unittest.main()
