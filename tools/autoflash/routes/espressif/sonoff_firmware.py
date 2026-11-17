"""Sonoff's own firmware for a device with no backup on this computer: the
official file eWeLink offers the device, else the same file as the
FlashDumps project keeps it in public, pinned (public_copies); and the flash a backup would have
held, made from it, the kind's layout and the device's identity.

eWeLink is asked as query_ota.latest_offer asks, with the device's own id and
a version older than any, so it offers the latest firmware; the file is fetched
as the device fetches it (firmware_name.read_head). Neither its link nor the
device's id is logged or kept. Whichever it comes from, the app in it is
checked as an ESP image for the device's chip, whole, and Sonoff's.
"""

from __future__ import annotations

import os
import struct
import urllib.parse
import urllib.request
import zlib
from typing import Any, TYPE_CHECKING

from tools.autoflash import hostsys, kinds, output, plans
from tools.autoflash.routes import public_copies
from tools.espressif import chips
from tools.espressif.analyze_vendor_ota import WRAPPER_SIZE, parse_esp_image
from tools.espressif.sonoff_identity import Identity
from tools.ewelink import firmware_name
from tools.ewelink.get_device_key import EwelinkError
from tools.ewelink.query_ota import latest_offer

if TYPE_CHECKING:
    from tools.autoflash.context import Context
    from tools.espressif.layouts import Layout

TIMEOUT = 60.0
FLASH_SIZE = 0x400000
SONOFF_PROJECT = b"esp32_fw"   # the project name in Sonoff's app description (plans.stock_slot)


class FirmwareError(ValueError):
    pass


def app_in_file(data: bytes, name: str) -> bytes:
    """The app a Sonoff update file holds for the firmware called `name`,
    its checksum checked (build_vendor_ota writes the same wrapper)."""
    if len(data) < WRAPPER_SIZE:
        raise FirmwareError("the file is too short to be a Sonoff update")
    for index in range(data[1]):
        record = firmware_name.RECORDS_AT + index * firmware_name.RECORD_SIZE
        if data[record:record + 32].split(b"\0", 1)[0].decode("ascii", "replace") != name:
            continue
        offset, length, checksum = struct.unpack_from(">III", data, record + 0x30)
        app = data[offset:offset + length]
        if len(app) != length or zlib.crc32(app) & 0xFFFFFFFF != checksum:
            raise FirmwareError("the firmware in the file is not whole")
        return app
    raise FirmwareError(f"the file holds no firmware called {name}")


def checked_app(app: bytes, kind: kinds.Kind, slot_size: int) -> bytes:
    """`app`, if it is a whole ESP image of Sonoff's firmware for the device's chip."""
    try:
        image = parse_esp_image(app)
    except (ValueError, struct.error) as exc:
        raise FirmwareError(f"the firmware is not an ESP image: {exc}") from exc
    if image["chip_id"] != chips.of(kind.chip).chip_id:
        raise FirmwareError(f"the firmware is not for the {chips.of(kind.chip).label}")
    if not image["checksum_valid"] or image["sha256_valid"] is False:
        raise FirmwareError("the firmware's checksum or hash does not match it")
    if app[80:112].split(b"\0", 1)[0] != SONOFF_PROJECT or len(app) > slot_size:
        raise FirmwareError("the firmware is not Sonoff's, or does not fit the device's slot")
    return app


def fetch(url: str, query: dict[str, Any] | None = None) -> bytes:
    asked = urllib.parse.urlsplit(url)
    if asked.scheme not in ("http", "https"):
        raise FirmwareError("not a web address")
    if query:
        extra = urllib.parse.urlencode(query)
        url = asked._replace(query=f"{asked.query}&{extra}" if asked.query else extra).geturl()
    request = urllib.request.Request(url, headers={"User-Agent": "autoflash-research/0.1"})
    return hostsys.download(request, timeout=TIMEOUT, say=output.detail)


def offered(ctx: Context, identity: Identity) -> bytes | None:
    """The file eWeLink offers the device, by its own id; None where it offers none."""
    if not identity.deviceid:
        return None
    query = {"deviceid": identity.deviceid, "model": identity.model, "version": "0.0.1"}
    try:
        asked = ctx.ewelink.use(lambda region, session: latest_offer(region, session["at"], query))
    except EwelinkError:  # eWeLink not reached
        return None
    for offer in asked.get("offers") or []:
        files = sorted(offer.get("binList") or [], key=lambda item: item.get("name") != "user1.bin")
        for item in files[:1]:
            return fetch(str(item.get("downloadUrl", "")),
                         {"deviceid": identity.deviceid, "ts": 1, "sign": 0})
    return None


# Testing (cli.py --simulate-no-sonoff-firmware): Sonoff's firmware never to
# be had, as with eWeLink and the public copy both out of reach.
SIMULATE_NONE = False


def official_app(ctx: Context, kind: kinds.Kind, identity: Identity, layout: Layout) -> tuple[bytes, str]:
    """Sonoff's firmware for the device, and where it came from."""
    if SIMULATE_NONE:
        raise FirmwareError("Sonoff's firmware for this device could not be fetched (simulated)")
    sources = [("the file eWeLink offers the device", lambda: offered(ctx, identity))]
    # A run against a stand-in for eWeLink (the emulator's) reaches nothing else.
    if not os.environ.get("AUTOFLASH_EWELINK_API_BASE"):
        sources.append((public_copies.SOURCE, lambda: public_copies.fetch(kind)))
    # Each source passed over is logged as it is, as the Beken route's are
    # (images.from_ewelink): the next one may still give it.
    for where, get in sources:
        try:
            data = get()
            if data is not None:
                return checked_app(app_in_file(data, kind.wrapper_model), kind, layout.slot_size), where
            output.log_only(f"    Sonoff's firmware: {where}: there is none")
        except (FirmwareError, public_copies.Unavailable, OSError, ValueError) as exc:
            output.log_only(f"    Sonoff's firmware: {where}: {exc}")
    raise FirmwareError("Sonoff's firmware for this device could not be fetched - from eWeLink, or "
                        "from the public copy of it")


def flash_of(table: bytes, settings: bytes, app: bytes, layout: Layout) -> bytes:
    """What a backup of the device would hold of it: Sonoff's table, its settings
    and its firmware in the first slot, the rest erased - so that the plans that
    write a backup back write this the same way (plans.back_to_sonoff)."""
    if len(settings) > layout.otadata - layout.nvs:
        raise FirmwareError("the settings do not fit the device's layout")
    flash = bytearray(b"\xff" * FLASH_SIZE)
    flash[plans.TABLE_ADDRESS:plans.TABLE_ADDRESS + len(table)] = table
    flash[layout.nvs:layout.nvs + len(settings)] = settings
    flash[layout.ota[0]:layout.ota[0] + len(app)] = app
    return bytes(flash)
