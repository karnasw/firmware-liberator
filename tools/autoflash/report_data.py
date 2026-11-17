"""A run's report as data, for the project's API to count: what the screen's
report says (report.py), as fixed words and bounded numbers, and nothing that
identifies the person, their computer or their network. Read from the run's
record, not from the screen's words, which change as the screen does.

The device is named by a hash of its MAC, the same from any computer, so a
second run on it is known as the same device; the API stores only a keyed
hash of that. Every value here is one tools/repo/report_schema.py allows,
which the API holds every report to.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
from typing import Any, TYPE_CHECKING

from tools.autoflash import __version__, failures, hostsys, interrupt, kinds, openbeken, records, routes, runs
from tools.autoflash import report
from tools.ewelink.firmware_name import A_NAME
from tools.ewelink.get_device_key import API

if TYPE_CHECKING:
    from tools.autoflash.context import Context
    from tools.autoflash.readout import Became

FORMAT = 1
DEVICE_PREFIX = "firmware-liberator device:"
# "put-back": the run ended with the device on the firmware it came from
# (report.run_result's "put back on ..."); "cancelled": the person stopped it.
RESULTS = ("converted", "updated", "put-back", "failed", "crashed", "cancelled")
# How the person stopped a cancelled run, sent as its stopped "error": q at a
# question, Ctrl-C (at once, or confirmed at a stage's end), or its window closed.
STOPPED_BY = ("quit", "ctrl-c", "window-closed")
# The highest number a device converted on one computer can have in a report
# (records.number_conversion): no cap on what people convert, only on what a
# report can claim.
NTH_DEVICE_LIMIT = 1_000_000
OS_FAMILIES = ("windows", "macos", "linux")
# The processor, as platform.machine() names it: which portable Pythons are used.
ARCHES = ("x86-64", "arm64", "other")
# Where an unexpected error came from in the tool's code (runner.crash_site).
CRASH_SITE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,59} at tools/[A-Za-z0-9_./-]{1,120}\.py:[0-9]{1,6}")
# How letting the device through this computer's firewall went (firewall.ensure_firewall):
# no firewall in the way, a rule added, one there already, adding it failed,
# or an organisation's policy that ignores rules added here.
FIREWALL_OUTCOMES = ("none", "added", "there", "failed", "managed")
# What carries this computer to the device's network (hostsys.connection_towards).
CONNECTIONS = ("wifi", "ethernet", "other")
# How the tool was started (AUTOFLASH_LAUNCHER, set by start.sh and start.cmd),
# and from what: a git clone, or a download.
LAUNCHERS = ("start.sh", "start.cmd", "other")
INSTALLS = ("git", "download")
# The longest firmware name a report carries: Sonoff's run to 30 or so.
FIRMWARE_NAME_LIMIT = 64
# eWeLink's regions, for a run that signed in.
EWELINK_REGIONS = tuple(API)
# The most a count or time sent can be: no figure needs more.
CTRL_C_LIMIT, NETWORKS_LIMIT, WAIT_LIMIT = 99, 64, 7 * 86400
TOOL_VERSION = re.compile(r"\d{1,3}\.\d{1,3}\.\d{1,3}")
# Where the Python came from, by report.python_description's word for it;
# "other" for one it has since learnt.
PYTHON_SOURCES = {"portable": "portable", "start.sh's": "start-sh", "venv": "venv", "system": "system"}
PYTHON_WORDS = (*PYTHON_SOURCES.values(), "other")
# The end-of-run relay check (report.check_hardware and its kin), by how
# their Relay answer starts: anything else was not run or could not be.
CHECKS = ("passed", "failed", "not-run")
# How the run left the meter's calibration, from what its steps found:
# Sonoff's own, Tasmota carrying Sonoff's over (with its factory figures or
# not), skipped, ESPHome's Calibration sensor's source, OpenBeken's driver's
# (openbeken.calibration_state: Sonoff's store, or changed since), or not
# checked.
METER_WORDS = ("sonoff", "carried", "carried-factory", "skipped", "stock-nvs", "from-backup", "missing",
               "stock-store", "changed", "reported", "not-checked")
# How a device's readout ended (readout.Became), as its report says it: the
# website took it; it was kept unsent; the website had readouts like it, or
# could take no more; it was passed by (one of several), the person stopped
# it, or it failed or crashed.
ACCEPTED = "accepted"
KEPT = "kept"
NOT_NEEDED = "not-needed"
WEBSITE_FULL = "website-full"
SKIPPED = "skipped"
NOT_READ = "not-read"
CANCELLED = "cancelled"
FAILED = "failed"
CRASHED = "crashed"
READOUT_RESULTS = (ACCEPTED, KEPT, NOT_NEEDED, WEBSITE_FULL, SKIPPED, NOT_READ, CANCELLED, FAILED, CRASHED)
TEXT_LIMIT = 200
MODEL = re.compile(r"[A-Z0-9][A-Z0-9-]{0,23}")
VERSION = re.compile(r"\d{1,4}(\.\d{1,4}){1,3}")
OS_VERSION = re.compile(r"[A-Za-z0-9 ._()+/-]{1,48}")


def device_hash(mac: str | None) -> str | None:
    """The device, as the same 64 hex digits from any computer: None without a MAC."""
    digits = re.sub(r"[^0-9a-f]", "", (mac or "").lower())
    if len(digits) != 12:
        return None
    normal = ":".join(digits[i:i + 2] for i in range(0, 12, 2))
    return hashlib.sha256(f"{DEVICE_PREFIX}{normal}".encode()).hexdigest()


def os_info() -> dict[str, str]:
    """The operating system's family, and its version as report.py words it."""
    family = "windows" if hostsys.IS_WINDOWS else "macos" if hostsys.IS_MAC else "linux"
    words = re.sub(r"^(Windows|macOS|Linux) ", "", report.computer_description()).strip()
    words = "".join(char for char in words if OS_VERSION.fullmatch(char))[:48].strip()
    machine = platform.machine().lower()
    arch = "x86-64" if machine in ("x86_64", "amd64") else "arm64" if machine in ("arm64", "aarch64") else "other"
    return {"family": family, "version": words or "unknown", "arch": arch}


