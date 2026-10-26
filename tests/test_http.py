"""End-to-end HTTP tests use disposable isolated ledgers, never the app data."""
from __future__ import annotations

import io
import json
from pathlib import Path
import shutil
import time
import unittest
from unittest.mock import patch
import uuid
import zipfile

from aiohttp import CookieJar, FormData
from aiohttp.test_utils import TestClient, TestServer

from server import LedgerServer


class HTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.parent = Path(__file__).resolve().parents[1] / ".tmp"
        self.parent.mkdir(exist_ok=True)
        self.root = self.parent / ("http-" + uuid.uuid4().hex)
        self.root.mkdir()
        self.instance = LedgerServer(self.root)
        self.client = TestClient(TestServer(self.instance.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()
        response = await self.client.get("/api/session?profile=live")
        self.csrf = (await response.json())["csrf"]

    async def asyncTearDown(self):
        await self.client.close()
        if self.root.resolve().is_relative_to(self.parent.resolve()):
            shutil.rmtree(self.root)

    async def post(self, route, data, profile="live"):
        return await self.client.post(route + "?profile=" + profile, json=data, headers={"X-CSRF-Token": self.csrf})

    async def setup_live(self):
        response = await self.post("/api/auth/setup", {"password": "test-password-123"})
        self.assertEqual(response.status, 200, await response.text())
        return (await response.json())["recovery_key"]

    async def command(self, action, data, profile="live"):
        response = await self.post("/api/action", {"action": action, "data": data, "request_id": str(uuid.uuid4())}, profile)
        self.assertEqual(response.status, 200, await response.text())
        return await response.json()

    async def make_store(self):
        return (await self.command("store.save", {"name": "HTTP 验收店", "notes": "<script>fake</script>"}))["id"]

    async def upload(self, owner_id, content=b"first attachment", slot="地图", profile="live"):
        data = FormData()
        for key, value in {"owner_type": "store", "owner_id": owner_id, "slot": slot, "notes": "测试文件"}.items():
            data.add_field(key, value)
        data.add_field("file", content, filename="map.json", content_type="application/json")
        return await self.client.post("/api/attachments?profile=" + profile, data=data, headers={"X-CSRF-Token": self.csrf})

    async def restore(self, content, password="test-password-123", profile="live", recovery_key=None):
        data = FormData()
        if password:
            data.add_field("password", password)
        if recovery_key:
            data.add_field("recovery_key", recovery_key)
        data.add_field("file", content, filename="backup.zip", content_type="application/zip")
        return await self.client.post("/api/restore?profile=" + profile, data=data, headers={"X-CSRF-Token": self.csrf})

    async def test_profiles_auth_ciphertext_and_expiry(self):
        state = await self.client.get("/api/state?profile=live")
        self.assertEqual(state.status, 401)
        demo = await (await self.client.get("/api/state?profile=demo")).json()
        self.assertGreater(len(demo["stores"]), 0)
        self.assertNotIn("demo-wifi-2026", json.dumps(demo))
        recovery = await self.setup_live()
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(state["stores"], [])
        ident = await self.make_store()
        response = await self.post("/api/credentials", {"store_id": ident, "label": "路由器", "username": "hidden-admin", "password": "  secret-value-http  ", "url": "192.168.1.1", "notes": "private note"})
        credential = (await response.json())["id"]
        response = await self.client.get("/api/state?profile=live")
        raw = await response.text()
        self.assertNotIn("secret-value-http", raw)
        self.assertNotIn("hidden-admin", raw)
        self.assertNotIn(b"secret-value-http", (self.root / "live" / "ledger.sqlite3").read_bytes())
        revealed = await (await self.post("/api/credentials/reveal", {"id": credential})).json()
        self.assertEqual(revealed["password"], "  secret-value-http  ")
        await self.post("/api/auth/lock", {})
        self.assertEqual((await self.post("/api/credentials/reveal", {"id": credential})).status, 401)
        self.assertEqual((await self.client.get("/api/backup?profile=live")).status, 401)
        self.assertEqual((await self.post("/api/auth/unlock", {"password": "incorrect-password"})).status, 400)
        self.assertEqual((await self.post("/api/auth/unlock", {"password": "test-password-123"})).status, 200)
        for session in self.instance.sessions.values():
            if "live" in session["keys"]:
                session["keys"]["live"] = (session["keys"]["live"][0], time.monotonic() - 1801)
        self.assertEqual((await self.client.get("/api/state?profile=live")).status, 401)
        response = await self.post("/api/auth/recover", {"recovery_key": recovery, "password": "new-test-password"})
        self.assertEqual(response.status, 200)
        self.assertNotEqual((await response.json())["recovery_key"], recovery)
        self.assertEqual((await (await self.post("/api/credentials/reveal", {"id": credential})).json())["password"], "  secret-value-http  ")

    async def test_same_named_ledger_attachments_and_restore_limit(self):
        from archive import MAX_ARCHIVE_BYTES
        from server import RESTORE_LIMIT
        self.assertGreaterEqual(RESTORE_LIMIT, MAX_ARCHIVE_BYTES)
        await self.setup_live()
        ledger = await self.command("ledger.save", {"direction": "expense", "date": "2026-10-25", "category": "研发耗材", "amount_cents": 100, "notes": "附件测试"})
        ids = []
        for content in (b"first", b"second"):
            data = FormData()
            data.add_field("owner_type", "ledger")
            data.add_field("owner_id", ledger["id"])
            data.add_field("file", content, filename="screenshot.png")
            response = await self.client.post("/api/attachments?profile=live", data=data, headers={"X-CSRF-Token": self.csrf})
            self.assertEqual(response.status, 200, await response.text())
            ids.append((await response.json())["id"])
        self.assertNotEqual(ids[0], ids[1])

    async def test_attachment_upload_retry_is_idempotent(self):
        await self.setup_live()
        store_id = await self.make_store()
        token = str(uuid.uuid4())
        received = []
        for content in (b"same", b"same", b"different"):
            data = FormData()
            for key, value in {"owner_type": "store", "owner_id": store_id, "slot": "地图", "upload_id": token}.items():
                data.add_field(key, value)
            data.add_field("file", content, filename="map.json")
            response = await self.client.post("/api/attachments?profile=live", data=data, headers={"X-CSRF-Token": self.csrf})
            if content == b"different":
                self.assertEqual(response.status, 400)
            else:
                self.assertEqual(response.status, 200, await response.text())
                received.append(await response.json())
        self.assertEqual(received[0], received[1])
        self.assertEqual(len(list((self.root / "live" / "attachments").iterdir())), 1)

    async def test_simple_cash_forms_and_intake_have_no_financial_side_effect(self):
        await self.setup_live()
        batch = await self.command("batch.create", {"kind": "pcba", "date": "2026-10-25", "serials": ["0001", "0002"], "notes": "扫码入库"})
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(len(state["ledger"]), 0)
        self.assertEqual(state["batches"][0]["supplier"], "")
        self.assertEqual(state["batches"][0]["cost_cents"], 0)
        self.assertEqual(state["batches"][0]["quantity"], 2)
        for direction, category in (("income", "甲方回款"), ("expense", "PCBA采购")):
            await self.command("ledger.save", {"direction": direction, "category": category,
                "amount_cents": 1050, "date": "2026-10-25", "notes": "只填备注，不填旧字段"})
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(len(state["ledger"]), 2)
        for row in state["ledger"]:
            self.assertEqual(row["purpose"], "")
            self.assertEqual(row["counterparty"], "")
            self.assertIsNone(row["batch_id"])
            self.assertIsNone(row["store_id"])
        response = await self.post("/api/action", {"action": "batch.create", "request_id": str(uuid.uuid4()),
            "data": {"kind": "frame", "date": "2026-10-25", "serials": ["frame-rejected"], "paid_cents": 100}})
        self.assertEqual(response.status, 400, await response.text())
        response = await self.post("/api/action", {"action": "ledger.save", "request_id": str(uuid.uuid4()),
            "data": {"direction": "income", "category": "吃饭", "date": "2026-10-25", "amount_cents": 100}})
        self.assertEqual(response.status, 400, await response.text())
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(len(state["components"]), 2)
        self.assertEqual(len(state["ledger"]), 2)

    async def test_editing_legacy_cash_retains_hidden_history(self):
        await self.setup_live()
        store_id = await self.make_store()
        batch = await self.command("batch.create", {"kind": "frame", "date": "2026-10-25", "serials": ["old-frame"]})
        conn = self.instance.stores["live"].db
        with conn:
            conn.execute("INSERT INTO ledger(id,direction,date,category,amount_cents,counterparty,purpose,store_id,batch_id,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                ("legacy", "expense", "2026-10-25", "原版研发分类", 500, "历史往来方", "历史摘要", store_id, batch["id"], "原备注", "2026-10-25"))
        await self.command("ledger.save", {"id": "legacy", "direction": "expense", "date": "2026-10-25",
            "category": "原版研发分类", "amount_cents": 600, "notes": "编辑后的备注"})
        row = dict(conn.execute("SELECT * FROM ledger WHERE id='legacy'").fetchone())
        self.assertEqual(row["counterparty"], "历史往来方")
        self.assertEqual(row["purpose"], "历史摘要")
        self.assertEqual(row["store_id"], store_id)
        self.assertEqual(row["batch_id"], batch["id"])
        self.assertEqual(row["amount_cents"], 600)
        self.assertEqual(row["notes"], "编辑后的备注")

    async def test_store_document_is_private_idempotent_and_preserves_legacy(self):
        from store import STORE_DOCUMENT_PREFIX
        await self.setup_live()
        store_id = await self.make_store()
        route = f"/api/stores/{store_id}/document"
        response = await self.client.get(route + "?profile=live")
        fresh = await response.json()
        self.assertEqual(fresh["text"], "")
        self.assertFalse(fresh["exists"])
        legacy = await self.post("/api/credentials", {"store_id": store_id, "label": "旧版路由器", "username": "old-admin", "password": "legacy-private-test", "notes": "保留旧资料"})
        legacy_id = (await legacy.json())["id"]
        imported = await (await self.client.get(route + "?profile=live")).json()
        self.assertTrue(imported["legacy_imported"])
        self.assertIn("legacy-private-test", imported["text"])
        document = "\n  自由编排的账号备忘\n密码：v12-private-document-value  \n\n"
        payload = {"text": document, "request_id": str(uuid.uuid4())}
        first = await self.post(route, payload)
        self.assertEqual(first.status, 200, await first.text())
        self.assertEqual(await (await self.post(route, payload)).json(), await first.json())
        ident = STORE_DOCUMENT_PREFIX + store_id
        revealed = await (await self.client.get(route + "?profile=live")).json()
        self.assertEqual(revealed["text"], document)
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertTrue(state["stores"][0]["has_document"])
        self.assertEqual([c["id"] for c in state["credentials"]], [legacy_id])
        self.assertNotIn("v12-private-document-value", json.dumps(state, ensure_ascii=False))
        self.assertNotIn(b"v12-private-document-value", (self.root / "live/ledger.sqlite3").read_bytes())
        listed = await (await self.client.get(f"/api/credentials?profile=live&store_id={store_id}")).json()
        self.assertEqual([c["id"] for c in listed["credentials"]], [legacy_id])
        for endpoint in ("/api/credentials/reveal", "/api/credentials/delete"):
            self.assertEqual((await self.post(endpoint, {"id": ident})).status, 400)
        self.assertEqual((await self.post("/api/credentials", {"id": ident, "store_id": store_id, "label": "不能覆盖", "password": "overwrite"})).status, 400)
        self.assertEqual((await self.post(route, {"text": "different", "request_id": payload["request_id"]})).status, 400)
        await self.post("/api/auth/lock", {})
        self.assertEqual((await self.client.get(route + "?profile=live")).status, 401)
        self.assertEqual((await self.post(route, {"text": "locked", "request_id": str(uuid.uuid4())})).status, 401)
        await self.post("/api/auth/unlock", {"password": "test-password-123"})
        self.assertEqual((await self.post(route, {"text": "", "request_id": str(uuid.uuid4())})).status, 200)
        cleared = await (await self.client.get(route + "?profile=live")).json()
        self.assertTrue(cleared["exists"])
        self.assertEqual(cleared["text"], "")
        self.assertFalse(cleared["legacy_imported"])
        self.assertEqual((await (await self.post("/api/credentials/reveal", {"id": legacy_id})).json())["password"], "legacy-private-test")

    async def test_document_backup_recovery_and_store_ownership(self):
        from store import STORE_DOCUMENT_PREFIX
        recovery = await self.setup_live()
        store_id = await self.make_store()
        second_id = (await self.command("store.save", {"name": "第二门店", "code": "00009"}))["id"]
        route = f"/api/stores/{store_id}/document"
        original = "备忘中的测试密码：private-backup-document"
        response = await self.post(route, {"text": original, "request_id": str(uuid.uuid4()), "store_id": second_id})
        self.assertEqual(response.status, 200)
        self.assertFalse((await (await self.client.get(f"/api/stores/{second_id}/document?profile=live")).json())["exists"])
        response = await self.client.get("/api/backup?profile=live")
        archive = await response.read()
        with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
            self.assertNotIn(b"private-backup-document", zipped.read("ledger.sqlite3"))
        await self.post(route, {"text": "后续内容", "request_id": str(uuid.uuid4())})
        self.assertEqual((await self.restore(archive)).status, 200)
        self.assertEqual((await self.client.get(route + "?profile=live")).status, 401)
        await self.post("/api/auth/recover", {"recovery_key": recovery, "password": "recovered-document-password"})
        self.assertEqual((await (await self.client.get(route + "?profile=live")).json())["text"], original)
        conn = self.instance.stores["live"].db
        with conn:
            conn.execute("UPDATE credentials SET store_id=? WHERE id=?", (second_id, STORE_DOCUMENT_PREFIX + store_id))
        self.assertEqual((await self.client.get(route + "?profile=live")).status, 400)
        self.assertEqual((await self.post(route, {"text": "不能误写另一门店", "request_id": str(uuid.uuid4())})).status, 400)

    async def test_document_limit_fits_encrypted_payload_for_multibyte_text(self):
        from server import DOCUMENT_LIMIT
        await self.setup_live()
        store_id = await self.make_store()
        route = f"/api/stores/{store_id}/document"
        for content in ("汉" * DOCUMENT_LIMIT, "\x01" * DOCUMENT_LIMIT):
            response = await self.post(route, {"text": content, "request_id": str(uuid.uuid4())})
            self.assertEqual(response.status, 200, await response.text())
            self.assertEqual((await (await self.client.get(route + "?profile=live")).json())["text"], content)
        response = await self.post(route, {"text": "汉" * (DOCUMENT_LIMIT + 1), "request_id": str(uuid.uuid4())})
        self.assertEqual(response.status, 400)

    async def test_attachment_replace_failure_preserves_old_and_locked_download(self):
        await self.setup_live()
        store_id = await self.make_store()
        response = await self.upload(store_id)
        self.assertEqual(response.status, 200, await response.text())
        ident = (await response.json())["id"]
        response = await self.upload(store_id, b"second")
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await response.json())["id"], ident)
        self.assertEqual(len(list((self.root / "live" / "attachments").iterdir())), 1)
        with patch("server.os.replace", side_effect=OSError("simulated full disk")):
            response = await self.upload(store_id, b"must never replace")
            self.assertEqual(response.status, 500)
        response = await self.client.get(f"/api/attachments/{ident}?profile=live")
        self.assertEqual(await response.read(), b"second")
        self.assertIn("attachment;", response.headers["Content-Disposition"])
        await self.post("/api/auth/lock", {})
        self.assertEqual((await self.client.get(f"/api/attachments/{ident}?profile=live")).status, 401)

    async def test_backup_restore_is_complete_and_wrong_import_does_not_replace(self):
        await self.setup_live()
        store_id = await self.make_store()
        await self.upload(store_id, b"backup-file")
        credential_response = await self.post("/api/credentials", {"store_id": store_id, "label": "test", "password": "backup-secret"})
        credential_id = (await credential_response.json())["id"]
        response = await self.client.get("/api/backup?profile=live")
        self.assertEqual(response.status, 200, await response.text() if response.status != 200 else "")
        backup = await response.read()
        with zipfile.ZipFile(io.BytesIO(backup)) as archive:
            self.assertNotIn(b"backup-secret", archive.read("ledger.sqlite3"))
        await self.command("store.save", {"name": "Later store"})
        response = await self.restore(backup, password="wrong-password")
        self.assertEqual(response.status, 400)
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(len(state["stores"]), 2)
        response = await self.restore(b"corrupt")
        self.assertEqual(response.status, 400)
        self.assertEqual(len((await (await self.client.get("/api/state?profile=live")).json())["stores"]), 2)
        response = await self.restore(backup)
        self.assertEqual(response.status, 200, await response.text())
        self.assertEqual((await self.client.get("/api/state?profile=live")).status, 401)
        await self.post("/api/auth/unlock", {"password": "test-password-123"})
        state = await (await self.client.get("/api/state?profile=live")).json()
        self.assertEqual(len(state["stores"]), 1)
        attachment_id = state["attachments"][0]["id"]
        self.assertEqual(await (await self.client.get(f"/api/attachments/{attachment_id}?profile=live")).read(), b"backup-file")
        self.assertEqual((await (await self.post("/api/credentials/reveal", {"id": credential_id})).json())["password"], "backup-secret")

    async def test_origin_host_csrf_and_path_guards(self):
        self.assertEqual((await self.client.get("/api/session", headers={"Host": "evil.example"})).status, 403)
        self.assertEqual((await self.client.get("/api/session", headers={"Origin": "https://evil.example"})).status, 403)
        self.assertEqual((await self.client.post("/api/auth/setup?profile=live", json={"password": "unauthorized-change"})).status, 403)
        self.assertFalse(self.instance.stores["live"].db.execute("SELECT 1 FROM settings WHERE key='vault.v1'").fetchone())
        self.assertEqual((await self.client.get("/data/live/ledger.sqlite3")).status, 404)
        self.assertEqual((await self.client.get("/../server.py")).status, 404)
        asset = await self.client.get("/vendor/pdfjs/pdf.mjs")
        self.assertEqual(asset.status, 200)
        self.assertIn("javascript", asset.headers["Content-Type"])
        self.assertEqual((await self.client.get("/vendor/pdfjs/../../server.py")).status, 404)


if __name__ == "__main__":
    unittest.main()
