import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.config import AccountNotFound, Config


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

    def test_no_account_limit(self):
        for index in range(60):
            Config.add_account(f"imap{index}.test", 993, f"user{index}@test", "p")
        Config.reset_for_tests()  # Reload from disk.
        snapshot = Config.snapshot()
        self.assertEqual(len(snapshot["accounts"]), 60)
        self.assertEqual(len({a["id"] for a in snapshot["accounts"]}), 60)


if __name__ == "__main__":
    unittest.main()
