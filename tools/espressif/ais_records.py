"""The two records Sonoff's "ais" SDK keeps in NVS, the firmware of the BASICR4
and MINIR4, where the S60's keeps frame_params: read from that firmware
(ais_factory_data.c, ais_pair; tools/espressif/README.md).

- The factory record (namespace "factory", blob "info"), written at the factory
  by AT+LICENSE_WRITE: the device's eWeLink identity, the chip it was made for,
  and a SHA-256 tying them together. Without one the firmware never leaves
  factory mode.
- The pair record (namespace "pair", blob "info"): the Wi-Fi it was paired
  with, and where it finds eWeLink. It counts as paired when the SSID is set.

Each starts with the magic and version aisFactoryLoadInfo and aisPairLoadInfo
check, and holds NUL-padded text at fixed places. The MINIR4's 1.2.0 writes its
pair record again as version 2, with room for a longer host, where the
BASICR4's 1.1.0 keeps version 1: its SSID's and password's places were seen in
an emulated one, given a version 1 record holding a made-up password.
"""

from __future__ import annotations

import hashlib
import struct

MAGIC, VERSION = 0x55AA, 1
HEADER = struct.pack("<HH", MAGIC, VERSION)

FACTORY_SIZE = 178
# Each text field's place and size.
FACTORY_FIELDS = {"chip_mac": (4, 16), "device_model": (20, 32), "deviceid": (52, 16),
                  "factory_apikey": (68, 40), "sta_mac": (108, 18), "sap_mac": (126, 18)}
FACTORY_UIID = 144   # u16
FACTORY_DIGEST = 146  # 32 bytes, to the end

PAIR_SIZE = 142
PAIR_PORT = 4  # u16
PAIR_FIELDS = {"host": (6, 32), "ssid": (38, 33), "password": (71, 65)}
# Each version's size and fields: version 1's above, which is the one written.
PAIR_LAYOUTS = {1: (PAIR_SIZE, PAIR_FIELDS),
                2: (176, {"host": (6, 65), "ssid": (71, 33), "password": (104, 65)})}


def is_factory(data: bytes) -> bool:
    return len(data) == FACTORY_SIZE and data.startswith(HEADER)


def pair_fields(data: bytes) -> dict[str, tuple[int, int]] | None:
    """Where a pair record of either version holds each field; None for
    anything else."""
    if len(data) < 4 or struct.unpack_from("<H", data)[0] != MAGIC:
        return None
    size, fields = PAIR_LAYOUTS.get(struct.unpack_from("<H", data, 2)[0], (0, {}))
    return fields if len(data) == size else None


def is_pair(data: bytes) -> bool:
    return pair_fields(data) is not None


def text(record: bytes, fields: dict[str, tuple[int, int]], name: str) -> bytes:
    at, size = fields[name]
    return bytes(record[at:at + size]).split(b"\0", 1)[0]


def _put(record: bytearray, fields: dict[str, tuple[int, int]], name: str, value: str | bytes) -> None:
    at, size = fields[name]
    encoded = value.encode() if isinstance(value, str) else value
    if len(encoded) >= size:
        raise ValueError(f"{name}: {value!r} does not fit its {size} bytes")
    record[at:at + size] = encoded.ljust(size, b"\0")


def factory_digest(record: bytes) -> bytes:
    """The SHA-256 the firmware stores with the fields, as aisFactorySaveInfo
    works it out: the fields run together, the uiid in decimal, the chip last."""
    field = lambda name: text(record, FACTORY_FIELDS, name)  # noqa: E731
    uiid = struct.unpack_from("<H", record, FACTORY_UIID)[0]
    signed = (field("factory_apikey") + field("sta_mac") + field("sap_mac") + field("device_model")
              + str(uiid).encode() + field("deviceid") + field("chip_mac"))
    return hashlib.sha256(signed).digest()


def factory_record(values: dict[str, str], uiid: int) -> bytes:
    """A factory record of these fields (FACTORY_FIELDS' names)."""
    record = bytearray(FACTORY_SIZE)
    record[:4] = HEADER
    for name in FACTORY_FIELDS:
        _put(record, FACTORY_FIELDS, name, values[name])
    struct.pack_into("<H", record, FACTORY_UIID, uiid)
    record[FACTORY_DIGEST:] = factory_digest(record)
    return bytes(record)


def resealed(record: bytes) -> bytes:
    """A factory record whose fields were changed, with its SHA-256 again."""
    out = bytearray(record)
    out[FACTORY_DIGEST:] = factory_digest(record)
    return bytes(out)


def pair_with(record: bytes, port: int | None = None, **fields: str) -> bytes:
    """A pair record, of whichever version it is, with these fields changed."""
    layout = pair_fields(record)
    if layout is None:
        raise ValueError("not a pair record")
    out = bytearray(record)
    if port is not None:
        struct.pack_into("<H", out, PAIR_PORT, port)
    for name, value in fields.items():
        _put(out, layout, name, value)
    return bytes(out)


def pair_record(ssid: str, password: str, host: str, port: int) -> bytes:
    record = bytearray(PAIR_SIZE)
    record[:4] = HEADER
    struct.pack_into("<H", record, PAIR_PORT, port)
    for name, value in (("host", host), ("ssid", ssid), ("password", password)):
        _put(record, PAIR_FIELDS, name, value)
    return bytes(record)
