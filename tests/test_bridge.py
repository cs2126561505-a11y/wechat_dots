import json
import http.client
import tempfile
import ssl
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

from wechat_probe import __main__ as cli
from wechat_probe.bridge import Outbox, gateway_post, normalized, publish, settings
from wechat_probe.core import ProbeError
from wechat_gateway.store import Binding, Policy, Store


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
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
                gateway_post("https://gateway.example/adapter/events", "secret", self.event(), {})
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
        lease = {"session": "original"}
        with patch("wechat_probe.bridge.require_live_admission", return_value=lease), patch("urllib.request.build_opener") as build:
            for status in (401, 403, 302, 307, 400):
                build.return_value.open.side_effect = urllib.error.HTTPError("https://gateway.example", status, "private", {}, None)
                with self.assertRaises(ProbeError):
                    gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease)
            for status in (408, 429, 503):
                build.return_value.open.side_effect = urllib.error.HTTPError("https://gateway.example", status, "private", {}, None)
                self.assertIsNone(gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease))

    def test_changed_admission_stops_before_network(self):
        with patch("wechat_probe.bridge.require_live_admission", return_value={"session": "replacement"}), patch("urllib.request.build_opener") as build:
            with self.assertRaisesRegex(ProbeError, "admission changed"):
                gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), {"session": "original"})
            build.assert_not_called()

    def test_certificate_failure_stops_instead_of_retrying(self):
        lease = {"session": "original"}
        with patch("wechat_probe.bridge.require_live_admission", return_value=lease), patch("urllib.request.build_opener") as build:
            failure = ssl.SSLCertVerificationError(1, "private diagnostic")
            for error in (failure, urllib.error.URLError(failure)):
                build.return_value.open.side_effect = error
                with self.assertRaisesRegex(ProbeError, "TLS certificate verification failed"):
                    gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease)

    def response(self, parts, length=None):
        response = MagicMock(status=200)
        response.__enter__.return_value = response
        response.getheader.return_value = length
        response.read1.side_effect = parts
        return response

    def test_response_transport_failure_retries_without_discarding_payload(self):
        lease = {"session": "original"}
        self.box.commit_batch(self.row["shard"], 7, [self.event()])
        with patch("wechat_probe.bridge.require_live_admission", return_value=lease), patch("urllib.request.build_opener") as build:
            for parts, length in (([TimeoutError("private response")], None),
                                  ([http.client.IncompleteRead(b"private response")], None),
                                  ([b'{"status":', b''], "100")):
                response = self.response(parts, length)
                build.return_value.open.return_value = response
                result = gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease)
                self.assertIsNone(result)
                response.__exit__.assert_called_once()
            self.box.deliver_one(lambda event: result, 1005)
            self.assertEqual(tuple(self.box.db.execute("SELECT status,data IS NOT NULL FROM outbox").fetchone()), ("pending", 1))

    def test_response_body_has_time_and_size_bounds(self):
        lease = {"session": "original"}
        with patch("wechat_probe.bridge.require_live_admission", return_value=lease), patch("urllib.request.build_opener") as build:
            response = self.response([b'{', b' ', b' '])
            build.return_value.open.return_value = response
            with patch("wechat_probe.bridge.time.monotonic", side_effect=[0, 0, 6, 12]):
                self.assertIsNone(gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease))
            self.assertEqual(response.read1.call_count, 2)
            response.__exit__.assert_called_once()
            response = self.response([b'x' * 4096] * 4 + [b'x'])
            build.return_value.open.return_value = response
            with self.assertRaises(ProbeError):
                gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease)
            response.__exit__.assert_called_once()

    def test_complete_response_acknowledgement(self):
        lease = {"session": "original"}
        body = b'{"status":"duplicate","event_id":"evt_' + b'a' * 64 + b'"}'
        response = self.response([body[:10], body[10:], b''], str(len(body)))
        with patch("wechat_probe.bridge.require_live_admission", return_value=lease), patch("urllib.request.build_opener") as build:
            build.return_value.open.return_value = response
            self.assertEqual(gateway_post("https://gateway.example/adapter/events", "x" * 43, self.event(), lease), json.loads(body))


if __name__ == "__main__":
    unittest.main()
