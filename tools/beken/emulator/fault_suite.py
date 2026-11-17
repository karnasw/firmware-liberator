#!/usr/bin/env python3
"""The Beken fault suite: every direction, every step that writes to or waits
on the device, every fault - on emulated Beken devices side by side (beken-emu.sh
device, from the public S61s dump), as tools/espressif/emulator/fault_suite.py
does for the Espressif devices.

    fault_suite.py prep                 the starting devices: one on the interim firmware,
                                        one on OpenBeken, one on ESPHome, converted by
                                        the tool from the dump
    fault_suite.py prep-update          the update scenarios' starting devices: on OpenBeken
                                        and on ESPHome as an earlier release converts them
    fault_suite.py check [--instances 1,2]
                                        the final firmware, with no faults: each installed
                                        from Sonoff's, then each device taken to the other,
                                        on the two emulators numbered
                                        (tools/repo/emulator_check.py)
    fault_suite.py list                 every scenario
    fault_suite.py list quick           those of a routine run (QUICK_OUTAGES)
    fault_suite.py list openbeken       those a new OpenBeken build is checked with
                                        (docs/updating-firmware.md)
    fault_suite.py batch [FILE]         the scenarios in FILE (one id a line; every
                                        scenario without), skipping those passed
    fault_suite.py report               the latest result of each
    fault_suite.py found                what each fault and length has found, in the
                                        results here and kept (tools/repo/fault_yield.py)
    fault_suite.py share FILE N...      FILE's scenarios shared out between machines
                                        running N emulators each, to end together:
                                        FILE.1, FILE.2, ... (--times-from DIR: another
                                        machine's suite folders, for their times)

Each scenario: a run of the tool from a starting device (a fresh dump for
Sonoff's firmware, a saved one otherwise); when the step under test has been
going for a share of its usual time (40%, and 10% and 90% at the steps that
write), the fault; then the tool run again, as a person would - switching the
device off and on where the run says to - until the device is converted, or four
runs fail. Every scenario also checks the fault landed while its step was
under way (MISTIMED if not), and that the converting run's report says its
end-of-run checks passed, on a device with a meter (the S61) the emulated
meter confirming the relay switched (FAIL-CHECKS if not:
tools/repo/fault_verdicts.py),
that each run left one line a stage on its screen, none of a try that
failed (FAIL-SCREEN if not), that a device converted to OpenBeken or ESPHome
runs this checkout's release of it (FAIL-NOT-LATEST if not), and that no run
met an error the tool did not see coming, though the next converted
(FAIL-UNEXPECTED if one did, as the Espressif suite has it).

The faults: the device switched off for 1 or 30 s (deviceoff), the network
gone - the router off - for 5, 15, 30, 120 or 180 s (outage), as the
Espressif suite's, the tool killed and run again after 1 or 180 s (crash), and
Ctrl-C (ctrlc); where ESPHome raises its own network (K2), the router alone off
for 150 s (fallback), the person joining that network as the run says to
(Person); at the steps that write, a power cut and
another as the tool sends it again (double: in the same run, if it sends its
command again itself), and the router giving the device
a new address once the step is done (readdress); and at each route's
"apply", the device switched off 10, 50 or 90% of the way through its
bootloader writing the new firmware in - timed off the device's own output.
And at each step that sends an image the stock bootloader installs - Sonoff's
firmware's (B2) and OpenBeken's fetch of ESPHome (J2, O1) - an image it
refuses (badimage): the tool sends it spoilt once (--simulate-bad-image
STEP), fetched whole but failing the bootloader's CRC check, so the device
starts the firmware it had again; the scenario passes when the tool sends
it again by itself and the device is converted.

And readouts (readout_scenarios): of a device fresh from Sonoff's firmware,
with a fault in the read and as Sonoff's firmware is put back, then what a
person does until a whole readout.tar is kept and the device works on Sonoff's
firmware; and of a device on OpenBeken, which must read its first stage alone.

The routes from the interim firmware start from a device a run to ESPHome left
there, found by a new run. A device converted through the interim firmware is
also checked: by the names it asked DHCP for, it was autoflash-recovery-CDEF
there from its first request and never OpenBeken's own name; and once ESPHome
is on it, the interim's settings are erased from its flash (interim_problems).

Each emulator is a device of its own (beken-emu.sh --instance N: autoflash-beken-emu-N),
AUTOFLASH_BEKEN_EMU_WORKERS of them at once (default 2: each takes a core, and the
tool's runs share what is left). Sonoff's firmware restarts by itself once
it has an update, in the emulator as on a real device
(tools/beken/emulator/README.md).

Results: captures/emulation/beken/suite/results.jsonl (AUTOFLASH_BEKEN_EMU_SUITE_DIR
for another folder), and each scenario's run
logs and the device's serial output beside it; a scenario run again keeps its
earlier attempts' beside them, numbered (logs/<scenario>.1 the first). A fault that never happened is
NOT-FIRED, not a pass. Each result's notes say how short of memory the computer
was; a failure while it was starved prints "memory-starved", as the device's
connections can then fail with nothing wrong.
"""
from __future__ import annotations

import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tools.autoflash import output  # noqa: E402
from tools.autoflash.output import CTRL_C_LOGGED  # noqa: E402
from tools.autoflash import esphome, kinds, openbeken  # noqa: E402
from tools.autoflash.routes import beken  # noqa: E402
from tools.beken.emulator import folders  # noqa: E402
from tools.repo import cpu_load, emulator_stats, fault_verdicts, fault_yield, scenario_times  # noqa: E402
from tools.repo.earlier_attempts import keep_earlier  # noqa: E402
from tools.repo.memory_pressure import MemoryWatch  # noqa: E402

BK_EMU = REPO / "tools/beken/emulator/beken-emu.sh"
SAVED = folders.SAVED
OUT = Path(os.environ.get("AUTOFLASH_BEKEN_EMU_SUITE_DIR", folders.SUITE))
RESULTS = OUT / "results.jsonl"
# A made-up eWeLink account: the device's eWeLink is the local stub, which takes any.
# And the emulated home Wi-Fi's details (cloud_online's), for a run sending
# them to ESPHome through its own network: given in advance, as for a run nobody
# sits through.
ACCOUNT = ("EWELINK_EMAIL=someone@example.invalid\nEWELINK_PASSWORD=made-up-password\n"
           "AUTOFLASH_WIFI_SSID=AutoflashNet\nAUTOFLASH_WIFI_PASSWORD=autoflash1234\n")

