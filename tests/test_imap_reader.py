import email
import imaplib
import unittest
from unittest.mock import patch

from scripts import imap_reader


HEADERS = {
    "4": b"From: old@example.com\nSubject: Older\nDate: Mon, 12 Oct 2026 08:00:00 +0000\n\n",
    "9": b"From: sender@example.com\nSubject: Hello\nDate: Wed, 14 Oct 2026 08:00:00 +0000\n\n",
    "12": b"From: new@example.com\nSubject: Newest\nDate: Fri, 16 Oct 2026 08:00:00 +0000\n\n",
}
WRITE_COMMANDS = {"store", "copy", "move", "append", "expunge", "delete"}


class FakeConnection:
    """Minimal imaplib stand-in. ``uids`` is the INBOX in sequence order."""

    instances = 0

    def __init__(self, uids=("4", "9"), validity=b"77", uidnext=True):
        FakeConnection.instances += 1
        self.calls = []
        self.uids = list(uids)
        self.validity = validity
        self.uidnext = uidnext
        self.dead = False
        self.untagged = {}

    def _check(self):
        if self.dead:
            raise imaplib.IMAP4.abort("socket error: EOF")

    def login(self, account, password):
        self.calls.append(("login", account, password))

    def select(self, folder, readonly=False):
        self._check()
        self.calls.append(("select", folder, readonly))
        self.untagged = {"UIDVALIDITY": [self.validity]}
        if self.uidnext:
            self.untagged["UIDNEXT"] = [str(int(self.uids[-1]) + 1 if self.uids else 1).encode()]
        return "OK", [str(len(self.uids)).encode()]

    def response(self, code):
        return code, self.untagged.get(code, [None])

    def _header_items(self, uids):
        return [
            (f"{self.uids.index(u) + 1} (UID {u} BODY[HEADER.FIELDS (FROM SUBJECT DATE)] {{65}}".encode(), HEADERS[u])
            for u in uids
        ]

    def fetch(self, message_set, query):
        self._check()
        self.calls.append(("seqfetch", message_set, query))
        if message_set == "*":
            return "OK", [f"{len(self.uids)} (UID {self.uids[-1]})".encode()]
        start, end = (int(n) for n in message_set.split(":"))
        return "OK", self._header_items(self.uids[start - 1:end])

    def uid(self, command, *args):
        self._check()
        self.calls.append((command, *args))
        if command == "search":
            low = int(args[-1][4:].split(":")[0])
            # Real servers return the highest UID for "n:*" even when it is below n.
            found = [u for u in self.uids if int(u) >= low] or self.uids[-1:]
            return "OK", [" ".join(found).encode()]
        if "HEADER.FIELDS" in str(args):
            return "OK", self._header_items(args[0].split(","))
        raw = b"From: sender@example.com\nSubject: Hello\nContent-Type: text/plain; charset=utf-8\n\nRead only body"
        return "OK", [(f"2 (UID {args[0]} BODY[] {{120}}".encode(), raw), b")"]

    def logout(self):
        self.calls.append(("logout",))

    def shutdown(self):
        self.calls.append(("shutdown",))


def _values(_account_id):
    return ("imap.test", 993, "user@test", "secret")


