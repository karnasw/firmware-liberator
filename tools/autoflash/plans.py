"""Plans: what the device is to write, for the plan phase to carry out.

The Tasmota route has a hand-written Berry program per phase. Every other
change of firmware is a few of the same moves - check the device is laid out as
expected, write some bytes and read them back, clear a region, restart - so
each is a plan, run by one Berry program (tools/espressif/safeboot/repartition/berry/plan.be.tmpl):

- the device checks its partition table and which slot it runs from first;
- a streamed write fetches one 4 KiB block at a time from this computer,
  writes it (skipping a block already right, so a resumed plan rewrites
  nothing it finished), reads it back, and sends what it read back here to be
  compared byte for byte;
- a whole write (the bootloader, a partition table, the boot selection) is
  fetched and checked in full before its first byte is written, so nothing
  waits on the network while the device cannot start, and the old bytes are
  put back if a sector does not read back right;
- an erase clears a region and checks it is clear;
- a copy moves bytes already on the device from one place to another, each
  sector read back, then the whole compared with where it came from - for
  what this computer does not have (the device's settings, say).

Each plan's data travels to the phase server on standard input, never as a
file: some holds the Wi-Fi password (an imprinted image) or the device's own
secrets (a Sonoff backup).
"""

from __future__ import annotations

import binascii
import hashlib
import json
import struct
from dataclasses import dataclass, field

try:
    from tools.espressif.layouts import MOVER_AT, SONOFF_DUAL_OTA, TEMPORARY_OTADATA, Layout
except ModuleNotFoundError:  # run as a script from tools/
    from layouts import MOVER_AT, SONOFF_DUAL_OTA, TEMPORARY_OTADATA, Layout  # type: ignore[no-redef]

SECTOR = 0x1000
# The layouts: Sonoff's, which the ESPHome route keeps, and Tasmota's.
SONOFF_TABLE_SHA256 = "f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf"
TASMOTA_TABLE_SHA256 = None  # set below from safeboot_migration, the one place it is pinned
TABLE_ADDRESS = 0x8000
SONOFF_OTA = (0x20000, 0x210000)
SONOFF_OTADATA = 0x1D000
SONOFF_NVS_END = SONOFF_OTADATA
OTADATA_SIZE = 0x2000
TASMOTA_APP0 = 0xE0000
# Whole writes are held in the device's memory while they are written.
WHOLE_LIMIT = 0x10000
# A real S60TPG wrote 810 KiB, reading every block back to this computer, in
# 36 s, and stops answering after 70-90 s busy; a plan keeps well inside that.
SECONDS_PER_BYTE = 36 / 810_000
BUSY_LIMIT = 55.0

try:
    from tools.espressif.safeboot.safeboot_migration import TARGET_TABLE_SHA256 as TASMOTA_TABLE_SHA256
    from tools.espressif.safeboot.safeboot_migration import TASMOTA_TABLES
    from tools.espressif.sonoff_identity import SECTOR_AT as IDENTITY_AT
except ModuleNotFoundError:  # run as a script from tools/
    from safeboot_migration import TARGET_TABLE_SHA256 as TASMOTA_TABLE_SHA256
    from safeboot_migration import TASMOTA_TABLES
    from sonoff_identity import SECTOR_AT as IDENTITY_AT  # type: ignore[no-redef]


class PlanError(ValueError):
    pass


@dataclass(frozen=True)
class Write:
    """`blob[start:start + length]` to `address`, the rest of its last sector erased."""
    blob: str
    address: int
    start: int = 0
    length: int | None = None  # to the end of the blob
    whole: bool = False


@dataclass(frozen=True)
class Erase:
    address: int
    length: int


@dataclass(frozen=True)
class Copy:
    """`length` bytes already on the device, from `source` to `address`."""
    source: int
    address: int
    length: int


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def erased_sha256(length: int) -> str:
    return sha256(b"\xff" * length)


def padded(data: bytes) -> bytes:
    return data + b"\xff" * (-len(data) % SECTOR)


