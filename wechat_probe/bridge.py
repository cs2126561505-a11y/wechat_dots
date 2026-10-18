"""Bounded guest-only text publisher. No desktop or WeChat send operations."""
from __future__ import annotations

import json
import http.client
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .core import Journal, ProbeError, digest, mention_targets, select_room
from .isolation import require_live_admission
from . import replica


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def normalized(row, account, room, senders, started_at, now):
    """Unknown mention metadata stays unknown and never creates an event."""
    if row.get("local_type") != 1:
        return None
    text, sender = row.get("content"), row.get("sender_username")
    if not isinstance(text, str) or not 1 <= len(text) <= 4096:
        return None
    targets = mention_targets(row.get("source"))
    if targets is None or account not in targets or sender == account:
        return None
    if not isinstance(sender, str) or not sender or sender.isdigit():
        raise ProbeError("Platform sender identity is missing; publishing stopped.")
    # Initial live scope is explicitly identified controlled human senders.
    # Unknown participants are not guessed to be non-bots.
    if sender not in senders:
        return None
    at, local_id, shard = row.get("create_time"), row.get("local_id"), row.get("shard")
    if type(at) is not int or type(local_id) is not int or local_id <= 0 or not isinstance(shard, str):
        raise ProbeError("Message identity or time is invalid.")
    if not max(started_at, now - 300) <= at <= now + 30:
        return None
    message_id = "replica_" + digest(canonical([account, room, shard, local_id, at, sender]))
    return {"schema_version": "0.2", "event_id": "probe_" + digest(message_id),
            "occurred_at": datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z"),
            "channel": "wechat", "account_id": account, "room_id": room,
            "sender_id": sender, "message_id": message_id, "message_type": "text", "text": text,
            "mention_bot": True, "from_self": False, "known_bot": False}


