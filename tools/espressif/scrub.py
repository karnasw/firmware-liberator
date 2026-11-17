"""Take what is the owner's out of a copy of a device's whole flash before the
copy leaves the computer (tools/autoflash/readout_espressif.py): its Wi-Fi network's
name and passwords, and its eWeLink identity, replaced by made-up values of the
same shape.

ESP-IDF keeps the station's password and its PMK in NVS (nvs.net80211:
sta.pswd, sta.pmk; the soft AP's as ap.passwd, ap.pmk), and Sonoff's own
settings keep the password again (frame_params "passwd"). NVS never erases an
entry in place: one replaced is only marked so, and its old bytes stay, so a
password the device had before is still there. So every entry of those names is
blanked whatever its state, and then every copy of each password found anywhere in the flash but the firmware images, Sonoff's own published
code: a password there is the firmware's, its setup network's. The network's
name goes the same way: the station's (sta.ssid, every one an entry replaced
has left), Sonoff's own copy (frame_params "ssid") and every other copy. The
soft AP's name (ap.ssid) is the device's own setup network, not the owner's,
and the identity below already takes the device id out of it.

Sonoff's ais SDK (the BASICR4's and MINIR4's firmware) keeps the network's
name and password in its pair record instead (tools/espressif/ais_records.py),
and they go the same way, as do the name and password of every older copy of
that record NVS has left. A pair or factory record of a version not known here
is blanked whole after its header: nothing says where its secrets are.

Sonoff's settings name the device to eWeLink (frame_params: deviceid, apikey,
encrypt_key, devicekey; on the ais SDK its factory record's deviceid and
factory_apikey, whose SHA-256 is worked out again). Each is replaced throughout the flash by a made-up
value of the same length and form, as tools/espressif/emulator/public_dump.py
makes the public test devices, which boot and convert in the emulator: the
identity only matters to eWeLink's servers, which an emulated copy never meets.

Each settings entry changed has its checksums made good again, so the copy
still starts as it was, on a device or in the emulator.

A password a firmware keeps somewhere its settings do not show goes as it is:
the copy is private, and a question about it would only make the owner
hesitate to send it.
"""

from __future__ import annotations

import hashlib
import json
import re
import struct
import zlib

from tools.espressif import ais_records, flashmap
from tools.espressif.inspect_stock_nvs import parse

PAGE = 4096
ENTRIES = 126
# Entry kinds that carry their bytes in the entries after them.
VARIABLE = {0x21, 0x41, 0x42}
TEXT_KEYS = {b"sta.pswd", b"ap.passwd"}
KEY_KEYS = {b"sta.pmk", b"ap.pmk"}
SSID_KEYS = {b"sta.ssid": "sta", b"ap.ssid": "ap"}
NVS_OFFSET, NVS_SIZE = 0x9000, 0x10000
FRAME_PASSWD = re.compile(rb'"passwd"\s*:\s*"((?:[^"\\]|\\.)*)"')
FRAME_SSID = re.compile(rb'"ssid"\s*:\s*"((?:[^"\\]|\\.)*)"')
IDENTITY = ("deviceid", "apikey", "encrypt_key", "devicekey")
FACTORY_IDENTITY = ("deviceid", "factory_apikey")
MIN_LEN = 4


def _nvs_span(flash: bytes) -> tuple[int, int]:
    try:
        partition = flashmap.nvs(flash)
        return partition.offset, partition.size
    except ValueError:
        return NVS_OFFSET, min(NVS_SIZE, max(0, len(flash) - NVS_OFFSET))


def _entries(flash: bytes, start: int, size: int):
    """(entry offset, key, payload offset, payload length) of every variable
    entry, written, erased or replaced, that looks like one."""
    for base in range(start, start + size - PAGE + 1, PAGE):
        if struct.unpack_from("<I", flash, base)[0] == 0xFFFFFFFF:
            continue
        index = 0
        while index < ENTRIES:
            at = base + 64 + index * 32
            _, kind, span, _ = struct.unpack_from("<BBBB", flash, at)
            key = flash[at + 8 : at + 24].split(b"\0", 1)[0]
            if kind in VARIABLE and 2 <= span <= ENTRIES - index and key:
                length = struct.unpack_from("<H", flash, at + 24)[0]
                yield at, key, at + 32, min(length, (span - 1) * 32)
                index += span
            else:
                index += 1