def python_info() -> dict[str, str]:
    version, _, source = report.python_description().partition(" (")
    return {"version": version if VERSION.fullmatch(version) else "unknown",
            "source": PYTHON_SOURCES.get(source.rstrip(")"), "other")}


# How a calibration's words begin, as the firmware says them, and the
# report's word for each: our ESPHome's Calibration sensor (devices/*/
# esphome.yaml, its `source`), and OpenBeken's driver (openbeken.
# CALIBRATION_STATES, CALIBRATION_CHANGED). tests/autoflash/test_report_data.py
# holds every word those say to one of these, so none is sent as "reported".
CALIBRATION_WORDS = (("stock NVS", "stock-nvs"), ("from backup", "from-backup"), ("missing", "missing"),
                     (openbeken.CALIBRATION_STATES["stock"], "stock-store"), (openbeken.CALIBRATION_CHANGED, "changed"))


def meter_word(captures: dict[str, Any], firmware: str | None) -> str:
    """How the run left the meter's calibration: one of METER_WORDS."""
    if firmware == "sonoff":
        return "sonoff"
    found: Any = None
    for key, value in captures.items():
        if key.endswith(".meter") or key.endswith(".calibration"):
            found = value   # the last step's counts: an update checks it again
    if isinstance(found, dict) and "skipped" in found:
        return "skipped"
    if isinstance(found, dict) and "changed" in found:
        return "carried-factory" if found.get("factory") else "carried"
    said = found.get("calibration") if isinstance(found, dict) else found
    if isinstance(said, str) and said:
        return next((word for start, word in CALIBRATION_WORDS if said.startswith(start)), "reported")
    return "not-checked"