@dataclass
class Plan:
    name: str                         # its step's report: plan-<name>-report.json
    title: str                        # what the progress line says it copies
    table_sha256: str                 # the layout the device must have
    running: int | None               # flash.current_ota() it must report
    steps: list = field(default_factory=list)
    blobs: dict[str, bytes] = field(default_factory=dict)
    restart: bool = True

    # ---------------------------------------------------------------- checks
    def span(self, step: Write) -> tuple[int, int]:
        blob = self.blobs[step.blob]
        length = len(blob) - step.start if step.length is None else step.length
        return step.start, length

    def regions(self) -> list[tuple[int, int, object]]:
        found = []
        for step in self.steps:
            if isinstance(step, Write):
                _, length = self.span(step)
                found.append((step.address, step.address + len(padded(b"\0" * length)), step))
            else:
                found.append((step.address, step.address + step.length, step))
        return found

    def check(self) -> "Plan":
        if not self.name.isidentifier():
            raise PlanError(f"plan name {self.name!r} is not a plain word")
        for step in self.steps:
            if isinstance(step, Write):
                if step.blob not in self.blobs:
                    raise PlanError(f"{self.name}: no data called {step.blob}")
                start, length = self.span(step)
                if step.address % SECTOR or start % SECTOR:
                    raise PlanError(f"{self.name}: {step.blob} is not sector-aligned")
                if length <= 0 or start + length > len(self.blobs[step.blob]):
                    raise PlanError(f"{self.name}: {step.blob} slice is outside it")
                if step.whole and length > WHOLE_LIMIT:
                    raise PlanError(f"{self.name}: {step.blob} is too big to write whole")
            elif isinstance(step, Erase):
                if step.address % SECTOR or step.length % SECTOR or step.length <= 0:
                    raise PlanError(f"{self.name}: an erase is not sector-aligned")
            elif isinstance(step, Copy):
                if step.source % SECTOR or step.address % SECTOR or step.length % SECTOR or step.length <= 0:
                    raise PlanError(f"{self.name}: a copy is not sector-aligned")
            else:
                raise PlanError(f"{self.name}: unknown step {step!r}")
        spans = sorted(self.regions(), key=lambda region: region[0])
        for (_, end, a), (start, _, b) in zip(spans, spans[1:]):
            if start < end:
                raise PlanError(f"{self.name}: {a} and {b} overlap")
        if spans and (spans[0][0] < 0 or spans[-1][1] > 0x400000):
            raise PlanError(f"{self.name}: a step is outside the 4 MiB flash")
        for step in self.steps:
            if isinstance(step, Copy):
                if step.source + step.length > 0x400000:
                    raise PlanError(f"{self.name}: a copy is from outside the 4 MiB flash")
                if any(start < step.source + step.length and step.source < end
                       for start, end, _ in spans):
                    raise PlanError(f"{self.name}: a copy is from a region the plan writes")
        if self.busy_estimate() > BUSY_LIMIT:
            raise PlanError(f"{self.name}: about {self.busy_estimate():.0f} s of writing keeps the "
                            f"device busy past {BUSY_LIMIT:.0f} s; split it")
        return self

    def final_table_sha256(self) -> str:
        """The partition table the device has once the plan is done."""
        for step in self.steps:
            if isinstance(step, Write) and step.address == TABLE_ADDRESS:
                start, length = self.span(step)
                return sha256(self.blobs[step.blob][start:start + length])
        return self.table_sha256

    def busy_estimate(self) -> float:
        written = sum(self.span(step)[1] for step in self.steps if isinstance(step, Write))
        copied = sum(step.length for step in self.steps if isinstance(step, Copy))
        return (written + copied) * SECONDS_PER_BYTE

    def blocks(self) -> int:
        """4 KiB blocks fetched from this computer, for the progress line."""
        return sum(len(padded(b"\0" * self.span(step)[1])) // SECTOR
                   for step in self.steps if isinstance(step, Write))

    # ------------------------------------------------- what the device reports
    def expected(self) -> dict[str, str]:
        """What the device's report must say for each step, besides PASS."""
        found = {}
        for index, step in enumerate(self.steps):
            if isinstance(step, Write):
                start, length = self.span(step)
                found[f"step{index}"] = sha256(self.blobs[step.blob][start:start + length])
            elif isinstance(step, Copy):
                found[f"step{index}"] = "copied"  # bytes this computer does not have
            else:
                found[f"step{index}"] = "erased"
        return found

    def summary(self) -> list[dict[str, object]]:
        """The steps for the evidence: places, sizes and hashes, never bytes."""
        rows = []
        for step in self.steps:
            if isinstance(step, Write):
                start, length = self.span(step)
                rows.append({"write": step.blob, "address": f"0x{step.address:06X}",
                             "bytes": length, "whole": step.whole,
                             "sha256": sha256(self.blobs[step.blob][start:start + length])})
            elif isinstance(step, Copy):
                rows.append({"copy": f"0x{step.source:06X}", "to": f"0x{step.address:06X}",
                             "bytes": step.length})
            else:
                rows.append({"erase": f"0x{step.address:06X}", "bytes": step.length})
        return rows

    # ---------------------------------------------------------- the transfer
    def bundle(self) -> bytes:
        """The plan and its data as one stream, for the server's standard input."""
        header = {
            "name": self.name, "title": self.title, "table_sha256": self.table_sha256,
            "running": self.running, "restart": self.restart,
            "steps": [(["write", s.blob, s.address, s.start, s.length, s.whole]
                       if isinstance(s, Write) else
                       ["copy", s.source, s.address, s.length] if isinstance(s, Copy) else
                       ["erase", s.address, s.length])
                      for s in self.steps],
            "blobs": [[name, len(data)] for name, data in self.blobs.items()],
        }
        return json.dumps(header).encode("ascii") + b"\n" + b"".join(self.blobs.values())

    @classmethod
    def from_bundle(cls, stream: bytes) -> "Plan":
        line, _, rest = stream.partition(b"\n")
        try:
            header = json.loads(line)
            blobs, offset = {}, 0
            for name, size in header["blobs"]:
                blobs[name] = rest[offset:offset + size]
                offset += size
            if offset != len(rest):
                raise PlanError("the plan's data is not the length it says")
            kinds = {"write": Write, "copy": Copy, "erase": Erase}
            steps = [kinds[row[0]](*row[1:]) for row in header["steps"]]
            plan = cls(name=header["name"], title=header["title"],
                       table_sha256=header["table_sha256"], running=header["running"],
                       steps=steps, blobs=blobs, restart=header["restart"])
        except (ValueError, KeyError, TypeError) as exc:
            raise PlanError(f"not a plan: {exc}") from exc
        return plan.check()

    def berry(self) -> str:
        """The steps as a Berry list, for the template."""
        rows = []
        for step in self.steps:
            if isinstance(step, Write):
                start, length = self.span(step)
                data = self.blobs[step.blob][start:start + length]
                pad = len(padded(data)) - len(data)
                rows.append(
                    f'{{"op":"write","blob":"{step.blob}","addr":{step.address},'
                    f'"first":{start // SECTOR},"len":{length},"whole":{str(step.whole).lower()},'
                    f'"sha":"{sha256(data)}","pad":{pad},"padsha":"{erased_sha256(pad)}"}}')
            elif isinstance(step, Copy):
                rows.append(f'{{"op":"copy","src":{step.source},"addr":{step.address},'
                            f'"len":{step.length}}}')
            else:
                rows.append(f'{{"op":"erase","addr":{step.address},"len":{step.length},'
                            f'"sha":"{erased_sha256(step.length)}"}}')
        return "[\n  " + ",\n  ".join(rows) + "\n]"


