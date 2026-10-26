"""Loopback-only local ledger. No vehicle, cloud or external account connections."""
from __future__ import annotations

import argparse
import asyncio
import base64
from contextlib import suppress
from datetime import datetime, timezone, timedelta
import hashlib
import hmac
import json
import logging
import mimetypes
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import tempfile
import time
import uuid

from aiohttp import web

from store import Store, STORE_DOCUMENT_PREFIX
from vault import Vault
from archive import create_backup, validate_backup

APP_ID = "car-ledger-local"
VERSION = "1.2.0"
ROOT = Path(__file__).resolve().parent
TZ = timezone(timedelta(hours=8))
DEMO_PASSWORD = "demo-ledger-public-v1"
FILE_LIMIT = 50 * 1024 * 1024
DOCUMENT_LIMIT = 40_000
RESTORE_LIMIT = 2 * 1024 * 1024 * 1024
INLINE_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "application/pdf"}
OWNER_TABLES = {"store": "stores", "ledger": "ledger", "batch": "batches", "vehicle": "vehicles", "shipment": "shipments"}
SESSION_KEY = web.RequestKey("ledger_session", dict)


def now():
    return datetime.now(TZ).isoformat(timespec="seconds")


def text_field(data, key, limit=2000, required=False):
    value = data.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key} 必须是文字")
    value = value.strip()
    if (required and not value) or len(value) > limit or "\x00" in value:
        raise ValueError("请填写有效内容，或缩短输入长度")
    return value


