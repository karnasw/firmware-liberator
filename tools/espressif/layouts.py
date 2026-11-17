"""The partition tables Sonoff's firmware comes on, and where each keeps things.

Every plan and every Berry program that writes a device's flash takes its
addresses from here, so a device laid out differently is one more entry, checked
by the same steps. A table not listed stops a run while the device can still go
back to Sonoff's firmware (installer.require_known_layout).
"""

from __future__ import annotations

import hashlib
import struct
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
TABLE_ENTRY_SIZE = 32
# Where a device leaving Tasmota takes the mover: in front of Tasmota's app, over
# its safeboot, which is all that has room. A layout whose ota_0 is not here
# leaves through a temporary table with it here (Layout.temporary_table).
MOVER_AT = 0x20000
TEMPORARY_OTADATA = 0x1D000
TEMPORARY_PHY = 0x1F000


@dataclass(frozen=True)
class Layout:
    key: str
    table_file: str      # as published in artifacts/
    table_sha256: str
    interim_sha256: str  # safeboot_migration.interim_table's, reviewed
    ota: tuple[int, int]
    slot_size: int
    nvs: int
    otadata: int
    phy: int
    temporary_sha256: str = ""  # temporary_table's, reviewed; for a layout that needs one

    @property
    def needs_temporary(self) -> bool:
        return self.ota[0] != MOVER_AT

    @property
    def leaving_sha256(self) -> str:
        """The table a device on Tasmota leaves for (temporary_table says why)."""
        return self.temporary_sha256 if self.needs_temporary else self.table_sha256

    @property
    def low_size(self) -> int:
        """Settings, boot selection and PHY data: all that sits before ota_0."""
        return self.ota[0] - self.nvs

    @property
    def moved_low(self) -> int:
        """Where a commit keeps a copy of them: the end of what ota_0 can reach."""
        return self.ota[1] - self.low_size

    @property
    def table(self) -> bytes:
        return (ROOT / self.table_file).read_bytes()

    def partitions(self, table: bytes | None = None) -> tuple[tuple[str, int, int, int, int, int], ...]:
        """Its table's entries, as safeboot_migration.partition_tuple gives them."""
        table = self.table if table is None else table
        rows = []
        for at in range(0, len(table), TABLE_ENTRY_SIZE):
            if table[at:at + 2] != b"\xaa\x50":
                break
            offset, size = struct.unpack_from("<II", table, at + 4)
            flags = struct.unpack_from("<I", table, at + 28)[0]
            rows.append((table[at + 12:at + 28].split(b"\0", 1)[0].decode("ascii"), table[at + 2], table[at + 3],
                         offset, size, flags))
        return tuple(rows)

    def interim_partitions(self) -> tuple[tuple[str, int, int, int, int, int], ...]:
        """Its table with what sits before ota_0 moved to `moved_low`, and ota_0
        short of that (safeboot_migration says why)."""
        rows = []
        for label, kind, subtype, offset, size, flags in self.partitions():
            if offset < self.ota[0]:
                offset = self.moved_low + offset - self.nvs
            elif label == "ota_0":
                size = self.moved_low - self.ota[0] - 0x9000
            rows.append((label, kind, subtype, offset, size, flags))
        return tuple(rows)

    def interim_table(self) -> bytes:
        """The table with those entries, its MD5 made again."""
        return self._table_of(self.interim_partitions())

    def temporary_partitions(self) -> tuple[tuple[str, int, int, int, int, int], ...]:
        """Its table with ota_0 where the mover goes, up to ota_1, which stays where
        it is, and the settings and boot selection in what is left in front of it."""
        where = {"nvs": (self.nvs, TEMPORARY_OTADATA - self.nvs),
                 "otadata": (TEMPORARY_OTADATA, TEMPORARY_PHY - TEMPORARY_OTADATA),
                 "phy_init": (TEMPORARY_PHY, MOVER_AT - TEMPORARY_PHY),
                 "ota_0": (MOVER_AT, self.ota[1] - MOVER_AT)}
        return tuple((label, kind, subtype, *where.get(label, (offset, size)), flags)
                     for label, kind, subtype, offset, size, flags in self.partitions())

    def temporary_table(self) -> bytes:
        """What a device leaving Tasmota has between Tasmota's table and its own, when
        the mover does not fit at its own ota_0, as Tasmota's app starts too soon
        after it: it then runs as a device with its ota_0 at MOVER_AT, until the
        Tasmota of the hub writes its own table (plans.own_table_from_temporary)."""
        return self._table_of(self.temporary_partitions())

    def _table_of(self, rows: tuple[tuple[str, int, int, int, int, int], ...]) -> bytes:
        table = bytearray(self.table)
        for index, (_, _, _, offset, size, _) in enumerate(rows):
            struct.pack_into("<II", table, index * TABLE_ENTRY_SIZE + 4, offset, size)
        md5_at = len(rows) * TABLE_ENTRY_SIZE
        table[md5_at + 16:md5_at + 32] = hashlib.md5(table[:md5_at]).digest()
        return bytes(table)


SONOFF_DUAL_OTA = Layout(
    "sonoff-dual-ota", "artifacts/sonoff-s60-partition-table.bin",
    "f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf",
    "cfb1e1c46fa209a6c1eada92123a02847ab8e72f67c0044afbcb1c29ead4c936",
    (0x20000, 0x210000), 0x1F0000, 0x9000, 0x1D000, 0x1F000)
# As the S60's, but for the unused "reserve" entry it leaves out.
SONOFF_NSPANEL = Layout(
    "sonoff-nspanel", "artifacts/sonoff-nspanel-partition-table.bin",
    "3c6436bcbd529015fcb3fd98565b7753199910b3dba30c9d6ccce77d08e09d70",
    "c8d033c1da0f25fb2df05784e0a97986c8748f6907a644638fa03009ca1a26d6",
    (0x20000, 0x210000), 0x1F0000, 0x9000, 0x1D000, 0x1F000)
# A larger NVS, for its history of readings, and smaller app slots: an interim
# Tasmota must be one that fits (the Layout's slot_size says), and the mover
# does not fit between its ota_0 and Tasmota's app, so a device on Tasmota leaves
# through a temporary table.
SONOFF_TH_ELITE = Layout(
    "sonoff-th-elite", "artifacts/sonoff-th-elite-partition-table.bin",
    "36d7dc76c5d2459ea174e48aaa19ff93dcb8130c43b124bba409d78b9c01c1b6",
    "0fc13093b8e5ea70ffd09aa04af510dad5b1aa0dece1f56e16abf4e809398865",
    (0x60000, 0x210000), 0x1A9000, 0x9000, 0x49000, 0x4B000,
    "ae0503897ebe1598242f35c51938f76d5fce70e651a88d2d2db3097eabbf1b19")
ALL = (SONOFF_DUAL_OTA, SONOFF_NSPANEL, SONOFF_TH_ELITE)


def of(key: str) -> Layout:
    for layout in ALL:
        if layout.key == key:
            return layout
    raise ValueError(f"no such layout: {key}")


def by_table(sha256: str) -> Layout | None:
    return next((layout for layout in ALL if layout.table_sha256 == sha256), None)
