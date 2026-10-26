"""Versioned encryption for local store credentials.

Only wrapped data keys are persisted. Callers keep unlocked keys in memory and
must never include them in logs, sessions written to disk, or backup manifests.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sqlite3
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt


SETTING_KEY = "vault.v1"
_PREFIX = b"car-ledger-local:v1:"
_KDF = {"name": "scrypt", "n": 131072, "r": 8, "p": 1}


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii")


def _decode(value: str, expected: int | None = None) -> bytes:
    if not isinstance(value, str) or len(value) > 1024 * 1024:
        raise ValueError("加密资料格式无效")
    try:
        result = base64.b64decode(value, altchars=b"-_", validate=True)
    except (ValueError, TypeError) as exc:
        raise ValueError("加密资料格式无效") from exc
    if expected is not None and len(result) != expected:
        raise ValueError("加密资料格式无效")
    return result


def _password(value: str) -> str:
    if not isinstance(value, str) or not 8 <= len(value) <= 1024:
        raise ValueError("台账密码至少需要 8 个字符，最多 1024 个字符")
    return value


def _derive(secret: str, salt: bytes) -> bytes:
    if not isinstance(secret, str) or not 1 <= len(secret) <= 1024:
        raise ValueError("密码或恢复密钥无效")
    return Scrypt(salt=salt, length=32, n=_KDF["n"], r=_KDF["r"], p=_KDF["p"]).derive(secret.encode("utf-8"))


def _recovery_key() -> str:
    return "CL1-" + secrets.token_urlsafe(32)


def _wrap(master_key: bytes, secret: str, purpose: str) -> dict[str, str]:
    salt, nonce = os.urandom(16), os.urandom(12)
    key = _derive(secret, salt)
    ciphertext = AESGCM(key).encrypt(nonce, master_key, _PREFIX + purpose.encode("ascii"))
    return {"salt": _encode(salt), "nonce": _encode(nonce), "ciphertext": _encode(ciphertext)}


def _unwrap(envelope: dict[str, Any], secret: str, purpose: str) -> bytes:
    try:
        if not isinstance(envelope, dict) or set(envelope) != {"salt", "nonce", "ciphertext"}:
            raise ValueError("加密资料格式无效")
        key = _derive(secret, _decode(envelope["salt"], 16))
        result = AESGCM(key).decrypt(
            _decode(envelope["nonce"], 12),
            _decode(envelope["ciphertext"], 48),
            _PREFIX + purpose.encode("ascii"),
        )
        if len(result) != 32:
            raise ValueError("加密资料格式无效")
        return result
    except (InvalidTag, KeyError, TypeError, ValueError) as exc:
        raise ValueError("密码或恢复密钥不正确，或加密资料已损坏") from exc


class Vault:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    @property
    def initialized(self) -> bool:
        return self.conn.execute("SELECT 1 FROM settings WHERE key=?", (SETTING_KEY,)).fetchone() is not None

    def _read(self) -> dict[str, Any]:
        row = self.conn.execute("SELECT value FROM settings WHERE key=?", (SETTING_KEY,)).fetchone()
        if row is None:
            raise ValueError("请先设置台账解锁密码")
        try:
            record = json.loads(row[0])
            if (
                not isinstance(record, dict)
                or set(record) != {"version", "kdf", "password", "recovery"}
                or record["version"] != 1
                or record["kdf"] != _KDF
            ):
                raise ValueError("加密资料版本或格式不受支持")
            return record
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError("台账加密设置已损坏") from exc

    def _write(self, record: dict[str, Any], *, create: bool = False) -> None:
        serialized = json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        try:
            with self.conn:
                if create:
                    self.conn.execute("INSERT INTO settings(key,value) VALUES (?,?)", (SETTING_KEY, serialized))
                else:
                    result = self.conn.execute("UPDATE settings SET value=? WHERE key=?", (serialized, SETTING_KEY))
                    if result.rowcount != 1:
                        raise ValueError("台账加密设置不存在")
        except sqlite3.IntegrityError as exc:
            raise ValueError("台账已设置密码，请使用解锁功能") from exc

    def setup(self, password: str) -> tuple[bytes, str]:
        _password(password)
        if self.initialized:
            raise ValueError("台账已设置密码，请使用解锁功能")
        master_key, recovery = AESGCM.generate_key(bit_length=256), _recovery_key()
        self._write(
            {
                "version": 1,
                "kdf": _KDF.copy(),
                "password": _wrap(master_key, password, "master:password"),
                "recovery": _wrap(master_key, recovery, "master:recovery"),
            },
            create=True,
        )
        return master_key, recovery

    def unlock(self, password: str) -> bytes:
        return _unwrap(self._read()["password"], password, "master:password")

    def unlock_recovery(self, recovery_key: str) -> bytes:
        if not isinstance(recovery_key, str) or not recovery_key.startswith("CL1-") or len(recovery_key) != 47:
            raise ValueError("恢复密钥不正确")
        return _unwrap(self._read()["recovery"], recovery_key, "master:recovery")

    def recover(self, recovery_key: str, new_password: str) -> tuple[bytes, str]:
        _password(new_password)
        record = self._read()
        master_key = self.unlock_recovery(recovery_key)
        new_recovery = _recovery_key()
        record["password"] = _wrap(master_key, new_password, "master:password")
        record["recovery"] = _wrap(master_key, new_recovery, "master:recovery")
        self._write(record)
        return master_key, new_recovery

    def change_password(self, master_key: bytes, old_password: str, new_password: str) -> None:
        _password(new_password)
        checked = self.unlock(old_password)
        if not isinstance(master_key, bytes) or not secrets.compare_digest(checked, master_key):
            raise ValueError("当前解锁状态无效，请重新解锁")
        record = self._read()
        record["password"] = _wrap(master_key, new_password, "master:password")
        self._write(record)

    @staticmethod
    def encrypt(master_key: bytes, payload: dict[str, Any], context: str) -> str:
        if not isinstance(master_key, bytes) or len(master_key) != 32:
            raise ValueError("台账尚未解锁")
        if not isinstance(payload, dict) or not isinstance(context, str) or not context or len(context) > 1000:
            raise ValueError("账号资料格式无效")
        try:
            raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError) as exc:
            raise ValueError("账号资料格式无效") from exc
        if len(raw) > 256 * 1024:
            raise ValueError("账号资料过长")
        nonce = os.urandom(12)
        ciphertext = AESGCM(master_key).encrypt(nonce, raw, _PREFIX + b"credential:" + context.encode("utf-8"))
        return json.dumps({"version": 1, "nonce": _encode(nonce), "ciphertext": _encode(ciphertext)}, separators=(",", ":"))

    @staticmethod
    def decrypt(master_key: bytes, ciphertext: str, context: str) -> dict[str, Any]:
        try:
            if not isinstance(master_key, bytes) or len(master_key) != 32:
                raise ValueError("台账尚未解锁")
            if not isinstance(ciphertext, str) or len(ciphertext) > 512 * 1024:
                raise ValueError("账号加密资料格式无效")
            if not isinstance(context, str) or not context or len(context) > 1000:
                raise ValueError("账号资料格式无效")
            record = json.loads(ciphertext)
            if not isinstance(record, dict) or set(record) != {"version", "nonce", "ciphertext"} or record["version"] != 1:
                raise ValueError("账号加密资料版本不受支持")
            raw = AESGCM(master_key).decrypt(
                _decode(record["nonce"], 12), _decode(record["ciphertext"]),
                _PREFIX + b"credential:" + context.encode("utf-8"),
            )
            result = json.loads(raw)
            if not isinstance(result, dict):
                raise ValueError("账号资料格式无效")
            return result
        except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError("无法解密账号资料：密钥不正确或资料已损坏") from exc


def verify_archive(conn: sqlite3.Connection, password: str | None = None, recovery_key: str | None = None) -> bytes:
    vault = Vault(conn)
    if not vault.initialized:
        raise ValueError("正式台账备份缺少加密设置")
    if recovery_key:
        return vault.unlock_recovery(recovery_key)
    if password:
        return vault.unlock(password)
    raise ValueError("请输入该备份的台账密码或恢复密钥")
