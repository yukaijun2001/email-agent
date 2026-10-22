import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from scripts.config import AccountNotFound, Config, parse_graph_import


class ConfigTests(unittest.TestCase):
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

    def test_accounts_are_persisted_without_exposing_passwords(self):
        first = Config.add_account("imap.test", 993, "user@test", "secret")
        second = Config.add_account("imap.other", 995, "other@test", "secret2")
        snapshot = Config.snapshot()
        self.assertTrue(snapshot["configured"])
        self.assertEqual([a["id"] for a in snapshot["accounts"]], [first["id"], second["id"]])
        for account in snapshot["accounts"]:
            self.assertNotIn("password", account)
        self.assertNotIn("secret", json.dumps(snapshot))
        self.assertEqual(snapshot["accounts"][0]["account"], "user@test")
        self.assertNotIn("label", snapshot["accounts"][0])
        stored = json.loads(self.path.read_text())["accounts"]
        self.assertEqual([a["password"] for a in stored], ["secret", "secret2"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_update_keeps_password_when_blank(self):
        created = Config.add_account("imap.test", 993, "user@test", "secret")
        Config.update_account(created["id"], "imap.changed", 995, "user@test", "")
        self.assertEqual(Config.connection_values(created["id"]), ("imap.changed", 995, "user@test", "secret"))

    def test_add_requires_password_and_rejects_duplicates(self):
        with self.assertRaisesRegex(ValueError, "授权码"):
            Config.add_account("imap.test", 993, "user@test", "")
        Config.add_account("imap.test", 993, "user@test", "secret")
        with self.assertRaisesRegex(ValueError, "已添加"):
            Config.add_account("IMAP.test", 993, "USER@test", "secret")

    def test_remove_and_unknown_ids(self):
        created = Config.add_account("imap.test", 993, "user@test", "secret")
        Config.remove_account(created["id"])
        self.assertEqual(Config.snapshot(), {"configured": False, "accounts": []})
        with self.assertRaises(AccountNotFound):
            Config.connection_values(created["id"])
        with self.assertRaises(AccountNotFound):
            Config.update_account("deadbeef", "imap.test", 993, "u", "p")

    def test_legacy_single_mailbox_file_is_migrated(self):
        self.path.write_text(json.dumps(
            {"imap_server": "imap.legacy", "imap_port": 993, "account": "old@test", "password": "pw"}
        ))
        snapshot = Config.snapshot()
        self.assertEqual(len(snapshot["accounts"]), 1)
        account_id = snapshot["accounts"][0]["id"]
        self.assertEqual(Config.connection_values(account_id), ("imap.legacy", 993, "old@test", "pw"))
        rewritten = json.loads(self.path.read_text())
        self.assertEqual(rewritten["accounts"][0]["id"], account_id)

    def test_graph_import_keeps_secrets_off_the_snapshot_and_rotates_tokens(self):
        parsed, errors = parse_graph_import(
            "\n".join([
                "one@outlook.com----pass-word----11111111-2222-3333-4444-555555555555----refresh.token.value",
                "two@outlook.com--secret--clientid02--refresh-token",
                "not-an-email----x----clientid03----token",
                "",
            ])
        )
        self.assertEqual(errors, ["第 3 行邮箱格式无效。"])
        self.assertEqual(parsed[0]["client_id"], "11111111-2222-3333-4444-555555555555")
        self.assertEqual(parsed[0]["password"], "pass-word")
        self.assertEqual(parsed[1]["refresh_token"], "refresh-token")
        added = Config.import_graph_accounts(parsed[0]["account"] + "----" + parsed[0]["password"] + "----" + parsed[0]["client_id"] + "----" + parsed[0]["refresh_token"])
        account = added["added"][0]
        self.assertEqual(account["protocol"], "graph")
        self.assertNotIn("refresh_token", account)
        self.assertNotIn("password", account)
        self.assertNotIn("refresh.token", json.dumps(Config.snapshot()))
        stored = json.loads(self.path.read_text())["accounts"][0]
        self.assertEqual(stored["password"], "pass-word")
        self.assertEqual(stored["refresh_token"], "refresh.token.value")
        Config.update_account(account["id"], "", 443, "one@outlook.com", "", "graph", stored["client_id"], "")
        self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "refresh.token.value")
        Config.replace_refresh_token(account["id"], "refresh.token.value", "rotated-token")
        self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "rotated-token")
        Config.replace_refresh_token(account["id"], "stale-token", "ignored-token")
        self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "rotated-token")

    def test_legacy_outlook_migrates_to_graph_and_can_be_edited_without_password(self):
        self.path.write_text(json.dumps({"accounts": [{
            "id": "1234abcd", "protocol": "outlook_imap",
            "imap_server": "outlook.office365.com", "imap_port": 993,
            "account": "old@outlook.com", "password": "",
            "client_id": "client-id-1", "refresh_token": "legacy-token",
        }]}))
        account = Config.snapshot()["accounts"][0]
        self.assertEqual(account["id"], "1234abcd")
        self.assertEqual(account["protocol"], "graph")
        self.assertTrue(account["configured"])
        persisted = json.loads(self.path.read_text())["accounts"][0]
        self.assertEqual(persisted["protocol"], "graph")
        self.assertEqual((persisted["imap_server"], persisted["imap_port"]), ("graph.microsoft.com", 443))
        self.assertEqual(persisted["refresh_token"], "legacy-token")
        Config.update_account(account["id"], "", 443, account["account"], "",
                              "outlook_imap", "client-id-2", "")
        self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "legacy-token")
        duplicate = Config.import_outlook_accounts("old@outlook.com----unused----client-id-2----token")
        self.assertEqual(duplicate["added"], [])
        self.assertIn("已添加", duplicate["errors"][0]["message"])

    def test_postgres_legacy_rows_use_graph_and_reject_duplicate_imports(self):
        row = ("1234abcd", "outlook_imap", "outlook.office365.com", 993,
               "old@outlook.com", "", "client-id-1", "legacy-token")
        normalized = Config._account_from_row(row)
        self.assertEqual(normalized["protocol"], "graph")
        self.assertEqual((normalized["imap_server"], normalized["imap_port"]), ("graph.microsoft.com", 443))
        self.assertEqual(normalized["refresh_token"], "legacy-token")
        # Execute the store's SQL against a small relational fixture, including
        # the skip-id branch used when saving an existing account.
        with sqlite3.connect(":memory:") as db:
            db.execute("CREATE TABLE mail_accounts (id, protocol, imap_server, account)")
            db.execute("INSERT INTO mail_accounts VALUES (?, ?, ?, ?)",
                       (row[0], row[1], row[2], row[4]))
            cursor = db.cursor()
            connection = MagicMock()
            sql_cursor = connection.__enter__.return_value.cursor.return_value.__enter__.return_value
            sql_cursor.execute.side_effect = lambda sql, args: cursor.execute(sql.replace("%s", "?"), args)
            sql_cursor.fetchone.side_effect = cursor.fetchone
            with patch.object(Config, "_uses_postgres", return_value=True), \
                 patch.object(Config, "_pg_connection", return_value=connection):
                for skip_id in (None, "deadbeef"):
                    with self.assertRaisesRegex(ValueError, "已添加"):
                        Config._ensure_unique_locked("graph.microsoft.com", "OLD@outlook.com", skip_id)
                Config._ensure_unique_locked("graph.microsoft.com", "old@outlook.com", row[0])
                Config._ensure_unique_locked("graph.microsoft.com", "new@outlook.com", None)

    def test_no_account_limit(self):
        for index in range(60):
            Config.add_account(f"imap{index}.test", 993, f"user{index}@test", "p")
        Config.reset_for_tests()  # Reload from disk.
        snapshot = Config.snapshot()
        self.assertEqual(len(snapshot["accounts"]), 60)
        self.assertEqual(len({a["id"] for a in snapshot["accounts"]}), 60)

    def test_outlook_preview_recognizes_all_separators_without_saving_secrets(self):
        app_id = "11111111-2222-3333-4444-555555555555"
        for separator in ("-", "--", "---", "----"):
            with self.subTest(separator=separator):
                text = separator.join(["user@outlook.com", "pass-word", app_id, "token--with-dashes"])
                result = Config.preview_outlook_accounts(text)
                self.assertEqual(result["errors"], [])
                self.assertEqual(result["accounts"], [{"line": 1, "account": "user@outlook.com", "client_id": app_id}])
                parsed, _ = parse_graph_import(text)
                self.assertEqual(parsed[0]["password"], "pass-word")
                self.assertEqual(parsed[0]["refresh_token"], "token--with-dashes")
        self.assertEqual(Config.snapshot()["accounts"], [])

    def test_outlook_import_preserves_line_numbers_and_allows_partial_success(self):
        text = "\n".join([
            "",
            "one@outlook.com----secret----clientid01----refresh.token",
            "not-an-email----secret----clientid02----refresh.token",
            "one@outlook.com----secret----clientid01----refresh.token",
            "two@outlook.com----secret----clientid02----refresh.token",
            "three@outlook.com----secret----short----refresh.token",
        ])
        preview = Config.preview_outlook_accounts(text)
        self.assertEqual([item["line"] for item in preview["errors"]], [3, 6])
        result = Config.import_outlook_accounts(text)
        self.assertEqual(result["added_lines"], [2, 5])
        self.assertEqual(sorted(item["line"] for item in result["errors"]), [3, 4, 6])
        for account in result["added"]:
            self.assertEqual(account["protocol"], "graph")
            self.assertNotIn("refresh_token", account)
            self.assertNotIn("password", account)
            self.assertEqual(Config.mailbox(account["id"])["refresh_token"], "refresh.token")
        Config.reset_for_tests()
        self.assertEqual(len(Config.snapshot()["accounts"]), 2)

    def test_outlook_import_rejects_empty_fields_and_empty_input(self):
        result = Config.preview_outlook_accounts("user@outlook.com--------clientid01----token")
        self.assertEqual(result["accounts"], [])
        self.assertEqual(len(result["errors"]), 1)
        with self.assertRaisesRegex(ValueError, "至少一行"):
            Config.import_outlook_accounts("\n  \n")


if __name__ == "__main__":
    unittest.main()
