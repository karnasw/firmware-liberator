"""An ESP-IDF NVS partition, written as ESP-IDF's nvs_partition_gen writes one
(format version 2), with Python's standard library alone: the tool builds a
device's settings with it where it has no copy of them to write back.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass

PAGE = 4096
ENTRY = 32
ENTRIES = 126
FIRST_ENTRY = 64
ACTIVE = 0xFFFFFFFE
FULL = 0xFFFFFFFC
VERSION2 = 0xFE
ANY_CHUNK = 0xFF
INTS = {"u8": (0x01, "<B"), "i8": (0x11, "<b"), "u16": (0x02, "<H"), "i16": (0x12, "<h"),
        "u32": (0x04, "<I"), "i32": (0x14, "<i"), "u64": (0x08, "<Q"), "i64": (0x18, "<q")}
STRING, BLOB_DATA, BLOB_INDEX = 0x21, 0x42, 0x48
# The longest string one page holds; a blob is split across pages.
LONGEST_STRING = 4000


class NvsError(ValueError):
    pass


@dataclass(frozen=True)
class Item:
    """One value: `kind` is "string" (its bytes, without the NUL ESP-IDF adds),
    "blob", or one of INTS, whose bytes are the little-endian value."""
    namespace: str
    key: str
    kind: str
    data: bytes


def crc(data: bytes) -> int:
    return zlib.crc32(data, 0xFFFFFFFF) & 0xFFFFFFFF


class Writer:
    def __init__(self, size: int) -> None:
        if size % PAGE or size < 2 * PAGE:
            raise NvsError("an NVS partition is two pages or more, in whole pages")
        # The last page is kept free, as ESP-IDF keeps one for moving entries.
        self.pages: list[bytearray] = []
        self.room = size // PAGE - 1
        self.used = ENTRIES
        self.namespaces: dict[str, int] = {}

    def new_page(self) -> None:
        if self.pages:
            struct.pack_into("<I", self.pages[-1], 0, FULL)
        if len(self.pages) == self.room:
            raise NvsError("the values do not fit the partition")
        page = bytearray(b"\xff" * PAGE)
        header = bytearray(b"\xff" * 32)
        struct.pack_into("<II", header, 0, ACTIVE, len(self.pages))
        header[8] = VERSION2
        struct.pack_into("<I", header, 28, crc(bytes(header[4:28])))
        page[:32] = header
        self.pages.append(page)
        self.used = 0

    def put(self, entry: bytearray, data: bytes = b"") -> None:
        """An entry and the data that follows it, in the page being filled."""
        struct.pack_into("<I", entry, 4, crc(bytes(entry[:4] + entry[8:32])))
        page = self.pages[-1]
        span = 1 + (len(data) + ENTRY - 1) // ENTRY
        at = FIRST_ENTRY + self.used * ENTRY
        page[at:at + ENTRY] = entry
        page[at + ENTRY:at + ENTRY + len(data)] = data
        for index in range(self.used, self.used + span):
            page[32 + index // 4] &= ~(1 << (index % 4 * 2)) & 0xFF
        self.used += span

    def room_for(self, span: int) -> None:
        if not self.pages or self.used + span > ENTRIES:
            self.new_page()

    @staticmethod
    def entry(namespace: int, kind: int, span: int, chunk: int, key: str) -> bytearray:
        name = key.encode("ascii")
        if not 0 < len(name) <= 15:
            raise NvsError(f"an NVS key is 1 to 15 characters: {key!r}")
        entry = bytearray(b"\xff" * ENTRY)
        entry[0:4] = bytes((namespace, kind, span, chunk))
        entry[8:24] = name.ljust(16, b"\0")
        return entry

    def namespace(self, name: str) -> int:
        if name not in self.namespaces:
            index = len(self.namespaces) + 1
            self.room_for(1)
            entry = self.entry(0, INTS["u8"][0], 1, ANY_CHUNK, name)
            entry[24] = index
            self.put(entry)
            self.namespaces[name] = index
        return self.namespaces[name]

    def add(self, item: Item) -> None:
        space = self.namespace(item.namespace)
        if item.kind in INTS:
            kind, form = INTS[item.kind]
            if len(item.data) != struct.calcsize(form):
                raise NvsError(f"{item.key} is not a {item.kind}")
            self.room_for(1)
            entry = self.entry(space, kind, 1, ANY_CHUNK, item.key)
            entry[24:24 + len(item.data)] = item.data
            self.put(entry)
        elif item.kind == "string":
            data = item.data + b"\0"
            if len(data) > LONGEST_STRING:
                raise NvsError(f"{item.key} is longer than a string can be")
            span = 1 + (len(data) + ENTRY - 1) // ENTRY
            if self.pages and self.used + span >= ENTRIES:
                self.new_page()
            self.room_for(span)
            entry = self.entry(space, STRING, span, ANY_CHUNK, item.key)
            struct.pack_into("<HHI", entry, 24, len(data), 0xFFFF, crc(data))
            self.put(entry, data)
        elif item.kind == "blob":
            self.blob(space, item)
        else:
            raise NvsError(f"no such NVS type: {item.kind}")

    def blob(self, space: int, item: Item) -> None:
        """Version 2's blob: its data in chunks, as many pages as it takes, then
        an index entry saying how many. As nvs_partition_gen does, a page with
        one entry left takes a chunk of nothing."""
        self.room_for(1)
        offset, chunks = 0, 0
        while True:
            tail = (ENTRIES - self.used - 1) * ENTRY
            chunk = item.data[offset:offset + tail]
            entry = self.entry(space, BLOB_DATA, 1 + (len(chunk) + ENTRY - 1) // ENTRY, chunks, item.key)
            struct.pack_into("<HHI", entry, 24, len(chunk), 0xFFFF, crc(chunk))
            self.put(entry, chunk)
            chunks += 1
            offset += len(chunk)
            if offset < len(item.data) or tail - len(chunk) < ENTRY:
                self.new_page()
            if offset >= len(item.data):
                break
        index = self.entry(space, BLOB_INDEX, 1, ANY_CHUNK, item.key)
        struct.pack_into("<IBB", index, 24, len(item.data), chunks, 0)
        self.put(index)


def build(items: list[Item] | tuple[Item, ...], size: int) -> bytes:
    """The partition of `size` bytes holding `items`, its pages unused erased."""
    writer = Writer(size)
    writer.new_page()
    for item in items:
        writer.add(item)
    image = b"".join(writer.pages)
    return image + b"\xff" * (size - len(image))


def used(image: bytes) -> bytes:
    """`image` without its erased pages at the end: all a plan has to write."""
    end = len(image)
    while end >= PAGE and image[end - PAGE:end] == b"\xff" * PAGE:
        end -= PAGE
    return image[:end]
