"""Pure validation and durable, content-minimising probe state."""
from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import xml.etree.ElementTree as ET
from pathlib import Path


class ProbeError(RuntimeError):
    """A failed prerequisite. Its message must contain no private data."""


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def select_room(rows: list[dict], name: str) -> str:
    matches = {r["username"] for r in rows
               if r.get("username", "").endswith("@chatroom")
               and name in (r.get("nick_name"), r.get("remark"))}
    if len(matches) != 1:
        raise ProbeError("Target group must have exactly one exact identity match.")
    return next(iter(matches))


def mention_targets(source: object) -> tuple[str, ...] | None:
    """Decode only explicit platform-source XML, never message body text.

    This is a candidate parser, not proof that a particular client stores this
    format. Missing, malformed or unfamiliar metadata remains UNKNOWN.
    """
    if isinstance(source, bytes):
        if len(source) > 16384:
            return None
        if source.startswith(b"\x28\xb5\x2f\xfd"):
            # Observed in the pinned 4.1.15.13 guest. Never use an unbounded
            # decompression call: compressed metadata is still untrusted input.
            try:
                import zstandard
            except ImportError:
                return None
            try:
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(source)) as reader:
                    source = reader.read(16385)
                if len(source) > 16384:
                    return None
            except (ValueError, zstandard.ZstdError):
                return None
        try:
            source = source.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None
    if not isinstance(source, str) or len(source) > 16384:
        return None
    # ElementTree supports CDATA; reject DTDs and entity declarations.
    if re.search(r"<!\s*(DOCTYPE|ENTITY)", source, re.I):
        return None
    try:
        root = ET.fromstring(source)
    except (ET.ParseError, ValueError):
        return None
    if root.tag != "msgsource":
        return None
    nodes = root.findall("atuserlist")
    if len(nodes) != 1 or len(nodes[0]):
        return None
    values = tuple(x.strip() for x in (nodes[0].text or "").split(",") if x.strip())
    if any(not re.fullmatch(r"[A-Za-z0-9_@.\-]+", x) for x in values):
        return None
    return values


def observation(row: dict, account: str, room: str, marker: str) -> dict | None:
    """Keep only labelled synthetic text; identity never falls back to a nickname."""
    if row.get("local_type") != 1:
        return None
    content = row.get("content")
    if not isinstance(content, str):
        return None
    cases = [case for case in ("PLAIN", "REAL_AT", "FAKE_AT", "SELF", "REPLY")
             if re.search(r"(?<![A-Za-z0-9_])" + re.escape(marker + "_" + case)
                          + r"(?![A-Za-z0-9_])", content)]
    if len(cases) != 1:
        return None
    sender = row.get("sender_username")
    if not isinstance(sender, str) or not sender or sender.isdigit():
        raise ProbeError("Platform sender identity is missing; observation rejected.")
    targets = mention_targets(row.get("source"))
    return {
        "event_id": digest(json.dumps([account, room, row["shard"], row["local_id"]])),
        "case": cases[0], "sender_alias": digest(sender)[:12],
        "from_self": sender == account,
        "mention_bot": None if targets is None else account in targets,
        "body_sha256": digest(content),
        "occurred_at": int(row["create_time"]),
    }


class Journal:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS config (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (shard TEXT PRIMARY KEY, local_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observations (event_id TEXT PRIMARY KEY, data TEXT NOT NULL);
        """)

    def close(self):
        self.conn.close()

    def initialize(self, config: dict, cursors: dict[str, int]):
        if self.conn.execute("SELECT 1 FROM config").fetchone():
            raise ProbeError("This run is already bound; use a new private run directory.")
        with self.conn:
            self.conn.execute("INSERT INTO config VALUES (1, ?)", (json.dumps(config),))
            self.conn.executemany("INSERT INTO cursors VALUES (?, ?)", cursors.items())

    def config(self) -> dict:
        row = self.conn.execute("SELECT data FROM config WHERE id=1").fetchone()
        if row is None:
            raise ProbeError("Bind a test account and group before watching.")
        return json.loads(row[0])

    def cursors(self) -> dict[str, int]:
        return dict(self.conn.execute("SELECT shard, local_id FROM cursors"))

    def commit_batch(self, shard: str, local_id: int, records: list[dict]):
        with self.conn:
            if not self.conn.execute("SELECT 1 FROM cursors WHERE shard=?", (shard,)).fetchone():
                raise ProbeError("An unbound shard appeared; restart with an explicit baseline.")
            for record in records:
                self.conn.execute("INSERT OR IGNORE INTO observations VALUES (?, ?)",
                                  (record["event_id"], json.dumps(record)))
            self.conn.execute("UPDATE cursors SET local_id=MAX(local_id, ?) WHERE shard=?",
                              (local_id, shard))

    def report(self) -> dict:
        rows = [json.loads(r[0]) for r in self.conn.execute("SELECT data FROM observations")]
        valid = {
            "PLAIN": lambda r: not r["from_self"] and r["mention_bot"] is False,
            "REAL_AT": lambda r: not r["from_self"] and r["mention_bot"] is True,
            "FAKE_AT": lambda r: not r["from_self"] and r["mention_bot"] is False,
            "SELF": lambda r: r["from_self"],
        }
        controls = {}
        for case, check in valid.items():
            seen = [r for r in rows if r["case"] == case]
            controls[case] = "passed" if seen and all(check(r) for r in seen) else (
                "inconclusive_or_failed" if seen else "not_observed")
        return {"observations": len(rows), "controls": controls,
                "mention_controls_passed": all(controls[c] == "passed" for c in ("PLAIN", "REAL_AT", "FAKE_AT")),
                "send_enabled": False, "dot_enabled": False,
                "send_blocker": "Desktop target identity binding has not passed admission.",
                "records": rows}
