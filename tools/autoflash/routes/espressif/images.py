"""The ESPHome images an Espressif device is given, as firmware.json pins them:
this project's own ESPHome for each kind, its bootloader, the mover, and
the partition tables a device leaves Tasmota for.

The run installs a pre-built ESPHome (artifacts/esphome-sonoff-s60-imprintable-*.bin,
from devices/sonoff-s60/esphome.yaml) rather than asking anyone to build one: building it
takes gigabytes of tools and minutes of compiling. Like the recovery Safeboot,
it holds placeholders instead of a Wi-Fi name and password, and the device's own
Wi-Fi is written into them in memory (imprint.py) - so the image on disk never
holds it, and the device joins the home network by itself when it starts.
A Beken device's images are routes/beken/images.py's; the device once it runs
ESPHome, whatever its family, is tools/autoflash/esphome.py's.
"""

from __future__ import annotations

import hashlib
import json
import struct
from pathlib import Path
from typing import TYPE_CHECKING

from tools.autoflash import imprint, kinds, output
from tools.autoflash.runs import ROOT
from tools.espressif import chips, layouts

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# What each is, as firmware.json pins it: tools/repo/update_firmware.py writes it,
# docs/updating-firmware.md says when.
FIRMWARE = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))
# By chip (what its devices share) and by kind (each one's own), as Beken's are.
PINS = FIRMWARE["espressif"]
# The published image, the ESPHome made for the Sonoff S60 (kinds.SONOFF_S60),
# pinned: a changed file is refused, never used unseen.
IMAGE = ROOT / PINS[kinds.SONOFF_S60.key]["esphome"]["file"]
IMAGE_SHA256 = PINS[kinds.SONOFF_S60.key]["esphome"]["sha256"]
IMAGE_SIZE = PINS[kinds.SONOFF_S60.key]["esphome"]["size"]
# ESPHome's bootloader, from the same build, put in place of Sonoff's: Sonoff's
# (ESP-IDF 4.4) starts ESPHome but cannot roll back an update that fails to
# start ("Bootloader too old for OTA rollback").  It is all that sits before
# the partition table at 0x8000, and the one part the device cannot start without.
# It is replaced only when that is the point: a power cut in the second it
# takes to write leaves a device that has to be opened.
BOOTLOADER = chips.ESP32C3.file("bootloader")
BOOTLOADER_SHA256 = chips.ESP32C3.pin("bootloader")["sha256"]
BOOTLOADER_SIZE = chips.ESP32C3.pin("bootloader")["size"]
BOOTLOADER_ROOM = 0x8000
# Sonoff's app slots, which the run leaves as they are: ESPHome goes into ota_0.
SLOT_ADDRESS = 0x20000
SLOT_SIZE = 0x1F0000
# What makes a file an app rather than, say, a factory image (which starts with
# the bootloader): the app description ESP-IDF puts first in every app.
APP_DESC_MAGIC = 0xABCD5432
# Testing only (run --simulate-bad-image STEP[:N]), as a Beken device's
# (routes/beken/images.py): the step whose image is sent spoilt, the first N
# times a run sends it (once, without N) - one byte in its middle changed, so
# the device takes it whole but refuses it, its own check of the image failing.
# Only D1 asks: the Berry Tasmota, which the installer checks before switching to it.
SPOIL_STEP: str | None = None
SPOIL_TIMES = 1


class ImageError(ValueError):
    pass


