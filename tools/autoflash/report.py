"""The report of a run, for the person to share: what was converted, how long
each stage took, what the device's hardware does, and where to post it.
"""

from __future__ import annotations

import itertools
import os
import platform
import re
import sys
import textwrap
import time
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import __version__, esphome, hostsys, join, kinds, output, records, report_upload, routes, runs, testers
from tools.autoflash.device import DeviceError
from tools.autoflash.runs import ROOT
from tools.ewelink import relay as ewelink_relay
from tools.autoflash.routes import update

if TYPE_CHECKING:
    from tools.autoflash.context import Context
    from tools.autoflash.state import RunState

# Where a run that failed is told to open an issue: the repository's issues,
# by a short address of the website's, one line on the screen
# (tools/repo/cloudflare_settings.py, ISSUES).
ISSUES_URL = "https://firmwareliberator.com/issues"
# The width the report is laid out at on the clipboard (clipboard_text), so
# one pasted anywhere reads the same, whatever window it came from.
CLIPBOARD_COLUMNS = 100
# The label of the report's id, which a run that failed shows under where to
# open an issue, and puts under its report on the clipboard: an issue pasted
# from it names its report.
REPORT_ID = "Report ID"
# Shown once a run has succeeded, after the report, in this order.
SPONSORS_URL = "https://github.com/sponsors/adamsthws"
KOFI_URL = "https://ko-fi.com/adamsthws"
DONATE_URL = "https://www.paypal.me/adamsthws"


def device_page_url(device_ip: str | None) -> str | None:
    """The address to open the converted device's Tasmota page at.

    Inside the emulator (tools/espressif/emulator) the device's address only exists in its
    container; the emulator says where your browser reaches it instead.
    """
    emulated = os.environ.get("AUTOFLASH_EMULATOR_URL")
    if emulated:
        return emulated
    return f"http://{device_ip}" if device_ip else None


def computer_description() -> str:
    """The operating system and its version, as someone else would want to know it."""
    if hostsys.IS_WINDOWS:
        release, build = platform.release(), platform.version()
        # Python before 3.12 calls every 10.0 build "10"; Windows 11 is 10.0
        # from build 22000 (the server releases there are named otherwise).
        if (release == "10" and sys.getwindowsversion().product_type == 1
                and int(build.rpartition(".")[2] or 0) >= 22000):
            release = "11"
        system = f"Windows {release} (build {build})"
    elif hostsys.IS_MAC:
        system = f"macOS {platform.mac_ver()[0]}"
    else:
        try:
            system = platform.freedesktop_os_release().get("PRETTY_NAME") or ""
        except OSError:
            system = ""
        system = system or f"Linux {platform.release()}"
    return system


def python_description() -> str:
    """The Python the tool ran on, and where it came from (start.sh, start.cmd)."""
    running = Path(sys.executable).resolve()
    portable = Path(os.environ.get("AUTOFLASH_PYTHON_HOME") or ROOT / ".python").resolve()
    if portable in running.parents:
        source = "portable"
    elif (hostsys.IS_MAC and "Python.framework" in running.parts
          and any((portable / name).exists() for name in ("installed-by-autoflash", "installed-by-s60"))):
        source = "start.sh's"
    elif sys.prefix != sys.base_prefix:
        source = "venv"
    else:
        source = "system"
    return f"{platform.python_version()} ({source})"


def public_text(text: str, limit: int = 240) -> str:
    """`text` fit for a public post: on one line, with nothing that identifies
    the device, the network or the person - not even a private address."""
    text = output.scrub(" ".join(str(text).split()))
    text = re.sub(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w])", "<address>", text)
    return text if len(text) <= limit else text[:limit - 3].rstrip() + "..."


def run_result(ctx: Context, ending: str) -> str:
    """How the run ended, for the report: "converted" ("updated", for an
    update of this tool's firmware), or where and why it stopped.

    ending: "converted", "failed" or "crashed".
    """
    if ending == "converted":
        if ctx.state.input("put_back"):
            return f"put back on {routes.FIRMWARES[runs.run_firmware(ctx.state)]}"
        return "updated" if runs.run_source(ctx.state) == "outdated" else "converted"
    # The stage that failed, not the first left to do: a failure may have sent
    # earlier steps back to be done again (E3 refused as changed sends E1, E2).
    failed = getattr(ctx, "last_failed_step", "")
    stopped = next(((number, step) for number, (step, _, _) in enumerate(routes.STEPS, 1)
                    if (step == failed if failed else not ctx.state.is_done(step))), None)
    # No total before the firmware is chosen: STEPS is then only the start
    # every route shares, and how many stages would follow is not known.
    total = f" of {len(routes.STEPS)}" if routes.ROUTE[0] else ""
    where = (f"stage {stopped[0]}{total} ({routes.stage_name(stopped[1])})"
             if stopped else "the end")
    reason = ctx.last_failure or (
        ctx.state.data.get("steps", {}).get(stopped[1], {}).get("error", "") if stopped else "")
    head = "crashed" if ending == "crashed" else "stopped"
    return f"{head} at {where}" + (f": {public_text(reason)}" if reason else "")


