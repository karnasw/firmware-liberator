#!/usr/bin/env python3
"""The fault suite: every direction, every step that writes to or waits on the
device, every fault - on emulators side by side, on one machine or several
(docs/testing.md).

    fault_suite.py --device MAC prep        the starting devices (needs the cloud)
    fault_suite.py list                   every scenario, and whether it needs the cloud
    fault_suite.py list quick             those of a routine run (QUICK_OUTAGES)
    fault_suite.py --device MAC batch [FILE]
                                          the scenarios in FILE (one id a line; every
                                          scenario without), skipping those passed
    fault_suite.py report                 the latest result of each
    fault_suite.py found                  what each fault and length has found, in the
                                          results here and kept (tools/repo/fault_yield.py)

Add --stub-cloud to prep/batch to run against a local eWeLink stub per emulator
(emu.sh --stub-cloud): fully offline, no cloud lock, every scenario in parallel
on AUTOFLASH_EMU_STUB_WORKERS emulators (from AUTOFLASH_EMU_FIRST_INSTANCE,
default 1). Without it the real cloud is used and the cloud scenarios take
turns, as below.

Each scenario: a conversion from a saved device; when the step under test has
been going for a share of its usual time (40%, or 10% and 90% at the riskiest
steps), the fault; then the tool run again, as a person would - switching the
device off and on, or joining its own network, where the run says to - until
the device is converted, or four runs fail. Every scenario also checks the fault
landed while its step was under way (MISTIMED if not), that the run left
no firewall rule and no helper behind, and that the converting run's report
says its end-of-run checks passed (FAIL-CHECKS if not: tools/repo/fault_verdicts.py),
and that each run of this checkout's tool left one line a stage on its screen,
none of a try that failed (FAIL-SCREEN if not).
And readouts (readout_scenarios, go_readout): of a device fresh from Sonoff's
firmware, faulted in the read and as Sonoff's firmware is put back, until a
whole readout.tar is kept and the device is back on Sonoff's; of a device on
Tasmota, which must read its first stage alone; and of every device in an account
(readout_all_scenarios, go_readout_all), on the stub, each sent to the API's
own code (in a container beside the emulator) and judged by what it
answered and kept.

How many at once, from the environment:
    AUTOFLASH_EMU_CLOUD_WORKERS     emulators for the scenarios that need eWeLink (from
                                    instance 1); each run and its device use the cloud only
                                    in its turn, and are side by side otherwise. Six
                                    unless set, or one per core on a computer with fewer
    AUTOFLASH_EMU_CLOUD_MOST        try up to this many, one more at a time while they
                                    pass
    AUTOFLASH_EMU_CLOUD_BACK_OFF=1  start at AUTOFLASH_EMU_CLOUD_WORKERS and drop one at a
                                    time whenever two of the last four fail
    AUTOFLASH_EMU_CLOUD_PAUSE_UP=1  start at AUTOFLASH_EMU_CLOUD_COOL_DOWN and add 5 s
                                    whenever two of the last four fail with the cloud to
                                    blame
    AUTOFLASH_EMU_CLOUD_COOL_DOWN   seconds between two devices' turns at the cloud (5)
    AUTOFLASH_EMU_ANY_WORKERS       emulators for the rest, after those
    AUTOFLASH_EMU_CONNECTION_LOG    seconds between looks at every device's connections,
                                    logged to connections.log on the real cloud (2; 0 for
                                    none)
    AUTOFLASH_EMU_MOST_WORKERS      start up to this many of those instead, of which the
                                    number in <AUTOFLASH_EMU_SUITE_DIR>/workers take
                                    scenarios when that file is there: a batch scaled
                                    while it runs
    AUTOFLASH_EMU_NO_CLOUD=1        a machine whose devices never reach the internet: only
                                    the scenarios that do not need it
    AUTOFLASH_EMU_SUITE_DIR         where results go (captures/emulation/suite; with
                                    --stub-cloud, captures/emulation/suite-stub)

Results: results.jsonl, a log per scenario, and what a failure leaves (its
run records and the device's serial output). A fault that never happened is
NOT-FIRED, not a pass; NO-INTERNET when it never happened because this computer
could not reach eWeLink, which says nothing of the tool or the device, and is run
again. Each row's notes say how short of memory the computer
was; a failure while it was starved prints "memory-starved", as the device's
connections can then fail with nothing wrong.
"""
import hashlib
import heapq
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
from tools.autoflash import kinds, output, records  # noqa: E402
from tools.autoflash.output import CTRL_C_LOGGED  # noqa: E402
from tools.autoflash.report import SONOFF_IDENTITY  # noqa: E402
from tools.repo import cpu_load, emulator_stats, fault_verdicts, fault_yield, scenario_times  # noqa: E402
from tools.repo.earlier_attempts import keep_earlier  # noqa: E402
from tools.repo.memory_pressure import LOW_MEMORY_KB, MemoryWatch, memory_now  # noqa: E402
from tools.espressif import layouts  # noqa: E402
from tools.espressif.emulator.patch_stock import holds_rsa_ca, trusts  # noqa: E402
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_TASMOTA_VERSION  # noqa: E402
from tools.autoflash.routes import espressif as espressif_routes  # noqa: E402
EMU = REPO / "captures/emulation"
OUT = Path(os.environ.get("AUTOFLASH_EMU_SUITE_DIR", EMU / "suite"))
RESULTS = OUT / "results.jsonl"
LOGS = OUT / "logs"
# A machine whose devices never get a turn at the cloud.
NO_CLOUD = os.environ.get("AUTOFLASH_EMU_NO_CLOUD") == "1"


def cloud_workers():
    """How many emulators take the scenarios that need eWeLink: six, as their
    turns at eWeLink come one at a time and 8 went no faster, and
    no more than one per core, or they fail falsely. A computer whose memory is
    mostly taken holds fewer (docs/testing.md)."""
    default = 0 if NO_CLOUD else min(6, os.cpu_count() or 1)
    return int(os.environ.get("AUTOFLASH_EMU_CLOUD_WORKERS", default))


# Each emulator serves its own local eWeLink stub (emu.sh --stub-cloud): the
# devices never share a cloud, so nothing is serialised and every scenario can run
# in parallel. Set by --stub-cloud; see docs/cloud-stub.md.
STUB_CLOUD = os.environ.get("AUTOFLASH_EMU_STUB_CLOUD") == "1"


def records_under(root):
    """The tool's records under `root`/captures: devices/, or plugs/ where a
    release before 3.0.0 keeps them - a legacy run's until this checkout's
    tool carries it on and renames them, or a device saved by one."""
    captures = Path(root) / "captures"
    earlier = captures / "plugs"
    return earlier if earlier.is_dir() and not (captures / "devices").is_dir() else captures / "devices"


def filed_as(log):
    """A run's log by its device and run (<mac>/runs/<when>/run.log): the same
    before and after the records' folder is renamed around it."""
    return log.relative_to(log.parents[3])


def saved(name):
    """A saved starting device's name. A stub run's are kept apart from the real
    cloud's: their flash is the other kind (emu.sh), so each would be refused,
    or replaced, by a batch of the other. Each device has its own (a test copy
    under another MAC: tools/espressif/emulator/copy_device.py): one saved from another
    device - its run records filed under that device's MAC - is that device's, and
    this one's goes beside it, its MAC's last four digits after the name."""
    base = f"stub-{name}" if STUB_CLOUD else name
    devices_saved = records_under(EMU / "saved" / base)
    owners = {d.name for d in devices_saved.iterdir() if d.name != "unidentified"} if devices_saved.is_dir() else set()
    if MAC and owners and MAC.replace(":", "-").lower() not in owners:
        return f"{base}-{MAC.replace(':', '').lower()[-4:]}"
    return base


MAC = None  # --device: the backed-up device the emulators run copies of
DEVICE_IP = "192.168.60.50"
DEVICE_URL = f"http://{DEVICE_IP}"
# An MQTT host for a device set up by its owner (notemplate): nobody's (RFC 5737).
NOTEMPLATE_MQTT_HOST = "192.0.2.10"
APP0_OFFSET = 0xE0000  # Tasmota's own firmware, on its layout (tools/espressif/safeboot/safeboot_migration.py)
DEVICE_CMD = "/opt/autoflash-emu/bin/device"
ESPHOME_OTA_PORT = 3232
MAX_RUNS = 4
TOOL = "tools/autoflash/cli.py"
HELPERS = ("serve_safeboot_migration.py", "serve_tasmota_ota.py")
# What a run's log says as it gets to where a fault is brought (when_said), as
# the tool words it: reworded there, a fault waiting on the old words never
# comes (NOT-FIRED), so a unit test holds each to the tool's code.
SAID = {"uploading": "Sending Tasmota (",
        "uploaded": "the device is restarting into Tasmota",
        "sending_wifi": "Sending the details for '",
        "seven_times": "Switch the device off and on 7 times",
        # Matched whatever its capitals: a release before 3.0.0 began it "Sending".
        "installer_again": "sending the installer again",
        "switch_now": "No answer from the device: switch it off and on - it is safe now"}