class LedgerServer:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.stores = {p: Store(self.data_dir / p) for p in ("demo", "live")}
        self.stores["demo"].seed_demo()
        self.demo_key = self._demo_vault()
        self.sessions = {}
        self.attempts = {}
        self.gate = asyncio.Lock()
        self.stop_event = asyncio.Event()
        self.app = web.Application(client_max_size=RESTORE_LIMIT + 1024 * 1024,
                                   middlewares=[self.security])
        self.app.router.add_get("/", self.static)
        self.app.router.add_get("/index.html", self.static)
        self.app.router.add_get("/app.js", self.static)
        self.app.router.add_get("/style.css", self.static)
        self.app.router.add_get("/vendor/pdfjs/{tail:.*}", self.vendor_asset)
        self.app.router.add_get("/health", self.health)
        self.app.router.add_get("/api/session", self.session_info)
        self.app.router.add_post("/api/auth/{operation}", self.auth)
        self.app.router.add_get("/api/state", self.state)
        self.app.router.add_post("/api/action", self.action)
        self.app.router.add_get("/api/stores/{store_id}/document", self.store_document_get)
        self.app.router.add_post("/api/stores/{store_id}/document", self.store_document_save)
        self.app.router.add_get("/api/credentials", self.credentials_list)
        self.app.router.add_post("/api/credentials", self.credentials_save)
        self.app.router.add_post("/api/credentials/reveal", self.credentials_reveal)
        self.app.router.add_post("/api/credentials/delete", self.credentials_delete)
        self.app.router.add_post("/api/attachments", self.attachment_upload)
        self.app.router.add_get("/api/attachments/{id}", self.attachment_get)
        self.app.router.add_post("/api/attachments/delete", self.attachment_delete)
        self.app.router.add_get("/api/backup", self.backup)
        self.app.router.add_post("/api/restore", self.restore)
        self.app.router.add_post("/api/shutdown", self.shutdown)
        self.app.on_cleanup.append(self.cleanup)
        self.app.on_startup.append(self.startup)
        self._seed_demo_assets()

    def _demo_vault(self):
        vault = Vault(self.stores["demo"].db)
        if not vault.initialized:
            key, _ = vault.setup(DEMO_PASSWORD)
            return key
        return vault.unlock(DEMO_PASSWORD)

    def _seed_demo_assets(self):
        store = self.stores["demo"]
        row = store.db.execute("SELECT id FROM stores ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return
        store_id = row[0]
        if not store.db.execute("SELECT 1 FROM credentials LIMIT 1").fetchone():
            for label, payload in [
                ("门店路由器（演示）", {"username": "admin-demo", "password": "demo-wifi-2026", "url": "192.168.10.1", "notes": "虚构示例，请替换为实际安装记录。"}),
                ("远程控制（演示）", {"username": "store-demo", "password": "demo-remote-only", "url": "", "notes": "仅展示账号备忘功能，不连接远程服务。"}),
            ]:
                ident = str(uuid.uuid4())
                cipher = Vault.encrypt(self.demo_key, payload, ident)
                store.db.execute("INSERT INTO credentials(id,store_id,label,payload,created_at) VALUES(?,?,?,?,?)", (ident, store_id, label, cipher, now()))
            store.db.commit()
        if not store.db.execute("SELECT 1 FROM attachments WHERE owner_type='store' LIMIT 1").fetchone():
            demo_image = ROOT / "static" / "demo-installation.png"
            image = demo_image.read_bytes() if demo_image.is_file() else base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aN3sAAAAASUVORK5CYII=")
            for slot, filename, mime, content in [
                ("地图参数", "map-demo.json", "application/json", json.dumps([{"card_id": 1, "table_id": None, "forward_id": 2}, {"card_id": 2, "table_id": 1, "forward_id": 1}], ensure_ascii=False, indent=2).encode()),
                ("安全池参数", "card_safety_pool-demo.json", "application/json", b'{"demo": true, "pools": []}'),
                ("安装照片示例", "installation-demo.png", "image/png", image),
            ]:
                ident = str(uuid.uuid4())
                storage = uuid.uuid4().hex
                path = self.data_dir / "demo" / "attachments" / storage
                path.write_bytes(content)
                store.db.execute("INSERT INTO attachments(id,owner_type,owner_id,slot,filename,mime,size,storage_name,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (ident, "store", store_id, slot, filename, mime, len(content), storage, "虚构演示资料，可替换。", now()))
            store.db.commit()

    def profile(self, request):
        profile = request.query.get("profile", "demo")
        if profile not in self.stores:
            raise ValueError("台账不存在")
        return profile

    def session(self, request):
        return request[SESSION_KEY]

    def require(self, request):
        profile = self.profile(request)
        session = self.session(request)
        if profile == "demo":
            return self.stores[profile], self.demo_key
        entry = session["keys"].get(profile)
        if not entry or time.monotonic() - entry[1] > 1800:
            session["keys"].pop(profile, None)
            raise web.HTTPUnauthorized(text="请先解锁正式台账")
        session["keys"][profile] = (entry[0], time.monotonic())
        return self.stores[profile], entry[0]

    def invalidate(self, profile):
        for session in self.sessions.values():
            session["keys"].pop(profile, None)

    def json_response(self, data, status=200):
        return web.json_response(data, status=status, dumps=lambda x: json.dumps(x, ensure_ascii=False))

    @web.middleware
    async def security(self, request, handler):
        response = None
        cookie = None
        try:
            local = request.transport.get_extra_info("sockname")
            port = local[1]
            expected = f"127.0.0.1:{port}"
            if request.remote not in ("127.0.0.1", "::1") or request.host != expected:
                raise web.HTTPForbidden(text="只允许从本机入口访问")
            origin = request.headers.get("Origin")
            if origin and origin != "http://" + expected:
                raise web.HTTPForbidden(text="访问来源不匹配，请从桌面入口打开")
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                raise web.HTTPForbidden(text="不允许跨站访问")
            sid = request.cookies.get("ledger_sid")
            session = self.sessions.get(sid)
            if not session:
                sid = secrets.token_urlsafe(32)
                session = {"csrf": secrets.token_urlsafe(32), "keys": {}, "last": time.monotonic()}
                self.sessions[sid] = session
                cookie = sid
            session["last"] = time.monotonic()
            request[SESSION_KEY] = session
            if len(self.sessions) > 200:
                self.sessions = {k: v for k, v in self.sessions.items() if time.monotonic() - v["last"] < 3600}
            if request.method not in ("GET", "HEAD", "OPTIONS"):
                supplied = request.headers.get("X-CSRF-Token", "")
                if not secrets.compare_digest(supplied, session["csrf"]):
                    raise web.HTTPForbidden(text="页面会话已过期，请刷新页面后重试")
            response = await handler(request)
        except web.HTTPException as exc:
            response = self.json_response({"error": exc.text or exc.reason}, status=exc.status)
        except (ValueError, KeyError, TypeError) as exc:
            response = self.json_response({"error": str(exc) or "请检查输入内容"}, status=400)
        except sqlite3.IntegrityError:
            response = self.json_response({"error": "编号重复或数据关联冲突，请刷新后核对"}, status=409)
        except OSError:
            logging.error("Storage operation failed at %s", request.path)
            response = self.json_response({"error": "本地文件保存失败，请检查磁盘空间或目录权限。原有记录仍保留。"}, status=500)
        except Exception as exc:
            logging.error("Unhandled %s at %s", type(exc).__name__, request.path)
            response = self.json_response({"error": "操作未完成，请刷新后重试；详情请查看本地日志。"}, status=500)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self' 'wasm-unsafe-eval'; worker-src 'self' blob:; font-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-src 'self' blob:; object-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'self'; frame-ancestors 'self'"
        if cookie:
            response.set_cookie("ledger_sid", cookie, httponly=True, samesite="Strict", path="/")
        return response

    async def body(self, request):
        try:
            data = await request.json()
        except Exception as exc:
            raise ValueError("请求格式不正确") from exc
        if not isinstance(data, dict):
            raise ValueError("请求内容必须是对象")
        return data

    async def health(self, request):
        return self.json_response({"app": APP_ID, "version": VERSION})

    async def static(self, request):
        name = request.path.lstrip("/") or "index.html"
        return web.FileResponse(ROOT / "static" / name)

    async def vendor_asset(self, request):
        folder = (ROOT / "static" / "vendor" / "pdfjs").resolve()
        asset = folder / request.match_info["tail"]
        if asset.is_symlink() or not asset.resolve().is_relative_to(folder) or not asset.is_file():
            raise web.HTTPNotFound(text="资源不存在")
        return web.FileResponse(asset)

    async def session_info(self, request):
        profile = self.profile(request)
        unlocked = profile == "demo"
        if profile == "live":
            try:
                self.require(request)
                unlocked = True
            except web.HTTPUnauthorized:
                pass
        return self.json_response({"app": APP_ID, "version": VERSION, "profile": profile,
            "initialized": Vault(self.stores[profile].db).initialized, "unlocked": unlocked,
            "csrf": self.session(request)["csrf"], "profiles": {"demo": True, "live": True}})

    async def auth(self, request):
        if self.profile(request) != "live":
            raise ValueError("演示台账无需设置解锁密码")
        data = await self.body(request)
        operation = request.match_info["operation"]
        session = self.session(request)
        vault = Vault(self.stores["live"].db)
        if operation == "lock":
            session["keys"].pop("live", None)
            return self.json_response({"ok": True})
        # Shared process-level delay prevents bypass by obtaining a new cookie.
        failures, retry_at = self.attempts.get("live", (0, 0))
        if time.monotonic() < retry_at:
            raise web.HTTPTooManyRequests(text="尝试次数较多，请稍后再试")
        async with self.gate:
            try:
                result = {"ok": True}
                if operation == "setup":
                    key, recovery = vault.setup(data.get("password", ""))
                    result["recovery_key"] = recovery
                elif operation == "unlock":
                    key = vault.unlock(data.get("password", ""))
                elif operation == "recover":
                    key, recovery = vault.recover(data.get("recovery_key", ""), data.get("password", ""))
                    self.invalidate("live")
                    result["recovery_key"] = recovery
                elif operation == "change":
                    _, key = self.require(request)
                    vault.change_password(key, data.get("old_password", ""), data.get("password", ""))
                    self.invalidate("live")
                else:
                    raise ValueError("不支持的解锁操作")
            except ValueError:
                self.attempts["live"] = (failures + 1, time.monotonic() + (30 if failures >= 4 else 0))
                raise
            self.attempts.pop("live", None)
            session["keys"]["live"] = (key, time.monotonic())
        return self.json_response(result)

    async def state(self, request):
        store, _ = self.require(request)
        state = store.state()
        for item in state.get("attachments", []):
            item.pop("storage_name", None)
        return self.json_response(state)

    async def action(self, request):
        store, _ = self.require(request)
        data = await self.body(request)
        ident = text_field(data, "request_id", 128, True)
        payload = data.get("data", {})
        if not isinstance(payload, dict):
            raise ValueError("操作数据格式错误")
        async with self.gate:
            result = store.action(text_field(data, "action", 80, True), payload, ident)
        return self.json_response(result)

    async def credentials_list(self, request):
        store, _ = self.require(request)
        rows = store.db.execute("SELECT id,store_id,label,created_at FROM credentials WHERE store_id=? AND id NOT LIKE ? ORDER BY created_at", (request.query.get("store_id", ""), STORE_DOCUMENT_PREFIX + "%")).fetchall()
        return self.json_response({"credentials": [dict(r) for r in rows]})

    def document_record(self, request, store):
        store_id = text_field(request.match_info, "store_id", 100, True)
        if not store.db.execute("SELECT 1 FROM stores WHERE id=?", (store_id,)).fetchone():
            raise ValueError("门店不存在")
        ident = STORE_DOCUMENT_PREFIX + store_id
        row = store.db.execute("SELECT * FROM credentials WHERE id=?", (ident,)).fetchone()
        if row and row["store_id"] != store_id:
            raise ValueError("账号备忘文档的门店归属不匹配")
        return store_id, ident, row

    @staticmethod
    def ordinary_credential_id(ident):
        if ident.casefold().startswith(STORE_DOCUMENT_PREFIX):
            raise ValueError("请通过门店的账号备忘文档编辑此内容")
        return ident

    async def store_document_get(self, request):
        store, key = self.require(request)
        store_id, ident, row = self.document_record(request, store)
        if row:
            payload = Vault.decrypt(key, row["payload"], ident)
            text = payload.get("text")
            if not isinstance(text, str):
                raise ValueError("账号备忘文档格式无效")
            return self.json_response({"store_id": store_id, "text": text, "exists": True,
                "updated_at": row["created_at"], "legacy_imported": False})
        # This runs only after the user explicitly opens the account document.
        # It offers existing encrypted entries as editable text without deleting
        # or silently rewriting any original credential records.
        sections = []
        legacy_rows = store.db.execute("SELECT * FROM credentials WHERE store_id=? AND id NOT LIKE ? ORDER BY created_at,id", (store_id, STORE_DOCUMENT_PREFIX + "%")).fetchall()
        for legacy in legacy_rows:
            data = Vault.decrypt(key, legacy["payload"], legacy["id"])
            lines = [legacy["label"]]
            for field, label in (("username", "账号 / Wi-Fi"), ("password", "密码"), ("url", "地址"), ("notes", "备注")):
                value = data.get(field, "")
                if isinstance(value, str) and value:
                    lines.append(label + "：" + value)
            sections.append("\n".join(lines))
        return self.json_response({"store_id": store_id, "text": "\n\n".join(sections), "exists": False,
            "updated_at": None, "legacy_imported": bool(sections)})

    async def store_document_save(self, request):
        store, key = self.require(request)
        data = await self.body(request)
        document = data.get("text")
        if not isinstance(document, str) or len(document) > DOCUMENT_LIMIT or "\x00" in document:
            raise ValueError("账号备忘必须是文字，最多 40000 个字符")
        request_id = "document:" + text_field(data, "request_id", 128, True)
        async with self.gate:
            store_id, ident, row = self.document_record(request, store)
            # A keyed fingerprint avoids persisting a guessable plain hash of a
            # document that may itself consist of a password.
            fingerprint = hmac.new(key, json.dumps({"store_id": store_id, "text": document},
                ensure_ascii=False, sort_keys=True).encode("utf-8"), hashlib.sha256).hexdigest()
            previous = store.db.execute("SELECT fingerprint,result_json FROM requests WHERE id=?", (request_id,)).fetchone()
            if previous:
                if not secrets.compare_digest(previous["fingerprint"], fingerprint):
                    raise ValueError("同一保存请求的文档内容已变化，请重新保存")
                return self.json_response(json.loads(previous["result_json"]))
            encrypted = Vault.encrypt(key, {"text": document}, ident)
            timestamp = now()
            result = {"store_id": store_id, "updated_at": timestamp, "exists": True}
            with store.db:
                store.db.execute("INSERT INTO credentials(id,store_id,label,payload,created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,label=excluded.label,created_at=excluded.created_at", (ident, store_id, "账号备忘文档", encrypted, timestamp))
                store.db.execute("INSERT INTO requests(id,fingerprint,result_json,created_at) VALUES(?,?,?,?)", (request_id, fingerprint, json.dumps(result, ensure_ascii=False), timestamp))
        return self.json_response(result)

    async def credentials_save(self, request):
        store, key = self.require(request)
        data = await self.body(request)
        store_id = text_field(data, "store_id", 100, True)
        label = text_field(data, "label", 120, True)
        payload = {k: text_field(data, k, 4000) for k in ("username", "url", "notes")}
        password = data.get("password", "")
        if not isinstance(password, str) or len(password) > 4000 or "\x00" in password:
            raise ValueError("密码格式无效或过长")
        payload["password"] = password
        ident = self.ordinary_credential_id(text_field(data, "id", 100) or str(uuid.uuid4()))
        async with self.gate:
            if not store.db.execute("SELECT 1 FROM stores WHERE id=?", (store_id,)).fetchone():
                raise ValueError("门店不存在")
            old = store.db.execute("SELECT store_id FROM credentials WHERE id=?", (ident,)).fetchone()
            if old and old[0] != store_id:
                raise ValueError("不能把账号静默移动到另一门店")
            cipher = Vault.encrypt(key, payload, ident)
            with store.db:
                store.db.execute("INSERT INTO credentials(id,store_id,label,payload,created_at) VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET label=excluded.label,payload=excluded.payload", (ident, store_id, label, cipher, now()))
        return self.json_response({"id": ident})

    async def credentials_reveal(self, request):
        store, key = self.require(request)
        data = await self.body(request)
        ident = self.ordinary_credential_id(text_field(data, "id", 100, True))
        row = store.db.execute("SELECT * FROM credentials WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("账号记录不存在")
        return self.json_response({"id": ident, "store_id": row["store_id"], "label": row["label"], **Vault.decrypt(key, row["payload"], ident)})

    async def credentials_delete(self, request):
        store, _ = self.require(request)
        data = await self.body(request)
        ident = self.ordinary_credential_id(text_field(data, "id", 100, True))
        async with self.gate:
            with store.db:
                result = store.db.execute("DELETE FROM credentials WHERE id=?", (ident,))
                if not result.rowcount:
                    raise ValueError("账号记录不存在")
        return self.json_response({"ok": True})

    def attachment_path(self, profile, storage_name):
        if not isinstance(storage_name, str) or not storage_name or Path(storage_name).name != storage_name or ":" in storage_name or "/" in storage_name or "\\" in storage_name:
            raise ValueError("附件路径无效")
        folder = self.data_dir / profile / "attachments"
        target = folder / storage_name
        if target.is_symlink() or not target.resolve().is_relative_to(folder.resolve()):
            raise ValueError("附件路径无效")
        return target

    async def read_upload(self, request, limit):
        if request.content_type != "multipart/form-data":
            raise ValueError("请选择要上传的文件")
        reader = await request.multipart()
        fields, file_info = {}, None
        temporary = self.data_dir / ".uploads"
        temporary.mkdir(exist_ok=True)
        path = temporary / uuid.uuid4().hex
        try:
            async for part in reader:
                if part.name == "file" and part.filename:
                    if file_info is not None:
                        raise ValueError("每次请选择一个文件")
                    filename = part.filename.replace("\\", "/").split("/")[-1]
                    if not filename or len(filename) > 240 or any(ord(c) < 32 for c in filename):
                        raise ValueError("文件名无效或过长")
                    size = 0
                    with path.open("xb") as output:
                        while chunk := await part.read_chunk(65536):
                            size += len(chunk)
                            if size > limit:
                                raise ValueError(f"文件过大，最大允许 {limit // (1024 * 1024)} MB")
                            output.write(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    if not size:
                        raise ValueError("不能上传空文件")
                    file_info = {"filename": filename, "size": size}
                else:
                    if part.name not in {"owner_type", "owner_id", "slot", "notes", "password", "recovery_key", "upload_id"}:
                        raise ValueError("上传字段无效")
                    if part.name in fields:
                        raise ValueError("上传字段重复")
                    raw = await part.read_chunk(16385)
                    if len(raw) > 16384 or not part.at_eof():
                        raise ValueError("附件说明过长")
                    fields[part.name] = raw.decode("utf-8")
            if not file_info:
                raise ValueError("请选择文件")
            return fields, file_info, path
        except BaseException:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            raise

    @staticmethod
    def safe_mime(path, filename):
        with path.open("rb") as file:
            head = file.read(16)
        if head.startswith(b"\x89PNG\r\n\x1a\n"):
            return "image/png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg"
        if head.startswith((b"GIF87a", b"GIF89a")):
            return "image/gif"
        if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
            return "image/webp"
        if head.startswith(b"%PDF-"):
            return "application/pdf"
        guessed = mimetypes.guess_type(filename)[0]
        return guessed if guessed in {"application/json", "text/plain", "application/zip"} else "application/octet-stream"

    async def attachment_upload(self, request):
        store, _ = self.require(request)
        profile = self.profile(request)
        fields, info, path = await self.read_upload(request, FILE_LIMIT)
        new_path = None
        committed = False
        try:
            owner_type = text_field(fields, "owner_type", 20, True)
            owner_id = text_field(fields, "owner_id", 100, True)
            if owner_type not in OWNER_TABLES:
                raise ValueError("附件关联类型无效")
            slot = (text_field(fields, "slot", 120) or info["filename"]) if owner_type == "store" else uuid.uuid4().hex
            notes = text_field(fields, "notes", 4000)
            mime = self.safe_mime(path, info["filename"])
            upload_id = text_field(fields, "upload_id", 100)
            request_id = "attachment:" + upload_id if upload_id else None
            fingerprint = None
            if request_id:
                with path.open("rb") as source:
                    content_hash = hashlib.file_digest(source, "sha256").hexdigest()
                fingerprint = hashlib.sha256(json.dumps({"owner_type": owner_type, "owner_id": owner_id,
                    "slot": fields.get("slot", ""), "notes": notes, "filename": info["filename"], "hash": content_hash}, sort_keys=True).encode()).hexdigest()
            async with self.gate:
                if request_id:
                    previous = store.db.execute("SELECT fingerprint,result_json FROM requests WHERE id=?", (request_id,)).fetchone()
                    if previous:
                        if previous["fingerprint"] != fingerprint:
                            raise ValueError("同一上传请求的内容已改变，请重新选择文件")
                        return self.json_response(json.loads(previous["result_json"]))
                if not store.db.execute(f"SELECT 1 FROM {OWNER_TABLES[owner_type]} WHERE id=?", (owner_id,)).fetchone():
                    raise ValueError("关联记录不存在")
                old = None
                if owner_type == "store":
                    old = store.db.execute("SELECT * FROM attachments WHERE owner_type='store' AND owner_id=? AND slot=?", (owner_id, slot)).fetchone()
                ident = old["id"] if old else str(uuid.uuid4())
                storage = uuid.uuid4().hex
                new_path = self.attachment_path(profile, storage)
                os.replace(path, new_path)
                result = {"id": ident, "filename": info["filename"], "slot": slot, "mime": mime, "size": info["size"]}
                with store.db:
                    if old:
                        store.db.execute("UPDATE attachments SET filename=?,mime=?,size=?,storage_name=?,notes=?,created_at=? WHERE id=?", (info["filename"], mime, info["size"], storage, notes, now(), ident))
                    else:
                        store.db.execute("INSERT INTO attachments(id,owner_type,owner_id,slot,filename,mime,size,storage_name,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (ident, owner_type, owner_id, slot, info["filename"], mime, info["size"], storage, notes, now()))
                    if request_id:
                        store.db.execute("INSERT INTO requests(id,fingerprint,result_json,created_at) VALUES(?,?,?,?)", (request_id, fingerprint, json.dumps(result, ensure_ascii=False), now()))
                committed = True
                if old:
                    with suppress(OSError, ValueError):
                        self.attachment_path(profile, old["storage_name"]).unlink(missing_ok=True)
            return self.json_response(result)
        finally:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            if new_path and not committed:
                with suppress(OSError):
                    new_path.unlink(missing_ok=True)

    async def attachment_get(self, request):
        store, _ = self.require(request)
        row = store.db.execute("SELECT * FROM attachments WHERE id=?", (request.match_info["id"],)).fetchone()
        if not row:
            raise web.HTTPNotFound(text="附件不存在")
        path = self.attachment_path(self.profile(request), row["storage_name"])
        if not path.is_file():
            raise web.HTTPNotFound(text="附件文件缺失，请从备份恢复")
        from urllib.parse import quote
        inline = row["mime"] in INLINE_TYPES and request.query.get("download") != "1"
        response = web.FileResponse(path)
        response.headers["Content-Type"] = row["mime"] if row["mime"] in INLINE_TYPES else "application/octet-stream"
        response.headers["Content-Disposition"] = f"{'inline' if inline else 'attachment'}; filename*=UTF-8''{quote(row['filename'], safe='')}"
        return response

    async def attachment_delete(self, request):
        store, _ = self.require(request)
        data = await self.body(request)
        async with self.gate:
            row = store.db.execute("SELECT storage_name FROM attachments WHERE id=?", (text_field(data, "id", 100, True),)).fetchone()
            if not row:
                raise ValueError("附件不存在")
            with store.db:
                store.db.execute("DELETE FROM attachments WHERE id=?", (data["id"],))
            with suppress(OSError):
                self.attachment_path(self.profile(request), row[0]).unlink(missing_ok=True)
        return self.json_response({"ok": True})

    async def backup(self, request):
        store, _ = self.require(request)
        profile = self.profile(request)
        export_dir = self.data_dir / ".exports"
        export_dir.mkdir(exist_ok=True)
        # Remove only stale generated exports in this known application directory.
        for old in export_dir.glob("*.zip"):
            if old.is_file() and not old.is_symlink() and time.time() - old.stat().st_mtime > 86400:
                with suppress(OSError):
                    old.unlink()
        destination = export_dir / f"ledger-{profile}-{uuid.uuid4().hex}.zip"
        async with self.gate:
            create_backup(store, destination, profile)
        response = web.FileResponse(destination)
        response.headers["Content-Type"] = "application/zip"
        response.headers["Content-Disposition"] = f'attachment; filename="car-ledger-{profile}-{datetime.now(TZ):%Y%m%d-%H%M%S}.zip"'
        return response

    async def restore(self, request):
        self.require(request)
        profile = self.profile(request)
        fields, _, path = await self.read_upload(request, RESTORE_LIMIT)
        temp_parent = self.data_dir / ".restore"
        temp_parent.mkdir(exist_ok=True)
        extracted = None
        try:
            async with self.gate:
                extracted = validate_backup(path, temp_parent, profile, password=fields.get("password") or None, recovery_key=fields.get("recovery_key") or None)
                if profile == "demo":
                    conn = sqlite3.connect(extracted / "ledger.sqlite3")
                    try:
                        Vault(conn).unlock(DEMO_PASSWORD)
                    finally:
                        conn.close()
                target = self.data_dir / profile
                previous = temp_parent / ("previous-" + uuid.uuid4().hex)
                self.stores[profile].close()
                moved = False
                try:
                    os.replace(target, previous)
                    moved = True
                    os.replace(extracted, target)
                    self.stores[profile] = Store(target)
                except BaseException:
                    if moved:
                        if target.exists():
                            failed = temp_parent / ("failed-" + uuid.uuid4().hex)
                            os.replace(target, failed)
                        os.replace(previous, target)
                    self.stores[profile] = Store(target)
                    raise
                self.invalidate(profile)
                if profile == "demo":
                    self.demo_key = self._demo_vault()
                # Keep one pre-restore snapshot for safe rollback; not a browsable file history.
                for old in temp_parent.glob("previous-*"):
                    if old != previous and old.is_dir() and not old.is_symlink() and old.resolve().is_relative_to(temp_parent.resolve()):
                        with suppress(OSError):
                            shutil.rmtree(old)
            return self.json_response({"ok": True, "locked": profile == "live"})
        finally:
            with suppress(OSError):
                path.unlink(missing_ok=True)
            if extracted and extracted.exists() and extracted.resolve().is_relative_to(temp_parent.resolve()):
                with suppress(OSError):
                    shutil.rmtree(extracted)

    async def shutdown(self, request):
        self.require(request)
        asyncio.get_running_loop().call_later(0.3, self.stop_event.set)
        return self.json_response({"ok": True})

    async def startup(self, app):
        self.expiry_task = asyncio.create_task(self.expire_sessions())

    async def expire_sessions(self):
        while True:
            await asyncio.sleep(15)
            current = time.monotonic()
            for session in list(self.sessions.values()):
                entry = session["keys"].get("live")
                if entry and current - entry[1] > 1800:
                    session["keys"].pop("live", None)
            self.sessions = {k: v for k, v in self.sessions.items() if current - v["last"] < 3600}

    async def cleanup(self, app):
        if hasattr(self, "expiry_task"):
            self.expiry_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.expiry_task
        self.sessions.clear()
        self.demo_key = b""
        for store in self.stores.values():
            store.close()


async def run(args):
    instance = LedgerServer(Path(args.data_dir))
    runner = web.AppRunner(instance.app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", args.port)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}"
    info = instance.data_dir / "server.json"
    info.write_text(json.dumps({"app": APP_ID, "version": VERSION, "port": port, "pid": os.getpid(), "url": url}), encoding="utf-8")
    print(url, flush=True)
    if args.open:
        import webbrowser
        webbrowser.open(url)
    try:
        await instance.stop_event.wait()
    finally:
        info.unlink(missing_ok=True)
        await runner.cleanup()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=18769)
    parser.add_argument("--data-dir", default=str(ROOT / "data"))
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
