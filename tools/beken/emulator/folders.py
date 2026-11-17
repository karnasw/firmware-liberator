#!/usr/bin/env python3
"""Where the Beken emulator keeps what it makes, in captures/emulation/beken/,
laid out as the Espressif emulator's is in captures/emulation/:

    state/, state-N/    a device (beken-emu.sh device, --instance N) and its computer's copy
    saved/              the starting devices the fault suite's prep saved
    legacy/<release>/   an earlier release's tree, for the update scenarios
    suite/, suite-*/    the fault suite's results and logs
    firmwares/          the public dumps fetched from upstream (beken-emu.sh dump)
    rebuilt/            a whole flash rebuilt for a device with no public dump
                        (tools/repo/emulator_check.py)

    python3 -I tools/beken/emulator/folders.py migrate

moves the folders an earlier checkout named after the S61 (captures/emulation/s61/,
s61-suite/, beken-firmwares/ ...) to these, once: beken-emu.sh and the fault suite
do it before they use them. All of them are emulator state that can be made
again. None is moved while a container uses it, and none over a folder already
there.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
EMU = ROOT / "captures/emulation"
BEKEN = EMU / "beken"


def state(instance: int | None = None) -> Path:
    """A device's state: beken-emu.sh's, or with --instance N its Nth's."""
    return BEKEN / ("state" if instance is None else f"state-{instance}")


SAVED = BEKEN / "saved"
LEGACY = BEKEN / "legacy"
SUITE = BEKEN / "suite"
FIRMWARES = BEKEN / "firmwares"
REBUILT = BEKEN / "rebuilt"


def old_names(emulation: Path) -> list[tuple[Path, Path]]:
    """Each folder an earlier checkout made that is there, and where it goes now."""
    beken = emulation / "beken"
    moves = []
    old = emulation / "s61"
    if old.is_dir():
        for path in sorted(old.iterdir()):
            device = re.fullmatch(r"autoflash-s61(?:-(\d+))?", path.name)
            if device:
                moves.append((path, beken / ("state" if device.group(1) is None else f"state-{device.group(1)}")))
            elif path.name == "saved":
                moves.append((path, beken / "saved"))
            elif path.name.startswith("earlier-"):
                moves.append((path, beken / "legacy" / path.name.removeprefix("earlier-")))
    for name, new in (("s61-suite", "suite"), ("s61-openbeken", "suite-openbeken"), ("beken-firmwares", "firmwares")):
        if (emulation / name).is_dir():
            moves.append((emulation / name, beken / new))
    return moves


def mounted() -> list[Path] | None:
    """Every host path a running container has mounted; None where Docker
    cannot say. With no Docker at all, nothing is."""
    if shutil.which("docker") is None:
        return []
    running = subprocess.run(["docker", "ps", "-q"], capture_output=True, text=True, check=False)
    if running.returncode:
        return None
    ids = running.stdout.split()
    if not ids:
        return []
    found = subprocess.run(["docker", "inspect", "-f", '{{range .Mounts}}{{println .Source}}{{end}}', *ids],
                           capture_output=True, text=True, check=False)
    if found.returncode:
        return None
    return [Path(os.path.realpath(line)) for line in found.stdout.splitlines() if line.strip()]


def migrate(emulation: Path = EMU, sources=mounted) -> list[str]:
    """Move the old folders to their new names: all of them, or none while a
    container uses any. What was done, or why not, as one line (none if
    there was nothing to move)."""
    moves = old_names(emulation)
    if not moves:
        return []
    used = sources()
    if used is None:
        return ["The Beken emulator's old folders are not moved to captures/emulation/beken/: "
                "Docker cannot say whether a container uses them."]
    olds = [Path(os.path.realpath(old)) for old, _new in moves]
    if any(path == old or old in path.parents for path in used for old in olds):
        return ["The Beken emulator's old folders are not moved to captures/emulation/beken/ "
                "while a container uses them: stop it (docker stop) and run this again."]
    moved, kept = [], []
    for old, new in moves:
        if new.exists():
            kept.append(f"{old.relative_to(emulation)} (beken/{new.relative_to(emulation / 'beken')} is there)")
            continue
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
        except OSError as exc:
            kept.append(f"{old.relative_to(emulation)} ({exc.strerror or exc})")
            continue
        moved.append(f"{old.relative_to(emulation)} to beken/{new.relative_to(emulation / 'beken')}")
    old = emulation / "s61"
    try:
        old.rmdir()
    except OSError:
        pass   # not there, or something else in it: left as it is
    line = []
    if moved:
        line.append("Moved the Beken emulator's folders in captures/emulation/, now named for it: " + ", ".join(moved))
    if kept:
        line.append(("and left " if moved else "Left the Beken emulator's old folders ") + ", ".join(kept))
    return ["; ".join(line) + "."]


def main(argv: list[str]) -> int:
    if argv != ["migrate"]:
        print(__doc__, file=sys.stderr)
        return 2
    for line in migrate():
        print(line, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