def checks_word(hardware: dict[str, str] | None) -> str:
    """The report's Checks answer (report.checks_passed), as the API takes it."""
    return {"pass": "passed", "fail": "failed"}.get(report.checks_passed(hardware or {}), "not-run")


def plain(text: str, limit: int = TEXT_LIMIT) -> str:
    """`text` for the API: report.public_text's, in printable ASCII alone."""
    text = "".join(char for char in report.public_text(text, limit) if " " <= char <= "~")
    return text[:limit]


def stopped_step(ctx: Context) -> str | None:
    """The step a run stopped at, as report.run_result finds it."""
    failed = getattr(ctx, "last_failed_step", "")
    if failed:
        return failed
    return next((step for step, _, _ in routes.STEPS if not ctx.state.is_done(step)), None)


def stages(ctx: Context) -> dict[str, dict[str, int]]:
    """Each step the run timed: its seconds, how long it kept the device busy,
    how many times it was tried, how long it waited on the person, why this
    computer joined a device's own network in it, if it did, and how many times the
    person was asked to switch the device off and on: where the setup network
    and power cycles, which a run should never need, keep being needed."""
    captures = ctx.state.data.get("captures", {})
    found = {}
    for step, _, _ in routes.STEPS:
        seconds = captures.get(f"{step}.seconds")
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            continue
        busy = captures.get(f"{step}.device_busy_seconds")
        tries = captures.get(f"{step}.attempts")
        waited = captures.get(f"{step}.waited")
        cycles = captures.get(f"{step}.power_cycles")
        found[step] = {"seconds": min(int(seconds), 86400),
                       "busy": min(int(busy), 86400) if isinstance(busy, (int, float)) else 0,
                       "tries": min(tries, 99) if isinstance(tries, int) and tries > 0 else 1,
                       # How long the person took over its questions.
                       "wait": min(max(int(waited), 0), WAIT_LIMIT) if isinstance(waited, (int, float)) else 0,
                       "setup_network": report.own_network_at(ctx.state, step),
                       "power_cycles": min(cycles, 99) if isinstance(cycles, int) and cycles > 0 else 0}
    return found


def converted_to_another(ctx: Context) -> bool:
    """Whether the run converted the device to a firmware other than Sonoff's:
    what the API counts as a conversion (website/api/reports.js) - not an
    update, a device put back, or one taken back to Sonoff's."""
    state = ctx.state
    return (not state.input("put_back") and runs.run_source(state) != "outdated"
            and routes.recorded_firmware(state) not in (None, "sonoff"))


def readout_payload(report_id: str, became: Became, seconds: float) -> dict[str, Any]:
    """One device's readout's report as the API takes it, but its proof: one
    report a readout, sent as it ends (readout.run). Nothing of the device's
    name in the account, nor the reference its readout was sent under; for
    how many one person converts, how many devices this computer has
    converted (records.converted_here)."""
    kind = kinds.for_account(became.product, became.model, among=kinds.EVERY)
    product, stock_model = became.product.strip().upper(), became.model.strip().upper()
    # Where eWeLink gave no product model, a kind with only one says it (report.run_model).
    product = product or (kind.only_product if kind else "")
    return {
        "format": FORMAT,
        "id": report_id,
        "tag": "dev" if os.environ.get("AUTOFLASH_REPORT_TAG") == "dev" else "user",
        "tool": __version__,
        "kind": "readout",
        "result": became.outcome,
        "error": became.error if became.outcome == FAILED else None,
        "family": kind.family if kind else None,
        "device": kind.key if kind else None,
        "model": product if MODEL.fullmatch(product) else None,
        "stock_model": stock_model if MODEL.fullmatch(stock_model) else None,
        "stock_version": became.version if VERSION.fullmatch(became.version) else None,
        "whole": became.whole,
        "device_hash": device_hash(became.mac),
        "os": os_info(),
        "python": python_info(),
        "seconds": max(0, min(int(seconds), 7 * 86400)),
        "converted_here": min(records.converted_here(), NTH_DEVICE_LIMIT),
    }