# ------------------------------------------------------------------ the plans


def bootloader(bootloader_image: bytes, *, table_sha256: str = SONOFF_TABLE_SHA256,
               running: int | None = 1, address: int = 0) -> Plan:
    """ESPHome's bootloader in place of whatever the device has, and nothing else:
    at `address`, where the device's chip looks for it (chips.Chip.bootloader_at)."""
    return Plan(
        name="bootloader", title="the bootloader", table_sha256=table_sha256, running=running,
        steps=[Write("bootloader", address, whole=True)], blobs={"bootloader": bootloader_image},
        restart=False,
    ).check()


def esphome(image: bytes, layout: Layout = SONOFF_DUAL_OTA) -> Plan:
    """ESPHome into ota_0 beside the Tasmota in ota_1; then the boot selection
    cleared, so the device starts ota_0, and a restart.  Until that clearing it
    still starts Tasmota."""
    return Plan(
        name="esphome", title="ESPHome", table_sha256=layout.table_sha256, running=1,
        steps=[Write("esphome", layout.ota[0]), Erase(layout.otadata, OTADATA_SIZE)],
        blobs={"esphome": image},
    ).check()


def keep_identity(sector: bytes, layout: Layout = SONOFF_DUAL_OTA) -> Plan:
    """The sector that keeps the device's Sonoff identity, at the top of the flash
    (tools/espressif/sonoff_identity.py), written from the hub before the commit
    takes Sonoff's settings: on Sonoff's layout it is the end of ota_1, past the
    Berry Tasmota running there, or no partition's at all."""
    if len(sector) != SECTOR:
        raise PlanError("the identity is not one sector")
    return Plan(
        name="keep_identity", title="the device's Sonoff identity", table_sha256=layout.table_sha256,
        running=1, steps=[Write("identity", IDENTITY_AT, whole=True)], blobs={"identity": sector},
        restart=False,
    ).check()


