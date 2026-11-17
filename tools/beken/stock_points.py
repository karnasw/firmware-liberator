#!/usr/bin/env python3
"""Find the places in a BK7238 eWeLink app where the emulator has to help it.

The stock firmware needs a few things of the emulator to come online
(cloud_online.StockHooks), each at a place in its code, and those places move
with every build. So they are found in the app itself, by what the code there
refers to, as patch_stock.py finds Bluetooth's start-up on an ESP32. A point
that cannot be found, or that more than one place could be, is an error naming
it: never a guess.

    python3 tools/beken/stock_points.py FILE...

FILE is a whole flash, an official update file (a FOTA container or an RBL) or
an app on its own. The app runs at APP_BASE, in Thumb: the BK7238 is an ARM968,
so every instruction is 16 bits but BL and BLX. Standard library only: this
runs outside the emulator's container too.
"""
from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, fields
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.beken import rbl, rebuild_dump, stock_app  # noqa: E402

APP_BASE = 0x10000
RAM = (0x400000, 0x480000)
# As far back as the start of a function is looked for: the longest here is about 2 KiB.
LONGEST_FUNCTION = 0x4000
# How far before its push a function can start.
BEFORE_PUSH = 8
# How many instructions an argument's set-up takes, at most, before its call.
ARGUMENT_REACH = 12
# ldr rY, [rX] and ldrb rY, [rX], with neither register filled in.
LDR_THROUGH, LDRB_THROUGH = 0x6800, 0x7800
# How far apart one source file's flags can be in RAM.
FLAGS_REACH = 0x10

NAME_LINK_TASK = b"ais_link_task\0"
NAME_APPFW_TASK = b"ais_appfw_task\0"
ENV_NOT_READY = b"ENV isn't initialize OK.\n\0"   # EasyFlash's own log line
PEM_BEGIN = b"-----BEGIN CERTIFICATE-----\0"
PEM_END = b"-----END CERTIFICATE-----\0"
# The device's identity is put together with this, hashed, and compared with
# the 32-byte hash its factory data holds: the flash-UID integrity gate.
IDENTITY_FORMAT = b"%s%s%s%d%s%s\0"


class PointError(ValueError):
    """A point could not be found, or not told from another."""

    def __init__(self, point: str, why: str):
        super().__init__(f"{point}: {why}")
        self.point = point


@dataclass(frozen=True)
class Points:
    after_memcmp: int     # just after the integrity gate's compare: r0 there is its result
    gate_passed: int      # where the gate's compare goes when it says equal
    ef_get_env_blob: int  # EasyFlash's read (key, buffer, size, &length)
    x509_crt_parse: int   # mbedtls_x509_crt_parse (chain, PEM, length)
    appfw_task: int       # ais_appfw_task's function
    appfw_loop: int       # the head of its loop: runs often, in that task
    link_task: int        # ais_link_task's function
    link_create: int      # the call that creates it
    ais_cloud_start: int  # the function that makes that call
    start_cloud: int      # what pairing success calls: checks cloud_flag, then ais_cloud_start
    lan_publish: int      # what it calls just before: the LAN service's mDNS record
    ctx_pointer: int      # RAM: the pointer to the AIS cloud context
    cloud_flag: int       # RAM: the byte saying the cloud is set up
    dispatch_url: str     # the URL the firmware dials first, as it has it
    url_offset: int       # its place in that context


