"""Offline project-contract checks. No network, MCP server, or messaging client.

Passing these tests does not establish authentication, consent, runtime policy
 enforcement, prompt-injection resistance, delivery, or dot memory isolation.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, ValidationError

ROOT = Path(__file__).resolve().parents[1]


def load_json(relative_path: str) -> dict[str, Any]:
    value = json.loads((ROOT / relative_path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError(f"Expected an object in {relative_path}")
    return value


class ContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.event_schema = load_json("contracts/message-event.schema.json")
        cls.policy_schema = load_json("contracts/group-policy.schema.json")
        cls.event = load_json("examples/message-event.json")
        cls.policy = load_json("config/group-policy.example.json")
        cls.catalog = load_json("contracts/tools.json")
        cls.tools = {tool["name"]: tool for tool in cls.catalog["tools"]}
        cls.event_validator = Draft202012Validator(
            cls.event_schema, format_checker=FormatChecker()
        )
        cls.policy_validator = Draft202012Validator(cls.policy_schema)

    def tool_validator(self, name: str) -> Draft202012Validator:
        return Draft202012Validator(self.tools[name]["inputSchema"])

    def test_all_project_json_parses(self) -> None:
        for folder in ("contracts", "config", "examples"):
            for path in (ROOT / folder).glob("*.json"):
                with self.subTest(path=path.name):
                    self.assertIsInstance(json.loads(path.read_text("utf-8")), dict)

    def test_schemas_are_valid(self) -> None:
        Draft202012Validator.check_schema(self.event_schema)
        Draft202012Validator.check_schema(self.policy_schema)
        for name, tool in self.tools.items():
            with self.subTest(tool=name):
                Draft202012Validator.check_schema(tool["inputSchema"])

    def test_examples_validate(self) -> None:
        self.event_validator.validate(self.event)
        self.policy_validator.validate(self.policy)

    def test_example_policy_is_fail_closed(self) -> None:
        self.assertFalse(self.policy["enabled"])
        self.assertEqual(self.policy["allowed_room_ids"], [])
        self.assertFalse(self.policy["notice_confirmed"])
        self.assertEqual(self.policy["transport"], "unselected")
        self.assertEqual(self.policy["reply_mode"], "draft_only")
        self.assertFalse(self.policy["publish_send_tool"])
        self.assertFalse(self.policy["allow_unofficial_transport"])
        self.assertLessEqual(
            self.policy["room_max_replies_per_minute"],
            self.policy["global_max_replies_per_minute"],
        )

    def test_enabling_requires_declared_prerequisites(self) -> None:
        valid = copy.deepcopy(self.policy)
        valid.update(enabled=True, transport="wechatferry_experimental",
                     allow_unofficial_transport=True,
                     allowed_room_ids=["room_synthetic_001"], notice_confirmed=True)
        self.policy_validator.validate(valid)
        for key, value in (("transport", "unselected"),
                           ("allowed_room_ids", []), ("notice_confirmed", False)):
            invalid = copy.deepcopy(valid)
            invalid[key] = value
            with self.subTest(field=key), self.assertRaises(ValidationError):
                self.policy_validator.validate(invalid)

    def test_unofficial_transport_requires_explicit_opt_in(self) -> None:
        for transport in ("wechaty_experimental", "wechatferry_experimental"):
            candidate = copy.deepcopy(self.policy)
            candidate["transport"] = transport
            with self.subTest(transport=transport):
                with self.assertRaises(ValidationError):
                    self.policy_validator.validate(candidate)
                candidate["allow_unofficial_transport"] = True
                self.policy_validator.validate(candidate)

    def test_policy_rejects_unsafe_first_stage_settings(self) -> None:
        changes = {
            "reply_mode": "auto", "publish_send_tool": True,
            "auth_required": False, "owner_approval_required": False,
            "cross_room_memory": True, "external_action_tools": True,
            "raw_message_log": True, "collect_only_triggered_messages": False,
            "mention_required": False, "unknown_policy_override": True,
        }
        for key, value in changes.items():
            candidate = copy.deepcopy(self.policy)
            candidate[key] = value
            with self.subTest(field=key), self.assertRaises(ValidationError):
                self.policy_validator.validate(candidate)

    def test_event_requires_identity_and_routing_fields(self) -> None:
        for key in ("event_id", "account_id", "room_id", "sender_id", "message_id"):
            candidate = copy.deepcopy(self.event)
            del candidate[key]
            with self.subTest(field=key), self.assertRaises(ValidationError):
                self.event_validator.validate(candidate)

    def test_event_rejects_invalid_formats_and_extra_fields(self) -> None:
        changes = {
            "occurred_at": "2026-10-16T12:00:00",
            "message_type": "image", "mention_bot": "true",
            "text": "x" * 4097, "approved": True,
        }
        for key, value in changes.items():
            candidate = copy.deepcopy(self.event)
            candidate[key] = value
            with self.subTest(field=key), self.assertRaises(ValidationError):
                self.event_validator.validate(candidate)

    def test_tool_inputs_accept_minimal_valid_samples(self) -> None:
        samples = {
            "wechat_get_connection": {},
            "wechat_get_event": {"event_id": "evt_synthetic_001"},
            "wechat_get_context": {"event_id": "evt_synthetic_001", "limit": 20},
            "wechat_propose_reply": {"event_id": "evt_synthetic_001",
                "text": "这是一条尚未发送的草稿。", "idempotency_key": "key_synthetic_001"},
            "wechat_send_reply": {"draft_id": "draft_synthetic_001"},
        }
        for name, sample in samples.items():
            with self.subTest(tool=name):
                self.tool_validator(name).validate(sample)

    def test_send_rejects_forged_approval_and_destination_fields(self) -> None:
        for key, value in (("approved", True), ("room_id", "another_room"),
                           ("text", "replacement"), ("url", "https://example.invalid"),
                           ("owner_id", "someone_else")):
            candidate = {"draft_id": "draft_synthetic_001", key: value}
            with self.subTest(field=key), self.assertRaises(ValidationError):
                self.tool_validator("wechat_send_reply").validate(candidate)

    def test_draft_requires_key_and_bounded_text(self) -> None:
        valid = {"event_id": "evt_synthetic_001", "text": "草稿",
                 "idempotency_key": "key_synthetic_001"}
        for text in ("", "x" * 801):
            candidate = dict(valid, text=text)
            with self.subTest(text_length=len(text)), self.assertRaises(ValidationError):
                self.tool_validator("wechat_propose_reply").validate(candidate)
        del valid["idempotency_key"]
        with self.assertRaises(ValidationError):
            self.tool_validator("wechat_propose_reply").validate(valid)

    def test_context_is_bounded_and_cannot_select_another_room(self) -> None:
        for limit in (0, 21, -1):
            with self.subTest(limit=limit), self.assertRaises(ValidationError):
                self.tool_validator("wechat_get_context").validate(
                    {"event_id": "evt_synthetic_001", "limit": limit}
                )
        with self.assertRaises(ValidationError):
            self.tool_validator("wechat_get_context").validate(
                {"event_id": "evt_synthetic_001", "room_id": "another_room"}
            )

    def test_policy_scope_is_limited_to_selected_wechat_candidates(self) -> None:
        expected = {"unselected", "wechaty_experimental", "wechatferry_experimental"}
        self.assertEqual(set(self.policy_schema["properties"]["transport"]["enum"]), expected)
        for transport in ("wecom_aibot", "openclaw_weixin", "unknown_adapter"):
            candidate = dict(self.policy, transport=transport)
            with self.subTest(transport=transport), self.assertRaises(ValidationError):
                self.policy_validator.validate(candidate)
        for transport in expected - {"unselected"}:
            candidate = dict(self.policy, enabled=True, transport=transport,
                             allow_unofficial_transport=True,
                             allowed_room_ids=["room_synthetic_001"], notice_confirmed=True)
            with self.subTest(enabled_transport=transport):
                self.policy_validator.validate(candidate)

    def test_events_are_scoped_to_wechat(self) -> None:
        self.assertEqual(self.event["channel"], "wechat")
        for channel in ("wecom", "wechat_dm", "unknown"):
            with self.subTest(channel=channel), self.assertRaises(ValidationError):
                self.event_validator.validate(dict(self.event, channel=channel))

    def test_scope_revision_requires_contract_version_0_2(self) -> None:
        for sample, validator in ((self.policy, self.policy_validator),
                                  (self.event, self.event_validator)):
            self.assertEqual(sample["schema_version"], "0.2")
            with self.assertRaises(ValidationError):
                validator.validate(dict(sample, schema_version="0.1"))

    def test_send_is_not_published_by_default(self) -> None:
        self.assertEqual(len(self.tools), len(self.catalog["tools"]))
        self.assertNotIn("wechat_send_reply", self.catalog["default_exposed_tools"])
        self.assertEqual(
            set(self.catalog["default_exposed_tools"]),
            {"wechat_get_connection", "wechat_get_event", "wechat_get_context", "wechat_propose_reply"},
        )


if __name__ == "__main__":
    unittest.main()
