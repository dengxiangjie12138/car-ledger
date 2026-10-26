"""Consistent portable backups with fail-closed validation before restore."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone, timedelta
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
from typing import Any
import uuid
import zipfile

from vault import verify_archive


FORMAT = "car-ledger-local-backup"
VERSION = 1
SCHEMA_VERSION = 1
MAX_ARCHIVE_BYTES = 2 * 1024 ** 3
MAX_DB_BYTES = 256 * 1024 ** 2
MAX_ATTACHMENT_BYTES = 50 * 1024 ** 2
MAX_MANIFEST_BYTES = 16 * 1024 ** 2
MAX_ENTRIES = 25000
_BASE_COLUMNS = {
    "settings": {"key", "value"},
    "credentials": {"id", "store_id", "label", "payload", "created_at"},
    "attachments": {"id", "owner_type", "owner_id", "slot", "filename", "mime", "size", "storage_name", "notes", "created_at"},
    "stores": {"id", "name", "contact", "phone", "address", "notes", "created_at"},
    "batches": {"id", "kind", "supplier", "date", "cost_cents", "notes", "created_at"},
    "components": {"id", "kind", "serial", "batch_id", "status", "store_id", "notes", "created_at", "revision"},
    "vehicles": {"id", "serial", "frame_id", "pcba_id", "store_id", "status", "created_at", "revision"},
    "ledger": {"id", "direction", "date", "category", "amount_cents", "counterparty", "purpose", "store_id", "batch_id", "notes", "voided", "void_reason", "created_at"},
    "shipments": {"id", "store_id", "date", "kind", "notes", "created_at", "voided", "void_reason"},
    "shipment_items": {"id", "shipment_id", "kind", "item_id", "serial", "pcba_serial", "before_json", "after_json"},
    "movements": {"seq", "id", "component_id", "action", "from_status", "to_status", "store_id", "date", "notes", "created_at"},
    "history": {"seq", "id", "vehicle_id", "event", "date", "old_pcba_serial", "new_pcba_serial", "from_store_id", "to_store_id", "notes", "created_at"},
    "requests": {"id", "fingerprint", "result_json", "created_at"},
}


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _storage_name(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 200 or value in {".", ".."}:
        raise ValueError("附件存储名称无效")
    if any(ch in value for ch in '/\\:\x00') or value.rstrip(". ") != value:
        raise ValueError("附件存储名称无效")
    if Path(value).name != value:
        raise ValueError("附件存储名称无效")
    # Reject NTFS alternate streams and device names even on non-Windows hosts.
    stem = value.split(".")[0].upper()
    if stem in {"CON", "PRN", "AUX", "NUL", *[f"COM{i}" for i in range(1, 10)], *[f"LPT{i}" for i in range(1, 10)]}:
        raise ValueError("附件存储名称无效")
    return value


def _profile(value: str) -> str:
    if value not in {"demo", "live"}:
        raise ValueError("台账类型无效")
    return value


def _read_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def _inspect_db(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    if conn.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("备份数据库版本不受支持")
    if conn.execute("PRAGMA quick_check").fetchone()[0] != "ok":
        raise ValueError("备份数据库已损坏")
    if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ValueError("备份数据库关联关系无效")
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger') LIMIT 1").fetchone():
        raise ValueError("备份包含不受支持的数据库对象")
    for table, columns in _BASE_COLUMNS.items():
        row = conn.execute("SELECT type FROM sqlite_master WHERE name=?", (table,)).fetchone()
        if row is None or row[0] != "table":
            raise ValueError("备份数据库缺少必要资料表")
        actual = {row[1] for row in conn.execute(f'PRAGMA table_info("{table}")')}
        if not columns.issubset(actual):
            raise ValueError("备份数据库资料表格式不正确")
    rows = list(conn.execute("SELECT storage_name,size FROM attachments"))
    seen: set[str] = set()
    for row in rows:
        name = _storage_name(row["storage_name"])
        if name.casefold() in seen:
            raise ValueError("备份包含重复附件存储名称")
        seen.add(name.casefold())
        if not isinstance(row["size"], int) or not 0 <= row["size"] <= MAX_ATTACHMENT_BYTES:
            raise ValueError("备份附件大小无效")
    if len(rows) + 2 > MAX_ENTRIES:
        raise ValueError("备份附件数量超出上限")
    return rows


def _cleanup(path: Path, parent: Path) -> None:
    resolved, base = path.resolve(), parent.resolve()
    if resolved != base and resolved.is_relative_to(base) and resolved.name.startswith(("backup-", "restore-")):
        shutil.rmtree(resolved, ignore_errors=True)


def _temporary_directory(parent: Path, prefix: str) -> Path:
    # Python 3.13's Windows mkdir(mode=0700), used by tempfile.mkdtemp,
    # restricts the ACL to the host user and excludes managed sandbox tokens.
    # Use an unpredictable atomically-created ordinary workspace directory.
    for _ in range(4):
        directory = parent / (prefix + uuid.uuid4().hex)
        try:
            directory.mkdir(mode=0o777)
            return directory
        except FileExistsError:
            continue
    raise ValueError("无法创建备份临时目录")


def create_backup(store: Any, destination: Path, profile: str) -> Path:
    """Caller holds the application write gate across snapshot and file copy."""
    profile = _profile(profile)
    destination = Path(destination).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    database_file = store.db.execute("PRAGMA database_list").fetchone()[2]
    if not database_file:
        raise ValueError("仅支持备份已保存到本地的台账")
    source_root = Path(database_file).resolve().parent
    scratch = _temporary_directory(destination.parent, "backup-")
    snapshot = scratch / "ledger.sqlite3"
    output = scratch / "output.zip"
    try:
        with closing(sqlite3.connect(snapshot)) as conn:
            store.db.backup(conn)
        with closing(_read_db(snapshot)) as conn:
            attachments = _inspect_db(conn)
        files: dict[str, Path] = {"ledger.sqlite3": snapshot}
        for row in attachments:
            name = _storage_name(row["storage_name"])
            source = source_root / "attachments" / name
            if source.is_symlink() or not source.is_file() or source.resolve().parent != (source_root / "attachments").resolve():
                raise ValueError("附件缺失或路径无效，无法生成完整备份")
            if source.stat().st_size != row["size"]:
                raise ValueError("附件大小与记录不一致，无法生成完整备份")
            files[f"attachments/{name}"] = source
        if snapshot.stat().st_size > MAX_DB_BYTES or sum(p.stat().st_size for p in files.values()) > MAX_ARCHIVE_BYTES:
            raise ValueError("台账超过单个备份包支持的大小")
        manifest = {
            "format": FORMAT, "version": VERSION, "schema_version": SCHEMA_VERSION,
            "profile": profile, "created_at": datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds"),
            "files": {name: {"size": path.stat().st_size, "sha256": _hash_file(path)} for name, path in files.items()},
        }
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(manifest_bytes) > MAX_MANIFEST_BYTES:
            raise ValueError("备份清单超过支持的大小，请减少附件后重试")
        if sum(p.stat().st_size for p in files.values()) + len(manifest_bytes) > MAX_ARCHIVE_BYTES:
            raise ValueError("台账与备份清单超过解压大小上限，请减少附件后重试")
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            archive.writestr("manifest.json", manifest_bytes)
            for name, path in files.items():
                archive.write(path, name)
        # A ZIP also contains headers and a manifest. Check its actual size so
        # every returned backup fits the same upload limit used by restore.
        if output.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("生成的备份包超过导入大小上限，请减少附件后重试")
        os.replace(output, destination)
        return destination
    except (OSError, sqlite3.DatabaseError, zipfile.BadZipFile) as exc:
        raise ValueError("生成备份失败，请检查磁盘空间及附件是否完整") from exc
    finally:
        _cleanup(scratch, destination.parent)


def _safe_entry(info: zipfile.ZipInfo) -> str:
    name = info.filename
    if not name or "\\" in name or "\x00" in name or ":" in name or name.startswith("/"):
        raise ValueError("备份包含非法文件路径")
    path = PurePosixPath(name)
    if any(part in {"", ".", ".."} for part in name.split("/")) or str(path) != name:
        raise ValueError("备份包含非法文件路径")
    if info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
        raise ValueError("备份不能包含目录项、链接或加密 ZIP 文件")
    if name not in {"ledger.sqlite3", "manifest.json"}:
        if len(path.parts) != 2 or path.parts[0] != "attachments":
            raise ValueError("备份包含非台账文件")
        _storage_name(path.parts[1])
    limit = MAX_MANIFEST_BYTES if name == "manifest.json" else MAX_DB_BYTES if name == "ledger.sqlite3" else MAX_ATTACHMENT_BYTES
    if not 0 <= info.file_size <= limit:
        raise ValueError("备份中单个文件大小超出上限")
    # Highly repetitive maps and blank images can legitimately exceed a 1000:1
    # ratio. Per-entry and aggregate uncompressed limits bound extraction work.
    return name


def validate_backup(
    archive_path: Path, temp_parent: Path, expected_profile: str,
    password: str | None = None, recovery_key: str | None = None,
) -> Path:
    """Return a validated staging directory; caller owns swapping/cleanup."""
    expected_profile = _profile(expected_profile)
    archive_path, temp_parent = Path(archive_path).resolve(), Path(temp_parent).resolve()
    temp_parent.mkdir(parents=True, exist_ok=True)
    staging = _temporary_directory(temp_parent, "restore-")
    try:
        if not archive_path.is_file() or archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise ValueError("备份文件不存在或大小超出上限")
        with zipfile.ZipFile(archive_path, "r") as archive:
            entries = archive.infolist()
            if not 2 <= len(entries) <= MAX_ENTRIES:
                raise ValueError("备份文件数量无效")
            names = [_safe_entry(info) for info in entries]
            if len({name.casefold() for name in names}) != len(names):
                raise ValueError("备份包含重复文件")
            if sum(info.file_size for info in entries) > MAX_ARCHIVE_BYTES:
                raise ValueError("备份解压后大小超出上限")
            if "manifest.json" not in names or "ledger.sqlite3" not in names:
                raise ValueError("备份缺少清单或数据库")
            try:
                manifest = json.loads(archive.read("manifest.json"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise ValueError("备份清单无法读取") from exc
            if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or manifest.get("version") != VERSION or manifest.get("schema_version") != SCHEMA_VERSION:
                raise ValueError("备份格式或版本不受支持")
            if manifest.get("profile") != expected_profile:
                raise ValueError("演示台账与正式台账的备份不能混用")
            listed = manifest.get("files")
            if not isinstance(listed, dict) or set(listed) != set(names) - {"manifest.json"}:
                raise ValueError("备份文件与清单不一致")
            for info in entries:
                name = info.filename
                if name == "manifest.json":
                    continue
                metadata = listed[name]
                if not isinstance(metadata, dict) or type(metadata.get("size")) is not int or metadata["size"] != info.file_size:
                    raise ValueError("备份文件大小与清单不一致")
                digest_text = metadata.get("sha256")
                if not isinstance(digest_text, str) or len(digest_text) != 64 or any(c not in "0123456789abcdef" for c in digest_text):
                    raise ValueError("备份校验值无效")
                target = staging.joinpath(*PurePosixPath(name).parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, copied = hashlib.sha256(), 0
                with archive.open(info) as source, target.open("xb") as output:
                    for chunk in iter(lambda: source.read(1024 * 1024), b""):
                        copied += len(chunk)
                        if copied > info.file_size:
                            raise ValueError("备份解压大小异常")
                        digest.update(chunk)
                        output.write(chunk)
                if copied != info.file_size or digest.hexdigest() != digest_text:
                    raise ValueError("备份文件校验失败，文件可能已损坏")
        (staging / "attachments").mkdir(exist_ok=True)
        with closing(_read_db(staging / "ledger.sqlite3")) as conn:
            attachments = _inspect_db(conn)
            expected_files = {"ledger.sqlite3", *[f"attachments/{row['storage_name']}" for row in attachments]}
            if set(listed) != expected_files:
                raise ValueError("备份附件与数据库记录不一致")
            for row in attachments:
                if listed[f"attachments/{row['storage_name']}"]["size"] != row["size"]:
                    raise ValueError("备份附件大小与数据库记录不一致")
            if expected_profile == "live":
                master = verify_archive(conn, password=password, recovery_key=recovery_key)
                # Verify every encrypted record, not merely the key envelope.
                from vault import Vault
                for credential in conn.execute("SELECT id,store_id,payload FROM credentials"):
                    # The server contract uses the stable credential ID as AAD.
                    Vault.decrypt(master, credential["payload"], credential["id"])
        return staging
    except ValueError:
        _cleanup(staging, temp_parent)
        raise
    except (OSError, sqlite3.DatabaseError, zipfile.BadZipFile, RuntimeError, KeyError, TypeError, NotImplementedError) as exc:
        _cleanup(staging, temp_parent)
        raise ValueError("备份无法恢复，请检查文件是否完整以及密码是否正确") from exc