# Each direction: the saved device it starts from, its target, and the steps that
# write to or wait on the device, with how long each usually takes (emulator).
# Each step's usual length in seconds, from passing runs: a fault lands at a
# share of it. F4 is 10 s (one restart; it was 31 s with two).
# The steps that write firmware are shorter from 2.0.2 - each block checked on
# the device, not sent back: E2, G1 and L1 by 12 s, R1 (two images) by 25 s - so
# a fault at 90% of the old lengths landed in the step after.
ROUTES = {
    "tasmota>esphome": ("suite-tasmota", "esphome", {"L1": 23, "M1": 8, "I2": 5}),
    "tasmota>sonoff": ("suite-tasmota", "sonoff",
                       {"L1": 23, "X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 32, "R2": 2}),
    "esphome>tasmota": ("suite-esphome", "tasmota",
                        {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "K1": 2, "E1": 2, "E2": 17, "E3": 5,
                         "F2": 35, "F4": 10, "G1": 21, "H1": 2}),
    "esphome>sonoff": ("suite-esphome", "sonoff",
                       {"X1": 12, "X2": 25, "D4": 31, "D5": 2, "R1": 32, "R2": 2}),
    "sonoff>tasmota": ("suite-stockC2-tasmota", "tasmota",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "K1": 2, "E1": 2, "E2": 17,
                        "E3": 5, "F2": 35, "F4": 10, "G1": 21, "H1": 2}),
    "sonoff>esphome": ("suite-stockC2-esphome", "esphome",
                       {"C3": 14, "C3fb": 14, "D1": 18, "D4": 31, "D5": 2, "K1": 2, "I1": 30, "I2": 5}),
    # Back to Sonoff with no backup on this computer: from a copy of the device on
    # Tasmota without one (prep), R1 putting Sonoff's firmware, fetched first,
    # together with the identity the device kept: 53 s in the emulator (60 s
    # would put a fault at 90% a second after it).
    "nobackup>sonoff": ("suite-tasmota-nobackup", "sonoff", {"R1": 53, "R2": 2}),
    # A device on the Tasmota release before this one, updated (prep_update):
    # U5 37 s in the emulator, and U6 2 s, as U5 now waits for Tasmota's boot
    # (12 s would put every fault at 40% and 90% after U6). U5 took 43-46 s
    # before; at 44 the fault at 90% came 3 s after U5 had ended (MISTIMED).
    "update>tasmota": ("suite-tasmota-earlier", "tasmota", {"U5": 38, "U6": 2}),
    # A device on this project's ESPHome of an earlier release of the tool, updated (prep_update).
    "update>esphome": ("suite-esphome-earlier", "esphome", {"U1": 6, "U2": 72}),
}
# The release whose ESPHome update>esphome starts on: any before this checkout's.
EARLIER_RELEASE = "v2.3.0"
# A route's name says what its device starts on, but for these.
STARTS_ON = {"nobackup": "tasmota"}

# The lengths above are the S60's. A device whose own layout has ota_0 elsewhere
# than where the mover goes (the TH Elite's: Layout.needs_temporary) leaves
# Tasmota through a temporary table, so never straight to ESPHome: only the
# hub's Tasmota writes its own table back, as D5 starts (D5ot). Its usual
# lengths where they differ from the S60's by more than a few seconds, the
# shorter where runs differed, from its public dump.
TEMPORARY_TABLE_LENGTHS = {"C3": 30, "D1": 35, "D4": 38, "D5": 4, "E2": 27, "E3": 18, "F2": 57, "F4": 15,
                           "G1": 37, "H1": 16, "I2": 22, "L1": 46, "X2": 32}
TEMPORARY_TABLE_TO_ESPHOME = ("L1", "X1", "X2", "D4", "D5", "K1", "I1", "I2")
# The releases legacy and update>esphome runs come from knew only the S60.
S60_ONLY_ROUTES = {"update>esphome"}


def device_kind(mac):
    """The kind of the device the emulators run copies of, by its record's product
    model: an S60 unless that says otherwise."""
    record = records.read_device(mac) if mac else {}
    return kinds.for_account(record.get("product_model"), None) or kinds.SONOFF_S60


def routes_for(kind):
    """ROUTES as `kind` takes them, each step with its usual length on it."""
    if kind is kinds.SONOFF_S60:
        return ROUTES
    layout = layouts.of(kind.layout)
    routes = {}
    for route, (start, target, steps) in ROUTES.items():
        if route in S60_ONLY_ROUTES:
            continue
        if layout.needs_temporary:
            if route == "tasmota>esphome":
                known = {**ROUTES["tasmota>sonoff"][2], **ROUTES["sonoff>esphome"][2]}
                steps = {step: known[step] for step in TEMPORARY_TABLE_TO_ESPHOME}
            steps = {step: TEMPORARY_TABLE_LENGTHS.get(step, length) for step, length in steps.items()}
        routes[route] = (start, target, steps)
    return routes


DEVICE = kinds.SONOFF_S60  # --device's kind (main)
# Past D1 a stock route needs the cloud no more: it starts from a device saved
# there (prep), on the interim Tasmota, so any machine can run it.
AFTER_D1 = {"sonoff>tasmota": "suite-afterD1-tasmota", "sonoff>esphome": "suite-afterD1-esphome"}
CLOUD_STEPS = {"C2", "C3", "C3fb", "D1"}
# C2 goes through eWeLink: from a fresh stock device.
C2_ROUTES = {"sonoff>tasmota": "tasmota", "sonoff>esphome": "esphome"}
OTHER = {"tasmota": "esphome", "esphome": "tasmota"}

# Where a step's work starts, if not at its own line, and how long it lasts from
# there. A run from a saved device finds it back on Sonoff's firmware at C3 (the
# emulator starting is the device switched on) and sends the installer again
# first: the backup itself starts at its own line. C3fb is the fault in that
# finding and sending again, at C3's own line.
# F4rs is the moment F4's reset restarts the device and saves the defaults it
# rebuilt, with the device's name and template put in (one restart): a fault
# there may cut it off before the save, or after.
# F4tp is the restart after F4's fallback sent the template itself (the
# reset's save did not take it): cut off before Tasmota saves, the device comes
# back without it.
# The tool says "device"; the releases legacy runs use, "plug".
# D5ot is a device on a temporary table given its own (TEMPORARY_TABLE_LENGTHS):
# one plan of a few seconds, then a restart.
STARTS = {"C3": (r"Backing up the (?:plug|device)'s Sonoff firmware", 12), "C3fb": (r"\] C3  ", 14),
          "F4rs": (r"Settings reset to Tasmota's defaults", 3), "F4tp": (r"Template applied as", 2),
          "D5ot": (r"on the temporary layout it left Tasmota through; writing its own", 8)}
# F4's reset without the Berry driver: the name and template not put in.
NOHOOK_RESET = re.compile(r"Settings reset to Tasmota's defaults, keeping the Wi-Fi; the (?:plug|device)")
STEP_OF = {"C3fb": "C3", "F4rs": "F4", "F4tp": "F4", "D5ot": "D5"}  # the tool's own name for a step
# Where each of STARTS is, and of ENDS, in a run's events (output.EVENTS) -
# (event, its step or phase) - by which a run of this checkout's tool is timed;
# an earlier release's, which wrote none, by its words in STARTS and ENDS.
STARTS_AT = {"C3": ("phase", "backup"), "C3fb": ("began", "C3"), "F4rs": ("phase", "settings-reset"),
             "F4tp": ("phase", "template"), "D5ot": ("phase", "own-layout")}

# Readouts (cli.py readout, unattended, docs/readouts.md) of a device fresh from
# Sonoff's firmware. Their phases are no steps of the tool's: each starts and
# ends at what the run says, and lasts in the emulator: the read as C3's, which
# reads the flash through the same bridge; putting Sonoff's firmware back, 5 s
# (bridge 4.4.6): the bridge restarting the device 1.5 s after it is
# asked, Sonoff's firmware answering about 2.5 s later, 4 s in all. The faults
# where each matters most, at 40%: in the read, and in putting it back just
# after the restart, as Sonoff's firmware starts. Before 4.4.6 the person
# switched the device off and on, 10 s in all, and the phase was 18 s; at 18 s the
# restart's phase was over before the fault (NOT-FIRED). Where the bridge will
# not restart, the person is still asked.
BACK = r"Restarting it into its Sonoff firmware|Read\. Now put it back on its Sonoff firmware"
STARTS.update({"read": (r"Reading the device's memory", 12), "back": (BACK, 5)})
ENDS = {"read": BACK, "back": r"It is back on its Sonoff firmware"}
STARTS_AT.update({"read": ("phase", "read"), "back": ("phase", "back")})
ENDS_AT = {"read": ("phase", "back"), "back": ("phase", "back-done")}
READOUT_FAULTS = [("read", "deviceoff", 1), ("read", "outage", 30), ("read", "crash", 1),
                  ("back", "deviceoff", 1), ("back", "outage", 30)]
# A readout cut off mid-read, the bridge still running, and the tool run again
# before the device is switched off and on: to Sonoff's firmware, or the readout;
# and (elsewhere) to Sonoff's firmware with this computer's records of the
# device's runs gone, as on another computer. The bridge is the conversions'
# installer too, but only the readout's.
READOUT_THEN = ("sonoff", "readout", "sonoff-elsewhere")
READOUT = "readout.tar"
# A readout of every device in the account (cli.py readout --all), the device
# fresh from Sonoff's firmware among devices the stub lists beside it, none of
# them emulated (AUTOFLASH_EMU_STUB_OTHERS): each sent to the API Worker's own
# code (unattended-test.sh's, by default). The accounts, by name: two first-stage
# devices; three alike (product, model, version: of which the API keeps one
# from one sender) and one not; two whose first is in the API's ENOUGH, with the device; and
# seven, enough quick first stages to reach the API's two readouts a minute.
# Made up, as docs/cloud-stub.md asks.
READOUT_ALL = "sonoff>readout-all"


def other_device(number, product, model=None, version="1.0.0"):
    """One of the account's other devices, made up (stub_server.Stub.other_item)."""
    return {"deviceid": f"10000000{number:02x}", "name": f"Test device {number}", "productModel": product,
            "model": model or f"SN-TEST-{product}-01", "uiid": 1, "fwVersion": version}


ACCOUNTS = {
    "mixed": [other_device(1, "TESTLIGHT"), other_device(2, "TESTSWITCH", version="2.1.0")],
    "alike": [other_device(1, "TESTLIGHT"), other_device(2, "TESTLIGHT"), other_device(3, "TESTLIGHT"),
              other_device(4, "TESTSWITCH")],
    "enough": [other_device(1, "TESTLIGHT"), other_device(2, "TESTSWITCH")],
    "many": [other_device(n, f"TESTMODEL{n}") for n in range(1, 8)],
}
# The faults in the emulated device's read (READOUT_FAULTS), after the other
# devices' first stages: the run stops there, and is run again.
READOUT_ALL_FAULTS = [("read", "deviceoff", 1), ("read", "crash", 1)]
# A line of the API's requests.log (website/api/local.mjs): when, the method,
# the path, the body's length, and the answer's status and body.
API_REQUEST = re.compile(r"^\S+ (\w+) (\S+) \d+ -> (\d+) (.*)$")
# What the readout of every device says as it starts on each one.
READING_OUT = re.compile(r"Reading out (.*) \((\d+) of (\d+)\)\.\.\.")
# Where the readout asks for the device to be switched off and on, and waits.
# The answers read off a run's report, in its log, each up to the gap after
# it, wherever it is (report.report_screen). The result is the line the
# harness prints from the run's own log (unattended_run.logged_result), as
# the screen says it above the report instead; an earlier release's report
# has it on the screen too. The labels before 3.0.0 too, for an earlier
# release's runs.
REPORT_RESULT = re.compile(r"Result:\s+(\S.*?)(?:\s{2,}|$)", re.M)
REPORT_BACKUP = re.compile(r"(?:Sonoff backup|\bBackup):\s+(\S.*?)(?:\s{2,}|$)", re.M)
REPORT_IDENTITY = re.compile(r"(?:Sonoff identity|\bIdentity):\s+(\S.*?)(?:\s{2,}|$)", re.M)
REPORT_UNFINISHED = re.compile(r"(?:Converted from|From):\s+Unfinished (?:conversion|run)")
# A readout's ask to switch it off and on now, and advice to, from a readout as
# from a conversion: in the log of a release that writes no events (run_asks,
# advised_to_switch).
READOUT_SWITCH = re.compile(r"Switch it off for five seconds, then on")
SWITCH_ADVICE = re.compile(r"switch (the device|the device|it) off and on", re.IGNORECASE)

# Each length where it can lead somewhere the others do not: an
# outage of 5 s is within every timeout of the tool's (8 s and up) and takes
# the device off the Wi-Fi, so it covers 1 s; 15 s outlasts the 8-10 s ones but
# not the 15-20 s ones; 30 s and 120 s each found bugs of their own, 60 s none
# that 30 s or 120 s did not. A crash's rerun after 1 s finds the step still going,
# after 180 s finished and any fallback network up; after 60 s - finished, no
# fallback yet - is a place of its own only where there is one (FALLBACK_STEPS).
OUTAGES = (5, 15, 30, 120, 180)
# A routine run's outages (list quick): the shortest and the longest. The
# others found bugs these did not, so the run before a release has them all.
QUICK_OUTAGES = (OUTAGES[0], OUTAGES[-1])
CORE = ([("outage", s) for s in OUTAGES]
        + [("deviceoff", s) for s in (1, 5, 30, 180)]
        + [("crash", s) for s in (1, 180)]
        + [("cancel", "continue"), ("cancel", "new"), ("ctrlc", "once")])
NEW = [("hangup", "continue"), ("sleep", 600), ("weak", 60),
       ("pcoff", 30), ("pcoff", 120), ("corrupt", "once")]
# Steps that send nothing a changed packet could reach: R2 knows Sonoff's
# firmware by its LAN port taking a connection, and reads nothing over it, so
# corrupt-once there only ever came back NO-TRAFFIC. U2 (an ESPHome update)
# asks ESPHome its version in a packet or two, then waits a minute while it
# confirms itself; its one read, the calibration, comes after the fault's
# minute is over.
NOTHING_TO_CORRUPT = {"R2", "U2"}
# Where a firmware raises its own network when it loses the home one: the
# installer (C3, D1: after 45 s), the mover and ESPHome (L1, M1, X1, I2: after
# 1.5 to 2 minutes). There the person joins it before being asked, and the run
# must carry on by itself through it.
FALLBACK_STEPS = {"C3", "D1", "L1", "M1", "X1", "I2"}
# Faults at 10% and 90% of the riskiest steps, and two in a row there.
CRITICAL = {"D4", "E3", "F2", "F4", "R1", "U5", "X2"}
# The tool's own handling - a second run refused, Ctrl-C twice apart or at its
# question - is the same at every step: tried at the riskiest, and where each
# firmware is first written from (C2, L1, X1). A 2-minute sleep, too, but also
# wherever a fallback network may come up during it.
OWN_HANDLING = [("second", "run"), ("ctrlc", "countdown"), ("ctrlc", "apart")]
OWN_HANDLING_STEPS = CRITICAL | {"C2", "L1", "X1"}
TIMING_FAULTS = [("deviceoff", 1), ("outage", 30), ("crash", 1)]
DOUBLE_FAULTS = [("double", "deviceoff-1"), ("double", "outage-30")]


def fault_wait(arg):
    """How long a run that ended before its fault did waits for the fault: a
    device off for 180 s, its run finished, was asked for its template while off."""
    return max(120, arg + 60) if isinstance(arg, int) else 120


def sid(route, step, fault, arg, at=40):
    return f"{route}:{step}{'' if at == 40 else f'@{at}'}:{fault}-{arg}"


def step_faults(step, quick=False):
    """The one-moment faults tried at `step` (40% of the way through); `quick`,
    a routine run's, the outages only at QUICK_OUTAGES."""
    faults = [f for f in CORE if not (quick and f[0] == "outage" and f[1] not in QUICK_OUTAGES)]
    if STEP_OF.get(step, step) in FALLBACK_STEPS or step == "C2":
        faults += [("crash", 60)]
    if step == "C3fb":
        return faults
    faults += [fault for fault in NEW if not (fault[0] == "corrupt" and step in NOTHING_TO_CORRUPT)]
    if step in OWN_HANDLING_STEPS:
        faults += OWN_HANDLING
    if step in OWN_HANDLING_STEPS or step in FALLBACK_STEPS:
        faults += [("sleep", 120)]
    return faults


def scenarios(quick=False):
    """Every scenario; `quick`, a routine run's (step_faults)."""
    for route, (_, target, steps) in ROUTES.items():
        for step in steps:
            faults = step_faults(step, quick) + ([("cancel", "switch")] if target in OTHER and step != "C3fb" else [])
            for fault, arg in faults:
                yield dict(id=sid(route, step, fault, arg), route=route, step=step, fault=fault, arg=arg, at=40)
            if step in CRITICAL:
                for at in (10, 90):
                    for fault, arg in TIMING_FAULTS:
                        yield dict(id=sid(route, step, fault, arg, at), route=route, step=step,
                                   fault=fault, arg=arg, at=at)
                for fault, arg in DOUBLE_FAULTS:
                    yield dict(id=sid(route, step, fault, arg), route=route, step=step, fault=fault, arg=arg, at=40)
            if step == "C3":
                yield dict(id=sid(route, step, "diskfull", "once"), route=route, step=step,
                           fault="diskfull", arg="once", at=40)
            if step in FALLBACK_STEPS:
                yield dict(id=sid(route, step, "fallback", 150), route=route, step=step,
                           fault="fallback", arg=150, at=40)
    for route in C2_ROUTES:
        for fault, arg in step_faults("C2", quick) + [("cancel", "switch"), ("token", "login")]:
            yield dict(id=sid(route, "C2", fault, arg), route=route, step="C2", fault=fault, arg=arg, at=40)
    yield from composite_scenarios()


def composite_scenarios():
    """Faults a moment in a step cannot make alone (docs/testing.md):

    ownnet-cut, ownnet-dropupload, ownnet-dropsetup (at C3): the home Wi-Fi
        gone from the backup, so D1 goes through the installer's own network;
        then the run cut off just after the upload, or this computer back on
        the home network during the upload, or while Tasmota is given the Wi-Fi.
    lostanswer-once (D1): the upload takes, its answer never arrives.
    seven-times (D4, F4, G1): the device switched off and on 7 times, which
        erases a Tasmota's settings; seven-advised (D1): Tasmota cannot join
        the home Wi-Fi, and that is done when the run advises it.
    nohook-run (F4): Tasmota will not take F4's name and template at the save.
    nohook-cut (F4tp): that, and the device switched off for a second at 10, 50
        and 90% of the restart after F4 sent the template itself.
    F4rs: a power cut or crash at F4's reset, at 10, 50 and 90% of it.
    legacy-v1.2.0, legacy-v2.0.2 (LEGACY_STEPS): a run of that release
        (legacy_tree) killed at 40% of each of its steps from Sonoff, then
        this checkout's tool, over the same captures, as a person updating
        their checkout would - its bundle and pins that release's; and (step
        "start") a device that release converted, then taken on by this one.
    blips-3, lossy-phase (BLIP_STEPS): two 3 s outages from 10% to 90% of a
        Berry phase, or one packet in ten lost for all of it: converted with
        no step tried again, or FAIL-RETRIED.
    lostapp-factory: a converted device's Tasmota written over by an ESPHome
        factory image uploaded through Tasmota's page (go_lostapp), then the
        tool, to Tasmota, ESPHome or Sonoff; lostapp-switched: only switched
        to its safeboot, its Tasmota unharmed, which must be started again,
        never replaced.
    notemplate-generic: a converted device's template replaced with Tasmota's
        generic one, and an MQTT host set, as a person might (go_notemplate);
        then the tool, to Tasmota, ESPHome or Sonoff.
    bootloop-fallback: a device converted to Tasmota, then put on its
        fallback module, as Tasmota does in a boot loop (go_bootloop): it
        must still run its own template (BAD-TEMPLATE).
    safemode-ten: a device converted to ESPHome, switched off within a minute
        of starting ten times, which ESPHome takes for a boot loop and starts
        in safe mode (go_safemode); then the tool, to ESPHome, Tasmota or
        Sonoff: it must know the device for ESPHome in safe mode, not the mover
        (FAIL-TAKEN-FOR-MOVER).
    D5ot, on a device with a temporary table only (TEMPORARY_TABLE_LENGTHS): a
        power cut or crash as its own table is written back, at 10, 50 and
        90% of it.

    The legacy ones need an S60: the releases they run knew no other device."""
    def one(route, step, fault, arg, at=40):
        return dict(id=sid(route, step, fault, arg, at), route=route, step=step, fault=fault, arg=arg, at=at)

    for route in C2_ROUTES:
        for arg in ("cut", "dropupload", "dropsetup"):
            yield one(route, "C3", "ownnet", arg)
        yield one(route, "D1", "lostanswer", "once")
        yield one(route, "D1", "seven", "advised")
        yield one(route, "D4", "seven", "times")
    for route in ("sonoff>tasmota", "esphome>tasmota"):
        for step in ("F4", "G1"):
            yield one(route, step, "seven", "times")
        yield one(route, "F4", "nohook", "run")
        for at in (10, 50, 90):
            yield one(route, "F4tp", "nohook", "cut", at)
        for at, fault, arg in ((10, "deviceoff", 1), (50, "deviceoff", 1), (90, "deviceoff", 1),
                               (50, "deviceoff", 5), (50, "crash", 1)):
            yield one(route, "F4rs", fault, arg, at)
    if DEVICE is kinds.SONOFF_S60:
        for tag, routes in LEGACY_STEPS.items():
            for route, steps in routes.items():
                for step in steps:
                    yield one(route, step, "legacy", tag)
            for route in LEGACY_STARTS[tag]:
                yield one(route, "start", "legacy", tag)
        # Killed at F4, past its commit; then the device switched off part way
        # through G1 of the run after, writing over 1.2.0's startup firmware,
        # which it cannot rebuild.
        yield one("sonoff>tasmota", "F4", "legacy", "v1.2.0+G1off")
        # Killed at F4; then the device switched off part way through U5 of the
        # run after, which updates the release it began with to this one's
        # once it is converted (runner.bring_up_to_date).
        for tag in LEGACY_STEPS:
            yield one("sonoff>tasmota", "F4", "legacy", f"{tag}+U5off")
    if layouts.of(DEVICE.layout).needs_temporary:
        for route in ("tasmota>sonoff", "tasmota>esphome"):
            for at, fault, arg in ((10, "deviceoff", 1), (50, "deviceoff", 1), (90, "deviceoff", 1),
                                   (50, "deviceoff", 30), (50, "crash", 1)):
                yield one(route, "D5ot", fault, arg, at)
    for route in ("tasmota>tasmota", "tasmota>esphome", "tasmota>sonoff"):
        yield one(route, "start", "lostapp", "factory")
    for route in ("tasmota>tasmota", "tasmota>esphome"):
        yield one(route, "start", "lostapp", "switched")
    for route in ("tasmota>tasmota", "tasmota>esphome", "tasmota>sonoff"):
        yield one(route, "start", "notemplate", "generic")
    for route in ("sonoff>tasmota", "esphome>tasmota"):
        yield one(route, "start", "bootloop", "fallback")
    for route in ("esphome>esphome", "esphome>tasmota", "esphome>sonoff"):
        yield one(route, "start", "safemode", "ten")
    for route, step in BLIP_STEPS:
        yield one(route, step, "blips", 3, at=10)
        yield one(route, step, "lossy", "phase", at=10)
    yield from readout_scenarios()
    yield from readout_all_scenarios()


def readout_scenarios():
    """A readout of a device fresh from Sonoff's firmware, unattended (go_readout);
    the same with a fault in each phase where it matters most (READOUT_FAULTS);
    and one started on a device on Tasmota, which must read its first stage alone."""
    yield dict(id=sid("sonoff>readout", "start", "none", "unattended"), route="sonoff>readout", step="start",
               fault="none", arg="unattended", at=40)
    for step, fault, arg in READOUT_FAULTS:
        yield dict(id=sid("sonoff>readout", step, fault, arg), route="sonoff>readout", step=step,
                   fault=fault, arg=arg, at=40)
    for then in READOUT_THEN:
        yield dict(id=sid("sonoff>readout", "read", "crash", 1) + f"-then-{then}", route="sonoff>readout",
                   step="read", fault="crash", arg=1, at=40, then=then)
    yield dict(id=sid("tasmota>readout", "start", "none", "first-stage"), route="tasmota>readout", step="start",
               fault="none", arg="first-stage", at=40)


def readout_all_scenarios():
    """A readout of every device in each account (ACCOUNTS), unattended
    (go_readout_all); and in the mixed one, with a fault in the device's read."""
    for account in ACCOUNTS:
        yield dict(id=sid(READOUT_ALL, "start", "none", account), route=READOUT_ALL, step="start",
                   fault="none", arg=account, at=40, account=account)
    for step, fault, arg in READOUT_ALL_FAULTS:
        yield dict(id=sid(READOUT_ALL, step, fault, arg), route=READOUT_ALL, step=step,
                   fault=fault, arg=arg, at=40, account="mixed")


def is_readout(route):
    """Whether `route` is a readout's (go_readout, go_readout_all), not a conversion's."""
    return route.partition(">")[2] in ("readout", "readout-all")


def device_product():
    """eWeLink's product model for the emulated device: its record's (a public
    dump's test copy has one), else its kind's example. The stub lists it so
    (AUTOFLASH_EMU_STUB_PRODUCT_MODEL), a product the tool asks /wanted of."""
    return ((records.read_device(MAC) if MAC else {}).get("product_model") or DEVICE.example_product_model).upper()


def api_asks(requests):
    """The API's requests.log as the tool's readout of every device made it:
    a /wanted for each device, then each /readout of it sent, a 429 sent
    again. Each device's [wanted, why not, [(status, id) of each send]] -
    wanted as the tool takes it: one it could not read goes ahead - and the
    sends no /wanted came before."""
    asks, unasked = [], []
    for line in requests.splitlines():
        found = API_REQUEST.match(line)
        if not found:
            continue
        _, path, status, body = found.groups()
        try:
            answer = json.loads(body)
        except ValueError:
            answer = {}
        answer = answer if isinstance(answer, dict) else {}
        if path == "/v1/wanted":
            asks.append([not (status == "200" and answer.get("wanted") is False), str(answer.get("why") or ""), []])
        elif path == "/v1/readout":
            (asks[-1][2] if asks else unasked).append((int(status), str(answer.get("id") or "")))
    return asks, unasked


def readout_all_problems(run, finished, api, account, device, kept=()):
    """What one run of a readout of every device did wrong, from its log
    (`run`), whether it finished, and what the API was asked and kept (`api`,
    its folder): [(verdict, why)], [] for nothing; and a summary. `account`
    is how many devices the account holds, `device` the emulated device's product
    model, `kept` the readouts the run kept unsent. Judged by the API's own
    answers, never its rules: every device asked /wanted once, all of them in
    a finished run (FAIL-NOT-ASKED); every one wanted sent until taken - 201,
    after nothing but 429s - but the one a stopped run stopped on
    (FAIL-NOT-SENT); none not wanted sent (FAIL-SENT-UNWANTED); each taken in
    the API's store (FAIL-NOT-STORED), the device's whole (FAIL-READOUT); and
    none kept unsent (FAIL-KEPT-UNSENT)."""
    problems = []
    devices = [(who, int(total)) for who, _, total in READING_OUT.findall(run)]
    requests = api / "requests.log"
    asks, unasked = api_asks(requests.read_text(encoding="utf-8", errors="replace") if requests.exists() else "")
    if finished and (len(devices) != account or any(total != account for _, total in devices)):
        problems.append(("FAIL-NOT-ASKED", f"{len(devices)} devices read out, of the account's {account}"))
    if len(asks) != len(devices) and (finished or len(asks) != len(devices) - 1):
        problems.append(("FAIL-NOT-ASKED", f"{len(asks)} asked /wanted, of {len(devices)} devices read out"))
    stored = sorted(path.name for path in api.glob("readouts__*.tar")) if api.is_dir() else []
    whole = False
    for number, (wanted, why, sent) in enumerate(asks):
        who = devices[number][0] if number < len(devices) else f"device {number + 1}"
        statuses = [status for status, _ in sent]
        if not wanted:
            if sent:
                problems.append(("FAIL-SENT-UNWANTED", f"{who}: not wanted ({why}), sent all the same"))
            continue
        if not sent:
            if finished or number != len(asks) - 1:   # else the device the run stopped on
                problems.append(("FAIL-NOT-SENT", f"{who}: wanted, never sent"))
            continue
        if statuses[-1] != 201 or any(status != 429 for status in statuses[:-1]):
            problems.append(("FAIL-NOT-SENT", f"{who}: wanted, answered {', '.join(map(str, statuses))}"))
            continue
        # The API's id is <day>-<the readout's SHA-256, its first 12 digits>;
        # its key readouts/<day>/<the first 24>.tar.
        day, _, digest = sent[-1][1].rpartition("-")
        kept_as = [name for name in stored if day and digest and name.startswith(f"readouts__{day}__{digest}")]
        if not kept_as:
            problems.append(("FAIL-NOT-STORED", f"{who}: taken as {sent[-1][1]!r}, not in the API's store"))
        elif who.endswith(f"({device})"):
            whole = True
            missing = fault_verdicts.readout_problems((api / kept_as[0]).read_bytes(), "espressif")
            if missing:
                problems.append(("FAIL-READOUT", f"{who}: {'; '.join(missing)}"))
    if unasked:
        problems.append(("FAIL-SENT-UNWANTED", f"{len(unasked)} sent before any /wanted"))
    problems += [("FAIL-KEPT-UNSENT", f"{name} kept unsent, though the API was there") for name in kept]
    summary = {"devices": len(devices), "asked": len(asks), "wanted": sum(1 for ask in asks if ask[0]),
               "taken": sum(1 for ask in asks if ask[2] and ask[2][-1][0] == 201),
               "429": sum(1 for ask in asks for status, _ in ask[2] if status == 429), "device_whole": whole}
    return problems, summary


# The steps that run a Berry phase on the device (stage, restore, plan), each on
# one way it is taken: a phase must ride out blips and a weak signal by
# itself, never needing the step tried again (PR #5: one early blip used up
# the retrying, and a later one failed on its first try). In I1 and R1 the
# two blips are more than its 20 s of retrying apart; from 2.0.2, E2, G1 and
# L1 are too short for that.
BLIP_STEPS = (("sonoff>tasmota", "E2"), ("esphome>tasmota", "G1"), ("sonoff>esphome", "I1"),
              ("tasmota>esphome", "L1"), ("tasmota>sonoff", "R1"))


# The releases whose runs this checkout must carry on: each one's steps from
# Sonoff, by route, and their usual lengths on the emulator then.  1.2.0 is
# the last before 2.0.0's renames; the latest release is the one people have
# when the firmware the tool installs changes (docs/updating-firmware.md),
# its runs pinned to the firmware before.
LEGACY_STEPS = {
    "v1.2.0": {"sonoff>tasmota": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "E1": 2, "E2": 29,
                                  "E3": 5, "F2": 35, "F4": 31, "G1": 33, "H1": 2}},
    "v2.0.2": {"sonoff>tasmota": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "D5": 2, "E1": 2,
                                  "E2": 17, "E3": 5, "F2": 35, "F4": 10, "G1": 21, "H1": 2},
               "sonoff>esphome": {"C2": 20, "C3": 14, "D1": 18, "D4": 31, "D5": 2, "I1": 30,
                                  "I2": 5}},
}
# Devices each release converted, then taken on by this checkout ("start").
# Before 2.0.0 the tool installed only Tasmota.
LEGACY_STARTS = {
    "v1.2.0": ("tasmota>esphome", "tasmota>sonoff"),
    "v2.0.2": ("tasmota>esphome", "tasmota>sonoff", "esphome>tasmota", "esphome>sonoff"),
}
LEGACY = EMU / "legacy"
LEGACY_TOOL = "tools/s60_autoflash.py"  # its name until 2.0.0
LEGACY_TOOLS = (LEGACY_TOOL, "tools/autoflash_cli.py")  # and until the reorganisation
# What a run says when an error it did not expect stops it (tools.autoflash.cli._main).
LEGACY_NAMED = {"G1", "H1"}  # killed past its F4: the device keeps the name that release gave it
_legacy_lock = threading.Lock()