# Each direction's steps that write to or wait on the device, with how long each
# took in passing runs, in seconds: a fault lands at a share of it.
ROUTES = {
    "sonoff>openbeken": {"B2": 30, "K1": 27, "B4": 12},
    "sonoff>esphome": {"B2": 32, "J1": 28, "B4": 12, "J2": 14, "K2": 30},
    "openbeken>esphome": {"B4": 12, "O1": 12, "K2": 31},
    "openbeken>sonoff": {"O2": 24, "K3": 75},
    "esphome>openbeken": {"E1": 6, "K1": 27, "B4": 12},
    "esphome>sonoff": {"E2": 15, "K3": 80},
    # A device a run to ESPHome left on the interim firmware, found by a new run.
    # Kept as OpenBeken, J3 waits for its restart: K1 finds it answering.
    "interim>esphome": {"B4": 12, "J2": 14, "K2": 30},
    "interim>openbeken": {"J3": 15, "B4": 12},
    "interim>sonoff": {"B4": 12, "J4": 24, "K3": 75},
    # A device on the firmware of the release before, updated (prep_update).
    "outdated>openbeken": {"U3": 14, "U4": 20},
    "outdated>esphome": {"U1": 7, "U2": 20},
}
# The kind of device the suite emulates: an S61, from its public dump. Another
# Beken device's flash (AUTOFLASH_BEKEN_EMU_FLASH) is said by
# AUTOFLASH_BEKEN_EMU_FLASH_DEVICE, its key, which its emulator stats are of.
DEVICE = kinds.SONOFF_S61
STATS_DEVICE = kinds.find(os.environ.get("AUTOFLASH_BEKEN_EMU_FLASH_DEVICE")) or DEVICE
# The release of each firmware a converted device must end on: this checkout's
# (FAIL-NOT-LATEST) - after an update from an earlier release's, too.
LATEST = {firmware: beken.published_version(DEVICE, firmware) for firmware in ("openbeken", "esphome")}
# The release whose firmware an update's device is on: any before this checkout's.
EARLIER_RELEASE = "v2.3.0"
# Where its tree keeps the dumps and the devices its prep saves.
EARLIER_FIRMWARES = "captures/emulation/beken-firmwares"
EARLIER_SAVED = "captures/emulation/s61/saved"
# The outages as the Espressif suite's, each length where it can lead somewhere
# the others do not: within the tool's shortest timeouts, past them, and long
# enough for ESPHome to raise its own network (a minute or two).
OUTAGES = (5, 15, 30, 120, 180)
# A routine run's outages (list quick), as the Espressif suite's: the shortest
# and the longest.
QUICK_OUTAGES = (OUTAGES[0], OUTAGES[-1])
CORE = ([("deviceoff", 1), ("deviceoff", 30)] + [("outage", s) for s in OUTAGES]
        + [("crash", 1), ("crash", 180), ("ctrlc", "once")])
# Where a firmware raises its own network when it cannot join the home one:
# ESPHome, at K2. There the router stays off 150 s (fallback), the run sets it up
# through its own network, the person joining it when the run says to.
FALLBACK_STEPS = {"K2"}
# At the steps that write, the riskiest moments too: just begun, nearly done.
TIMING = [("deviceoff", 1), ("crash", 1)]
RUN_LIMIT = 1500       # seconds one run of the tool may take
RECOVERY_RUNS = 4      # runs after the fault before a scenario fails
# The steps whose image the stock bootloader installs, and the tool can send
# spoilt (--simulate-bad-image STEP): Sonoff's firmware's, and OpenBeken's
# fetch of ESPHome.
BAD_IMAGE_STEPS = {"B2", "J2", "O1"}
# Those of them where the device, refusing the image, is on OpenBeken, which the
# tool can restart: there the image is spoilt once more than the run sends it
# again (runner.GO_BACK_LIMIT, 2), so the run restarts the device - as a person
# switching it off and on would - and sends it afresh (badimage-restart).
RESTART_IMAGE_STEPS = {"J2", "O1"}
SPOILT_PAST_LIMIT = 3
# The interim firmware's settings, which it erases before the firmware it
# fetched starts (tools/beken/openbeken/README.md#the-interim-firmware): its
# own sector, and the flash variables its Wi-Fi keeps.
INTERIM_SETTINGS = ((0x3E1000, 0x1000), (0x1E5000, 0x8000))
# The name a device asks its router's DHCP for, by its firmware.
DHCP_NAME = re.compile(r"DHCPACK\(\S+\) \S+ \S+ (\S+)")
# The address its router gave it, each time.
DHCP_ADDRESS = re.compile(r"DHCPACK\(\S+\) (\S+) ")
NAME_PREFIX = {"interim": "autoflash-recovery-", "openbeken": "openbeken-", "esphome": "esphome-"}
# A new OpenBeken build's check: each thing this project's driver and patches
# do on it, once - its Wi-Fi and settings taken from Sonoff's (B4 after B2),
# the interim firmware named, fetching ESPHome and wiping itself (J2), kept
# as OpenBeken (J3), each way back to Sonoff's firmware (J4, O2), and
# installed from ESPHome (K1).
OPENBEKEN_CHECK = ["sonoff>openbeken:B4:crash-1", "sonoff>esphome:J2:badimage-once", "interim>openbeken:J3:crash-1",
                   "interim>sonoff:J4:crash-1", "openbeken>sonoff:O2:crash-1", "esphome>openbeken:K1:crash-1"]

# Readouts (cli.py readout, unattended, docs/readouts.md) of a device fresh from
# Sonoff's firmware: each phase where the fault matters most, the run's events
# (output.PHASES) as it starts and ends, and how long it lasts in the emulator
# (seconds).
# The read is 256 paced chunks of 16 KB - B4 reads 8 of them in 12 s. Putting
# Sonoff's firmware back is the readout OpenBeken fetching it (O2's 24 s), then
# the bootloader writing it in and Sonoff's firmware starting (K3's 75 s): its
# faults land at 10%, in the fetch, the part only the readout has - the
# bootloader's write is the conversions' own, cut in their apply scenarios.
READOUT_PHASES = {"read": ("read", "read-done", 400), "back": ("back", "back-done", 100)}
READOUT_FAULTS = [("read", "deviceoff", 1, 40), ("read", "outage", 30, 40), ("read", "crash", 1, 40),
                  ("back", "deviceoff", 1, 10), ("back", "outage", 30, 10)]
READOUT = "readout.tar"


def sid(route: str, step: str, fault: str, arg, at: int = 40) -> str:
    return f"{route}:{step}{'' if at == 40 else f'@{at}'}:{fault}-{arg}"


def scenarios(quick: bool = False) -> list[dict]:
    """Every scenario; `quick`, a routine run's: the outages only at QUICK_OUTAGES."""
    found = []
    for route, steps in ROUTES.items():
        for step in steps:
            for fault, arg in CORE:
                if quick and fault == "outage" and arg not in QUICK_OUTAGES:
                    continue
                found.append(dict(id=sid(route, step, fault, arg), route=route, step=step,
                                  fault=fault, arg=arg, at=40))
            if step in beken.DESTRUCTIVE_STEPS:
                for at in (10, 90):
                    for fault, arg in TIMING:
                        found.append(dict(id=sid(route, step, fault, arg, at), route=route, step=step,
                                          fault=fault, arg=arg, at=at))
                # A power cut there, then another as the tool sends it again; and the
                # device at a new address once it is sent, as a router may give it.
                for fault, arg in (("double", "deviceoff-1"), ("readdress", "new")):
                    found.append(dict(id=sid(route, step, fault, arg), route=route, step=step,
                                      fault=fault, arg=arg, at=40))
            if step in FALLBACK_STEPS:
                found.append(dict(id=sid(route, step, "fallback", 150), route=route, step=step,
                                  fault="fallback", arg=150, at=40))
        # An image the bootloader refuses, sent again.
        for step in set(steps) & BAD_IMAGE_STEPS:
            found.append(dict(id=sid(route, step, "badimage", "once"), route=route, step=step,
                              fault="badimage", arg="once", at=40))
        for step in set(steps) & RESTART_IMAGE_STEPS:
            found.append(dict(id=sid(route, step, "badimage", "restart"), route=route, step=step,
                              fault="badimage", arg="restart", at=40))
        # The device switched off while its bootloader writes the new firmware in:
        # timed off the device's own output, a share of the way through the write.
        for at in (10, 50, 90) if set(steps) & beken.DESTRUCTIVE_STEPS else ():
            found.append(dict(id=sid(route, "apply", "deviceoff", 1, at), route=route, step="apply",
                              fault="deviceoff", arg=1, at=at))
    return found + readout_scenarios()


def readout_scenarios() -> list[dict]:
    """A readout of a device fresh from Sonoff's firmware, unattended; the same
    with a fault in each phase where it matters most (READOUT_FAULTS); and one
    started on a device on OpenBeken, which must read its first stage alone."""
    def one(route: str, step: str, fault: str, arg, at: int = 40) -> dict:
        return dict(id=sid(route, step, fault, arg, at), route=route, step=step, fault=fault, arg=arg, at=at)
    return [one("sonoff>readout", "start", "none", "unattended"),
            *(one("sonoff>readout", step, fault, arg, at) for step, fault, arg, at in READOUT_FAULTS),
            one("openbeken>readout", "start", "none", "first-stage")]


