"""Durable authorization and state transitions; no desktop or network I/O here."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
EVENT_NAME = "wechat.group_message.created"
RECEIPT_FIELDS = frozenset({
    "account_id", "room_id", "message_id", "body_sha256", "occurred_at", "from_self"})


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt_shape(receipt) -> bool:
    return (isinstance(receipt, dict) and set(receipt) == RECEIPT_FIELDS
        and isinstance(receipt["message_id"], str) and 0 < len(receipt["message_id"]) <= 256
        and receipt["from_self"] is True
        and isinstance(receipt["body_sha256"], str) and len(receipt["body_sha256"]) == 64
        and all(c in "0123456789abcdef" for c in receipt["body_sha256"])
        and type(receipt["occurred_at"]) in (int, float)
        and (type(receipt["occurred_at"]) is int or math.isfinite(receipt["occurred_at"])))


class GatewayError(ValueError):
    """Public errors contain no message bodies, identifiers or credentials."""


@dataclass(frozen=True)
class Binding:
    principal: str
    account_id: str
    room_id: str


@dataclass(frozen=True)
class Policy:
    enabled: bool = False
    transport_admitted: bool = False
    notice_confirmed: bool = False
    send_enabled: bool = False
    message_ttl: int = 300
    draft_ttl: int = 900
    retention: int = 86400
    context_age: int = 600
    context_chars: int = 8000
    reply_chars: int = 800
    room_rate: int = 3
    global_rate: int = 10

    def __post_init__(self):
        for name in ("enabled", "transport_admitted", "notice_confirmed", "send_enabled"):
            if type(getattr(self, name)) is not bool:
                raise GatewayError("Invalid policy flag.")
        for name, maximum in (("message_ttl", 300), ("draft_ttl", 900),
                              ("retention", 86400), ("context_age", 600),
                              ("context_chars", 8000), ("reply_chars", 800),
                              ("room_rate", 3), ("global_rate", 10)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise GatewayError("Invalid policy limit.")
        if self.enabled and not (self.transport_admitted and self.notice_confirmed):
            raise GatewayError("Transport admission and room notice are required.")


class Store:
    def __init__(self, path: Path, bindings: tuple[Binding, ...], policy=Policy(), clock=time.time):
        self.bindings = frozenset(bindings)
        self.policy = policy
        self.clock = clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS events (
          id TEXT PRIMARY KEY, principal TEXT NOT NULL, account TEXT NOT NULL,
          room TEXT NOT NULL, message TEXT NOT NULL, occurred REAL NOT NULL,
          received REAL NOT NULL, data TEXT NOT NULL,
          UNIQUE(principal, account, room, message));
        CREATE TABLE IF NOT EXISTS drafts (
          id TEXT PRIMARY KEY, event TEXT NOT NULL REFERENCES events(id),
          principal TEXT NOT NULL, account TEXT NOT NULL, room TEXT NOT NULL,
          idem TEXT NOT NULL, text TEXT NOT NULL, hash TEXT NOT NULL,
          policy TEXT NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
          status TEXT NOT NULL, approved_by TEXT, approved REAL,
          attempted REAL, receipt TEXT, execute_before REAL,
          UNIQUE(principal, event), UNIQUE(principal, idem));
        CREATE TABLE IF NOT EXISTS subscriptions (
          id TEXT PRIMARY KEY, principal TEXT NOT NULL, account TEXT NOT NULL,
          room TEXT NOT NULL, url TEXT NOT NULL, secret TEXT NOT NULL,
          old_secret TEXT, rotate_until REAL, expires REAL NOT NULL,
          generation INTEGER NOT NULL DEFAULT 1, grant_family TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS deliveries (
          subscription TEXT NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
          event TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
          status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
          next_at REAL NOT NULL, lease_until REAL, last_code INTEGER,
          PRIMARY KEY(subscription,event));
        CREATE TABLE IF NOT EXISTS send_receipts (
          account TEXT NOT NULL, room TEXT NOT NULL, message TEXT NOT NULL,
          draft TEXT NOT NULL UNIQUE REFERENCES drafts(id), receipt_hash TEXT NOT NULL,
          PRIMARY KEY(account,room,message));
        CREATE TABLE IF NOT EXISTS send_receipt_gaps (
          account TEXT NOT NULL, room TEXT NOT NULL,
          PRIMARY KEY(account,room));
        """)
        self.event_schema = json.loads((ROOT / "contracts/message-event.schema.json").read_text("utf-8"))
        self.validator = Draft202012Validator(self.event_schema, format_checker=FormatChecker())
        # After a crash, desktop execution may already have happened. Never resend.
        with self.transaction():
            self._migrate_send_state()
            self.db.execute("UPDATE drafts SET status='delivery_unknown' WHERE status='sending'")

    def _migrate_send_state(self):
        # Keep identity/hash tombstones independently of the prunable receipt body.
        if "execute_before" not in {r["name"] for r in self.db.execute("PRAGMA table_info(drafts)")}:
            self.db.execute("ALTER TABLE drafts ADD COLUMN execute_before REAL")
        self.db.execute("""UPDATE drafts SET execute_before=MIN(expires,attempted+120)
            WHERE attempted IS NOT NULL AND execute_before IS NULL""")
        rows = self.db.execute("""SELECT d.* FROM drafts d LEFT JOIN send_receipts s ON s.draft=d.id
            WHERE d.status='sent' AND s.draft IS NULL""").fetchall()
        for row in rows:
            try:
                receipt = json.loads(row["receipt"] or "null")
            except (TypeError, ValueError):
                receipt = None
            if (_receipt_shape(receipt) and receipt["account_id"] == row["account"]
                    and receipt["room_id"] == row["room"]):
                try:
                    self.db.execute("INSERT INTO send_receipts VALUES (?,?,?,?,?)", (
                        row["account"], row["room"], receipt["message_id"], row["id"], digest(receipt)))
                    continue
                except sqlite3.IntegrityError:
                    pass
            # Legacy pruning or conflicting receipts can make old message identities
            # unrecoverable. Preserve reads, but fail closed for future room sends.
            self.db.execute("INSERT OR IGNORE INTO send_receipt_gaps VALUES (?,?)",
                            (row["account"], row["room"]))

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except BaseException:
                self.db.rollback()
                raise

    def close(self):
        with self.lock:
            self.db.close()

    def authorize(self, binding: Binding):
        if not self.policy.enabled or binding not in self.bindings:
            raise GatewayError("Access is disabled or outside the authorized room.")

    def policy_hash(self):
        return digest([asdict(self.policy), sorted((b.principal, b.account_id, b.room_id) for b in self.bindings)])

    def ingest(self, binding: Binding, event: dict) -> dict:
        self.authorize(binding)
        if not self.validator.is_valid(event):
            raise GatewayError("Invalid normalized event.")
        if (event["account_id"], event["room_id"]) != (binding.account_id, binding.room_id):
            raise GatewayError("Adapter identity does not match the bound room.")
        if event["from_self"] or event["known_bot"] or event["mention_bot"] is not True:
            return {"status": "filtered"}
        now = self.clock()
        occurred = datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).timestamp()
        if not now - self.policy.message_ttl <= occurred <= now + 30:
            return {"status": "expired"}
        event = dict(event)
        event["event_id"] = "evt_" + digest([binding.principal, "wechat", binding.account_id,
                                              binding.room_id, event["message_id"]])
        body = canonical(event)
        with self.transaction():
            existing = self.db.execute("SELECT data FROM events WHERE id=?", (event["event_id"],)).fetchone()
            if existing:
                if existing[0] != body:
                    raise GatewayError("An existing message has conflicting content.")
                return {"status": "duplicate", "event_id": event["event_id"]}
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)", (
                event["event_id"], binding.principal, binding.account_id, binding.room_id,
                event["message_id"], occurred, now, body))
            self.db.execute("""INSERT INTO deliveries(subscription,event,status,next_at)
                SELECT id,?,'pending',? FROM subscriptions
                WHERE principal=? AND account=? AND room=? AND expires>?""",
                (event["event_id"], now, binding.principal, binding.account_id, binding.room_id, now))
        return {"status": "eligible", "event_id": event["event_id"]}

    def event_row(self, principal: str, event_id: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM events WHERE principal=? AND id=?", (principal, event_id)).fetchone()
        if row is None or row["received"] < self.clock() - self.policy.retention:
            raise GatewayError("Event unavailable.")
        self.authorize(Binding(principal, row["account"], row["room"]))
        return row

    def get_event(self, principal: str, event_id: str):
        return json.loads(self.event_row(principal, event_id)["data"])

    def get_context(self, principal: str, event_id: str, limit=20):
        if type(limit) is not int or not 1 <= limit <= 20:
            raise GatewayError("Invalid context limit.")
        event = self.event_row(principal, event_id)
        with self.lock:
            rows = self.db.execute("""SELECT data FROM events WHERE principal=? AND account=?
                AND room=? AND occurred>=? AND occurred<=? AND received>=?
                ORDER BY occurred DESC,id DESC LIMIT ?""", (principal, event["account"], event["room"],
                self.clock() - self.policy.context_age, self.clock() + 30,
                self.clock() - self.policy.retention, limit)).fetchall()
        result, remaining = [], self.policy.context_chars
        for row in rows:
            item = json.loads(row[0])
            if len(item["text"]) > remaining:
                break
            remaining -= len(item["text"])
            result.append(item)
        return {"events": list(reversed(result)), "captured_triggered_messages_only": True}

    def propose(self, principal: str, event_id: str, text: str, idempotency_key: str):
        event = self.event_row(principal, event_id)
        if not isinstance(text, str) or not text.strip() or len(text) > self.policy.reply_chars:
            raise GatewayError("Reply text is empty or too long.")
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
            raise GatewayError("Invalid idempotency key.")
        now = self.clock()
        content_hash = digest([principal, event_id, event["account"], event["room"], text])
        draft_id = "draft_" + digest([principal, event_id])
        with self.transaction():
            previous = self.db.execute("SELECT * FROM drafts WHERE principal=? AND (event=? OR idem=?)",
                                       (principal, event_id, idempotency_key)).fetchall()
            if previous:
                if len(previous) != 1 or previous[0]["hash"] != content_hash or previous[0]["idem"] != idempotency_key:
                    raise GatewayError("Reply slot or idempotency key already has different content.")
                return self.draft_view(previous[0])
            if now - event["occurred"] > self.policy.message_ttl:
                raise GatewayError("Event expired before draft creation.")
            self.db.execute("""INSERT INTO drafts
                (id,event,principal,account,room,idem,text,hash,policy,created,expires,status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,'draft_pending')""", (
                draft_id, event_id, principal, event["account"], event["room"], idempotency_key,
                text, content_hash, self.policy_hash(), now, now + self.policy.draft_ttl))
            return self.draft_view(self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone())

    @staticmethod
    def draft_view(row):
        return {"draft_id": row["id"], "event_id": row["event"], "text": row["text"],
                "content_hash": row["hash"], "status": row["status"], "expires_at": iso(row["expires"])}

    def review(self, principal: str, draft_id: str):
        with self.lock:
            row = self.db.execute("SELECT * FROM drafts WHERE principal=? AND id=?", (principal, draft_id)).fetchone()
        if row is None:
            raise GatewayError("Draft unavailable.")
        self.event_row(principal, row["event"])
        return dict(self.draft_view(row), account_id=row["account"], room_id=row["room"])

    def approve(self, principal: str, draft_id: str, content_hash: str, owner: str):
        # This method is reachable only through the separate owner-authenticated route.
        self.review(principal, draft_id)
        with self.transaction():
            row = self.db.execute("SELECT * FROM drafts WHERE id=?", (draft_id,)).fetchone()
            if row["hash"] != content_hash or row["policy"] != self.policy_hash() or row["expires"] <= self.clock():
                raise GatewayError("Approval content, policy or expiry no longer matches.")
            if row["status"] not in ("draft_pending", "approved"):
                raise GatewayError("Draft is no longer awaiting approval.")
            self.db.execute("UPDATE drafts SET status='approved',approved_by=?,approved=? WHERE id=?",
                            (owner, self.clock(), draft_id))
        return {"draft_id": draft_id, "status": "approved"}

    def claim_send(self, binding: Binding, draft_id: str):
        self.authorize(binding)
        if not self.policy.send_enabled:
            raise GatewayError("Desktop sending is disabled.")
        with self.transaction():
            now = self.clock()
            row = self.db.execute("SELECT * FROM drafts WHERE id=? AND principal=? AND account=? AND room=?",
                (draft_id, binding.principal, binding.account_id, binding.room_id)).fetchone()
            if row is None or row["status"] != "approved" or not row["approved_by"]:
                raise GatewayError("Independently approved draft required.")
            if row["expires"] <= now or row["policy"] != self.policy_hash():
                raise GatewayError("Approval expired or policy changed.")
            uncertain = self.db.execute("SELECT 1 FROM drafts WHERE account=? AND room=? AND status IN ('sending','delivery_unknown')",
                                        (binding.account_id, binding.room_id)).fetchone()
            gap = self.db.execute("SELECT 1 FROM send_receipt_gaps WHERE account=? AND room=?",
                                  (binding.account_id, binding.room_id)).fetchone()
            counts = self.db.execute("""SELECT COUNT(*),SUM(CASE WHEN account=? AND room=? THEN 1 ELSE 0 END)
                FROM drafts WHERE attempted>=?""", (binding.account_id, binding.room_id, now - 60)).fetchone()
            if uncertain or gap or counts[0] >= self.policy.global_rate or (counts[1] or 0) >= self.policy.room_rate:
                raise GatewayError("Room delivery is blocked or rate limited.")
            execute_before = min(row["expires"], now + 120)
            self.db.execute("UPDATE drafts SET status='sending',attempted=?,execute_before=? WHERE id=?",
                            (now, execute_before, draft_id))
            return {"draft_id": draft_id, "account_id": row["account"], "room_id": row["room"],
                    "text": row["text"], "content_hash": row["hash"], "attempted_at": now,
                    "execute_before": execute_before}

    def finish_send(self, binding: Binding, draft_id: str, receipt: dict | None):
        # Record an outcome even if access has since been revoked; this never initiates a send.
        with self.transaction():
            row = self.db.execute("SELECT * FROM drafts WHERE id=? AND principal=? AND account=? AND room=?",
                (draft_id, binding.principal, binding.account_id, binding.room_id)).fetchone()
            if row is not None and row["status"] == "sent" and _receipt_shape(receipt):
                previous = self.db.execute("SELECT receipt_hash FROM send_receipts WHERE draft=?",
                                           (draft_id,)).fetchone()
                if previous is not None and previous[0] == digest(receipt):
                    return {"draft_id": draft_id, "status": "sent"}
            if row is None or row["status"] != "sending":
                raise GatewayError("No matching send attempt.")
            verified = _receipt_shape(receipt)
            if verified:
                verified = (receipt["account_id"] == row["account"] and receipt["room_id"] == row["room"]
                    and receipt["body_sha256"] == hashlib.sha256(row["text"].encode()).hexdigest()
                    and row["execute_before"] is not None
                    and row["attempted"] - 2 <= receipt["occurred_at"] <= min(row["execute_before"], self.clock() + 30))
            if verified:
                try:
                    self.db.execute("INSERT INTO send_receipts VALUES (?,?,?,?,?)", (
                        row["account"], row["room"], receipt["message_id"], draft_id, digest(receipt)))
                except sqlite3.IntegrityError:
                    verified = False
            status = "sent" if verified else "delivery_unknown"
            self.db.execute("UPDATE drafts SET status=?,receipt=? WHERE id=?",
                            (status, canonical(receipt) if verified else None, draft_id))
            return {"draft_id": draft_id, "status": status}

    def prune(self):
        with self.transaction():
            self.db.execute("DELETE FROM subscriptions WHERE expires<=?", (self.clock(),))
            self.db.execute("UPDATE subscriptions SET old_secret=NULL,rotate_until=NULL WHERE rotate_until<=?", (self.clock(),))
            self.db.execute("UPDATE drafts SET status='expired' WHERE expires<=? AND status IN ('draft_pending','approved')", (self.clock(),))
            # Keep non-content draft/receipt tombstones: unresolved attempts block
            # the room, and confirmed message identities remain reserved forever.
            self.db.execute("UPDATE drafts SET text='',receipt=NULL WHERE created<?", (self.clock() - self.policy.retention,))
            self.db.execute("UPDATE events SET data='{}' WHERE received<?", (self.clock() - self.policy.retention,))