def legacy_tree(tag):
    """The tool as release `tag` shipped, to run in the emulator: its tagged
    files, and only tools/espressif/emulator/legacy/<tag>.patch on top, where it has
    one - 1.2.0's points it at the cloud stub, as AUTOFLASH_EWELINK_API_BASE
    has done since 2.0.0."""
    tree = LEGACY / tag
    with _legacy_lock:
        if not (tree / ".complete").exists():
            shutil.rmtree(tree, ignore_errors=True)
            tree.mkdir(parents=True)
            archive = subprocess.run(["git", "-C", str(REPO), "archive", tag], capture_output=True,
                                     check=True).stdout
            subprocess.run(["tar", "-x", "-C", str(tree)], input=archive, check=True)
            patch = REPO / f"tools/espressif/emulator/legacy/{tag}.patch"
            if patch.exists():
                subprocess.run(["patch", "-s", "-p1", "-d", str(tree), "-i", str(patch)], check=True)
            (tree / ".complete").write_text(f"{tag}\n")
    return tree


def esphome_factory_image():
    """A whole-flash image as ESPHome's builder makes one (factory.bin), for
    a serial flasher: its bootloader from 0, a partition table at 0x8000 -
    Sonoff's, which ESPHome's S60 config keeps - and the app in the first
    slot, at 0x20000. It starts with the bootloader's 0xE9, as an app does."""
    artifacts = REPO / "artifacts"
    image = bytearray(b"\xff" * 0x20000)
    bootloader = next(artifacts.glob("esphome-sonoff-s60-bootloader-*.bin")).read_bytes()
    table = (artifacts / "sonoff-s60-partition-table.bin").read_bytes()
    image[:len(bootloader)] = bootloader
    image[0x8000:0x8000 + len(table)] = table
    app = next(artifacts.glob("esphome-sonoff-s60-imprintable-*.bin")).read_bytes()
    return bytes(image) + app


def needs_cloud(sc):
    # A legacy run starts from Sonoff, at C2, whatever step it stops at; a
    # readout signs in to eWeLink, and sends the bridge through it.
    return (sc["fault"] == "legacy" or is_readout(sc["route"])
            or (sc["route"].startswith("sonoff>") and sc["step"] in CLOUD_STEPS))


# Where eWeLink's own behaviour is under test: the send through it (C2, and C3fb
# sending it again), and what a run of an old release does there. Everywhere
# else a cloud scenario's fault falls once the device has left Sonoff's firmware,
# and the stub answers for eWeLink as well (each has passed there); a
# real-cloud batch leaves those to it. A device back on Sonoff's
# firmware after C3 or D1 has the installer sent again through eWeLink: one
# power cut at each keeps that on the real cloud too. The lengths at C3fb are the
# shortest and longest of each fault, where its outcome on the stub differed.
REAL_CLOUD_C3FB = {("deviceoff", 1), ("deviceoff", 180), ("outage", 5), ("outage", 180), ("crash", 1),
                   ("ctrlc", "once")}


def cloud_matters(sc):
    if not needs_cloud(sc):
        return False
    if sc["fault"] == "legacy":
        return sc["step"] == "C2"
    if sc["step"] == "C2":
        return True
    if sc["step"] == "C3fb":
        return (sc["fault"], sc["arg"]) in REAL_CLOUD_C3FB
    return (sc["fault"], sc["arg"]) == ("deviceoff", 1) and sc["step"] in ("C3", "D1")


def kind_of(sc):
    """How `list` marks a scenario: on the real cloud, on the stub only, or anywhere."""
    return ("cloud" if cloud_matters(sc) else "stub") if needs_cloud(sc) else "any"


def starts_online(sc):
    # A device fresh from Sonoff, its first run looking it up in eWeLink at once.
    return sc.get("step") == "C2" or sc.get("fault") == "legacy"


# -- the cloud: one device at a time ----------------------------------------------
# Shut: the device's TCP to the internet answered with a reset, and eWeLink's to
# it, so each end lets go of their connection at once and the next turn starts
# clean. Only dropped, eWeLink kept the dead connection a while, and an update
# command sent in a turn soon after could go down it, never to arrive. The
# rest of the device's way out is dropped.
SHUT = [["FORWARD", "-s", "192.168.60.50", "-o", "eth0", "-j", "DROP"],
        ["FORWARD", "-s", "192.168.60.50", "-o", "eth0", "-p", "tcp", "-j", "REJECT",
         "--reject-with", "tcp-reset"],
        ["FORWARD", "-d", "192.168.60.50", "-i", "eth0", "-p", "tcp", "-j", "REJECT",
         "--reject-with", "tcp-reset"]]


def device_internet(n, on):
    """The device's own way to the internet (eWeLink), on or off: off for every
    device but the one whose turn it is, as every emulated device is the same one."""
    if on:
        script = "; ".join(f"while iptables -D {' '.join(rule)} 2>/dev/null; do :; done" for rule in SHUT)
    else:
        script = "; ".join(f"iptables -C {' '.join(rule)} 2>/dev/null || "
                           f"iptables -I {rule[0]} 1 {' '.join(rule[1:])}" for rule in SHUT)
    sh("docker", "exec", name(n), "sh", "-c", script)


ESTABLISHED = re.compile(r"\btcp\b.*\bESTABLISHED src=192\.168\.60\.50 "
                         r"dst=(?!10\.|172\.(?:1[6-9]|2\d|3[01])\.|192\.168\.)(\S+) sport=(\d+) dport=(\d+)")


# The device's connection to this computer: what a run serves it.
FETCHING = re.compile(r"\btcp\b.*\bESTABLISHED src=192\.168\.60\.50 dst=192\.168\.60\.1 ")


def device_fetching(n):
    return bool(FETCHING.search(sh("docker", "exec", name(n), "cat", "/proc/net/nf_conntrack").stdout))


CONNECTION = re.compile(r"\btcp\s+\d+\s+\d+\s+(\w+) src=192\.168\.60\.50 dst=(\S+) sport=(\d+) dport=(\d+)")


def connection_changes(seen, emulator, conntrack, at):
    """Lines for what an emulator's device opened and closed since `seen`, which it
    updates: each connection once as it is seen, and again once no longer up.
    Its many short ones to this computer only when they are up."""
    lines, live = [], {}
    for state, dst, sport, dport in CONNECTION.findall(conntrack):
        conn = (emulator, dst, sport, dport)
        live[conn] = up = state == "ESTABLISHED"
        before = seen.get(conn)
        local = dst.startswith(("10.", "172.", "192.168."))
        if (before is None and (up or not local)) or (before is not None and before != up):
            lines.append(f"{at} {emulator} {state} {dst}:{dport}")
    for conn in [c for c in seen if c[0] == emulator and c not in live]:
        del seen[conn]
    seen.update(live)
    return lines


def log_connections(path, every, stop):
    """Every device's connections, with times, for the whole batch: what a turn's log
    shows of its own device, of all of them, between turns too."""
    seen = {}
    with open(path, "a", encoding="utf-8") as out:
        while not stop.wait(every):
            for emulator in sh("docker", "ps", "--format", "{{.Names}}").stdout.split():
                if not emulator.startswith("autoflash-emu-"):
                    continue
                try:
                    conntrack = sh("docker", "exec", emulator, "cat", "/proc/net/nf_conntrack", timeout=10).stdout
                except subprocess.TimeoutExpired:
                    continue
                out.writelines(line + "\n" for line in connection_changes(seen, emulator, conntrack,
                                                                           time.strftime("%T")))
            out.flush()


def cloud_connections(n):
    """The device's open connections to the internet, as its emulator's firewall
    tracks them. Sonoff's firmware prints nothing when it signs on to eWeLink."""
    out = sh("docker", "exec", name(n), "cat", "/proc/net/nf_conntrack").stdout
    return {m.groups() for m in ESTABLISHED.finditer(out)}


class CloudQueue:
    """Turns at the cloud, one device at a time, by how long the run can wait:
    finding the device in eWeLink (A1, A2: about a minute) first, then a device
    that went back to Sonoff's firmware part way (FALLEN: three minutes), then
    the rest (FRESH), and last a C2 scenario's turn before its run starts
    (STARTING), which can wait for ever."""
    FINDING, FALLEN, FRESH, STARTING = -1, 0, 1, 2  # STARTING: a scenario not under way yet

    # Every emulated device is the same eWeLink device: the last one's connection,
    # cut on its side, stays open at eWeLink a while, and the next to connect
    # is not seen online. A pause between turns lets eWeLink let go of it.
    COOL_DOWN = float(os.environ.get("AUTOFLASH_EMU_CLOUD_COOL_DOWN", "5"))

    def __init__(self):
        self.cv = threading.Condition()
        self.holder = None
        self.waiting = []
        self.tickets = 0
        self.released = 0.0

    def acquire(self, n, priority, give_up):
        if STUB_CLOUD:
            return True  # own stub per instance: no shared cloud to take turns at
        with self.cv:
            self.tickets += 1
            entry = (priority, self.tickets, n)
            heapq.heappush(self.waiting, entry)
            while not (self.holder is None and self.waiting[0] == entry
                       and time.monotonic() - self.released >= TUNER.cool_down):
                if give_up():
                    self.waiting.remove(entry)
                    heapq.heapify(self.waiting)
                    self.cv.notify_all()
                    return False
                self.cv.wait(1.0)
            heapq.heappop(self.waiting)
            self.holder = n
            return True

    def release(self, n):
        if STUB_CLOUD:
            return
        with self.cv:
            if self.holder == n:
                self.holder = None
                self.released = time.monotonic()
                self.cv.notify_all()


CLOUD = CloudQueue()


def wait_for_turn(sc, priority, give_up):
    """Whether `sc` got a turn, saying how long it waited: a run past its sign-in
    keeps its own time limits meanwhile, and can fail for the queue alone."""
    asked = time.time()
    got = CLOUD.acquire(sc.n, priority, give_up)
    waited = time.time() - asked
    if got and waited >= 1:
        sc.say(f"CLOUD: waited {waited:.0f}s for a turn")
    return got


class Tuner:
    """How many cloud scenarios at once, and the pause between turns: raised
    (then the pause lowered) a step after every WINDOW cloud scenarios with no
    trouble, and the last step taken back at the first trouble - then held.
    Trouble: a cloud scenario that did not pass with the cloud to blame -
    eWeLink saying the device is offline or the login gone, or a turn held to
    its limit for nothing."""
    WINDOW = 4
    NOT_THE_CLOUD = re.compile(r"stopped part way|was not fully delivered|did not request every firmware byte")
    TROUBLE = re.compile(r"offline in eWeLink|eWeLink shows it offline|access token|"
                         r"turn given up after 2[34]\ds")
    # A send the device never asked for: the command went down the last turn's
    # connection, whatever became of an earlier download in the scenario.
    COMMAND_LOST = re.compile(r"no firmware bytes were transferred")

    def __init__(self):
        self.workers = cloud_workers()
        self.most = int(os.environ.get("AUTOFLASH_EMU_CLOUD_MOST", str(self.workers)))
        self.cool_down = float(os.environ.get("AUTOFLASH_EMU_CLOUD_COOL_DOWN", "5"))
        self.phase = "workers" if self.most > self.workers else "cool-down"
        if os.environ.get("AUTOFLASH_EMU_CLOUD_PAUSE_UP") == "1":
            # From a short pause up: 5 s longer at each trouble the cloud is to
            # blame for, never shorter; how many at once left as it is.
            self.phase = "pause-up"
        elif os.environ.get("AUTOFLASH_EMU_CLOUD_BACK_OFF") == "1":
            # From many at once down: one fewer at each trouble, any failure
            # counting (a machine short of memory fails all sorts of ways),
            # until WINDOW pass in a row.
            self.phase = "backing-off"
        self.recent = []
        self.changed = time.time()  # scenarios started before a change say nothing of it
        self.lock = threading.Lock()

    def note(self, text):
        line = f"{time.strftime('%T')} TUNER {text} (workers {self.workers}, pause {self.cool_down:.0f}s)"
        print(line, flush=True)
        with open(OUT / "tuning.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")

    def finished(self, row, log, started):
        """Judged on the cloud scenarios started since the last change, WINDOW
        at a time: two of them failing is trouble (one alone may be anything),
        WINDOW passing in a row a step on."""
        if STUB_CLOUD or not needs_cloud(row) or self.phase == "held" or started < self.changed:
            return
        if row["verdict"] == "NO-INTERNET":
            return  # this computer's network, not the cloud's, nor how many at once
        with self.lock:
            ok = row["verdict"] in ("PASS", "NO-TRAFFIC")
            if not ok and self.phase != "backing-off":
                # A failure of the scenario's own - a join, a step - says
                # nothing of how many at once the cloud allows. Nor does a
                # download the device began and gave up: that is its network, and
                # Sonoff's firmware stays away from eWeLink after one, which
                # would read as the cloud's fault.
                try:
                    text = Path(log).read_text(encoding="utf-8", errors="replace")
                    ok = not self.COMMAND_LOST.search(text) and (
                        not self.TROUBLE.search(text) or bool(self.NOT_THE_CLOUD.search(text)))
                except OSError:
                    pass
            self.recent.append((ok, row["id"], row["verdict"]))
            last = self.recent[-self.WINDOW:]
            failed = [(i, v) for ok, i, v in last if not ok]
            if len(failed) >= 2:
                why = "; ".join(f"{v} in {i}" for i, v in failed)
                if self.phase == "pause-up":
                    self.cool_down += 5
                    self.note(f"cloud trouble ({why}): pause 5s longer")
                elif self.phase == "backing-off":
                    self.workers = max(1, self.workers - 1)
                    self.note(f"trouble ({why}): one fewer at once")
                elif self.phase == "workers":
                    self.workers = max(1, self.workers - 1)
                    self.phase = "cool-down"
                    self.note(f"trouble ({why}): one fewer at once; now the pause")
                else:
                    self.cool_down += 5
                    self.phase = "held"
                    self.note(f"trouble ({why}): pause put back; held from here")
            elif len(last) == self.WINDOW and not failed:
                if self.phase in ("backing-off", "pause-up"):
                    self.note(f"{self.WINDOW} passed in a row as it is")  # still watched
                elif self.phase == "workers":
                    self.workers += 1
                    if self.workers >= self.most:
                        self.phase = "cool-down"
                    self.note(f"{self.WINDOW} passed in a row: one more at once")
                elif self.cool_down >= 5:
                    self.cool_down -= 5
                    self.note(f"{self.WINDOW} passed in a row: pause 5s shorter")
                else:
                    # Still watched: the pause may yet prove too short.
                    self.note("no pause left to take off")
            else:
                return
            self.recent = []
            self.changed = time.time()


TUNER = Tuner()
BOOTS = re.compile(r"(?P<sonoff>\[BUILD TIME\]|colinkTcp|tmp ip = )|(?P<bridge>autoflash_bridge)|"
                   r"(?P<tasmota>Project tasmota)|(?P<esphome>\[I\]\[app)")
INTENT = re.compile(rf"\] (?:A1|A2|C2)  |(?i:{re.escape(SAID['installer_again'])})")
HOLD_AT_MOST = 240.0
# A step after C2 under way: its device has left Sonoff's firmware, so the turn is free.
PAST_THE_CLOUD = re.compile(r"\] (?!A\d|C2\b)[A-Z]\d\w*  ")
# A sign-in with no device needing the cloud: time for it and the list it asks for.
SIGN_IN_TURN = 15.0
# A device's turn lets its run sign in once the device is back at eWeLink. Until
# then eWeLink can still count the last turn's connection as the device, and
# what the run sends goes down it to no one, seen failing two scenarios in turn.
# Seen: the first connection, to find its server, lasts 2 s; the next is its
# session, which may drop again within seconds - 12 of 91 did before 20 s, 4 of
# them after 10 s - and a run let in on one finds its device offline and stops. A
# device that does not get there leaves the run to find that out.
SIGNED_ON_AFTER = 15.0
DEVICE_BACK_AT_MOST = 60.0
# A run's command that this long has not brought its device to fetch anything has
# been lost, and eWeLink does not pass one on again: the turn is of no more use
# (seen: 3 minutes of one, with the device back on eWeLink after a power cut).
TURN_IDLE = 60.0
SENT = re.compile(rf"\] C2  |(?i:{re.escape(SAID['installer_again'])})|upgrade command sent again")
# Where a run asks for a turn before it signs in (get_device_key.wait_for_turn).
TURN_FILE = "captures/cloud-turn"


def device_busy(state, since):
    """Each Berry step's longest busy time, from the runs since `since`."""
    busy = {}
    for path in records_under(state / "work").glob("*/runs/*/autoflash-state.json"):
        try:
            if path.stat().st_mtime < since:
                continue
            captures = json.loads(path.read_text()).get("captures", {})
        except (OSError, ValueError):
            continue
        for key, seconds in captures.items():
            if key.endswith(".device_busy_seconds") and isinstance(seconds, (int, float)):
                step = key.split(".")[0]
                busy[step] = max(busy.get(step, 0), round(seconds, 1))
    return busy


def run_logs(state):
    found = []
    for log in records_under(state / "work").glob("*/runs/*/run.log"):
        try:  # a scenario starting replaces the captures under it
            found.append((log.stat().st_mtime, log, log.read_text(errors="replace")))
        except FileNotFoundError:
            pass
    return sorted(found, key=lambda f: f[0])


def run_asks(log, text):
    """What a run log's runs asked of the person: the networks to join, in
    turn; how many times to go back to the home one; to switch the device off
    and on as the run waits, during a conversion and a readout; and whether its
    last run has ended. From the runs' events (output.EVENTS); a release that
    writes none, from its words (`text`) - "from" says which."""
    events = output.events(log.parent)
    if events:
        def count(**fields):
            return sum(all(item.get(k) == v for k, v in fields.items()) for item in events)
        return {"from": "events", "joins": [item.get("network") for item in events if item["event"] == "join"],
                "backs": count(event="home"), "switch_now": count(event="switch", now=True) - count(key=True),
                "readout_switch": count(event="switch", now=True, key=True), "ended": events[-1]["event"] == "ended"}
    return {"from": "words", "joins": [name.rstrip(",") for name in JOIN.findall(text)], "backs": text.count(BACK),
            "switch_now": len(SWITCH_NOW.findall(text)), "readout_switch": len(READOUT_SWITCH.findall(text)),
            "ended": text.rstrip().endswith("=== run ended ===")}