def route_steps(route: str) -> list[str]:
    """Every step a run of `route` records, after the start all routes share."""
    source, target = route.split(">")
    return [step for step, _, _ in beken.steps_for(target, source)]


def interim_problems(route: str, names: list[str], flash: bytes | None) -> list[str]:
    """What a converted device shows wrong of the interim firmware: from the
    names it asked DHCP for, in order, and its flash once stopped (None when
    the route does not need it). On the interim firmware its name is
    autoflash-recovery-CDEF from its first request - never OpenBeken's own -
    and once it has fetched ESPHome its settings are gone."""
    source, target = route.split(">")
    steps = route_steps(route)
    problems = []
    if "J1" in steps and not any(name.startswith(NAME_PREFIX["interim"]) for name in names):
        problems.append("the interim firmware never asked DHCP by its own name")
    if "J2" in steps and any(name.startswith(NAME_PREFIX["openbeken"]) for name in names):
        problems.append("the interim firmware asked DHCP by OpenBeken's own name")
    if target in NAME_PREFIX and not (names and names[-1].startswith(NAME_PREFIX[target])):
        problems.append(f"the device's last name was {names[-1] if names else 'none'}, not {target}'s")
    if "J2" in steps and flash is not None:
        for start, size in INTERIM_SETTINGS:
            if flash[start:start + size] != b"\xff" * size:
                problems.append(f"the interim firmware's settings at {start:#x} were not erased")
    return problems


def converted(state: dict, route: str) -> bool:
    """Whether a run's record (autoflash-state.json) has every step of the
    route done: the device on its new firmware, and checked there."""
    steps = state.get("steps", {})
    return all((steps.get(step) or {}).get("status") == "done" for step in route_steps(route))


def metered(state: dict) -> bool:
    """Whether the end-of-run checks must have the meter confirm the relay
    switched, by the kind a run's record (autoflash-state.json) says it
    converted: one with a meter (kinds.Kind.has_meter, the S61) measures the
    emulated load, one without (the Basic Gen5) has nothing to confirm it. A
    run that stopped before it knew the kind is held to the meter: the dump's
    device, the S61, has one."""
    kind = kinds.by_key((state.get("inputs") or {}).get("device_kind"))
    return kind.has_meter if kind else True


def clear(folder: Path) -> None:
    """A folder gone, read-only files in it too (OpenBeken's toolchain unpacks
    so): a scenario started over what is left would fail for that reason alone."""
    def writable_then(remove, path, _exc):
        os.chmod(Path(path).parent, 0o700)
        remove(path)
    if not folder.exists():
        return
    try:
        shutil.rmtree(folder, onerror=writable_then)
    except OSError as exc:
        raise RuntimeError(f"cannot remove {folder}, so cannot start from a clean device: {exc}. "
                           "If a container left it owned by root, chown it through one.") from exc


def filed_as(log: Path) -> Path:
    """A run's log by its device and run (<mac>/runs/<when>/run.log): the same
    before and after the records' folder is renamed around it."""
    return log.relative_to(log.parents[3])


class Instance:
    """One emulated device, its Wi-Fi and its computer."""

    def __init__(self, n: int):
        self.n = n
        # As beken-emu.sh --instance N names them.
        self.device = f"autoflash-beken-emu-{n}"
        self.network = f"autoflash-beken-emu-{n}"
        self.computer = f"autoflash-beken-emu-computer-{n}"
        self.state = folders.state(n)
        self.before: set[Path] = set()   # runs the starting device came with, not this scenario's
        self.env = {key: value for key, value in os.environ.items() if key != "AUTOFLASH_BEKEN_EMU_NETWORK"}
        self.env["AUTOFLASH_BEKEN_EMU_COMPUTER"] = self.computer

    def command(self, *args) -> list[str]:
        return [str(BK_EMU), "--instance", str(self.n), *args]

    def sh(self, *args, timeout=None, check=False):
        return subprocess.run(self.command(*args), env=self.env, capture_output=True, text=True,
                              timeout=timeout, check=check)

    def stop(self) -> None:
        # Long enough for the device to keep its flash as it stops (device.py).
        for name in (self.computer, self.device):
            subprocess.run(["docker", "stop", "-t", "30", name], capture_output=True)
        # And gone (--rm), before a container of the same name is started again.
        for _ in range(30):
            found = subprocess.run(["docker", "ps", "-aq", "--filter", f"name=^({self.computer}|{self.device})$"],
                                   capture_output=True, text=True).stdout.strip()
            if not found:
                return
            time.sleep(1)

    def reset(self, start: str) -> None:
        """The device as `start` (sonoff: the dump, untouched; else saved by prep)."""
        self.stop()
        clear(self.state)
        if start != "sonoff":
            shutil.copytree(SAVED / start, self.state)
        (self.state / "work/captures").mkdir(parents=True, exist_ok=True)
        (self.state / "work/captures/tool.env").write_text(ACCOUNT, encoding="utf-8")
        self.before = {filed_as(path) for path in self.all_runs()}

    def dhcp_log(self) -> str:
        return subprocess.run(["docker", "exec", self.device, "cat", "/run/autoflash-beken-emu/dnsmasq.log"],
                              capture_output=True, text=True).stdout

    def dhcp_names(self) -> list[str]:
        """The names the device asked its router's DHCP for, in order."""
        return [found.group(1) for found in DHCP_NAME.finditer(self.dhcp_log())]

    def lease(self) -> str | None:
        """The device's address: its lease, or - released while it goes on
        using the address, as ESPHome does on rejoining after its network was
        gone - the last its router gave it."""
        found = subprocess.run(["docker", "exec", self.device, "sh", "-c",
                                "sort -n /run/autoflash-beken-emu/leases | tail -1"],
                               capture_output=True, text=True).stdout.split()
        if len(found) > 2:
            return found[2]
        given = DHCP_ADDRESS.findall(self.dhcp_log())
        return given[-1] if given else None

    def answering(self) -> bool:
        address = self.lease()
        if not address:
            return False
        try:
            with socket.create_connection((address, 80), timeout=3):
                return True
        except OSError:
            return False

    def stub_said(self) -> bytes:
        try:
            return (self.state / "stub.log").read_bytes()
        except OSError:
            return b""

    def online(self, since: int = 0) -> bool:
        """Sonoff's firmware in its eWeLink: the time zone it asks once there -
        since the stub's log was `since` bytes long."""
        return b'"TZ":{' in self.stub_said()[since:]

    def start(self, firmware: str, wait: float = 300) -> bool:
        self.sh("device", timeout=600, check=True)
        return self.ready(firmware, wait)

    def ready(self, firmware: str, wait: float = 300) -> bool:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            if (self.online() if firmware == "sonoff" else self.answering()):
                return True
            time.sleep(3)
        return False

    def power(self, how: str) -> None:
        self.sh("power", how, timeout=60, check=True)

    def router(self, how: str) -> None:
        """The home Wi-Fi's router "off" or "on" (beken-emu.sh router)."""
        self.sh("router", how, timeout=60, check=True)

    def tool(self, target: str, log: Path, new: bool = False, more: tuple[str, ...] = ()) -> subprocess.Popen:
        """A run of the tool to `target`, or a readout (target "readout"), on a
        terminal of its own as a person's (tools/repo/unattended_run.py): the
        screen it ends on is printed after it, for FAIL-SCREEN. A readout is
        kept unsent (--no-api), for readout_problems to judge."""
        args = (["--no-api", "computer", "tools/repo/unattended_run.py", "--command", "readout", "--",
                 "--env-file", "captures/tool.env"]
                if target == "readout" else
                ["--no-api", "computer", "tools/repo/unattended_run.py", "--", "--env-file", "captures/tool.env",
                 "--firmware", target] + (["--new"] if new else [])) + list(more)
        out = open(log, "a", encoding="utf-8")
        return subprocess.Popen(self.command(*args), env=self.env, stdout=out, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL)

    def all_runs(self) -> list[Path]:
        """Every run's log in the instance. A device saved by a release before
        3.0.0 has its records in plugs/, until this checkout's tool carries it
        on and renames them."""
        captures = self.state / "work/captures"
        earlier = captures / "plugs"
        records = earlier if earlier.is_dir() and not (captures / "devices").is_dir() else captures / "devices"
        found = []
        for log in records.glob("*/runs/*/run.log"):
            try:  # the tool files a run under its device once it knows the device
                found.append((log.stat().st_mtime, log))
            except FileNotFoundError:
                pass
        return [log for _, log in sorted(found)]

    def runs(self) -> list[Path]:
        """This scenario's runs, oldest first."""
        return [path for path in self.all_runs() if filed_as(path) not in self.before]

    def run_texts(self) -> list[str]:
        """The words of each of this scenario's runs, oldest first."""
        found = []
        for path in self.runs():
            try:
                found.append(path.read_text(encoding="utf-8", errors="replace"))
            except OSError:
                pass   # filed away as it was read
        return found

    def last_outcome(self, since: float) -> dict | None:
        """How the newest run since `since` ended, as it said itself
        (fault_verdicts.run_outcomes): None for none."""
        found = fault_verdicts.run_outcomes(self.state / "work/captures", since)
        return found[-1] if found else None

    def last_state(self) -> dict:
        logs = self.runs()
        try:
            return json.loads((logs[-1].parent / "autoflash-state.json").read_text(encoding="utf-8"))
        except (IndexError, OSError, ValueError):
            return {}

    def last_events(self) -> list[dict]:
        """What the newest run did (output.EVENTS), from its start."""
        logs = self.runs()
        return fault_verdicts.last_run_events(output.events(logs[-1].parent) if logs else [])

    def all_events(self) -> list[dict]:
        """What each of this scenario's runs did, oldest first."""
        return [item for path in self.runs() for item in output.events(path.parent)]

    def last_log(self) -> str:
        logs = self.runs()
        try:
            return logs[-1].read_text(encoding="utf-8", errors="replace") if logs else ""
        except FileNotFoundError:
            return ""   # filed away just now: looked at again next time


