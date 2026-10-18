"""Pure one-shot send checks and a private local attempt ledger; no UI/network I/O.

UI evidence and scoped DB reads must come from separately reviewed collectors.
This module validates their consistency, not their authenticity. A window title
never supplies a room ID. Neither a read lease nor a SendPlan enables sending.
An executor must recheck live admission, collect current UI evidence, call
consume immediately before its single action, and never retry that action.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
import secrets
import sqlite3

from .core import ProbeError, digest


def _json(value):
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ProbeError("Send evidence cannot be serialized consistently.") from None


def _check(condition, message="Send evidence is incomplete or inconsistent."):
    if not condition:
        raise ProbeError(message)


def _time(value):
    return type(value) in (int, float) and (type(value) is int or math.isfinite(value)) and 0 <= value < 1e12


def _identity(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", value) and not value.isdigit()


def _run(run, process):
    _check(isinstance(run, dict) and isinstance(process, dict))
    _check(set(process) == {"pid", "started", "exe"} and type(process["pid"]) is int and process["pid"] > 0)
    _check(_time(process["started"]) and isinstance(process["exe"], str) and bool(process["exe"]))
    _check(run.get("process") == process and run.get("room_name") == "test-group"
           and run.get("client_version") == "4.1.15.13" and _time(run.get("started_at")))
    _check(_identity(run.get("account_id")) and _identity(run.get("room_id"))
           and run["room_id"].endswith("@chatroom") and not run["account_id"].endswith("@chatroom"))
    return digest(_json(run))


def _baseline(value):
    _check(isinstance(value, dict) and 1 <= len(value) <= 32)
    _check(all(isinstance(k, str) and re.fullmatch(r"message/message_[0-9]+\.db", k)
               and type(v) is int and v >= 0 for k, v in value.items()))
    return tuple(sorted(value.items()))


def unique_target(contacts, name, expected_room):
    """Input must include every exact nickname/remark hit, including private chats."""
    _check(isinstance(contacts, list) and name == "test-group")
    matches = []
    for row in contacts:
        _check(isinstance(row, dict))
        if name in (row.get("nick_name"), row.get("remark")):
            _check(_identity(row.get("username")))
            matches.append(row["username"])
    _check(len(matches) == 1 and matches[0] == expected_room
           and expected_room.endswith("@chatroom"), "Target contact is not an exact, unique group.")
    return expected_room


@dataclass(frozen=True)
class Challenge:
    marker: str = field(repr=False)
    peer: str = field(repr=False)
    issued_at: float
    run_hash: str
    baseline: tuple


def make_challenge(run, peer, baseline, now):
    _check(isinstance(run, dict))
    run_hash = _run(run, run.get("process"))
    _check(_time(now) and now >= run["started_at"] and _identity(peer)
           and peer != run["account_id"] and not peer.endswith("@chatroom"))
    return Challenge("WXDOTS_" + secrets.token_hex(16), peer, now, run_hash, _baseline(baseline))


def _ui(ui, process_json, title, marker, text, now, hwnd=None):
    _check(isinstance(ui, dict), "Current UI evidence is required.")
    _check(ui.get("source") in ("native-uia", "human-reviewed-screenshot")
           and isinstance(ui.get("evidence_hash"), str)
           and re.fullmatch(r"[0-9a-f]{64}", ui["evidence_hash"]))
    _check(_time(ui.get("observed_at")) and 0 <= now - ui["observed_at"] <= 5)
    _check(_json(ui.get("process")) == process_json and type(ui.get("hwnd")) is int and ui["hwnd"] > 0)
    _check(hwnd is None or ui["hwnd"] == hwnd)
    _check(ui.get("chat_type") == "group" and ui.get("title") == title
           and ui.get("visible_marker") == marker and ui.get("input_text") == text
           and ui.get("foreground") is True and ui.get("input_focused") is True)
    return ui["hwnd"]


@dataclass(frozen=True)
class SendPlan:
    draft_id: str
    account_id: str = field(repr=False)
    room_id: str = field(repr=False)
    text: str = field(repr=False)
    body_sha256: str
    command_hash: str
    attempted_at: float
    execute_before: float
    run_hash: str
    process_json: str = field(repr=False)
    baseline: tuple
    challenge: Challenge = field(repr=False)
    hwnd: int
    evidence_hash: str

    def fingerprint(self):
        return digest(_json([self.draft_id, self.account_id, self.room_id, self.body_sha256,
            self.command_hash, self.attempted_at, self.execute_before, self.run_hash,
            self.process_json, self.baseline, self.challenge.marker, self.challenge.peer,
            self.challenge.issued_at, self.challenge.baseline, self.hwnd, self.evidence_hash]))


def prepare_send(command, run, process, contacts, baseline, challenge, marker_db, ui, now):
    """marker_db explicitly carries query scope plus a standard replica message row."""
    run_hash = _run(run, process)
    _check(isinstance(command, dict) and set(command) == {"draft_id", "account_id", "room_id", "text",
        "content_hash", "attempted_at", "execute_before"})
    _check(isinstance(command["draft_id"], str) and re.fullmatch(r"draft_[0-9a-f]{64}", command["draft_id"]))
    _check(isinstance(command["content_hash"], str) and re.fullmatch(r"[0-9a-f]{64}", command["content_hash"]))
    _check((command["account_id"], command["room_id"]) == (run["account_id"], run["room_id"]))
    text = command["text"]
    _check(isinstance(text, str) and 1 <= len(text) <= 800 and bool(text.strip()))
    _check(all(_time(x) for x in (now, command["attempted_at"], command["execute_before"])))
    _check(run["started_at"] <= command["attempted_at"] <= now < command["execute_before"]
           <= command["attempted_at"] + 120)
    unique_target(contacts, run["room_name"], run["room_id"])
    cursors = dict(_baseline(baseline))
    _check(isinstance(challenge, Challenge) and challenge.run_hash == run_hash
           and re.fullmatch(r"WXDOTS_[0-9a-f]{32}", challenge.marker)
           and _identity(challenge.peer) and challenge.peer != run["account_id"]
           and not challenge.peer.endswith("@chatroom") and _time(challenge.issued_at)
           and run["started_at"] <= challenge.issued_at <= now and now - challenge.issued_at <= 120)
    before = dict(_baseline(dict(challenge.baseline)))
    _check(cursors.keys() == before.keys() and all(cursors[k] >= before[k] for k in cursors))
    _check(isinstance(marker_db, dict) and marker_db.get("account_id") == run["account_id"]
           and marker_db.get("room_id") == run["room_id"] and marker_db.get("process") == process)
    row = marker_db.get("row")
    _check(isinstance(row, dict) and row.get("shard") in cursors and row.get("local_type") == 1
           and type(row.get("local_id")) is int and before[row["shard"]] < row["local_id"] <= cursors[row["shard"]]
           and row.get("sender_username") == challenge.peer and row.get("content") == challenge.marker
           and type(row.get("create_time")) is int
           and max(int(challenge.issued_at), run["started_at"]) <= row["create_time"] <= now + 2)
    process_json = _json(process)
    hwnd = _ui(ui, process_json, run["room_name"], challenge.marker, text, now)
    return SendPlan(command["draft_id"], run["account_id"], run["room_id"], text, digest(text),
        digest(_json(command)), command["attempted_at"], command["execute_before"], run_hash,
        process_json, tuple(sorted(cursors.items())), challenge, hwnd, ui["evidence_hash"])


class ActionLedger:
    """Keep this DB in the reviewed private run; deleting it loses local history."""
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.execute("""CREATE TABLE IF NOT EXISTS consumed (
            draft TEXT PRIMARY KEY, plan_hash TEXT NOT NULL, marker_hash TEXT NOT NULL UNIQUE,
            consumed_at REAL NOT NULL, execute_before REAL NOT NULL, evidence_hash TEXT NOT NULL)""")

    def close(self):
        self.db.close()

    def consume(self, plan, run, process, ui, now):
        """Commit before returning. The caller may then perform exactly one action."""
        _check(isinstance(plan, SendPlan) and _run(run, process) == plan.run_hash
               and _json(process) == plan.process_json and digest(plan.text) == plan.body_sha256
               and (plan.account_id, plan.room_id) == (run["account_id"], run["room_id"])
               and plan.challenge.run_hash == plan.run_hash)
        _check(all(_time(x) for x in (now, plan.attempted_at, plan.execute_before))
               and run["started_at"] <= plan.attempted_at <= now < plan.execute_before <= plan.attempted_at + 120
               and 0 <= now - plan.challenge.issued_at <= 120)
        _ui(ui, plan.process_json, run["room_name"], plan.challenge.marker, plan.text, now, plan.hwnd)
        try:
            with self.db:
                self.db.execute("INSERT INTO consumed VALUES (?,?,?,?,?,?)", (plan.draft_id, plan.fingerprint(),
                    digest(plan.challenge.marker), now, plan.execute_before, ui["evidence_hash"]))
        except sqlite3.IntegrityError:
            raise ProbeError("Draft or marker already consumed; desktop action must not repeat.") from None
        return {"draft_id": plan.draft_id, "consumed_at": now, "execute_before": plan.execute_before}

    def confirm_receipt(self, plan, scan, now):
        """Scan includes every bound shard with after/complete/rows query evidence.

        The collector must exhaust pagination under the bound account/process and
        real room query. Missing/partial/ambiguous evidence never yields a receipt.
        Failure leaves the action consumed; only read-only confirmation may retry.
        """
        _check(isinstance(plan, SendPlan))
        saved = self.db.execute("SELECT plan_hash,consumed_at FROM consumed WHERE draft=?", (plan.draft_id,)).fetchone()
        _check(saved is not None and saved[0] == plan.fingerprint())
        _check(isinstance(scan, dict) and scan.get("account_id") == plan.account_id
               and scan.get("room_id") == plan.room_id and _json(scan.get("process")) == plan.process_json)
        _check(_time(now) and _time(scan.get("scanned_at")) and saved[1] <= scan["scanned_at"] <= now
               and now - scan["scanned_at"] <= 5)
        baseline = dict(plan.baseline)
        shards = scan.get("shards")
        _check(isinstance(shards, dict) and shards.keys() == baseline.keys())
        matches, seen, count = [], set(), 0
        for shard, cursor in baseline.items():
            batch = shards[shard]
            _check(isinstance(batch, dict) and batch.get("after") == cursor and batch.get("complete") is True
                   and isinstance(batch.get("rows"), list))
            for row in batch["rows"]:
                count += 1
                _check(count <= 2000 and isinstance(row, dict) and row.get("shard") == shard
                       and type(row.get("local_id")) is int and row["local_id"] > cursor
                       and type(row.get("create_time")) is int)
                identity = (shard, row["local_id"])
                _check(identity not in seen)
                seen.add(identity)
                if row.get("local_type") != 1 or row.get("content") != plan.text:
                    continue
                _check(_identity(row.get("sender_username")))
                if row["sender_username"] == plan.account_id:
                    _check(plan.attempted_at - 2 <= row["create_time"] <= min(plan.execute_before, now + 2))
                    matches.append(row)
        _check(len(matches) == 1, "Exactly one new self-sent message is required; outcome remains unknown.")
        row = matches[0]
        message_id = "replica_" + digest(_json([plan.account_id, plan.room_id, row["shard"],
            row["local_id"], row["create_time"], row["sender_username"]]))
        return {"account_id": plan.account_id, "room_id": plan.room_id, "message_id": message_id,
                "body_sha256": plan.body_sha256, "occurred_at": row["create_time"], "from_self": True}
