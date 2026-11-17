#!/usr/bin/env python3
"""A device for the emulator from a public dump of a stock one, for the kinds of
device no one here owns.

    python tools/espressif/emulator/public_dump.py KIND
    python tools/espressif/emulator/public_dump.py --remove KIND

The dump is fetched from where it is published, checked against its hash and
filed as a test copy's backup (copy_device.py), under a MAC made up for the kind:
emu.sh --device MAC runs it. Nothing of it reaches this repository, and no image
holds it (AGENTS.md).

A public dump is of someone's real device, and some hold its eWeLink identity: it
is replaced with a made-up one on the way in, so that no run can ever speak as
that device, whatever it reaches.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from tools.autoflash import kinds, records  # noqa: E402
from tools.espressif import ais_records, build_vendor_ota, flashmap  # noqa: E402
from tools.espressif.emulator import appimage, copy_device, rewrite_nvs  # noqa: E402
from tools.espressif.inspect_stock_nvs import parse  # noqa: E402

COMMIT = "4d3556605da2ce101382a01b6fb7d84e68c3df2a"
FLASH_DUMPS = f"https://raw.githubusercontent.com/openshwprojects/FlashDumps/{COMMIT}/IoT/ESP32"
OTA_FIRMWARE = f"https://raw.githubusercontent.com/openshwprojects/FlashDumps/{COMMIT}/IoT/zOTAz/eWeLink"
CACHE = ROOT / "captures" / "emulation" / "public-dumps"
FLASH_SIZE = 0x400000


@dataclass(frozen=True)
class Dump:
    """A public whole-flash dump, and the number its made-up identity is made from."""
    file: str
    sha256: str
    number: int

    @property
    def url(self) -> str:
        return f"{FLASH_DUMPS}/{self.file}"

    @property
    def mac(self) -> str:
        return f"12:34:56:e3:2d:{self.number:02x}"  # locally administered, so nobody's

    @property
    def deviceid(self) -> str:
        return f"10000e32{self.number:02x}"  # as long as a real one

    @property
    def apikey(self) -> str:
        return f"00000000-0000-4000-8000-0000000e32{self.number:02x}"  # in a real one's shape


DUMPS = {
    "sonoff-pow-elite": Dump("Sonoff-Powr316D.bin",
                             "d31118361c0bfdece510eed23ae4fa7ad5ebad2e7b76204d9a7bc09a66f9187e", 1),
    "sonoff-th-elite": Dump("Sonoff-THR320D.bin",
                            "538d8e6244388abe6f758e821a2c58ccdfe3a30badd248f1c64b72c23d6cdf6d", 2),
    "sonoff-powct": Dump("Sonoff-POWCT-20250518.bin",
                         "295cd9b7791d6a088216bfafc03d63bbb4622df5e68d2f26f499ed64161b3625", 3),
    "sonoff-nspanel": Dump("Sonoff-NSPanel.bin",
                           "a047dcdb5fac92d10d956cd09b0100df4aae82ef61b5df9bf71aed7270c89596", 4),
}


def fetched(dump: "Dump | Update") -> bytes:
    """The file, once fetched into CACHE, checked against its hash."""
    path = CACHE / dump.file
    data = path.read_bytes() if path.is_file() else b""
    if hashlib.sha256(data).hexdigest() != dump.sha256:
        with urllib.request.urlopen(urllib.parse.quote(dump.url, safe=":/"), timeout=120) as reply:
            data = reply.read()
        if hashlib.sha256(data).hexdigest() != dump.sha256:
            raise SystemExit(f"{dump.url} is not the file pinned here (its hash differs)")
        CACHE.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.write_bytes(data)
    return data


@dataclass(frozen=True)
class Update:
    """eWeLink's public firmware update for a device no whole dump of which is
    published: run on a stock device of the same chip's flash in place of that
    one's firmware. It tests the firmware, not the device's real layout, which is
    why a real one with another stops before anything is written over Sonoff's."""
    file: str
    sha256: str
    number: int
    donor: str         # the key in DUMPS or S60_DUMP of the flash it goes on
    stock_model: str   # what its firmware calls itself, read from it
    uiid: int = 0      # eWeLink's number for its kind, where it differs from the donor's or its firmware checks it
    settings: str = ""  # Sonoff's user settings (user_usual) for it, where the donor's are not its own
    table_at: int = flashmap.TABLE_AT  # where its firmware reads the partition table
    ais_sdk: bool = False  # its firmware is Sonoff's ais SDK, which keeps its own records (ais_rows)

    @property
    def url(self) -> str:
        return f"{OTA_FIRMWARE}/{self.file}"

    mac = Dump.mac
    deviceid = Dump.deviceid
    apikey = Dump.apikey