# Often enough that a fault timed from a step's start lands where it should: an
# update's send takes about 7 s, so 90% of it leaves under a second.
STEP_POLL = 0.1


def happened(instance: Instance, event: str, **fields) -> bool:
    """Whether the newest run has had `event`, with `fields` (output.EVENTS)."""
    return any(item["event"] == event and all(item.get(k) == v for k, v in fields.items())
               for item in instance.last_events())


def wait_for_step(instance: Instance, step: str, run: subprocess.Popen, limit: float = RUN_LIMIT) -> bool:
    """Until the run starts `step`; False if it ends, or takes too long, first."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and run.poll() is None:
        if happened(instance, "began", step=step):
            return True
        time.sleep(STEP_POLL)
    return False


def asked_to_switch(instance: Instance) -> bool:
    """Whether the newest run's last words told the person to switch the device
    off and on (fault_verdicts.advised_to_switch)."""
    return fault_verdicts.advised_to_switch(instance.last_events())


def not_applied(notes: dict, what: str) -> None:
    notes.setdefault("not_applied", []).append(what)


def running(container: str) -> bool:
    return subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container],
                          capture_output=True, text=True).stdout.strip() == "true"


def must(notes: dict, *command: str, **options) -> subprocess.CompletedProcess:
    """A command that starts a fault, or ends one the run needs ended."""
    done = subprocess.run(command, capture_output=True, text=True, **options)
    if done.returncode:
        not_applied(notes, " ".join(command[:3]))
    return done


# What the device says once it has really stopped (device.py): the switch itself is
# only a file written, which says nothing of whether the device saw it.
SWITCHED_OFF = b"[device] switched off"


def cut_power(instance: Instance, seconds: float, wait: float = 30) -> bool:
    """The device off for `seconds`, and on again; False if it never went off."""
    serial = instance.state / "serial.log"
    try:
        offset = serial.stat().st_size
    except OSError:
        offset = 0
    instance.power("off")
    went_off, deadline = False, time.monotonic() + wait
    while not went_off and time.monotonic() < deadline:
        time.sleep(0.2)
        try:
            with open(serial, "rb") as output:
                output.seek(offset)
                went_off = SWITCHED_OFF in output.read()
        except OSError:
            pass
    time.sleep(seconds)
    instance.power("on")
    return went_off


def inject(instance: Instance, fault: str, arg, notes: dict) -> None:
    if fault == "deviceoff":
        if not cut_power(instance, arg):
            not_applied(notes, "the device switched off")
    elif fault == "outage":
        # The network gone, as the Espressif suite's outage is, the router off:
        # the device's access point off the air, and this computer off it too.
        address = subprocess.run(["docker", "inspect", "-f",
                                  "{{(index .NetworkSettings.Networks \"%s\").IPAddress}}" % instance.network,
                                  instance.computer], capture_output=True, text=True).stdout.strip()
        notes["outage_address"] = address
        if not address:
            not_applied(notes, "the computer's address on the Wi-Fi")
            return
        try:
            instance.router("off")
        except subprocess.CalledProcessError:
            not_applied(notes, "the router switched off")
        must(notes, "docker", "network", "disconnect", instance.network, instance.computer)
        time.sleep(arg)
        try:
            instance.router("on")
        except subprocess.CalledProcessError:
            not_applied(notes, "the router switched on again")
        # A run may end while the computer is off the Wi-Fi: then there is no
        # computer to bring back, and the outage still happened.
        back = subprocess.run(["docker", "network", "connect", "--ip", address, instance.network, instance.computer],
                              capture_output=True, text=True)
        if back.returncode and running(instance.computer):
            not_applied(notes, "docker network connect")
    elif fault == "fallback":
        # The router alone off, long enough for ESPHome to raise its own network.
        try:
            instance.router("off")
            time.sleep(arg)
            instance.router("on")
        except subprocess.CalledProcessError:
            not_applied(notes, "the router switched off and on")
    elif fault == "crash":
        must(notes, "docker", "kill", instance.computer)
    elif fault == "ctrlc":
        must(notes, "docker", "kill", "--signal", "INT", instance.computer)


def recover(instance: Instance, target: str, notes: dict) -> None:
    """What a person does before running the tool again: switch the device off
    and on, where the last run said to."""
    if asked_to_switch(instance):
        instance.power("cycle")
        instance.ready(target, wait=300)
        notes["power_cycles"] = notes.get("power_cycles", 0) + 1


# The bootloader's own lines as it writes an image in (rt_ota), and as it is done.
APPLY_BEGINS = re.compile(rb"OTA firmware\(app\) upgrade\(")
APPLY_PROGRESS = re.compile(rb"OTA Write: \[[=> ]*\] (\d+)%")
APPLY_ENDS = re.compile(rb"ota_erase_dl_rbl erase")


class ApplyCut(threading.Thread):
    """Switch the device off for a second when its bootloader is `at`% of the
    way through writing an image in, whichever run, or restart, that is in."""

    def __init__(self, instance: Instance, at: int):
        super().__init__(daemon=True)
        self.instance, self.at = instance, at
        self.fired: int | None = None
        self.done = threading.Event()

    def run(self) -> None:
        offset, seen, begun = 0, b"", False
        while not self.done.is_set():
            try:
                with open(self.instance.state / "serial.log", "rb") as serial:
                    serial.seek(offset)
                    new = serial.read()
            except OSError:
                new = b""
            offset += len(new)
            seen = seen[-4096:] + new
            # One read may hold an apply's end and the next one's start, and more.
            while True:
                if not begun:
                    start = APPLY_BEGINS.search(seen)
                    if not start:
                        break
                    begun, seen = True, seen[start.end():]
                end = APPLY_ENDS.search(seen)
                for found in APPLY_PROGRESS.finditer(seen[:end.start()] if end else seen):
                    if int(found.group(1)) >= self.at:
                        if cut_power(self.instance, 1):
                            self.fired = int(found.group(1))
                        return
                if not end:
                    break
                begun, seen = False, seen[end.end():]   # over before its moment: the next apply, if any
            time.sleep(0.2)


def readdress(instance: Instance, notes: dict) -> None:
    """The router forgets the device's address and gives it another when it next asks."""
    address = instance.lease() or ""
    last = int(address.rsplit(".", 1)[1]) if address else 50
    must(notes, "docker", "exec", instance.device, "/run/autoflash-beken-emu/router", "55" if last < 55 else "50", timeout=60)
    notes["moved_from"] = address


