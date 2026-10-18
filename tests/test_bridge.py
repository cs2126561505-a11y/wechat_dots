import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_probe import __main__ as cli
from wechat_probe.bridge import Outbox, gateway_post, normalized, publish, settings
from wechat_probe.core import ProbeError
from wechat_gateway.store import Binding, Policy, Store


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.row = {"local_type": 1, "content": "hi", "sender_username": "wxid_peer",
                    "source": "<msgsource><atuserlist>wxid_self</atuserlist></msgsource>",
                    "create_time": 1000, "local_id": 7, "shard": "message/message_0.db"}
        self.box = Outbox(self.root / "outbox.sqlite3")
        self.box.initialize({"account": "wxid_self"}, {self.row["shard"]: 6}, 999)

    def tearDown(self):
        self.box.close()
        self.tmp.cleanup()

    def event(self, **changes):
        return normalized(dict(self.row, **changes), "wxid_self", "room@chatroom", ["wxid_peer"], 999, 1005)

    def test_real_mention_and_controlled_sender_required(self):
        event = self.event()
        self.assertTrue(event["mention_bot"])
        self.assertFalse(event["from_self"])
        self.assertFalse(event["known_bot"])
        for changes in ({"source": None}, {"source": "<msgsource/>"},
                        {"source": "<msgsource><atuserlist>wxid_other</atuserlist></msgsource>"},
                        {"content": "@test hi", "source": None}, {"sender_username": "wxid_self"},
                        {"sender_username": "wxid_unreviewed"}, {"local_type": 3}, {"content": "x" * 4097}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.event(**changes))
        with self.assertRaises(ProbeError):
            self.event(sender_username=None)

    def test_time_window_and_stable_message_identity(self):
        self.assertIsNone(self.event(create_time=998))
        self.assertIsNone(self.event(create_time=1040))
        self.assertIsNone(normalized(self.row, "wxid_self", "room@chatroom", ["wxid_peer"], 500, 1400))
        first = self.event()
        later = normalized(self.row, "wxid_self", "room@chatroom", ["wxid_peer"], 500, 1020)
        self.assertEqual(first["message_id"], later["message_id"])
        self.assertNotEqual(first["message_id"], self.event(create_time=1001)["message_id"])

    def test_cursor_and_outbox_atomic_conflict(self):
        event = self.event()
        self.box.commit_batch(self.row["shard"], 7, [event])
        other = self.event(local_id=8)
        with self.assertRaises(ProbeError):
            self.box.commit_batch(self.row["shard"], 9, [other, dict(event, text="changed")])
        self.assertEqual(self.box.cursors()[self.row["shard"]], 7)
        self.assertEqual(self.box.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 1)
        with self.assertRaises(ProbeError):
            self.box.commit_batch("message/new.db", 1, [])

    def test_resume_lost_ack_and_gateway_deduplication(self):
        binding = Binding("owner", "wxid_self", "room@chatroom")
        gateway = Store(self.root / "gateway.sqlite3", (binding,), Policy(enabled=True, transport_admitted=True, notice_confirmed=True), clock=lambda: 1010)
        try:
            self.box.commit_batch(self.row["shard"], 7, [self.event()])
            def lose_ack(event):
                self.assertEqual(gateway.ingest(binding, event)["status"], "eligible")
                return None
            self.assertTrue(self.box.deliver_one(lose_ack, 1005))
            self.box.close()
            self.box = Outbox(self.root / "outbox.sqlite3")
            self.box.initialize({"account": "wxid_self"}, {self.row["shard"]: 999}, 1010)
            self.assertEqual(self.box.cursors()[self.row["shard"]], 7)
            self.assertEqual(self.box.started_at(), 999)
            self.assertTrue(self.box.deliver_one(lambda e: gateway.ingest(binding, e), 1010))
            saved = self.box.db.execute("SELECT status,data FROM outbox").fetchone()
            self.assertEqual(tuple(saved), ("duplicate", None))
            self.assertEqual(gateway.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 1)
            with self.assertRaises(ProbeError):
                self.box.initialize({"account": "other"}, {}, 1010)
        finally:
            gateway.close()

    def test_expired_payload_never_leaves_outbox(self):
        self.box.commit_batch(self.row["shard"], 7, [self.event()])
        post = Mock()
        self.assertFalse(self.box.deliver_one(post, 1301))
        post.assert_not_called()
        self.assertEqual(tuple(self.box.db.execute("SELECT status,data FROM outbox").fetchone()), ("expired", None))

    def test_bad_ack_keeps_pending_payload(self):
        self.box.commit_batch(self.row["shard"], 7, [self.event()])
        with self.assertRaises(ProbeError):
            self.box.deliver_one(lambda e: {"status": "eligible"}, 1005)
        self.assertEqual(self.box.db.execute("SELECT status FROM outbox").fetchone()[0], "pending")

    def test_live_entrypoints_block_before_files_or_network(self):
        with patch("wechat_probe.__main__.require_live_admission", side_effect=ProbeError("blocked")), patch("wechat_probe.__main__.private_run") as private, patch("wechat_probe.__main__.emit"):
            self.assertEqual(cli.main(["publish", "--seconds", "1"]), 2)
            private.assert_not_called()
        with patch("wechat_probe.bridge.require_live_admission", side_effect=ProbeError("blocked")), patch("wechat_probe.bridge.Journal") as journal, patch("urllib.request.build_opener") as opener:
            with self.assertRaises(ProbeError):
                publish(SimpleNamespace(seconds=1), self.root, Mock())
            with self.assertRaises(ProbeError):
                gateway_post("https://gateway.example/adapter/events", "secret", self.event())
            journal.assert_not_called()
            opener.assert_not_called()

    def test_settings_exact_account_room_endpoint_and_sender(self):
        config = {"account_id": "wxid_self", "room_id": "room@chatroom"}
        data = dict(config, endpoint="https://gateway.example/adapter/events", controlled_senders=["wxid_peer"])
        (self.root / "bridge-token.txt").write_text("x" * 43)
        def save(value):
            (self.root / "bridge.json").write_text(json.dumps(value), encoding="utf-8")
        save(data)
        self.assertEqual(settings(self.root, config)[0], data)
        for changes in ({"account_id": "other"}, {"room_id": "other"}, {"endpoint": "http://gateway.example/adapter/events"},
                        {"endpoint": "https://gateway.example/adapter/events?to=other"}, {"controlled_senders": []},
                        {"controlled_senders": ["wxid_self"]}):
            save(dict(data, **changes))
            with self.assertRaises(ProbeError):
                settings(self.root, config)

    def test_http_error_categories_and_redirect_stop(self):
        with patch("wechat_probe.bridge.require_live_admission"), patch("urllib.request.build_opener") as build:
            for status in (401, 403, 302, 307, 400):
                build.return_value.open.side_effect = urllib.error.HTTPError("https://gateway.example", status, "private", {}, None)
                with self.assertRaises(ProbeError):
                    gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event())
            for status in (408, 429, 503):
                build.return_value.open.side_effect = urllib.error.HTTPError("https://gateway.example", status, "private", {}, None)
                self.assertIsNone(gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event()))


if __name__ == "__main__":
    unittest.main()
