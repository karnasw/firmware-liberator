"""What a backup says about a device, for the run's log: enough to add a device
the tool has not met from one person's report. None of it is secret: no Wi-Fi,
account or identity is read, and the MAC is left out. What only the chip itself
can say comes from the bridge's GET /info, where it has one.
"""

from __future__ import annotations

import re
import struct

from tools.espressif import ais_records, flashmap
from tools.espressif.inspect_stock_nvs import parse
from tools.ewelink import firmware_findings

APP_DESC_MAGIC = 0xABCD5432
FIRMWARE_NAME = re.compile(rb"FW[A-Z]+-[A-Z0-9]+-[A-Z0-9-]+")
INFO_LINE = re.compile(r"([a-z][a-z0-9 ]*): ?([\x20-\x7e]*)")
INFO_MAX_LINES = 32


def _text(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("ascii", errors="replace")


def _word(efuse: bytes, index: int) -> int:
    return struct.unpack_from("<I", efuse, index * 4)[0]


def security(efuse: bytes) -> dict[str, object]:
    """Chip revision, secure boot and flash encryption, from the eFuse file:
    for a classic ESP32 the revision is only known as at least this, as telling
    2 from 3 takes a register no file holds (parse_info's chip revision does)."""
    if len(efuse) == flashmap.ESP32_EFUSE_SIZE:
        crypt_count = (_word(efuse, 0) >> 20) & 0x7F
        rev1, rev2 = (_word(efuse, 3) >> 15) & 1, (_word(efuse, 5) >> 20) & 1
        return {"revision": f"at least {2 if rev1 and rev2 else rev1}",
                "secure_boot": bool(_word(efuse, 6) & 0x30),
                "flash_encryption": bin(crypt_count).count("1") % 2 == 1}
    crypt_count = (_word(efuse, 2) >> 18) & 7
    return {"revision": str((_word(efuse, 9) >> 18) & 7),
            "secure_boot": bool((_word(efuse, 3) >> 20) & 1),
            "flash_encryption": bin(crypt_count).count("1") % 2 == 1}


def _app(flash: bytes, slot: flashmap.Partition) -> str:
    app = flash[slot.offset:slot.offset + slot.size]
    if app[:1] != bytes([flashmap.IMAGE_MAGIC]):
        return "empty"
    described = ""
    if struct.unpack_from("<I", app, 32)[0] == APP_DESC_MAGIC:
        described = (f"{_text(app[80:112])} version {_text(app[48:80])}, "
                     f"ESP-IDF {_text(app[144:176])}")
    names = sorted({name.decode() for name in FIRMWARE_NAME.findall(app)})
    return f"{described or 'no description'}; firmware names: {', '.join(names) or 'none found'}"


def parse_info(body: bytes) -> dict[str, str]:
    """The name: value lines of the bridge's GET /info. It names itself on the
    first line; a line not of that form is left out, not trusted into the log."""
    found = {}
    for line in body.decode("ascii", errors="replace").splitlines()[1:INFO_MAX_LINES]:
        match = INFO_LINE.fullmatch(line)
        if match:
            found[match[1]] = match[2]
    return found


def info_lines(info: dict[str, str]) -> list[str]:
    """What the bridge said about the chip, a line each, for the log."""
    revision = [f"Chip revision: {info['chip revision']}"] if "chip revision" in info else []
    return revision + [f"Installer says {name}: {value}" for name, value in info.items() if name != "chip revision"]


def lines(flash: bytes, efuse: bytes | None, info: dict[str, str] | None = None) -> list[str]:
    """The facts, a line each, for the log. `info`, what the bridge said, has
    the chip's exact revision, where the eFuse file may only have a lower bound."""
    info = info or {}
    found = [f"Flash size: {len(flash) // 0x100000} MB"]
    try:
        found.insert(0, f"Chip: {flashmap.chip_of(flash)}")
    except ValueError as exc:
        found.append(f"Chip: not known ({exc})")
    if efuse:
        fuses = security(efuse)
        if "chip revision" not in info:
            found.append(f"Chip revision: {fuses['revision']}")
        found.append(f"Secure boot: {'on' if fuses['secure_boot'] else 'off'}")
        found.append(f"Flash encryption: {'on' if fuses['flash_encryption'] else 'off'}")
    try:
        for part in flashmap.partitions(flash):
            found.append(f"Partition: {part.label} type {part.type:#04x}/{part.subtype:#04x} "
                         f"at {part.offset:#x}, {part.size:#x} bytes")
        found.insert(1, f"Partition table at {flashmap.table_at(flash):#x}")
        for slot in flashmap.app_slots(flash):
            found.append(f"App in {slot.label}: {_app(flash, slot)}")
            app = flash[slot.offset:slot.offset + slot.size]
            if app[:1] == bytes([flashmap.IMAGE_MAGIC]) and FIRMWARE_NAME.search(app):  # Sonoff's, not ours
                found += [f"Its firmware ({slot.label}): {finding.what} - {finding.detail}"
                          for finding in firmware_findings.findings(app)]
        found += records(flash)
    except ValueError as exc:
        found.append(f"Partitions: not readable ({exc})")
    return found + info_lines(info)


def records(flash: bytes) -> list[str]:
    """The ais SDK's records the settings hold, and which version each is: one
    not known here is blanked whole in a readout (scrub.py), so it says so."""
    nvs = flashmap.nvs(flash)
    try:
        names, values = parse(flash[nvs.offset:nvs.offset + nvs.size])
    except (ValueError, struct.error):
        return []
    found = []
    for (space, key), value in sorted(values.items()):
        if key != "info" or names.get(space) not in ("pair", "factory") or value.data[:2] != b"\xaa\x55":
            continue
        known = ais_records.is_pair(value.data) if names[space] == "pair" else ais_records.is_factory(value.data)
        version = struct.unpack_from("<H", value.data, 2)[0]
        found.append(f"Settings: {names[space]} record version {version}, {len(value.data)} bytes"
                     + ("" if known else " - a version not known here"))
    return found