def _apps(flash: bytes) -> list[range]:
    """Where the firmware images are: Sonoff's own published code, the same on
    every device, checked byte for byte when it starts. A password found there
    is the firmware's (its setup network's), not the owner's, so nothing in
    them is changed."""
    try:
        return [range(slot.offset, slot.offset + slot.size) for slot in flashmap.app_slots(flash)]
    except ValueError:
        return []


def _replace_all(out: bytearray, old: bytes, new: bytes, apps: list[range]) -> int:
    count, at = 0, out.find(old)
    while at >= 0 and old:
        if not any(at in app for app in apps):
            out[at : at + len(old)] = new
            count += 1
        at = out.find(old, at + len(old))
    return count


def _reseal(before: bytes, out: bytearray) -> None:
    """Each settings entry whose bytes were changed here, with its data and
    header checksums worked out again: only one whose checksum held before."""
    start, size = _nvs_span(before)
    for at, _, payload, length in _entries(before, start, size):
        stored = struct.unpack_from("<I", before, at + 28)[0]
        if (before[payload : payload + length] == out[payload : payload + length]
                or zlib.crc32(before[payload : payload + length], 0xFFFFFFFF) != stored):
            continue
        struct.pack_into("<I", out, at + 28, zlib.crc32(bytes(out[payload : payload + length]), 0xFFFFFFFF))
        header = bytes(out[at : at + 4]) + bytes(out[at + 8 : at + 32])
        struct.pack_into("<I", out, at + 4, zlib.crc32(header, 0xFFFFFFFF))


# The ais SDK's records, by their namespace (their key is "info"), and how each
# version known here is told.
AIS_SPACES = {"pair": ais_records.is_pair, "factory": ais_records.is_factory}


def _blank_unknown_ais(flash: bytes, out: bytearray, space: str) -> int:
    """Blank, after its header, each `space` record whose version is not one
    known here; how many were."""
    start, size = _nvs_span(flash)
    try:
        names, _ = parse(flash[start:start + size])
    except (ValueError, struct.error):
        return 0
    blanked = 0
    for at, key, payload, length in _entries(flash, start, size):
        data = flash[payload : payload + length]
        if (key == b"info" and names.get(flash[at]) == space and len(data) > 4
                and struct.unpack_from("<H", data)[0] == ais_records.MAGIC and not AIS_SPACES[space](data)):
            out[payload + 4 : payload + length] = bytes(length - 4)
            blanked += 1
    return blanked


def _name(value: bytes) -> bool:
    """Whether `value` can be a Wi-Fi network's name: 1 to 32 bytes, not erased
    flash (0xFF) or nothing (0x00) over and over."""
    return 0 < len(value) <= 32 and set(value) - {0x00, 0xFF} != set()


def _password(value: bytes) -> bool:
    """Whether `value` can be a Wi-Fi password: printable, of WPA's lengths (or
    WEP's shorter ones), not one byte over and over. An entry that was never
    given one holds erased flash, 0xFF, found everywhere else as well."""
    return MIN_LEN <= len(value) <= 64 and all(0x20 <= byte < 0x7F for byte in value) and len(set(value)) > 1