# What a run's starting point is called in its report: one for each of SOURCES.
CONVERTED_FROM = {"sonoff": "Sonoff", "tasmota": "Tasmota", "esphome": "ESPHome", "openbeken": "OpenBeken",
                  "midway": "unfinished run", "committed": "unfinished run",
                  "installer": "unfinished run", "interim": "unfinished run",
                  "safeboot": "Tasmota safeboot",
                  "untemplated": "Tasmota, no template",
                  "outdated": "earlier release"}   # of the run's firmware, named before it


def converted_from(ctx: Context, firmware: str | None, stock_version: str = "") -> str:
    """What the device ran at the run's start, for its report: "__", to fill in,
    for a run that stopped before it found that out (the device not on the
    network at all) - not Sonoff's, which runs.run_source takes when none is
    recorded, for a run of 1.x.  Sonoff's firmware with its version
    (`stock_version`), where the run learned it; another's without, as the
    version a converted device's record keeps is its backup's, not what it ran."""
    if ctx.state.input("replaced_build"):
        return "its own build"
    source = (ctx.state.input("put_back") or {}).get("came_from") or runs.run_source(ctx.state)
    found = (ctx.state.input("source") or ctx.state.input("stock_version")
             or any(map(ctx.state.is_done, ctx.state.data.get("steps", {}))))
    if not found:
        return "__"
    if source == "sonoff":
        return f"{CONVERTED_FROM[source]} {stock_version}".rstrip()
    return (f"{routes.FIRMWARES[firmware]}, " if source == "outdated" and firmware else "") + CONVERTED_FROM[source]


def own_network_at(state: RunState, step: str) -> str | None:
    """Why this computer first joined a device's own network during `step`
    (join.OWN_NETWORK_REASONS), on either family - the installer's bridge
    network, Tasmota's setup network, ESPHome's or the mover's - as
    runner.execute keeps it; None where it joined none."""
    why = state.data.get("captures", {}).get(f"{step}.{join.OWN_NETWORK}")
    return why if why in join.OWN_NETWORK_REASONS else None


def own_network_joined(state: RunState) -> bool:
    """Whether this computer joined a device's own network during the run."""
    return bool(state.input(join.OWN_NETWORK))


def run_model(ctx: Context) -> str:
    """The device's product model as eWeLink names it (S60TPG), for the report
    shown and the one sent alike; "" where nothing says. What the run asked
    eWeLink comes first; then what the device's record kept from an earlier
    run, as a run from a converted device asks neither Sonoff's firmware nor
    eWeLink; then its kind's, where the kind has only one; last the account's
    record of it, found by its MAC, only where the run is signed in already,
    as a report asks no password. What the account says is kept in the
    device's record, for the next run on this computer."""
    state = ctx.state
    asked = str(state.input("product_model") or "").strip().upper()
    kind = kinds.by_key(state.input("device_kind"))
    if asked or kind is None:
        return asked
    mac = state.input("device_mac") or ""
    # A record's model only if one of the kind's: an earlier version kept the
    # kind's short name (S60) where eWeLink gave none.
    found = kind.product_of(records.read_device(mac).get("model")) or kind.only_product
    if found or not mac or not getattr(getattr(ctx, "ewelink", None), "held", None):
        return found
    for device in ctx.account_devices() or []:
        sta_mac = (device.get("params") or {}).get("staMac")
        if sta_mac and hostsys.normalise_mac(str(sta_mac)) == hostsys.normalise_mac(mac):
            found = kind.product_of(str(device.get("productModel") or ""))
            if found:
                records.remember_device(mac, model=found)
            return found
    return ""


