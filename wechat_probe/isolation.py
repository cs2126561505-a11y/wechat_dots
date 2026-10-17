"""Fail-closed, short-lived admission for an inspected dedicated Windows guest.

The host issues a private lease after inspecting VM sharing settings. A lease is
bound to the actual guest, Windows identity, account directory and live process.
It never enables sending or dot triggers.
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from .core import ProbeError

BLOCK_REASON = "Live database access needs a current, inspected dedicated-guest lease."
MAX_LEASE_SECONDS = 900
CLIENT_SHA256 = "a832fb321ddb5f9701dd3a7bb1958804f09b620b9ad328f52e2e352651ab886c"


def lease_path():
    return Path(os.environ.get("LOCALAPPDATA", "")) / "WeChatDotsLab" / "admission.json"


def read_lease():
    path = lease_path()
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 16384:
        raise ProbeError(BLOCK_REASON)
    try:
        lease = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(lease, dict):
            raise ValueError()
        return lease
    except (ValueError, OSError) as exc:
        raise ProbeError("Invalid private admission lease.") from exc


def validate_lease(lease, facts, process, accounts, now=None):
    now = time.time() if now is None else now
    try:
        issued, expires = lease["issued_at"], lease["expires_at"]
        if not all(type(x) in (int, float) for x in (issued, expires)):
            raise ValueError()
        if not issued <= now < expires or not 0 < expires - issued <= MAX_LEASE_SECONDS:
            raise ValueError()
        if lease["schema"] != 1 or lease["scope"] != "test-group-read-only":
            raise ValueError()
        if facts["model"] != "VirtualBox" or facts["elevated"] is not False:
            raise ValueError()
        for field in ("uuid", "machine_guid", "sid", "profile"):
            if not facts[field] or facts[field].casefold() != lease[field].casefold():
                raise ValueError()
        if any(facts[k] != 0 for k in ("network_drives", "smb_connections", "sync_processes")):
            raise ValueError()
        if not all(lease["host_checks"].get(k) is True for k in
                   ("no_shared_folders", "clipboard_disabled", "dragdrop_disabled",
                    "usb_disabled", "nat_without_forwarding", "test_account_only")):
            raise ValueError()
        if process != lease["process"] or accounts != [lease["account_dir"]]:
            raise ValueError()
        if facts["client_sha256"].lower() != CLIENT_SHA256:
            raise ValueError()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ProbeError("Guest, account, process or admission lifetime no longer matches review.") from exc


def runtime_facts():
    command = r'''
$ErrorActionPreference='Stop'
[Console]::OutputEncoding=[Text.UTF8Encoding]::new($false)
$id=[Security.Principal.WindowsIdentity]::GetCurrent()
$p=[Security.Principal.WindowsPrincipal]::new($id)
$smbCount=$null
try { $smbCount=@(Get-SmbConnection -ErrorAction Stop).Count }
catch {
    if($_.FullyQualifiedErrorId -notmatch 'Windows System Error 5,Get-SmbConnection'){throw}
    $report='C:\ProgramData\WeChatDotsLab-Isolation\smb-status.json'
    foreach($item in @((Split-Path $report),$report)){
        if((Get-Item -LiteralPath $item).Attributes -band [IO.FileAttributes]::ReparsePoint){throw 'Redirected SMB report.'}
        $acl=Get-Acl -LiteralPath $item
        $owner=$acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
        if($owner -notin @('S-1-5-32-544','S-1-5-18')){throw 'SMB report owner is not trusted.'}
        foreach($rule in $acl.Access){
            $sid=$rule.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value
            $writeMask=[Security.AccessControl.FileSystemRights]::Write -bor [Security.AccessControl.FileSystemRights]::Delete -bor [Security.AccessControl.FileSystemRights]::ChangePermissions -bor [Security.AccessControl.FileSystemRights]::TakeOwnership -bor [Security.AccessControl.FileSystemRights]::DeleteSubdirectoriesAndFiles
            if($rule.AccessControlType -eq 'Allow' -and ($rule.FileSystemRights -band $writeMask) -and $sid -notin @('S-1-5-32-544','S-1-5-18')){throw 'SMB report is writable without elevation.'}
        }
    }
    $s=Get-Content -LiteralPath $report -Raw -Encoding UTF8 | ConvertFrom-Json
    $age=[DateTimeOffset]::UtcNow.ToUnixTimeSeconds()-$s.checked_at
    if($s.status -ne 'checked' -or $s.query -ne 'Get-SmbConnection' -or $age -lt 0 -or $age -gt 20 -or $null -eq $s.connection_count){throw 'SMB inspection is stale or unavailable.'}
    $smbCount=$s.connection_count
}
[ordered]@{
model=(Get-CimInstance Win32_ComputerSystem).Model
uuid=(Get-CimInstance Win32_ComputerSystemProduct).UUID
machine_guid=(Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Cryptography').MachineGuid
sid=$id.User.Value
user=$id.Name
profile=[Environment]::GetFolderPath('UserProfile')
elevated=$p.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
network_drives=@(Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=4').Count
smb_connections=$smbCount
sync_processes=@(Get-Process -Name OneDrive,Dropbox,GoogleDriveFS -ErrorAction SilentlyContinue).Count
} | ConvertTo-Json -Compress
'''
    result = subprocess.run([os.path.join(os.environ["SystemRoot"], "System32", "WindowsPowerShell",
                                         "v1.0", "powershell.exe"), "-NoProfile", "-Command", command],
                            capture_output=True, encoding="utf-8", check=True, timeout=30,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    return json.loads(result.stdout)


def check_account_path(account, profile):
    account, profile = Path(account), Path(profile)
    if not account.is_absolute() or not account.is_relative_to(profile):
        raise ProbeError("Account data is outside the reviewed local user profile.")
    for path in (account / "db_storage", account, *account.parents):
        if path.is_symlink() or getattr(path.stat(), "st_file_attributes", 0) & 0x400:
            raise ProbeError("Account path contains a redirected filesystem component.")
    if not (account / "db_storage").is_dir():
        raise ProbeError("Reviewed account database directory is missing.")


def require_live_admission(account=None, process=None):
    lease = read_lease()  # Fail before runtime discovery when unconfigured.
    if os.name != "nt" or sys.maxsize <= 2**32:
        raise ProbeError(BLOCK_REASON)
    import hashlib
    import psutil
    from . import replica
    facts = runtime_facts()
    current = replica.process_binding()
    if psutil.Process(current["pid"]).username().casefold() != facts["user"].casefold():
        raise ProbeError("WeChat belongs to a different Windows identity.")
    facts["client_sha256"] = hashlib.sha256(Path(current["exe"]).read_bytes()).hexdigest()
    reviewed = Path(lease.get("account_dir", ""))
    check_account_path(reviewed, facts["profile"])
    accounts = sorted(str(p) for p in reviewed.parent.iterdir() if (p / "db_storage").is_dir())
    validate_lease(lease, facts, current, accounts)
    if account is not None and Path(account) != reviewed:
        raise ProbeError("Requested account differs from admission.")
    if process is not None and process != current:
        raise ProbeError("Requested process differs from admission.")
    return lease


def check_lease_still_active(lease):
    if read_lease() != lease or not lease["issued_at"] <= time.time() < lease["expires_at"]:
        raise ProbeError("Read admission expired or was revoked.")
    # The parent and file ACL were checked at admission. Only elevated operators
    # can replace this report; the monitor refreshes the same SMB query every 5 s.
    try:
        report = json.loads(Path(r"C:\ProgramData\WeChatDotsLab-Isolation\smb-status.json").read_text(encoding="utf-8-sig"))
        validate_smb_report(report)
    except (OSError, ValueError) as exc:
        raise ProbeError("Privileged SMB inspection is unavailable.") from exc
    reviewed = Path(lease["account_dir"])
    check_account_path(reviewed, lease["profile"])
    if sorted(str(p) for p in reviewed.parent.iterdir() if (p / "db_storage").is_dir()) != [str(reviewed)]:
        raise ProbeError("Account directory set changed.")


def validate_smb_report(report, now=None):
    now = time.time() if now is None else now
    if (report.get("status") != "checked" or report.get("query") != "Get-SmbConnection"
            or type(report.get("connection_count")) is not int or report["connection_count"] != 0
            or type(report.get("checked_at")) not in (int, float)
            or not 0 <= now - report["checked_at"] <= 20):
        raise ProbeError("Privileged SMB inspection is stale or reports an active connection.")


def assessment(account_count: int | None = None) -> dict:
    return {
        "live_read_enabled": False,
        "isolation_verified": False,
        "required_environment": "dedicated_windows_vm_with_current_private_lease",
        "multiple_account_directories": None if account_count is None else account_count > 1,
        "reason": "Each live command verifies the private guest, account and process lease.",
    }
