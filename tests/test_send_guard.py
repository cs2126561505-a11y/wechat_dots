"""Synthetic send evidence only; no desktop, WeChat data or network access."""
from dataclasses import replace
import sqlite3
import tempfile
import unittest
from pathlib import Path

from wechat_probe.core import ProbeError, digest
from wechat_probe.send_guard import ActionLedger, make_challenge, prepare_send, unique_target


class SendGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "send.sqlite3"
        self.ledger = ActionLedger(self.path)
        self.process = {"pid": 123, "started": 900.0, "exe": r"C:\Synthetic\Weixin.exe"}
        self.run = {"account_id": "wxid_self", "room_id": "room@chatroom", "room_name": "test-group",
                    "process": self.process, "client_version": "4.1.15.13", "started_at": 950}
        self.shard = "message/message_0.db"
        self.other_shard = "message/message_1.db"
        self.before = {self.shard: 10, self.other_shard: 20}
        self.challenge = make_challenge(self.run, "wxid_peer", self.before, 1000)
        self.baseline = {self.shard: 11, self.other_shard: 20}
        self.command = {"draft_id": "draft_" + "a" * 64, "account_id": "wxid_self", "room_id": "room@chatroom",
                        "text": "Reviewed reply", "content_hash": "b" * 64, "attempted_at": 1002.0, "execute_before": 1122.0}
        self.contacts = [{"username": "room@chatroom", "nick_name": "test-group", "remark": ""}]
        self.marker_db = {"account_id": "wxid_self", "room_id": "room@chatroom", "process": self.process,
            "row": {"shard": self.shard, "local_id": 11, "local_type": 1, "create_time": 1001,
                    "sender_username": "wxid_peer", "content": self.challenge.marker}}
        self.ui = {"source": "native-uia", "evidence_hash": "c" * 64, "observed_at": 1003.0,
            "process": self.process, "hwnd": 456, "chat_type": "group", "title": "test-group",
            "visible_marker": self.challenge.marker, "input_text": self.command["text"],
            "foreground": True, "input_focused": True}

    def tearDown(self):
        self.ledger.close()
        self.tmp.cleanup()

    def plan(self, **changes):
        args = dict(command=self.command, run=self.run, process=self.process, contacts=self.contacts,
                    baseline=self.baseline, challenge=self.challenge, marker_db=self.marker_db, ui=self.ui, now=1003)
        args.update(changes)
        return prepare_send(**args)

    def scan(self, rows=None):
        row = {"shard": self.shard, "local_id": 12, "local_type": 1, "create_time": 1004,
               "sender_username": "wxid_self", "content": self.command["text"]}
        return {"account_id": "wxid_self", "room_id": "room@chatroom", "process": self.process,
                "scanned_at": 1005, "shards": {
                    self.shard: {"after": 11, "complete": True, "rows": [row] if rows is None else rows},
                    self.other_shard: {"after": 20, "complete": True, "rows": []}}}

    def test_exact_target_rejects_same_named_private_group_or_duplicate_hits(self):
        self.assertEqual(unique_target(self.contacts, "test-group", "room@chatroom"), "room@chatroom")
        for username in ("wxid_private", "different@chatroom", "room@chatroom"):
            with self.subTest(username=username), self.assertRaises(ProbeError):
                self.plan(contacts=self.contacts + [{"username": username, "nick_name": "other", "remark": "test-group"}])
        for name in ("test-group extra", "Test-group", "test-grou"):
            with self.assertRaises(ProbeError):
                self.plan(contacts=[dict(self.contacts[0], nick_name=name)])

    def test_command_process_run_and_deadline_are_bound(self):
        plan = self.plan()
        self.assertEqual(plan.body_sha256, digest(self.command["text"]))
        self.assertEqual(plan.baseline, tuple(sorted(self.baseline.items())))
        for changes in ({"account_id": "wxid_other"}, {"room_id": "elsewhere@chatroom"},
                        {"draft_id": "title-derived"}, {"text": "changed"}, {"content_hash": "invalid"},
                        {"attempted_at": 1004}, {"execute_before": 1003}, {"execute_before": 1123}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                self.plan(command=dict(self.command, **changes))
        with self.assertRaises(ProbeError):
            self.plan(process=dict(self.process, pid=124))
        with self.assertRaises(ProbeError):
            self.plan(run=dict(self.run, started_at=1001))

    def test_fresh_controlled_marker_must_match_explicit_db_scope(self):
        for changes in ({"sender_username": "wxid_self"}, {"sender_username": "wxid_stranger"},
                        {"create_time": 999}, {"local_id": 10}, {"local_id": 12},
                        {"content": "hi"}, {"local_type": 3}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                self.plan(marker_db=dict(self.marker_db, row=dict(self.marker_db["row"], **changes)))
        for changes in ({"room_id": "different@chatroom"}, {"account_id": "wxid_other"}, {"process": {}}):
            with self.assertRaises(ProbeError):
                self.plan(marker_db=dict(self.marker_db, **changes))
        with self.assertRaises(ProbeError):
            self.plan(challenge=replace(self.challenge, issued_at=880))
        with self.assertRaises(ProbeError):
            self.plan(baseline={self.shard: 11})
        other = make_challenge(self.run, "wxid_peer", self.before, 1000)
        self.assertNotEqual(other.marker, self.challenge.marker)

    def test_missing_stale_partial_or_unfocused_ui_evidence_never_passes(self):
        for changes in ({"title": "test-group extra"}, {"chat_type": "friend"}, {"visible_marker": "hi"},
                        {"input_text": "Reviewed"}, {"input_focused": False}, {"foreground": False},
                        {"observed_at": 997}, {"source": "guessed-ocr"}, {"evidence_hash": ""}, {"hwnd": 0}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                self.plan(ui=dict(self.ui, **changes))
        with self.assertRaises(ProbeError):
            self.plan(ui=None)
        self.plan(ui=dict(self.ui, source="human-reviewed-screenshot"))

    def test_consumed_record_is_committed_before_return_and_survives_restart(self):
        plan = self.plan()
        permit = self.ledger.consume(plan, self.run, self.process, self.ui, 1003)
        self.assertEqual(permit["execute_before"], self.command["execute_before"])
        observer = sqlite3.connect(self.path)
        try:
            self.assertEqual(observer.execute("SELECT COUNT(*) FROM consumed").fetchone()[0], 1)
        finally:
            observer.close()
        self.ledger.close()
        self.ledger = ActionLedger(self.path)
        with self.assertRaises(ProbeError):
            self.ledger.consume(plan, self.run, self.process, self.ui, 1003)
        second = self.plan(command=dict(self.command, draft_id="draft_" + "d" * 64))
        with self.assertRaises(ProbeError):
            self.ledger.consume(second, self.run, self.process, self.ui, 1003)
        stored = self.ledger.db.execute("SELECT * FROM consumed").fetchone()
        self.assertNotIn(plan.text, str(stored))
        self.assertNotIn(self.challenge.marker, str(stored))

    def test_last_moment_ui_process_run_or_expiry_change_blocks_consumption(self):
        plan = self.plan()
        for ui in (dict(self.ui, hwnd=457), dict(self.ui, input_text="changed"), dict(self.ui, visible_marker="other")):
            with self.assertRaises(ProbeError):
                self.ledger.consume(plan, self.run, self.process, ui, 1003)
        with self.assertRaises(ProbeError):
            self.ledger.consume(plan, dict(self.run, started_at=951), self.process, self.ui, 1003)
        with self.assertRaises(ProbeError):
            self.ledger.consume(plan, self.run, dict(self.process, started=901), self.ui, 1003)
        with self.assertRaises(ProbeError):
            self.ledger.consume(replace(plan, room_id="elsewhere@chatroom"), self.run, self.process, self.ui, 1003)
        with self.assertRaises(ProbeError):
            self.ledger.consume(plan, self.run, self.process, dict(self.ui, observed_at=1122), 1122)
        self.assertEqual(self.ledger.db.execute("SELECT COUNT(*) FROM consumed").fetchone()[0], 0)

    def test_exact_new_self_message_yields_stable_gateway_receipt_only_after_consumption(self):
        plan = self.plan()
        scan = self.scan()
        with self.assertRaises(ProbeError):
            self.ledger.confirm_receipt(plan, scan, 1005)
        self.ledger.consume(plan, self.run, self.process, self.ui, 1003)
        receipt = self.ledger.confirm_receipt(plan, scan, 1005)
        self.assertEqual(set(receipt), {"account_id", "room_id", "message_id", "body_sha256", "occurred_at", "from_self"})
        self.assertEqual(receipt["body_sha256"], digest(self.command["text"]))
        self.assertTrue(receipt["from_self"])
        self.assertEqual(self.ledger.confirm_receipt(plan, scan, 1005), receipt)

    def test_wrong_scope_incomplete_shards_or_stale_scan_never_yields_receipt(self):
        plan = self.plan()
        self.ledger.consume(plan, self.run, self.process, self.ui, 1003)
        for changes in ({"account_id": "wxid_other"}, {"room_id": "other@chatroom"},
                        {"process": {}}, {"scanned_at": 998}):
            with self.assertRaises(ProbeError):
                self.ledger.confirm_receipt(plan, dict(self.scan(), **changes), 1005)
        for change in ("missing", "partial", "wrong-baseline"):
            scan = self.scan()
            if change == "missing":
                del scan["shards"][self.other_shard]
            elif change == "partial":
                scan["shards"][self.other_shard]["complete"] = False
            else:
                scan["shards"][self.shard]["after"] = 10
            with self.assertRaises(ProbeError):
                self.ledger.confirm_receipt(plan, scan, 1005)

    def test_receipt_rejects_old_wrong_sender_body_time_or_multiple_matches(self):
        plan = self.plan()
        self.ledger.consume(plan, self.run, self.process, self.ui, 1003)
        original = self.scan()["shards"][self.shard]["rows"][0]
        for changes in ({"local_id": 11}, {"sender_username": "2"}, {"sender_username": "wxid_peer"},
                        {"content": "prefix Reviewed reply"}, {"create_time": 999}, {"create_time": 1123}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                self.ledger.confirm_receipt(plan, self.scan([dict(original, **changes)]), 1005)
        for rows in ([], [original, original], [original, dict(original, local_id=13)]):
            with self.assertRaises(ProbeError):
                self.ledger.confirm_receipt(plan, self.scan(rows), 1005)
        scan = self.scan()
        scan["shards"][self.other_shard]["rows"] = [dict(original, shard=self.other_shard, local_id=21)]
        with self.assertRaises(ProbeError):
            self.ledger.confirm_receipt(plan, scan, 1005)
        with self.assertRaises(ProbeError):
            self.ledger.consume(plan, self.run, self.process, self.ui, 1003)


if __name__ == "__main__":
    unittest.main()