class Outbox:
    """Commit the scan cursor and pending payload in the same transaction."""
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS binding (id INTEGER PRIMARY KEY CHECK(id=1), data TEXT NOT NULL, started INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS cursors (shard TEXT PRIMARY KEY, local_id INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (
              id TEXT PRIMARY KEY, hash TEXT NOT NULL, data TEXT, occurred INTEGER NOT NULL,
              status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, next_at REAL NOT NULL DEFAULT 0);
        """)

    def close(self):
        self.db.close()

    def initialize(self, binding, cursors, started_at):
        with self.db:
            saved = self.db.execute("SELECT data FROM binding WHERE id=1").fetchone()
            if saved:
                if saved[0] != canonical(binding):
                    raise ProbeError("Publisher binding changed; use a new private run.")
                return
            self.db.execute("INSERT INTO binding VALUES (1,?,?)", (canonical(binding), started_at))
            self.db.executemany("INSERT INTO cursors VALUES (?,?)", cursors.items())

    def cursors(self):
        return dict(self.db.execute("SELECT shard,local_id FROM cursors"))

    def started_at(self):
        return self.db.execute("SELECT started FROM binding WHERE id=1").fetchone()[0]

    def commit_batch(self, shard, cursor, events):
        with self.db:
            if shard not in self.cursors():
                raise ProbeError("An unbound message shard appeared.")
            for event in events:
                body = canonical(event)
                old = self.db.execute("SELECT hash FROM outbox WHERE id=?", (event["message_id"],)).fetchone()
                if old and old[0] != digest(body):
                    raise ProbeError("A message has conflicting content; publishing stopped.")
                at = int(datetime.fromisoformat(event["occurred_at"].replace("Z", "+00:00")).timestamp())
                self.db.execute("INSERT OR IGNORE INTO outbox(id,hash,data,occurred,status) VALUES (?,?,?,?,'pending')",
                                (event["message_id"], digest(body), body, at))
            if self.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] > 2000:
                raise ProbeError("Test publisher capacity reached; start a new reviewed run.")
            self.db.execute("UPDATE cursors SET local_id=MAX(local_id,?) WHERE shard=?", (cursor, shard))

    def deliver_one(self, post, now):
        with self.db:
            self.db.execute("UPDATE outbox SET status='expired',data=NULL WHERE status='pending' AND occurred<?", (now - 300,))
            row = self.db.execute("SELECT * FROM outbox WHERE status='pending' AND next_at<=? ORDER BY occurred,id LIMIT 1", (now,)).fetchone()
            if row is None:
                return False
            # Persist an attempt before HTTP. An interrupted request is retried
            # with the same message ID; gateway ingestion is idempotent.
            self.db.execute("UPDATE outbox SET attempts=attempts+1,next_at=? WHERE id=?",
                            (now + min(30, 2 ** min(row["attempts"] + 1, 5)), row["id"]))
        result = post(json.loads(row["data"]))
        if result is None:  # Only transport errors, 408/429 and 5xx are retried.
            return True
        if not isinstance(result, dict) or result.get("status") not in ("eligible", "duplicate", "filtered", "expired"):
            raise ProbeError("Gateway acknowledgement is invalid; publishing stopped.")
        if result["status"] in ("eligible", "duplicate") and not re.fullmatch(r"evt_[0-9a-f]{64}", str(result.get("event_id", ""))):
            raise ProbeError("Gateway event acknowledgement is missing.")
        with self.db:
            self.db.execute("UPDATE outbox SET status=?,data=NULL WHERE id=?", (result["status"], row["id"]))
        return True

    def summary(self):
        return {"publisher_counts": dict(self.db.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status")), "send_enabled": False}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def gateway_post(endpoint, token, event, expected_lease):
    # Queue entries belong to the read session that produced them. A new valid
    # lease (even for the same guest) must not authorize that session implicitly.
    if require_live_admission() != expected_lease:
        raise ProbeError("Publisher admission changed; publishing stopped.")
    request = urllib.request.Request(endpoint, data=canonical(event).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"}, method="POST")
    try:
        response = urllib.request.build_opener(NoRedirect()).open(request, timeout=10)
        with response:
            if response.status != 200:
                raise ProbeError("Gateway acknowledgement is invalid.")
            declared = response.getheader("Content-Length")
            if declared is not None:
                if not re.fullmatch(r"[0-9]{1,6}", declared) or int(declared) > 16384:
                    raise ProbeError("Gateway acknowledgement is invalid.")
                declared = int(declared)
            # read() can wait indefinitely when a peer trickles bytes before
            # each socket timeout. read1() returns after one underlying read,
            # letting us enforce a total body budget as well as a size limit.
            deadline, parts, length = time.monotonic() + 10, [], 0
            while length < 16385:
                if time.monotonic() >= deadline:
                    raise TimeoutError("Acknowledgement deadline exceeded.")
                part = response.read1(min(4096, 16385 - length))
                if not part:
                    break
                parts.append(part)
                length += len(part)
            if length > 16384:
                raise ProbeError("Gateway acknowledgement is invalid.")
            body = b"".join(parts)
            if declared is not None and length != declared:
                raise http.client.IncompleteRead(body)
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        if code in (408, 429) or 500 <= code <= 599:
            return None
        raise ProbeError("Gateway rejected the event; publishing stopped.") from None
    except (OSError, urllib.error.URLError, http.client.HTTPException):
        return None
    try:
        return json.loads(body)
    except (ValueError, UnicodeError):
        raise ProbeError("Gateway acknowledgement is invalid.") from None


def settings(run, config):
    path, token_path = run / "bridge.json", run / "bridge-token.txt"
    for item in (path, token_path, run / "bridge.sqlite3"):
        if item.is_symlink() or (item.exists() and item.resolve() != item.absolute()):
            raise ProbeError("Publisher files must remain in the private run directory.")
    data = json.loads(path.read_text("utf-8"))
    if not isinstance(data, dict) or set(data) != {"endpoint", "account_id", "room_id", "controlled_senders"}:
        raise ProbeError("Explicit private publisher configuration required.")
    parsed = urlsplit(data["endpoint"])
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.port not in (None, 443) or parsed.query or parsed.fragment or parsed.path != "/adapter/events"):
        raise ProbeError("Exact HTTPS gateway event endpoint required.")
    if (data["account_id"], data["room_id"]) != (config["account_id"], config["room_id"]):
        raise ProbeError("Publisher account or room does not match the read binding.")
    senders = data["controlled_senders"]
    if (not isinstance(senders, list) or not 1 <= len(senders) <= 10
            or any(not isinstance(s, str) or not re.fullmatch(r"[A-Za-z0-9_.@-]{1,256}", s) or s.isdigit() for s in senders)
            or len(set(senders)) != len(senders) or config["account_id"] in senders):
        raise ProbeError("Explicit controlled human sender identities required.")
    token = token_path.read_text("ascii").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", token):
        raise ProbeError("Private adapter credential is invalid.")
    return data, token


def publish(args, run: Path, emit):
    lease = require_live_admission()
    if not 1 <= args.seconds <= 900:
        raise ProbeError("Publishing duration must be between 1 and 900 seconds.")
    journal = Journal(run / "probe.sqlite3")
    try:
        config = journal.config()
    finally:
        journal.close()
    if config["candidate_commit"] != replica.COMMIT or config["client_version"] != replica.CLIENT_VERSION or config["room_name"] != "test-group":
        raise ProbeError("Read binding baseline changed.")
    data, token = settings(run, config)
    module = replica.load_db_module()
    db = replica.open_reader(module, Path(config["account_dir"]), config["process"], run / "cache")
    if select_room(replica.contacts(db, config["room_name"]), config["room_name"]) != config["room_id"]:
        raise ProbeError("Group identity changed; publishing stopped.")
    box = Outbox(run / "bridge.sqlite3")
    try:
        # New publishers take a fresh cursor; an existing outbox resumes its
        # durable cursor only under the identical account/process/settings.
        started = int(time.time())
        baseline = {rel: replica.read_shard(db, config["room_id"], rel, None)
                    for rel, _, _ in db._db_files if rel.startswith("message/")}
        box.initialize({"reader": config, "publisher": data}, baseline, started)
        deadline = time.monotonic() + args.seconds
        emit({"publishing": True, "seconds": args.seconds, "history_backfill": False, "send_enabled": False})
        while time.monotonic() < deadline:
            require_live_admission()
            db._refresh_db_files()
            for shard, cursor in box.cursors().items():
                rows = replica.read_shard(db, config["room_id"], shard, cursor)
                events = []
                for row in rows:
                    event = normalized(row, config["account_id"], config["room_id"], data["controlled_senders"], box.started_at(), time.time())
                    if event is not None:
                        events.append(event)
                if rows:
                    box.commit_batch(shard, rows[-1]["local_id"], events)
            for _ in range(10):
                if time.monotonic() >= deadline or not box.deliver_one(lambda event: gateway_post(data["endpoint"], token, event, lease), time.time()):
                    break
            time.sleep(1)
        emit(box.summary())
    finally:
        box.close()
