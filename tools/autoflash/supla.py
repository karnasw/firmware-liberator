"""Supla GUI-Generic instead of the final Tasmota: the build, and the memory
layout it runs on.

GUI-Generic is built for each user at https://gui-generic-builder-new.supla.io/,
with their own choice of features and the device's template, so the tool
publishes none: it takes the .zip the builder gave and checks it before
anything is written.

The plug is given what the builder made for it: its bootloader, its
partition table, its boot selection and the app at 0x10000, as flashing over
USB would leave it.  Under ESPHome's bootloader, on Sonoff's layout, a real
S60 ran GUI-Generic but could not mount its LittleFS settings partition, so
every setting was lost at the next restart; with the builder's own bootloader
and layout the same build keeps them.
"""

from __future__ import annotations

import binascii
import hashlib
import re
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import esphome, imprint, kinds

# The builder's layout: two app slots, the second for the update GUI-Generic
# takes later, and at the end of the flash the LittleFS partition its
# settings are kept in (labelled "spiffs", as Arduino looks for it) and a
# core dump partition.
NVS_ADDRESS = 0x9000
NVS_SIZE = 0x5000
OTADATA_ADDRESS = 0xE000
OTADATA_SIZE = 0x2000
APP0_ADDRESS = 0x10000
APP1_ADDRESS = 0x1F0000
APP_SIZE = 0x1E0000
LITTLEFS_ADDRESS = 0x3D0000
LITTLEFS_SIZE = 0x20000
COREDUMP_ADDRESS = 0x3F0000
COREDUMP_SIZE = 0x10000
# The bootloader ends before the partition table.
BOOTLOADER_LIMIT = 0x8000

TABLE_SIZE = 0x1000
_ENTRY = struct.Struct("<2sBBII16sI")
_MAGIC = b"\xaa\x50"
_MD5_MAGIC = b"\xeb\xeb"
APP, DATA = 0x00, 0x01

