#!/usr/bin/env python3
"""Make each device's status badge from devices/status.json.

    python3 tools/repo/device_status.py

devices/status.json is the one place a device's status is set. A README's badge
is a shields.io endpoint badge, which reads its words and colour from a file: this
writes one such file per device, under devices/status/, so the colour follows the
status as the words do. Run it after editing devices/status.json;
tests/repo/test_device_status.py fails until you do.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import kinds  # noqa: E402

BADGES = ROOT / "devices" / "status"


def badge(kind: kinds.Kind) -> str:
    """The badge file's text for `kind`, as shields.io's endpoint badge reads it."""
    return json.dumps({"schemaVersion": 1, "label": "Status", "message": kind.status.label,
                       "color": kind.status.colour}, indent=2) + "\n"


def stale() -> list[Path]:
    """The badge files that are missing or say something else, and those of no kind."""
    wanted = {BADGES / f"{kind.key}.json": badge(kind) for kind in kinds.EVERY}
    found = [path for path, text in wanted.items()
             if not path.exists() or path.read_text(encoding="utf-8") != text]
    return found + [path for path in BADGES.glob("*.json") if path not in wanted]


def main() -> int:
    BADGES.mkdir(exist_ok=True)
    for kind in kinds.EVERY:
        (BADGES / f"{kind.key}.json").write_text(badge(kind), encoding="utf-8")
    for path in BADGES.glob("*.json"):
        if path.stem not in {kind.key for kind in kinds.EVERY}:
            path.unlink()
    print(f"Badges written to {BADGES.relative_to(ROOT)}/ for {len(kinds.EVERY)} devices.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