def report_fields(ctx: Context, hardware: dict[str, str] | None = None,
                  result: str = "converted") -> list[tuple[str, str]]:
    """This run's answers for a Test reports post, as (label, value) - nothing private.

    No addresses, Wi-Fi name, account or device id: only what is useful to
    someone deciding whether the tool will work for them.  `hardware` is what
    check_hardware() measured.  "__" marks what the run could not tell.
    """
    hardware = hardware or {}
    kind = kinds.of_run(ctx.state)
    # None for a run stopped before its firmware was chosen (A3).
    firmware = routes.recorded_firmware(ctx.state)
    espressif = kind.family == "espressif"
    # A run that starts on a converted device never asks Sonoff's firmware or
    # eWeLink: what the device's record kept from its first conversion - the
    # model, and the Sonoff firmware its backup holds - stands in.
    record = records.read_device(ctx.state.input("device_mac") or "")
    # A converted device with no record here says its kind, if not its exact model.
    model = run_model(ctx) or (kind.name if runs.run_source(ctx.state) != "sonoff" else "")
    stock_version = str(ctx.state.input("stock_version") or record.get("stock_version") or "")
    captures = ctx.state.data.get("captures", {})
    retries = {number: captures[f"{step}.attempts"] - 1 for number, (step, _, _) in enumerate(routes.STEPS, 1)
               if isinstance(captures.get(f"{step}.attempts"), int) and captures[f"{step}.attempts"] > 1}
    retried = [f"{number} ({count} {'retry' if count == 1 else 'retries'})"
               for number, count in retries.items()]
    # The lowest of the run's readings (each step's signal_dbm): from Sonoff's
    # advert at A2 or eWeLink's record at B1, then from whichever firmware the
    # device runs as a step begins, where it gives one.
    readings = [value for key, value in captures.items() if key.endswith(".signal_dbm")
                and isinstance(value, int) and not isinstance(value, bool)]
    signal = min(readings) if readings else None
    fields = [
        ("Result", result),
        ("Firmware", routes.FIRMWARES[firmware] if firmware else "not chosen"),
        ("From", converted_from(ctx, firmware, stock_version)),
        ("Liberator", __version__),
        ("Model", model or "__"),
        # How it went, never where it is or which device: the path holds the MAC.
        # A run from a converted device takes none: one is there or not.
        ("Backup", records.BACKUP_OUTCOMES[ctx.state.input("backup_outcome")
                                      or ("existing" if routes.sonoff_backup_exists(ctx) else "not taken")][1]),
        # Where going back to Sonoff's firmware took the device's identity from:
        # only on such a run (sonoff.step_sonoff_restore), and in its log, not on
        # the screen (LOG_ONLY).
        *([("Identity", SONOFF_IDENTITY.get(captures.get("R1.identity"), "not read"))]
          if espressif and firmware == "sonoff" else []),
        ("OS", computer_description()),
        ("Python", python_description()),
        # How long it took, each stage's time and how long it kept the device
        # busy are not shown: the report sent carries them (report_data).
        ("Retries", ", ".join(retried) or "none"),
        ("Signal", f"{signal} dBm" if isinstance(signal, int) else "not recorded"),
        # Only where the route has it: the Wi-Fi typed is an Espressif route's.
        *([("Wi-Fi typed", "yes" if ctx.state.input("wifi_typed") else "no")] if espressif else []),
        ("Setup network", "yes" if own_network_joined(ctx.state) else "no"),
        # Whether the device had to be switched off and on for its new firmware
        # to come back (arrival.until_back), on either family.
        ("Power cycled", "true" if ctx.state.input("power_cycles") else "false"),
        # Only where there is something to check (checks_apply).
        *([("Checks", checks_passed(hardware))] if checks_apply(kind, firmware) else []),
        # Not how the meter's calibration went: a device's readings a few
        # percent out is no news to the person converting it. The report
        # sent holds it (report_data.meter_word), the log why.
    ]
    return [(label, answer(value)) for label, value in fields]


SONOFF_IDENTITY = {"backup": "backup", "kept": "device", "account": "eWeLink account"}


# Every line the relay's check says, and what it says of the check: passed,
# failed, or not run (None). The report's Checks answer, the API's and the
# fault suites' read this, never how the words begin: a line not here fails
# tests/autoflash/test_report.py, and is taken for a failure meanwhile.
RELAY_SWITCHES = "switches"
METER_CONFIRMS = "switches (meter confirms)"
SWITCHES_THROUGH_EWELINK = "switches (through eWeLink)"
DID_NOT_SWITCH, RELAY_UNCHECKED, RELAY_NOT_CHECKED, MOTOR_MODE = (
    "did not switch", "could not be checked", "not checked", "motor mode")
RELAY_LINES = {RELAY_SWITCHES: True, METER_CONFIRMS: True, "switches (meter unchanged)": True,
               SWITCHES_THROUGH_EWELINK: True, DID_NOT_SWITCH: False, RELAY_UNCHECKED: False,
               RELAY_NOT_CHECKED: None, MOTOR_MODE: None}
# How the release check's line ends where the device runs the release installed.
AS_INSTALLED = ", as installed"


def check_passed(label: str, value: str) -> bool | None:
    """Whether one end-of-run check passed, by its line: None for one that did
    not run, or a line that is no check of its own (Power meter, which the
    Relay line's answer takes in)."""
    if label == "Relay":
        return RELAY_LINES.get(value, False)
    if label == "Release":
        return value.endswith(AS_INSTALLED)
    if label == "Sensor":
        return None if value == NO_READING else not value.startswith(OUT_OF_RANGE)
    return None


def checks_passed(hardware: dict[str, str]) -> str:
    """The end-of-run checks as one answer, whatever the device: "fail" if any
    failed, "pass" if any ran, else "not run".  What each found is in the log."""
    found = [passed for label, value in hardware.items() if (passed := check_passed(label, value)) is not None]
    return "fail" if False in found else "pass" if found else "not run"


def checks_apply(kind: kinds.Kind, firmware: str | None) -> bool:
    """Whether a run on `firmware` has an end-of-run check to make of a device
    of `kind`, and its report a Checks line: a relay to switch, sensors to read,
    or a firmware whose release the device can be asked."""
    return kind.has_relay or bool(kind.sensors) or firmware in update.RELEASE


def answer(value: str) -> str:
    """A report answer as shown, here and as the run checks it: starting with a
    capital - "No", "Converted".  Only the first letter, so "S60TPG" and
    "6m 32s" stay as they are."""
    return value[:1].upper() + value[1:]


