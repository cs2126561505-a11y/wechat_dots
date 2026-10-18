"""Fetch one pinned public source file for static review and restricted loading."""
from __future__ import annotations

import hashlib
import pathlib
import urllib.request

COMMIT = "62503d76a0db069171cb4e42e87090ad285db489"
EXPECTED = "1b8958a0e9a3f8d6c2f8fe89acd72c14bb542c3c16635c31b52e05c0f19b247b"
ROOT = pathlib.Path(__file__).resolve().parents[1]


def main():
    url = f"https://raw.githubusercontent.com/fanyuantaier/wechatauto-replica/{COMMIT}/wechatauto/db.py"
    with urllib.request.urlopen(url, timeout=30) as response:
        content = response.read(1_000_001)
    if len(content) > 1_000_000 or hashlib.sha256(content).hexdigest() != EXPECTED:
        raise RuntimeError("Pinned source integrity check failed")
    target = ROOT / ".cache" / "wechatauto-replica" / "wechatauto" / "db.py"
    if not target.resolve().is_relative_to(ROOT):
        raise RuntimeError("Source cache path escapes the project")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    print("Pinned source verified and saved. No upstream code executed.")


if __name__ == "__main__":
    main()
