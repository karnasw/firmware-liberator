"""Supla GUI-Generic instead of the final Tasmota: the image, and the memory
layout it needs.

GUI-Generic is built for each user at https://gui-generic-builder-new.supla.io/,
with their own choice of features and the device's template, so the tool
publishes none: it takes the file the builder gave (the app .bin, or the .zip
it came in) and checks it before anything is written.

The builder's own partition table puts its app at 0x10000, over where Sonoff's
layout keeps the plug's settings and boot selection, and is meant for a plug
flashed over USB.  Over the air, the app goes into Sonoff's ota_0 instead - an
app runs from wherever its partition is - and the table written is Sonoff's
with room taken from the end of ota_1 for what GUI-Generic needs and Sonoff's
layout lacks: the LittleFS partition its settings are kept in (labelled
"spiffs", as Arduino looks for it) and a core dump partition.
"""

from __future__ import annotations

import hashlib
import re
import struct
import zipfile
from pathlib import Path

from . import esphome, imprint, kinds

# Sonoff's app slot ota_0, which GUI-Generic goes into, and its ota_1, made
# smaller for the partitions after it.  An update GUI-Generic later takes goes
# into the slot it does not run from, so an image has to fit the smaller one.
OTA0_ADDRESS = 0x20000
OTA0_SIZE = 0x1F0000
OTA1_ADDRESS = 0x210000
OTA1_SIZE = 0x1C0000
# Where the builder's own table has them, at the end of the flash.
LITTLEFS_ADDRESS = 0x3D0000
LITTLEFS_SIZE = 0x20000
COREDUMP_ADDRESS = 0x3F0000
COREDUMP_SIZE = 0x10000

TABLE_SIZE = 0x1000
_ENTRY = struct.Struct("<2sBBII16sI")
_MAGIC = b"\xaa\x50"
_MD5_MAGIC = b"\xeb\xeb"
APP, DATA = 0x00, 0x01

# The partitions of the layout GUI-Generic is given: (label, type, subtype,
# address, size, flags).  The first five are Sonoff's, as they are.
LAYOUT = (
    ("nvs", DATA, 0x02, 0x9000, 0x10000, 0),
    ("reserve", DATA, 0x02, 0x19000, 0x4000, 0),
    ("otadata", DATA, 0x00, 0x1D000, 0x2000, 0),
    ("phy_init", DATA, 0x01, 0x1F000, 0x1000, 0),
    ("ota_0", APP, 0x10, OTA0_ADDRESS, OTA0_SIZE, 0),
    ("ota_1", APP, 0x11, OTA1_ADDRESS, OTA1_SIZE, 0),
    ("spiffs", DATA, 0x82, LITTLEFS_ADDRESS, LITTLEFS_SIZE, 0),
    ("coredump", DATA, 0x03, COREDUMP_ADDRESS, COREDUMP_SIZE, 0),
)

# What tells GUI-Generic from any other Arduino app: its name, and Supla's.
MARKERS = (b"GUI-Generic", b"SUPLA")
# Nothing the builder gives is near this; a bigger member of a zip is not read.
LARGEST_FILE = 0x400000
# The Tasmota-style template a build for a device carries, as the builder
# writes it: {"NAME":"S60","GPIO":[...],...}.
_TEMPLATE_GPIO = re.compile(rb'\{"NAME":"[^"]{0,32}","GPIO":\[([0-9,]{1,200})\]')
_BUILDER_VERSION = re.compile(r"\.v(NX\d+(?:\.\d+)*)")


class ImageError(ValueError):
    pass


# ----------------------------------------------------------------- the layout


def encode_table(partitions) -> bytes:
    """A partition table as ESP-IDF's gen_esp32part.py writes it: an entry
    for each partition, the MD5 of them all, then erased flash to a sector."""
    entries = b"".join(
        _ENTRY.pack(_MAGIC, kind, subtype, address, size, label.encode("ascii").ljust(16, b"\0"), flags)
        for label, kind, subtype, address, size, flags in partitions)
    md5 = _MD5_MAGIC + b"\xff" * 14 + hashlib.md5(entries).digest()
    table = entries + md5
    return table + b"\xff" * (TABLE_SIZE - len(table))


