"""Admission boundary while a dedicated Windows test environment is unavailable.

Account folders are not a process-memory security boundary. No command-line
switch or environment variable may override this gate. A future change needs
evidence from the dedicated VM/spare machine before enabling any live read.
"""
from .core import ProbeError


BLOCK_REASON = (
    "Live database access is not admitted: use a dedicated Windows VM or spare "
    "computer with only the test account, then verify account and process isolation. "
    "No keys or chat databases were read by this command."
)


def require_live_admission():
    # Deliberately unconditional: the current shared host has not passed review.
    raise ProbeError(BLOCK_REASON)


def assessment(account_count: int | None = None) -> dict:
    return {
        "live_read_enabled": False,
        "isolation_verified": False,
        "required_environment": "dedicated_windows_vm_or_spare_computer",
        "multiple_account_directories": None if account_count is None else account_count > 1,
        "reason": "Account-directory selection cannot isolate a process-wide memory scan.",
    }