S60_DUMP = Dump("Sonoff_S60TPG_UK_Plug_ESP32-C3_SM-049_1.2.0.bin",
                "a638d7251c20a20ff14cf0eef9be3058bedd26ed80e260d7d0107d6b2599dff4", 0xF0)
UPDATES = {
    # Built with the S60 bootloader's ESP-IDF (v4.4.2-dirty), but for a table at
    # 0xC000 (its load_partitions maps sector 0 and adds 0xC000). Its uiid is
    # SonoffLAN's (DEVICES.md), which has the S60's as the S60's settings do; the
    # MINIR4's, below, is the same.
    "sonoff-basicr4": Update("SONOFF_BASICR4_(FWSW-E32R4-BASICR4-ESP32C3)_ESP32-C3_v1.1.0_user1.bin",
                             "cd4bbf2b08d9918a920d81c3ed2cef6e546673fd2c280106fd6852528866ac4a", 5,
                             "s60", "SN-ESP32C3-BASICR4-01", 138, table_at=0xC000, ais_sdk=True),
    "sonoff-minir4": Update("SONOFF_MINIR4_(FWSW-E32R4-MINIR4-ESP32D0WDR2V3)_ESP32_v1.2.0_user1.bin",
                            "c881ddd667bc9d8c6905acf30051a1f2e4270c4be2dcfcfa39a5bfdd137c6ec0", 6,
                            "sonoff-pow-elite", "SN-ESP32D0-MINIR4-01", 138, ais_sdk=True),
    # The DualR3's firmware keeps its work mode as deviceMode in user_usual (1
    # its two relays, 2 a curtain motor) and, finding none, starts as a motor,
    # refusing every switch. Its own settings in relay mode, as a DualR3 out of
    # the box is taken to be, in place of the TH Elite's; the firmware writes the
    # rest of its defaults itself. uiid 126 is eWeLink's DualR3.
    # docs/espressif-research.md#the-dual-r3s-work-mode
    "sonoff-dualr3": Update("SONOFF_DUALR3-Family_(FWSWES-E32DR3-DUALR3-ESP32D0WDV3)_ESP32_v1.6.1_user1.bin",
                            "4f14b1298061ee6669fd5fcbfa89baf6c2ef3daafa8c575736f38088179240b2", 7,
                            "sonoff-th-elite", "E32-2SW-P0", 126, '{"init":"true","deviceMode":1}'),
    **{f"sonoff-m5-{gang}c": Update("SONOFF_M5-1C-3C_(FWSWES-E32T5-T5-ESP32D0WDV3)_ESP32_v1.3.0_user1.bin",
                                    "f149baf000eb226c27babd1ea2a4bef6bcc3ca2973d9c57063106730112f4e6b", 7 + gang,
                                    "sonoff-pow-elite", "ESP32D0-MSW-T5", 159 + gang)
       for gang in (1, 2, 3)},
}


def identities(frame: dict, dump: Dump) -> dict[bytes, bytes]:
    """What of the device's own eWeLink identity to replace, and with what. The
    two keys are often one value, which then stays one."""
    new = {frame["deviceid"].encode(): dump.deviceid.encode(), frame["apikey"].encode(): dump.apikey.encode()}
    if frame.get("encrypt_key"):
        new.setdefault(frame["encrypt_key"].encode(), dump.apikey.replace("-4000-", "-4001-").encode())
    return new


