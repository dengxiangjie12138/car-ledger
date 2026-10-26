from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import shutil
from types import SimpleNamespace
import unittest
from unittest import mock
import uuid
import zipfile

from archive import create_backup, validate_backup
import archive as ledger_archive
from store import SCHEMA
from vault import Vault


DDL = "PRAGMA user_version=1; PRAGMA foreign_keys=ON;" + SCHEMA


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parent / ".runs" / ("archive-" + uuid.uuid4().hex)
        self.root.mkdir(parents=True)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "attachments").mkdir()
        self.conn = sqlite3.connect(self.source / "ledger.sqlite3")
        self.conn.executescript(DDL)
        self.conn.execute("INSERT INTO stores(id,name,created_at) VALUES ('store1','示例门店','2026-10-25')")
        self.conn.execute("INSERT INTO ledger(id,direction,date,category,amount_cents,counterparty,purpose,created_at) VALUES ('entry1','expense','2026-10-25','研发',12345,'商家','开发采购','2026-10-25')")
        self.attachment = b"demo receipt bytes\x00\xff"
        (self.source / "attachments" / "random-id.bin").write_bytes(self.attachment)
        (self.source / "attachments" / "unreferenced.bin").write_text("excluded")
        self.conn.execute(
            "INSERT INTO attachments VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("file1", "store", "store1", "install", "原始安装图.bin", "application/octet-stream", len(self.attachment), "random-id.bin", "", "2026-10-25"),
        )
        self.conn.commit()
        self.store = SimpleNamespace(db=self.conn)
        self.backup = self.root / "backup.zip"
        self.stage = self.root / "stage"

    def tearDown(self):
        self.conn.close()
        base = (Path(__file__).resolve().parent / ".runs").resolve()
        target = self.root.resolve()
        if target.parent == base and target.name.startswith("archive-"):
            shutil.rmtree(target)

    def _rewrite(self, transform):
        with zipfile.ZipFile(self.backup) as archive:
            entries = {name: archive.read(name) for name in archive.namelist()}
        transform(entries)
        with zipfile.ZipFile(self.backup, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, raw in entries.items():
                archive.writestr(name, raw)

    def test_roundtrip_only_valid_attachments_and_snapshot(self):
        create_backup(self.store, self.backup, "demo")
        with zipfile.ZipFile(self.backup) as archive:
            self.assertEqual({"manifest.json", "ledger.sqlite3", "attachments/random-id.bin"}, set(archive.namelist()))
        restored = validate_backup(self.backup, self.stage, "demo")
        self.assertEqual(self.attachment, (restored / "attachments" / "random-id.bin").read_bytes())
        with closing(sqlite3.connect(restored / "ledger.sqlite3")) as db:
            self.assertEqual(12345, db.execute("SELECT amount_cents FROM ledger").fetchone()[0])

    def test_live_requires_unlock_and_checks_record_ciphertext(self):
        vault = Vault(self.conn)
        master, recovery = vault.setup("archive-password-123")
        payload = Vault.encrypt(master, {"username": "user", "password": "very-private"}, "cred1")
        self.conn.execute("INSERT INTO credentials VALUES (?,?,?,?,?)", ("cred1", "store1", "远控", payload, "2026-10-25"))
        self.conn.commit()
        create_backup(self.store, self.backup, "live")
        with self.assertRaises(ValueError):
            validate_backup(self.backup, self.stage, "live", password="wrong-password")
        self.assertEqual([], list(self.stage.iterdir()))
        restored = validate_backup(self.backup, self.stage, "live", recovery_key=recovery)
        self.assertTrue((restored / "ledger.sqlite3").is_file())
        with zipfile.ZipFile(self.backup) as archive:
            self.assertNotIn(b"very-private", archive.read("ledger.sqlite3"))
            self.assertNotIn(recovery.encode(), archive.read("ledger.sqlite3"))

    def test_mixed_profiles_rejected(self):
        create_backup(self.store, self.backup, "demo")
        with self.assertRaisesRegex(ValueError, "不能混用"):
            validate_backup(self.backup, self.stage, "live", password="anything123")
        self.assertEqual([], list(self.stage.iterdir()))

    def test_tampered_attachment_preserves_source_and_cleans_staging(self):
        create_backup(self.store, self.backup, "demo")
        self._rewrite(lambda files: files.__setitem__("attachments/random-id.bin", b"tampered"))
        with self.assertRaises(ValueError):
            validate_backup(self.backup, self.stage, "demo")
        self.assertEqual([], list(self.stage.iterdir()))
        self.assertEqual(self.attachment, (self.source / "attachments" / "random-id.bin").read_bytes())
        self.assertEqual(12345, self.conn.execute("SELECT amount_cents FROM ledger").fetchone()[0])

    def test_traversal_extra_file_and_missing_reference_rejected(self):
        for illegal in ("../escaped.txt", "attachments/../../escaped.txt", "attachments\\escaped.txt", "other.txt"):
            create_backup(self.store, self.backup, "demo")
            self._rewrite(lambda files, name=illegal: files.__setitem__(name, b"bad"))
            with self.assertRaises(ValueError):
                validate_backup(self.backup, self.stage, "demo")
        self.assertFalse((self.root / "escaped.txt").exists())
        create_backup(self.store, self.backup, "demo")
        def remove_reference(files):
            del files["attachments/random-id.bin"]
            manifest = json.loads(files["manifest.json"])
            del manifest["files"]["attachments/random-id.bin"]
            files["manifest.json"] = json.dumps(manifest).encode()
        self._rewrite(remove_reference)
        with self.assertRaisesRegex(ValueError, "附件与数据库"):
            validate_backup(self.backup, self.stage, "demo")

    def test_missing_attachment_does_not_overwrite_prior_backup(self):
        self.backup.write_bytes(b"previous backup")
        (self.source / "attachments" / "random-id.bin").unlink()
        with self.assertRaises(ValueError):
            create_backup(self.store, self.backup, "demo")
        self.assertEqual(b"previous backup", self.backup.read_bytes())

    def test_physical_zip_size_limit_preserves_existing_backup(self):
        create_backup(self.store, self.backup, "demo")
        with zipfile.ZipFile(self.backup) as archive:
            cap = sum(info.file_size for info in archive.infolist())
        self.backup.write_bytes(b"previous backup")
        original_zip = zipfile.ZipFile
        def uncompressed_zip(*args, **kwargs):
            kwargs["compression"] = zipfile.ZIP_STORED
            return original_zip(*args, **kwargs)
        with mock.patch.object(ledger_archive, "MAX_ARCHIVE_BYTES", cap), mock.patch.object(ledger_archive.zipfile, "ZipFile", uncompressed_zip):
            with self.assertRaisesRegex(ValueError, "备份包超过"):
                create_backup(self.store, self.backup, "demo")
        self.assertEqual(b"previous backup", self.backup.read_bytes())
        self.assertFalse(any(p.name.startswith("backup-") for p in self.root.iterdir()))

    def test_uncompressed_limit_includes_manifest(self):
        self.backup.write_bytes(b"previous backup")
        raw_size = (self.source / "ledger.sqlite3").stat().st_size + len(self.attachment)
        with mock.patch.object(ledger_archive, "MAX_ARCHIVE_BYTES", raw_size + 1):
            with self.assertRaisesRegex(ValueError, "备份清单超过"):
                create_backup(self.store, self.backup, "demo")
        self.assertEqual(b"previous backup", self.backup.read_bytes())

    def test_highly_compressible_attachment_roundtrips(self):
        raw = b"\0" * (8 * 1024 * 1024)
        (self.source / "attachments" / "random-id.bin").write_bytes(raw)
        self.conn.execute("UPDATE attachments SET size=?", (len(raw),))
        self.conn.commit()
        create_backup(self.store, self.backup, "demo")
        with zipfile.ZipFile(self.backup) as archive:
            info = archive.getinfo("attachments/random-id.bin")
            self.assertGreater(info.file_size / info.compress_size, 1000)
        restored = validate_backup(self.backup, self.stage, "demo")
        self.assertEqual(hashlib.sha256(raw).hexdigest(), hashlib.sha256((restored / "attachments" / "random-id.bin").read_bytes()).hexdigest())

    def test_all_business_tables_and_action_columns_are_required(self):
        mutations = [
            "DROP TABLE batches", "DROP TABLE shipments", "DROP TABLE shipment_items",
            "DROP TABLE movements", "DROP TABLE history", "DROP TABLE requests",
            "ALTER TABLE shipments DROP COLUMN notes",
            "ALTER TABLE shipment_items DROP COLUMN after_json",
            "ALTER TABLE movements DROP COLUMN created_at",
            "ALTER TABLE history DROP COLUMN notes",
            "ALTER TABLE vehicles DROP COLUMN revision",
            "ALTER TABLE requests DROP COLUMN result_json",
        ]
        for statement in mutations:
            with self.subTest(statement=statement):
                create_backup(self.store, self.backup, "demo")
                def alter_database(files):
                    db_file = self.root / "malformed.sqlite3"
                    db_file.write_bytes(files["ledger.sqlite3"])
                    with closing(sqlite3.connect(db_file)) as conn:
                        conn.execute(statement)
                        conn.commit()
                    files["ledger.sqlite3"] = db_file.read_bytes()
                    manifest = json.loads(files["manifest.json"])
                    manifest["files"]["ledger.sqlite3"] = {
                        "size": len(files["ledger.sqlite3"]),
                        "sha256": hashlib.sha256(files["ledger.sqlite3"]).hexdigest(),
                    }
                    files["manifest.json"] = json.dumps(manifest).encode()
                self._rewrite(alter_database)
                with self.assertRaises(ValueError):
                    validate_backup(self.backup, self.stage, "demo")
                self.assertEqual([], list(self.stage.iterdir()))


if __name__ == "__main__":
    unittest.main()