def leave_tasmota(mover: bytes, table: bytes, layout: Layout = SONOFF_DUAL_OTA, *,
                  tasmota_table: str | None = None, settings: bytes | None = None) -> Plan:
    """From official Tasmota on Tasmota's layout to the mover on Sonoff's, or on
    the temporary table of a layout that needs one (Layout.temporary_table).

    The bootloader stays as it is: it may be a build of Tasmota's own, which
    starts the mover as well, and the full Tasmota has too little memory left
    to hold ESPHome's while it writes it. The hub writes it (D5), on a Tasmota
    that has room, and a route without the hub is only for a device that has it.

    Tasmota runs from app0 at 0xE0000, across both of Sonoff's app slots, but
    the mover fits in front of it, at 0x20000: Sonoff's ota_0.  With the
    region Sonoff's layout keeps its settings and boot selection in cleared,
    and Sonoff's partition table written last, the device starts the mover from
    ota_0.  The table is the one moment a power cut leaves a device that cannot
    start; it is one sector, written in a few milliseconds.

    `tasmota_table` is the table the device has: the one a conversion writes,
    or Tasmota's own on a device converted before the identity was kept.
    `settings`, an NVS holding the device's Sonoff identity, goes where Sonoff's
    layout keeps its settings, so that the next firmware has it as Sonoff's
    firmware kept it; the rest of that region is cleared.
    """
    if MOVER_AT + len(padded(mover)) > TASMOTA_APP0:
        raise PlanError("the mover does not fit in front of Tasmota")
    if sha256(table) != layout.leaving_sha256:
        raise PlanError("that is not Sonoff's partition table")
    tasmota_table = tasmota_table or TASMOTA_TABLE_SHA256
    if tasmota_table not in TASMOTA_TABLES:
        raise PlanError("that is not a Tasmota layout this tool converts to")
    blobs = {"mover": mover, "table": table}
    clear: list = [Erase(layout.nvs, MOVER_AT - layout.nvs)]
    if settings:
        written = len(padded(settings))
        if layout.nvs + written > (TEMPORARY_OTADATA if layout.needs_temporary else layout.otadata):
            raise PlanError("the settings do not fit where the next layout keeps them")
        blobs["settings"] = settings
        clear = [Write("settings", layout.nvs), Erase(layout.nvs + written, MOVER_AT - layout.nvs - written)]
    return Plan(
        name="leave_tasmota", title="the mover", table_sha256=tasmota_table, running=0,
        steps=[Write("mover", MOVER_AT), *clear, Write("table", TABLE_ADDRESS, whole=True)],
        blobs=blobs,
    ).check()


def own_table_from_temporary(sonoff_table: bytes, layout: Layout) -> Plan:
    """From the temporary table to the device's own, the Berry Tasmota running from
    ota_1, which both have in the same place.

    The settings Tasmota has stay where they are, the start of the larger NVS. The
    rest of that is cleared, or NVS would find the temporary ota_0 and boot
    selection there. The boot selection for ota_1 goes in first. The temporary
    one goes last of all but the table, after what it could start: a power cut
    between the two finds no selection and no ota_0, and the bootloader starts
    ota_1, the next valid slot. The table is written last, as ever.
    """
    if sha256(sonoff_table) != layout.table_sha256:
        raise PlanError("that is not Sonoff's partition table")
    return Plan(
        name="own_table", title="the device's own memory layout", table_sha256=layout.temporary_sha256, running=1,
        steps=[
            Write("otadata", layout.otadata, whole=True),
            Erase(MOVER_AT, layout.otadata - MOVER_AT),
            # Its phy data, and the start of its ota_0, which would look like an app.
            Erase(layout.phy, layout.ota[0] + SECTOR - layout.phy),
            Erase(TEMPORARY_OTADATA, MOVER_AT - TEMPORARY_OTADATA),
            Write("table", TABLE_ADDRESS, whole=True),
        ],
        blobs={"otadata": otadata_selecting(1), "table": sonoff_table},
    ).check()


