"""Take what is the owner's out of a copy of a Beken device's whole flash before
it leaves the computer (tools/autoflash/readout_beken.py): its Wi-Fi network's name
and password and its eWeLink identity, as tools/espressif/scrub.py does for an
Espressif device's.

eWeLink's Beken firmware keeps its settings as EasyFlash V4 (EF40) KV40 nodes
(tools/beken/ef40.py): the Wi-Fi network's name plain under `wifi-ssid` and
encoded under `ssid`, its password the same under `wifi-pass` and `pass`, and a
JSON setting may name the network ("ssid"); the eWeLink identity under `apiKey`, `app-apikey`,
`secureCertSha256` and `user`; a Matter device's fabric under keys `f/...`.
The `factory` record is kept as it is: it is the device's own factory
credentials, encrypted (base64 of ciphertext), not its owner's, and the stock
firmware does not start with it changed in any way - found booting variants of
an S61 in the emulator, each with one setting taken out. An EF40 store never erases a node in place, so every node of those
names is changed, whatever its state. The name's and the password's every other
copy goes too - OpenBeken, which the readout runs, saves the Wi-Fi it imported in
its own settings - but none in the firmware areas, whose bytes are code, and no
other copy of a name shorter than MIN_LEN, which unrelated bytes hold too. Each node
changed has its CRC made good again, so the copy still starts in the emulator.
"""

from __future__ import annotations

import re
import struct
import zlib

from tools.espressif.scrub import made_up

# The settings stores at the top of a 4 MB BK7238's flash (docs/beken-research.md):
# the factory TLV, OpenBeken's settings, the EF40 app store and the system store.
STORES = (0x3E0000, 0x400000)
# The firmware areas: the app partition and the download slot, as the 4 MB table lays them out.
FIRMWARE = (range(0x11000, 0x286000), range(0x286000, 0x3E0000))
PASSWORDS = {b"wifi-pass", b"pass"}
NETWORK = {b"wifi-ssid", b"ssid"}
JSON_SSID = re.compile(rb'"ssid"\s*:\s*"((?:[^"\\]|\\.)*)"')
IDENTITY = {b"apiKey", b"app-apikey", b"secureCertSha256", b"user"}
MATTER = b"f/"
JSON_IDENTITY = re.compile(rb'"(deviceid|apikey|devicekey|encrypt_key)"\s*:\s*"([^"]{4,})"')
MIN_LEN = 4


def _nodes(flash: bytes):
    """(start, name, value start, value length) of every KV40 node in the stores."""
    start, end = STORES
    at = flash.find(b"KV40", start, end)
    while 0 <= at < end - 20:
        name_len = flash[at + 12]
        value_len = struct.unpack_from("<I", flash, at + 16)[0]
        value = at + 20 + name_len
        if 0 < name_len <= 64 and value_len <= 0x2000 and value + value_len <= end:
            yield at, bytes(flash[at + 20:value]), value, value_len
        at = flash.find(b"KV40", at + 4, end)


def _reseal(out: bytearray, at: int, value: int, value_len: int) -> None:
    struct.pack_into("<I", out, at + 8, zlib.crc32(bytes(out[at + 12:value + value_len])) & 0xFFFFFFFF)


def _in_firmware(at: int) -> bool:
    return any(at in area for area in FIRMWARE)


def _replace_all(out: bytearray, old: bytes, new: bytes) -> int:
    count, at = 0, out.find(old)
    while at >= 0 and old:
        if not _in_firmware(at):
            out[at:at + len(old)] = new
            count += 1
        at = out.find(old, at + len(old))
    return count


def scrub(flash: bytes) -> tuple[bytes, int]:
    """`flash` with its Wi-Fi network's name and password and its eWeLink identity
    taken out, and how many places were changed."""
    out = bytearray(flash)
    secrets: dict[bytes, bytes] = {}
    changed = 0
    for at, name, value, length in _nodes(flash):
        data = bytes(flash[value:value + length])
        if name in PASSWORDS or name in NETWORK:
            new = b"*" * length
            plain = data.rstrip(b"\0")
            if name in (b"wifi-pass", b"wifi-ssid") and len(plain) >= MIN_LEN:
                secrets[plain] = b"*" * len(plain)
        elif name in IDENTITY:
            # A clean printable id (a UUID, the app-apikey) is replaced by a
            # made-up one of its shape; anything else (a binary cert blob, the
            # obfuscated apiKey) is zeroed whole, so no byte of it is left.
            printable = data.rstrip(b"\0")
            if printable and all(0x20 <= byte < 0x7F for byte in printable):
                new = made_up(data.decode("latin1")).encode("latin1")
            else:
                new = bytes(length)
            if len(data) >= MIN_LEN:
                secrets[data] = new
        elif name.startswith(MATTER):
            new = bytes(length)
        else:
            for found in JSON_IDENTITY.finditer(data):
                secrets[found.group(2)] = made_up(found.group(2).decode("latin1")).encode("latin1")
            # A network named in a JSON setting, however short: starred there.
            new = bytearray(data)
            for found in JSON_SSID.finditer(data):
                begin, end = found.span(1)
                new[begin:end] = b"*" * (end - begin)
                if end - begin >= MIN_LEN:
                    secrets[found.group(1)] = b"*" * (end - begin)
            if bytes(new) == data:
                continue
            new = bytes(new)
        out[value:value + length] = new
        _reseal(out, at, value, length)
        changed += 1
    for old in sorted(secrets, key=len, reverse=True):
        changed += _replace_all(out, old, secrets[old])
    for at, _name, value, length in _nodes(bytes(out)):
        if bytes(out[value:value + length]) != flash[value:value + length]:
            _reseal(out, at, value, length)
    return bytes(out), changed


def wifi_password(flash: bytes) -> str | None:
    """The plain Wi-Fi password the stock store holds, if any: for a test that it is gone."""
    found = None
    for _, name, value, length in _nodes(flash):
        if name == b"wifi-pass":
            found = flash[value:value + length].rstrip(b"\0").decode("latin1")
    return found


def json_identity(flash: bytes) -> list[str]:
    """Every identity a JSON setting names, for a test that none is left."""
    return [found.group(2).decode("latin1") for found in JSON_IDENTITY.finditer(flash)]

