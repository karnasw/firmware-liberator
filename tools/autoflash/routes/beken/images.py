"""The firmware a Beken device is given: the OpenBeken and ESPHome images
published in artifacts/ and pinned in firmware.json under "beken", by device,
and Sonoff's own firmware for a device going back, made from the public dump
of its stock flash when it is needed - never published here (AGENTS.md).

Each is an rt_ota image the device's stock bootloader applies from its download
slot (tools/beken/rbl.py); the stock firmware's updater takes one wrapped in a
FOTA container (tools/beken/fota_wrap.py), OpenBeken fetches one by URL, and
an ESPHome takes one inside a UF2 (tools/beken/uf2.py).
"""

from __future__ import annotations

import hashlib
import json
import os
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from tools.autoflash import account, kinds, output, records
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.routes import public_copies
from tools.autoflash.runs import ROOT
from tools.beken import fota_wrap, layouts
from tools.beken import rbl
from tools.ewelink import offered_firmware
from tools.ewelink.get_device_key import EwelinkError
from tools.ewelink.query_ota import latest_offer

if TYPE_CHECKING:
    from tools.autoflash.context import Context

PINS = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8")).get("beken", {})
# The version the FOTA container says it is (the S60's bridge is sent the same way).
FOTA_VERSION = fota_wrap.ANNOUNCED_VERSION
# The firmware a run to these installs from Sonoff's first, to back the device
# up through (only OpenBeken reads it out), then has fetch the one chosen.
# The person, who did not choose it, is told of the interim firmware alone.
INTERIM = {"esphome": "openbeken"}
INTERIM_NAME = "the interim firmware"
# Testing only (run --simulate-bad-image [STEP[:N]]): the step whose image is
# sent spoilt, the first N times a run sends it (once, without N) - one byte of
# the RBL's body changed, so the device fetches it whole and an update's own CRC
# (made after) passes, but its bootloader, checking the body's CRC in the RBL's
# header, refuses to install it.
SPOIL_STEP: str | None = None
SPOIL_TIMES = 1
# Testing only (run --simulate-unknown-table): the official file's table read
# as if its NET info were at another place, as a device on another table's would be.
UNKNOWN_TABLE = False
# Each kind's stock flash, as another project publishes it: where, and what it
# is, by hash.  Fetched once into the directory the emulator keeps dumps in.
def as_sent(ctx: Context, step: str, image: bytes) -> bytes:
    """`image` as `step` sends it: spoilt, where a test asked for that step's
    (SPOIL_STEP) and this run has sent it spoilt fewer than SPOIL_TIMES times -
    so a test may ask on every run, as a route reaches the step only after a
    restart asked for."""
    spoilt_so_far = int(ctx.state.data.get("captures", {}).get(f"{step}.spoilt") or 0)
    if step != SPOIL_STEP or spoilt_so_far >= SPOIL_TIMES:
        return image
    ctx.state.capture(f"{step}.spoilt", spoilt_so_far + 1)
    spoilt = bytearray(image)
    spoilt[rbl.HEADER + (len(image) - rbl.HEADER) // 2] ^= 0xFF
    output.say(f"    The image is spoilt, as asked (--simulate-bad-image {step}): its bootloader will refuse it.")
    return bytes(spoilt)


OFFERED = "the file eWeLink offers the device"
PUBLIC_COPY = public_copies.SOURCE
BACKUP = "the device's own backup"


@dataclass(frozen=True)
class Official:
    """A kind's official update file, its partition table known."""
    image: bytes                 # the rt_ota image in it, as its bootloader applies it
    name: str                    # the firmware name its wrapper gives
    version: str
    layout: layouts.Layout
    table: tuple[layouts.Partition, ...]
    source: str                  # where it came from: OFFERED or PUBLIC_COPY


def offered_file(ctx: Context) -> bytes | None:
    """The update file eWeLink offers the run's device, as the device fetches
    it, asked with its own id as query_ota.latest_offer asks; fetched once a
    run, signing in to eWeLink first where the run has not yet. None where it
    offers none, eWeLink is not reached, or the device is not found in the
    account. Neither the link nor the id is logged."""
    if not hasattr(ctx, "offered_sonoff_file"):
        ctx.offered_sonoff_file = _offered_file(ctx)
    return ctx.offered_sonoff_file


def _offered_file(ctx: Context) -> bytes | None:
    deviceid = account.device_id(ctx)
    if not deviceid:
        output.log_only(f"    Sonoff's firmware: {OFFERED}: the device is not found in the eWeLink account")
        return None
    query = {"deviceid": deviceid, "model": kinds.of_run(ctx.state).stock_model}
    try:
        asked = ctx.ewelink.use(lambda region, session: latest_offer(region, session["at"], query))
    except EwelinkError:  # eWeLink not reached
        return None
    # Its user1.bin alone: a user2.bin is the same firmware for the other slot.
    offers = [{**offer, "binList": sorted(offer.get("binList") or [],
                                          key=lambda item: item.get("name") != "user1.bin")[:1]}
              for offer in asked.get("offers") or []]
    files, problems = offered_firmware.fetch(offers, deviceid)
    for problem in problems:
        output.log_only(f"    Sonoff's firmware: {OFFERED}: {problem}")
    return files[0].data if files else None


def checked(kind: kinds.Kind, data: bytes, source: str) -> Official:
    """An official update file for `kind`, refused unless it holds the kind's
    firmware alone, whole, on the partition table this kind is known to be on:
    everything a conversion writes or reads goes where that table says."""
    try:
        ((name, version, image),) = fota_wrap.unwrap(data)
        app = rbl.unpack(image)
        if UNKNOWN_TABLE:
            app = app.replace(struct.pack("<I", 0x3E1000), struct.pack("<I", 0x1E1000))
        layout, table = layouts.known(app)
    except (ValueError, rbl.RblError) as exc:
        raise NothingWritten(f"the official update file for the {kind.name} was refused: {exc}. Its device may not "
                             "be laid out as this tool expects, so nothing was written") from exc
    if name != kind.wrapper_model:
        raise NothingWritten(f"the official update file for the {kind.name} is for {name}, not "
                             f"{kind.wrapper_model}. Nothing was written")
    if layout.key != kind.layout:
        raise NothingWritten(f"the official update file for the {kind.name} has the {layout.key} partition table, "
                             f"not the {kind.layout} one expected. Nothing was written")
    return Official(image, name, version, layout, table, source)


def from_ewelink(ctx: Context | None, kind: kinds.Kind) -> Official | None:
    """The file eWeLink offers the device, if it passes checked(); why not, logged."""
    data = offered_file(ctx) if ctx is not None else None
    if data is None:
        return None
    try:
        return checked(kind, data, OFFERED)
    except NothingWritten as exc:
        output.log_only(f"    Sonoff's firmware: {OFFERED}: {exc}")
        return None


def against_stub() -> bool:
    """Whether the run is against a stand-in for eWeLink (the emulator's),
    which is all it reaches, as on the Espressif route (sonoff_firmware)."""
    return bool(os.environ.get("AUTOFLASH_EWELINK_API_BASE"))


def official(kind: kinds.Kind, ctx: Context | None = None) -> Official:
    """A kind's official update file: the one eWeLink offers the run's device,
    as on the Espressif route, else the public copy this tool pins by hash;
    either refused as checked() says."""
    found = from_ewelink(ctx, kind)
    if found is not None:
        return found
    if against_stub():
        raise NothingWritten(f"Sonoff's firmware for the {kind.name} could not be fetched from eWeLink. "
                             "Nothing was written")
    try:
        return checked(kind, public_copies.fetch(kind), PUBLIC_COPY)
    except public_copies.Unavailable as exc:
        raise NothingWritten(f"{exc}. Nothing was written") from exc


def first_firmware(firmware: str) -> str:
    """The firmware a run to `firmware` sends from Sonoff's: the interim one, if it has one."""
    return INTERIM.get(firmware, firmware)


def pin(kind: kinds.Kind, firmware: str) -> dict:
    try:
        return PINS[kind.key][firmware]
    except KeyError:
        raise Abort(f"no {firmware} image is published for the {kind.name} (artifacts/firmware.json)") from None


def image(ctx: Context, firmware: str) -> tuple[bytes, str]:
    """The published image of `firmware` for the run's kind of device, checked
    against its pin - or, for ESPHome, one built by hand (--esphome), checked
    as an rt_ota app.  The image and its version."""
    kind = kinds.of_run(ctx.state)
    own = getattr(ctx, "esphome_firmware", None) if firmware == "esphome" else None
    if own:
        data = Path(own).read_bytes()
        try:
            header = rbl.header(data)
            rbl.unpack(data)
        except rbl.RblError as exc:
            raise Abort(f"{own} is not an rt_ota image for the device: {exc}") from exc
        if header.name != "app":
            raise Abort(f"{own} is an rt_ota {header.name!r} image, not an app")
        output.say(f"Using the ESPHome built by hand at {own} ({header.version}).")
        return data, header.version
    pinned = pin(kind, firmware)
    path = ROOT / pinned["file"]
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise Abort(f"the published {firmware} image is missing: {exc}") from exc
    if len(data) != pinned["size"] or hashlib.sha256(data).hexdigest() != pinned["sha256"]:
        raise Abort(f"{pinned['file']} is not the file firmware.json pins: refusing to send it")
    return data, str(pinned["version"])


def backup_copy(mac: str | None) -> tuple[bytes, str, layouts.Layout] | None:
    """Sonoff's firmware as the device's backup keeps it beside its data (B4,
    or a readout): the rt_ota image, its version and its partition table's
    layout - where the file is there, whole as the backup's manifest says,
    and an app on a table this tool knows."""
    backup = records.find_backup(mac) if mac else None
    if backup is None or backup.family != "beken":
        return None
    try:
        regions = json.loads((backup.directory / records.BEKEN_MANIFEST).read_text(encoding="utf-8"))["regions"]
    except (OSError, ValueError, KeyError):
        regions = {}
    for path in sorted(backup.directory.glob("stock-app-*.rbl")):
        data = path.read_bytes()
        if (regions.get(path.name) or {}).get("sha256") not in (None, hashlib.sha256(data).hexdigest()):
            continue
        try:
            layout, _table = layouts.known(rbl.unpack(data))
        except (ValueError, rbl.RblError, layouts.LayoutError):
            continue
        return data, path.stem.removeprefix("stock-app-"), layout
    return None


def stock_rbl(kind: kinds.Kind, ctx: Context | None = None) -> tuple[bytes, str]:
    """Sonoff's own firmware for a device of `kind`, as the rt_ota image its
    bootloader applies, from the first of these to have it, as the Espressif
    route goes back: the device's own backup (backup_copy), the file eWeLink
    offers the device, then the public copy this tool pins
    (public_copies) - the last two as checked() says.  The image and
    Sonoff's version of it."""
    kept = backup_copy(ctx.state.input("device_mac")) if ctx is not None else None
    if kept is not None and kept[2].key == kind.layout:
        output.log_only(f"    Sonoff's firmware {kept[1]}: from {BACKUP}")
        return kept[0], kept[1]
    offered = from_ewelink(ctx, kind)
    if offered is not None:
        output.log_only(f"    Sonoff's firmware {offered.version}: from {OFFERED}")
        return offered.image, offered.version
    if against_stub():
        raise Abort(f"Sonoff's firmware for the {kind.name} could not be fetched from eWeLink")
    try:
        found = official(kind)
    except NothingWritten as exc:
        raise Abort(str(exc)) from exc
    output.log_only(f"    Sonoff's firmware {found.version}: from {PUBLIC_COPY}")
    return found.image, found.version
