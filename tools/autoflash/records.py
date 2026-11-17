"""Where each device's records live: one directory a device, named by its MAC.

    captures/devices/aa-bb-cc-dd-ee-ff/
        device.json          what the device is: its eWeLink name and id, its model
        sonoff-backup/     its Sonoff firmware, whole, as it came - never rewritten
        runs/20261013-031500/   one conversion: its state, its log, what it read

A run starts in devices/unidentified/runs/ and moves under the device's MAC as soon
as the device is found (A1), before anything is written to it.  A device's MAC
never changes, so a later run - or the emulator - finds its backup by it.

Everything here is private: the backup holds the home Wi-Fi password and the
device's eWeLink keys, and a MAC identifies the device.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.espressif import flashmap

from . import kinds, oslock

ROOT = Path(__file__).resolve().parents[2]
CAPTURES = ROOT / "captures"
DEVICES = CAPTURES / "devices"
# What releases before 3.0.0 called the folder and each device's file, from
# when the tool converted only smart plugs: take_over_earlier_names() renames them.
EARLIER_FOLDER = "plugs"
EARLIER_FILE = "plug.json"
# Runs whose device is not found yet.
UNIDENTIFIED = "unidentified"
BACKUP = "sonoff-backup"
RUNS = "runs"
DEVICE_FILE = "device.json"
MAC_RE = re.compile(r"^(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}$", re.IGNORECASE)
# The eFuse dump's base MAC: words 6 and 7 of EFUSE_RD_MAC_SPI_SYS, bytes reversed.


def folder_name(mac: str) -> str:
    """A MAC as a directory name: lower case, with dashes (Windows allows no colons)."""
    if not MAC_RE.match(mac.strip()):
        raise ValueError(f"not a MAC address: {mac!r}")
    return mac.strip().lower().replace(":", "-")


class EarlierNames(Exception):
    """What an earlier release kept cannot be renamed as things are."""


def take_over_earlier_names() -> None:
    """Records kept by a release before 3.0.0, under the names it gave them -
    captures/plugs/, and plug.json in each device's folder - renamed to
    today's.

    The folder is renamed where it is, in one step: nothing in it is copied
    or rewritten, a Sonoff backup least of all.  Called before a command
    reads or writes any record, and harmless every time after the first.
    Raises EarlierNames, changing nothing, while a run of the earlier release
    still holds a device there, or when both folders exist."""
    earlier = DEVICES.with_name(EARLIER_FOLDER)
    if earlier.is_dir():
        if DEVICES.exists():
            raise EarlierNames(
                f"{earlier} (as releases before 3.0.0 named it) and {DEVICES} both exist. Move each device's "
                f"folder from {earlier.name}/ into {DEVICES.name}/, delete the empty {earlier.name}/, and run again.")
        held = [lock.parent.name.replace("-", ":") for lock in sorted(earlier.glob("*/run.lock"))
                if oslock.held_elsewhere(lock)]
        if held:
            raise EarlierNames(f"a run of an earlier release is converting a device right now ({', '.join(held)}): "
                               "let it finish, then run again.")
        try:
            earlier.rename(DEVICES)
        except OSError as error:
            if earlier.exists() or not DEVICES.is_dir():   # not another run doing the same at this moment
                raise EarlierNames(f"could not rename {earlier} to {DEVICES.name}: {error}. Close anything that "
                                   "has a file in it open, and run again.") from error
    if DEVICES.is_dir():
        for old in DEVICES.glob(f"*/{EARLIER_FILE}"):
            new = old.with_name(DEVICE_FILE)
            if not new.exists():
                old.rename(new)


def earlier_run_name(name: str) -> str:
    """A run's name (runs.run_name) as it is now, from one a release before
    3.0.0 wrote down: plugs/<mac>/runs/<when> is devices/<mac>/runs/<when>."""
    first, slash, rest = name.partition("/")
    return f"{DEVICES.name}/{rest}" if slash and first == EARLIER_FOLDER else name


class DeviceBusy(Exception):
    """Another run is converting this device right now."""


_HELD: dict[str, Any] = {}  # the open lock files, kept for the run's life


def hold(mac: str, run: str = "") -> None:
    """This run's hold on the device, refused while another run has it.

    The operating system's own file lock, held as long as this process lives:
    let go however the run ends, a crash included, so it can never be left
    behind to refuse the next one."""
    if mac in _HELD:
        return
    path = device_dir(mac) / "run.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.seek(0)
        held_by = handle.read().strip()
        handle.close()
        raise DeviceBusy(held_by) from None
    handle.seek(0)
    handle.truncate()
    handle.write(f"run {run or '?'}, process {os.getpid()}, since {time.strftime('%H:%M:%S')}\n")
    handle.flush()
    _HELD[mac] = handle


def busy() -> list[str]:
    """The devices another run is converting right now, each with who has it."""
    found = []
    if DEVICES.is_dir():
        for lock in sorted(DEVICES.glob("*/run.lock")):
            if oslock.held_elsewhere(lock):
                mac = lock.parent.name.replace("-", ":")
                try:
                    held_by = lock.read_text(encoding="utf-8").strip()
                except OSError:
                    held_by = ""
                found.append(f"{mac}{f' ({held_by})' if held_by else ''}")
    return found


def device_dir(mac: str) -> Path:
    return DEVICES / folder_name(mac)


def backup_dir(mac: str) -> Path:
    return device_dir(mac) / BACKUP


def runs_dir(mac: str | None) -> Path:
    return (device_dir(mac) if mac else DEVICES / UNIDENTIFIED) / RUNS


def new_run_dir(mac: str | None) -> Path:
    """A directory for a new run, named by when it started; not yet made."""
    base = runs_dir(mac) / time.strftime("%Y%m%d-%H%M%S")
    path, suffix = base, 2
    while path.exists():
        path, suffix = base.with_name(f"{base.name}-{suffix}"), suffix + 1
    return path


def mac_of_run(directory: Path) -> str | None:
    """The device a run belongs to, from where it is kept; None if not found yet."""
    try:
        relative = directory.resolve().relative_to(DEVICES.resolve())
    except ValueError:
        return None
    name = relative.parts[0] if relative.parts else ""
    return name.replace("-", ":") if MAC_RE.match(name) else None


def mac_from_efuse(efuse: bytes) -> str:
    """The device's MAC from its eFuse registers, as the chip itself reports it."""
    return flashmap.mac_of_efuse(efuse)


