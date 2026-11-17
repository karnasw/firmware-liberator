"""A device's Sonoff identity: what of Sonoff's settings makes it the device its
eWeLink account knows, kept so that the device can go back to Sonoff's firmware
with no backup on the computer.

It is a few entries of Sonoff's NVS, 300 to 550 bytes: frame_params (the
device's id, its key, model and kind), data_backups (a copy of them), and on
some devices productId, channel_params or PID. Not its Wi-Fi - frame_params'
ssid and passwd are left out, as is ESP-IDF's own - nor its histories, logs,
radio calibration or the eWeLink certificates a POW Elite caches.

On Tasmota's layout the tool keeps it in one sector of its own at the very top
of the flash (SECTOR_AT), after Tasmota's filesystem: a partition entry names
it, and the sector says what it is itself - a magic, a version, a length and a
checksum - so it is found even if a reflash loses the entry. Leaving Tasmota,
it goes back into the settings the next firmware has (nvs()), where ESPHome
keeps it as it keeps Sonoff's own.

Where the device has none, it is rebuilt from the device's eWeLink account record
(from_account), which has the id, the key, the model and the kind. What the
record lacks Sonoff's firmware does without: frame_params' sha256, which it
sends neither in its dispatch nor its register, and the other entries
(docs/espressif-research.md says how that was shown).
"""

from __future__ import annotations

import json
import struct
import zlib
from dataclasses import dataclass

try:
    from tools.espressif import nvs_image
    from tools.espressif.inspect_stock_nvs import parse
except ModuleNotFoundError:  # run as a script from tools/
    import nvs_image  # type: ignore[no-redef]
    from inspect_stock_nvs import parse  # type: ignore[no-redef]

SECTOR_AT = 0x3FF000
SECTOR_SIZE = 0x1000
MAGIC = b"SonoffID"
VERSION = 1
HEADER = struct.Struct("<8sHHI")  # magic, version, length, CRC-32 of what follows
# (namespace, key) of the entries that are the identity.
KEPT = (("frame_params", "content"), ("data_backups", "content"), ("productId", "productId"),
        ("channel_params", "content"), ("PID", "content"))
WIFI_FIELDS = ("ssid", "passwd")
FRAME = ("frame_params", "content")


class IdentityError(ValueError):
    pass


@dataclass(frozen=True)
class Identity:
    items: tuple[nvs_image.Item, ...]

    def frame(self) -> dict:
        found = next(item for item in self.items if (item.namespace, item.key) == FRAME)
        return json.loads(found.data.split(b"\0", 1)[0])

    @property
    def deviceid(self) -> str:
        return str(self.frame().get("deviceid", ""))

    @property
    def model(self) -> str:
        return str(self.frame().get("model", ""))


def frame_without_wifi(data: bytes) -> bytes:
    try:
        frame = json.loads(data.split(b"\0", 1)[0])
    except ValueError as exc:
        raise IdentityError("frame_params is not Sonoff's JSON") from exc
    if not isinstance(frame, dict) or not frame.get("deviceid") or not frame.get("apikey"):
        raise IdentityError("frame_params holds no device id and key")
    for field in WIFI_FIELDS:
        frame.pop(field, None)
    return json.dumps(frame, separators=(",", ":")).encode()


def from_nvs(partition: bytes) -> Identity | None:
    """The identity in a device's NVS partition, or None where it has none."""
    names, values = parse(partition)
    found = {}
    for (space, key), value in values.items():
        where = (names.get(space, ""), key)
        if where in KEPT and value.kind in (0x21, 0x41, 0x48):
            data = value.data[:value.size] if value.kind == 0x48 else value.data
            if where == FRAME:
                try:
                    data = frame_without_wifi(data)
                except IdentityError:
                    return None
            found[where] = nvs_image.Item(*where, "string" if value.kind == 0x21 else "blob",
                                          data.rstrip(b"\0") if value.kind == 0x21 else data)
    if FRAME not in found:
        return None
    return Identity(tuple(found[where] for where in KEPT if where in found))


def from_account(device: dict, dispatch: tuple[str, int], *, encrypted: bool) -> Identity:
    """The identity rebuilt from the device's eWeLink account record: its id
    (deviceid), its key (devicekey, which is frame_params' apikey and, where
    the firmware has one, its encrypt_key) and extra's model and uiid. Its
    dispatch server is the account's region's; `encrypted`, for the firmware
    of a classic ESP32 device, which also keeps encrypt_key."""
    extra = device.get("extra") or {}
    deviceid, key, model = device.get("deviceid"), device.get("devicekey"), extra.get("model")
    if not (deviceid and key and model and extra.get("uiid")):
        raise IdentityError("the eWeLink record lacks the device's id, key, model or kind")
    frame = {"init": "true", "uiid": int(extra["uiid"]), "deviceid": deviceid, "apikey": key, "model": model,
             "dis_server": dispatch[0], "dis_port": dispatch[1]}
    if encrypted:
        frame |= {"encrypt_key": key, "is_encrypt": True}
    backups = {"init": "true", "deviceid": deviceid, "apikey": key, "model": model}
    return Identity((
        nvs_image.Item(*FRAME, "blob", json.dumps(frame, separators=(",", ":")).encode()),
        nvs_image.Item("data_backups", "content", "blob", json.dumps(backups, separators=(",", ":")).encode()),
    ))


def payload(identity: Identity) -> bytes:
    return json.dumps([[item.namespace, item.key, item.kind, item.data.hex()] for item in identity.items],
                      separators=(",", ":")).encode()


def to_sector(identity: Identity) -> bytes:
    """The sector that keeps it: header, then the entries as JSON."""
    body = payload(identity)
    record = HEADER.pack(MAGIC, VERSION, len(body), zlib.crc32(body)) + body
    if len(record) > SECTOR_SIZE:
        raise IdentityError("the identity does not fit its sector")
    return record + b"\xff" * (SECTOR_SIZE - len(record))


def from_sector(sector: bytes) -> Identity | None:
    """What a sector keeps, or None where it is not a whole, unchanged record."""
    if len(sector) < HEADER.size:
        return None
    magic, version, length, checksum = HEADER.unpack_from(sector)
    body = sector[HEADER.size:HEADER.size + length]
    if magic != MAGIC or version != VERSION or len(body) != length or zlib.crc32(body) != checksum:
        return None
    try:
        items = tuple(nvs_image.Item(space, key, kind, bytes.fromhex(data))
                      for space, key, kind, data in json.loads(body))
    except (ValueError, TypeError):
        return None
    return Identity(items) if any((i.namespace, i.key) == FRAME for i in items) else None


def nvs(identity: Identity, size: int, wifi: tuple[str, str] | None = None) -> bytes:
    """An NVS partition of `size` holding the identity alone - with `wifi`, the
    network Sonoff's firmware is to join, in frame_params as it keeps it."""
    items = list(identity.items)
    if wifi is not None:
        frame = identity.frame() | {"ssid": wifi[0], "passwd": wifi[1]}
        items = [nvs_image.Item(*FRAME, "blob", json.dumps(frame, separators=(",", ":")).encode())
                 if (item.namespace, item.key) == FRAME else item for item in items]
    return nvs_image.build(items, size)