class Code:
    """An app, read as Thumb: where words are loaded and functions are called."""

    def __init__(self, app: bytes, base: int = APP_BASE):
        self.app, self.base = app, base
        self.end = base + len(app)
        self.loads: dict[int, list[int]] = {}
        self.calls: dict[int, list[int]] = {}
        self.jumps: set[int] = set()
        halves = struct.unpack("<%dH" % (len(app) // 2), app[:len(app) // 2 * 2])
        for index, half in enumerate(halves):
            at = base + index * 2
            if half & 0xF800 == 0x4800:   # ldr rX, [pc, #imm]
                word = self.word(((at + 4) & ~3) + (half & 0xFF) * 4)
                if word is not None:
                    self.loads.setdefault(word, []).append(at)
            elif half & 0xF800 == 0xF000 and index + 1 < len(halves) and halves[index + 1] & 0xE800 == 0xE800:
                self.calls.setdefault(self.call_target(at), []).append(at)
            elif half & 0xF800 == 0xE000:   # b: a tail call is a function's only reference sometimes
                self.jumps.add(at + 4 + ((half & 0x7FF) - (0x800 if half & 0x400 else 0)) * 2)
        self.words = set(struct.unpack("<%dI" % (len(app) // 4), app[:len(app) // 4 * 4]))

    def half(self, address: int) -> int | None:
        if self.base <= address and address + 2 <= self.end:
            return struct.unpack_from("<H", self.app, address - self.base)[0]
        return None

    def word(self, address: int) -> int | None:
        if self.base <= address and address + 4 <= self.end:
            return struct.unpack_from("<I", self.app, address - self.base)[0]
        return None

    def is_call(self, address: int) -> bool:
        first, second = self.half(address), self.half(address + 2)
        return first is not None and second is not None and first & 0xF800 == 0xF000 and second & 0xE800 == 0xE800

    def call_target(self, address: int) -> int:
        first, second = self.half(address), self.half(address + 2)
        offset = ((first & 0x7FF) << 12) | ((second & 0x7FF) << 1)
        if offset & 0x400000:
            offset -= 0x800000
        target = address + 4 + offset
        return target if second & 0xF800 == 0xF800 else target & ~3   # BLX goes to a word boundary

    def strings(self, text: bytes) -> list[int]:
        found, at = [], self.app.find(text)
        while at >= 0:
            found.append(self.base + at)
            at = self.app.find(text, at + 1)
        return found

    def loads_of_string(self, text: bytes) -> list[int]:
        return sorted(load for at in self.strings(text) for load in self.loads.get(at, ()))

    def literal(self, address: int) -> tuple[int, int] | None:
        """(register, word) if a pc-relative load is at `address`."""
        half = self.half(address)
        if half is None or half & 0xF800 != 0x4800:
            return None
        word = self.word(((address + 4) & ~3) + (half & 0xFF) * 4)
        return None if word is None else ((half >> 8) & 7, word)

    def string_at(self, address: int, longest: int = 80) -> bytes | None:
        if not self.base <= address < self.end:
            return None
        raw = self.app[address - self.base:address - self.base + longest]
        end = raw.find(0)
        return raw[:end] if end >= 0 else None

    def function_of(self, address: int) -> int | None:
        """The start of the function holding `address`: found by the nearest
        `push {.., lr}` before it with something calling, jumping to or holding
        the address of it or of a few instructions before it, where the compiler
        has moved an argument's set-up. So data looking like a push is passed over."""
        at = address & ~1
        while at >= self.base and address - at < LONGEST_FUNCTION:
            if self.half(at) & 0xFF00 == 0xB500:
                for entry in range(at, at - BEFORE_PUSH - 2, -2):
                    if entry in self.calls or entry in self.jumps or entry | 1 in self.words:
                        return entry
            at -= 2
        return None

    def walk(self, start: int, count: int):
        """Up to `count` instructions from `start`: (address, half), a BL as one."""
        at = start
        for _ in range(count):
            half = self.half(at)
            if half is None:
                return
            yield at, half
            at += 4 if self.is_call(at) else 2

    def functions_loading(self, text: bytes) -> list[int]:
        return sorted({f for f in map(self.function_of, self.loads_of_string(text)) if f is not None})


def _one(point: str, found, nothing: str) -> int:
    found = sorted(set(found))
    if not found:
        raise PointError(point, nothing)
    if len(found) > 1:
        raise PointError(point, "more than one place could be it: %s" % ", ".join("%#x" % f for f in found))
    return found[0]


def _x509_crt_parse(code: Code) -> int:
    both = set(code.functions_loading(PEM_BEGIN)) & set(code.functions_loading(PEM_END))
    return _one("x509_crt_parse", both, "no function holds both of the PEM certificate markers")


def _ef_get_env_blob(code: Code) -> int:
    """Of the EasyFlash functions that log it is not ready, the only one of four
    arguments keeps r3 as it starts: its others take fewer."""
    def keeps_r3(function: int) -> bool:
        return any(half & 0xFFF8 == 0x0018 for _, half in code.walk(function + 2, 4))   # movs rX, r3
    functions = code.functions_loading(ENV_NOT_READY)
    if not functions:
        raise PointError("ef_get_env_blob", "no function logs EasyFlash's not-ready line")
    return _one("ef_get_env_blob", filter(keeps_r3, functions),
                "none of the functions logging EasyFlash's not-ready line takes four arguments")


def _integrity_gate(code: Code) -> tuple[int, int]:
    """After the identity is formatted, a compare of 32 bytes (r2) whose result
    is tested at once: (just after it, where it goes when equal)."""
    found = []
    for load in code.loads_of_string(IDENTITY_FORMAT):
        size_set = False
        for at, half in code.walk(load, 24):
            size_set = size_set or half == 0x2220   # movs r2, #0x20
            if size_set and code.is_call(at):
                test, branch = code.half(at + 4), code.half(at + 6)
                if test == 0x2800 and branch is not None and branch & 0xFF00 == 0xD000:   # cmp r0, #0; beq
                    offset = (branch & 0xFF) - (0x100 if branch & 0x80 else 0)
                    found.append((at + 4, at + 6 + 4 + offset * 2))
                break
    if not found:
        raise PointError("after_memcmp", "no 32-byte compare follows the identity's format string")
    if len(set(found)) > 1:
        raise PointError("after_memcmp", "more than one compare follows it: %s"
                         % ", ".join("%#x" % f[0] for f in found))
    return found[0]


def _task_created(code: Code, point: str, name: bytes) -> tuple[int, int]:
    """Where a task of this name is created, by the one load of its name:
    (the task's function, as r0 is loaded before the call; the call)."""
    load = _one(point, code.loads_of_string(name), "the task's name is not used, or is not in this firmware")
    entry = None
    for at, _ in code.walk(load, ARGUMENT_REACH):
        loaded = code.literal(at)
        if loaded and loaded[0] == 0:
            entry = loaded[1]
        if code.is_call(at):
            if entry is None or not entry & 1 or not code.base <= entry < code.end:
                raise PointError(point, "the call creating the task at %#x has no function in r0" % at)
            return entry & ~1, at
    raise PointError(point, "no call follows the load of the task's name at %#x" % load)


def _loop_head(code: Code, task: int) -> int:
    """Where a task's loop starts: the target of the first branch back."""
    for at, half in code.walk(task, LONGEST_FUNCTION // 2):
        if half & 0xF800 == 0xE000:   # b
            offset = (half & 0x7FF) - (0x800 if half & 0x400 else 0)
            target = at + 4 + offset * 2
            if task <= target < at:
                return target
    raise PointError("appfw_loop", "no branch back in the task at %#x" % task)


def _first_read(code: Code, function: int, point: str, through: int) -> int:
    """The first word in RAM that `function` loads into a register and at once
    reads through, by `through` (ldr or ldrb rY, [rX])."""
    for at, _ in code.walk(function, 8):
        loaded = code.literal(at)
        if loaded and RAM[0] <= loaded[1] < RAM[1]:
            if any(half & 0xFFF8 == through | loaded[0] << 3 for _, half in code.walk(at + 2, 3)):
                return loaded[1]
    raise PointError(point, "the function at %#x does not start by reading a word in RAM" % function)


def _lan_publish(code: Code, start_cloud: int, flag: int) -> int:
    """Called straight before start_cloud. Another call can be, so it must also
    be of start_cloud's source file: it starts by reading a flag of that file's,
    which the linker puts beside cloud_flag."""
    def beside_the_flag(function: int) -> bool:
        try:
            return abs(_first_read(code, function, "lan_publish", LDRB_THROUGH) - flag) < FLAGS_REACH
        except PointError:
            return False
    called = {code.call_target(at - 4) for at in code.calls.get(start_cloud, ()) if code.is_call(at - 4)}
    if not called:
        raise PointError("lan_publish", "no call comes straight before one to start_cloud")
    return _one("lan_publish", filter(beside_the_flag, called),
                "none of what is called before start_cloud starts by reading a flag beside cloud_flag: %s"
                % ", ".join("%#x" % c for c in sorted(called)))


def _dispatch_url(code: Code, flag: int) -> tuple[str, int]:
    """The factory dispatch URL, and its place in the context: the function that
    sets the cloud up checks cloud_flag and copies the context's fields to a
    structure on its stack, whose first field is its first `add r0, sp, #imm`."""
    found = []
    for function in sorted({code.function_of(load) for load in code.loads.get(flag, ())} - {None}):
        for at, _ in code.walk(function, 0x100):
            loaded = code.literal(at)
            text = code.string_at(loaded[1]) if loaded else None
            if text and text.startswith(b"http://") and b"%" not in text:
                found.append((function, at, text.decode("ascii", "replace")))
    if not found:
        raise PointError("dispatch_url", "no function checking cloud_flag loads a plain http:// URL")
    if len({f[1] for f in found}) > 1:
        raise PointError("dispatch_url", "more than one could be it: %s" % ", ".join("%#x" % f[1] for f in found))
    function, load, url = found[0]
    base = next(((half & 0xFF) * 4 for _, half in code.walk(function, 32) if half & 0xFF00 == 0xA800), None)
    before = [code.half(load - back) or 0 for back in (2, 4, 6)]
    field = next((half & 0xFF for half in before if half & 0xFF00 == 0x2000), None)   # movs r0, #imm
    copied = any(half == 0x4468 for _, half in code.walk(load, 4))   # add r0, sp
    if base is None or field is None or not copied or field <= base:
        raise PointError("url_offset", "the copy of the URL at %#x is not of a stack structure's field" % load)
    return url, field - base


def _holding(code: Code, call: int) -> int:
    return _one("ais_cloud_start", filter(None, [code.function_of(call)]), "no function holds the call creating the task")


def _callers(code: Code, start: int) -> int:
    return _one("start_cloud", filter(None, map(code.function_of, code.calls.get(start, ()))),
                "nothing calls ais_cloud_start")


# Each finder: what it names, what it does, and the points it works from.
FINDERS = (
    (("after_memcmp", "gate_passed"), _integrity_gate, ()),
    (("ef_get_env_blob",), _ef_get_env_blob, ()),
    (("x509_crt_parse",), _x509_crt_parse, ()),
    (("appfw_task",), lambda code: _task_created(code, "appfw_task", NAME_APPFW_TASK)[0], ()),
    (("appfw_loop",), _loop_head, ("appfw_task",)),
    (("link_task", "link_create"), lambda code: _task_created(code, "link_task", NAME_LINK_TASK), ()),
    (("ais_cloud_start",), _holding, ("link_create",)),
    (("start_cloud",), _callers, ("ais_cloud_start",)),
    (("ctx_pointer",), lambda code, function: _first_read(code, function, "ctx_pointer", LDR_THROUGH), ("ais_cloud_start",)),
    (("cloud_flag",), lambda code, function: _first_read(code, function, "cloud_flag", LDRB_THROUGH), ("start_cloud",)),
    (("lan_publish",), _lan_publish, ("start_cloud", "cloud_flag")),
    (("dispatch_url", "url_offset"), _dispatch_url, ("cloud_flag",)),
)


def survey(app: bytes, base: int = APP_BASE) -> dict[str, int | str | PointError]:
    """Every point, or the PointError saying why it was not found."""
    code = Code(app, base)
    results: dict = {}
    for names, finder, needs in FINDERS:
        try:
            for need in needs:
                if isinstance(results[need], PointError):
                    raise PointError(names[0], f"needs {need}, not found")
            value = finder(code, *(results[need] for need in needs))
        except PointError as exc:
            results.update((name, exc if name == exc.point else PointError(name, str(exc))) for name in names)
            continue
        results.update(zip(names, value if len(names) > 1 else (value,)))
    return {field.name: results[field.name] for field in fields(Points)}


def find(app: bytes, base: int = APP_BASE) -> Points:
    """All the points of `app`, or a PointError for the first that was not found."""
    results = survey(app, base)
    for value in results.values():
        if isinstance(value, PointError):
            raise value
    return Points(**results)


def app_of_flash(flash: bytes) -> bytes:
    """The app in a whole flash: to the size its RBL header gives when that
    header is the app's, else the whole of the app partition."""
    table = rebuild_dump.partitions(flash)["app"]
    logical = stock_app.unstripe(flash)
    start, end = table.offset, table.offset + table.length
    try:
        header = rbl.header(logical[end - rbl.HEADER:end])
    except rbl.RblError:
        return logical[start:end - rbl.HEADER]
    return logical[start:start + header.size_raw] if header.name == "app" else logical[start:end - rbl.HEADER]


def app_of_file(data: bytes) -> bytes:
    """The app in a whole flash, an official update file or an app on its own."""
    if data[:len(rbl.MAGIC)] == rbl.MAGIC or data[:4] == struct.pack("<I", 0x1BEEF11E):
        return rbl.unpack(rebuild_dump.official_image(data))
    return app_of_flash(data) if len(data) >= 0x200000 else data


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("files", nargs="+", type=Path)
    args = parser.parse_args(argv)
    missing = 0
    for path in args.files:
        print(path.name)
        for name, value in survey(app_of_file(path.read_bytes())).items():
            missing += isinstance(value, PointError)
            print("  %-16s %s" % (name, value if isinstance(value, (str, PointError)) else "%#x" % value))
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