def not_fired(fault: str, notes: dict) -> bool:
    """A device converted though its fault never happened proves nothing."""
    return bool(notes.get("not_applied")
                or (fault == "fallback" and not notes.get("joined_own_network"))
                or (fault == "double" and not notes.get("second_cut"))
                or (fault == "readdress" and not (notes.get("moved_from") and notes.get("moved_to")
                                                  and notes["moved_from"] != notes["moved_to"]))
                or (fault == "ctrlc" and not notes.get("ctrlc_seen")))


def wait_for(run: subprocess.Popen, seen: Callable[[], bool], limit: float = RUN_LIMIT) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline and run.poll() is None:
        if seen():
            return True
        time.sleep(0.5)
    return False


def finish(run: subprocess.Popen, instance: Instance) -> None:
    try:
        run.wait(timeout=RUN_LIMIT)
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", instance.computer], capture_output=True)


def start_of(route: str) -> str:
    """The saved device a route starts from: an update's is for its firmware."""
    source, target = route.split(">")
    return f"outdated-{target}" if source == "outdated" else source


JOIN_AGAIN_EVERY = 15.0   # seconds between tries of a join that has not taken, while the run asks


class Person(threading.Thread):
    """As a person at the computer: joins the device's own network when a run
    says to - again while the run still waits, should a try not take - and goes
    back to the home network when it says that, from the computer's Wi-Fi
    menu (computer/bin/wifi). What it did goes in `notes`. What the run asks it
    reads from the run's events (output.EVENTS: join, joined, home)."""

    def __init__(self, instance: Instance, notes: dict):
        super().__init__(daemon=True)
        self.instance, self.notes = instance, notes
        self.done = threading.Event()
        self.acted: dict[Path, tuple[int, int]] = {}   # each run's joins asked and homes told, done
        self.tried = -JOIN_AGAIN_EVERY                  # when a join was last tried

    def wifi(self, *args: str) -> bool:
        """`wifi ARGS` on the computer, as your user with its Wi-Fi's rights."""
        script = ('exec setpriv --reuid="$HOST_UID" --regid="$HOST_GID" --clear-groups '
                  '--inh-caps +net_admin,+net_raw --ambient-caps +net_admin,+net_raw '
                  '/work/tools/beken/emulator/computer/bin/wifi "$@"')
        try:
            done = subprocess.run(["docker", "exec", self.instance.computer, "sh", "-c", script, "wifi", *args],
                                  capture_output=True, text=True, timeout=120)
        except subprocess.TimeoutExpired:
            return False
        return done.returncode == 0

    def run(self) -> None:
        while not self.done.wait(2):
            self.look()

    def look(self) -> None:
        """One look at what the newest run asks, done."""
        logs = self.instance.runs()
        if not logs:
            return
        found = [item["event"] for item in output.events(logs[-1].parent)]
        asked, told = found.count("join"), found.count("home")
        joined, homed = self.acted.get(logs[-1], (0, 0))
        if told > homed:
            self.wifi("home")
            self.acted[logs[-1]] = (asked, told)
        elif asked > joined:
            if "joined" in found[len(found) - found[::-1].index("join"):]:
                self.acted[logs[-1]] = (asked, homed)
            elif time.monotonic() - self.tried >= JOIN_AGAIN_EVERY:
                self.tried = time.monotonic()
                if self.wifi("join"):
                    self.notes["joined_own_network"] = self.notes.get("joined_own_network", 0) + 1


def run_scenario(instance: Instance, sc: dict) -> dict:
    source, target = sc["route"].split(">")
    folder = OUT / "logs" / sc["id"].replace(":", "_").replace(">", "-")
    keep_earlier(folder)
    folder.mkdir(parents=True)
    log = folder / "tool.log"
    started, began = time.monotonic(), time.time()
    notes: dict = {}
    memory = MemoryWatch()
    memory.start()
    instance.reset(start_of(sc["route"]))
    if not instance.start(source):
        notes["memory"] = memory.note()
        return dict(result="NOT-READY", notes=notes)
    person = Person(instance, notes)
    person.start()
    if target == "readout":
        outcome = readout_scenario(instance, sc, log, notes)
    elif sc["step"] == "apply":
        outcome = apply_scenario(instance, sc, target, log, notes)
    elif sc["fault"] == "badimage":
        outcome = bad_image_scenario(instance, sc, target, log, notes)
    else:
        outcome = step_scenario(instance, sc, target, log, notes)
    person.done.set()
    person.join(10)
    if outcome["result"] in ("PASS", "FAIL") and fault_verdicts.unexpected(
            instance.run_texts(), fault_verdicts.run_outcomes(instance.state / "work/captures", began)):
        # As the Espressif suite: a run stopped by an error the tool did not see
        # coming is a bug, whatever the fault, though the next run converts.
        outcome = dict(outcome, result="FAIL-UNEXPECTED")
    # Asked while it runs: a route through J2 stops it below, for its flash.
    release = release_of(instance, target) if outcome["result"] == "PASS" and target in LATEST else None
    if outcome["result"] == "PASS" and target != "readout":
        names = instance.dhcp_names()
        flash = None
        if "J2" in route_steps(sc["route"]):
            instance.stop()   # its flash kept as it stops
            flash = (instance.state / "flash.bin").read_bytes()
        notes["dhcp_names"] = names
        problems = interim_problems(sc["route"], names, flash)
        if problems:
            outcome = dict(outcome, result="FAIL")
            notes["interim"] = problems
        checks = fault_verdicts.checks_problem(instance.last_log(), target, metered(instance.last_state()),
                                               instance.last_outcome(began))
        if checks and not problems:
            outcome = dict(outcome, result="FAIL-CHECKS")
            notes["checks"] = checks
        if target in LATEST and release != LATEST[target] and outcome["result"] == "PASS":
            # Converted, but not on this checkout's release: an update from an
            # earlier one, or a conversion that release began, left behind.
            outcome = dict(outcome, result="FAIL-NOT-LATEST")
            notes["release"] = f"{target} {release}"
    if outcome["result"] == "PASS" and log.exists():
        screen = fault_verdicts.screen_problems(log.read_text(encoding="utf-8", errors="replace"))
        if screen:
            outcome = dict(outcome, result="FAIL-SCREEN")
            notes["screen"] = screen
    for index, path in enumerate(instance.runs()):
        shutil.copy(path, folder / f"run-{index + 1}.log")
        if (path.parent / output.EVENTS).exists():
            shutil.copy(path.parent / output.EVENTS, folder / f"run-{index + 1}.{output.EVENTS}")
    # Every address the router gave the device, and any it gave back (lease).
    (folder / "dhcp.log").write_text(instance.dhcp_log(), encoding="utf-8")
    if log.exists():   # every run's recording: what each stage showed on the way
        fault_verdicts.keep_recordings(log.read_text(encoding="utf-8", errors="replace"),
                                       instance.state / "work", folder)
    try:
        shutil.copy(instance.state / "serial.log", folder / "serial.log")
    except OSError:
        pass
    notes["memory"] = memory.note()
    return dict(**outcome, notes=notes, seconds=round(time.monotonic() - started))


