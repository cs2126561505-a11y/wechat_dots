"""Run with python -m wechat_probe; live console output is always redacted."""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

from .core import Journal, ProbeError, digest, observation, select_room
from . import replica
from .isolation import assessment, require_live_admission


def emit(data):
    print(json.dumps(data, ensure_ascii=False), flush=True)


def private_run(name: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", name):
        raise ProbeError("Run name must contain only letters, digits, underscores or hyphens.")
    root = replica.ROOT / ".runtime"
    run = root / name
    for p in (root, run):
        if p.is_symlink() or (p.exists() and p.resolve() != p.absolute()):
            raise ProbeError("Private state path must not be redirected.")
        p.mkdir(exist_ok=True)
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW
        result = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                                capture_output=True, text=True, check=True, creationflags=flags)
        sid = next(csv.reader(io.StringIO(result.stdout)))[1]
        if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
            raise ProbeError("Cannot establish private directory ownership.")
        subprocess.run(["icacls", str(run), "/inheritance:r", "/grant:r",
                        f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"],
                       capture_output=True, check=True, creationflags=flags)
    else:
        run.chmod(0o700)
    return run


def audit():
    replica.verify_source()
    return {
        "candidate": "wechatauto-replica", "version": replica.VERSION,
        "commit": replica.COMMIT, "db_sha256": replica.DB_SHA256,
        "required_client_version": replica.CLIENT_VERSION,
        "read_admission": "requires_current_private_guest_lease",
        "isolation": assessment(),
        "upstream_default_constructor": "excluded: account fallback and key caches",
        "upstream_gui": "excluded: process-memory writes and ambiguous display-name routing",
        "mention_metadata": "unverified; requires real/fake/plain controls",
        "private_cache_scope": "whole selected database files, potentially multiple chats in the test account",
        "key_persistence": False, "send_enabled": False, "dot_enabled": False,
    }


def discover():
    module = replica.load_db_module()
    process = replica.process_binding()
    accounts = replica.account_paths(module)
    return {"client_version": replica.CLIENT_VERSION, "main_process_count": 1,
            "account_count": len(accounts),
            "accounts": [{"index": i, "alias": digest(str(p))[:12]} for i, p in enumerate(accounts)],
            "isolation": assessment(len(accounts)),
            "note": "Directory discovery only; no keys or chat messages read."}


def bind(args, run: Path):
    require_live_admission()
    if args.room != "test-group":
        raise ProbeError("This admission only covers test-group.")
    journal = Journal(run / "probe.sqlite3")
    try:
        if journal.conn.execute("SELECT 1 FROM config").fetchone():
            raise ProbeError("This run is already bound; choose a new run name.")
        module = replica.load_db_module()
        accounts = replica.account_paths(module)
        if args.account_index < 0 or args.account_index >= len(accounts):
            raise ProbeError("Account index is not in the discovered directory list.")
        if digest(str(accounts[args.account_index]))[:12] != args.account_alias:
            raise ProbeError("Account alias changed; repeat discovery before binding.")
        process = replica.process_binding()
        db = replica.open_reader(module, accounts[args.account_index], process, run / "cache")
        room = select_room(replica.contacts(db, args.room), args.room)
        account = db.wxid
        # A real contact row is mandatory; upstream get_self_info otherwise invents a fallback.
        conn = db._open("contact/contact.db")
        try:
            if not conn.execute("SELECT 1 FROM contact WHERE username=?", (account,)).fetchone():
                raise ProbeError("Bound account identity has no corresponding contact record.")
        finally:
            conn.close()
        shards = [rel for rel, _, _ in db._db_files if rel.startswith("message/")]
        cursors = {shard: replica.read_shard(db, room, shard, None) for shard in shards}
        marker = "WXDOTS_" + secrets.token_hex(4).upper()
        config = {"account_dir": str(accounts[args.account_index]), "account_id": account,
                  "room_id": room, "room_name": args.room, "process": process,
                  "started_at": int(time.time()), "marker": marker,
                  "candidate_commit": replica.COMMIT, "client_version": replica.CLIENT_VERSION}
        journal.initialize(config, cursors)
        emit({"bound": True, "account_alias": digest(account)[:12], "room_alias": digest(room)[:12],
              "room_name": args.room, "shards": len(shards), "marker": marker,
              "history_backfill": False, "send_enabled": False,
              "controls": {"ordinary_text": marker + "_PLAIN", "real_mention": marker + "_REAL_AT",
                           "typed_fake_mention": marker + "_FAKE_AT", "own_message": marker + "_SELF"}})
    finally:
        journal.close()


def watch(args, run: Path):
    require_live_admission()
    if not 1 <= args.seconds <= 900:
        raise ProbeError("Watch duration must be between 1 and 900 seconds.")
    journal = Journal(run / "probe.sqlite3")
    try:
        config = journal.config()
        if config["candidate_commit"] != replica.COMMIT or config["client_version"] != replica.CLIENT_VERSION:
            raise ProbeError("Probe baseline changed; start a new run.")
        module = replica.load_db_module()
        db = replica.open_reader(module, Path(config["account_dir"]), config["process"], run / "cache")
        room = select_room(replica.contacts(db, config["room_name"]), config["room_name"])
        if room != config["room_id"]:
            raise ProbeError("Group identity changed; reading stopped.")
        deadline = time.monotonic() + args.seconds
        emit({"watching": True, "seconds": args.seconds, "marker": config["marker"], "send_enabled": False})
        while time.monotonic() < deadline:
            db._refresh_db_files()
            for shard, cursor in journal.cursors().items():
                rows = replica.read_shard(db, room, shard, cursor)
                records = []
                for row in rows:
                    if int(row["create_time"]) < config["started_at"]:
                        continue
                    record = observation(row, config["account_id"], room, config["marker"])
                    if record is not None:
                        records.append(record)
                if rows:
                    journal.commit_batch(shard, rows[-1]["local_id"], records)
                    for record in records:
                        emit({"observation": record})
            time.sleep(1)
        emit(journal.report())
    finally:
        journal.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Restricted test-group reader and event publisher. No WeChat send command.")
    parser.add_argument("--run", default="test-group", help="Private run name; use a new name to bind again.")
    subs = parser.add_subparsers(dest="command", required=True)
    subs.add_parser("audit")
    subs.add_parser("discover")
    p = subs.add_parser("bind")
    p.add_argument("--account-index", required=True, type=int)
    p.add_argument("--account-alias", required=True)
    p.add_argument("--room", default="test-group")
    p = subs.add_parser("watch")
    p.add_argument("--seconds", type=int, default=60)
    p = subs.add_parser("publish")
    p.add_argument("--seconds", type=int, default=60)
    subs.add_parser("report")
    args = parser.parse_args(argv)
    try:
        if args.command == "audit":
            emit(audit())
        elif args.command == "discover":
            emit(discover())
        else:
            if args.command in ("bind", "watch", "publish"):
                require_live_admission()
            run = private_run(args.run)
            if args.command == "bind":
                bind(args, run)
            elif args.command == "watch":
                watch(args, run)
            elif args.command == "publish":
                from .bridge import publish
                publish(args, run, emit)
            else:
                journal = Journal(run / "probe.sqlite3")
                try:
                    journal.config()
                    emit(journal.report())
                finally:
                    journal.close()
        return 0
    except ProbeError as exc:
        state = {"status": "blocked", "reason": str(exc), "send_enabled": False}
        state.update({"publishing": False} if args.command == "publish" else {"dot_enabled": False})
        emit(state)
        return 2
    except KeyboardInterrupt:
        emit({"status": "stopped", "send_enabled": False})
        return 130
    except Exception as exc:
        # Raw upstream errors can include private paths, account IDs or message text.
        emit({"status": "blocked", "reason": "Local diagnostic failed without fallback.",
              "error_type": type(exc).__name__, "send_enabled": False})
        return 2

if __name__ == "__main__":
    raise SystemExit(main())