# Of a donor's settings, what is not its own alone.
KEPT_SPACES = {"frame_params", "nvs.net80211", "phy", "misc", "data_backups", "user_usual", "user_unusual"}

def factory_record(update: Update) -> bytes:
    """The record a device of `update`'s kind would have been given at the
    factory (tools/espressif/ais_records.py), with the test copy's made-up
    identity."""
    octets = bytes.fromhex(update.mac.replace(":", ""))
    softap = octets[:5] + bytes([(octets[5] + 1) & 0xFF])  # ESP-IDF's softAP MAC: the base + 1
    return ais_records.factory_record({
        "chip_mac": octets.hex().upper(), "device_model": update.stock_model, "deviceid": update.deviceid,
        "factory_apikey": update.apikey, "sta_mac": ":".join(f"{b:02X}" for b in octets),
        "sap_mac": ":".join(f"{b:02X}" for b in softap)}, update.uiid)


# A paired device's home network, made up: rewrite_nvs points it at the emulator's.
HOME_WIFI = ("home-wifi", "made-up-password")


def ais_rows(update: Update) -> tuple[tuple[str, str, str, str], ...]:
    """The ais SDK's records of a device of `update`'s kind that has been made
    and paired, as rows for the NVS generator. The donor's settings, in
    another SDK's places, mean nothing to its firmware."""
    paired = ais_records.pair_record(*HOME_WIFI, *rewrite_nvs.DISPATCH)
    return (("factory", "namespace", "", ""), ("info", "data", "hex2bin", factory_record(update).hex()),
            ("pair", "namespace", "", ""), ("info", "data", "hex2bin", paired.hex()))


def anonymised(flash: bytes, dump: "Dump | Update", stock_model: str | None = None, uiid: int = 0,
               user_settings: str = "", added: tuple[tuple[str, str, str, str], ...] = ()) -> bytes:
    """The dump with the device's identity replaced throughout its settings - and,
    given `stock_model`, what it calls itself, given `uiid` its kind's number, and
    given `user_settings` those for its user settings. `added` are more settings,
    as rows for the NVS generator."""
    partition = flashmap.nvs(flash)
    settings = flash[partition.offset:partition.offset + partition.size]
    names, values = parse(settings)
    frame = next(json.loads(value.data.split(b"\0", 1)[0]) for (space, key), value in values.items()
                 if names[space] == "frame_params" and key == "content")
    replaced = identities(frame, dump)
    # The eWeLink account it was paired to, made up as the stub's is (the device's
    # own apikey). Kept, it is someone's account, and the stub's would differ
    # from it: the DualR3's firmware, seeing a new account, puts its user
    # settings back to their defaults.
    account = next((json.loads(value.data.split(b"\0", 1)[0]).get("finalUserApikey")
                    for (space, key), value in values.items()
                    if names[space] == "user_unusual" and key == "content"), None)
    if account:
        replaced.setdefault(account.encode(), dump.apikey.encode())
    if stock_model:
        replaced[frame["model"].encode()] = stock_model.encode()
    if uiid:
        replaced[f'"uiid":{frame["uiid"]}'.encode()] = f'"uiid":{uiid}'.encode()
    rows: list[tuple[str, str, str, str]] = []
    for space in sorted({space for space, _ in values}):
        if stock_model and names[space] not in KEPT_SPACES:
            continue
        rows.append((names[space], "namespace", "", ""))
        for (_, key), value in sorted(item for item in values.items() if item[0][0] == space):
            data = value.data
            if user_settings and (names[space], key) == ("user_usual", "content"):
                data = user_settings.encode()
            for old, new in replaced.items():
                data = data.replace(old, new)
            if names[space] == "nvs.net80211" and key in ("sta.mac", "ap.mac"):
                data = bytes.fromhex(dump.mac.replace(":", ""))
            if value.kind in rewrite_nvs.INTS:
                signed = value.kind & 0x10 != 0
                rows.append((key, "data", rewrite_nvs.INTS[value.kind],
                             str(int.from_bytes(data, "little", signed=signed))))
            elif value.kind == 0x21:
                rows.append((key, "data", "string", data.rstrip(b"\0").decode()))
            else:
                rows.append((key, "data", "hex2bin", data.hex()))
    new = rewrite_nvs.rebuild(rows + list(added), partition.size)
    out = bytearray(flash)
    out[partition.offset:partition.offset + partition.size] = new
    if any(old in out for old in replaced):
        # A copy somewhere else in the flash: rather no device than a real identity.
        for old, new_value in replaced.items():
            if len(old) != len(new_value):  # the model, from the settings: not an identity
                continue
            at = out.find(old)
            while at >= 0:
                out[at:at + len(old)] = new_value
                at = out.find(old, at + 1)
    return bytes(out)


