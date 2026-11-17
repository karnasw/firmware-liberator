#!/usr/bin/env python3
"""The partition tables Beken devices' stock firmware comes on, read from the
firmware itself.

Each BK7238 eWeLink app holds the SDK's table, as entries of five words: owner,
description (a pointer to its name), start, length, options. A run refuses a
device whose official update file holds a table not listed here before anything
is written (routes/beken/images.py): everything the conversion does, from where
the update lands to where the device's own data is read, follows from this table.

The flash size is not asked of the stock firmware, as it says nothing of it
(tools/beken/README.md): the table is the only word on it before a conversion.

    python3 tools/beken/layouts.py FILE...
"""
from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

APP_BASE = 0x10000
ENTRY = 20
# Where a table's walk stops being one: no more entries than this.
LONGEST_TABLE = 32
# An entry's owner: the SDK's flash owners are few.
OWNERS = 8
# What each is found by, and must have (Application's and the rest's names, as
# the SDK spells them: "ota" in lower case).
KNOWN_NAMES = (b"Application", b"ota", b"RF Firmware", b"NET info", b"NVS")


class LayoutError(ValueError):
    """The file holds no table, more than one, or one not known here."""


@dataclass(frozen=True)
class Partition:
    description: str
    start: int
    length: int

    def __str__(self) -> str:
        return f"{self.description} 0x{self.start:x}+0x{self.length:x}"


@dataclass(frozen=True)
class Layout:
    key: str
    flash_size: int
    # What the conversion depends on: description to (start, length).
    partitions: tuple[tuple[str, int, int], ...]

    def matches(self, table: tuple[Partition, ...]) -> bool:
        found = {(part.description.lower(), part.start, part.length) for part in table}
        return all((name.lower(), start, length) in found for name, start, length in self.partitions)


# The SDK's 4 MB table, as the S61, BASIC-1GS, BASIC-1GSP, S41 and T6's official
# files all hold it: the download slot the stock bootloader applies an update
# from, the radio calibration, OpenBeken's settings, and the stock store.
BEKEN_4M = Layout("beken-4m", 0x400000, (
    ("Application", 0x11000, 0x250000),
    ("ota", 0x286000, 0x15A000),
    ("RF Firmware", 0x3E0000, 0x1000),
    ("NET info", 0x3E1000, 0x1000),
    ("NVS", 0x3E2000, 0xB000),
))
ALL = (BEKEN_4M,)


def of(key: str) -> Layout:
    for layout in ALL:
        if layout.key == key:
            return layout
    raise ValueError(f"no such layout: {key}")


def _string_at(app: bytes, address: int, base: int) -> bytes | None:
    if not base <= address < base + len(app):
        return None
    raw = app[address - base:address - base + 40]
    end = raw.find(0)
    return raw[:end] if end > 0 else None


def _entry(app: bytes, at: int, base: int) -> Partition | None:
    """The entry at `at` in `app`, if it looks like one: a small owner, and a
    description that is printable text."""
    if at < 0 or at + ENTRY > len(app):
        return None
    owner, description, start, length, _options = struct.unpack_from("<5I", app, at)
    text = _string_at(app, description, base)
    if owner >= OWNERS or not text or not all(32 <= c < 127 for c in text) or not length:
        return None
    return Partition(text.decode("ascii"), start, length)


def tables(app: bytes, base: int = APP_BASE) -> list[tuple[Partition, ...]]:
    """Every table in an app, each found from an entry whose description is one
    of KNOWN_NAMES: a word pointing at the name, in an entry, walked both ways."""
    found: list[tuple[Partition, ...]] = []
    seen: set[int] = set()
    for name in KNOWN_NAMES:
        needle, at = name + b"\0", app.find(name + b"\0")
        while at >= 0:
            # A name ending another string is not this name.
            if at == 0 or app[at - 1] == 0:
                word = struct.pack("<I", base + at)
                ref = app.find(word)
                while ref >= 0:
                    first = ref - 4
                    if first not in seen and _entry(app, first, base):
                        while _entry(app, first - ENTRY, base) and ref - first < LONGEST_TABLE * ENTRY:
                            first -= ENTRY
                        table, walk = [], first
                        while (entry := _entry(app, walk, base)) and len(table) < LONGEST_TABLE:
                            seen.add(walk)
                            table.append(entry)
                            walk += ENTRY
                        if tuple(table) not in found:
                            found.append(tuple(table))
                    ref = app.find(word, ref + 1)
            at = app.find(needle, at + 1)
    return found


def table_of(app: bytes, base: int = APP_BASE) -> tuple[Partition, ...]:
    """The one table in an app. No table, or two that differ, is an error: the
    SDK could then choose either, and this could not say which."""
    # Short walks are other structures that happen to name a string like these.
    found = [table for table in tables(app, base) if len(table) >= 3]
    if not found:
        raise LayoutError("no partition table found in it")
    if len(found) > 1:
        raise LayoutError("it holds %d partition tables: %s" % (
            len(found), " / ".join(", ".join(map(str, table)) for table in found)))
    return found[0]


def known(app: bytes, base: int = APP_BASE) -> tuple[Layout, tuple[Partition, ...]]:
    """The layout of an app, and its table as read; a LayoutError for one not listed."""
    table = table_of(app, base)
    for layout in ALL:
        if layout.matches(table):
            return layout, table
    raise LayoutError("its partition table is not one this tool knows: " + ", ".join(map(str, table)))


def main(argv: list[str] | None = None) -> int:
    from tools.beken import stock_points
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    failed = 0
    for path in args.files:
        print(path.name)
        try:
            layout, table = known(stock_points.app_of_file(path.read_bytes()))
            print(f"  {layout.key}, {layout.flash_size // 0x100000} MB")
            for part in table:
                print(f"  {part}")
        except (LayoutError, ValueError) as exc:
            failed += 1
            print(f"  {exc}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