def sonoff_layout_back(sonoff_table: bytes, layout: Layout = SONOFF_DUAL_OTA) -> Plan:
    """From the interim layout a commit stopped part way leaves, back to Sonoff's.

    The commit moved Sonoff's settings, reserve, boot selection and PHY data to
    the end of ota_0 and pointed an interim table at them (safeboot_migration
    says why); Tasmota has run on them there since.  Copied back, and Sonoff's
    table written last, the device is as it was before the commit - with
    whatever Tasmota saved meanwhile - and the restart starts the same Tasmota
    from it.  A power cut before the table leaves the interim layout, as good
    as before; the table is the one sector a cut leaves unable to start.
    """
    if sha256(sonoff_table) != layout.table_sha256:
        raise PlanError("that is not Sonoff's partition table")
    return Plan(
        name="sonoff_layout", title="Sonoff's layout", table_sha256=layout.interim_sha256, running=1,
        steps=[Copy(layout.moved_low, layout.nvs, layout.low_size),
               Write("table", TABLE_ADDRESS, whole=True)],
        blobs={"table": sonoff_table},
    ).check()


def otadata_selecting(slot: int) -> bytes:
    """A boot selection that starts ota_<slot>, as ESP-IDF's otatool writes it."""
    sequence = slot + 1
    entry = struct.pack("<I20sII", sequence, b"\xff" * 20, 0xFFFFFFFF,
                        binascii.crc32(struct.pack("<I", sequence), 0xFFFFFFFF) & 0xFFFFFFFF)
    return padded(entry) + b"\xff" * SECTOR


def stock_slot(backup: bytes, layout: Layout = SONOFF_DUAL_OTA) -> int:
    """Which of the backup's app slots holds Sonoff's own firmware."""
    # The project name in the app's description, 80 bytes into the image.
    found = [index for index, address in enumerate(layout.ota)
             if backup[address + 80:address + 112].split(b"\0")[0] == b"esp32_fw"]
    if len(found) != 1:
        raise PlanError("the backup does not hold exactly one Sonoff firmware")
    return found[0]


def app_length(image: bytes) -> int:
    """How long the app at the start of `image` is, its checksum and hash included."""
    from .imprint import parse_esp_image
    return parse_esp_image(image)["parsed_image_size"]


