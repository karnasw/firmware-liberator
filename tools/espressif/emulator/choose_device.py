#!/usr/bin/env python3
"""Choose which of your devices the emulator runs, from their Sonoff backups.

Every conversion keeps the device's Sonoff firmware in
captures/devices/<mac>/sonoff-backup/ (tools/autoflash/records.py). This lists
them, by MAC address, and writes the chosen one's directory and MAC to --out,
a line each, for emu.sh. A dump in captures/emulation/ (flash.bin and
efuse.bin), the way the emulator used to be set up, is offered too.

The backups are only read: emu.sh boots a working copy in its state directory.

    python -I tools/espressif/emulator/choose_device.py --out FILE [--device MAC]

With one device it is chosen without asking; with several, a menu asks - or,
with nobody at the keyboard, the device chosen last time is used.

With none here - captures/ is only ever a working copy - the maintainer's
sealed backup is fetched from its private package and filed here first
(tools/repo/private_backup.py restore, .github/private-firmware-backup.md), given its key and
a login that can read the package; and a test copy asked for by --device that
is not here yet is made from it (copy_device.py). AUTOFLASH_EMU_NO_RESTORE=1 says
not to.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.autoflash import records  # noqa: E402
from tools.autoflash.prompts import QuitRun, ask_choice  # noqa: E402

LEGACY = ROOT / "captures" / "emulation"
HINT = [
    "Each device is named by its MAC address",
]


@dataclass(frozen=True)
class Choice:
    mac: str
    directory: Path
    label: str


def choices() -> list[Choice]:
    found = [Choice(b.mac, b.directory, b.describe()) for b in records.backups() if b.family == "espressif"]
    flash, efuse = LEGACY / "flash.bin", LEGACY / "efuse.bin"
    if flash.is_file() and efuse.is_file():
        mac = records.mac_from_efuse(efuse.read_bytes())
        if all(c.mac != mac for c in found):
            found.append(Choice(mac, LEGACY, f"{mac}  (the dump in captures/emulation/)"))
    return found


def previous(out: Path) -> str | None:
    try:
        lines = out.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    return lines[1] if len(lines) > 1 else None


def choose(found: list[Choice], wanted: str | None, last: str | None, interactive: bool) -> Choice:
    if wanted:
        try:
            key = records.folder_name(wanted)
        except ValueError as exc:
            raise SystemExit(f"--device: {exc}") from exc
        match = [c for c in found if records.folder_name(c.mac) == key]
        if not match:
            raise SystemExit(f"No Sonoff backup of a device with MAC {wanted}. Backed up: "
                             + (", ".join(c.mac for c in found) or "none"))
        return match[0]
    if len(found) == 1:
        return found[0]
    # The one used last time first, so pressing 1 carries on with it.
    found = sorted(found, key=lambda c: c.mac != last)
    if interactive:
        return found[ask_choice("Which device should the emulator run", [c.label for c in found],
                                help_lines=HINT)]
    if last and found[0].mac == last:
        return found[0]
    raise SystemExit("Several devices are backed up; say which with --device <MAC>: "
                     + ", ".join(c.mac for c in found))


def restored(wanted: str | None) -> list[Choice]:
    """The devices here once the sealed backup is - and the test copy asked for."""
    from tools.repo import private_backup
    from tools.espressif.emulator import copy_device
    try:
        mac = private_backup.restore()
    except private_backup.SealError as exc:
        print(f"The sealed Sonoff backup could not be restored: {exc}\n"
              "(it needs AUTOFLASH_BACKUP_KEY and a GitHub login that can read the package:\n"
              ".github/private-firmware-backup.md)", file=sys.stderr)
        return choices()
    print(f"Restored the sealed Sonoff backup of {mac} (tools/repo/private_backup.py).")
    if wanted and records.folder_name(wanted) != records.folder_name(mac) and \
            not (records.DEVICES / records.folder_name(wanted)).exists():
        copy_device.copy(mac, wanted)
        print(f"Made the test copy {wanted} from it (copy_device.py).")
    return choices()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--device", help="the MAC address of the device to emulate")
    args = parser.parse_args(argv)
    try:
        records.take_over_earlier_names()
    except records.EarlierNames as error:
        sys.exit(str(error))

    found = choices()
    try:
        wanted = records.folder_name(args.device) if args.device else None
    except ValueError:
        wanted = None  # choose() says what is wrong with it
    missing = not found or (wanted is not None and all(records.folder_name(c.mac) != wanted for c in found))
    if missing and os.environ.get("AUTOFLASH_EMU_NO_RESTORE") != "1":
        found = restored(args.device)
    if not found:
        print("No Sonoff backup to emulate yet. A conversion keeps one for each device, in\n"
              "captures/devices/<MAC>/sonoff-backup/; tools/espressif/flash_readout.py takes one without\n"
              "converting. See docs/emulator.md.", file=sys.stderr)
        return 1
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    try:
        chosen = choose(found, args.device, previous(args.out), interactive)
    except QuitRun:
        return 130
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(f"{chosen.directory}\n{chosen.mac}\n", encoding="utf-8")
    print(f"Emulating {chosen.label}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