class ReaderTests(unittest.TestCase):
    def setUp(self):
        FakeConnection.instances = 0

    def assertReadOnly(self, connection):
        self.assertIn(("select", "INBOX", True), connection.calls)
        self.assertFalse(any(call[0] in WRITE_COMMANDS for call in connection.calls))
        self.assertFalse(any(call[0] == "select" and not call[2] for call in connection.calls))

    def patched(self, connection_factory):
        if isinstance(connection_factory, FakeConnection):
            instance = connection_factory
            connection_factory = lambda *a, **k: instance
        return (
            patch.object(imap_reader.config.Config, "connection_values", side_effect=_values),
            patch.object(imap_reader.imaplib, "IMAP4_SSL", side_effect=connection_factory),
        )

    def test_list_fetches_only_newest_by_sequence_number(self):
        connection = FakeConnection(("4", "9", "12"))
        a, b = self.patched(connection)
        with a, b:
            messages = imap_reader.InboxReader().list_messages("aaaaaaaa", 2)
        self.assertEqual([m["uid"] for m in messages], ["12", "9"])
        self.assertEqual({m["account_id"] for m in messages}, {"aaaaaaaa"})
        # No SEARCH ALL: one EXAMINE plus one FETCH of the last two messages.
        self.assertEqual([c[0] for c in connection.calls], ["login", "select", "seqfetch"])
        self.assertEqual(connection.calls[-1][1], "2:3")
        self.assertIn("BODY.PEEK", connection.calls[-1][2])
        self.assertReadOnly(connection)

    def test_empty_inbox_skips_fetch(self):
        connection = FakeConnection(())
        a, b = self.patched(connection)
        with a, b:
            self.assertEqual(imap_reader.InboxReader().list_messages("aaaaaaaa"), [])
        self.assertNotIn("seqfetch", [c[0] for c in connection.calls])

    def test_sessions_are_reused_between_requests(self):
        connection = FakeConnection()
        a, b = self.patched(connection)
        with a, b:
            reader = imap_reader.InboxReader()
            reader.list_messages("aaaaaaaa")
            reader.list_messages("aaaaaaaa")
            reader.get_message("aaaaaaaa", "9")
        self.assertEqual(FakeConnection.instances, 1)
        self.assertEqual([c for c in connection.calls if c[0] == "login"], [("login", "user@test", "secret")])
        self.assertNotIn(("logout",), connection.calls)

    def test_dead_pooled_session_is_replaced_transparently(self):
        connections = [FakeConnection(), FakeConnection()]
        a, b = self.patched(lambda *args, **kw: connections.pop(0))
        with a, b:
            reader = imap_reader.InboxReader()
            reader.list_messages("aaaaaaaa")
            first = reader.pool._idle["aaaaaaaa"][0].connection
            first.dead = True  # Server dropped the idle connection.
            messages = reader.list_messages("aaaaaaaa")
        self.assertEqual([m["uid"] for m in messages], ["9", "4"])
        self.assertEqual(FakeConnection.instances, 2)

    def test_changed_settings_open_a_new_session(self):
        connections = [FakeConnection(), FakeConnection()]
        settings = {"value": ("imap.test", 993, "user@test", "secret")}
        with patch.object(imap_reader.config.Config, "connection_values", side_effect=lambda _id: settings["value"]), \
             patch.object(imap_reader.imaplib, "IMAP4_SSL", side_effect=lambda *a, **k: connections.pop(0)):
            reader = imap_reader.InboxReader()
            reader.list_messages("aaaaaaaa")
            settings["value"] = ("imap.test", 993, "user@test", "new-secret")
            reader.list_messages("aaaaaaaa")
        self.assertEqual(FakeConnection.instances, 2)

    def test_list_all_merges_by_date_and_isolates_failures(self):
        connections = {"aaaaaaaa": FakeConnection(("4", "9")), "bbbbbbbb": FakeConnection(("12",))}

        def values(account_id):
            if account_id == "cccccccc":
                raise imap_reader.config.AccountNotFound(account_id)
            return (account_id, 993, "user@test", "secret")

        with patch.object(imap_reader.config.Config, "connection_values", side_effect=values), \
             patch.object(imap_reader.imaplib, "IMAP4_SSL", side_effect=lambda host, *a, **k: connections[host]):
            messages, errors = imap_reader.InboxReader().list_all(["aaaaaaaa", "bbbbbbbb", "cccccccc"], 50)
        self.assertEqual([(m["account_id"], m["uid"]) for m in messages],
                         [("bbbbbbbb", "12"), ("aaaaaaaa", "9"), ("aaaaaaaa", "4")])
        self.assertEqual(errors, [{"account_id": "cccccccc", "error": "邮箱不存在或已被删除。"}])

    def test_check_new_costs_one_round_trip_when_idle(self):
        connection = FakeConnection(("4", "9"))
        a, b = self.patched(connection)
        with a, b:
            reader = imap_reader.InboxReader()
            validity, last, emails = reader.check_new("aaaaaaaa", "", None)
            self.assertEqual((validity, last, emails), ("77", 9, []))
            connection.calls.clear()
            self.assertEqual(reader.check_new("aaaaaaaa", validity, last), ("77", 9, []))
            self.assertEqual([c[0] for c in connection.calls], ["select"])
            connection.uids.append("12")
            validity, last, emails = reader.check_new("aaaaaaaa", validity, last)
        self.assertEqual(last, 12)
        self.assertEqual([m["subject"] for m in emails], ["Newest"])
        self.assertReadOnly(connection)

    def test_check_new_without_uidnext_falls_back_to_search(self):
        connection = FakeConnection(("4", "9"), uidnext=False)
        a, b = self.patched(connection)
        with a, b:
            reader = imap_reader.InboxReader()
            validity, last, _ = reader.check_new("aaaaaaaa", "", None)
            self.assertEqual(last, 9)
            # "10:*" returns UID 9 on real servers; it must not be reported as new.
            self.assertEqual(reader.check_new("aaaaaaaa", validity, last), ("77", 9, []))
            connection.uids.append("12")
            _, last, emails = reader.check_new("aaaaaaaa", validity, last)
        self.assertEqual((last, [m["uid"] for m in emails]), (12, ["12"]))

    def test_check_new_resets_on_uidvalidity_change(self):
        connection = FakeConnection(("4", "9", "12"), validity=b"88")
        a, b = self.patched(connection)
        with a, b:
            self.assertEqual(imap_reader.InboxReader().check_new("aaaaaaaa", "77", 4), ("88", 12, []))

    def test_opening_message_uses_peek_and_is_cached(self):
        connection = FakeConnection()
        a, b = self.patched(connection)
        with a, b:
            reader = imap_reader.InboxReader()
            message = reader.get_message("aaaaaaaa", "9")
            fetches = len([c for c in connection.calls if c[0] == "fetch"])
            again = reader.get_message("aaaaaaaa", "9")
        self.assertEqual(message["subject"], "Hello")
        self.assertEqual(message["account_id"], "aaaaaaaa")
        self.assertEqual(again, message)
        self.assertEqual(len([c for c in connection.calls if c[0] == "fetch"]), fetches)
        self.assertTrue(any("BODY.PEEK[]" in str(call) for call in connection.calls))
        self.assertReadOnly(connection)

    def _open(self, raw):
        connection = FakeConnection()
        connection.uid = lambda command, *args: (connection.calls.append((command, *args)) or
                                                 ("OK", [(b"2 (UID 9 BODY[] {1}", raw), b")"]))
        a, b = self.patched(connection)
        with a, b:
            reader = imap_reader.InboxReader()
            message = reader.get_message("aaaaaaaa", "9")
            html = reader.get_message_html("aaaaaaaa", "9")
        return connection, message, html

    def test_html_message_renders_everything(self):
        raw = (
            b'From: a@b\nTo: me@x\nSubject: Rich\nMIME-Version: 1.0\n'
            b'Content-Type: multipart/mixed; boundary="M"\n\n'
            b'--M\nContent-Type: multipart/related; boundary="R"\n\n'
            b'--R\nContent-Type: multipart/alternative; boundary="A"\n\n'
            b'--A\nContent-Type: text/plain; charset=utf-8\n\nplain version\n'
            b'--A\nContent-Type: text/html; charset=utf-8\n\n'
            b'<p>Hi <b>there</b></p><img src="cid:logo@x"><img src="https://cdn.example/p.gif">'
            b'<script>alert(1)</script>\n'
            b'--A--\n'
            b'--R\nContent-Type: image/png\nContent-ID: <logo@x>\nContent-Transfer-Encoding: base64\n\niVBORw0KGgo=\n'
            b'--R--\n'
            b'--M\nContent-Type: image/jpeg; name="photo.jpg"\nContent-Disposition: attachment; filename="photo.jpg"\n'
            b'Content-Transfer-Encoding: base64\n\n/9j/4AAQ\n'
            b'--M\nContent-Type: application/pdf; name="doc.pdf"\nContent-Disposition: attachment; filename="doc.pdf"\n'
            b'Content-Transfer-Encoding: base64\n\nJVBERi0=\n'
            b'--M--\n'
        )
        connection, message, html = self._open(raw)
        self.assertEqual(message["to"], "me@x")
        self.assertNotIn("body", message)
        self.assertIn("<p>Hi <b>there</b></p>", html)
        self.assertNotIn("plain version", html)  # HTML alternative wins.
        self.assertIn('src="data:image/png;base64,iVBORw0KGgo="', html)
        self.assertIn('src="https://cdn.example/p.gif"', html)  # Remote images load.
        self.assertIn('src="data:image/jpeg;base64,/9j/4AAQ"', html)  # Image attachment shown.
        self.assertIn("photo.jpg", html)
        self.assertIn("doc.pdf", html)
        self.assertNotIn("<script", html)
        # The inline logo is referenced by cid:, so it is not repeated at the end.
        self.assertEqual(html.count("iVBORw0KGgo="), 1)
        self.assertEqual(len([c for c in connection.calls if c[0] == "fetch"]), 1)

    def test_plain_text_message_is_rendered_as_rich_text(self):
        raw = b"From: a@b\nSubject: Hi\nContent-Type: text/plain; charset=utf-8\n\nLine <1>\nhttps://example.com/x"
        _connection, _message, html = self._open(raw)
        self.assertIn('<div class="mail-text">Line &lt;1&gt;', html)
        self.assertIn('<a href="https://example.com/x"', html)

    def test_forwarded_message_is_shown_inline(self):
        raw = (
            b'From: a@b\nSubject: Fwd\nMIME-Version: 1.0\nContent-Type: multipart/mixed; boundary="F"\n\n'
            b'--F\nContent-Type: text/plain; charset=utf-8\n\nsee below\n'
            b'--F\nContent-Type: message/rfc822\n\n'
            b'From: orig@c\nSubject: Original\nContent-Type: text/html; charset=utf-8\n\n<p>inner</p>\n'
            b'--F--\n'
        )
        _connection, _message, html = self._open(raw)
        self.assertIn("see below", html)
        self.assertIn("mail-forward", html)
        self.assertIn("Original", html)
        self.assertIn("<p>inner</p>", html)

    def test_empty_message_gets_placeholder(self):
        _connection, _message, html = self._open(b"From: a@b\nSubject: x\nContent-Type: application/octet-stream\n\n")
        self.assertIn("这封邮件没有正文", html)

    def test_parse_fetch_handles_uid_after_literal(self):
        data = [(b"3 (BODY[HEADER.FIELDS (SUBJECT)] {10}", b"Subject: x"), b" UID 42)"]
        self.assertEqual(imap_reader._parse_fetch(data), [("42", b"Subject: x")])

    def test_unknown_account_raises_mailbox_error(self):
        with patch.object(imap_reader.config.Config, "connection_values",
                          side_effect=imap_reader.config.AccountNotFound("x")):
            with self.assertRaisesRegex(imap_reader.MailboxError, "不存在"):
                imap_reader.InboxReader().list_messages("deadbeef")


if __name__ == "__main__":
    unittest.main()