# Where each answer goes on the screen: the device's on the right, the run's
# and this computer's on the left.  The fault suites read each answer up to
# the gap after it, wherever it is (tools/espressif/emulator/fault_suite.py).
RIGHT = ("Firmware", "From", "Model", "Backup", "Signal", "Checks")
# Answers the run's log has, for the fault suites and someone helping, and the
# screen does not: how the run ended, which the line above the report says
# ("Conversion complete!", "FAILED: ..."), in full where it stopped; where an
# identity came from, nothing the person needs; whether the Wi-Fi was typed
# or a setup network joined, which in every run anyone reported neither was;
# and whether the device was switched off and on, which the run asked of
# the person itself.
LOG_ONLY = ("Result", "Identity", "Wi-Fi typed", "Setup network", "Power cycled")
# Columns each side needs at the least, else one column, as Markdown has it.
SIDE = 34


def report_screen(fields: list[tuple[str, str]], columns: int | None = None,
                  right: tuple[str, ...] = RIGHT, below: list[tuple[str, str]] = (),
                  balance: bool = True) -> list[str]:
    """For the terminal: plain labels, lined up, in two columns where the
    window is wide enough - half the rows - else one. `right`: the labels
    of the right-hand column, the device's - a conversion's (RIGHT), or a
    readout's (readout.report_fields); the rest on the left. `below`: (label, value) pairs each on
    a row of its own under them, the window's whole width to fit a list in
    before it wraps - between the words a value's no-break spaces do not join.
    Every value on the left starts in one column: those of the left-hand
    column and those below."""
    margin, gap = "    ", "   "
    columns = columns or output.terminal_size((100, 24)).columns
    side = (columns - len(margin) - len(gap) - 2) // 2
    under = list(below)

    def rows_under(width: int) -> list[str]:
        rows = []
        for label, value in under:
            lines = textwrap.wrap(value, max(20, columns - len(margin) - width - 2)) or [""]
            rows.append(f"{margin}{label + ':':<{width}} {lines[0]}".replace("\u00a0", " "))
            rows.extend(f"{margin}{'':<{width}} {line}".replace("\u00a0", " ") for line in lines[1:])
        return rows
    if side < SIDE:
        width = max(len(label) for label, _ in [*fields, *under]) + 1
        return [f"{margin}{label + ':':<{width}} {value}" for label, value in fields] + rows_under(width)

    def cells(column: list[tuple[str, str]], width: int = 0) -> list[str]:
        width = width or max((len(label) for label, _ in column), default=0) + 1
        rows = []
        for label, value in column:
            lines = textwrap.wrap(value, side - width - 1) or [""]
            rows.append(f"{label + ':':<{width}} {lines[0]}")
            rows.extend(" " * (width + 1) + line for line in lines[1:])
        return rows
    left = [(label, value) for label, value in fields if label not in right]
    right = [(label, value) for label, value in fields if label in right]
    # The columns ending together, or the left one row longer, which looks the
    # better: the device's answers at the bottom go left while its column is the
    # longer (an S61's, with fewer of this computer's). Not where `balance` is
    # off - a readout's counts, which stay together.
    while balance and len(right) > len(left):
        left.append(right.pop())
    width = max((len(label) for label, _ in [*left, *under]), default=0) + 1
    left, right = cells(left, width), cells(right)
    lines = [(margin + f"{one:<{side}}{gap}{two}").rstrip()
              for one, two in itertools.zip_longest(left, right, fillvalue="")]
    return lines + rows_under(width)


# Long enough for the CSE7766 and Tasmota's energy driver to report a new
# reading. With every relay off Tasmota keeps its last reading until its energy
# watchdog zeroes it, after 4 s without one, on a 1 s tick: 5 s.
METER_SETTLE = 6.0
# Each switch of the check is tried this often, this many seconds apart, until
# the relay reads as asked: a reply lost as the device's Wi-Fi blinks - ESPHome
# taking its own network down once it has joined yours - is not a relay that
# did not switch (seen in the Beken fault suite: "did not switch", its meter
# reading 107 W on and 0 W off). Switching to where it is already is harmless.
SWITCH_TRIES, SWITCH_PAUSE = 3, 3.0


def switched_to(switch: Callable[[], bool]) -> bool:
    """Whether `switch()` - one try at switching the relay, true once it
    reads as asked - got there in SWITCH_TRIES, a lost reply tried again."""
    for attempt in range(1, SWITCH_TRIES + 1):
        try:
            if switch():
                return True
        except (DeviceError, OSError, ValueError):
            if attempt == SWITCH_TRIES:
                raise
        if attempt < SWITCH_TRIES:
            time.sleep(SWITCH_PAUSE)
    return False