def outcome(ctx: Context, ending: str) -> tuple[str, dict[str, Any] | None]:
    """How the run ended, as its report says it: its result, and where it
    stopped and why (None for one that converted) - the class of what it
    stopped with (failures.classify), or how the person stopped it."""
    if ending == "converted":
        put_back = ctx.state.input("put_back") or {}
        return ("put-back" if put_back else "updated" if runs.run_source(ctx.state) == "outdated"
                else "converted"), None
    if ending == "cancelled":
        # Where people stop, and how: no words, as nothing went wrong.
        return ending, {"step": stopped_step(ctx), "error": getattr(ctx, "stopped_by", None) or "quit", "text": "",
                        "crash_site": None}
    reason = ctx.last_failure or ""
    text = plain(reason)
    # The class what stopped it said (errors.Abort's failure), else classed by
    # the words sent, as the API checks them (website/api/review.js), but for
    # words cut short, where the whole reason says more.
    site = getattr(ctx, "crash_site", None)
    said = getattr(ctx, "last_failure_class", None)
    # A crash escaped the runner: in the step under way, or in none (cli.py).
    step = (getattr(ctx, "last_failed_step", "") or None) if ending == "crashed" else stopped_step(ctx)
    return ending, {"step": step,
                    "error": "crash" if ending == "crashed"
                    else said if said in failures.CLASSES
                    else failures.classify(reason if text.endswith("...") else text),
                    "text": text,
                    "crash_site": site if isinstance(site, str) and CRASH_SITE.fullmatch(site) else None}


def payload(ctx: Context, report_id: str, seconds: float, resumed: bool, ending: str,
            hardware: dict[str, str] | None = None) -> dict[str, Any]:
    """The run's report as the API takes it, but its proof. `ending`:
    "converted", "failed", "crashed" or "cancelled" (how: ctx.stopped_by,
    one of STOPPED_BY)."""
    state = ctx.state
    kind = kinds.by_key(state.input("device_kind"))
    firmware = routes.recorded_firmware(state)
    espressif = kinds.of_run(state).family == "espressif"
    record = records.read_device(state.input("device_mac") or "")
    captures = state.data.get("captures", {})
    source = runs.run_source(state)
    product = report.run_model(ctx)
    stock = str(state.input("stock_version") or record.get("stock_version") or "")
    readings = [value for key, value in captures.items() if key.endswith(".signal_dbm")
                and isinstance(value, int) and not isinstance(value, bool)]
    put_back = state.input("put_back") or {}
    result, stopped = outcome(ctx, ending)
    backup = state.input("backup_outcome") or ("existing" if routes.sonoff_backup_exists(ctx) else "not taken")
    # For a conversion, the device's number among those converted on this
    # computer (records.number_conversion): how many one computer converts.
    nth = record.get(records.CONVERTED_NUMBER)
    if result != "converted" or not converted_to_another(ctx) or not isinstance(nth, int) or isinstance(nth, bool):
        nth = None
    # The version that began a run another carried on: where resuming goes wrong.
    begun = state.input("begun_on")
    if not resumed or not isinstance(begun, str) or not TOOL_VERSION.fullmatch(begun) or begun == __version__:
        begun = None
    # The longest a device took to answer once switched off and on (arrival.until_back).
    back = state.input("back_after_power_cycle")
    back = min(back, WAIT_LIMIT) if isinstance(back, int) and not isinstance(back, bool) and back >= 0 else None
    return {
        "format": FORMAT,
        "id": report_id,
        "tag": "dev" if os.environ.get("AUTOFLASH_REPORT_TAG") == "dev" else "user",
        "tool": __version__,
        "kind": "run",
        "result": result,
        "stopped": stopped,
        "family": kind.family if kind else None,
        "device": kind.key if kind else None,
        "model": product if MODEL.fullmatch(product) else None,
        "stock_version": stock if VERSION.fullmatch(stock) else None,
        "device_hash": device_hash(state.input("device_mac")),
        "firmware": firmware,
        "source": put_back.get("came_from") or source,
        "backup": backup,
        "identity": captures.get("R1.identity") if captures.get("R1.identity") in ("backup", "kept", "account")
        else None,
        "os": os_info(),
        "python": python_info(),
        "seconds": max(0, min(int(seconds), 7 * 86400)),
        "resumed": bool(resumed),
        "stages": stages(ctx),
        "signal": max(-127, min(min(readings), 0)) if readings else None,
        # Each only where its route has it, as report.report_fields shows them.
        "wifi_typed": bool(state.input("wifi_typed")) if espressif else None,
        "setup_network": report.own_network_joined(state),
        "power_cycled": bool(state.input("power_cycles")),
        "checks": checks_word(hardware),
        "meter": meter_word(captures, firmware) if kinds.of_run(state).has_meter else None,
        "nth_device": nth,
        "chip": chip_info(state),
        "computer": computer_info(state),
        "ewelink_region": ewelink_region(ctx),
        "ctrl_c_carried_on": min(interrupt.carried_on(), CTRL_C_LIMIT),
        "launcher": os.environ.get("AUTOFLASH_LAUNCHER") if os.environ.get("AUTOFLASH_LAUNCHER") in LAUNCHERS
        else "other",
        "install": "git" if (runs.ROOT / ".git").exists() else "download",
        **firmware_name_info(state, captures),
        "begun_on": begun,
        "back_after_power_cycle": back,
    }