# Where the S60's bootloader (ESP-IDF v4.4.2-dirty) says where its table is:
# c.lui of 0x8000, in load_partition_table's bootloader_mmap and in its error
# message. Each is the flash offset of the instruction and its register.
S60_BOOTLOADER_TABLE_AT = ((0x316A, 10), (0x3194, 13))  # a0, a3
S60_BOOTLOADER_SHA256 = "1b8ab8e5dbf1474c796bda4bc356c03fb22040e5eae63ed584359730bf1d72e1"


def c_lui(register: int, upper: int) -> bytes:
    """RISC-V's compressed lui, for an upper immediate of 1 to 31."""
    return (0x6001 | (register << 7) | (upper << 2)).to_bytes(2, "little")


def table_moved(flash: bytes, to: int) -> bytes:
    """An S60 flash as a device built for its partition table at `to` would have
    it: its bootloader reading it there and nothing where it was. The settings
    partition, which covers `to` on the S60, starts after it instead and keeps
    its end; nothing else moves. A real one's layout past the table is not
    known, so this is the least a table at `to` needs."""
    bootloader_end = appimage.parse(flash).length
    if hashlib.sha256(flash[:bootloader_end]).hexdigest() != S60_BOOTLOADER_SHA256:
        raise SystemExit("the donor's bootloader is not the S60's this knows how to move the table for")
    old = flashmap.table_at(flash)
    out = bytearray(flash)
    for at, register in S60_BOOTLOADER_TABLE_AT:
        if out[at:at + 2] != c_lui(register, old >> 12):
            raise SystemExit(f"the S60's bootloader does not read its table at 0x{old:x}")
        out[at:at + 2] = c_lui(register, to >> 12)
    boot = bytearray(out[:bootloader_end])
    appimage.reseal(boot, appimage.parse(boot))
    out[:bootloader_end] = boot

    settings = flashmap.nvs(flash)
    start, end = to + flashmap.TABLE_SIZE, settings.offset + settings.size
    if not settings.offset <= to < end:
        raise SystemExit(f"0x{to:x} is not in the settings partition, which this moves out of its way")
    kept = flash[settings.offset:settings.offset + end - start]
    if flash[settings.offset + len(kept):end].count(0xFF) != end - settings.offset - len(kept):
        raise SystemExit("the settings do not fit after the moved table")
    entries = b""
    for at in range(old, old + flashmap.TABLE_SIZE, flashmap.ENTRY_SIZE):
        entry = bytearray(flash[at:at + flashmap.ENTRY_SIZE])
        if entry[:2] != flashmap.ENTRY_MAGIC:
            break
        if struct.unpack_from("<I", entry, 4)[0] == settings.offset:
            struct.pack_into("<II", entry, 4, start, end - start)
        entries += entry
    table = entries + b"\xeb\xeb" + b"\xff" * 14 + hashlib.md5(entries).digest()
    out[old:old + flashmap.TABLE_SIZE] = b"\xff" * flashmap.TABLE_SIZE
    out[settings.offset:end] = b"\xff" * (end - settings.offset)
    out[to:to + flashmap.TABLE_SIZE] = table.ljust(flashmap.TABLE_SIZE, b"\xff")
    out[start:end] = kept
    return bytes(out)