def release_of(instance: Instance, firmware: str) -> str | None:
    """The release of this project's `firmware` the device runs: OpenBeken's, as
    its build string says (openbeken.version); ESPHome's, as its API says
    (project_version).  None where it does not answer as that.  A few tries:
    it may be starting."""
    for _ in range(3):
        address = instance.lease()
        if address and firmware == "openbeken":
            found = openbeken.info(address)
            if found:
                return openbeken.version(found)
        elif address:
            info = esphome.api_device_info(address)
            if info:
                if info["project_name"] != kinds.SONOFF_S61.esphome_project:
                    return None   # not this project's ESPHome
                return str(info["project_version"])
        time.sleep(5)
    return None


def apply_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    cut = ApplyCut(instance, sc["at"])
    cut.start()
    try:
        runs = 0
        while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS + 1:
            if runs:
                recover(instance, target, notes)
            finish(instance.tool(target, log, new=not runs), instance)
            runs += 1
    finally:
        cut.done.set()
        cut.join(5)
    notes["cut_at"] = cut.fired
    if cut.fired is None:
        return dict(result="NO-STEP", runs=runs)
    return dict(result="PASS" if converted(instance.last_state(), sc["route"]) else "FAIL", runs=runs)


def bad_image_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    """The step's image sent spoilt, once - each run is asked, as a route may
    reach the step only after a restart, and the tool spoils it once a run's
    record - and runs again, as a person would, until the device is converted:
    which must come from a run that saw the image was not installed and sent
    it again."""
    spoil = ("--simulate-bad-image",
             f"{sc['step']}:{SPOILT_PAST_LIMIT}" if sc["arg"] == "restart" else sc["step"])
    finish(instance.tool(target, log, new=True, more=spoil), instance)
    runs = 1
    while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS:
        recover(instance, target, notes)
        finish(instance.tool(target, log, more=spoil), instance)
        runs += 1
    # The image found not installed, and sent again (errors.GoBack, output.EVENTS).
    again = [item for item in instance.all_events() if item["event"] == "again" and item.get("step") == sc["step"]]
    notes["sent_again"] = bool(again)
    ok = converted(instance.last_state(), sc["route"]) and notes["sent_again"]
    if sc["arg"] == "restart":
        # Converted by the run that restarted the device, not by a person's run after.
        notes["restarted"] = any(item.get("restarted") for item in again)
        ok = ok and notes["restarted"] and runs == 1
    return dict(result="PASS" if ok else "FAIL", runs=runs)


def cut_as_sent_again(instance: Instance, run: subprocess.Popen, sc: dict, notes: dict) -> bool:
    """A double's second cut, as the run under way sends its command again: one
    that does converts by itself, and no run again would get the cut."""
    def resent() -> int:   # the command sent again (tools/ewelink/ota_push, output.EVENTS)
        return sum(item["event"] == "resent" for item in instance.last_events())
    before = resent()
    if not wait_for(run, lambda: resent() > before):
        return False
    time.sleep(ROUTES[sc["route"]][sc["step"]] * sc["at"] / 100)
    inject(instance, "deviceoff", 1, notes)
    notes["second_cut"] = True
    return True


def step_scenario(instance: Instance, sc: dict, target: str, log: Path, notes: dict) -> dict:
    run = instance.tool(target, log, new=True)
    # A run a fault ended reaches its later steps only through runs again, with
    # the switch off and on the tool asks for, as a person would.
    reruns = 0
    while not wait_for_step(instance, sc["step"], run):
        run.wait(timeout=60)
        reruns += 1
        if reruns > RECOVERY_RUNS or converted(instance.last_state(), sc["route"]):
            return dict(result="NO-STEP")
        recover(instance, target, notes)
        run = instance.tool(target, log)
    if sc["fault"] == "readdress":
        if not wait_for(run, lambda: happened(instance, "done", step=sc["step"])):
            notes["mistimed"] = True
        readdress(instance, notes)
    else:
        time.sleep(ROUTES[sc["route"]][sc["step"]] * sc["at"] / 100)
        if happened(instance, "done", step=sc["step"]):
            notes["mistimed"] = True
        fault, arg = ("deviceoff", 1) if sc["fault"] == "double" else (sc["fault"], sc["arg"])
        inject(instance, fault, arg, notes)
        if fault == "crash":
            run.wait(timeout=60)
            time.sleep(arg)
        if sc["fault"] == "double":
            cut_as_sent_again(instance, run, sc, notes)
    finish(run, instance)
    second = sc["fault"] == "double" and not notes.get("second_cut")
    runs = 1
    while not converted(instance.last_state(), sc["route"]) and runs <= RECOVERY_RUNS:
        recover(instance, target, notes)
        run = instance.tool(target, log)
        if second and wait_for_step(instance, sc["step"], run):
            # The second cut, as the tool sends the step again.
            time.sleep(ROUTES[sc["route"]][sc["step"]] * sc["at"] / 100)
            inject(instance, "deviceoff", 1, notes)
            notes["second_cut"] = True
            second = False
        finish(run, instance)
        runs += 1
    if sc["fault"] == "readdress":
        notes["moved_to"] = instance.lease()
    if sc["fault"] == "ctrlc":
        notes["ctrlc_seen"] = any(CTRL_C_LOGGED in path.read_text(encoding="utf-8", errors="replace")
                                  for path in instance.runs())
    ok = converted(instance.last_state(), sc["route"])
    if notes.get("mistimed"):
        return dict(result="MISTIMED", runs=runs)
    if ok and not_fired(sc["fault"], notes):
        return dict(result="NOT-FIRED", runs=runs)
    return dict(result="PASS" if ok else "FAIL", runs=runs)


def readout_problems(instance: Instance) -> list[str] | None:
    """What the newest readout.tar this scenario's runs kept lacks
    (fault_verdicts.readout_problems), or None when none kept one."""
    kept = [path.parent / READOUT for path in instance.runs() if (path.parent / READOUT).is_file()]
    if not kept:
        return None
    return fault_verdicts.readout_problems(kept[-1].read_bytes(), "beken")


def settle(instance: Instance, since: int, wait: float = 120) -> None:
    """Until the device has started on Sonoff's firmware (in eWeLink since the
    stub's log was `since` long) or on OpenBeken (answering), or `wait` runs out."""
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline and not (instance.online(since) or instance.answering()):
        time.sleep(3)


def sonoff_works(instance: Instance, wait: float = 300) -> bool:
    """Whether the device, switched off and on, starts Sonoff's firmware and is
    back in eWeLink: on its Sonoff firmware, and working."""
    since = len(instance.stub_said())
    instance.power("cycle")
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if instance.online(since):
            return not instance.answering()
        time.sleep(3)
    return False


