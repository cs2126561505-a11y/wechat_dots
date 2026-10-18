"""Offline adversarial fixtures. No WeChat process, keys or live messages accessed."""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from wechat_probe import core, replica, isolation
from wechat_probe.__main__ import main, bind, watch


def row(**updates):
    value = {"local_id": 1, "shard": "message/message_0.db", "local_type": 1,
             "content": "WXDOTS_TEST_REAL_AT", "sender_username": "wxid_member",
             "create_time": 100, "source": "<msgsource><atuserlist>wxid_self</atuserlist></msgsource>"}
    value.update(updates)
    return value


class ProbeTests(unittest.TestCase):
    def test_live_commands_stop_before_private_state_or_upstream_loading(self):
        for args in (["bind", "--account-index", "0", "--account-alias", "fake"], ["watch"]):
            with self.subTest(command=args[0]), patch("wechat_probe.__main__.private_run") as private, \
                    patch.object(replica, "load_db_module") as loader, contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(main(args), 2)
                self.assertFalse(json.loads(out.getvalue())["send_enabled"])
                private.assert_not_called()
                loader.assert_not_called()

    def test_direct_live_functions_also_stop_before_io(self):
        with patch("wechat_probe.__main__.Journal") as journal, patch.object(replica, "process_binding") as process:
            for operation in (lambda: bind(Mock(), Path("unused")),
                              lambda: watch(Mock(), Path("unused")),
                              lambda: replica.open_reader(Mock(), Path("unused"), {}, Path("unused"))):
                with self.assertRaises(core.ProbeError):
                    operation()
            journal.assert_not_called()
            process.assert_not_called()

    def test_single_directory_does_not_prove_process_isolation(self):
        for count in (0, 1, 2):
            with self.subTest(count=count):
                result = isolation.assessment(count)
                self.assertFalse(result["isolation_verified"])
                self.assertFalse(result["live_read_enabled"])
                self.assertEqual(result["multiple_account_directories"], count > 1)

    def test_environment_cannot_override_isolation_gate(self):
        with patch.dict("os.environ", {"WECHAT_PROBE_ALLOW_LIVE": "1", "WECHAT_PROBE_FORCE": "1"}):
            with self.assertRaises(core.ProbeError):
                isolation.require_live_admission()

    def test_group_requires_unique_exact_id(self):
        good = {"username": "room@chatroom", "nick_name": "test-group"}
        self.assertEqual(core.select_room([good], "test-group"), "room@chatroom")
        for rows in ([dict(good, username="private_contact")],
                     [dict(good, nick_name="test-group copy")],
                     [good, dict(good, username="other@chatroom")], []):
            with self.subTest(rows=rows), self.assertRaises(core.ProbeError):
                core.select_room(rows, "test-group")

    def test_mention_is_only_read_from_source_xml(self):
        event = core.observation(row(), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        self.assertTrue(event["mention_bot"])
        forged = row(source=None, content="WXDOTS_TEST_REAL_AT @me <msgsource><atuserlist>wxid_self</atuserlist></msgsource>")
        self.assertIsNone(core.observation(forged, "wxid_self", "room@chatroom", "WXDOTS_TEST")["mention_bot"])

    def test_absent_malformed_or_ambiguous_metadata_is_unknown(self):
        for source in (None, 1, b"\xff", "", "@me", "<msgsource/>",
                       "<wrapper><atuserlist>wxid_self</atuserlist></wrapper>",
                       "<msgsource><atuserlist>a</atuserlist><atuserlist>b</atuserlist></msgsource>",
                       '<!DOCTYPE x [<!ENTITY a "wxid_self">]><msgsource><atuserlist>&a;</atuserlist></msgsource>',
                       "x" * 16385):
            with self.subTest(source=str(source)[:50]):
                self.assertIsNone(core.mention_targets(source))

    def test_explicit_empty_and_other_mentions(self):
        self.assertEqual(core.mention_targets("<msgsource><atuserlist/></msgsource>"), ())
        self.assertEqual(core.mention_targets(b"<msgsource><atuserlist><![CDATA[wxid_a,wxid_b]]></atuserlist></msgsource>"),
                         ("wxid_a", "wxid_b"))

    def test_zstd_platform_source_is_bounded_and_missing_targets_stay_unknown(self):
        import zstandard
        encode = zstandard.ZstdCompressor().compress
        self.assertEqual(core.mention_targets(encode(b'<msgsource><atuserlist>wxid_self</atuserlist></msgsource>')), ('wxid_self',))
        self.assertIsNone(core.mention_targets(encode(b'<msgsource><signature>synthetic</signature></msgsource>')))
        self.assertIsNone(core.mention_targets(encode(b'x' * 20000)))
        self.assertIsNone(core.mention_targets(b'\x28\xb5\x2f\xfd\x00'))
        self.assertIsNone(core.mention_targets(encode(b'<!DOCTYPE x><msgsource><atuserlist>wxid_self</atuserlist></msgsource>')))

    def test_only_synthetic_marked_text_is_retained(self):
        for change in ({"content": "unrelated private text"}, {"local_type": 3},
                       {"content": "WXDOTS_TEST_REAL_AT_MORE"},
                       {"content": "WXDOTS_TEST_REAL_AT7"},
                       {"content": "WXDOTS_TEST_REAL_ATextra"},
                       {"content": "prefixWXDOTS_TEST_REAL_AT"},
                       {"content": "WXDOTS_TEST_REAL_AT WXDOTS_TEST_FAKE_AT"}):
            self.assertIsNone(core.observation(row(**change), "wxid_self", "room@chatroom", "WXDOTS_TEST"))

    def test_sender_cannot_fall_back_to_nickname_or_numeric_id(self):
        for sender in (None, "", "2"):
            with self.assertRaises(core.ProbeError):
                core.observation(row(sender_username=sender), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        record = core.observation(row(sender_username="wxid_self"), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        self.assertTrue(record["from_self"])

    def test_observation_contains_no_raw_body_or_identifiers(self):
        record = core.observation(row(), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        encoded = json.dumps(record)
        for secret in ("wxid_self", "wxid_member", "room@chatroom", "WXDOTS_TEST"):
            self.assertNotIn(secret, encoded)
        self.assertNotIn("content", record)

    def test_identical_local_ids_in_other_shards_are_distinct(self):
        a = core.observation(row(), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        b = core.observation(row(shard="message/message_1.db"), "wxid_self", "room@chatroom", "WXDOTS_TEST")
        self.assertNotEqual(a["event_id"], b["event_id"])

    def test_journal_replay_and_restart_are_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            journal = core.Journal(path)
            journal.initialize({"bound": True}, {"message/message_0.db": 0})
            record = core.observation(row(), "wxid_self", "room@chatroom", "WXDOTS_TEST")
            journal.commit_batch("message/message_0.db", 1, [record])
            journal.close()
            journal = core.Journal(path)
            journal.commit_batch("message/message_0.db", 0, [record])
            self.assertEqual(journal.cursors()["message/message_0.db"], 1)
            self.assertEqual(journal.report()["observations"], 1)
            with self.assertRaises(core.ProbeError):
                journal.commit_batch("unbound", 1, [record])
            self.assertEqual(journal.report()["observations"], 1)
            journal.close()

    def test_unknown_controls_do_not_enable_sending(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = core.Journal(Path(directory) / "state.sqlite3")
            journal.initialize({}, {"message/message_0.db": 0})
            for i, case in enumerate(("PLAIN", "REAL_AT", "FAKE_AT"), 1):
                record = core.observation(row(local_id=i, content="WXDOTS_TEST_" + case, source=None),
                                          "wxid_self", "room@chatroom", "WXDOTS_TEST")
                journal.commit_batch("message/message_0.db", i, [record])
            report = journal.report()
            self.assertFalse(report["mention_controls_passed"])
            self.assertFalse(report["send_enabled"])
            self.assertFalse(report["dot_enabled"])
            journal.close()

    def test_source_tampering_rejected_before_import(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db.py"
            path.write_text("raise RuntimeError('untrusted')")
            with self.assertRaises(core.ProbeError):
                replica.verify_source(path)

    def test_raw_upstream_errors_are_not_printed(self):
        out = io.StringIO()
        with patch("wechat_probe.__main__.discover", side_effect=RuntimeError("secret-account")):
            with contextlib.redirect_stdout(out):
                self.assertEqual(main(["discover"]), 2)
        self.assertNotIn("secret-account", out.getvalue())

    def test_sql_read_uses_bound_table_shard_identity_and_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "fixture.sqlite3"
            conn = sqlite3.connect(path)
            table = "Msg_" + hashlib.md5(b"room@chatroom").hexdigest()
            conn.execute("CREATE TABLE Name2Id(user_name TEXT)")
            conn.execute("INSERT INTO Name2Id VALUES ('wxid_member')")
            conn.execute(f'CREATE TABLE "{table}"(local_id INTEGER,local_type INTEGER,create_time INTEGER,'
                         'message_content TEXT,source TEXT,real_sender_id INTEGER)')
            for i in (1, 2):
                conn.execute(f'INSERT INTO "{table}" VALUES (?,1,100,?,?,1)',
                             (i, "WXDOTS_TEST_REAL_AT", row()["source"]))
            conn.commit()
            conn.close()
            class DB:
                def _open(self, shard):
                    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
                    c.row_factory = sqlite3.Row
                    return c
            self.assertEqual(replica.read_shard(DB(), "room@chatroom", "message/message_0.db", None), 2)
            rows = replica.read_shard(DB(), "room@chatroom", "message/message_0.db", 1)
            self.assertEqual([r["local_id"] for r in rows], [2])
            self.assertEqual(rows[0]["sender_username"], "wxid_member")
            self.assertEqual(replica.read_shard(DB(), "another@chatroom", "message/message_0.db", 0), [])


if __name__ == "__main__":
    unittest.main()
