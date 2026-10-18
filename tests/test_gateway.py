"""Synthetic state-machine and callback tests; these do not claim live admission."""
import base64
import copy
import hashlib
import hmac
import json
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet

from wechat_gateway.store import Binding, EVENT_NAME, GatewayError, Policy, Store, iso
from wechat_gateway.webhooks import Events, callback_parts, public_addresses, signature, signing_key


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "state.sqlite3"
        self.now = 1800000000.0
        self.binding = Binding("owner", "account", "room@chatroom")
        self.other = Binding("other", "account", "room@chatroom")
        self.policy = Policy(enabled=True, transport_admitted=True, notice_confirmed=True, send_enabled=True)
        self.store = Store(self.path, (self.binding, self.other), self.policy, lambda: self.now)
        self.event = {"schema_version": "0.2", "event_id": "untrusted-adapter-id",
            "occurred_at": iso(self.now), "channel": "wechat", "account_id": "account",
            "room_id": "room@chatroom", "sender_id": "sender", "message_id": "msg1",
            "message_type": "text", "text": "@test hi", "mention_bot": True,
            "from_self": False, "known_bot": False}
        self.secret = "whsec_" + base64.b64encode(bytes(range(32))).decode()
        self.calls = []
        self.response_code = 200
        self.cipher_key = Fernet.generate_key()
        self.events = Events(self.store, self.cipher_key, self.post, has_grant=lambda principal, family: True)
        self.params = {"name": EVENT_NAME, "arguments": {"account_id": "account", "room_id": "room@chatroom"},
            "delivery": {"mode": "webhook", "url": "https://callback.example/receive", "secret": self.secret}}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def post(self, url, body, headers):
        self.calls.append((url, body, headers))
        parsed = json.loads(body)
        if parsed.get("type") == "verification":
            return 200, json.dumps({"challenge": parsed["challenge"]}).encode()
        return self.response_code, b"{}"

    def ingest(self, **updates):
        return self.store.ingest(self.binding, dict(self.event, **updates))["event_id"]

    def draft(self):
        return self.store.propose("owner", self.ingest(), "Reviewed reply", "key1")

    def approved(self):
        draft = self.draft()
        self.store.approve("owner", draft["draft_id"], draft["content_hash"], "local-owner")
        return draft

    def test_default_closed_and_invalid_policy(self):
        with self.assertRaises(GatewayError):
            Policy(enabled=True)
        self.store.policy = Policy()
        with self.assertRaises(GatewayError):
            self.ingest()

    def test_scoped_dedupe_persists_after_restart(self):
        first = self.ingest()
        self.assertNotEqual(first, self.event["event_id"])
        self.assertEqual(self.store.ingest(self.binding, self.event)["status"], "duplicate")
        self.store.close()
        self.store = Store(self.path, (self.binding, self.other), self.policy, lambda: self.now)
        self.assertEqual(self.store.ingest(self.binding, self.event)["event_id"], first)
        other = self.store.ingest(self.other, self.event)["event_id"]
        self.assertNotEqual(first, other)
        with self.assertRaises(GatewayError):
            self.store.ingest(self.binding, dict(self.event, text="changed"))

    def test_rejects_foreign_scope_and_missing_mention_evidence(self):
        for update in ({"room_id": "elsewhere"}, {"account_id": "elsewhere"}, {"mention_bot": None}):
            with self.subTest(update=update), self.assertRaises(GatewayError):
                self.store.ingest(self.binding, dict(self.event, **update))
        for update in ({"mention_bot": False}, {"from_self": True}, {"known_bot": True}):
            self.assertEqual(self.store.ingest(self.binding, dict(self.event, **update))["status"], "filtered")
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM events").fetchone()[0], 0)

    def test_stale_future_and_cross_principal_access(self):
        for offset in (-301, 31):
            self.assertEqual(self.store.ingest(self.binding, dict(self.event, occurred_at=iso(self.now + offset)))["status"], "expired")
        event_id = self.ingest()
        with self.assertRaises(GatewayError):
            self.store.get_event("other", event_id)
        self.store.bindings = frozenset()
        with self.assertRaises(GatewayError):
            self.store.get_event("owner", event_id)

    def test_context_is_bounded_and_uses_authorized_cache_only(self):
        for index in range(24):
            event_id = self.ingest(message_id=str(index), text="x" * 1000)
        context = self.store.get_context("owner", event_id)
        self.assertLessEqual(len(context["events"]), 20)
        self.assertLessEqual(sum(len(e["text"]) for e in context["events"]), 8000)
        with self.assertRaises(GatewayError):
            self.store.get_context("owner", event_id, 21)

    def test_draft_idempotence_conflicts_and_no_send_before_owner(self):
        draft = self.draft()
        self.assertEqual(self.store.propose("owner", draft["event_id"], "Reviewed reply", "key1"), draft)
        for text, key in (("changed", "key1"), ("Reviewed reply", "key2")):
            with self.assertRaises(GatewayError):
                self.store.propose("owner", draft["event_id"], text, key)
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, draft["draft_id"])
        with self.assertRaises(GatewayError):
            self.store.approve("owner", draft["draft_id"], "forged-hash", "owner")

    def test_approval_expires_and_changes_with_policy(self):
        draft = self.approved()
        self.now += 901
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, draft["draft_id"])
        self.now -= 901
        self.store.policy = Policy(enabled=True, transport_admitted=True, notice_confirmed=True, send_enabled=True, room_rate=2)
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, draft["draft_id"])

    def test_unknown_outcome_and_restart_block_resends(self):
        draft = self.approved()
        self.store.claim_send(self.binding, draft["draft_id"])
        self.store.close()
        self.store = Store(self.path, (self.binding, self.other), self.policy, lambda: self.now)
        self.assertEqual(self.store.review("owner", draft["draft_id"])["status"], "delivery_unknown")
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, draft["draft_id"])
        event_id = self.ingest(message_id="msg2")
        second = self.store.propose("owner", event_id, "other", "key2")
        self.store.approve("owner", second["draft_id"], second["content_hash"], "owner")
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, second["draft_id"])

    def test_send_confirmation_must_match_source_body_and_time(self):
        draft = self.approved()
        self.store.claim_send(self.binding, draft["draft_id"])
        bad = {"account_id": "account", "room_id": "other-room", "message_id": "reply1",
            "from_self": True, "body_sha256": hashlib.sha256(b"Reviewed reply").hexdigest(), "occurred_at": self.now}
        self.assertEqual(self.store.finish_send(self.binding, draft["draft_id"], bad)["status"], "delivery_unknown")

    def test_verified_confirmation_is_sent_once(self):
        draft = self.approved()
        self.store.claim_send(self.binding, draft["draft_id"])
        receipt = {"account_id": "account", "room_id": "room@chatroom", "message_id": "reply1",
            "from_self": True, "body_sha256": hashlib.sha256(b"Reviewed reply").hexdigest(), "occurred_at": self.now}
        self.assertEqual(self.store.finish_send(self.binding, draft["draft_id"], receipt)["status"], "sent")
        with self.assertRaises(GatewayError):
            self.store.claim_send(self.binding, draft["draft_id"])

    def test_signature_matches_standard_webhook_formula(self):
        expected = base64.b64encode(hmac.new(bytes(range(32)), b"event.1800000000.{}", hashlib.sha256).digest()).decode()
        self.assertEqual(signature(self.secret, "event", 1800000000, b"{}"), "v1," + expected)
        for bad in ("secret", "whsec_YQ==", "whsec_!", None):
            with self.assertRaises(GatewayError):
                signing_key(bad)

    def test_callback_rejects_local_and_mixed_dns_answers(self):
        for url in ("http://example.com", "https://u:p@example.com", "https://example.com:444", "https://example.com/#x", "https://example.com/\r\nHost:x"):
            with self.assertRaises(GatewayError):
                callback_parts(url)
        for addresses in (("127.0.0.1",), ("192.168.1.1",), ("8.8.8.8", "10.0.0.1")):
            answers = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443)) for ip in addresses]
            with patch("socket.getaddrinfo", return_value=answers), self.assertRaises(GatewayError):
                public_addresses("example.com")

    def test_subscription_challenge_required_and_secret_encrypted(self):
        self.events.post = lambda *args: (200, b'{"challenge":"wrong"}')
        with self.assertRaises(GatewayError):
            self.events.subscribe("owner", self.params)
        self.assertEqual(self.store.db.execute("SELECT COUNT(*) FROM subscriptions").fetchone()[0], 0)
        self.events.post = self.post
        sub = self.events.subscribe("owner", self.params)
        stored = self.store.db.execute("SELECT secret FROM subscriptions").fetchone()[0]
        self.assertNotIn(self.secret, stored)
        self.assertEqual(self.events.subscribe("owner", self.params)["id"], sub["id"])

    def test_delivery_retries_same_event_id_and_fresh_signature(self):
        self.events.subscribe("owner", self.params)
        event_id = self.ingest()
        self.response_code = 503
        self.events.deliver_one()
        first = self.calls[-1]
        self.now += 3
        self.response_code = 200
        self.events.deliver_one()
        second = self.calls[-1]
        self.assertEqual(first[1], second[1])
        self.assertEqual(first[2]["webhook-id"], event_id)
        self.assertNotEqual(first[2]["webhook-signature"], second[2]["webhook-signature"])
        self.assertFalse(self.events.deliver_one())
        self.assertEqual(self.store.db.execute("SELECT status FROM deliveries").fetchone()[0], "accepted")

    def test_unsubscribe_revocation_expiry_and_413_stop_delivery(self):
        self.events.subscribe("owner", self.params)
        self.ingest()
        params = copy.deepcopy(self.params)
        del params["delivery"]["secret"]
        self.events.unsubscribe("owner", params)
        self.events.unsubscribe("owner", params)
        self.assertFalse(self.events.deliver_one())
        self.events.subscribe("owner", self.params)
        self.ingest(message_id="msg2")
        self.response_code = 413
        self.events.deliver_one()
        self.now += 60
        self.assertFalse(self.events.deliver_one())
        self.ingest(message_id="msg3")
        self.store.bindings = frozenset()
        call_count = len(self.calls)
        self.events.deliver_one()
        self.assertEqual(len(self.calls), call_count)

    def test_rotated_secret_dual_signs_during_window(self):
        self.events.subscribe("owner", self.params)
        changed = copy.deepcopy(self.params)
        changed["delivery"]["secret"] = "whsec_" + base64.b64encode(b"z" * 32).decode()
        self.events.subscribe("owner", changed)
        self.ingest()
        self.events.deliver_one()
        self.assertEqual(len(self.calls[-1][2]["webhook-signature"].split()), 2)


if __name__ == "__main__":
    unittest.main()