def readout_scenario(instance: Instance, sc: dict, log: Path, notes: dict) -> dict:
    """A readout of the device, with the fault in the phase under test; then
    what a person does, up to RECOVERY_RUNS times: switch the device off and on
    where the run said to, put Sonoff's firmware back with the tool where the
    device was left on OpenBeken, else read it out again - until a whole readout
    is kept. It passes when one is, and the device works on Sonoff's firmware."""
    if sc["route"].split(">")[0] != "sonoff":
        return converted_readout(instance, log, notes)
    run = instance.tool("readout", log)
    if sc["fault"] != "none":
        begins, ends, length = READOUT_PHASES[sc["step"]]
        if not wait_for(run, lambda: happened(instance, "phase", phase=begins)):
            finish(run, instance)
            return dict(result="NO-STEP", runs=1)
        time.sleep(length * sc["at"] / 100)
        if happened(instance, "phase", phase=ends):
            notes["mistimed"] = True
        inject(instance, sc["fault"], sc["arg"], notes)
        if sc["fault"] == "crash":
            run.wait(timeout=60)
            time.sleep(sc["arg"])
    finish(run, instance)
    runs = 1
    while readout_problems(instance) != [] and runs <= RECOVERY_RUNS:
        since = len(instance.stub_said())
        recover(instance, "sonoff", notes)
        settle(instance, since)
        again = "sonoff" if instance.answering() else "readout"
        notes.setdefault("then", []).append(again)
        finish(instance.tool(again, log), instance)
        runs += 1
    problems = readout_problems(instance)
    notes["readout"] = problems if problems is not None else ["none kept"]
    if notes.get("mistimed"):
        return dict(result="MISTIMED", runs=runs)
    if not_fired(sc["fault"], notes):
        return dict(result="NOT-FIRED", runs=runs)
    if problems != []:
        return dict(result="FAIL-READOUT", runs=runs)
    return dict(result="PASS" if sonoff_works(instance) else "FAIL-NOT-SONOFF", runs=runs)


def converted_readout(instance: Instance, log: Path, notes: dict) -> dict:
    """A readout started on a device on OpenBeken: its first stage alone
    (FAIL-READOUT), ending with no failure (FAIL-EXIT), nothing served to the
    device (FAIL-SENT), which is left answering on its firmware (FAIL-DISTURBED)."""
    run = instance.tool("readout", log)
    finish(run, instance)
    said = instance.last_log()
    notes["said"] = (re.findall(r"FAILED: (.*)", said) or ["nothing"])[-1]
    kept = [path.parent / READOUT for path in instance.runs() if (path.parent / READOUT).is_file()]
    problems = fault_verdicts.first_stage_problems(kept[-1].read_bytes(), "openbeken") if kept else None
    notes["readout"] = problems if problems is not None else ["none kept"]
    if happened(instance, "phase", phase="serving"):   # the readout firmware, or Sonoff's
        return dict(result="FAIL-SENT", runs=1)
    if run.returncode != 0:
        return dict(result="FAIL-EXIT", runs=1)
    if notes["readout"]:
        return dict(result="FAIL-READOUT", runs=1)
    return dict(result="PASS" if instance.answering() else "FAIL-DISTURBED", runs=1)


def all_passed(ids_file: str | None = None) -> bool:
    """Whether every scenario of `ids_file` (else every one) has passed last."""
    wanted = Path(ids_file).read_text(encoding="utf-8").split() if ids_file else [sc["id"] for sc in scenarios()]
    passed = {i for i, entry in latest().items() if entry.get("result") == "PASS"}
    return all(i in passed for i in wanted)


def record(sc: dict, outcome: dict) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    with RESULTS.open("a", encoding="utf-8") as results:
        results.write(json.dumps({"id": sc["id"], "at": time.strftime("%Y-%m-%dT%H:%M:%S"), **outcome}) + "\n")


def latest() -> dict[str, dict]:
    found: dict[str, dict] = {}
    try:
        for line in RESULTS.read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            found[entry["id"]] = entry
    except (OSError, ValueError):
        pass
    return found


def starved_label(outcome: dict) -> str:
    starved = outcome.get("result") != "PASS" and outcome.get("notes", {}).get("memory", {}).get("starved")
    return "  (memory-starved)" if starved else ""