class DeviceMonitor:
    """The device's way to the cloud, for one scenario: shut, but for a turn each
    time the tool is about to need it."""

    def __init__(self, scenario):
        self.sc, self.n, self.state = scenario, scenario.n, scenario.state
        self.stop = threading.Event()

    def start(self):
        if not STUB_CLOUD:  # a stub per instance: its device never needs a turn
            threading.Thread(target=self.watch, daemon=True).start()
        return self

    def run_text(self):
        return "".join(text for _, _, text in run_logs(self.state))

    def live_run(self):
        text = self.run_text()
        at = text.rfind("=== run started")
        if at < 0 or "=== run ended ===" in text[at:]:
            return None
        line = text[text.rfind("\n", 0, at) + 1:text.find("\n", at)]
        return line, text[at:]

    def turn_file(self):
        return self.state / "work" / TURN_FILE

    def watch(self):
        self.turn_file().unlink(missing_ok=True)  # the last scenario's run, killed while it waited
        # Shut first, and kept shut whatever goes wrong below.
        device_internet(self.n, False)
        while not self.stop.is_set():
            try:
                self.watching()
                return
            except Exception as exc:  # noqa: BLE001
                device_internet(self.n, False)
                self.sc.say(f"CLOUD: watcher error {exc!r}; carrying on")
                self.stop.wait(1.0)

    def watching(self):
        handled = {}
        live = self.live_run()
        if live:
            handled[live[0]] = len(INTENT.findall(live[1]))
        if starts_online(self.sc.sc) and not NO_CLOUD and not getattr(self, "started_online", False):
            # A fresh device, as a real one is, online from the start: A1 gives it
            # about a minute to show in eWeLink, less than a turn asked for
            # then and Sonoff's firmware reconnecting take. Held through C2.
            self.started_online = True
            if getattr(self, "preheld", False) or wait_for_turn(self.sc, CLOUD.FINDING, self.stop.is_set):
                # The first run only, as any turn: the next run asks again
                # (A1, at the front of the queue), and the cloud is free for
                # others while this one's fault plays out.
                self.hold("from the start", restart_once=True)
            live = self.live_run()
            if live:
                handled[live[0]] = len(INTENT.findall(live[1]))
        while not self.stop.is_set():
            device_internet(self.n, False)
            live = self.live_run()
            if live:
                run, text = live
                intents = list(INTENT.finditer(text))
                if len(intents) > handled.get(run, 0):
                    handled[run] = len(intents)
                    again = "again" in intents[-1].group(0)
                    finding = re.search(r"\] A[12]  ", intents[-1].group(0))
                    priority = (CLOUD.FINDING if finding else CLOUD.FALLEN if again else CLOUD.FRESH)
                    if NO_CLOUD:
                        self.sc.say("CLOUD: wanted, but this machine gives no turns")
                    elif wait_for_turn(self.sc, priority, self.stop.is_set):
                        seen = self.hold("finding" if finding else "fallen back" if again else "fresh", run=run)
                        if seen is not None:
                            handled[run] = seen
                if self.turn_file().exists() and not NO_CLOUD and wait_for_turn(self.sc, CLOUD.FINDING, self.stop.is_set):
                    self.hold("a sign-in", limit=SIGN_IN_TURN)
            self.stop.wait(1.0)

    def intents(self, run):
        live = self.live_run()
        return len(INTENT.findall(live[1])) if live and live[0] == run else None

    def connections_seen(self, seen, before, now):
        """The device's connections begun in this turn, each with when it was
        first seen, saying which came and went: nothing else shows them."""
        live = cloud_connections(self.n) - before
        for connection in sorted(live - seen.keys()):
            self.sc.say(f"CLOUD: device connected to {connection[0]}")
        for connection in sorted(seen.keys() - live):
            self.sc.say(f"CLOUD: device's connection to {connection[0]} closed")
        return {connection: seen.get(connection, now) for connection in live}

    def hold(self, why, limit=HOLD_AT_MOST, run=None, restart_once=False):
        """Gives the device its way out until the run ends, the device leaves
        Sonoff's firmware or `limit` runs out; returns how many times `run`
        had asked by then, all of them answered by this turn. `restart_once`:
        a device not back at eWeLink in DEVICE_BACK_AT_MOST is switched off and on
        once, as a person would, and waited for again (a fresh device's, with
        nothing yet sent to it)."""
        uart = self.state / "logs/uart.log"
        pos = uart.stat().st_size if uart.exists() else 0
        ended = self.run_text().count("=== run ended ===")
        # Asked for again while it waited: this turn is theirs too.
        asked = self.intents(run)
        steps_from = len(self.run_text())
        watched = device_first = limit > SIGN_IN_TURN  # a short turn is a sign-in no device needs
        before = cloud_connections(self.n) if watched else set()
        first_seen = {}
        looked = 0.0
        device_internet(self.n, True)
        self.sc.say(f"CLOUD: turn taken ({why})")
        since = time.time()
        opened = opened_at = since
        back_at = None
        sent_seen = 0
        idle_from = since
        try:
            while not self.stop.is_set() and time.time() - since < limit:
                now = time.time()
                if watched and now - looked >= 1:
                    looked = now
                    first_seen = self.connections_seen(first_seen, before, now)
                    if device_first and any(now - seen >= SIGNED_ON_AFTER for seen in first_seen.values()):
                        device_first, back_at = False, now
                        self.sc.say(f"CLOUD: device back after {now - opened_at:.0f}s")
                    elif device_first and now - opened_at >= DEVICE_BACK_AT_MOST and restart_once:
                        # Seen against the real cloud: a fresh device's every TLS
                        # dial to eWeLink timing out, the run then stopping at
                        # A1 to ask, its fault never come (NOT-FIRED).
                        restart_once = False
                        self.sc.say(f"CLOUD: device not back after {now - opened_at:.0f}s; switched off and on, "
                                    "as a person would, and waited for again")
                        self.sc.notes["fresh_device_restarted"] = True
                        self.sc.ex(DEVICE_CMD, "restart")
                        before, first_seen = cloud_connections(self.n), {}
                        opened_at = time.time()
                    elif device_first and now - opened_at >= DEVICE_BACK_AT_MOST:
                        device_first, back_at = False, now
                        self.sc.say(f"CLOUD: device not back after {now - opened_at:.0f}s; the run goes on")
                    if back_at is not None:
                        # This run's sends: one before it, cancelled say, is not this turn's to wait on.
                        text = self.run_text()
                        sent = len(SENT.findall(text, max(text.rfind("=== run started"), 0)))
                        if sent > sent_seen or (sent and device_fetching(self.n)):
                            sent_seen, idle_from = sent, now
                        if sent and now - max(idle_from, back_at) >= TURN_IDLE:
                            self.sc.say(f"CLOUD: nothing fetched for {TURN_IDLE:.0f}s; the turn is free")
                            break
                if not device_first:
                    self.turn_file().unlink(missing_ok=True)
                if time.time() - opened > 2:
                    # Again now and then: a device's emulator started in the
                    # turn (a fresh one) starts with its way shut.
                    device_internet(self.n, True)
                    opened = time.time()
                now_asked = self.intents(run)
                if asked is not None and now_asked is not None and now_asked > asked:
                    # The installer sent again part way through: a whole turn
                    # from here. Seen: a turn running out as it was sent again
                    # went with the device shut off, the download never begun.
                    asked = now_asked
                    since = time.time()
                    self.sc.say("CLOUD: turn renewed (asked again)")
                # Held through the download, not only to its start: Sonoff's
                # firmware that loses eWeLink part way gives the download up.
                # Its end is the installer starting, below.
                text = self.run_text()
                now_ended = text.count("=== run ended ===")
                if now_ended < ended:
                    # A fresh device's emulator wiped the last scenario's runs
                    # after this turn began: counted from here.
                    ended = now_ended
                    steps_from = 0
                elif now_ended > ended or PAST_THE_CLOUD.search(text, steps_from):
                    break
                with open(uart, encoding="utf-8", errors="replace") as f:
                    f.seek(pos); boots = f.read(); pos = f.tell()
                if any(m.lastgroup != "sonoff" for m in BOOTS.finditer(boots)):
                    break
                self.stop.wait(0.5)
        finally:
            device_internet(self.n, False)
            CLOUD.release(self.n)
            self.sc.say(f"CLOUD: turn given up after {time.time() - since:.0f}s")
        return asked


def name(n):
    return f"autoflash-emu-{n}"


def sh(*args, timeout=None):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


# The note each fault leaves once it has happened, for those that wait for a
# moment in the run that may never come.
FIRED_NOTE = {("ownnet", "cut"): "cut_after_upload", ("ownnet", "dropupload"): "dropped_off",
              ("ownnet", "dropsetup"): "dropped_off", ("lostanswer", "once"): "answer_dropped",
              ("seven", "times"): "seven_times", ("seven", "advised"): "seven_times"}


def not_fired(sc, notes, converted):
    """A device converted though its fault never happened proves nothing. A failure
    stands whatever happened: it may be a real one, or a brick."""
    if not converted:
        return False
    note = FIRED_NOTE.get((sc["fault"], sc["arg"]))
    if sc["fault"] in ("cancel", "ctrlc"):
        note = "ctrl_c_logged"
    return bool(notes.get("not_applied") or (note and not notes.get(note)))


# -- faults ---------------------------------------------------------------------
# One TCP packet in 20 of 128 bytes or more (one in 2 in a short step, whose
# few packets may be small: a final check's answers), a byte of its data
# changed - at 64, past any header - and its checksum made good again, so only
# the receiver's own checks can tell. By length band (u32 matches masks, not
# ranges): the tap passes uploads as segments of up to 64 KB (GSO), not only
# packets of one frame.
SHORT_STEP = 5  # seconds or less: every other packet
LENGTHS = [("0x0080", "0xff80"), ("0x0100", "0xff00"), ("0x0200", "0xfe00"), ("0x0400", "0xfc00"), ("0x0800", "0xf800"), ("0x1000", "0xf000"),
           ("0x2000", "0xe000"), ("0x4000", "0xc000"), ("0x8000", "0x8000")]


def corrupt_filter(value, mask, every=20):
    return (f"protocol ip u32 match ip protocol 6 0xff match u16 {value} {mask} at 2 "
            f"action gact ok random determ pipe {every} "
            "action pedit ex munge offset 64 u8 set 0x5a pipe action csum ip4h tcp").split()


# The device unable to reach this computer, though it answers: rare, and until it
# restarts. Looked at when a scenario fails with it (probe, below), before the
# device is used again - never while a run is using it.
STRANDED = re.compile(r"did not fetch the \w+ instructions|returned HTTP -1")

# What the run tells the person to join, and to go back from, in the log of a
# release that writes no events (run_asks).
JOIN = re.compile(r"join:\s*\n\[[^\]]*\]\s+(?:network\s+)?(\S+)")
BACK = "Back to your usual Wi-Fi"
# Said while the run waits on, and carries on by itself once it is done, by a
# release that writes no events. (In the run's log each line has its time first.)
SWITCH_NOW = re.compile(re.escape(SAID["switch_now"]) +   # before 3.0.0, said over two lines:
                        r"|switch it off and on:\s+(?:\[[\d:]+\]\s+)?at this stage that is safe\. "
                        r"This carries on by itself")