def as_sent(ctx: Context, step: str, image: bytes) -> bytes:
    """`image` as `step` sends it: spoilt, where a test asked for that step's
    (SPOIL_STEP) and this run has sent it spoilt fewer than SPOIL_TIMES times."""
    spoilt_so_far = int(ctx.state.data.get("captures", {}).get(f"{step}.spoilt") or 0)
    if step != SPOIL_STEP or spoilt_so_far >= SPOIL_TIMES:
        return image
    ctx.state.capture(f"{step}.spoilt", spoilt_so_far + 1)
    spoilt = bytearray(image)
    spoilt[len(image) // 2] ^= 0xFF
    output.say(f"    The image is spoilt, as asked (--simulate-bad-image {step}): the device will refuse it.")
    return bytes(spoilt)


def check_app(image: bytes, label: str, chip: chips.Chip = chips.ESP32C3) -> None:
    """Refuse anything but an app for the device's chip that fits Sonoff's slot."""
    if len(image) > SLOT_SIZE:
        raise ImageError(f"{label} is {len(image):,} bytes; the device's app slot holds {SLOT_SIZE:,}")
    if len(image) < 64 or image[0] != 0xE9:
        raise ImageError(f"{label} is not an ESP32 firmware image")
    if struct.unpack_from("<H", image, 12)[0] != chip.chip_id:
        raise ImageError(f"{label} is not built for the device's {chip.label}")
    if struct.unpack_from("<I", image, 32)[0] != APP_DESC_MAGIC:
        raise ImageError(
            f"{label} is not an app image: use firmware.ota.bin, not firmware.factory.bin"
        )
    try:
        report = imprint.parse_esp_image(image)
    except ValueError as exc:
        raise ImageError(f"{label} is not a valid firmware image: {exc}") from exc
    if not report["checksum_valid"] or report["sha256_valid"] is False:
        raise ImageError(f"{label} fails its own integrity check")


def image_pin(kind: kinds.Kind) -> dict:
    """What firmware.json pins for `kind`'s ESPHome."""
    try:
        return PINS[kind.key]["esphome"]
    except KeyError:
        raise ImageError(f"no ESPHome is published for the {kind.name}") from None


def image_file(kind: kinds.Kind) -> Path:
    return ROOT / image_pin(kind)["file"]


def chip_of(kind: kinds.Kind) -> chips.Chip:
    return chips.of(kind.chip)


def published_image(kind: kinds.Kind = kinds.SONOFF_S60) -> bytes:
    """The pinned image, its placeholders untouched."""
    pin = image_pin(kind)
    path = IMAGE if kind is kinds.SONOFF_S60 else ROOT / pin["file"]
    image = path.read_bytes()
    size, digest = (IMAGE_SIZE, IMAGE_SHA256) if kind is kinds.SONOFF_S60 else (pin["size"], pin["sha256"])
    if len(image) != size or hashlib.sha256(image).hexdigest() != digest:
        raise ImageError(f"{path.name} differs from the pinned file")
    check_app(image, path.name, chip_of(kind))
    if not imprint.has_placeholders(image):
        raise ImageError(f"{path.name} has no untouched Wi-Fi placeholders")
    return image


def bootloader_file(chip: chips.Chip = chips.ESP32C3) -> tuple[Path, int, str]:
    """The pinned bootloader for `chip`: its file, size and hash."""
    if chip is chips.ESP32C3:
        return BOOTLOADER, BOOTLOADER_SIZE, BOOTLOADER_SHA256
    pin = chip.pin("bootloader")
    return ROOT / pin["file"], pin["size"], pin["sha256"]


def published_bootloader(chip: chips.Chip = chips.ESP32C3) -> bytes:
    """The pinned bootloader, checked to be one for this device."""
    path, size, digest = bootloader_file(chip)
    bootloader = path.read_bytes()
    if len(bootloader) != size or hashlib.sha256(bootloader).hexdigest() != digest:
        raise ImageError(f"{path.name} differs from the pinned file")
    if len(bootloader) > BOOTLOADER_ROOM - chip.bootloader_at or bootloader[0] != 0xE9:
        raise ImageError(f"{path.name} is not an ESP32 bootloader that fits before 0x8000")
    if struct.unpack_from("<H", bootloader, 12)[0] != chip.chip_id:
        raise ImageError(f"{path.name} is not built for the device's {chip.label}")
    report = imprint.parse_esp_image(bootloader)
    if not report["checksum_valid"] or report["sha256_valid"] is not True:
        raise ImageError(f"{path.name} fails its own integrity check")
    return bootloader


# The mover (devices/esp32c3/mover.yaml): a small ESPHome, run only while a
# device leaves Tasmota, that takes the next firmware as an ESPHome update.
MOVER = chips.ESP32C3.file("mover")
MOVER_SHA256 = chips.ESP32C3.pin("mover")["sha256"]
MOVER_SIZE = chips.ESP32C3.pin("mover")["size"]
# Sonoff's partition table, as a stock S60 has it: written back when a device
# leaves Tasmota's layout.
SONOFF_TABLE = ROOT / "artifacts" / "sonoff-s60-partition-table.bin"


def mover_for(ssid: str, password: str, chip: chips.Chip = chips.ESP32C3) -> bytes:
    """The pinned mover with this Wi-Fi written in."""
    if chip is chips.ESP32C3:
        path, size, digest = MOVER, MOVER_SIZE, MOVER_SHA256
    else:
        pin = chip.pin("mover")
        path, size, digest = ROOT / pin["file"], pin["size"], pin["sha256"]
    image = path.read_bytes()
    if len(image) != size or hashlib.sha256(image).hexdigest() != digest:
        raise ImageError(f"{path.name} differs from the pinned file")
    check_app(image, path.name, chip)
    return imprint.imprint(image, ssid, password)


def interim_of(kind: kinds.Kind) -> Path:
    """The bundle's Tasmota for a device of `kind` to run part way through."""
    chip = chip_of(kind)
    return chip.bundle_file(chip.interim_name)


def layout_of(kind: kinds.Kind) -> layouts.Layout:
    return layouts.of(kind.layout)


def sonoff_table(kind: kinds.Kind = kinds.SONOFF_S60) -> bytes:
    layout = layout_of(kind)
    table = layout.table
    if hashlib.sha256(table).hexdigest() != layout.table_sha256:
        raise ImageError(f"{layout.table_file} is not Sonoff's partition table")
    return table


def leaving_table(kind: kinds.Kind = kinds.SONOFF_S60) -> bytes:
    """The table a device of `kind` leaves Tasmota for: Sonoff's, or a temporary one."""
    layout = layout_of(kind)
    table = layout.temporary_table() if layout.needs_temporary else sonoff_table(kind)
    if hashlib.sha256(table).hexdigest() != layout.leaving_sha256:
        raise ImageError(f"the table a {kind.name} leaves Tasmota for is not the reviewed one")
    return table


def image_for(ssid: str, password: str, own: Path | None = None, kind: kinds.Kind = kinds.SONOFF_S60) -> bytes:
    """The image to install: the published one with this Wi-Fi written in, or
    one built by hand (--esphome PATH), imprinted too if it has placeholders."""
    if own is None:
        return imprint.imprint(published_image(kind), ssid, password)
    image = own.read_bytes()
    check_app(image, own.name, chip_of(kind))
    if imprint.has_placeholders(image):
        return imprint.imprint(image, ssid, password)
    return image  # built with its own Wi-Fi
