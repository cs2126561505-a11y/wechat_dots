import copy
import json
import subprocess
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wechat_probe import ui_probe
from wechat_probe.core import ProbeError


class UiProbeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # Windows may expose TEMP through an 8.3 alias (for example WECHAT~1).
        # Production private_run starts under replica.ROOT, which is resolved
        # already; give the simulated private directory the same invariant.
        self.root = Path(self.tmp.name).resolve()
        self.lease = {"process": {"pid": 42, "started": 1000.125}, "issued_at": 1001, "expires_at": 1901}
        self.node = {"id": 0, "parent": -1, "depth": 0, "control_type": "ControlType.Edit",
                     "name": "PRIVATE CHAT CONTENT", "automation_id": "PRIVATE ID", "class_name": "PRIVATE CLASS",
                     "enabled": True, "offscreen": False, "password": False, "value_pattern": True, "text_pattern": False}
        self.snapshot = {"schema": 1, "process_id": 42, "process_started_ms": 1000125,
                         "session_verified": True, "windows": 1, "errors": 0,
                         "status": "complete", "nodes": [self.node]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_live_gate_precedes_files_and_process(self):
        with patch.object(ui_probe, "require_live_admission", side_effect=ProbeError("blocked")), patch.object(ui_probe, "private_run") as private, patch.object(ui_probe.subprocess, "run") as run, patch.object(ui_probe, "emit"):
            with self.assertRaises(ProbeError):
                ui_probe.inspect_guest()
            self.assertEqual(ui_probe.main([]), 2)
            private.assert_not_called()
            run.assert_not_called()

    def test_limits_and_platform_rejected_before_private_state(self):
        with patch.object(ui_probe, "require_live_admission", return_value=self.lease), patch.object(ui_probe, "private_run") as private, patch.object(ui_probe, "WINDOWS", True):
            for seconds, nodes in ((0, 1), (31, 1), (1, 0), (1, 257), (True, 1), (1, True)):
                with self.subTest(seconds=seconds, nodes=nodes), self.assertRaises(ProbeError):
                    ui_probe.inspect_guest(seconds=seconds, max_nodes=nodes)
            with patch.object(ui_probe, "WINDOWS", False), self.assertRaises(ProbeError):
                ui_probe.inspect_guest()
            private.assert_not_called()

    def test_summary_contains_only_fixed_aggregates(self):
        result = ui_probe.summarize(self.snapshot, 42, 1000125, 256)
        self.assertEqual(result["control_types"], {"Edit": 1})
        self.assertEqual(result["input_candidates"], 1)
        self.assertEqual(result["value_pattern_nodes"], 1)
        self.assertFalse(result["send_enabled"])
        public = json.dumps(result)
        for value in ("PRIVATE", "process_id", "process_started_ms", "automation_id", "class_name"):
            self.assertNotIn(value, public)
        self.snapshot["nodes"][0]["password"] = True
        self.assertEqual(ui_probe.summarize(self.snapshot, 42, 1000125, 256)["input_candidates"], 0)

    def test_summary_rejects_foreign_identity_and_untrusted_properties(self):
        for changes in ({"process_id": 99}, {"process_started_ms": 1000126}, {"session_verified": False},
                        {"status": "scope_mismatch"}, {"windows": 17}, {"errors": -1}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                ui_probe.summarize(dict(self.snapshot, **changes), 42, 1000125, 256)
        for changes in ({"control_type": "ControlType.PRIVATE_CHAT"}, {"name": "x" * 161},
                        {"parent": 1}, {"depth": 9}, {"enabled": "private"}):
            with self.subTest(changes=changes), self.assertRaises(ProbeError):
                ui_probe.summarize(dict(self.snapshot, nodes=[dict(self.node, **changes)]), 42, 1000125, 256)
        with self.assertRaises(ProbeError):
            ui_probe.summarize(dict(self.snapshot, nodes=[self.node, self.node]), 42, 1000125, 1)

    def worker_context(self, effect):
        stack = ExitStack()
        self.addCleanup(stack.close)
        admission = stack.enter_context(patch.object(ui_probe, "require_live_admission", return_value=self.lease))
        stack.enter_context(patch.object(ui_probe, "private_run", return_value=self.root))
        stack.enter_context(patch.object(ui_probe, "WINDOWS", True))
        stack.enter_context(patch.dict(ui_probe.os.environ, {"SystemRoot": str(self.root / "Windows")}))
        run = stack.enter_context(patch.object(ui_probe.subprocess, "run", side_effect=effect))
        return run, admission

    def complete_worker(self, command, **kwargs):
        self.assertEqual(command[command.index("-TargetProcessId") + 1], "42")
        self.assertEqual(command[command.index("-StartedMilliseconds") + 1], "1000125")
        self.assertIn("-Mta", command)
        self.assertNotIn("-ExecutionPolicy", command)
        self.assertEqual(kwargs["timeout"], 25)
        self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(kwargs["stderr"], subprocess.DEVNULL)
        source = Path(command[command.index("-File") + 1])
        self.assertEqual(source.parent, self.root)
        self.assertTrue(source.read_text("utf-8-sig").startswith("\nparam("))
        output = Path(command[command.index("-OutputPath") + 1])
        output.write_text(json.dumps(self.snapshot), encoding="utf-8")
        return SimpleNamespace(returncode=0)

    def test_worker_result_is_private_and_rechecks_admission(self):
        run, admission = self.worker_context(self.complete_worker)
        result = ui_probe.inspect_guest()
        self.assertEqual(result["nodes"], 1)
        self.assertEqual(admission.call_count, 2)
        self.assertEqual(len(list(self.root.glob("ui-snapshot-*.json"))), 1)
        self.assertEqual(list(self.root.glob("*.ps1")), [])
        self.assertEqual(run.call_count, 1)

    def test_changed_admission_rejects_completed_snapshot(self):
        _, admission = self.worker_context(self.complete_worker)
        changed = copy.deepcopy(self.lease)
        changed["issued_at"] += 1
        admission.side_effect = [self.lease, changed]
        with self.assertRaisesRegex(ProbeError, "admission changed"):
            ui_probe.inspect_guest()

    def test_worker_timeout_and_errors_hide_private_diagnostics(self):
        run, _ = self.worker_context(Mock())
        for failure in (subprocess.TimeoutExpired(["PRIVATE COMMAND"], 25, output=b"PRIVATE CONTENT", stderr=b"PRIVATE ERROR"),
                        SimpleNamespace(returncode=2)):
            run.side_effect = failure if isinstance(failure, Exception) else None
            run.return_value = failure
            with patch.object(ui_probe, "emit") as emit:
                self.assertEqual(ui_probe.main([]), 2)
                self.assertNotIn("PRIVATE", json.dumps(emit.call_args.args[0]))
            self.assertEqual(list(self.root.glob("*.ps1")), [])

    def test_oversized_private_report_is_rejected(self):
        def worker(command, **kwargs):
            output = Path(command[command.index("-OutputPath") + 1])
            output.write_bytes(b"x" * (ui_probe.MAX_REPORT_BYTES + 1))
            return SimpleNamespace(returncode=0)
        self.worker_context(worker)
        with self.assertRaisesRegex(ProbeError, "exceeds its limit"):
            ui_probe.inspect_guest()

    def test_fixed_worker_has_only_read_only_native_imports(self):
        # This audit guards the narrow native surface, not incidental formatting.
        import re
        imports = re.findall(r'\[DllImport\("([^\"]+)"\)\] private static extern \w+ (\w+)\(', ui_probe.WORKER_SOURCE)
        self.assertEqual(set(imports), {("user32.dll", "EnumWindows"), ("user32.dll", "GetWindowThreadProcessId"), ("user32.dll", "IsWindowVisible")})
        for forbidden in ("InvokePattern", "SetFocus", "SetValue", "SendInput", "SendKeys", "ReadProcessMemory", "WriteProcessMemory", "VirtualAllocEx", "GetCurrentPattern", "RootElement", "ensure_materialized"):
            self.assertNotIn(forbidden, ui_probe.WORKER_SOURCE)
        self.assertIn("target.SessionId == current.SessionId", ui_probe.WORKER_SOURCE)
        self.assertIn("element.Current.ProcessId != pid", ui_probe.WORKER_SOURCE)


if __name__ == "__main__":
    unittest.main()
