"""Restricted loader for one audited upstream DB module, not its package entrypoint."""
from __future__ import annotations

import hashlib
import importlib.util
import logging
import os
import re
import sqlite3
import sys
import types
from pathlib import Path

from .core import ProbeError
from .isolation import require_live_admission

COMMIT = "62503d76a0db069171cb4e42e87090ad285db489"
VERSION = "1.2.6.1"
CLIENT_VERSION = "4.1.15.13"
DB_SHA256 = "1b8958a0e9a3f8d6c2f8fe89acd72c14bb542c3c16635c31b52e05c0f19b247b"
ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".cache" / "wechatauto-replica" / "wechatauto" / "db.py"


def verify_source(path: Path = SOURCE):
    source = path.read_bytes() if path.is_file() else b""
    if hashlib.sha256(source).hexdigest() != DB_SHA256:
        raise ProbeError("Pinned upstream db.py is missing or its SHA-256 does not match.")
    return source


def load_db_module():
    source = verify_source()
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise ProbeError("Live diagnostics require 64-bit Python on Windows.")
    # Importing wechatauto normally eagerly imports GUI, recall and media modules.
    # Supply only its logger dependency; execute exactly the hash-checked db.py.
    names = ("wechatauto", "wechatauto.logger")
    if any(n in sys.modules for n in names):
        raise ProbeError("Run the probe in a fresh process without the upstream package loaded.")
    package = types.ModuleType("wechatauto")
    package.__path__ = []
    logger = types.ModuleType("wechatauto.logger")
    logger.wxlog = logging.getLogger("wechat_probe.upstream")
    logger.wxlog.disabled = True
    sys.modules[names[0]], sys.modules[names[1]] = package, logger
    try:
        spec = importlib.util.spec_from_file_location("_wechat_probe_db", SOURCE)
        module = importlib.util.module_from_spec(spec)
        # Execute the same bytes we verified, without a second disk read.
        exec(compile(source, str(SOURCE), "exec"), module.__dict__)
    finally:
        for name in names:
            sys.modules.pop(name, None)
    # Fix upstream's absent 64-bit CloseHandle signature before any live reads.
    import ctypes
    from ctypes import wintypes
    module._k32.CloseHandle.argtypes = [wintypes.HANDLE]
    module._k32.CloseHandle.restype = wintypes.BOOL
    return module


def executable_version(executable: str) -> str:
    import ctypes
    from ctypes import wintypes
    version = ctypes.WinDLL("version", use_last_error=True)
    version.GetFileVersionInfoSizeW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(wintypes.DWORD)]
    size = version.GetFileVersionInfoSizeW(executable, None)
    if not size:
        raise ProbeError("Cannot establish the running client version.")
    buffer = ctypes.create_string_buffer(size)
    version.GetFileVersionInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
    if not version.GetFileVersionInfoW(executable, 0, size, buffer):
        raise ProbeError("Cannot read client version information.")
    pointer, length = ctypes.c_void_p(), wintypes.UINT()
    version.VerQueryValueW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.UINT)]
    if not version.VerQueryValueW(buffer, "\\", ctypes.byref(pointer), ctypes.byref(length)):
        raise ProbeError("Client version information is unavailable.")
    words = ctypes.cast(pointer, ctypes.POINTER(wintypes.DWORD))
    return ".".join(map(str, (words[2] >> 16, words[2] & 65535, words[3] >> 16, words[3] & 65535)))


def process_binding() -> dict:
    import psutil
    processes = []
    for proc in psutil.process_iter(["pid", "ppid", "name", "exe", "create_time"]):
        if (proc.info["name"] or "").lower() == "weixin.exe":
            processes.append(proc.info)
    pids = {p["pid"] for p in processes}
    mains = [p for p in processes if p["ppid"] not in pids]
    if len(mains) != 1:
        raise ProbeError("Expected exactly one main Weixin process; select the test session explicitly.")
    proc = mains[0]
    if executable_version(proc["exe"]) != CLIENT_VERSION:
        raise ProbeError("Client version changed or does not match the audited version.")
    return {"pid": proc["pid"], "started": proc["create_time"], "exe": proc["exe"]}