def batch(ids_file: str | None = None) -> None:
    every = {sc["id"]: sc for sc in scenarios()}
    wanted = (Path(ids_file).read_text(encoding="utf-8").split() if ids_file else list(every))
    earlier = latest()   # those run here before: a re-run's pass rate is apart (emulator_stats)
    passed = {i for i, entry in earlier.items() if entry.get("result") == "PASS"}
    todo: queue.Queue = queue.Queue()
    # The longest first, so the batch does not end on one started last (scenario_times).
    for sc in scenario_times.longest_first([every[i] for i in wanted if i in every and i not in passed],
                                           scenario_times.times(*suite_roots())):
        todo.put(sc)
    workers = int(os.environ.get("AUTOFLASH_BEKEN_EMU_WORKERS", "2"))
    lock = threading.Lock()

    def work(n: int) -> None:
        instance = Instance(n)
        try:
            while True:
                try:
                    sc = todo.get_nowait()
                except queue.Empty:
                    return
                cpu_load.wait_for_room(lambda line: print(line, flush=True))
                started = time.time()
                try:
                    outcome = run_scenario(instance, sc)
                except Exception as exc:  # noqa: BLE001 - one scenario's failure is recorded, not the batch's
                    # Where it was raised too, as the Espressif suite keeps it: the error alone
                    # (a FileNotFoundError, say) does not tell which file.
                    outcome = dict(result="ERROR", notes={"error": repr(exc), "trace": traceback.format_exc()})
                with lock:
                    record(sc, outcome)
                    print(f"{outcome['result']:9} {sc['id']}{starved_label(outcome)}", flush=True)
                # The device's eWeLink is always the local stub (ACCOUNT).
                emulator_stats.fault_run(OUT, family=STATS_DEVICE.family, device=STATS_DEVICE.key, scenario=sc,
                                         verdict=outcome["result"], tries=outcome.get("runs"),
                                         seconds=round(time.time() - started), cloud="stub", workers=workers,
                                         rerun=sc["id"] in earlier,
                                         outcomes=fault_verdicts.run_outcomes(instance.state / "work/captures",
                                                                              started),
                                         error=outcome.get("notes", {}).get("error"),
                                         starved=outcome.get("notes", {}).get("memory", {}).get("starved"))
        finally:
            instance.stop()   # an emulator never left running

    emulator_stats.began(OUT, family=STATS_DEVICE.family, suite="fault", device=STATS_DEVICE.key, total=len(every),
                         planned=todo.qsize(), cloud="stub", workers=workers,
                         tier=emulator_stats.tier(wanted, every, (sc["id"] for sc in scenarios(quick=True))))
    threads = [threading.Thread(target=work, args=(n,)) for n in range(1, workers + 1)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(emulator_stats.send(OUT), flush=True)


def prep() -> None:
    """The starting devices, converted by the tool from the dump: on the interim
    firmware (a run to ESPHome stopped once that has started), on OpenBeken,
    then on ESPHome, each saved with the captures its runs left."""
    instance = Instance(1)
    try:
        instance.reset("sonoff")
        if not instance.start("sonoff"):
            raise SystemExit("the device did not come online")
        log = OUT / "prep-interim.log"
        OUT.mkdir(parents=True, exist_ok=True)
        run = instance.tool("esphome", log, new=True)
        if not wait_for_step(instance, "B4", run):
            raise SystemExit(f"the device did not start the interim firmware: see {log}")
        subprocess.run(["docker", "kill", instance.computer], capture_output=True)
        save(instance, "interim")
        instance.reset("sonoff")
        if not instance.start("sonoff"):
            raise SystemExit("the device did not come online")
        for target, route in (("openbeken", "sonoff>openbeken"), ("esphome", "openbeken>esphome")):
            log = OUT / f"prep-{target}.log"
            OUT.mkdir(parents=True, exist_ok=True)
            for attempt in range(RECOVERY_RUNS):
                instance.tool(target, log, new=attempt == 0).wait(timeout=RUN_LIMIT)
                if converted(instance.last_state(), route):
                    break
                if asked_to_switch(instance):
                    instance.power("cycle")
                    instance.ready(target)
            else:
                raise SystemExit(f"the device was not converted to {target}: see {log}")
            save(instance, target)
            instance.start(target)
    finally:
        instance.stop()


def save(instance: Instance, firmware: str) -> None:
    instance.stop()
    clear(SAVED / firmware)
    shutil.copytree(instance.state, SAVED / firmware, ignore=keep_for_saving)
    print(f"saved the device on {firmware} in {SAVED / firmware}", flush=True)


def prep_update() -> None:
    """The update scenarios' starting devices: on OpenBeken and on ESPHome as
    EARLIER_RELEASE converts them, which no run of this checkout can make. That
    release's own suite makes them, from its tagged files: in its tree, its
    folders are named as that release names them (v2.3.0's after the S61)."""
    tree = folders.LEGACY / EARLIER_RELEASE
    clear(tree)
    (tree / "captures/emulation").mkdir(parents=True)
    archive = subprocess.run(["git", "-C", str(REPO), "archive", EARLIER_RELEASE], capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
    shutil.copytree(folders.FIRMWARES, tree / EARLIER_FIRMWARES)
    try:
        subprocess.run([str(tree / "tools/beken/emulator/fault_suite.py"), "prep"], check=True)
        for firmware in ("openbeken", "esphome"):
            kept = SAVED / f"outdated-{firmware}"
            clear(kept)
            shutil.copytree(tree / EARLIER_SAVED / firmware, kept)
            print(f"saved the device on {EARLIER_RELEASE}'s {firmware} in {kept}", flush=True)
    finally:
        clear(tree)


# The check: two ways, each a final firmware installed, then that device taken to
# the other, as tools/espressif/emulator/public_devices.py's for the S60.
CHECK_WAYS = (("sonoff>openbeken", "openbeken>esphome"), ("sonoff>esphome", "esphome>openbeken"))


def check_way(n: int, routes: tuple[str, ...]) -> int:
    instance = Instance(n)
    OUT.mkdir(parents=True, exist_ok=True)
    try:
        instance.reset("sonoff")
        if not instance.start("sonoff"):
            print(f"FAIL  {routes[0]}: the device did not come online", flush=True)
            return 1
        for route in routes:
            started = time.time()
            target = route.split(">")[1]
            log = OUT / f"check-{route.replace('>', '-')}.log"
            instance.before = {filed_as(path) for path in instance.all_runs()}
            for attempt in range(RECOVERY_RUNS):
                instance.tool(target, log, new=attempt == 0).wait(timeout=RUN_LIMIT)
                if converted(instance.last_state(), route):
                    break
                if asked_to_switch(instance):
                    instance.power("cycle")
                    instance.ready(target)
            problems = [] if converted(instance.last_state(), route) else ["not converted"]
            problems += interim_problems(route, instance.dhcp_names(), None)
            checks = fault_verdicts.checks_problem(instance.last_log(), target, metered(instance.last_state()),
                                                   instance.last_outcome(started))
            if checks:
                problems.append(checks)
            print(f"{'FAIL' if problems else 'PASS'}  {route}{': ' + ', '.join(problems) if problems else ''}", flush=True)
            emulator_stats.check_run(OUT, family=STATS_DEVICE.family, device=STATS_DEVICE.key, route=route, passed=not problems,
                                     seconds=round(time.time() - started), cloud="stub")
            if problems:
                return 1
            instance.stop()
            if not instance.start(target):
                print(f"FAIL  {route}: the device did not start again on {target}", flush=True)
                return 1
        return 0
    finally:
        instance.stop()


def check(instances: tuple[int, ...] = (1, 2)) -> int:
    """The final firmware of every way, side by side, each on its emulator of
    `instances`: no faults, no account. A way passes only by getting to its
    end: one whose thread raised has failed."""
    failed = [1] * len(CHECK_WAYS)
    emulator_stats.began(OUT, family=STATS_DEVICE.family, suite="check", device=STATS_DEVICE.key,
                         total=sum(map(len, CHECK_WAYS)), planned=sum(map(len, CHECK_WAYS)), cloud="stub", workers=2)

    def way(index: int) -> None:
        try:
            failed[index] = check_way(instances[index], CHECK_WAYS[index])
        except Exception as exc:  # noqa: BLE001 - said, and the way failed
            print(f"FAIL  {' then '.join(CHECK_WAYS[index])}: {exc!r}", flush=True)
    threads = [threading.Thread(target=way, args=(index,)) for index in range(len(CHECK_WAYS))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    print(emulator_stats.send(OUT), flush=True)
    return 1 if any(failed) else 0


def keep_for_saving(folder: str, names: list[str]) -> list[str]:
    """What a saved device leaves out: its logs and power switch, its stub's
    certificates (for its own network's router: each instance makes its own),
    the backup B4 made (so a route that takes B4 has it to do), and of its
    computer's copy of the checkout all but the captures its runs left."""
    if Path(folder).name == "work":
        return [name for name in names if name != "captures"]
    return [name for name in names if name.endswith(".log")
            or name in ("power", "certs.part", "certs-for", "ca.crt", "leaf.crt", "leaf.key", "sonoff-backup")]


def suite_roots() -> list[Path]:
    """This suite's folders here, and those kept from other machines."""
    return [folders.BEKEN, *scenario_times.kept(folders.EMU, folders.BEKEN.name)]


def report() -> None:
    found = latest()
    for sc in scenarios():
        entry = found.get(sc["id"])
        print(f"{(entry or {}).get('result', '-'):9} {sc['id']}{starved_label(entry or {})}")


def main(argv: list[str]) -> int:
    command = argv[0] if argv else ""
    for line in folders.migrate():
        print(line, file=sys.stderr)
    if command == "prep":
        prep()
    elif command == "prep-update":
        prep_update()
    elif command == "check":
        import argparse
        parser = argparse.ArgumentParser(prog="fault_suite.py check")
        parser.add_argument("--instances", default="1,2", help="the two emulators to use (default 1,2)")
        numbers = parser.parse_args(argv[1:]).instances.split(",")
        if len(numbers) != len(CHECK_WAYS) or not all(number.isdigit() for number in numbers):
            parser.error(f"--instances needs {len(CHECK_WAYS)} numbers, such as 3,4")
        return check(tuple(map(int, numbers)))
    elif command == "list":
        if argv[1:] not in ([], ["openbeken"], ["quick"]):
            print("list takes openbeken or quick", file=sys.stderr)
            return 2
        for i in (OPENBEKEN_CHECK if argv[1:] == ["openbeken"] else
                  [sc["id"] for sc in scenarios(quick=argv[1:] == ["quick"])]):
            print(i)
    elif command == "batch":
        batch(argv[1] if len(argv) > 1 else None)
        return 0 if all_passed(argv[1] if len(argv) > 1 else None) else 1
    elif command == "report":
        report()
    elif command == "found":
        for line in fault_yield.found(fault_yield.rows(*suite_roots())):
            print(line)
    elif command == "share":
        # share IDS WORKERS... [--times-from DIR]..., as the Espressif suite's
        import argparse
        parser = argparse.ArgumentParser(prog="fault_suite.py share")
        parser.add_argument("file")
        parser.add_argument("workers", nargs="+", type=int)
        parser.add_argument("--times-from", action="append", default=[], type=Path, metavar="DIR")
        args = parser.parse_args(argv[1:])
        try:
            for line in scenario_times.write_shares(args.file, args.workers, scenarios(),
                                                    scenario_times.times(*suite_roots(), *args.times_from)):
                print(line)
        except ValueError as exc:
            parser.error(str(exc))
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
