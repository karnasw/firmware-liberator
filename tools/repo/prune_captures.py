#!/usr/bin/env python3
"""What in captures/ can be made again, and has not been used for a while.

    python3 tools/repo/prune_captures.py [--days 14]            what it would remove, and how much
    python3 tools/repo/prune_captures.py [--days 14] --delete   remove it

Only what is listed in PRUNABLE, every part of it made again by the next run, build
or prep: an emulator's device and its computer's copy of the checkout, the suites'
logs, the build caches and the earlier releases' trees. A backup anywhere in them
is kept, as a device's stock backup cannot be made again (AGENTS.md); so are the
starting devices prep saved, the public dumps and everything not listed. An emulator
running keeps its state.
"""

from __future__ import annotations

import argparse
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "captures"
# What may go, by its path in captures/, and what it is.
PRUNABLE = {
    "emulation/state": "an Espressif emulator's device and its computer's copy of the checkout",
    "emulation/state-*": "an Espressif emulator's device and its computer's copy of the checkout",
    "emulation/beken/state": "a Beken emulator's device and its computer's copy of the checkout",
    "emulation/beken/state-*": "a Beken emulator's device and its computer's copy of the checkout",
    "emulation/legacy/*": "an earlier release's tree, for the legacy scenarios",
    "emulation/beken/legacy/*": "an earlier release's tree, for the Beken update scenarios",
    "emulation/*/logs": "a suite's logs",
    "emulation/beken/*/logs": "a Beken suite's logs",
    "emulation/public-devices": "the public devices' check's logs",
    "emulation/device-check": "the emulator check's logs",
    "esphome-build": "ESPHome's build cache",
    "firmware-build": "the firmware build cache",
    "openbeken-build": "OpenBeken's build cache",
    "unattended": "unattended runs' screens",
}


def protected(path: Path) -> bool:
    """A backup, wherever it is: never removed."""
    return any("backup" in part.lower() for part in path.parts)


def running_states() -> set[Path] | None:
    """The state of each emulator running now; None where Docker cannot say."""
    found = subprocess.run(["docker", "ps", "--format", "{{.Names}}"], capture_output=True, text=True, check=False)
    if found.returncode:
        return None
    states = set()
    for name in found.stdout.split():
        if name == "autoflash-emu":
            states.add(CAPTURES / "emulation/state")
        elif name.startswith("autoflash-emu-") and name.removeprefix("autoflash-emu-").isdigit():
            states.add(CAPTURES / f"emulation/state-{name.removeprefix('autoflash-emu-')}")
        elif name == "autoflash-beken-emu":
            states.add(CAPTURES / "emulation/beken/state")
        elif name.startswith("autoflash-beken-emu-") and name.removeprefix("autoflash-beken-emu-").isdigit():
            states.add(CAPTURES / f"emulation/beken/state-{name.removeprefix('autoflash-beken-emu-')}")
    return states


def candidates(captures: Path) -> list[tuple[Path, str]]:
    """Each, once: a suite's logs inside an emulator's state go with the state."""
    found: dict[Path, str] = {}
    for pattern, what in PRUNABLE.items():
        for path in sorted(captures.glob(pattern)):
            if path.is_dir():
                found.setdefault(path, what)
    return [(path, what) for path, what in found.items()
            if not any(other != path and other in path.parents for other in found)]


def walk(path: Path):
    """Every file and link under `path`, with its size and age, not following links."""
    for directory, _dirs, files in os.walk(path):
        for name in files:
            file = Path(directory, name)
            try:
                info = file.lstat()
            except OSError:
                continue
            yield file, info.st_size, info.st_mtime


def last_used(path: Path) -> float:
    return max((mtime for _file, _size, mtime in walk(path)), default=path.lstat().st_mtime)


def remove(path: Path) -> tuple[int, int]:
    """Everything under `path` but its backups; the bytes freed, and the files that would not go."""
    freed = refused = 0
    for file, size, _mtime in walk(path):
        if protected(file.relative_to(CAPTURES)):
            continue
        try:
            os.chmod(file.parent, os.stat(file.parent).st_mode | stat.S_IWUSR)
            file.unlink()
            freed += size
        except OSError:
            refused += 1
    for directory, dirs, _files in os.walk(path, topdown=False):
        for name in dirs:
            try:
                Path(directory, name).rmdir()
            except OSError:
                pass   # a backup in it, or a file that would not go
    try:
        path.rmdir()
    except OSError:
        pass
    return freed, refused


def size(path: Path) -> int:
    return sum(file_size for file, file_size, _mtime in walk(path) if not protected(file.relative_to(CAPTURES)))


def human(count: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if count < 1024 or unit == "GB":
            return f"{count:.1f} {unit}" if unit != "B" else f"{count} B"
        count /= 1024
    return f"{count} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--days", type=float, default=14, help="unused for this long (default 14)")
    parser.add_argument("--delete", action="store_true", help="remove it; without, only say what would go")
    args = parser.parse_args(argv)
    running = running_states()
    if running is None:
        print("Docker cannot say which emulators run: their states are left alone.", file=sys.stderr)
    cutoff = time.time() - args.days * 86400
    total = 0
    for path, what in candidates(CAPTURES):
        name = path.relative_to(CAPTURES)
        is_state = what.startswith(("an Espressif emulator", "a Beken emulator"))
        if is_state and (running is None or path in running):
            continue
        if last_used(path) > cutoff:
            continue
        if args.delete:
            freed, refused = remove(path)
            total += freed
            print(f"removed  {human(freed):>9}  {name}  ({what})"
                  + (f": {refused} files would not go (another user's, from a container?)" if refused else ""))
        else:
            amount = size(path)
            total += amount
            print(f"would go {human(amount):>9}  {name}  ({what})")
    print(f"{'Freed' if args.delete else 'Would free'} {human(total)}"
          + ("" if args.delete else ": --delete removes it") + ". Backups are kept wherever they are.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
