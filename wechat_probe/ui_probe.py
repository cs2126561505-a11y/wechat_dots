"""Bounded, observation-only UI Automation check for the admitted Windows guest.

The worker reads properties only. It never materializes controls, reads process
memory, invokes patterns, changes focus, or sends input. Private node names stay
in the guest's restricted run directory; the console receives fixed aggregates.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import secrets
import subprocess
from pathlib import Path

from .__main__ import emit, private_run
from .core import ProbeError
from .isolation import require_live_admission

MAX_NODES = 256
MAX_DEPTH = 24
MAX_SECONDS = 30
MAX_REPORT_BYTES = 2 * 1024 * 1024
WINDOWS = os.name == "nt"
CONTROL_TYPES = frozenset("Button Calendar CheckBox ComboBox Edit Hyperlink Image ListItem List Menu MenuBar MenuItem ProgressBar RadioButton ScrollBar Slider Spinner StatusBar Tab TabItem Text ToolBar ToolTip Tree TreeItem Custom Group Thumb DataGrid DataItem Document SplitButton Window Pane Header HeaderItem Table TitleBar Separator SemanticZoom AppBar".split())
STATUSES = frozenset(("complete", "node_limit", "depth_limit", "time_limit", "window_limit", "provider_error", "no_windows", "scope_mismatch"))
MSAA_STATUSES = frozenset(("s_ok", "no_interface", "invalid_argument", "other_failure", "invalid_result", "child_count_failed", "invalid_child_count"))

# Kept as an embedded, fixed source so no external automation package or local
# profile script can add actions. The worker runs in an isolated MTA process.
WORKER_SOURCE = r'''
param([int]$TargetProcessId,[long]$StartedMilliseconds,[int]$NodeLimit,[int]$Seconds,[string]$OutputPath,[ValidateSet('control','raw')][string]$View='control',[switch]$MsaaClient)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
try {
    Add-Type -AssemblyName UIAutomationClient,UIAutomationTypes,WindowsBase,Accessibility
    $source=@'
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Windows.Automation;
using Accessibility;

public sealed class ReadOnlyNode {
    public int id, parent, depth;
    public string control_type, name, automation_id, class_name;
    public bool enabled, offscreen, password, value_pattern, text_pattern;
}
public sealed class ReadOnlySnapshot {
    public int schema = 1, process_id, windows, errors;
    public long process_started_ms;
    public bool session_verified;
    public string status = "complete";
    public string view;
    public bool msaa_requested;
    public List<MsaaResult> msaa_results = new List<MsaaResult>();
    public List<ReadOnlyNode> nodes = new List<ReadOnlyNode>();
}
public sealed class MsaaResult {
    public string status;
    public int? child_count;
}
public sealed class ReadOnlyInspector {
    private delegate bool EnumCallback(IntPtr hwnd, IntPtr argument);
    [DllImport("user32.dll")] private static extern bool EnumWindows(EnumCallback callback, IntPtr argument);
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);
    [DllImport("user32.dll")] private static extern bool IsWindowVisible(IntPtr hwnd);
    [DllImport("oleacc.dll", ExactSpelling=true, PreserveSig=true)]
    private static extern int AccessibleObjectFromWindow(IntPtr hwnd, uint objectId, ref Guid interfaceId,
        [MarshalAs(UnmanagedType.Interface)] out object accessible);
    private readonly int pid, nodeLimit, seconds;
    private readonly long started;
    private readonly Stopwatch watch = Stopwatch.StartNew();
    private readonly ReadOnlySnapshot result = new ReadOnlySnapshot();
    private readonly List<IntPtr> windows = new List<IntPtr>();
    private readonly TreeWalker walker;
    private bool halted;
    private int visited;
    private ReadOnlyInspector(int targetPid, long startedMilliseconds, int limit, int duration, bool raw, bool msaa) {
        pid = targetPid; started = startedMilliseconds; nodeLimit = limit; seconds = duration;
        result.process_id = pid; result.process_started_ms = started;
        result.view = raw ? "raw" : "control"; result.msaa_requested = msaa;
        walker = raw ? TreeWalker.RawViewWalker : TreeWalker.ControlViewWalker;
    }
    private bool SameProcessAndSession() {
        using (Process target = Process.GetProcessById(pid))
        using (Process current = Process.GetCurrentProcess()) {
            long at = (target.StartTime.ToUniversalTime().Ticks - new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc).Ticks) / TimeSpan.TicksPerMillisecond;
            return !target.HasExited && target.SessionId == current.SessionId && Math.Abs(at - started) <= 2;
        }
    }
    private bool Budget() {
        if (halted) return false;
        if (watch.Elapsed.TotalSeconds >= seconds) { result.status = "time_limit"; halted = true; return false; }
        if (visited >= nodeLimit) { result.status = "node_limit"; halted = true; return false; }
        return true;
    }
    private static string Bounded(string value, int limit) {
        if (value == null) return "";
        return value.Length <= limit ? value : value.Substring(0, limit);
    }
    private bool Enumerate(IntPtr hwnd, IntPtr argument) {
        if (!Budget()) return false;
        uint owner;
        GetWindowThreadProcessId(hwnd, out owner);
        if (owner != (uint)pid || !IsWindowVisible(hwnd)) return true;
        if (windows.Count >= 16) { result.status = "window_limit"; halted = true; return false; }
        windows.Add(hwnd);
        return true;
    }
    private void RequestClientObject(IntPtr hwnd) {
        MsaaResult observation = new MsaaResult();
        object accessible = null;
        try {
            Guid iid = new Guid("618736e0-3c3d-11cf-810c-00aa00389b71");
            int hr = AccessibleObjectFromWindow(hwnd, unchecked((uint)-4), ref iid, out accessible);
            if (hr != 0) {
                observation.status = hr == unchecked((int)0x80004002) ? "no_interface" :
                    hr == unchecked((int)0x80070057) ? "invalid_argument" : "other_failure";
            } else {
                IAccessible root = accessible as IAccessible;
                if (root == null) observation.status = "invalid_result";
                else {
                    try {
                        int count = root.accChildCount;
                        observation.status = count >= 0 ? "s_ok" : "invalid_child_count";
                        if (count >= 0) observation.child_count = count;
                    } catch (Exception) { observation.status = "child_count_failed"; }
                }
            }
        } catch (Exception) { observation.status = "other_failure"; }
        finally {
            if (accessible != null && Marshal.IsComObject(accessible)) {
                try { Marshal.ReleaseComObject(accessible); } catch (Exception) { }
            }
        }
        result.msaa_results.Add(observation);
    }
    private void Walk(AutomationElement element, int parent, int depth) {
        if (!Budget()) return;
        visited++;
        try {
            // Process identity is checked before any potentially private name.
            if (element.Current.ProcessId != pid) return;
            ReadOnlyNode node = new ReadOnlyNode();
            node.id = result.nodes.Count; node.parent = parent; node.depth = depth;
            node.password = element.Current.IsPassword;
            node.control_type = element.Current.ControlType.ProgrammaticName;
            node.name = node.password ? "" : Bounded(element.Current.Name, 160);
            node.automation_id = Bounded(element.Current.AutomationId, 96);
            node.class_name = Bounded(element.Current.ClassName, 96);
            node.enabled = element.Current.IsEnabled;
            node.offscreen = element.Current.IsOffscreen;
            node.value_pattern = (bool)element.GetCurrentPropertyValue(AutomationElement.IsValuePatternAvailableProperty);
            node.text_pattern = (bool)element.GetCurrentPropertyValue(AutomationElement.IsTextPatternAvailableProperty);
            result.nodes.Add(node);
            if (!Budget()) return;
            AutomationElement child = walker.GetFirstChild(element);
            if (depth >= 24) {
                if (child != null && result.status == "complete") result.status = "depth_limit";
                return;
            }
            while (child != null && Budget()) {
                Walk(child, node.id, depth + 1);
                if (!Budget()) return;
                child = walker.GetNextSibling(child);
            }
        } catch (Exception) {
            result.errors++;
            if (result.status == "complete") result.status = "provider_error";
        }
    }
    private ReadOnlySnapshot Capture() {
        if (!SameProcessAndSession()) { result.status = "scope_mismatch"; return result; }
        result.session_verified = true;
        EnumCallback callback = Enumerate;
        bool enumerationCompleted = EnumWindows(callback, IntPtr.Zero);
        GC.KeepAlive(callback);
        if (!enumerationCompleted && !halted) { result.status = "provider_error"; result.errors++; }
        result.windows = windows.Count;
        // A window-limit result is deliberately inconclusive; do not expand it.
        foreach (IntPtr hwnd in windows) {
            if (!Budget()) break;
            uint owner;
            GetWindowThreadProcessId(hwnd, out owner);
            if (owner != (uint)pid || !SameProcessAndSession()) { result.status = "scope_mismatch"; break; }
            if (result.msaa_requested) {
                RequestClientObject(hwnd);
                if (!Budget()) break;
                GetWindowThreadProcessId(hwnd, out owner);
                if (owner != (uint)pid || !SameProcessAndSession()) { result.status = "scope_mismatch"; break; }
            }
            try { Walk(AutomationElement.FromHandle(hwnd), -1, 0); }
            catch (Exception) { result.errors++; result.status = "provider_error"; }
        }
        if (!SameProcessAndSession()) result.status = "scope_mismatch";
        else if (windows.Count == 0 && result.status == "complete") result.status = "no_windows";
        return result;
    }
    public static ReadOnlySnapshot Run(int pid, long started, int limit, int seconds, bool raw, bool msaa) {
        if (pid <= 0 || started <= 0 || limit < 1 || limit > 256 || seconds < 1 || seconds > 30)
            throw new ArgumentException("Invalid observation limits.");
        return new ReadOnlyInspector(pid, started, limit, seconds, raw, msaa).Capture();
    }
}
'@
    $references=@([System.Windows.Automation.AutomationElement].Assembly.Location,[System.Windows.Automation.ControlType].Assembly.Location,[System.Windows.Rect].Assembly.Location,[System.Diagnostics.Process].Assembly.Location,[Accessibility.IAccessible].Assembly.Location)
    Add-Type -TypeDefinition $source -ReferencedAssemblies $references
    $snapshot=[ReadOnlyInspector]::Run($TargetProcessId,$StartedMilliseconds,$NodeLimit,$Seconds,($View -eq 'raw'),$MsaaClient.IsPresent)
    $json=$snapshot | ConvertTo-Json -Depth 8 -Compress
    $bytes=[Text.UTF8Encoding]::new($false).GetBytes($json)
    if($bytes.Length -gt 2097152){throw 'Report limit exceeded.'}
    $stream=[IO.File]::Open($OutputPath,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {$stream.Write($bytes,0,$bytes.Length)} finally {$stream.Dispose()}
    exit 0
} catch { exit 2 }
'''


def summarize(snapshot, process_id, started_ms, node_limit, view="control", msaa_client=False):
    """Validate the private report, then construct an allowlisted public result."""
    if (not isinstance(snapshot, dict) or snapshot.get("schema") != 1
            or snapshot.get("process_id") != process_id or snapshot.get("process_started_ms") != started_ms
            or snapshot.get("session_verified") is not True or snapshot.get("status") not in STATUSES
            or snapshot.get("view") != view or snapshot.get("msaa_requested") is not msaa_client):
        raise ProbeError("UI observation identity or format is invalid.")
    if snapshot["status"] == "scope_mismatch":
        raise ProbeError("UI observation process or Windows session changed.")
    for key, limit in (("windows", 16), ("errors", node_limit + 16)):
        if type(snapshot.get(key)) is not int or not 0 <= snapshot[key] <= limit:
            raise ProbeError("UI observation counts are invalid.")
    nodes = snapshot.get("nodes")
    if not isinstance(nodes, list) or len(nodes) > node_limit:
        raise ProbeError("UI observation exceeded the node limit.")
    msaa = snapshot.get("msaa_results")
    if not isinstance(msaa, list) or len(msaa) > snapshot["windows"] or (not msaa_client and msaa):
        raise ProbeError("MSAA observation results are invalid.")
    public_msaa = []
    for observation in msaa:
        if not isinstance(observation, dict) or observation.get("status") not in MSAA_STATUSES:
            raise ProbeError("MSAA observation category is invalid.")
        count = observation.get("child_count")
        if observation["status"] == "s_ok":
            if type(count) is not int or not 0 <= count <= 2147483647:
                raise ProbeError("MSAA observation count is invalid.")
        elif count is not None:
            raise ProbeError("MSAA failure cannot prove a child count.")
        public_msaa.append({"status": observation["status"], "child_count": count})
    types = {}
    value_patterns = text_patterns = input_candidates = 0
    for index, node in enumerate(nodes):
        if (not isinstance(node, dict) or node.get("id") != index
                or type(node.get("parent")) is not int or not -1 <= node["parent"] < index
                or type(node.get("depth")) is not int or not 0 <= node["depth"] <= MAX_DEPTH):
            raise ProbeError("UI observation tree is invalid.")
        for key, limit in (("name", 160), ("automation_id", 96), ("class_name", 96)):
            if not isinstance(node.get(key), str) or len(node[key]) > limit:
                raise ProbeError("UI observation properties exceeded their limits.")
        for key in ("value_pattern", "text_pattern", "password", "enabled", "offscreen"):
            if type(node.get(key)) is not bool:
                raise ProbeError("UI observation property flags are invalid.")
        kind = node.get("control_type")
        if not isinstance(kind, str) or not kind.startswith("ControlType.") or kind[12:] not in CONTROL_TYPES:
            raise ProbeError("UI observation control type is invalid.")
        kind = kind[12:]
        types[kind] = types.get(kind, 0) + 1
        value_patterns += node["value_pattern"]
        text_patterns += node["text_pattern"]
        input_candidates += kind == "Edit" and node["enabled"] and not node["password"]
    return {"status": snapshot["status"], "view": view, "msaa_requested": msaa_client, "msaa_results": public_msaa,
            "ui_observation_only": True, "windows": snapshot["windows"],
            "nodes": len(nodes), "control_types": types, "value_pattern_nodes": value_patterns,
            "text_pattern_nodes": text_patterns, "input_candidates": input_candidates,
            "provider_errors": snapshot["errors"], "private_report_written": True, "send_enabled": False}


def inspect_guest(run_name="ui-readonly", seconds=15, max_nodes=MAX_NODES, view="control", msaa_client=False):
    # This call precedes filesystem setup, process launch and all UI API use.
    lease = require_live_admission()
    if not WINDOWS:
        raise ProbeError("UI observation requires the admitted Windows guest.")
    if type(seconds) is not int or not 1 <= seconds <= MAX_SECONDS or type(max_nodes) is not int or not 1 <= max_nodes <= MAX_NODES:
        raise ProbeError("UI observation limits are invalid.")
    if view not in ("control", "raw") or type(msaa_client) is not bool:
        raise ProbeError("UI observation comparison mode is invalid.")
    process = lease["process"]
    if (type(process["pid"]) is not int or process["pid"] <= 0 or type(process["started"]) not in (int, float)
            or not math.isfinite(process["started"]) or process["started"] <= 0):
        raise ProbeError("UI observation process binding is invalid.")
    started_ms = int(process["started"] * 1000)
    run = private_run(run_name)
    suffix = secrets.token_hex(8)
    script = run / ("ui-worker-" + suffix + ".ps1")
    output = run / ("ui-snapshot-" + suffix + ".json")
    try:
        with script.open("x", encoding="utf-8-sig") as handle:
            handle.write(WORKER_SOURCE)
        powershell = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
        command = [str(powershell), "-NoLogo", "-NoProfile", "-NonInteractive", "-Mta",
                   "-File", str(script), "-TargetProcessId", str(process["pid"]), "-StartedMilliseconds", str(started_ms),
                   "-NodeLimit", str(max_nodes), "-Seconds", str(seconds), "-OutputPath", str(output), "-View", view]
        if msaa_client:
            command.append("-MsaaClient")
        # DEVNULL prevents private provider diagnostics entering a transcript or
        # being retained unboundedly in Python. run() kills/reaps on timeout.
        try:
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=seconds + 10,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), check=False)
        except subprocess.TimeoutExpired:
            raise ProbeError("UI observation worker timed out and was stopped.") from None
        if result.returncode != 0:
            raise ProbeError("UI observation worker failed; no private diagnostics were printed.")
        if require_live_admission() != lease:
            raise ProbeError("UI observation admission changed; discard this snapshot.")
        if (not output.is_file() or output.is_symlink() or output.resolve() != output.absolute()
                or output.stat().st_size > MAX_REPORT_BYTES):
            raise ProbeError("UI observation report is unavailable or exceeds its limit.")
        with output.open("rb") as handle:
            body = handle.read(MAX_REPORT_BYTES + 1)
        if len(body) > MAX_REPORT_BYTES:
            raise ProbeError("UI observation report exceeds its limit.")
        snapshot = json.loads(body.decode("utf-8-sig"))
        return summarize(snapshot, process["pid"], started_ms, max_nodes, view, msaa_client)
    finally:
        # The generated script is ours; no recursive cleanup or wildcard delete.
        if script.is_file() and not script.is_symlink():
            script.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Observe UI properties inside the admitted guest only; no input or send actions.")
    parser.add_argument("--run", default="ui-readonly")
    parser.add_argument("--seconds", type=int, default=15)
    parser.add_argument("--max-nodes", type=int, default=MAX_NODES)
    parser.add_argument("--view", choices=("control", "raw"), default="control")
    parser.add_argument("--msaa-client", action="store_true", help="Request the bound window's standard OBJID_CLIENT interface before UIA observation.")
    args = parser.parse_args(argv)
    try:
        emit(inspect_guest(args.run, args.seconds, args.max_nodes, args.view, args.msaa_client))
        return 0
    except KeyboardInterrupt:
        emit({"status": "stopped", "ui_observation_only": True, "send_enabled": False})
        return 130
    except ProbeError as exc:
        emit({"status": "blocked", "reason": str(exc), "ui_observation_only": True, "send_enabled": False})
        return 2
    except Exception as exc:
        emit({"status": "blocked", "reason": "UI observation failed without fallback.",
              "error_type": type(exc).__name__, "ui_observation_only": True, "send_enabled": False})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