def wifi(flash: bytes) -> tuple[bytes, int]:
    """`flash` with its Wi-Fi network's name, passwords and keys blanked, and how
    many places were."""
    out = bytearray(flash)
    start, size = _nvs_span(flash)
    secrets: set[bytes] = set()
    names: set[bytes] = set()      # every network name, for the PMKs
    station: set[bytes] = set()    # the owner's network's, to take out
    blanked = 0
    for _, key, at, length in _entries(flash, start, size):
        if key in TEXT_KEYS:
            value = bytes(out[at : at + length]).split(b"\0", 1)[0]
            if _password(value):
                secrets.add(value)
                out[at : at + len(value)] = b"*" * len(value)
                blanked += 1
        elif key in KEY_KEYS:
            out[at : at + length] = bytes(length)
            blanked += 1
        elif key in SSID_KEYS and length >= 36:
            count = struct.unpack_from("<I", flash, at)[0]
            name = flash[at + 4 : at + 4 + count] if 0 < count <= 32 else b""
            if _name(name):
                names.add(name)
                if SSID_KEYS[key] == "sta":
                    station.add(name)
                    out[at + 4 : at + 4 + count] = b"*" * count
                    blanked += 1
    for _, _, at, length in _entries(flash, start, size):
        layout = ais_records.pair_fields(flash[at : at + length])
        if layout is None:
            continue
        for field, kept in (("password", secrets), ("ssid", station)):
            value = ais_records.text(flash[at : at + length], layout, field)
            if (_password if field == "password" else _name)(value):
                kept.add(value)
                names.update([value] if field == "ssid" else [])
                place = at + layout[field][0]
                out[place : place + len(value)] = b"*" * len(value)
                blanked += 1
    blanked += _blank_unknown_ais(flash, out, "pair")
    for found in FRAME_PASSWD.finditer(flash):
        if _password(found.group(1)):
            secrets.add(found.group(1))
    apps = _apps(flash)
    # Sonoff's own copy of the network's name, however short.
    for found in FRAME_SSID.finditer(flash):
        name, (begin, end) = found.group(1), found.span(1)
        if _name(name) and not any(begin in app for app in apps):
            station.add(name)
            names.add(name)
            out[begin:end] = b"*" * (end - begin)
            blanked += 1
    # A password's PMK is its network name's too: one kept elsewhere is found by it.
    keys = {hashlib.pbkdf2_hmac("sha1", secret, name, 4096, 32) for secret in secrets for name in names}
    for secret in sorted(secrets | {json.dumps(s.decode("utf-8", "replace"))[1:-1].encode() for s in secrets},
                         key=len, reverse=True):
        blanked += _replace_all(out, secret, b"*" * len(secret), apps)
    # Every other copy of the network's name: one of MIN_LEN or more, as a
    # shorter one would be found in unrelated bytes too.
    for name in sorted(station, key=len, reverse=True):
        if len(name) >= MIN_LEN:
            blanked += _replace_all(out, name, b"*" * len(name), apps)
    for key in keys:
        blanked += _replace_all(out, key, bytes(len(key)), apps)
    _reseal(flash, out)
    return bytes(out), blanked


def made_up(value: str) -> str:
    """A value in the form of `value`, the same length, naming nobody: each
    letter or digit a 0 but the first, kept, so a device id still starts 1000."""
    return value[:1] + re.sub(r"[0-9A-Za-z]", "0", value[1:])


def identity(flash: bytes) -> tuple[bytes, int]:
    """`flash` with the device's eWeLink identity, as its frame_params names it,
    replaced throughout by made-up values; and how many places were."""
    out = bytearray(flash)
    start, size = _nvs_span(flash)
    values: set[str] = set()
    for _, key, at, length in _entries(flash, start, size):
        if key == b"content":
            try:
                frame = json.loads(flash[at : at + length].split(b"\0", 1)[0])
            except ValueError:
                continue
            if isinstance(frame, dict):
                values |= {str(frame[name]) for name in IDENTITY
                           if isinstance(frame.get(name), str) and len(frame[name]) >= MIN_LEN}
        elif ais_records.is_factory(flash[at : at + length]):
            for name in FACTORY_IDENTITY:
                value = ais_records.text(flash[at : at + length], ais_records.FACTORY_FIELDS, name)
                if len(value) >= MIN_LEN:
                    values.add(value.decode("ascii", "replace"))
    replaced = 0
    apps = _apps(flash)
    for value in sorted(values, key=len, reverse=True):
        replaced += _replace_all(out, value.encode(), made_up(value).encode(), apps)
    for _, _, at, length in _entries(flash, start, size):
        if ais_records.is_factory(flash[at : at + length]) and out[at : at + length] != flash[at : at + length]:
            out[at : at + length] = ais_records.resealed(bytes(out[at : at + length]))
    replaced += _blank_unknown_ais(flash, out, "factory")
    _reseal(flash, out)
    return bytes(out), replaced