def read_again(read: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """The meter's reading, `read()` tried again as a switch is
    (switched_to) where its answer was lost: none, or an error the network
    gave. None where every try was lost."""
    for attempt in range(1, SWITCH_TRIES + 1):
        try:
            found = read()
        except (DeviceError, OSError, ValueError):
            if attempt == SWITCH_TRIES:
                raise
            found = None
        if found is not None:
            return found
        if attempt < SWITCH_TRIES:
            time.sleep(SWITCH_PAUSE)
    return None
# How long a device back on Sonoff's firmware may take to reach eWeLink once the
# run has seen it on the network: an Espressif run's last step sees it there,
# seconds before its firmware has dialled eWeLink (seconds). Looked for every 10 s.
ONLINE_WAIT = 180.0
# How long a device on Tasmota, ESPHome or OpenBeken may take to answer again before its
# relay is checked: one that lost power or its Wi-Fi as the run ended is still
# starting, or waiting out a router's restart of up to three minutes and then
# rejoining (seconds).
ANSWER_WAIT = 240.0
MAINS_VOLTS, DEAD_VOLTS = 90.0, 20.0     # clearly live / clearly switched off
LOAD_WATTS, IDLE_WATTS = 1.0, 0.5
# A reading as the relay has just gone off may still count the last pulse from
# before: 3.3 W, one BL0937 pulse at 106 W in ESPHome's 2 s window, the next
# reading 0 W (seen in the Beken fault suite). Not idle, it is read this often
# more, this far apart (one window and more), before it is the meter's answer.
OFF_TRIES, OFF_PAUSE = 2, 3.0


def meter_number(reading: dict[str, Any] | None, key: str) -> float | None:
    value = (reading or {}).get(key)
    # Tasmota gives a device with two channels each as a list (the Dual
    # R3's Power): the first is Relay 1's, the one switched.
    if isinstance(value, list) and value:
        value = value[0]
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def settled_off(read: Callable[[], dict[str, Any] | None]) -> dict[str, Any] | None:
    """The meter's reading with the relay off: `read()`, taken again
    (OFF_TRIES) while it still shows a load, as a pulse from before the
    switch may be counted in the first; a meter that never drops is still
    the meter's answer."""
    found = read()
    for _ in range(OFF_TRIES):
        watts = meter_number(found, "Power")
        if watts is None or watts < IDLE_WATTS:
            break
        time.sleep(OFF_PAUSE)
        found = read() or found
    return found


def describe_hardware(switched: bool, restored: bool,
                      on: dict[str, Any] | None, off: dict[str, Any] | None, metered: bool = True) -> dict[str, str]:
    """The report's Relay and Power meter lines, from the relay's
    replies and the meter read with it on and with it off; the Relay line
    alone for a device with no meter (`metered` False).

    The relay answering both commands proves the firmware drives it; a reading
    that drops when it is off - the voltage, where the meter is on the switched
    side, or the power of whatever is plugged in - proves it really switches.
    """
    def reading(values: dict[str, Any] | None) -> str:
        volts, watts = meter_number(values, "Voltage"), meter_number(values, "Power")
        if volts is None and watts is None:
            return "no reading"
        return ", ".join(part for part in (
            f"{volts:g} V" if volts is not None else "",
            f"{watts:g} W" if watts is not None else "") if part)

    v_on, v_off = meter_number(on, "Voltage"), meter_number(off, "Voltage")
    p_on, p_off = meter_number(on, "Power"), meter_number(off, "Power")
    seen = ((v_on is not None and v_off is not None and v_on >= MAINS_VOLTS and v_off < DEAD_VOLTS)
            or (p_on is not None and p_off is not None and p_on >= LOAD_WATTS and p_off < IDLE_WATTS))
    if not (switched and restored):
        relay = DID_NOT_SWITCH
    elif not metered:
        return {"Relay": RELAY_SWITCHES}
    elif seen:
        relay = METER_CONFIRMS
    else:
        relay = "switches (meter unchanged)"
    if not metered:
        return {"Relay": relay}
    return {
        "Relay": relay,
        "Power meter": "no readings" if on is None and off is None
                       else f"on: {reading(on)}; off: {reading(off)}",
    }


def has_meter(ctx: Context) -> bool:
    """Whether the run's kind of device has a power meter: a check of one
    without leaves the meter out, neither read nor shown."""
    return kinds.of_run(ctx.state).has_meter


def checking(metered: bool) -> str:
    return "Checking the relay and power meter..." if metered else "Checking the relay..."


def check_hardware(ctx: Context) -> dict[str, str]:
    """Switch the relay to the other state and back - two switches - reading the
    meter after each, so one reading is with it on and one with it off.

    Whatever is plugged into the device is switched for a few seconds, without
    asking: the run has just finished, and the device ends as it started. A device
    that lost power or its Wi-Fi as the run ended is waited for first.
    """
    device = ctx.device

    def state(reply: Any) -> str:
        # POWER, or POWER1 on a template with more than one relay.
        return str(reply.get("POWER", reply.get("POWER1", ""))).upper() if isinstance(reply, dict) else ""

    def switch(on: bool) -> bool:
        return state(device.command(f"Power {'ON' if on else 'OFF'}")) == ("ON" if on else "OFF")

    metered = has_meter(ctx)

    def energy() -> dict[str, Any] | None:
        status = device.command("Status 10")
        found = status.get("StatusSNS", {}).get("ENERGY") if isinstance(status, dict) else None
        return found if isinstance(found, dict) else None

    def meter(off: bool) -> dict[str, Any] | None:
        if not metered:
            return None
        time.sleep(METER_SETTLE)
        return settled_off(lambda: read_again(energy)) if off else read_again(energy)

    output.say(checking(metered))
    try:
        waited = device.wait_reachable(timeout=ANSWER_WAIT)
        if waited >= 1:
            output.say(f"    The device answered again after {waited:.0f}s.")
        was_on = state(device.command("Power")) == "ON"
        switched = switched_to(lambda: switch(not was_on))
        first = meter(off=was_on)
        restored = switched_to(lambda: switch(was_on))
        second = meter(off=not was_on)
    except (DeviceError, OSError, ValueError) as exc:
        output.say(f"    The device did not answer the check: {exc}")
        return said(UNCHECKED, metered)
    on, off = (second, first) if was_on else (first, second)
    return said(describe_hardware(switched, restored, on, off, metered))


def for_device(result: dict[str, str], metered: bool) -> dict[str, str]:
    """`result` for a device with a meter or without (`metered`): one without
    has no Power meter line at all, as "not checked" would say it has one."""
    return result if metered else {label: value for label, value in result.items() if label != "Power meter"}


# What a check the device did not answer found.
UNCHECKED = {"Relay": RELAY_UNCHECKED, "Power meter": "could not be checked"}


def said(result: dict[str, str], metered: bool = True) -> dict[str, str]:
    """`result`, each of its answers shown: every check says them, whatever it
    found, as a fault suite reads them off the screen (for_device's)."""
    result = for_device(result, metered)
    for label, value in result.items():
        output.say(f"    {label}: {answer(value)}")
    return result


def check_esphome_hardware(ctx: Context) -> dict[str, str]:
    """check_hardware's two switches and two readings, through ESPHome's web API."""
    host = ctx.state.input("device_ip")
    metered = has_meter(ctx)
    output.say(checking(metered))
    started = time.monotonic()
    was_on = esphome.relay(host)
    while was_on is None and time.monotonic() - started < ANSWER_WAIT:   # still starting, perhaps
        time.sleep(5)
        was_on = esphome.relay(host)
    if was_on is None:
        output.say(f"    The device did not answer the check within {ANSWER_WAIT:.0f}s.")
        return said(UNCHECKED, metered)

    def meter(off: bool) -> dict[str, Any] | None:
        if not metered:
            return None
        time.sleep(METER_SETTLE)
        read = lambda: read_again(lambda: esphome.meter(host) or None)   # noqa: E731
        return settled_off(read) if off else read()
    switched = switched_to(lambda: esphome.switch_relay(host, not was_on))
    first = meter(off=was_on)
    restored = switched_to(lambda: esphome.switch_relay(host, was_on))
    second = meter(off=not was_on)
    on, off = (second, first) if was_on else (first, second)
    return said(describe_hardware(switched, restored, on, off, metered))


def check_ewelink_hardware(ctx: Context) -> dict[str, str]:
    """check_hardware's two switches and two readings for a device back on
    Sonoff's firmware, through eWeLink as the app switches it and asks for
    its readings.  One that sends none (no meter, or the DualR3, which
    refuses the asking) has its relay checked alone."""
    metered = has_meter(ctx)
    device_id = str(records.read_device(ctx.state.input("device_mac") or "").get("ewelink_id") or "")
    if not device_id:
        return for_device({"Relay": RELAY_NOT_CHECKED, "Power meter": "not checked"}, metered)
    output.say("Checking the relay through eWeLink...")
    record = wait_until_online(ctx, device_id)
    if record is not None and ewelink_relay.motor_mode(record.get("params") or {}):
        return said({"Relay": MOTOR_MODE, "Power meter": "not checked"}, metered)
    if record is not None:
        checked = ewelink_relay.check_relay(
            account=ctx.ewelink, device_id=device_id, say=output.say)
    else:
        output.say(f"    eWeLink has not shown the device online within {ONLINE_WAIT:.0f}s, so its relay "
                   "could not be switched there. Check the eWeLink app.")
        checked = None
    if checked is None:
        return said({"Relay": RELAY_UNCHECKED, "Power meter": "not checked"}, metered)
    if checked.on is None and checked.off is None:
        relay = SWITCHES_THROUGH_EWELINK if checked.switched and checked.restored else DID_NOT_SWITCH
        return said({"Relay": relay, "Power meter": "not checked"}, metered)
    return said(describe_hardware(checked.switched, checked.restored, checked.on, checked.off, metered),
                metered)


# How often, and how far apart, the device is asked its release: an ESPHome's
# API may still be starting when the relay has been switched through its web
# server (seconds).
RELEASE_TRIES, RELEASE_PAUSE = 3, 5.0


def check_release(ctx: Context) -> dict[str, str]:
    """The Release line: whether the device runs the release of its firmware
    this version installs, as this project built it (update.RELEASE says only
    of a build it published). Nothing for Sonoff's firmware, whose release is
    eWeLink's to choose, or an ESPHome the person gave (--esphome)."""
    firmware = runs.run_firmware(ctx.state)
    if firmware not in update.RELEASE or (firmware == "esphome" and ctx.esphome_firmware):
        return {}
    published = routes.family_of(ctx.state).published_version(kinds.of_run(ctx.state), firmware)
    if not published:
        return {}
    running = None
    for attempt in range(RELEASE_TRIES):
        if attempt:
            time.sleep(RELEASE_PAUSE)
        try:
            running = update.RELEASE[firmware](ctx)
        except (DeviceError, OSError, ValueError):
            running = None
        if running is not None:
            break
    if running == published:
        return {"Release": f"{published}{AS_INSTALLED}"}
    return {"Release": f"{running}, not {published}" if running else f"not this project's {published}"}


# What each quantity a sensor measures may read and be believed (Kind.sensors):
# a THS01's own range. A reading past it is the sensor failing, not the room.
SENSOR_RANGES = {"temperature": (-40.0, 125.0), "humidity": (0.0, 100.0)}
SENSOR_UNITS = {"temperature": "C", "humidity": "%"}
NO_READING, OUT_OF_RANGE = "no reading", "out of range: "


def describe_sensors(found: dict[str, float]) -> dict[str, str]:
    """The Sensor line, from the readings `found`: none is no reading, as from
    a device with nothing plugged into its socket - no failure."""
    if not found:
        return {"Sensor": NO_READING}
    text = ", ".join(f"{value:g} {SENSOR_UNITS[quantity]}" for quantity, value in found.items())
    believed = all(SENSOR_RANGES[quantity][0] <= value <= SENSOR_RANGES[quantity][1]
                   for quantity, value in found.items())
    return {"Sensor": text if believed else OUT_OF_RANGE + text}


def tasmota_readings(ctx: Context, quantities: tuple[str, ...]) -> dict[str, float]:
    """`quantities` as Tasmota's (or OpenBeken's) Status 10 gives them, in
    whichever sensor's section ("SI7021": {"Temperature": 21.3}), in Celsius."""
    status = ctx.device.command("Status 10")
    sensors = status.get("StatusSNS", {}) if isinstance(status, dict) else {}
    fahrenheit = isinstance(sensors, dict) and sensors.get("TempUnit") == "F"
    found = {}
    for section in (sensors.values() if isinstance(sensors, dict) else ()):
        if not isinstance(section, dict):
            continue
        for quantity in quantities:
            value = section.get(quantity.capitalize())
            if quantity not in found and isinstance(value, (int, float)) and not isinstance(value, bool):
                found[quantity] = (value - 32) / 1.8 if quantity == "temperature" and fahrenheit else float(value)
    return {quantity: round(value, 1) for quantity, value in found.items()}


def check_sensors(ctx: Context) -> dict[str, str]:
    """The Sensor line, for a device with sensors (Kind.sensors): their readings
    and whether they are believable. Not on Sonoff's firmware: what eWeLink
    names its readings has not been seen here."""
    quantities = kinds.of_run(ctx.state).sensors
    firmware = runs.run_firmware(ctx.state)
    if not quantities or firmware == "sonoff":
        return {}
    try:
        found = (esphome.readings(ctx.state.input("device_ip"), quantities) if firmware == "esphome"
                 else tasmota_readings(ctx, quantities))
    except (DeviceError, OSError, ValueError):
        found = {}
    return describe_sensors(found)


def end_of_run_checks(ctx: Context) -> dict[str, str]:
    """Every end-of-run check the device has: its relay switched (each
    firmware's own way), its release asked, its sensors read. The relay's
    lines are said as they always are; the others only when they fail, being
    in the log whatever they found."""
    firmware = runs.run_firmware(ctx.state)
    # OpenBeken takes Tasmota's commands (Power, Status 10): the same check.
    relay = {"esphome": check_esphome_hardware, "tasmota": check_hardware, "openbeken": check_hardware,
             "sonoff": check_ewelink_hardware}.get(firmware) if kinds.of_run(ctx.state).has_relay else None
    found = relay(ctx) if relay else {}
    for label, value in {**check_release(ctx), **check_sensors(ctx)}.items():
        (output.say if check_passed(label, value) is False else output.log_only)(f"    {label}: {answer(value)}")
        found[label] = value
    return found


def wait_until_online(ctx: Context, device_id: str) -> dict[str, Any] | None:
    """The device's record once eWeLink shows it online, with its relay's state
    in it (or that it drives a motor, with no relay), and it answers there,
    within ONLINE_WAIT: what a Beken run's arrival waits for already, and an
    Espressif run's does not, as its last step sees the device on the network
    only. The state comes with the device's first reports, a second or two
    after it answers (seen in the emulator, where its record starts empty),
    and switching needs it. None by then, and at once when eWeLink cannot be
    reached."""
    started = time.monotonic()
    while True:
        devices = ctx.account_devices()
        record = next((found for found in devices or [] if found.get("deviceid") == device_id and found.get("online")
                       and (ewelink_relay.relay_state(found.get("params") or {}) is not None
                            or ewelink_relay.motor_mode(found.get("params") or {}))), None)
        if record and ewelink_relay.answers(account=ctx.ewelink, device_id=device_id):
            output.progress_clear()
            waited = time.monotonic() - started
            if waited >= 1:
                output.say(f"    eWeLink shows the device online after {waited:.0f}s.")
            return record
        if devices is None or time.monotonic() - started >= ONLINE_WAIT:
            output.progress_clear()
            return None
        output.progress(f"waiting for eWeLink to show the device online... {time.monotonic() - started:.0f}s")
        time.sleep(10)


def invite_report(ctx: Context, seconds: float, resumed: bool) -> None:
    """Ask for a report, with this run's details ready to paste."""
    from tools.autoflash import report_data   # it reads this module's words
    mac = ctx.state.input("device_mac")
    if mac and records.MAC_RE.match(mac) and report_data.converted_to_another(ctx):
        records.number_conversion(mac)   # whether a report is sent or not, so the count stays true
    with output.working("Generating your report..."):
        hardware = end_of_run_checks(ctx)
        fields = report_fields(ctx, hardware, result=run_result(ctx, "converted"))
    report_upload.send(ctx, "converted", seconds, resumed, hardware)
    offer_report(ctx, fields)
    hold_out_hat()


def hold_out_hat() -> None:
    """The links to support the project, under the report of a run that
    finished: a conversion's, or a readout's (readout.end). A run that failed
    has the way to open an issue there instead (ask_for_an_issue)."""
    output.tell("")
    output.show("You've reached the bit where I hold out my hat!", output.GREEN)
    output.tell("Free software, not free biscuits... Throw your friendly neighbourhood dev a biscuit:")
    output.show_links([(SPONSORS_URL, "GitHub"), (KOFI_URL, "Ko-fi"), (DONATE_URL, "PayPal")], hint=True)


def report_ending(code: int | None, ending: str | None) -> str | None:
    """Which stopped run offers its report: "failed" or "crashed", else None.

    A run that failed offers it as one that converted does (that one already
    has).  Not one the operator stopped - quit at a question (130) or Ctrl-C -
    nor one this computer stopped (the terminal closed: nobody to read it).
    """
    if ending in ("crashed", "cancelled"):
        return "crashed" if ending == "crashed" else None
    return "failed" if code not in (None, 0, 130) else None


def send_cancelled_report(ctx: Context, how: str) -> None:
    """A run the person stopped (`how`: one of report_data.STOPPED_BY) sends
    its report too, and shows none: the stages people stop at say which
    worry them. Nothing is asked of them."""
    ctx.stopped_by = how
    report_upload.send(ctx, "cancelled", time.monotonic() - ctx.run_started - output.waited(), ctx.run_resumed)


def invite_stopped_report(ctx: Context, ending: str) -> None:
    """A run that failed asks for its report too: the runs that fail say most
    about what to fix, and leaving them out would make the tool look better than
    it is.  Not one the operator stopped - that says nothing about the tool.
    ending: "failed" or "crashed"."""
    seconds = time.monotonic() - ctx.run_started - output.waited()
    fields = report_fields(ctx, result=run_result(ctx, ending))
    report_upload.send(ctx, ending, seconds, ctx.run_resumed)
    offer_report(ctx, fields, stopped=run_result(ctx, ending))


def clipboard_text(lines: list[str], result: str | None = None) -> str:
    """A report as it goes on the clipboard: as shown (`lines`, laid out
    CLIPBOARD_COLUMNS wide, its report's ID under it), its margin taken off;
    a conversion's starting with its `result` in full, which its screen
    says above it instead."""
    return (f"Result: {result}\n" if result else "") + textwrap.dedent("\n".join(lines)).strip("\n") + "\n"


def ask_for_an_issue(copy: str | None = None, report_id: str | None = None) -> None:
    """Where the links to support the project would be, for a run that failed
    or crashed - not one stopped by choice: the way to open an issue, its
    report's ID under it where one was sent, the report on the clipboard to
    paste in (`copy`, clipboard_text) and said so where it took, and the
    run's log, which one needs. A run that failed before any stage began has
    no report (cli.issue_alone): nothing is copied. The tool is meant to finish every run, so one
    it could not is something to fix. A run that worked leaves the clipboard
    alone."""
    output.tell("")
    output.tell("If it should not have stopped, please open an issue here:")
    output.show_link(ISSUES_URL)
    # Under the link, as far in.
    if report_id:
        output.tell(f"    {REPORT_ID}: {report_id}")
    if copy and hostsys.copy_to_clipboard(copy):
        output.tell("    This report is on your clipboard.")
    output.tell("")
    output.tell("This run's log:")
    # From the checkout: short enough for its row in a window 80 wide.
    log = output.log_path()
    output.tell(f"    {runs.display_path(log) if log else testers.LOG_ELSEWHERE}")


def offer_report(ctx: Context, fields: list[tuple[str, str]], stopped: str | None = None) -> None:
    """The run's report on the screen, under what the run said last. For a run
    that stopped (`stopped`: how, as run_result says it), the way to open an
    issue for it after it (ask_for_an_issue)."""
    ctx.reported = True
    output.tell("")
    for label, value in fields:
        if label in LOG_ONLY:
            output.log_only(f"    {label}: {value}")
    shown = [(label, value) for label, value in fields if label not in LOG_ONLY]
    for line in report_screen(shown):
        output.tell(line)
    if stopped:
        report_id = getattr(ctx, "last_report_id", None)
        copy = report_screen(shown, CLIPBOARD_COLUMNS, below=[(REPORT_ID, report_id)] if report_id else ())
        ask_for_an_issue(clipboard_text(copy, dict(fields).get("Result")), report_id)