def decode_table(table: bytes) -> list[tuple[str, int, int, int, int, int]]:
    """The partitions of a table, its MD5 checked."""
    partitions, offset = [], 0
    while table[offset:offset + 2] == _MAGIC:
        _, kind, subtype, address, size, label, flags = _ENTRY.unpack_from(table, offset)
        partitions.append((label.split(b"\0")[0].decode("ascii"), kind, subtype, address, size, flags))
        offset += _ENTRY.size
    if table[offset:offset + 2] != _MD5_MAGIC or table[offset + 16:offset + 32] != hashlib.md5(table[:offset]).digest():
        raise ImageError("the partition table's MD5 is missing or wrong")
    return partitions


def table() -> bytes:
    """The partition table GUI-Generic runs on (LAYOUT)."""
    return encode_table(LAYOUT)


# ------------------------------------------------------------------ the image


def builder_version(name: str) -> str | None:
    """The release the builder names a file for: NX26.11.08.2 from
    GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin."""
    found = _BUILDER_VERSION.search(name)
    return found.group(1) if found else None


def _from_zip(path: Path) -> tuple[bytes, str]:
    """The app in a zip the builder gave: the one .bin that is an app, its
    bootloader, partition table and boot selection left out.  Read in
    memory, never unpacked."""
    try:
        with zipfile.ZipFile(path) as archive:
            found = []
            for member in archive.infolist():
                if member.is_dir() or not member.filename.lower().endswith(".bin"):
                    continue
                if member.file_size > LARGEST_FILE:
                    raise ImageError(f"{member.filename} in {path.name} is larger than any firmware for the device")
                data = archive.read(member)
                if len(data) >= 64 and data[0] == 0xE9 and struct.unpack_from("<I", data, 32)[0] == esphome.APP_DESC_MAGIC:
                    found.append((data, Path(member.filename).name))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImageError(f"{path.name} cannot be read as a zip: {exc}") from exc
    if len(found) != 1:
        raise ImageError(f"{path.name} holds {len(found)} firmware apps, not one: give the app's .bin itself")
    return found[0]


def load(path: Path) -> tuple[bytes, str]:
    """The GUI-Generic app in `path` (the .bin, or the builder's .zip), and
    the name of its file."""
    if not path.is_file():
        raise ImageError(f"{path} is not a file")
    if path.stat().st_size > LARGEST_FILE * 2:
        raise ImageError(f"{path.name} is larger than any firmware for the device")
    if zipfile.is_zipfile(path):
        return _from_zip(path)
    return path.read_bytes(), path.name


def check(image: bytes, label: str) -> None:
    """Refuse anything but a GUI-Generic app for the ESP32-C3 that fits both
    of its slots, and passes its own integrity check."""
    if len(image) > OTA1_SIZE:
        raise ImageError(f"{label} is {len(image):,} bytes; Supla's layout on the device holds "
                         f"{OTA1_SIZE:,} in each of its two slots")
    try:
        esphome.check_app(image, label)
    except esphome.ImageError as exc:
        raise ImageError(str(exc).replace("firmware.ota.bin, not firmware.factory.bin",
                                          "the app's .bin, not the bootloader or partitions.bin")) from exc
    if not all(marker in image for marker in MARKERS):
        raise ImageError(f"{label} is not Supla's GUI-Generic firmware")


def template_gpio(image: bytes) -> tuple[int, ...] | None:
    """The GPIO of the device template a build carries, if it has one."""
    found = _TEMPLATE_GPIO.search(image)
    return tuple(int(pin) for pin in found.group(1).split(b",") if pin) if found else None


def template_matches(image: bytes, kind: kinds.Kind) -> bool | None:
    """Whether a build's template is the device's wiring (as its Tasmota
    template has it); None when it carries no template to compare."""
    gpio = template_gpio(image)
    return None if gpio is None else gpio == kind.tasmota_gpio


def describe(image: bytes) -> dict[str, object]:
    """What the evidence records of an image: its size and hash, never bytes."""
    report = imprint.parse_esp_image(image)
    return {"size": len(image), "sha256": hashlib.sha256(image).hexdigest(),
            "app_version": image[48:80].split(b"\0")[0].decode("ascii", "replace"),
            "esp_idf": image[144:176].split(b"\0")[0].decode("ascii", "replace"),
            "parsed_size": report["parsed_image_size"]}