def account_paths(module) -> list[Path]:
    root = module.auto_detect_db_dir()
    if root is None:
        raise ProbeError("WeChat data directory was not found.")
    return sorted(p.resolve() for p in Path(root).iterdir()
                  if p.is_dir() and not p.is_symlink() and (p / "db_storage").is_dir())


def open_reader(module, account: Path, process: dict, cache: Path):
    require_live_admission()  # Must precede process access, database I/O and cache creation.
    if process_binding() != process:
        raise ProbeError("WeChat process changed; re-bind before reading.")
    account = account.resolve(strict=True)
    cache.mkdir(parents=True, exist_ok=True)

    class RestrictedDB(module.WeChatDB):
        def __init__(self):
            # Deliberately bypass the upstream constructor and all key fallback paths.
            self.db_dir = str(account.parent)
            self.account = account.name
            self.account_dir = str(account)
            self.workdir = str(cache)
            self._keys = {}
            self._db_files = self._collect_db_files()
            self.master_key = self.cfg_dword = None
            if not self._db_files:
                raise ProbeError("No permitted databases in the bound account directory.")
            self._keys = self._extract_keys_pid(process["pid"], set())
            self.unkeyed = [rel for rel, _, _ in self._db_files if not self._key_works(rel)]
            if self.unkeyed:
                raise ProbeError("Some required database keys are unavailable; no account fallback was attempted.")

        def _collect_db_files(self):
            base = account / "db_storage"
            paths = [base / "contact" / "contact.db"] + sorted((base / "message").glob("message_[0-9]*.db"))
            out = []
            for p in paths:
                if not p.exists():
                    continue
                if p.is_symlink() or not p.resolve().is_relative_to(base.resolve()):
                    raise ProbeError("Database path escapes the bound account.")
                rel = p.relative_to(base).as_posix()
                if rel == "contact/contact.db" or re.fullmatch(r"message/message_\d+\.db", rel):
                    out.append((rel, str(p), p.stat().st_size))
            return out

        def _save_keys(self):
            pass  # Keys remain in this process only, never serialized.

        def _refresh_db_files(self):
            if [x[:2] for x in self._collect_db_files()] != [x[:2] for x in self._db_files]:
                raise ProbeError("Database shards changed; a new baseline is required.")

        def _auto_diagnose_key_failure(self, rel):
            raise ProbeError("A bound database key is unavailable.")

        def _try_other_accounts(self):
            raise ProbeError("Account fallback is disabled.")

        def _find_weixin_pids(self):
            return [process["pid"]]

        def _open(self, rel):
            if process_binding() != process:
                raise ProbeError("WeChat restarted; reading stopped.")
            if rel not in {x[0] for x in self._db_files}:
                raise ProbeError("Database outside the bound file set.")
            conn = super()._open(rel)
            conn.execute("PRAGMA query_only=ON")
            return conn

    return RestrictedDB()


def contacts(db, name: str) -> list[dict]:
    conn = db._open("contact/contact.db")
    try:
        return [dict(r) for r in conn.execute(
            "SELECT username, nick_name, remark FROM contact "
            "WHERE nick_name=? OR remark=?", (name, name))]
    finally:
        conn.close()


def read_shard(db, room: str, shard: str, after: int | None):
    table = "Msg_" + hashlib.md5(room.encode()).hexdigest()
    conn = db._open(shard)
    try:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone():
            return 0 if after is None else []
        if after is None:
            return conn.execute(f'SELECT COALESCE(MAX(local_id),0) FROM "{table}"').fetchone()[0]
        rows = conn.execute(
            f'SELECT m.local_id,m.local_type,m.create_time,m.message_content,m.source,'
            f'n.user_name AS sender_username FROM "{table}" m '
            'LEFT JOIN Name2Id n ON n.rowid=m.real_sender_id '
            'WHERE m.local_id>? ORDER BY m.local_id LIMIT 200', (after,)).fetchall()
        out = []
        for r in rows:
            row = dict(r)
            row["shard"] = shard
            content = row.pop("message_content")
            row["content"] = db._friendly_content(content, "文本") if isinstance(content, bytes) else content
            out.append(row)
        return out
    except sqlite3.Error as exc:
        raise ProbeError("Database schema or read failed; no shard was silently skipped.") from exc
    finally:
        conn.close()