def read_device(mac: str) -> dict[str, Any]:
    try:
        return json.loads((device_dir(mac) / DEVICE_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def remember_device(mac: str, **details: Any) -> None:
    """Add what is known about a device to its device.json; empty values are left out."""
    record = read_device(mac)
    record.update({key: value for key, value in details.items() if value not in (None, "")})
    record["mac"] = folder_name(mac).replace("-", ":")
    path = device_dir(mac) / DEVICE_FILE
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# A device's number among those converted on this computer, in the order each
# was first converted here (device.json): for the report, which says how many
# devices one computer converts without saying which computer.
CONVERTED_NUMBER = "converted_number"


def number_conversion(mac: str) -> int:
    """The device's number among those converted on this computer, given it at
    its first conversion here: one more than the highest any device has. Not
    capped, and only from this tool's records: deleting them starts again."""
    number = read_device(mac).get(CONVERTED_NUMBER)
    if isinstance(number, int) and not isinstance(number, bool):
        return number
    number = converted_here() + 1
    remember_device(mac, **{CONVERTED_NUMBER: number})
    return number


def converted_here() -> int:
    """How many devices this computer has converted, as number_conversion
    numbers them: the highest number given, 0 for none."""
    taken = [0]
    for path in DEVICES.glob(f"*/{DEVICE_FILE}"):
        try:
            value = json.loads(path.read_text(encoding="utf-8")).get(CONVERTED_NUMBER)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(value, int) and not isinstance(value, bool):
            taken.append(value)
    return max(taken)


# How a run's backup went, by the outcome its backup step records
# ("backup_outcome"): what its stage line says, and what the report says.
BACKUP_OUTCOMES = {
    "saved": ("complete", "Completed"),
    "existing": ("already done", "Earlier run's"),
    "failed": ("skipped", "Failed"),
    "not wanted": ("skipped", "Not wanted"),
    "unreadable": ("skipped", "Unreadable"),
    "not taken": ("skipped", "Not taken"),
    # An S61 converted straight to ESPHome: only OpenBeken reads its own data
    # out, and the device keeps it, untouched.
}


# A Beken device's own data, where it is in its flash: the factory record (MAC,
# radio calibration), the stock store (Wi-Fi, meter calibration, eWeLink
# identity) and the system store, 0x3e0000 to the end of the 4 MB.
BEKEN_REGION = (0x3E0000, 0x20000)
BEKEN_BACKUP_BIN = "config-0x3e0000.bin"
BEKEN_MANIFEST = "backup.json"


@dataclass(frozen=True)
class Backup:
    """One device's Sonoff backup, as the emulator and later runs see it: an
    Espressif device's whole flash and eFuses, or a Beken device's own data with
    Sonoff's firmware beside it (`family`)."""
    mac: str
    directory: Path
    version: str
    taken: str
    name: str
    family: str = "espressif"

    def describe(self) -> str:
        """For a menu: the MAC first, since that is what tells devices apart."""
        extra = [f"Sonoff {self.version}"] + ([f"backed up {self.taken}"] if self.taken else [])
        # The name eWeLink gave it, which tells nothing apart, is left out.
        named = f'"{self.name}", ' if self.name and self.name not in {k.name for k in kinds.ALL} else ""
        return f"{self.mac}  ({named}{', '.join(extra)})"


def find_backup(mac: str) -> Backup | None:
    directory = backup_dir(mac)
    if (directory / "flash.bin").is_file() and (directory / "efuse.bin").is_file():
        family = "espressif"
    elif (directory / BEKEN_BACKUP_BIN).is_file() and (directory / BEKEN_MANIFEST).is_file():
        family = "beken"
    else:
        return None
    record = read_device(mac)
    return Backup(mac=folder_name(mac).replace("-", ":"), directory=directory,
                  version=str(record.get("stock_version") or "unknown version"),
                  taken=str(record.get("backed_up") or ""), name=str(record.get("ewelink_name") or ""),
                  family=family)


def backups() -> list[Backup]:
    """Every device with a Sonoff backup, most recently backed up first."""
    if not DEVICES.is_dir():
        return []
    found = [find_backup(child.name) for child in DEVICES.iterdir()
             if child.is_dir() and MAC_RE.match(child.name)]
    return sorted((b for b in found if b), key=lambda b: b.taken, reverse=True)


README = """\
The Sonoff firmware of the {kind} with MAC address {mac}, as the tool
read it on {date}, before anything replaced it. It was running Sonoff
{version}.

  flash.bin    the device's whole flash chip: bootloader, partition table, the
               Sonoff firmware, its settings, its eWeLink device id and keys
  efuse.bin    the chip's eFuse registers; only the emulator needs them
  report.json  what the backup holds, as checked when it was taken

This is the original: nothing rewrites it, and the files are read-only. Keep
it private. flash.bin holds your Wi-Fi password and the device's eWeLink keys in
plain text. It belongs to this device only - the MAC above is on no other.

Going back to the Sonoff firmware
  The tool writes this backup back over the air when asked to return the
  device to Sonoff (docs/technical-details.md). Without the tool, write
  flash.bin back over USB-serial. That means opening the device and wiring a
  3.3 V USB-serial adapter to its chip - never while the device is connected
  to the mains. Then, with the chip held in its download mode:

      esptool.py write_flash 0x0 flash.bin

  It boots the Sonoff {version} it had, with its settings and eWeLink identity.
  One of its two app slots holds the conversion's small installer instead of
  an older Sonoff version; the device boots the other.

Running it in the emulator
  tools/espressif/emulator/emu.sh offers every backed-up device by its MAC address and
  runs a copy; this backup is never changed (docs/emulator.md).
"""


def _write_read_only(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    with os.fdopen(fd, "wb") as handle:
        handle.write(data)


def save_backup(flash: bytes, efuse: bytes, report: dict[str, Any],
                version: str | None, date: str | None = None, *,
                kind: str = "device") -> tuple[Path, bool]:
    """Keep a device's Sonoff firmware as its original backup; `kind`, what the
    device is called in the README beside it (its kinds.Kind's name).

    Returns where it is, and whether it was saved now: a device that has one
    already keeps it, whatever was read this time.  The MAC comes from the
    chip's own eFuses, so it is filed under the device it came from.
    """
    mac = mac_from_efuse(efuse)
    directory = backup_dir(mac)
    if (directory / "flash.bin").exists():
        return directory, False
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{BACKUP}.", dir=directory.parent))
    _write_read_only(staging / "flash.bin", flash)
    _write_read_only(staging / "efuse.bin", efuse)
    _write_read_only(staging / "report.json", (json.dumps(report, indent=2) + "\n").encode())
    date = date or time.strftime("%Y-%m-%d")
    readme = README.format(kind=kind, mac=mac, date=date, version=version or "(unknown version)")
    _write_read_only(staging / "README.txt", readme.encode())
    # Whole or not at all: a backup is only ever seen complete.
    staging.rename(directory)
    remember_device(mac, stock_version=version, backed_up=date)
    return directory, True


BEKEN_README = """\
The {kind} with MAC address {mac}: its own data, read out of it through
OpenBeken on {date}, just after Sonoff {version} was replaced; and
Sonoff's firmware, as the image that puts it back.

  config-0x3e0000.bin  the last 128 KiB of the device's flash, as read: the
                       factory record (MAC, radio calibration), the store
                       Sonoff's firmware keeps its Wi-Fi, meter calibration
                       and eWeLink identity in, and the system store. None of
                       it is changed by a conversion: the device keeps its own.
  stock-app-*.rbl      Sonoff's firmware, the same for every device of this kind,
                       made from the public dump of a stock device: what the
                       device's bootloader installs to go back
  backup.json          what this is: offsets, hashes, when

This is the original: nothing rewrites it, and the files are read-only. Keep
it private. config-0x3e0000.bin holds your Wi-Fi password and the device's
eWeLink keys. It belongs to this device only - the MAC above is on no other.

Going back to the Sonoff firmware
  Run the conversion tool and choose Sonoff's own firmware: the device fetches
  stock-app-*.rbl and its bootloader installs it, and it reads its own data
  from where it still is on the device. config-0x3e0000.bin is for the day that
  data is lost, written back at 0x3e0000 over a serial connection.
"""


def save_beken_backup(mac: str, region: bytes, stock: bytes | None, stock_version: str,
                      *, kind: str = "device", date: str | None = None,
                      source: str = "OpenBeken GET /api/flash after conversion") -> tuple[Path, bool]:
    """Keep a Beken device's own data as its backup, Sonoff's firmware beside it
    where there is one; `kind`, what the device is called in the README;
    `source`, how it was read out.

    Returns where it is, and whether it was saved now: a device that has one
    already keeps it, whatever was read this time.
    """
    directory = backup_dir(mac)
    if find_backup(mac) is not None:
        return directory, False
    directory.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=f".{BACKUP}.", dir=directory.parent))
    start, _size = BEKEN_REGION
    manifest: dict[str, Any] = {
        "device": kind, "mac": mac, "taken": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": source,
        "regions": {BEKEN_BACKUP_BIN: {"flash_offset": f"0x{start:x}", "length": len(region),
                                       "sha256": hashlib.sha256(region).hexdigest()}},
    }
    _write_read_only(staging / BEKEN_BACKUP_BIN, region)
    if stock is not None:
        name = f"stock-app-{stock_version or 'unknown'}.rbl"
        _write_read_only(staging / name, stock)
        manifest["regions"][name] = {"sha256": hashlib.sha256(stock).hexdigest(),
                                     "contents": "Sonoff's firmware as the rt_ota image the bootloader applies"}
    _write_read_only(staging / BEKEN_MANIFEST, (json.dumps(manifest, indent=2) + "\n").encode())
    date = date or time.strftime("%Y-%m-%d")
    readme = BEKEN_README.format(kind=kind, mac=mac, date=date, version=stock_version or "(unknown version)")
    _write_read_only(staging / "README.txt", readme.encode())
    staging.rename(directory)  # whole or not at all: a backup is only ever seen complete
    remember_device(mac, stock_version=stock_version or None, backed_up=date)
    return directory, True