def firmware_name_info(state: Any, captures: dict[str, Any]) -> dict[str, Any]:
    """The name the device's own updater checks, as the run learned it
    (account.note_firmware_name, kept as its step's firmware_name), and
    whether it is the name this tool wraps its firmware under for that kind:
    one that is not may turn the update down."""
    name = None
    for key, value in captures.items():
        if key.endswith(".firmware_name") and isinstance(value, str) and value:
            name = value
    if name is None or len(name) > FIRMWARE_NAME_LIMIT or not A_NAME.fullmatch(name):
        return {"firmware_name": None, "firmware_name_expected": None}
    kind = kinds.by_key(state.input("device_kind"))
    return {"firmware_name": name, "firmware_name_expected": name == kind.wrapper_model if kind else None}


def chip_info(state: Any) -> dict[str, Any]:
    """The chip's revision, the flash chip's ID and size, as the installer
    said them (installer.note_hardware): an Espressif run's alone."""
    found = state.input("hardware") or {}
    revision, flash_id, size = found.get("revision"), found.get("flash_id"), found.get("flash_mb")
    return {"revision": revision if isinstance(revision, str) and re.fullmatch(r"\d{1,3}\.\d{1,3}", revision) else None,
            "flash_id": flash_id if isinstance(flash_id, str) and re.fullmatch(r"[0-9a-f]{6}", flash_id) else None,
            "flash_mb": size if isinstance(size, int) and not isinstance(size, bool) and 0 < size <= 128 else None}


def computer_info(state: Any) -> dict[str, Any]:
    """What on this computer's side may get in a run's way: what carries it to
    the device's network, a VPN, how many networks it is on, the note on other
    security software shown, the firewall rule, administrator rights asked
    for. Asked of the system as the report is made; nothing is sent anywhere."""
    device_ip = state.input("device_ip")
    connection = hostsys.connection_towards(device_ip) if device_ip else None
    firewall = state.input("firewall_outcome")
    return {"connection": connection if connection in CONNECTIONS else None,
            "vpn": hostsys.vpn_active(),
            "networks": min(len(hostsys.lan_networks()), NETWORKS_LIMIT),
            "security_note": bool(hostsys.security_note_shown),
            "firewall": firewall if firewall in FIREWALL_OUTCOMES else None,
            "admin": bool(hostsys.asked_for_admin)}


def ewelink_region(ctx: Context) -> str | None:
    """The region of the eWeLink account the run signed in to, if it did."""
    held = getattr(getattr(ctx, "ewelink", None), "held", None)
    region = held[0] if isinstance(held, tuple) and held else None
    return region if region in EWELINK_REGIONS else None