def with_update(donor: bytes, update: Update) -> bytes:
    """The donor device's flash with the update's firmware in place of its own,
    and started."""
    firmware = fetched(update)
    app = firmware[build_vendor_ota.WRAPPER_SIZE:]
    added = ais_rows(update) if update.ais_sdk else ()
    flash = bytearray(anonymised(donor, update, update.stock_model, update.uiid, update.settings, added))
    if update.table_at != flashmap.table_at(flash):
        flash = bytearray(table_moved(flash, update.table_at))
    first, second = flashmap.app_slots(flash)[:2]
    if len(app) > first.size:
        raise SystemExit(f"{update.file} does not fit the donor's slot")
    flash[first.offset:first.offset + first.size] = app.ljust(first.size, b"\xff")
    flash[second.offset:second.offset + second.size] = b"\xff" * second.size
    otadata = next(p for p in flashmap.partitions(flash) if p.type == flashmap.TYPE_DATA and p.subtype == 0)
    flash[otadata.offset:otadata.offset + otadata.size] = b"\xff" * otadata.size
    return bytes(flash)


def built(dump: "Dump | Update") -> tuple[bytes, bytes]:
    """The test copy's flash and eFuses, made from the fetched dump: the same
    bytes each time, for the same recipe."""
    if isinstance(dump, Update):
        donor = S60_DUMP if dump.donor == "s60" else DUMPS[dump.donor]
        flash = with_update(fetched(donor), dump)
    else:
        flash = anonymised(fetched(dump), dump)
    if len(flash) != FLASH_SIZE:
        raise SystemExit(f"{dump.url} is not a whole flash")
    # An emulated C3 is the revision its firmware needs, or its bootloader stops there.
    efuse = flashmap.esp32_efuse(dump.mac) if flashmap.chip_of(flash) == "esp32" else flashmap.esp32c3_efuse(
        dump.mac, flashmap.min_chip_revision(flash, flashmap.app_slots(flash)[0]))
    return flash, efuse


def make(key: str) -> Path:
    """The kind's test copy, made again where one made before differs from what
    this makes now: an earlier recipe's, which its firmware may not start on."""
    dump = DUMPS.get(key) or UPDATES[key]
    target = records.DEVICES / records.folder_name(dump.mac)
    flash, efuse = built(dump)
    backup = target / "sonoff-backup"
    if target.exists():
        try:
            if (backup / "flash.bin").read_bytes() == flash and (backup / "efuse.bin").read_bytes() == efuse:
                return target
        except OSError:
            pass
        copy_device.remove(dump.mac)  # a real device's backup is left alone: it stops here
        print(f"{target} was made by an earlier version of this; it is made again "
              "(emu.sh --fresh puts an emulator that ran it on the new one).", file=sys.stderr)
    backup.mkdir(parents=True, mode=0o700)
    report = {"note": f"From the public dump {dump.url}, its identity made up."}
    (backup / "flash.bin").write_bytes(flash)
    (backup / "efuse.bin").write_bytes(efuse)
    (backup / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (backup / "README.txt").write_text(f"{report['note']}\nA test copy, for the emulator.\n", encoding="utf-8")
    (target / "device.json").write_text(json.dumps({
        "backed_up": time.strftime("%Y-%m-%d"), "ewelink_name": "Emulator test copy",
        "mac": dump.mac, "stock_version": "", "test_copy_of": dump.url,
        "product_model": kinds.find(key).example_product_model if kinds.find(key) else "",
    }, indent=2) + "\n", encoding="utf-8")
    for path in backup.iterdir():
        path.chmod(0o400)
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("kind", choices=sorted({*DUMPS, *UPDATES}))
    parser.add_argument("--remove", action="store_true", help="delete the test copy")
    args = parser.parse_args(argv)
    try:
        records.take_over_earlier_names()
    except records.EarlierNames as error:
        sys.exit(str(error))
    mac = (DUMPS.get(args.kind) or UPDATES[args.kind]).mac
    if args.remove:
        copy_device.remove(mac)
        print(f"Removed the test copy of {args.kind}.")
        return 0
    print(f"{make(args.kind)}: run it with tools/espressif/emulator/emu.sh --device {mac}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
