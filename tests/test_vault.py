import json
import sqlite3
import unittest

from vault import Vault, verify_archive


class VaultTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.db.execute("CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL)")
        self.vault = Vault(self.db)

    def tearDown(self):
        self.db.close()

    def test_encrypted_roundtrip_and_aad(self):
        master, recovery = self.vault.setup("test-password-123")
        payload = {"username": "门店账号", "password": "secret-测试", "url": "http://local.test"}
        encrypted = self.vault.encrypt(master, payload, "credential-1")
        self.assertNotIn(payload["password"], encrypted)
        self.assertEqual(payload, self.vault.decrypt(master, encrypted, "credential-1"))
        self.assertNotEqual(encrypted, self.vault.encrypt(master, payload, "credential-1"))
        with self.assertRaises(ValueError):
            self.vault.decrypt(master, encrypted, "credential-2")
        corrupted = json.loads(encrypted)
        corrupted["ciphertext"] = "AA" + corrupted["ciphertext"][2:]
        with self.assertRaises(ValueError):
            self.vault.decrypt(master, json.dumps(corrupted), "credential-1")
        settings = self.db.execute("SELECT value FROM settings").fetchone()[0]
        self.assertNotIn("test-password-123", settings)
        self.assertNotIn(recovery, settings)
        self.assertNotIn(master.hex(), settings)

    def test_password_change_and_recovery_preserve_credentials(self):
        master, recovery = self.vault.setup("old-password-123")
        encrypted = self.vault.encrypt(master, {"password": "门店密码"}, "record")
        with self.assertRaises(ValueError):
            self.vault.unlock("wrong-password")
        with self.assertRaises(ValueError):
            self.vault.change_password(master, "wrong-password", "new-password-123")
        self.vault.change_password(master, "old-password-123", "new-password-123")
        self.assertEqual(master, self.vault.unlock("new-password-123"))
        with self.assertRaises(ValueError):
            self.vault.unlock("old-password-123")
        recovered, new_recovery = self.vault.recover(recovery, "recovered-password-123")
        self.assertEqual(master, recovered)
        self.assertNotEqual(recovery, new_recovery)
        self.assertEqual({"password": "门店密码"}, self.vault.decrypt(recovered, encrypted, "record"))
        with self.assertRaises(ValueError):
            self.vault.unlock_recovery(recovery)
        self.assertEqual(master, self.vault.unlock_recovery(new_recovery))
        self.assertEqual(master, verify_archive(self.db, password="recovered-password-123"))
        self.assertEqual(master, verify_archive(self.db, recovery_key=new_recovery))

    def test_setup_validation_does_not_overwrite(self):
        self.assertFalse(self.vault.initialized)
        for password in ("", "1234567", None, 12345678):
            with self.assertRaises(ValueError):
                self.vault.setup(password)
        self.assertFalse(self.vault.initialized)
        master, _ = self.vault.setup("correct-password")
        with self.assertRaises(ValueError):
            self.vault.setup("replacement-password")
        self.assertEqual(master, self.vault.unlock("correct-password"))


if __name__ == "__main__":
    unittest.main()
