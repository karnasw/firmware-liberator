#!/usr/bin/env python3
"""Make a BK7238 device's whole stock flash again, with no serial cable, to run it
in the emulator: a device with no public dump can be added this way.

A device's own bootloader, partition table, radio calibration and settings are
read from it once it runs OpenBeken (GET /api/flash, backup.read_flash), and
its stock app, which the conversion overwrote, comes from the model's official
OTA file. This puts the two together as the stock bootloader would have left
them after applying that file:

    app        the official firmware, CRC-striped, with the file's RBL header
               in the partition's last 96 bytes
    download   the file's RBL from its second sector on: the bootloader erases
               only the first when it has applied one
    the rest   as read, less what the interim firmware wrote (INTERIM_WRITES)

Proven on the S61, whose public dump is the answer: see README.md, "Rebuilding
a dump". What is read carries the device's Wi-Fi password and eWeLink identity,
and the result is Sonoff's firmware: neither ever goes in this repository.

    python3 tools/beken/rebuild_dump.py READOUT.bin OFFICIAL.bin OUT.bin [--compare DUMP.bin]
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.beken import fota_wrap, rbl  # noqa: E402

BLOCK, STRIPED_BLOCK = 32, 34
SECTOR = 0x1000
# What follows a firmware in its last block, then zeros: the same in the real
# S61's dump, of its 1.0.2, and after the stock bootloader applied 1.1.0 in the
# emulator; past the firmware's size, so nothing reads it.
LAST_BLOCK_FILL = bytes.fromhex("0000100080800229")
FAL_MAGIC = b"01PE"
FAL_ENTRY = 64
# The bootloader, and so its partition table, in the flash's first 64 KiB of logical space.
BOOTLOADER_PHYSICAL = 0x11000
# What this project's OpenBeken, built for the 4 MB table, writes outside the
# app and the download slot when it is first started, as a range of physical
# flash: its own store, its boot counters and its config. Read a device soon
# after its conversion: one left to run may have written more.
INTERIM_WRITES = ((0x1E5000, 0x1ED000), (0x262004, 0x262008), (0x26A000, 0x26A008), (0x3E1000, 0x3E2000))
# The 4 MB table's download partition: INTERIM_WRITES is known for that build only.
TABLE_4M_DOWNLOAD = 0x286000
# Regions of a whole flash on the 4 MB table, to say what a comparison found.
REGIONS_4M = (("bootloader", 0x000000, 0x011000), ("app", 0x011000, 0x1E0000), ("old radio copy", 0x1E0000, 0x1E1000),
              ("app partition's rest", 0x1E1000, 0x285000), ("app's RBL header", 0x285000, 0x286000),
              ("download", 0x286000, 0x3E0000), ("factory TLV", 0x3E0000, 0x3E1000),
              ("OpenBeken's config sector", 0x3E1000, 0x3E2000), ("EasyFlash app store", 0x3E2000, 0x3EE000),
              ("user config", 0x3EE000, 0x3F6000), ("EasyFlash system store", 0x3F6000, 0x400000))


class RebuildError(ValueError):
    """These inputs cannot make a whole flash."""


@dataclass(frozen=True)
class Partition:
    name: str
    device: str
    offset: int
    length: int

    def physical(self, offset: int = 0) -> int:
        """Where `offset` into the partition is in the flash: a partition on
        the CRC device counts in logical bytes, 34 of flash for every 32."""
        address = self.offset + offset
        return physical(address) if self.device == "beken_onchip_crc" else address


def physical(logical: int) -> int:
    return logical // BLOCK * STRIPED_BLOCK + logical % BLOCK


def crc16(block: bytes) -> int:
    """Beken's CRC after each 32 bytes: polynomial 0x8005, from 0xFFFF, MSB first."""
    reg = 0xFFFF
    for byte in block:
        reg ^= byte << 8
        for _ in range(8):
            reg = (reg << 1) ^ 0x8005 if reg & 0x8000 else reg << 1
            reg &= 0xFFFF
    return reg


def stripe(logical: bytes) -> bytes:
    """`logical` as the flash holds it, each 32 bytes followed by their CRC;
    a short last block is filled as the stock bootloader's write fills it."""
    out = bytearray()
    for start in range(0, len(logical), BLOCK):
        block = logical[start:start + BLOCK]
        block += LAST_BLOCK_FILL[:BLOCK - len(block)].ljust(BLOCK - len(block), b"\0")
        out += block + crc16(block).to_bytes(2, "big")
    return bytes(out)


def partitions(flash: bytes) -> dict[str, Partition]:
    """The FAL partition table in the bootloader at the start of `flash`:
    entries of magic, name, device, offset, length, CRC-32 of the first 60 bytes."""
    import zlib
    logical = b"".join(flash[start:start + BLOCK] for start in range(0, BOOTLOADER_PHYSICAL, STRIPED_BLOCK))
    table = {}
    at = logical.find(FAL_MAGIC)
    while at >= 0:
        entry = logical[at:at + FAL_ENTRY]
        if len(entry) == FAL_ENTRY and zlib.crc32(entry[:60]) == int.from_bytes(entry[60:64], "little"):
            name = entry[4:28].split(b"\0")[0].decode("ascii", "replace")
            device = entry[28:52].split(b"\0")[0].decode("ascii", "replace")
            table[name] = Partition(name, device, int.from_bytes(entry[52:56], "little"),
                                    int.from_bytes(entry[56:60], "little"))
        at = logical.find(FAL_MAGIC, at + 1)
    for needed in ("bootloader", "app", "download"):
        if needed not in table:
            raise RebuildError(f"no {needed} partition in the bootloader's table: is this a whole read-out?")
    return table