def back_to_sonoff(backup: bytes, running: int, layout: Layout = SONOFF_DUAL_OTA) -> list[Plan]:
    """The device's own Sonoff firmware and settings, from its backup, beside the
    Berry Tasmota it runs; then the boot selection set to it, and a restart.

    Sonoff's app is more than one phase may write, so it is split across two;
    nothing the first writes is used until the second sets the boot selection.
    The bootloader stays ESPHome's: Sonoff's firmware starts under it.
    """
    if len(backup) != 0x400000 or sha256(backup[TABLE_ADDRESS:TABLE_ADDRESS + SECTOR]) != layout.table_sha256:
        raise PlanError("that is not a backup of a device on Sonoff's layout")
    slot = stock_slot(backup, layout)
    if slot == running:
        raise PlanError("Sonoff's firmware belongs in the slot the device is running from")
    address = layout.ota[slot]
    app = backup[address:address + app_length(backup[address:address + layout.slot_size])]
    half = len(padded(app[:len(app) // 2]))
    settings = backup[layout.nvs:layout.otadata]
    phy = backup[layout.phy:layout.phy + SECTOR]
    blobs = {"sonoff": app, "settings": settings, "phy": phy}
    if slot == 0:
        select = Erase(layout.otadata, OTADATA_SIZE)
    else:
        blobs["otadata"] = otadata_selecting(slot)
        select = Write("otadata", layout.otadata, whole=True)
    first = Plan(
        name="sonoff_app", title="Sonoff's firmware", table_sha256=layout.table_sha256,
        running=running, steps=[Write("sonoff", address, 0, half)], blobs={"sonoff": app},
        restart=False,
    ).check()
    second = Plan(
        name="sonoff_rest", title="Sonoff's firmware and settings", table_sha256=layout.table_sha256,
        running=running,
        steps=[Write("sonoff", address + half, half, len(app) - half),
               Write("settings", layout.nvs), Write("phy", layout.phy, whole=True), select],
        blobs=blobs,
    ).check()
    return [first, second]


# Supla's layout (tools/autoflash/supla.py): the builder's own.  Pinned, so a
# change to the layout is a change to this too.
SUPLA_TABLE_SHA256 = "5826ebb499d58c5707f565be1c80a6e3d1d5d2198dadadb4d9f75d5773ff12aa"
SUPLA_APP0 = 0x10000
SUPLA_APP_SIZE = 0x1E0000
SUPLA_NVS = (0x9000, 0x5000)
SUPLA_OTADATA = 0xE000
SUPLA_TAIL = 0x3D0000           # the LittleFS and core dump partitions, to the end of the flash
# The app's first 64 KiB go over Sonoff's settings, reserve, boot selection
# and PHY data, the rest from Sonoff's ota_0 on.
SUPLA_HEAD = SONOFF_OTA[0] - SUPLA_APP0


def supla(image: bytes, bootloader_image: bytes, table: bytes, otadata: bytes) -> list[Plan]:
    """GUI-Generic laid out as the builder lays it out, written from the
    Berry Tasmota running from Sonoff's ota_1, in two plans as it is more
    than one may write; then the end of the flash cleared for its LittleFS
    and core dump, its settings partition cleared, and its boot selection,
    bootloader (what is left of a longer one after it cleared) and partition
    table, the table last, and a restart.

    The app goes in at 0x10000, Sonoff's ota_0 first and its first 64 KiB
    last: until those overwrite Sonoff's boot selection the device starts
    Tasmota as it did.  After, with no boot selection, the bootloader tries
    each slot: ota_0 then holds no app (the image is checked for that), so it
    starts the Tasmota in ota_1 - with its settings, which share those 64
    KiB, maybe gone.  The bootloader and the table are the sectors a power cut
    leaves unable to start; each is written in a few milliseconds.
    """
    if len(image) > SUPLA_APP_SIZE:
        raise PlanError("GUI-Generic does not fit Supla's slots")
    if len(image) <= SUPLA_HEAD + SECTOR:
        raise PlanError("that is too small to be GUI-Generic")
    if image[SUPLA_HEAD] == 0xE9:
        # Sonoff's ota_0 would hold an app's start: one cut short there could be started.
        raise PlanError("GUI-Generic would leave what looks like an app in Sonoff's ota_0")
    if sha256(table) != SUPLA_TABLE_SHA256:
        raise PlanError("that is not Supla's partition table")
    if SUPLA_APP0 + len(padded(image)) > SONOFF_OTA[1]:
        raise PlanError("GUI-Generic would reach the Tasmota it is written from")
    if len(padded(bootloader_image)) > TABLE_ADDRESS:
        raise PlanError("the bootloader does not fit before the partition table")
    split = max(len(image) // 2 // SECTOR * SECTOR, SUPLA_HEAD + SECTOR)
    blobs = {"supla": image}
    # What is left of a longer bootloader after it, cleared once it is in place.
    after_bootloader = len(padded(bootloader_image))
    leftover = ([Erase(after_bootloader, TABLE_ADDRESS - after_bootloader)]
                if after_bootloader < TABLE_ADDRESS else [])
    first = Plan(
        name="supla_app", title="Supla", table_sha256=SONOFF_TABLE_SHA256, running=1,
        steps=[Write("supla", SONOFF_OTA[0], SUPLA_HEAD, split - SUPLA_HEAD)], blobs=blobs,
        restart=False,
    ).check()
    second = Plan(
        name="supla_rest", title="Supla and its memory layout", table_sha256=SONOFF_TABLE_SHA256,
        running=1,
        steps=[Write("supla", SUPLA_APP0 + split, split, len(image) - split),
               Write("supla", SUPLA_APP0, 0, SUPLA_HEAD),
               Erase(SUPLA_TAIL, 0x400000 - SUPLA_TAIL),
               Erase(*SUPLA_NVS),
               Write("otadata", SUPLA_OTADATA, whole=True),
               Write("bootloader", 0, whole=True),
               *leftover,
               Write("table", TABLE_ADDRESS, whole=True)],
        blobs=blobs | {"otadata": otadata, "bootloader": bootloader_image, "table": table},
    ).check()
    return [first, second]