# The partitions of the builder's table: (label, type, subtype, address,
# size, flags).  A build whose table differs is refused, not guessed at.
LAYOUT = (
    ("nvs", DATA, 0x02, NVS_ADDRESS, NVS_SIZE, 0),
    ("otadata", DATA, 0x00, OTADATA_ADDRESS, OTADATA_SIZE, 0),
    ("app0", APP, 0x10, APP0_ADDRESS, APP_SIZE, 0),
    ("app1", APP, 0x11, APP1_ADDRESS, APP_SIZE, 0),
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


def selected_slot(otadata: bytes) -> int | None:
    """The app slot a boot selection starts, as ESP-IDF's bootloader reads
    it: the valid entry with the highest sequence number; None for none."""
    found = []
    for offset in (0, OTADATA_SIZE // 2):
        sequence, _, _, crc = struct.unpack_from("<I20sII", otadata, offset)
        if sequence != 0xFFFFFFFF and crc == binascii.crc32(struct.pack("<I", sequence), 0xFFFFFFFF):
            found.append(sequence)
    return (max(found) - 1) % 2 if found else None


# ------------------------------------------------------------------ the build


@dataclass(frozen=True)
class Build:
    """What the builder's .zip holds: GUI-Generic and what starts it."""
    app: bytes
    name: str                         # the app's file name, which names the release
    bootloader: bytes
    table: bytes                      # a whole sector
    otadata: bytes

    def sha256(self) -> str:
        """One hash of all four, so a resumed run can tell any of them changed."""
        digest = hashlib.sha256()
        for part in (self.app, self.bootloader, self.table, self.otadata):
            digest.update(hashlib.sha256(part).digest())
        return digest.hexdigest()


# The files of the builder's .zip besides the app, by name.
_PARTS = {"bootloader.bin": "bootloader", "partitions.bin": "table", "boot_app0.bin": "otadata"}


def builder_version(name: str) -> str | None:
    """The release the builder names a file for: NX26.11.08.2 from
    GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin."""
    found = _BUILDER_VERSION.search(name)
    return found.group(1) if found else None


def load(path: Path) -> Build:
    """The build in the builder's .zip at `path`, read in memory, never
    unpacked: the one .bin that is an app, and its bootloader, partition
    table and boot selection."""
    if not path.is_file():
        raise ImageError(f"{path} is not a file")
    if not zipfile.is_zipfile(path):
        raise ImageError(f"{path.name} is not the .zip the builder gave: Supla is installed with the "
                         "bootloader and partition table in it, so give the .zip as it is")
    apps, parts = [], {}
    try:
        with zipfile.ZipFile(path) as archive:
            for member in archive.infolist():
                name = Path(member.filename).name
                if member.is_dir() or not name.lower().endswith(".bin"):
                    continue
                if member.file_size > LARGEST_FILE:
                    raise ImageError(f"{name} in {path.name} is larger than any firmware for the device")
                data = archive.read(member)
                if name.lower() in _PARTS:
                    parts[_PARTS[name.lower()]] = data
                elif len(data) >= 64 and data[0] == 0xE9 and struct.unpack_from("<I", data, 32)[0] == esphome.APP_DESC_MAGIC:
                    apps.append((data, name))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImageError(f"{path.name} cannot be read as a zip: {exc}") from exc
    if len(apps) != 1:
        raise ImageError(f"{path.name} holds {len(apps)} firmware apps, not one")
    missing = [name for name, part in _PARTS.items() if part not in parts]
    if missing:
        raise ImageError(f"{path.name} has no {' or '.join(missing)}: give the .zip as the builder gave it")
    table_part = parts["table"]
    return Build(app=apps[0][0], name=apps[0][1], bootloader=parts["bootloader"],
                 table=table_part + b"\xff" * max(0, TABLE_SIZE - len(table_part)), otadata=parts["otadata"])


def check_app(image: bytes, label: str) -> None:
    """Refuse anything but a GUI-Generic app for the ESP32-C3 that fits its
    slots, and passes its own integrity check."""
    if len(image) > APP_SIZE:
        raise ImageError(f"{label} is {len(image):,} bytes; Supla's layout on the device holds "
                         f"{APP_SIZE:,} in each of its two slots")
    try:
        esphome.check_app(image, label)
    except esphome.ImageError as exc:
        raise ImageError(str(exc).replace("firmware.ota.bin, not firmware.factory.bin",
                                          "the app's .bin, not the bootloader or partitions.bin")) from exc
    if not all(marker in image for marker in MARKERS):
        raise ImageError(f"{label} is not Supla's GUI-Generic firmware")


def check(build: Build) -> None:
    """Refuse a build but for the ESP32-C3 laid out as this tool knows: the
    app as check_app; a bootloader that passes its own integrity check and
    ends before the table; the builder's table (LAYOUT); and a boot selection
    that starts app0."""
    check_app(build.app, build.name)
    bootloader = build.bootloader
    if len(bootloader) < 64 or bootloader[0] != 0xE9:
        raise ImageError("bootloader.bin is not an ESP32 bootloader")
    if struct.unpack_from("<H", bootloader, 12)[0] != esphome.ESP32_C3_CHIP_ID:
        raise ImageError("bootloader.bin is not built for the device's ESP32-C3")
    if len(bootloader) > BOOTLOADER_LIMIT:
        raise ImageError("bootloader.bin is too big to fit before the partition table")
    try:
        report = imprint.parse_esp_image(bootloader)
    except ValueError as exc:
        raise ImageError(f"bootloader.bin is not a valid bootloader: {exc}") from exc
    if not report["checksum_valid"] or report["sha256_valid"] is False:
        raise ImageError("bootloader.bin fails its own integrity check")
    if len(build.table) != TABLE_SIZE or decode_table(build.table) != list(LAYOUT):
        raise ImageError("partitions.bin is a memory layout this version of the tool does not know")
    if len(build.otadata) != OTADATA_SIZE or selected_slot(build.otadata) != 0:
        raise ImageError("boot_app0.bin does not start the app's slot")


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