class Scenario:
    def __init__(self, n, sc, log):
        self.n, self.sc, self.log = n, sc, log
        self.created = time.time()   # its emulator's start and any turn at eWeLink counted too
        self.state = EMU / f"state-{n}"
        self.fired = None
        self.ctrl_c_before = 0
        self.notes = {}
        self.step_length = 60  # the step under test's usual length (go_watched)
        self.earlier_release_wrote = {}  # each run log, as far as a legacy run left it

    def unexpected(self):
        """Whether this checkout's runs in the scenario met an error the tool did
        not see coming (fault_verdicts.unexpected): not an earlier release's,
        which is out already (1.2.0's TimeoutError as G1 starts)."""
        return fault_verdicts.unexpected(
            (self.ours(p, t) for m, p, t in run_logs(self.state) if m >= self.started),
            fault_verdicts.run_outcomes(self.state / "work/captures", self.started))

    def ours(self, log, text):
        """What this checkout's runs wrote to a run log: past what an earlier
        release's run (go_legacy), carried on in the same run, wrote first."""
        return text[self.earlier_release_wrote.get(filed_as(log), 0):]

    def say(self, text):
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%T')} {text}\n")

    def ex(self, *args, timeout=60, as_tool=False):
        """A command in the emulator: as root, or as the user the tool runs as."""
        user = ("-u", f"{os.getuid()}:{os.getgid()}") if as_tool else ()
        return sh("docker", "exec", *user, name(self.n), *args, timeout=timeout)

    def run_tool(self, target, extra=(), source=None, fresh=False, code=None):
        """A run to `target`, or a readout (target "readout"): of this
        checkout's tool, or of the one in `code` (legacy_tree) - which stays in
        place for the runs after, until another `code`. A release before 2.0.0
        had no --firmware: Tasmota only."""
        args = [str(REPO / "tools/espressif/emulator/unattended-test.sh"), f"--instance={self.n}",
                f"--device={MAC}", "--keep"]
        every = self.sc.get("route") == READOUT_ALL
        if target == "readout":
            args.append("--readout")
        # Kept unsent, for readout_problems to judge, but a readout of every
        # device's, which the API's side judges (readout_all_problems).
        if not every:
            args.append("--no-api")
        if STUB_CLOUD:
            args.append("--stub-cloud")
        if fresh:
            args.append("--fresh")
        if source:
            args.append(f"--from={saved(source)}")
        if code:
            args.append(f"--code={code}")
        legacy = code is not None and (Path(code) / LEGACY_TOOL).exists()
        args += ["--", *([] if legacy or target == "readout" else ["--firmware", target]),
                 *(["--all"] if every and target == "readout" else []), *extra]
        env = dict(os.environ)
        # The account's other devices, and the API's ENOUGH, are a readout of
        # every device's own: any other scenario's account is the device alone.
        for setting in ("AUTOFLASH_EMU_STUB_OTHERS", "AUTOFLASH_API_ENOUGH", "AUTOFLASH_LOCAL_READOUT_LIMIT"):
            env.pop(setting, None)
        if STUB_CLOUD:
            # The device's product, as eWeLink lists it, on every run: else the stub
            # takes it from this checkout's earlier runs (emu.sh), and on one with
            # none lists no product, as eWeLink never does.
            env["AUTOFLASH_EMU_STUB_PRODUCT_MODEL"] = device_product()
        if every:
            env["AUTOFLASH_EMU_STUB_OTHERS"] = str(self.others_file())
            env["AUTOFLASH_API_ENOUGH"] = ",".join(self.enough())
        if every and self.sc.get("account") == "many":
            # One a minute, not the website's two: its eight readouts, 10 to 75 s
            # apart, came three in no one minute, and the 429 never came (NOT-FIRED);
            # over the four or five minutes they take, two now share a minute.
            env["AUTOFLASH_LOCAL_READOUT_LIMIT"] = "1"
        if not STUB_CLOUD and not NO_CLOUD:
            env["AUTOFLASH_EMULATOR_CLOUD_TURN"] = f"/work/{TURN_FILE}"
        if (self.sc["fault"], self.sc["arg"]) == ("ctrlc", "countdown"):
            env["AUTOFLASH_EMULATOR_COUNTDOWN"] = "30"
        if self.sc["fault"] == "nohook":
            env["AUTOFLASH_EMULATOR_NO_AT_SAVE"] = "1"
        with open(self.log, "a", encoding="utf-8") as out:
            out.write(f"--- {' '.join(args[1:])}\n")
            out.flush()
            try:
                return subprocess.call(args, stdout=out, stderr=subprocess.STDOUT, timeout=3600, env=env)
            except subprocess.TimeoutExpired:
                out.write("HARNESS: the run took over an hour; stopped\n")
                return 124

    def current_run(self, since, headers):
        """The text of the run going on now: in a log written since `since`,
        after its last header, once there are more headers than `headers`."""
        logs = [(m, p, t) for m, p, t in run_logs(self.state) if m >= since]
        if not logs:
            return None
        text = logs[-1][2]
        if headers is not None and text.count("=== run started") <= headers:
            return None
        return text[text.rfind("=== run started"):]

    def current_events(self, since, headers):
        """The events (output.EVENTS) of the run current_run gives: None where
        its log has none - an earlier release's, which wrote none."""
        logs = [(m, p, t) for m, p, t in run_logs(self.state) if m >= since]
        if not logs or (headers is not None and logs[-1][2].count("=== run started") <= headers):
            return None
        found = output.events(logs[-1][1].parent)
        return fault_verdicts.last_run_events(found) if found else None

    def reached(self, since, headers, at, words):
        """Whether the current run is at `at` - (event, its step or phase) in its
        events - or, for one that writes none, has said `words`."""
        events = self.current_events(since, headers)
        if events is not None:
            name, value = at
            return any(item["event"] == name and value in (item.get("step"), item.get("phase")) for item in events)
        return bool(re.search(words, self.current_run(since, headers) or ""))

    def headers_now(self):
        logs = run_logs(self.state)
        return logs[-1][2].count("=== run started") if logs else 0

    def wait_for_step(self, since, headers, stop, step=None):
        step = step or self.sc["step"]
        at, words = STARTS_AT.get(step, ("began", step)), STARTS.get(step, (rf"\] {step}  ",))[0]
        while not stop.is_set():
            if self.reached(since, headers, at, words):
                return True
            time.sleep(0.3)
        return False

    def inject(self, since, headers, delay, stop, fault, arg, first=True, step=None):
        """The fault, `delay` seconds into the step under test - or `step`."""
        at = step or self.sc["step"]
        if not self.wait_for_step(since, headers, stop, at):
            return
        time.sleep(delay)
        if stop.is_set():
            return
        step = STEP_OF.get(at, at)
        if self.reached(since, headers, ENDS_AT.get(at, ("done", step)), ENDS.get(at, rf"\] {step} done")):
            self.notes["mistimed"] = True  # the step was over before the fault
        if first:
            self.fired = time.time()
            self.ctrl_c_before = self.ctrl_c_lines()
        self.say(f"FAULT {fault} {arg} at {at} +{delay:.1f}s")
        self.fault(fault, arg, since, headers)
        self.say("FAULT done")

    def ctrl_c_lines(self):
        """In every run log the device has: a saved device's carries on from the Ctrl-C
        that stopped prep's run, so what counts is any more since the fault."""
        return sum(text.count(CTRL_C_LOGGED) for _, _, text in run_logs(self.state))

    def not_applied(self, what):
        self.say(f"FAULT: did not happen: {what}")
        self.notes.setdefault("not_applied", []).append(what)

    def must(self, *args, timeout=60, **options):
        """A command that starts a fault, or ends one the run needs ended. What
        only cleans up goes through ex: a restart may have taken what it undoes."""
        result = self.ex(*args, timeout=timeout, **options)
        if result.returncode:
            self.not_applied(" ".join(args[:4]))
        return result

    def router(self, state):
        self.must(DEVICE_CMD, "router", state)

    def signal(self, sig, *patterns, must=True):
        """To whichever of them are running; the run may have ended by a later
        Ctrl-C of two, which is not `must`."""
        patterns = patterns or (TOOL,)
        hit = [self.ex("pkill", f"-{sig}", "-f", pattern).returncode == 0 for pattern in patterns]
        if must and not any(hit):
            self.not_applied(f"{sig} to a run not running")

    def fault(self, fault, arg, since, headers):
        p = DEVICE_CMD
        if fault == "outage":
            self.router("off"); time.sleep(arg); self.router("on")
        elif fault == "deviceoff":
            self.must(p, "off"); time.sleep(arg); self.must(p, "on")
        elif fault == "crash":
            self.signal("KILL", *HELPERS, TOOL)
        elif fault == "legacy":
            self.signal("KILL", *HELPERS, *LEGACY_TOOLS, TOOL)
        elif fault == "cancel":
            self.signal("INT"); time.sleep(1); self.signal("INT", must=False)
        elif fault == "ctrlc" and arg == "once":
            self.signal("INT")
        elif fault == "ctrlc" and arg == "apart":
            self.signal("INT"); time.sleep(11); self.signal("INT", must=False)
        elif fault == "ctrlc" and arg == "countdown":
            self.signal("INT")
            deadline = time.time() + 900
            while time.time() < deadline:
                text = self.current_run(since, headers) or ""
                if "You pressed Ctrl-C" in text:
                    time.sleep(3)
                    self.signal("INT", must=False)
                    self.notes["countdown_pressed"] = True
                    break
                time.sleep(0.5)
        elif fault == "hangup":
            self.signal("HUP")
        elif fault == "sleep":
            # The computer asleep: everything of the run stopped, and nothing
            # of it on the network, then all back at once.
            self.offline(True)
            self.signal("STOP", TOOL, *HELPERS, "tools/repo/unattended_run.py")
            time.sleep(arg)
            self.signal("CONT", "tools/repo/unattended_run.py", *HELPERS, TOOL, must=False)
            self.offline(False)
        elif fault == "fallback":
            # The home network gone long enough for the device to raise its own;
            # the person joins that as soon as it appears, before being asked.
            self.router("off")
            ends = time.time() + arg
            joined = None
            while time.time() < ends:
                network = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
                if network and not joined:
                    time.sleep(5)
                    result = self.ex("/opt/autoflash-emu/bin/wifi-root", "join", network, timeout=90)
                    joined = network
                    self.joined = network
                    self.say(f"PERSON: joined {network} before being asked (rc {result.returncode})")
                    if result.returncode:
                        self.not_applied(f"joining {network}")
                    else:
                        self.notes["joined_early"] = network
                time.sleep(2)
            self.router("on")
            if not joined:
                self.notes["no_own_network"] = True
        elif fault == "pcoff":
            self.offline(True); time.sleep(arg); self.offline(False)
        elif fault == "weak":
            self.must("tc", "qdisc", "add", "dev", "tap0", "root", "netem",
                      "delay", "150ms", "100ms", "loss", "10%")
            self.must("tc", "filter", "add", "dev", "tap0", "ingress", "pref", "6", "protocol", "ip",
                      "u32", "match", "u32", "0", "0", "action", "gact", "ok", "random", "netrand", "drop", "10")
            time.sleep(arg)
            self.ex("tc", "filter", "del", "dev", "tap0", "ingress", "pref", "6")
            self.ex("tc", "qdisc", "del", "dev", "tap0", "root")
        elif fault == "blips":
            # Two short outages in one Berry phase, from 10% to 90% of it:
            # further apart than its 20 s of retrying (BLIP_STEPS).
            self.router("off"); time.sleep(arg); self.router("on")
            time.sleep(max(0.0, 0.8 * self.step_length - arg))
            self.say(f"FAULT blips: the second, {0.8 * self.step_length:.0f}s after the first")
            self.router("off"); time.sleep(arg); self.router("on")
        elif fault == "lossy":
            # One packet in ten lost, as "weak", for the rest of the phase.
            self.must("tc", "qdisc", "add", "dev", "tap0", "root", "netem",
                      "delay", "150ms", "100ms", "loss", "10%")
            self.must("tc", "filter", "add", "dev", "tap0", "ingress", "pref", "6", "protocol", "ip",
                      "u32", "match", "u32", "0", "0", "action", "gact", "ok", "random", "netrand", "drop", "10")
            time.sleep(self.step_length)
            self.ex("tc", "filter", "del", "dev", "tap0", "ingress", "pref", "6")
            self.ex("tc", "qdisc", "del", "dev", "tap0", "root")
        elif fault == "corrupt":
            filters = [(way, str(base + i), value, mask) for way, base in (("egress", 10), ("ingress", 20))
                       for i, (value, mask) in enumerate(LENGTHS)]
            every = 2 if self.step_length <= SHORT_STEP else 20
            # All in one go: one call each would take a second, most of a short step.
            self.ex("sh", "-c", "; ".join(
                "tc filter add dev tap0 " + " ".join([way, "pref", pref, *corrupt_filter(value, mask, every)])
                for way, pref, value, mask in filters))
            # On until a packet has been changed: a step sending little (the
            # bootloader, a plan) may not pass 20 large packets in seconds.
            # Looked at often, so a changed packet or two is all there is.
            edited, deadline, said = 0, time.time() + 60, ""
            while not edited and time.time() < deadline:
                time.sleep(0.2)
                # Read before the counters, and judged by below: a packet counted
                # with the step not yet said done was changed while it ran. Read
                # again after, a 2 s step (U6) had always ended (MISTIMED).
                said = self.current_run(since, headers) or ""
                if "=== run ended ===" in said:
                    break
                edited = sum(int(n) for n in re.findall(r"pedit.*?Sent \d+ bytes (\d+) pkt", self.ex(
                    "sh", "-c", "tc -s filter show dev tap0 egress; tc -s filter show dev tap0 ingress").stdout,
                    re.S))
            self.ex("sh", "-c", "; ".join(f"tc filter del dev tap0 {way} pref {pref}"
                                          for way, pref, _, _ in filters))
            self.notes["corrupted_packets"] = edited
            step = STEP_OF.get(self.sc["step"], self.sc["step"])
            if edited and re.search(rf"\] {step} done", said):
                self.notes["mistimed"] = True  # changed only once the step was over
        elif fault == "second":
            # A second window, same command, while this one works.
            out = sh("docker", "exec", "-u", f"{os.getuid()}:{os.getgid()}", "-w", "/work",
                     "-e", "HOME=/tmp", "-e", f"AUTOFLASH_EMULATOR_URL=http://localhost:{8080 + self.n}",
                     "-e", "AUTOFLASH_EMULATOR_SUDO_PASSWORD=autoflash", name(self.n), "timeout", "150",
                     "/opt/venv/bin/python", TOOL, "run", "--unattended", "--env-file", "/tmp/autoflash-unattended.env",
                     "--firmware", self.target, timeout=200)
            said = re.sub(r"\s+", " ", re.sub(r"\x1b\[[0-9;]*m", "", out.stdout + out.stderr))
            self.notes["second_refused"] = "another run is converting this device" in said
            self.say("SECOND RUN: " + said[-600:])
        elif fault == "diskfull":
            pid = self.ex("pgrep", "-f", "-o", TOOL).stdout.strip()
            # As the tool's user: the emulator's root has no CAP_SYS_RESOURCE, so
            # may not touch another user's limits. The soft limit only, or
            # that user could not raise it again.
            self.must("prlimit", f"--pid={pid}", "--fsize=1000000:", as_tool=True)
            self.notes["diskfull_pid"] = pid
            time.sleep(60)
            self.ex("prlimit", f"--pid={pid}", "--fsize=unlimited:", as_tool=True)
        elif fault == "token":
            # Beside the run, so not given the stub's address as the run is.
            stub = ("AUTOFLASH_EWELINK_API_BASE=https://apia.autoflash-stub.test "
                    "SSL_CERT_FILE=/run/autoflash-emu/stub-ca-bundle.pem && " if STUB_CLOUD else "")
            out = self.must("sh", "-c", "cd /work && set -a && . /tmp/autoflash-unattended.env && " + stub +
                            "/opt/venv/bin/python -c 'import os; from tools.ewelink.get_device_key import login; "
                            "login(os.environ[\"EWELINK_EMAIL\"], os.environ[\"EWELINK_PASSWORD\"], \"+44\", \"eu\"); "
                            "print(\"logged in again\")'", timeout=60)
            self.notes["token"] = (out.stdout + out.stderr).strip()[-200:]
        elif fault == "ownnet":
            # D1 through the installer's own network (the fallback, below);
            # and, on it, when the run gets that far: the run cut off just
            # after the upload, or this computer back on the home network.
            words, act = {"cut": (SAID["uploaded"], self.cut_run),
                          "dropupload": (SAID["uploading"], self.drop_join),
                          "dropsetup": (SAID["sending_wifi"], self.drop_join)}[arg]
            threading.Thread(target=self.when_said, args=(since, words, act), daemon=True).start()
            self.fault("fallback", 150, since, headers)
        elif fault == "lostanswer":
            # The installer's answer to the upload ("Upload verified") never
            # reaches this computer, every retransmission dropped too: on only
            # now, as the backup's flash - which holds the same words - is read.
            rule = ["INPUT", "-s", "192.168.60.50", "-p", "tcp", "--sport", "80", "-m", "string",
                    "--string", "Upload verified", "--algo", "bm", "-j", "DROP"]
            self.notes["answer_dropped"] = self.must("iptables", "-I", *rule).returncode == 0
            deadline = time.time() + 600
            while time.time() < deadline:
                text = self.current_run(since, headers) or ""
                if "did not complete" in text or "=== run ended ===" in text or re.search(r"\] D1 done", text):
                    break
                time.sleep(1)
            self.ex("iptables", "-D", *rule)
        elif fault == "seven" and arg == "times":
            self.must(DEVICE_CMD, "cycle", "7", timeout=300)
            self.notes["seven_times"] = True
        elif fault == "seven" and arg == "advised":
            # Tasmota sent through the installer cannot join the home Wi-Fi
            # (gone from just after the upload), and the run advises the 7
            # power cycles: done, and the Wi-Fi back.
            if not self.when_said(since, SAID["uploaded"], lambda: None):
                return
            self.router("off")
            self.say("FAULT: the home Wi-Fi gone after the upload")
            if self.when_said(since, SAID["seven_times"], lambda: None, limit=900):
                time.sleep(10)  # a person reads it
                self.say("PERSON: switching the device off and on 7 times")
                self.must(DEVICE_CMD, "cycle", "7", timeout=300)
                self.notes["seven_times"] = True
            self.router("on")
        elif fault == "nohook":
            pass  # the run's own condition (run_tool): there is nothing to do to it

    def when_said(self, since, words, act, limit=1800):
        """Once any run since `since` has said `words`: act(). False if none
        did within `limit` seconds, or the scenario ended first."""
        deadline = time.time() + limit
        while time.time() < deadline and not getattr(self, "ended", False):
            if any(words in text for m, _, text in run_logs(self.state) if m >= since):
                act()
                return True
            time.sleep(0.3)
        return False

    def cut_run(self):
        self.say("FAULT: the tool killed just after the upload")
        self.signal("KILL", *HELPERS, TOOL)
        self.notes["cut_after_upload"] = True

    def drop_join(self):
        time.sleep(3)  # part way
        self.must("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
        self.say("FAULT: this computer back on the home network, off the device's")
        self.notes["dropped_off"] = True
        self.dropped = True  # the person stand-in joins again when asked

    def offline(self, off):
        """The computer off the network: nothing between it and the device, and
        no internet - the device's own way to the internet is left as it is."""
        rules = [("INPUT", "-s", "192.168.60.50"), ("OUTPUT", "-d", "192.168.60.50"),
                 ("OUTPUT", "-o", "eth0")]
        # The router is in the same container, and stays up: its address
        # leases and names still answer the device.
        router = [("INPUT", "-s", "192.168.60.50", "-p", "udp", "-m", "multiport", "--dports", "53,67"),
                  ("OUTPUT", "-d", "192.168.60.50", "-p", "udp", "-m", "multiport", "--sports", "53,67")]
        for (chain, *match), action in [(r, "DROP") for r in rules] + [(r, "ACCEPT") for r in router]:
            if off:
                self.must("iptables", "-I", chain, "1", *match, "-j", action)
            else:
                while self.ex("iptables", "-D", chain, *match, "-j", action).returncode == 0:
                    pass

    def screen_result(self):
        text = Path(self.log).read_text(encoding="utf-8", errors="replace")
        runs = text.split("--- ")[-1]
        clean = re.sub(r"\x1b\[[0-9;]*m", "", runs)
        if "there is nothing to convert" in clean:
            return "Converted (already: nothing to convert)"
        found = REPORT_RESULT.findall(clean)
        if found and found[-1].strip() == "Updated":
            return "Converted (updated)"   # an update's report says so, and it counts the same
        if found:
            return found[-1].strip()
        why = why_no_report(clean)
        return f"no report ({why})" if why else "no report"

    def person(self, stop):
        """As a person at the computer would: join the device's own network when
        the run says to - again if dropped off it, while the run still asks -
        and go back to the home one when it says that, or when the network it
        is on goes (as a computer's Wi-Fi does by itself)."""
        seen = {}  # log -> (joins, backs) already acted on, or there before
        switched = {}  # log -> power-cycle requests already acted on, or there before
        told = {}  # log -> a readout's asks to switch it off and on, acted on or there before
        joined = None
        wanted = None  # (log, the name told) while the run asks for a join
        gone_since = None
        silent = 0  # looks in a row the device has not answered on its network
        while not stop.is_set():
            if getattr(self, "dropped", False):
                joined = self.joined = None
                self.dropped = False
            joined = joined or getattr(self, "joined", None)
            on_air = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
            if joined:
                if on_air:
                    gone_since = None
                elif gone_since is None:
                    gone_since = time.time()
                elif time.time() - gone_since > 5:
                    self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
                    self.say(f"PERSON: {joined} gone; this computer back on the home network by itself")
                    joined = self.joined = None
                    gone_since = None
            if joined and on_air and wanted:
                # Dropped off a network that came back by the same name - the
                # device restarted on it (the installer into Tasmota, say), too
                # quickly to be seen gone: joined again while the run asks.
                gateway = self.ex("cat", "/run/autoflash-emu/gateway").stdout.strip() or "192.168.4.1"
                silent = 0 if self.ex("ping", "-c1", "-W1", gateway).returncode == 0 else silent + 1
                if silent >= 5:  # about 15 s: not a moment's drop as Tasmota tests the details
                    self.say(f"PERSON: dropped off {joined}; joining it again")
                    joined = self.joined = None
                    silent = 0
            else:
                silent = 0
            # As run_logs read it: the tool moves a run filed as unidentified
            # under its device's MAC once it knows it, so the path may be gone.
            for written, log, text in run_logs(self.state):
                if written < self.started:
                    continue
                asks = run_asks(log, text)
                joins, backs = asks["joins"], asks["backs"]
                if seen.get(log, (None,))[0] != asks["from"]:
                    # History from before this scenario - or before its events
                    # began, the log carried on by this checkout's tool.
                    seen[log] = (asks["from"], len(joins), backs)
                    told[log] = asks["readout_switch"]
                    switched[log] = asks["switch_now"]
                    continue
                _, done_joins, done_backs = seen[log]
                if len(joins) > done_joins:
                    wanted = (log, joins[-1])
                    stop.wait(8)  # a person takes a moment
                if backs > done_backs:
                    wanted = None
                    if joined:
                        self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)
                        self.say("PERSON: back on the home network")
                        joined = self.joined = None
                seen[log] = (asks["from"], len(joins), backs)
                asked = asks["switch_now"]
                if asked > switched.setdefault(log, asked):
                    switched[log] = asked
                    stop.wait(30)  # a person reads it, and gets up
                    if not stop.is_set():
                        self.ex(DEVICE_CMD, "restart")
                        self.say("PERSON: switched the device off and on, as the run said")
                        self.notes.setdefault("switched", 0)
                        self.notes["switched"] += 1
                asked = asks["readout_switch"]
                if asked > told[log]:
                    told[log] = asked
                    stop.wait(5)  # it says what to do, and waits until it is done
                    if not stop.is_set():
                        self.ex(DEVICE_CMD, "restart")
                        self.say("PERSON: switched the device off and on, as the readout said")
                        self.notes["switched"] = self.notes.get("switched", 0) + 1
                if wanted and wanted[0] == log and asks["ended"]:
                    wanted = None  # the run that asked is over
            if wanted and not joined:
                # The network picked from the list once it shows: the run may
                # give a pattern (tasmota-ABCDEF-NNNN) or two names.
                on_air = self.ex("cat", "/run/autoflash-emu/softap").stdout.strip()
                if on_air:
                    result = self.ex("/opt/autoflash-emu/bin/wifi-root", "join", on_air, timeout=90)
                    self.say(f"PERSON: joined {on_air} (as told: {wanted[1]}; rc {result.returncode})")
                    if result.returncode == 0:
                        joined = on_air
                        self.notes.setdefault("joined", []).append(on_air)
                    else:
                        stop.wait(5)
            stop.wait(2)
        if joined:
            self.ex("/opt/autoflash-emu/bin/wifi-root", "home", timeout=60)

    def probe(self):
        """What the network shows while the device is asked to connect here: does
        its SYN reach this computer, and what comes back."""
        import urllib.parse
        out = []

        def run(*args, timeout=30):
            try:
                done = self.ex(*args, timeout=timeout)
                out.append(f"$ {' '.join(args)}  (exit {done.returncode})\n{done.stdout}{done.stderr}")
            except subprocess.TimeoutExpired:
                out.append(f"$ {' '.join(args)}  (no answer in {timeout}s)")

        capture = subprocess.Popen(["docker", "exec", name(self.n), "timeout", "25", "tcpdump", "-n", "-e",
                                    "-i", "tap0", "-c", "300", "host", "192.168.60.50"],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        time.sleep(2)
        connect = 'Br var c=tcpclient() var ok=c.connect("192.168.60.1",8089) c.close() return ok'
        for command in (connect, "Ping4 192.168.60.1", "Status 0", connect):
            # Tasmota answers only requests with a Referer, as the tool's are.
            run("curl", "-sS", "-m", "15", "-H", "Referer: http://192.168.60.50/",
                "http://192.168.60.50/cm?cmnd=" + urllib.parse.quote(command), timeout=20)
        out.append("$ tcpdump\n" + capture.communicate(timeout=40)[0])
        run("ss", "-tanp")
        run("iptables-save", "-c")
        run("ip", "neigh")
        run("ip", "-s", "link", "show", "tap0")
        LOGS.mkdir(parents=True, exist_ok=True)
        (LOGS / (key(self.sc["id"]) + "-probe.txt")).write_text("\n".join(out))
        self.notes["probed"] = True
        self.say("PROBE: the device not reaching this computer, looked at")

    def go(self):
        monitor = DeviceMonitor(self)
        if starts_online(self.sc) and not NO_CLOUD and not STUB_CLOUD:
            # The turn before the run, not beside it: A1 looks the device up in
            # eWeLink within seconds of starting, and finds it offline if it
            # is still waiting for its turn then.
            self.say("CLOUD: waiting for a turn before starting")
            # Last in the queue: a run under way that needs the cloud cannot
            # wait as long as one not started yet can, and this turn lasts.
            wait_for_turn(self, CLOUD.STARTING, lambda: False)
            monitor.preheld = True
        monitor.start()
        self.started = time.time()
        uart = self.state / "logs/uart.log"
        self.uart_from = uart.stat().st_size if uart.exists() else 0  # this scenario's networks: from here
        attending = threading.Event()
        threading.Thread(target=self.person, args=(attending,), daemon=True).start()
        self.memory = MemoryWatch()
        self.memory.start()
        try:
            return self.go_watched()
        finally:
            attending.set()
            monitor.stop.set()
            device_internet(self.n, False)
            CLOUD.release(self.n)

    def go_watched(self):
        sc = self.sc
        route, step = sc["route"], sc["step"]
        if route == READOUT_ALL:
            return self.go_readout_all()
        if is_readout(route):
            return self.go_readout()
        if sc["fault"] == "legacy":
            return self.go_legacy()
        if sc["fault"] == "lostapp":
            return self.go_lostapp()
        if sc["fault"] == "notemplate":
            return self.go_notemplate()
        if sc["fault"] == "bootloop":
            return self.go_bootloop()
        if sc["fault"] == "safemode":
            return self.go_safemode()
        if step == "C2":
            source, self.target, steps = None, C2_ROUTES[route], {"C2": 20}
        else:
            source, self.target, steps = ROUTES[route]
            if route in AFTER_D1 and step not in CLOUD_STEPS:
                source = AFTER_D1[route]
        length = STARTS[step][1] if step in STARTS else steps[step]
        self.step_length = length
        delay = max(0.3, sc["at"] / 100 * length)
        if sc["fault"] == "corrupt":
            # From the step's start: its data can be over well before 40% of
            # its usual time (X1's upload), and a short step's in seconds.
            delay = 0.3
        fault, arg = sc["fault"], sc["arg"]
        first_fault = ((arg.split("-")[0], int(arg.split("-")[1])) if fault == "double" else
                       ("deviceoff", 1) if (fault, arg) == ("nohook", "cut") else (fault, arg))
        stop = threading.Event()
        since = time.time()
        injector = threading.Thread(target=self.inject, args=(since, None, delay, stop, *first_fault),
                                    daemon=True)
        injector.start()
        results = [(self.run_tool(self.target, source=source, fresh=source is None), None)]
        results[-1] = (results[-1][0], self.screen_result())
        stop.set()
        injector.join(fault_wait(arg))  # what the fault did is part of the verdict
        if self.fired is None:
            return self.finish(results, "NOT-FIRED")
        if fault == "crash":
            time.sleep(arg)
        target = OTHER.get(self.target, self.target) if (fault, arg) == ("cancel", "switch") else self.target
        extra = ["--new"] if (fault, arg) == ("cancel", "new") else []
        return self.again(results, target, extra, delay, first_fault)

    def go_legacy(self):
        """An earlier release's run (sc["arg"]) on a device fresh from Sonoff,
        killed at 40% of the step under test - or, at "start", let convert it
        to the route's first firmware - then this checkout's tool again and
        again, as a person would, until the device is converted (to the route's
        target)."""
        sc = self.sc
        step, (source, self.target) = sc["step"], sc["route"].split(">")
        # "+G1off": the device switched off, too, part way through G1 of the
        # first run after - G1 then writing over that release's startup
        # firmware, unproven (step_restore), and carrying on after a cut.
        # That G1 is this checkout's, and as long as its.  "+U5off": part way
        # through U5, the update to this release that run goes on to once the
        # device is converted - as long as an update's (update>tasmota).
        tag, _, then = sc["arg"].partition("+")
        steps = LEGACY_STEPS[tag].get(sc["route"], {})
        later = None
        if then:
            later_step = then.removesuffix("off")
            lengths = ROUTES[sc["route"]][2] | ROUTES["update>tasmota"][2]
            later = (later_step, "deviceoff", 1, 0.6 * lengths[later_step])
        old = legacy_tree(tag)
        stop = threading.Event()
        if step != "start":
            self.step_length = steps[step]
            threading.Thread(target=self.inject, args=(time.time(), None, 0.4 * self.step_length, stop,
                                                       "legacy", sc["arg"]), daemon=True).start()
        first = source if step == "start" else self.target
        results = [(self.run_tool(first, fresh=True, code=old), None)]
        results[-1] = (results[-1][0], self.screen_result())
        stop.set()
        self.earlier_release_wrote = {filed_as(log): len(text) for _, log, text in run_logs(self.state)}
        if step == "start":
            if "Converted" not in results[-1][1]:
                return self.finish(results, "OLD-RUN-FAILED")
            # Its starting point, not the end: the conversion under test is next.
            results[-1] = (results[-1][0], f"on {dict(tasmota='Tasmota', esphome='ESPHome')[source]}, by {sc['arg']}")
        elif self.fired is None:
            return self.finish(results, "NOT-FIRED")
        return self.again(results, self.target, [], code=REPO, later=later)

    def go_lostapp(self):
        """A converted device on Tasmota, and what was asked on Reddit: another
        firmware's whole-flash image (ESPHome's factory.bin) uploaded through
        Tasmota's page, which switches to its safeboot for a file that big,
        whose upload takes the image, writes it over Tasmota and refuses it at
        the end. The device then starts the safeboot, even switched off and on.
        Then the tool, as a person would run it, until the device is converted."""
        sc = self.sc
        self.target = sc["route"].split(">")[1]
        # The emulator up with the converted device: asked for what it runs, a
        # run says there is nothing to do.
        results = [(self.run_tool("tasmota", source="suite-tasmota"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        factory = self.state / "work/captures/esphome-factory.bin"
        factory.write_bytes(esphome_factory_image())
        device = "http://192.168.60.50"
        # From the device's own page, as a browser on it is: Tasmota turns away
        # a request without (CSRF protection).
        page_of = ["curl", "-s", "-e", f"{device}/"]
        self.say("LOSTAPP: Tasmota switched to its safeboot, as its page does for a large file")
        self.ex(*page_of, "-m", "10", f"{device}/u4?u4=fct&api=")
        if not self.until_page("/", "safeboot", 120):
            return self.finish(results, "NOT-FIRED")
        if sc["arg"] == "factory":  # "switched": its Tasmota left as it is
            self.say(f"LOSTAPP: ESPHome's factory image ({factory.stat().st_size} bytes) uploaded to it")
            page = self.ex(*page_of, "-m", "300", "-F", "u2=@/work/captures/esphome-factory.bin",
                           f"{device}/u2?fsz={factory.stat().st_size}", timeout=320).stdout
            self.notes["upload_page"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", page))[-300:]
            self.say(f"LOSTAPP: it answered: {self.notes['upload_page'][-120:]}")
            # The emulated Tasmota 15.6.0 safeboot says the upload worked and
            # writes nothing (app0 is unchanged after it); a real one
            # writes the image over Tasmota as it arrives, and checks it only
            # at the end. That, as it is on such a device: the device off, the
            # image where Tasmota was, and on again.
            sh("docker", "stop", name(self.n), timeout=120)
            with open(self.state / "flash.bin", "r+b") as flash:
                flash.seek(APP0_OFFSET)
                flash.write(factory.read_bytes())
            self.say("LOSTAPP: off; the image written over Tasmota (app0), as a real safeboot does; on again")
            sh(str(REPO / "tools/espressif/emulator/emu.sh"), *(["--stub-cloud"] if STUB_CLOUD else ["--cloud", "--yes"]),
               "--detach", f"--device={MAC}", f"--instance={self.n}", timeout=300)
        else:
            self.say("LOSTAPP: switched off and on")
            self.ex(DEVICE_CMD, "restart")
            time.sleep(5)
        if not self.until_page("/", "safeboot", 120):
            self.say("LOSTAPP: not on its safeboot after all")
            return self.finish(results, "NOT-FIRED")
        self.notes["stuck_on_safeboot"] = True
        results[-1] = (results[-1][0], "on Tasmota's safeboot, Tasmota missing")
        return self.again(results, self.target, [])

    def tasmota(self, command):
        """What the device's Tasmota answers to `command`, as JSON - or None."""
        out = self.ex("curl", "-s", "-m", "10", "-G", "-e", f"{DEVICE_URL}/",
                      "--data-urlencode", f"cmnd={command}", f"{DEVICE_URL}/cm").stdout
        try:
            return json.loads(out)
        except ValueError:
            return None

    def release_of(self, firmware):
        """The release of `firmware` the device runs: official Tasmota's, or this
        project's ESPHome's; None if it does not answer as that."""
        return self.tasmota_release() if firmware == "tasmota" else self.esphome_release()

    def esphome_release(self):
        """The release of this project's ESPHome the device runs, as its API
        says (project_version), asked from the emulated computer by the tool
        itself (esphome.api_device_info): the device's API is on that network
        only.  None if it does not answer as this project's.  A few tries: it may be
        starting."""
        code = ("import json, sys; sys.path.insert(0, '/work'); from tools.autoflash import esphome; "
                f"print(json.dumps(esphome.api_device_info({DEVICE_IP!r})))")
        for _ in range(3):
            said = self.ex("python3", "-c", code, as_tool=True).stdout.strip().splitlines()
            try:
                info = json.loads(said[-1]) if said else None
            except ValueError:
                info = None
            if isinstance(info, dict):
                if info.get("project_name") != DEVICE.esphome_project:
                    return None   # not this project's ESPHome
                return info.get("project_version") or None
            time.sleep(5)
        return None

    def tasmota_release(self):
        """The release of official Tasmota the device runs, as its version says
        (16.0.0(release-tasmota32)); None if it does not answer as one."""
        version = ((self.tasmota("Status 2") or {}).get("StatusFWR") or {}).get("Version", "")
        found = re.match(r"(\d+(?:\.\d+)+)\(release-tasmota", version)
        return found.group(1) if found else None

    def template_ok(self):
        """Whether the device runs Tasmota with its own template, asked a few
        times: it may still be starting."""
        for _ in range(6):
            template, module = self.tasmota("Template"), self.tasmota("Module")
            if isinstance(template, dict) and isinstance(module, dict):
                self.notes["template"] = template.get("NAME")
                return own_template(template, module)
            time.sleep(5)
        return False

    def go_notemplate(self):
        """A converted device on Tasmota whose template a person replaced with
        Tasmota's generic one - which switches nothing (firmware-liberator discussion
        #8) - having set it up for themselves (an MQTT host). Then the tool:
        to Tasmota it gives the template alone, the MQTT host kept; to the
        others it leaves Tasmota as from any converted device, never by X1."""
        self.target = self.sc["route"].split(">")[1]
        results = [(self.run_tool("tasmota", source="suite-tasmota"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        generic = json.dumps({"NAME": "Generic", "GPIO": [0] * 22, "FLAG": 0, "BASE": 1}, separators=(",", ":"))
        self.say("NOTEMPLATE: the generic template and an MQTT host set, as a person might")
        self.tasmota(f"Backlog MqttHost {NOTEMPLATE_MQTT_HOST}; Template {generic}; Module 0")
        deadline = time.time() + 120
        while time.time() < deadline:
            time.sleep(5)
            template = self.tasmota("Template")
            if isinstance(template, dict) and template.get("NAME") == "Generic":
                break
        else:
            self.say("NOTEMPLATE: the device did not take the generic template")
            return self.finish(results, "NOT-FIRED")
        results[-1] = (results[-1][0], "on Tasmota, without the device's template")
        return self.again(results, self.target, [])

    def go_bootloop(self):
        """A device converted to Tasmota, then put through what Tasmota does
        at the sixth start in a row within 10 s of the last: taking that for
        a boot loop, it goes over to its fallback module - Generic, unless
        the device's own template is set there - keeping its Wi-Fi. Left on
        Generic, the device switches nothing (firmware-liberator discussion #8).

        Done here as Tasmota would, by `Module` to the fallback `Module2`
        names: Tasmota counts those starts in RTC memory, which the emulator
        loses at every restart (device-supervisor starts QEMU afresh). The
        restarts themselves would also do harm a real device may not: each
        start ending before 8 s counts as a quick power cycle, kept in flash,
        and at the seventh Tasmota erases every setting, its Wi-Fi too."""
        route = self.sc["route"]
        source, self.target, _ = ROUTES[route]
        source = AFTER_D1.get(route, source)
        results = [(self.run_tool(self.target, source=source), None)]
        results[-1] = (results[-1][0], self.screen_result())
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            results.append((self.run_tool(self.target), None))
            results[-1] = (results[-1][0], self.screen_result())
        self.target_final = self.target
        if "Converted" not in results[-1][1]:
            return self.finish(results, None)
        fallback = self.tasmota("Module2")
        fallback = fallback.get("Module2") if isinstance(fallback, dict) else None
        if not isinstance(fallback, dict) or len(fallback) != 1:
            self.say(f"BOOTLOOP: the device did not say its fallback module ({fallback})")
            return self.finish(results, "NOT-FIRED")
        (number, name), = fallback.items()
        self.notes["fallback_module"] = name
        self.say(f"BOOTLOOP: the device goes over to its fallback module, {name} ({number})")
        self.tasmota(f"Module {number}")
        time.sleep(15)  # Module restarts the device; template_ok waits on from there
        return self.finish(results, None)

    def ports_open(self, *ports):
        """Which of `ports` the device takes a connection on now."""
        probe = ("import socket, sys\n"
                 "for port in sys.argv[1:]:\n"
                 "    try:\n"
                 "        socket.create_connection(('192.168.60.50', int(port)), timeout=2).close()\n"
                 "        print(port)\n"
                 "    except OSError:\n"
                 "        pass\n")
        out = self.ex("python3", "-I", "-c", probe, *map(str, ports)).stdout
        return {int(port) for port in out.split()}

    def go_safemode(self):
        """A device converted to ESPHome, then switched off within a minute of
        starting ten times in a row: ESPHome takes that for a boot loop and
        starts in safe mode - Wi-Fi and its update service only, no page, no
        relay - for five minutes, then restarts as itself. All the network
        sees is the update service, as the mover has. Then the tool, to
        ESPHome, Tasmota or Sonoff.

        ESPHome keeps its count in flash, so the emulator keeps it too. Each
        start counts as it begins, before Wi-Fi: cut once the update service
        answers. A normal start has its page up by then; safe mode never."""
        self.target = self.sc["route"].split(">")[1]
        results = [(self.run_tool("esphome", source="suite-esphome"), None)]
        results[-1] = (results[-1][0], self.screen_result())
        if "nothing to convert" not in results[-1][1]:
            return self.finish(results, "NOT-FIRED")
        cuts = 0
        while True:
            deadline, open_now = time.time() + 50, set()
            while time.time() < deadline and ESPHOME_OTA_PORT not in open_now:
                time.sleep(0.5)
                open_now = self.ports_open(ESPHOME_OTA_PORT, 80)
            if ESPHOME_OTA_PORT not in open_now:
                self.say(f"SAFEMODE: ESPHome did not answer within 50 s of starting ({cuts} cuts)")
                return self.finish(results, "NOT-FIRED")
            time.sleep(2)
            if 80 not in open_now and 80 not in self.ports_open(80):
                break  # in safe mode
            if cuts == 12:
                self.say("SAFEMODE: still starting normally after 12 cuts")
                return self.finish(results, "NOT-FIRED")
            self.ex(DEVICE_CMD, "off"); time.sleep(1); self.ex(DEVICE_CMD, "on")
            cuts += 1
        self.notes["safemode_cuts"] = cuts
        self.say(f"SAFEMODE: in safe mode after {cuts} cuts: its update service only")
        results[-1] = (results[-1][0], "on ESPHome's safe mode")
        return self.again(results, self.target, [])

    def go_readout(self):
        """A readout of a device fresh from Sonoff's firmware, found as a person
        without its address would have it found, with the fault in the phase
        under test (READOUT_FAULTS) - the person switching the device off and on
        when the readout asks (person). Then the readout run again, as a
        person would, switching the device off and on where the run says to,
        until a whole readout is kept and the device is back on Sonoff's
        firmware, or MAX_RUNS runs. Or (tasmota>readout) a readout of a device
        on Tasmota, at its address: its first stage alone, the device left as it was."""
        sc = self.sc
        if sc["route"].split(">")[0] != "sonoff":
            return self.go_readout_converted()
        self.target = "sonoff"   # what the device must end on, by name too
        fault, arg = sc["fault"], sc["arg"]
        stop = threading.Event()
        injector = None
        if fault != "none":
            self.step_length = STARTS[sc["step"]][1]
            injector = threading.Thread(target=self.inject, daemon=True, args=(
                time.time(), None, sc["at"] / 100 * self.step_length, stop, fault, arg))
            injector.start()
        results = [(self.run_tool("readout", fresh=True), None)]
        results[-1] = (results[-1][0], self.readout_result())
        stop.set()
        if injector:
            injector.join(fault_wait(arg))
            if self.fired is None:
                return self.finish_readout(results, "NOT-FIRED")
            if fault == "crash":
                time.sleep(arg)
        if sc.get("then"):
            # Run again at once, the device not switched off and on: on the bridge still.
            self.say(f"PERSON: ran the tool again ({sc['then']}) before switching the device off and on")
            target, _, where = sc["then"].partition("-")
            if where == "elsewhere":
                self.ex("sh", "-c", "rm -rf /state/work/captures/devices/*/runs /state/work/captures/plugs/*/runs", as_tool=True)
                self.say("records of the device's runs taken away, as on another computer")
            results.append((self.run_tool(target), None))
            results[-1] = (results[-1][0], self.readout_result())
            # Its report says where it converted from; any stage of the
            # conversions' says it went on as one.
            if REPORT_UNFINISHED.search(self.last_said()) or re.search(
                    r"Stage \d+ - (Installing interim firmware|Updating the bootloader|Making room)", self.last_said()):
                return self.finish_readout(results, "FAIL-TAKEN-FOR-CONVERSION")
        while not self.readout_done() and len(results) < MAX_RUNS:
            if self.advised_to_switch():
                self.say("following the run's advice: the device switched off and on")
                self.ex(DEVICE_CMD, "restart")
                time.sleep(5)
                if self.readout_done():
                    break  # read already: it only had to be put back
            results.append((self.run_tool("readout"), None))
            results[-1] = (results[-1][0], self.readout_result())
        return self.finish_readout(results, None)

    def go_readout_converted(self):
        """A readout of a device converted to Tasmota, at its address: its
        first stage alone (FAIL-READOUT, judged by first_stage_kept), ending
        with no failure (FAIL-EXIT), the device sent nothing (FAIL-SENT) and
        keeping its Tasmota and template (FAIL-DISTURBED)."""
        self.target = "tasmota"
        results = [(self.run_tool("readout", ["--device-ip", DEVICE_IP], source=ROUTES["tasmota>sonoff"][0]), None)]
        results[-1] = (results[-1][0], self.readout_result())
        sent = any("serve_tasmota_ota.py" in t for m, _, t in run_logs(self.state) if m >= self.started)
        problems = self.first_stage_kept()
        self.notes["readout"] = problems if problems is not None else ["none kept"]
        if sent:
            return self.finish_readout(results, "FAIL-SENT")
        if results[-1][0] != 0:
            return self.finish_readout(results, "FAIL-EXIT")
        if self.notes["readout"]:
            return self.finish_readout(results, "FAIL-READOUT")
        return self.finish_readout(results, None if self.template_ok() else "FAIL-DISTURBED")

    def go_readout_all(self):
        """A readout of every device in the account (cli.py readout --all) the
        stub lists for the scenario (ACCOUNTS), the device fresh from Sonoff's
        firmware among them, each readout sent to the API's own code - with
        the fault, where there is one, in the device's read. Then, as a person
        would, the readout of every device run again until one finishes,
        switching the device off and on where the run says to, or MAX_RUNS runs."""
        sc = self.sc
        self.target = "sonoff"
        fault, arg = sc["fault"], sc["arg"]
        stop = threading.Event()
        injector = None
        if fault != "none":
            self.step_length = STARTS[sc["step"]][1]
            injector = threading.Thread(target=self.inject, daemon=True, args=(
                time.time(), None, sc["at"] / 100 * self.step_length, stop, fault, arg))
            injector.start()
        judged = []
        results = [self.run_readout_all(judged, fresh=True)]
        stop.set()
        if injector:
            injector.join(fault_wait(arg))
            if self.fired is None:
                return self.finish_readout_all(results, judged, "NOT-FIRED")
            if fault == "crash":
                time.sleep(arg)
        while results[-1][0] != 0 and len(results) < MAX_RUNS:
            if self.advised_to_switch():
                self.say("following the run's advice: the device switched off and on")
                self.ex(DEVICE_CMD, "restart")
                time.sleep(5)
            results.append(self.run_readout_all(judged))
        if self.advised_to_switch() and self.firmware_now(wait=120) != "sonoff":
            self.say("following the run's advice: the device switched off and on")
            self.ex(DEVICE_CMD, "restart")
        return self.finish_readout_all(results, judged, None)

    def run_readout_all(self, judged, fresh=False):
        """One run of the readout of every device, and what it did wrong
        (readout_all_problems) added to `judged`; its row in the results. What
        the API was asked and kept is kept beside the scenario's log, as the
        next run starts the API afresh."""
        since = time.time()
        code = self.run_tool("readout", fresh=fresh)
        logs = [(m, p, t) for m, p, t in run_logs(self.state) if m >= since]
        run = fault_verdicts.last_run(logs[-1][2]) if logs else ""
        kept = sorted(path.name for path in logs[-1][1].parent.glob("readout*.tar")) if logs else []
        api = Path(self.log).with_name(f"{Path(self.log).stem}-api-{len(judged) + 1}")
        keep_earlier(api)
        if (self.state / "api").is_dir():
            shutil.copytree(self.state / "api", api)
            # And what the API said as it ran: an error that stopped it is
            # seen there (one run: Node 22.14's sqlite refused the database's
            # numbered parameters, and nothing was asked).
            if (self.state / "api-server.out").exists():
                shutil.copy(self.state / "api-server.out", api / "api-server.out")
        problems, summary = readout_all_problems(run, code == 0, api, len(ACCOUNTS[self.sc["account"]]) + 1,
                                                 device_product(), kept)
        judged.append({"code": code, "problems": problems, **summary})
        if problems:
            self.say(f"READOUT-ALL: {problems}")
        return code, (f"{summary['taken']} taken of {summary['wanted']} wanted, {summary['asked']} asked"
                      + ("" if code == 0 else f"; {self.readout_result()}"))

    def others_file(self):
        """The account's other devices for the stub (AUTOFLASH_EMU_STUB_OTHERS), beside the scenario's log."""
        path = Path(self.log).with_name(f"{Path(self.log).stem}-others.json")
        path.write_text(json.dumps(ACCOUNTS[self.sc["account"]], indent=1) + "\n", encoding="utf-8")
        return path

    def enough(self):
        """The API's ENOUGH (AUTOFLASH_API_ENOUGH): in the enough account, the
        device's product model and its first other device's."""
        if self.sc.get("account") != "enough":
            return []
        return [device_product(), ACCOUNTS["enough"][0]["productModel"]]

    def finish_readout_all(self, results, judged, special):
        """A readout of every device's verdict: each run as readout_all_problems
        judges it, a finished run at the end, and the device on working Sonoff
        firmware; in the many account, NOT-FIRED where the API never answered
        429, as its limit was never reached."""
        self.ended = True
        left = self.leftovers()
        names, networks = self.names_seen()
        self.notes["names"], self.notes["networks"] = names, networks
        unknown, final = name_problems(names, networks, self.target)
        self.notes["memory"] = self.memory.note()
        self.notes["runs"] = judged
        problems = [problem for run in judged for problem in run["problems"]]
        self.notes["firmware"] = self.firmware_now(wait=120)
        if not special:
            if self.notes.get("mistimed"):
                special = "MISTIMED"
            elif self.notes.get("not_applied"):
                special = "NOT-FIRED"
            elif self.unexpected():
                special = "FAIL-UNEXPECTED"
            elif problems:
                special = problems[0][0]
            elif results[-1][0] != 0:
                special = "FAIL-NOT-FINISHED"
            elif self.notes["firmware"] != "sonoff":
                special = "BRICK" if self.bricked() else "FAIL-NOT-SONOFF"
            elif unknown or final:
                special = "BAD-NAME"
                self.notes["bad_names"] = unknown + ([final] if final else [])
            elif self.screen_problems():
                special = "FAIL-SCREEN"
            elif left:
                special = "LEFTOVER"
            elif self.sc["account"] == "many" and not any(run["429"] for run in judged):
                special = "NOT-FIRED"
        return self.keep(results, special or "PASS", left)

    def readout_result(self):
        """How the last readout ended, as the results list it."""
        logs = run_logs(self.state)
        run = fault_verdicts.last_run(logs[-1][2]) if logs else ""
        failed = re.findall(r"FAILED: (.*)", run)
        # As the tool logs it, or before 3.0.0 said it on the screen too.
        if re.search(r"Not sent from a test run: kept in |Readout saved in .*, not sent\.", run):
            return "Readout kept, not sent"
        return f"Stopped: {failed[-1].strip()}" if failed else "no readout"

    def readout_kept(self):
        """What the newest readout.tar this scenario's runs kept lacks of a whole
        readout (fault_verdicts.readout_problems), or None when none kept one."""
        kept = sorted((path.stat().st_mtime, path) for path in
                      records_under(self.state / "work").glob(f"*/runs/*/{READOUT}"))
        kept = [path for written, path in kept if written >= self.started]
        return fault_verdicts.readout_problems(kept[-1].read_bytes(), "espressif") if kept else None

    def first_stage_kept(self):
        """What the newest readout.tar this scenario's runs kept lacks of the
        first stage alone of a device on Tasmota (fault_verdicts.first_stage_problems),
        or None when none kept one."""
        kept = sorted((path.stat().st_mtime, path) for path in
                      records_under(self.state / "work").glob(f"*/runs/*/{READOUT}"))
        kept = [path for written, path in kept if written >= self.started]
        return fault_verdicts.first_stage_problems(kept[-1].read_bytes(), "tasmota") if kept else None

    def firmware_now(self, wait=0):
        """What the device answers as, as the tool tells (detect.identify), looked
        at until it is Sonoff's firmware or `wait` seconds are up."""
        probe = (f"import sys; sys.path.insert(0, '/work'); from tools.autoflash import detect; "
                 f"print(detect.identify({DEVICE_IP!r}))")
        deadline = time.time() + wait
        while True:
            found = self.ex("/opt/venv/bin/python", "-c", probe, timeout=60, as_tool=True).stdout.strip()
            if found == "sonoff" or time.time() >= deadline:
                return found
            time.sleep(5)

    def readout_done(self):
        """A whole readout kept, and the device back on Sonoff's firmware: given
        two minutes, as it may be starting."""
        return self.readout_kept() == [] and self.firmware_now(wait=120) == "sonoff"

    def finish_readout(self, results, special):
        """A readout scenario's verdict: a whole readout kept and the device on
        working Sonoff firmware, or for one of a converted device, the device as it was."""
        self.ended = True
        left = self.leftovers()
        names, networks = self.names_seen()
        self.notes["names"], self.notes["networks"] = names, networks
        unknown, final = name_problems(names, networks, self.target)
        self.notes["memory"] = self.memory.note()
        converted = self.sc["route"] != "sonoff>readout"
        if not converted:
            problems = self.readout_kept()
            self.notes["readout"] = problems if problems is not None else ["none kept"]
            self.notes["firmware"] = self.firmware_now()
        if not special:
            if self.notes.get("mistimed"):
                special = "MISTIMED"
            elif self.notes.get("not_applied"):
                special = "NOT-FIRED"
            elif self.unexpected():
                special = "FAIL-UNEXPECTED"
            elif not converted and self.notes["readout"]:
                special = "FAIL-READOUT"  # no whole readout kept
            elif not converted and self.notes["firmware"] != "sonoff":
                special = "BRICK" if self.bricked() else "FAIL-NOT-SONOFF"
            elif unknown or final:
                special = "BAD-NAME"
                self.notes["bad_names"] = unknown + ([final] if final else [])
            elif self.screen_problems():
                special = "FAIL-SCREEN"
            elif left:
                special = "LEFTOVER"
        return self.keep(results, special or "PASS", left)

    def until_page(self, path, words, limit):
        """Whether the device's page `path` says `words`, in any case, within `limit` seconds."""
        deadline = time.time() + limit
        while time.time() < deadline:
            page = self.ex("curl", "-s", "-m", "5", "-e", "http://192.168.60.50/",
                           f"http://192.168.60.50{path}").stdout
            if words.lower() in page.lower():
                return True
            time.sleep(3)
        return False

    def screen_problems(self):
        """What the runs of this checkout's tool left of a stage on their
        screens that they should have taken away (fault_verdicts), kept in the
        notes.  Not an earlier release's (--code= its tree), which is out already;
        a run that puts this checkout back (--code= this checkout) is this checkout's."""
        runs = ("\n" + Path(self.log).read_text(encoding="utf-8", errors="replace")).split("\n--- ")
        ours = "\n".join(run for run in runs[1:] if not earlier_release_run(run.partition("\n")[0]))
        problems = fault_verdicts.screen_problems(ours)
        if problems:
            self.notes["screen"] = problems
        return problems

    def last_said(self):
        """What the last run told the person: the screen it ended on, and its own
        log - which keeps what a question showed while it was asked, gone from
        the screen once an unattended run stops there (seen: "switch the device
        off and on", the advice beside "What now", never followed)."""
        screen = Path(self.log).read_text(encoding="utf-8", errors="replace").split("--- ")[-1]
        logs = run_logs(self.state)
        own = logs[-1][2].split("=== run started")[-1] if logs else ""
        return re.sub(r"\s+", " ", screen + "\n" + own)

    def advised_to_switch(self):
        """Whether the last run ended saying to switch the device off and on: by
        its events (fault_verdicts.advised_to_switch), or, a release's that writes
        none, by its words (last_said)."""
        logs = run_logs(self.state)
        events = output.events(logs[-1][1].parent) if logs else []
        if events:
            return fault_verdicts.advised_to_switch(fault_verdicts.last_run_events(events))
        return bool(SWITCH_ADVICE.search(self.last_said()))

    def again(self, results, target, extra, delay=None, first_fault=None, code=None, later=None):
        """The tool run again, as a person would - switching the device off and
        on, or joining its own network, where the run says to - until the device
        is converted, or MAX_RUNS runs in all. `code`: the first of them from
        there (run_tool), and the rest after it. `later`: (step, fault, arg,
        delay), a fault in the first of them too (noted as later_fired)."""
        sc = self.sc
        fault, arg = sc["fault"], sc["arg"]
        first = len(results)
        while "Converted" not in results[-1][1] and len(results) < MAX_RUNS:
            said = self.last_said()
            if self.advised_to_switch():
                self.say("following the run's advice: the device switched off and on")
                self.ex(DEVICE_CMD, "restart")
                time.sleep(5)
            elif "this computer did not join" in said:
                # Asked for the device's own network, which the person stand-in
                # could not join (it was not there), and nothing else to try.
                self.notes["needs_join"] = True
                break
            stop2 = threading.Event()
            if fault == "double" and len(results) == 1:
                # The same fault again, in the run that recovers from the first.
                threading.Thread(target=self.inject, args=(time.time(), self.headers_now(), delay, stop2,
                                                           *first_fault, False), daemon=True).start()
            if later and len(results) == 1:
                step, later_fault, later_arg, later_delay = later

                # Given its stop event, not reading stop2 when done: by then the
                # loop may have made a new one, never set, for the next run.
                def later_inject(since, headers, stop, step, fault, arg, delay):
                    self.inject(since, headers, delay, stop, fault, arg, False, step)
                    if not stop.is_set():
                        self.notes["later_fired"] = f"{fault}-{arg} at {step}"
                threading.Thread(target=later_inject, daemon=True,
                                 args=(time.time(), self.headers_now(), stop2, step,
                                       later_fault, later_arg, later_delay)).start()
            results.append((self.run_tool(target, extra, code=code), None))
            results[-1] = (results[-1][0], self.screen_result())
            stop2.set()
            extra, code = [], None
            said = re.sub(r"\s+", " ", Path(self.log).read_text(encoding="utf-8", errors="replace").split("--- ")[-1])
            if (fault, arg) == ("cancel", "switch") and "cannot switch to" in said:
                # Refused, as it should be: started again for the other one, as it says.
                self.notes["switch_refused"] = True
                extra = ["--new"]
        # What the device must end on: the last run's target. A run cancelled as it
        # finished converted the device as it was asked, and none followed for `target`.
        self.target_final = target if len(results) > first else self.target
        return self.finish(results, None)

    def names_seen(self):
        """The names the device went by on the home network (DHCP, in order,
        repeats folded) and the networks it put out (its serial log)."""
        dhcp = self.ex("sh", "-c", "grep DHCPACK /run/autoflash-emu/dnsmasq.log 2>/dev/null"
                                   " | grep ' 192.168.60.50 '").stdout
        names, networks = [], []
        for line in dhcp.splitlines():
            name = line.split()[-1]
            if ":" not in name and (not names or names[-1] != name):
                names.append(name)
        try:
            with open(self.state / "logs/uart.log", "rb") as uart:
                uart.seek(getattr(self, "uart_from", 0))
                text = uart.read().decode("utf-8", "replace")
            for network in re.findall(r"esp32_wifi: guest SoftAP (\S+) up", text):
                if not networks or networks[-1] != network:
                    networks.append(network)
        except OSError:
            pass
        return names, networks

    def backup_whole(self):
        backups = list(records_under(self.state / "work").glob("*/sonoff-backup/flash.bin"))
        return bool(backups) and all(b.stat().st_size == 4 * 1024 * 1024 for b in backups)

    def leftovers(self):
        found = []
        rules = self.ex("ufw", "status").stdout
        if "8088" in rules or "8089" in rules or "autoflash" in rules.lower():
            found.append("firewall rule")
        if self.ex("pgrep", "-f", "|".join(HELPERS)).stdout.strip():
            found.append("helper running")
        if self.ex("pgrep", "-f", TOOL).stdout.strip():
            found.append("tool running")
        return found

    def finish(self, results, special):
        self.ended = True
        sc = self.sc
        converted = "Converted" in results[-1][1]
        fault, arg = sc["fault"], sc["arg"]
        left = self.leftovers()
        names, networks = self.names_seen()
        self.notes["names"], self.notes["networks"] = names, networks
        unknown, final = name_problems(names, networks, getattr(self, "target_final", self.target))
        # How long each Berry phase kept the device busy, the longest try of
        # each (the report the run sends, as each stage's "busy"). Kept, not judged: a real
        # S60TPG once stopped answering at 70-90 s, an S60TPF was fine at 2
        # minutes (report, test-reports discussion) - report lists the longest.
        self.notes["device_busy"] = device_busy(self.state, self.started)
        if fault == "legacy" and sc["step"] in LEGACY_NAMED and final and re.match(r"^tasmota-[0-9A-F]{6}-\d{4}$", final):
            final = None  # set up by that release's F4, under Tasmota's own name, as it left every device
        if fault == "legacy" and arg.endswith("+G1off"):
            # Whether the restore after the cut carried on after sectors it had
            # written, or the cut came before it wrote any (the device says).
            uart = self.state / "logs/uart.log"
            with open(uart, "rb") as log:
                log.seek(getattr(self, "uart_from", 0))
                found = re.findall(rb"AUTOFLASH RESTORE: carrying on after (\d+) sectors", log.read())
            self.notes["restore_carried_on_after"] = int(found[-1]) if found else 0
        if fault == "legacy" and "+" in arg and not self.notes.get("later_fired") and not special:
            special = "NOT-FIRED"
        self.notes["memory"] = self.memory.note()
        ours = re.sub(r"\s+", " ", "".join(self.ours(p, t) for m, p, t in run_logs(self.state)
                                           if m >= self.started))
        self.notes["ctrl_c_logged"] = self.ctrl_c_lines() > self.ctrl_c_before
        if not_fired(sc, self.notes, converted) and not special:
            special = "NOT-FIRED"
        converted_from_untemplated = "Converting the device from Tasmota without the device's template" in ours
        target_final = getattr(self, "target_final", self.target)
        template_bad = converted and target_final == "tasmota" and not self.template_ok()
        # The report of the last run, from a converted device (its backup taken
        # by the run that converted it): not a legacy release's.
        last_run = re.sub(r"\x1b\[[0-9;]*m", "", Path(self.log).read_text(
            encoding="utf-8", errors="replace").split("--- ")[-1])
        line = REPORT_BACKUP.findall(last_run)
        backup_line = (line[-1].strip() if line and not sc["route"].startswith("sonoff>")
                       and fault != "legacy" else None)
        no_backup = sc["route"].startswith("nobackup>")
        # The report's end-of-run checks, from the run that converted the device.
        logs = run_logs(self.state)
        # Where the identity came from, in the run's log alone (report.LOG_ONLY);
        # on the screen too before 3.0.0. None to look at when the last run
        # found nothing to convert: it makes no report.
        identity = ((REPORT_IDENTITY.findall(fault_verdicts.last_run(logs[-1][2])) if logs else [])
                    or REPORT_IDENTITY.findall(last_run)
                    if "nothing to convert" not in results[-1][1] else None)
        outcomes = fault_verdicts.run_outcomes(self.state / "work/captures", self.started)
        checks = (fault_verdicts.checks_problem(logs[-1][2], target_final, outcome=outcomes[-1] if outcomes else None)
                  if converted and logs else None)
        if checks:
            self.notes["checks"] = checks
        if not special:
            if self.notes.get("mistimed"):
                special = "MISTIMED"
            elif self.unexpected():
                # A run stopped by an error the tool did not see coming - a bug,
                # whatever the fault - though the next run may convert the device
                # (seen: a report that could not be made, KeyError: 'safeboot').
                # Not an earlier release's, which is out already (1.2.0's
                # TimeoutError as G1 starts).
                special = "FAIL-UNEXPECTED"
            elif fault == "corrupt" and not self.notes.get("corrupted_packets"):
                special = "NO-TRAFFIC"
            elif fault == "lostapp" and (arg == "switched") == ("Official Tasmota (app0) uploaded" in "".join(
                    t for m, _, t in run_logs(self.state) if m >= self.started)):  # not the saved device's own
                # Its own Tasmota, there all along, written over - or none there, not put back.
                special = "FAIL-REPLACED" if arg == "switched" else "FAIL-NOT-PUT-BACK"
            elif fault in ("blips", "lossy") and converted and (len(results) > 1 or any(
                    "That may have been the network - trying again" in t
                    for m, _, t in run_logs(self.state) if m >= self.started)):
                special = "FAIL-RETRIED"  # the phase did not ride it out by itself
            elif fault == "ctrlc" and arg in ("once", "apart") and converted and len(results) > 1:
                special = "FAIL-STOPPED"  # one Ctrl-C (or two, apart) must not stop the run
            elif fault == "ctrlc" and arg == "countdown" and not self.notes.get("countdown_pressed"):
                special = "NOT-FIRED"
            elif fault == "ctrlc" and arg == "countdown" and len(results) == 1:
                special = "FAIL-NOT-STOPPED"  # Ctrl-C at the question must stop at once
            elif fault == "hangup" and len(results) == 1:
                special = "FAIL-NOT-STOPPED"
            elif fault == "hangup" and "when its window was closed" not in re.sub(
                    r"\s+", " ", "".join(t for _, _, t in run_logs(self.state))):
                special = "FAIL-NO-NOTE"  # the next run must say why the last one stopped
            elif fault == "second" and not self.notes.get("second_refused"):
                special = "FAIL-NOT-REFUSED"
            elif fault == "second" and converted and len(results) > 1:
                special = "FAIL-DISTURBED"  # the refused run must not disturb the first
            elif fault == "diskfull" and not self.backup_whole():
                special = "FAIL-BAD-BACKUP"  # never on without the whole backup
            elif fault == "nohook" and not NOHOOK_RESET.search(
                    re.sub(r"\s+", " ", "".join(t for _, _, t in run_logs(self.state)))):
                special = "NOT-FIRED"  # F4 took the name and template at the save after all
            elif any(len(re.findall(r"Type PROCEED to go ahead", run)) > 1
                     for m, p, t in run_logs(self.state) if m >= self.started
                     for run in self.ours(p, t).split("=== run started")):
                special = "FAIL-ASKED-AGAIN"  # the go-ahead is asked once a run
            elif fault == "safemode" and "ESPHome in safe mode" not in ours:
                special = "FAIL-TAKEN-FOR-MOVER"  # sent the startup firmware, even to stay on ESPHome
            elif fault == "notemplate" and not converted_from_untemplated:
                special = "FAIL-NOT-UNTEMPLATED"  # taken for something else
            elif fault == "notemplate" and "ESPHome's update service did not answer" in ours:
                special = "FAIL-X1-ON-TASMOTA"  # sent the hub's steps, as in #8
            elif fault == "notemplate" and self.target == "tasmota" and converted and (
                    "Settings reset to Tasmota's defaults" in ours
                    or (self.tasmota("MqttHost") or {}).get("MqttHost") != NOTEMPLATE_MQTT_HOST):
                special = "FAIL-SETTINGS-LOST"  # its owner's settings reset with the template
            elif (fault == "legacy" and converted and target_final in LATEST
                  and (release := self.release_of(target_final)) != LATEST[target_final]):
                # Begun by an earlier release, it ends on this one's Tasmota or
                # ESPHome: updated once converted (runner.bring_up_to_date).
                self.notes["release"] = f"{target_final} {release}"
                special = "FAIL-NOT-LATEST"
            elif converted and checks:
                special = "FAIL-CHECKS"  # its relay did not switch, or was never checked
            elif converted and template_bad:
                special = "BAD-TEMPLATE"  # a Tasmota that switches nothing
            elif converted and backup_line and backup_line != records.BACKUP_OUTCOMES["not taken" if no_backup else "existing"][1]:
                special = "FAIL-BACKUP-LINE"  # its backup is on this computer, or none is
            elif converted and no_backup and identity is not None and not kept_identity_reported(identity):
                special = "FAIL-IDENTITY"  # not the identity the device kept
            elif converted and (unknown or final):
                # A name this device should never have, or not its converted
                # one at the end (docs/device-names.md).
                special = "BAD-NAME"
                self.notes["bad_names"] = unknown + ([final] if final else [])
            elif converted and self.screen_problems():
                special = "FAIL-SCREEN"  # a stage's output left behind once it moved on
            elif left and converted:
                special = "LEFTOVER"
        # Every setup network can be joined (lcgamboa/qemu #15,
        # and the bridge's open since 4.4.3): none is beyond the emulator.
        verdict = special or ("PASS" if converted else "BRICK" if self.bricked() else
                              # Asked for the device's own network to be joined: right only
                              # where the device really is on it - each is looked at.
                              "NEEDS-JOIN" if self.notes.get("needs_join") else "FAIL")
        return self.keep(results, no_internet(verdict, [r for _, r in results]), left)

    def bricked(self):
        """Whether the device's serial output ends as a device that cannot start does."""
        uart = self.state / "logs/uart.log"
        tail = uart.read_text(errors="replace")[-20000:] if uart.exists() else ""
        return "invalid header" in tail or "No bootable app" in tail or "ets_main.c" in tail

    def keep(self, results, verdict, left):
        """The scenario's row, written to the results, and what a failure
        leaves kept beside them; its emulator stopped."""
        sc = self.sc
        row = {**sc, "instance": self.n, "verdict": verdict, "runs": len(results),
               "results": [r for _, r in results], "codes": [c for c, _ in results],
               "notes": self.notes, "leftovers": left, "log": str(self.log),
               "finished": time.strftime("%Y-%m-%d %H:%M:%S"), "host": os.uname().nodename,
               "seconds": round(time.time() - self.created)}
        with open(RESULTS, "a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        # Every run's recording, whatever the verdict: what each stage showed
        # on the way, gone from the emulator when its next scenario begins.
        screens = LOGS / (key(sc["id"]) + "-screens")
        keep_earlier(screens)
        fault_verdicts.keep_recordings(Path(self.log).read_text(encoding="utf-8", errors="replace"),
                                       self.state / "work", screens)
        if verdict != "PASS":
            # This scenario's own screens: the run's log goes back further.
            if STRANDED.search(re.sub(r"\s+", " ", Path(self.log).read_text(encoding="utf-8", errors="replace"))):
                try:
                    self.probe()
                except Exception as exc:  # noqa: BLE001 - a look, never the verdict
                    self.say(f"PROBE: failed: {exc!r}")
            keep = LOGS / (key(sc["id"]) + "-evidence")
            keep_earlier(keep)
            shutil.copytree(records_under(self.state / "work"), keep, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("*.bin"))
            uart = self.state / "logs/uart.log"
            if uart.exists():
                (keep / "uart-tail.log").write_text(uart.read_text(errors="replace")[-300_000:])
        sh("docker", "stop", name(self.n), timeout=120)
        return row


# The release each firmware a legacy run ends on must be: this checkout's
# (FAIL-NOT-LATEST).
LATEST = {"tasmota": OFFICIAL_TASMOTA_VERSION, "esphome": espressif_routes.published_version(DEVICE, "esphome")}


def earlier_release_run(header):
    """Whether a run's header in a scenario's log (its unattended-test.sh
    arguments) is of an earlier release's tree, not this checkout's: one with
    --code= of another tree.  The run that carries a legacy run on names this
    checkout (code=REPO), to put it back in place."""
    found = re.search(r"--code=(\S+)", header)
    return bool(found) and Path(found.group(1)).resolve() != REPO.resolve()


def name_problems(names, networks, target):
    """The names (DHCP) and networks the device had that this device should never
    have, and - when the last name is not the converted device's - that name.
    A device's names are made from its MAC (docs/device-names.md); those of
    earlier releases are allowed, for a device saved by one."""
    raw = (MAC or "").replace(":", "").replace("-", "").lower()
    if len(raw) != 12:
        return [], None
    two, three = raw[-4:].upper(), raw[-6:]
    # ESP-IDF's default access point name: from the access-point MAC (the
    # Wi-Fi MAC plus one) on an ESP32-C3, from the Wi-Fi MAC on a classic ESP32.
    ap = f"{(int(raw, 16) + (DEVICE.chip != 'esp32')) & 0xFFFFFFFFFFFF:012X}"[-6:]
    allowed = {"espressif", f"autoflash-recovery-{two}", f"tasmota-{three}", f"esphome-{three}", f"ESP_{ap}",
               f"Sonoff-Setup-{two}", f"sonoff-s60-{three}", "s60-ota-bridge", f"sonoff-s60-mover-{three}",
               f"S60-OTA-Bridge-{two}"}
    # Tasmota's own (its settings erased, or set up by 1.2.0), and 1.2.0's
    # Safeboot's, four decimal digits of no fixed rule - and official Tasmota's
    # after that Safeboot, until 1.2.0's F4: its saved name had a format in it
    # (s60-recovery-%04d), which Tasmota takes for its own (artifacts/README.md).
    numbered = re.compile(rf"^((tasmota-{three.upper()}|s60-recovery)-|x)\d{{4}}$")
    unknown = [n for n in dict.fromkeys(names + networks) if n not in allowed and not numbered.match(n)]
    converted = {"tasmota": f"tasmota-{three}", "esphome": f"esphome-{three}", "sonoff": "espressif"}.get(target)
    final = names[-1] if names and converted and names[-1] != converted else None
    return unknown, final


def own_template(template, module):
    """Whether Tasmota's answers to Template and Module are the device's own
    template, in use (module 0)."""
    return DEVICE.names_its_tasmota(str(template.get("NAME"))) and "0" in (module.get("Module") or {})


def kept_identity_reported(lines):
    """Whether the last of a report's "Sonoff identity" lines says the device's
    own: the report starts each answer with a capital."""
    return bool(lines) and lines[-1].strip().lower() == SONOFF_IDENTITY["kept"].lower()


def key(scenario_id):
    return scenario_id.replace(">", "-to-").replace(":", "_").replace("@", "_at")


# This computer, not its device, cut off from eWeLink: the name not found, the
# network down. Seen on a test machine once in 75 scenarios, before the fault.
COMPUTER_OFFLINE = re.compile(r"eWeLink is unreachable \(eWeLink connection failed: [^)]*?"
                              r"(?:Name or service not known|Temporary failure in name resolution|"
                              r"Network is unreachable)")


def no_internet(verdict, results):
    """NO-INTERNET for a fault that never happened because this computer could
    not reach eWeLink first; any other verdict as it is. A fault that cuts this
    computer off (pcoff) has happened, so is never taken for this."""
    if verdict == "NOT-FIRED" and any(COMPUTER_OFFLINE.search(result) for result in results):
        return "NO-INTERNET"
    return verdict


def why_no_report(screen):
    """Where a run that wrote no report stopped, and what it said there: a stage
    stopped at a question an unattended run cannot answer says so only on screen."""
    code = re.findall(r"Exit code (-?\d+)", screen)
    stops = list(re.finditer(r"^ *Stage +(\d+)(?: of \d+)? - (.+?) +- Stopped *$", screen, re.M))
    if not stops:
        return f"exit {code[-1]}" if code else ""
    stop = stops[-1]
    before = screen[:stop.start()]
    began = list(re.finditer(rf"^ *Stage +{stop[1]}(?: of \d+)? - .+- Underway.*$", before, re.M))
    said = before[began[-1].end():] if began else ""
    lines = [line.strip(" -!") for line in said.splitlines()
             if line.strip() and not re.match(r" *\d+\. ", line) and "unattended run cannot" not in line]
    detail = " ".join(lines)
    detail = detail if len(detail) <= 200 else detail[:197] + "..."
    return (f"stopped at stage {stop[1]}, {stop[2]}" + (f": {detail}" if detail else "")
            + (f"; exit {code[-1]}" if code else ""))


def latest():
    rows = {}
    if RESULTS.exists():
        for line in RESULTS.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                rows[row["id"]] = row
    return rows


def allowed(default):
    """How many of a batch's emulators may take a scenario now: the number in
    OUT/workers when it holds one, so a running batch can be scaled to what
    its machine has room for; else how many the batch began with."""
    try:
        return int((OUT / "workers").read_text())
    except (OSError, ValueError):
        return default


def worker(n, todo, lock, cloud=False, room=None):
    while True:
        # A cloud worker beyond how many the tuner allows now waits its turn,
        # and one beyond OUT/workers until it is raised or nothing is left.
        while cloud and n > TUNER.workers:
            time.sleep(10)
        while room and not room(n) and not todo.empty():
            time.sleep(10)
        try:
            sc = todo.get_nowait()
        except queue.Empty:
            return
        skip = OUT / "skip.txt"
        if skip.exists() and sc["id"] in skip.read_text().split():
            continue
        while memory_now()[0] < LOW_MEMORY_KB:
            time.sleep(10)
        cpu_load.wait_for_room(lambda line: print(line, flush=True))
        log = LOGS / (key(sc["id"]) + ".log")
        keep_earlier(log)
        started = time.time()
        try:
            row = Scenario(n, sc, log).go()
        except Exception as exc:  # noqa: BLE001
            row = {**sc, "instance": n, "verdict": "HARNESS-ERROR", "error": repr(exc), "trace": traceback.format_exc(),
                   "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
            with open(RESULTS, "a", encoding="utf-8") as f:
                f.write(json.dumps(row) + "\n")
            sh("docker", "stop", name(n), timeout=120)
        with lock:
            print(f"{time.strftime('%T')} [{n}] {row['verdict']:16} {sc['id']}{starved_label(row)}", flush=True)
        emulator_stats.fault_run(OUT, family=DEVICE.family, device=DEVICE.key, scenario=sc, verdict=row["verdict"],
                                 tries=row.get("runs"), seconds=round(time.time() - started),
                                 cloud=cloud_of(sc), workers=WORKERS, rerun=sc["id"] in EARLIER,
                                 outcomes=fault_verdicts.run_outcomes(EMU / f"state-{n}" / "work/captures",
                                                                      started),
                                 error=row.get("error"),
                                 starved=row.get("notes", {}).get("memory", {}).get("starved"))
        TUNER.finished(row, log, started)


# How many emulators the batch runs scenarios on at most, and the scenarios
# that had run here before it began (a re-run's pass rate is apart), for its
# emulator stats.
WORKERS = None
EARLIER: set = set()


def cloud_of(sc):
    """Whose eWeLink the scenario's runs met, for its emulator stats."""
    return "stub" if STUB_CLOUD else "real" if needs_cloud(sc) else "none"


def read_ids(path) -> list[str]:
    """The scenario ids in a file, one to a line - as `list` writes them, each
    with whether it needs the cloud after it."""
    return [line.split()[0] for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def batch(ids_file=None):
    """The scenarios in ids_file (every one without) not yet passed here: the
    cloud ones on AUTOFLASH_EMU_CLOUD_WORKERS emulators from instance 1, the rest on
    AUTOFLASH_EMU_ANY_WORKERS after them."""
    global WORKERS, EARLIER
    LOGS.mkdir(parents=True, exist_ok=True)
    known = {sc["id"]: sc for sc in scenarios()}
    earlier = latest()
    EARLIER = set(earlier)
    done = {i for i, row in earlier.items() if row["verdict"] in ("PASS", "NO-TRAFFIC")}
    cloud, anywhere = queue.Queue(), queue.Queue()
    ids = read_ids(ids_file) if ids_file else list(known)
    stale = stale_starts(known[i] for i in ids if i not in done)
    if stale:
        raise SystemExit(f"Saved devices not on the firmware this checkout installs: {', '.join(stale)}. Run prep.")
    untrusting = untrusting_starts() if STUB_CLOUD else []
    if untrusting:
        raise SystemExit(f"Saved devices not trusting the stub's CA as it is now: {', '.join(untrusting)}. "
                         "Run prep (prep-update for an -earlier one).")
    stub_only = 0
    # The longest first, so the batch does not end on one started last (scenario_times).
    took = scenario_times.times(*suite_roots())
    for sc in scenario_times.longest_first([known[i] for i in ids], took):
        i = sc["id"]
        if i in done:
            continue
        # With a stub per instance nothing is shared, so every scenario runs in
        # the one parallel pool; otherwise the cloud ones take turns at eWeLink,
        # and a no-cloud machine skips them.
        if STUB_CLOUD:
            anywhere.put(known[i])
        elif kind_of(known[i]) == "stub":
            stub_only += 1
        elif not (NO_CLOUD and needs_cloud(known[i])):
            (cloud if needs_cloud(known[i]) else anywhere).put(known[i])
    if STUB_CLOUD:
        nc = 0
        na = int(os.environ.get("AUTOFLASH_EMU_STUB_WORKERS", os.environ.get("AUTOFLASH_EMU_ANY_WORKERS", "3")))
        first = int(os.environ.get("AUTOFLASH_EMU_FIRST_INSTANCE", "1"))
    else:
        nc = cloud_workers()
        na = int(os.environ.get("AUTOFLASH_EMU_ANY_WORKERS", "0"))
        first = int(os.environ.get("AUTOFLASH_EMU_FIRST_INSTANCE", nc + 1))  # clear of another batch's emulators
    print(f"batch: {cloud.qsize()} cloud on {nc}, {anywhere.qsize()} others on {na}", flush=True)
    if stub_only:
        print(f"batch: {stub_only} left to the stub (--stub-cloud batch): their fault comes after eWeLink",
              flush=True)
    lock = threading.Lock()
    threads = [] if STUB_CLOUD else [threading.Thread(target=worker, args=(n, cloud, lock, True))
                                     for n in range(1, max(nc, TUNER.most) + 1)]
    most = max(na, int(os.environ.get("AUTOFLASH_EMU_MOST_WORKERS", na)))
    threads += [threading.Thread(target=worker, args=(n, anywhere, lock, False, lambda n: n - first < allowed(na)))
                for n in range(first, first + most)]
    stop_log = threading.Event()
    every = float(os.environ.get("AUTOFLASH_EMU_CONNECTION_LOG", "2"))
    if not STUB_CLOUD and every > 0:
        threading.Thread(target=log_connections, args=(OUT / "connections.log", every, stop_log),
                         daemon=True).start()
    WORKERS = len(threads)
    emulator_stats.began(OUT, family=DEVICE.family, suite="fault", device=DEVICE.key, total=len(known),
                         planned=cloud.qsize() + anywhere.qsize(), cloud="stub" if STUB_CLOUD else "real",
                         workers=WORKERS, tier=emulator_stats.tier(ids, known, (sc["id"] for sc in scenarios(quick=True))))
    for t in threads:
        t.start()
        time.sleep(20)  # staggered: start-ups are the busiest moment
    for t in threads:
        t.join()
    stop_log.set()
    print(emulator_stats.send(OUT), flush=True)
    report()


def installs(target):
    """What firmware.json pins that the device under test (DEVICE) converted to
    `target` is made with: its kind's ESPHome, its chip's everything else."""
    espressif = json.loads((REPO / "artifacts/firmware.json").read_text(encoding="utf-8"))["espressif"]
    chip = espressif[DEVICE.chip]
    if target == "esphome":
        return {"image": espressif[DEVICE.key]["esphome"], "bootloader": chip["bootloader"], "mover": chip["mover"]}
    return chip[target]


def converted_start(target):
    return EMU / "saved" / saved(ROUTES[f"{target}>sonoff"][0])


def stale_starts(todo):
    """The saved converted devices `todo` would start from that prep did not make
    with the firmware this checkout installs. A run from one leaves an older
    firmware than the tool installs, untested for the one it does, and passes."""
    # A legacy run makes its own device, and Sonoff's firmware is not this checkout's to change.
    # An update's device is made from one (prep_update), on the release before on purpose.
    targets = {sc["route"].split(">")[0] for sc in todo if sc["fault"] != "legacy"} - {"sonoff", "update"}
    stale = []
    for target in sorted(targets):
        try:
            made = json.loads((converted_start(target) / "firmware.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            made = None
        if made != installs(STARTS_ON.get(target, target)):
            stale.append(saved(ROUTES[f"{target}>sonoff"][0]))
    return stale


def stub_ca():
    """The CA the stub serves this device, as emu.sh chooses it: an RSA one for
    a firmware trusting eWeLink's RSA CA. None while it is not made yet."""
    source = records_under(REPO) / MAC.replace(":", "-").lower() / "sonoff-backup/flash.bin"
    try:
        rsa = holds_rsa_ca(source.read_bytes())
        return (EMU / ("stub-cloud-rsa" if rsa else "stub-cloud") / "ca.crt").read_bytes()
    except (OSError, ValueError):
        return None


def untrusting_starts():
    """This device's saved stub devices holding a Sonoff firmware - on the device, or
    in its backup, which a run back to Sonoff's writes - that does not trust
    the CA the stub serves it now: made before that CA was made again, or
    before emu.sh chose an RSA one for it. It turns the stub down
    (unknown_ca), never comes online, and its scenario fails its checks."""
    pem = stub_ca()
    found = []
    last4 = MAC.replace(":", "").lower()[-4:]
    for kept in sorted((EMU / "saved").glob("stub-*")):
        name = kept.name.removeprefix("stub-").removesuffix(f"-{last4}")
        if saved(name) != kept.name:
            continue   # another device's
        for flash in [kept / "flash.bin", *records_under(kept).glob("*/sonoff-backup/flash.bin")]:
            try:
                trusted = trusts(flash.read_bytes(), pem)
            except (OSError, ValueError):
                continue   # not a whole flash, or none
            if trusted is False:
                found.append(kept.name)
                break
    return found


def prepared(said, stop_after, stopped):
    """Whether a starting device's run got where it was meant to: stopped once
    `stop_after` was done, or else converted. Saved whatever happened, a device
    whose run stopped at the start was once listed as saved, and its
    scenarios all failed."""
    return stopped if stop_after else any(found.startswith("Converted") for found in REPORT_RESULT.findall(said))


def make_start(save, args, stop_after=None):
    """One starting device, saved as `save`: a run with `args`, stopped once
    `stop_after` is done, or else converted."""
    n = int(os.environ.get("AUTOFLASH_EMU_PREP_INSTANCE", "1"))
    # A run that could not start would count as one that failed, and take the
    # device saved before it away.
    if sh("docker", "ps", "-q", "--filter", f"name=^{name(n)}$").stdout.strip():
        raise SystemExit(f"{name(n)} is running: stop it, or choose another with AUTOFLASH_EMU_PREP_INSTANCE")
    unattended = str(REPO / "tools/espressif/emulator/unattended-test.sh")

    def stopped_after(step, sc, since, stopped):
        while True:
            if re.search(rf"\] {step} done", sc.current_run(since, None) or ""):
                stopped.set()
                sc.signal("INT"); time.sleep(0.5); sc.signal("INT")
                return
            time.sleep(0.2)

    log = OUT / f"prep-{save}.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    # A fresh device has the cloud from the start, as a C2 scenario's does.
    fresh = "--fresh" in args
    sc = Scenario(n, {"id": "prep", "step": "C2" if fresh else "", "fault": "prep", "arg": ""}, log)
    monitor = DeviceMonitor(sc).start()
    since = time.time()
    stopped = threading.Event()
    if stop_after:
        threading.Thread(target=stopped_after, args=(stop_after, sc, since, stopped), daemon=True).start()
    stub = ["--stub-cloud"] if STUB_CLOUD else []
    with open(log, "w", encoding="utf-8") as out:
        # A conversion: no readout to send, so no API beside it (--no-api).
        # The device's product as every scenario's run has it (Scenario.run_tool).
        env = dict(os.environ, AUTOFLASH_EMU_STUB_PRODUCT_MODEL=device_product()) if STUB_CLOUD else None
        subprocess.call([unattended, f"--instance={n}", f"--device={MAC}", f"--save={saved(save)}", "--no-api",
                         *stub, *args], stdout=out, stderr=subprocess.STDOUT, env=env)
    monitor.stop.set()
    device_internet(n, False)
    CLOUD.release(n)
    kept = EMU / "saved" / saved(save)
    if not prepared(log.read_text(encoding="utf-8", errors="replace"), stop_after, stopped.is_set()):
        # Not the device it should be: none rather than one every scenario
        # from it would start wrong.
        shutil.rmtree(kept, ignore_errors=True)
        raise SystemExit(f"{saved(save)} NOT MADE: its run did not get there - see {log}")
    print(saved(save), "saved" if (kept / "flash.bin").exists() else "MISSING", flush=True)


def prep():
    """The starting devices, saved: one converted to each firmware; a stock device
    stopped just after C2 (the installer sent, before the backup) for each;
    and one of those carried on as far as D1 (on the interim Tasmota), so the
    steps after D1 need no cloud; and the one on Tasmota with no backup."""
    for target in ("tasmota", "esphome"):
        make_start(ROUTES[f"{target}>sonoff"][0], ["--fresh", "--", "--firmware", target])
        (converted_start(target) / "firmware.json").write_text(json.dumps(installs(target)), encoding="utf-8")
        stock = ROUTES[f"sonoff>{target}"][0]
        make_start(stock, ["--fresh", "--", "--firmware", target], stop_after="C2")
        make_start(AFTER_D1[f"sonoff>{target}"], [f"--from={saved(stock)}", "--", "--firmware", target],
                   stop_after="D1")
    no_backup_start()


def no_backup_start():
    """The device saved on Tasmota, copied without its Sonoff backup: a computer
    that never had one. The copy leaves it out; nothing is deleted."""
    kept = converted_start("nobackup")
    shutil.rmtree(kept, ignore_errors=True)
    shutil.copytree(converted_start("tasmota"), kept, symlinks=True,
                    ignore=shutil.ignore_patterns(records.BACKUP))
    print(kept.name, "saved", flush=True)


def earlier_tasmota():
    """The app of the official Tasmota release before this one, as firmware.json
    pins it: Tasmota's own, fetched from where it is published."""
    earlier = installs("tasmota")["earlier"][0]
    file = earlier["files"]["app"]
    url = f"https://github.com/arendst/Tasmota/releases/download/{earlier['tag']}/{file['name']}"
    with urllib.request.urlopen(url, timeout=120) as answer:
        app = answer.read()
    if hashlib.sha256(app).hexdigest() != file["sha256"]:
        raise SystemExit(f"{url} is not the file firmware.json pins")
    return app


def prep_update():
    """The update scenarios' starting devices, which no run of this checkout can
    make, the tool installing only this release. For update>tasmota, the device
    saved on Tasmota, the release before in its app0; for update>esphome, a
    device EARLIER_RELEASE's tool converted: a build of this checkout's has the device's Wi-Fi
    written in, so cannot simply be put in its slot."""
    kept = EMU / "saved" / saved(ROUTES["update>tasmota"][0])
    shutil.rmtree(kept, ignore_errors=True)
    shutil.copytree(converted_start("tasmota"), kept, symlinks=True)
    app = earlier_tasmota()
    with open(kept / "flash.bin", "r+b") as flash:
        flash.seek(APP0_OFFSET)
        flash.write(app)
    print(kept.name, "saved", flush=True)
    make_start(ROUTES["update>esphome"][0],
               ["--fresh", f"--code={legacy_tree(EARLIER_RELEASE)}", "--", "--firmware", "esphome"])


def listing(quick=False):
    for sc in scenarios(quick):
        print(sc["id"], kind_of(sc))


def starved_label(row):
    starved = row["verdict"] != "PASS" and row.get("notes", {}).get("memory", {}).get("starved")
    return "  (memory-starved)" if starved else ""


def suite_roots():
    """This suite's folders here, and those kept from other machines."""
    return [EMU, *scenario_times.kept(EMU)]


def found():
    for line in fault_yield.found(fault_yield.rows(*suite_roots())):
        print(line)


def report():
    from collections import Counter
    rows = latest()
    print(Counter(r["verdict"] for r in rows.values()))
    for r in rows.values():
        if r["verdict"] != "PASS":
            print(f"  {r['verdict']:16} {r['id']}  runs={r.get('runs')}  {r.get('results', r.get('error'))}{starved_label(r)}")
    # The device kept busiest by one Berry phase (device_busy): a real S60TPG once
    # stopped answering at 70-90 s, so the longest are worth a look.
    busiest = sorted(((max(busy.values()), r["id"], step) for r in rows.values()
                      for busy in [r.get("notes", {}).get("device_busy") or {}] if busy
                      for step, s in busy.items() if s == max(busy.values())), reverse=True)[:10]
    if busiest:
        print("Longest the device was kept busy by one phase:")
        for seconds, scenario, step in busiest:
            print(f"  {seconds:6.1f}s  {step:3}  {scenario}")


def main():
    import argparse
    global MAC, DEVICE, ROUTES, STUB_CLOUD, OUT, RESULTS, LOGS
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device", help="the backed-up device's MAC, as emu.sh --device takes it")
    parser.add_argument("--stub-cloud", action="store_true",
                        help="each emulator serves its own local eWeLink stub (offline, fully "
                             "parallel, no cloud lock); the default uses the real cloud")
    parser.add_argument("command", choices=["prep", "prep-update", "list", "batch", "report", "found", "share"])
    parser.add_argument("file", nargs="?", help="batch: the scenario ids to run (else all); share: those to "
                        "share out; list: quick, for a routine run's")
    parser.add_argument("workers", nargs="*", type=int,
                        help="share: how many emulators each machine runs, one number a machine")
    parser.add_argument("--times-from", action="append", default=[], type=Path, metavar="DIR",
                        help="share: another machine's suite folders, copied here, for how long its scenarios took")
    args = parser.parse_args()
    if args.command == "share":
        if not args.file or not args.workers:
            parser.error("share needs the ids file and each machine's emulators: share all.txt 4 4 6")
        try:
            for line in scenario_times.write_shares(args.file, args.workers, scenarios(),
                                                    scenario_times.times(*suite_roots(), *args.times_from)):
                print(line)
        except ValueError as exc:
            parser.error(str(exc))
        return
    STUB_CLOUD = STUB_CLOUD or args.stub_cloud
    if STUB_CLOUD and "AUTOFLASH_EMU_SUITE_DIR" not in os.environ:
        # Apart from the real cloud's results, whose batch would otherwise
        # take a stub run's passes for its own.
        OUT = EMU / "suite-stub"
        RESULTS, LOGS = OUT / "results.jsonl", OUT / "logs"
    if args.command in ("prep", "prep-update", "batch") and not args.device:
        parser.error("--device is needed: the backed-up device the emulators run copies of")
    MAC = args.device
    DEVICE = device_kind(MAC)
    ROUTES = routes_for(DEVICE)
    if DEVICE is not kinds.SONOFF_S60 and "AUTOFLASH_EMU_SUITE_DIR" not in os.environ:
        # Its scenarios share the S60's names, not their passes.
        OUT = OUT.with_name(f"{OUT.name}-{DEVICE.key}")
        RESULTS, LOGS = OUT / "results.jsonl", OUT / "logs"
    if args.command == "list" and args.file not in (None, "quick"):
        parser.error("list takes only quick")
    {"prep": prep, "prep-update": prep_update, "list": lambda: listing(args.file == "quick"), "report": report, "found": found,
     "batch": lambda: batch(args.file)}[args.command]()


if __name__ == "__main__":
    main()