def official_image(official: bytes) -> bytes:
    """The RBL in an official file: in a FOTA container, or the file itself."""
    if official[:len(rbl.MAGIC)] == rbl.MAGIC:
        return official
    images = fota_wrap.unwrap(official)
    if len(images) != 1:
        raise RebuildError(f"the official file holds {len(images)} images, not one")
    return images[0][2]


def erase(flash: bytearray, start: int, end: int) -> None:
    flash[start:end] = b"\xff" * (end - start)


def rebuild(readout: bytes, official: bytes, interim_writes: tuple[tuple[int, int], ...] | None = None) -> bytes:
    """The stock flash of the device `readout` was read from, running `official`.
    `interim_writes` is where the firmware it runs wrote: given for any table
    but the 4 MB one, whose build is INTERIM_WRITES."""
    table = partitions(readout)
    app, download = table["app"], table["download"]
    if interim_writes is None:
        if download.offset != TABLE_4M_DOWNLOAD:
            raise RebuildError("what the interim firmware wrote is known only on the 4 MB table: "
                               f"this one's download partition is at 0x{download.offset:x}")
        interim_writes = INTERIM_WRITES
    image = official_image(official)
    firmware = rbl.unpack(image)
    if rbl.header(image).name != "app":
        raise RebuildError(f"the official file's image is for {rbl.header(image).name!r}, not the app")
    if len(firmware) > app.length - rbl.HEADER or len(image) > download.length:
        raise RebuildError("the official firmware does not fit this device's partitions")

    flash = bytearray(readout)
    # The bootloader erases as far as the firmware it writes: as far as the
    # interim firmware's, and then the official one's, in a sector's whole.
    tail = app.physical(app.length - rbl.HEADER)
    written = max(_interim_size(readout, tail), len(firmware))
    end = app.physical(0) + (physical(written) + SECTOR - 1) // SECTOR * SECTOR
    erase(flash, app.physical(0), min(end, tail))
    striped = stripe(firmware)
    flash[app.physical(0):app.physical(0) + len(striped)] = striped
    erase(flash, tail, tail + len(stripe(image[:rbl.HEADER])))
    header = stripe(image[:rbl.HEADER])
    flash[tail:tail + len(header)] = header

    erase(flash, download.physical(0), download.physical(0) + download.length)
    remainder = image[SECTOR:]
    flash[download.physical(SECTOR):download.physical(SECTOR) + len(remainder)] = remainder

    for start, stop in interim_writes:
        erase(flash, start, stop)
    return bytes(flash)


def _interim_size(readout: bytes, tail: int) -> int:
    """How much the firmware the device now runs takes of the app partition, by
    the RBL header in its last bytes: 0 if it has none."""
    logical = b"".join(readout[start:start + BLOCK] for start in range(tail, tail + STRIPED_BLOCK * 3, STRIPED_BLOCK))
    try:
        return rbl.header(logical[:rbl.HEADER]).size_raw
    except rbl.RblError:
        return 0


def surviving_old_app(readout: bytes, official: bytes) -> tuple[int, int]:
    """(bytes equal, bytes compared) of the old stock app that outlived the
    conversion against the official firmware: the interim firmware is shorter,
    so the rest of what the device ran is still there. All equal says the file
    is the version the device ran; little equal, that it is another."""
    table = partitions(readout)
    app = table["app"]
    firmware = stripe(rbl.unpack(official_image(official)))
    start = physical(_interim_size(readout, app.physical(app.length - rbl.HEADER)))
    start = (start + SECTOR - 1) // SECTOR * SECTOR   # past the sector the interim firmware ends in
    old = readout[app.physical(0) + start:app.physical(0) + len(firmware)]
    return sum(a == b for a, b in zip(old, firmware[start:])), len(old)


def compare(flash: bytes, other: bytes, regions: tuple[tuple[str, int, int], ...] = REGIONS_4M) -> list[tuple[str, int, int, int]]:
    """For each region: its name, start, end, and how many of its bytes differ."""
    return [(name, start, end, sum(a != b for a, b in zip(flash[start:end], other[start:end])))
            for name, start, end in regions if end <= min(len(flash), len(other))]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("readout", type=Path, help="the device's whole flash, read over OpenBeken's REST API")
    parser.add_argument("official", type=Path, help="the model's official OTA file")
    parser.add_argument("out", type=Path, help="where the rebuilt flash is written")
    parser.add_argument("--compare", type=Path, help="a real dump to compare it with, region by region")
    args = parser.parse_args(argv)
    readout, official = args.readout.read_bytes(), args.official.read_bytes()
    try:
        flash = rebuild(readout, official)
        same, compared = surviving_old_app(readout, official)
    except (RebuildError, rbl.RblError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    args.out.write_bytes(flash)
    print("%s: %d bytes. Of the old app that survived, %d of %d bytes match the official file%s."
          % (args.out, len(flash), same, compared,
             "" if same == compared else ": the device ran another version"))
    if args.compare:
        for name, start, end, differ in compare(flash, args.compare.read_bytes()):
            print("  0x%06x-0x%06x %-26s %s" % (start, end, name, "same" if not differ else "%d bytes differ" % differ))
    return 0


if __name__ == "__main__":
    sys.exit(main())
