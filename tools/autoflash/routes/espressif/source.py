"""What an Espressif device runs when a run starts, where what it answers as
does not say: part way, past the commit or not, as this computer's runs
left it; ESPHome in safe mode; Tasmota's own safeboot.  And whether one on
Tasmota leaves it for ESPHome without the hub.  The device is found, whatever
its family, by routes/source.py.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import time
from typing import TYPE_CHECKING

from tools.autoflash import kinds, output, records, runs, unlock
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort
from tools.autoflash.routes.espressif import evidence
from tools.autoflash.state import ATTEMPTED, FAILED, PENDING, RunState
from tools.autoflash.routes.espressif import images

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def past_the_commit(ctx: Context, running: str) -> str:
    """"committed" for a device part way that a run of this computer's left past
    its commit: on Tasmota's layout, not Sonoff's, so the way on is official
    Tasmota first - never the hub's steps, which need Sonoff's two slots.
    An official Tasmota without the device's template is one too, if a run left
    it so; any other is "untemplated", never "midway": the hub's steps begin
    by sending ESPHome an update, which no Tasmota takes."""
    if running not in ("midway", "untemplated"):
        return running
    mac = ctx.state.input("device_mac")
    for directory in reversed(runs.run_directories()):
        if directory.resolve() == ctx.dir.resolve() or records.mac_of_run(directory) != mac:
            continue
        try:
            state = RunState.open(directory)
            report = json.loads((directory / "live" / "commit-report.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
            try:
                state = RunState.open(directory)
            except (OSError, ValueError):
                continue
        committed = state.is_done("E3") or report.get("status") == "PASS"
        # Stopped as the commit ran (seen in the fault suite: Ctrl-C a second
        # after the device fetched the new table), with no report: it finished if
        # the device fetched the table and runs the startup firmware now, which
        # starts only once the new layout is in place - as E3's own check says.
        finished_alone = (not committed and state.status("E3") == ATTEMPTED
                          and evidence.table_fetched(directory / "live") and ctx.device.is_safeboot())
        if not (committed or finished_alone) and wrote_nothing(state):
            # Stopped before it wrote anything - X1 sent to a Tasmota, finding
            # no update service: seen hiding the run before it.
            continue
        if (committed or finished_alone) and not state.is_done("H1"):
            if finished_alone:
                output.say("    Its last run was stopped as the commit ran, and the device finished it:")
                output.say("    it fetched the new layout, and runs the startup firmware now.")
            else:
                output.say("    This computer's record of it says its last run got past the commit:")
            output.say("    it is on Tasmota's layout, so official Tasmota goes on first.")
            # G1 puts the official startup firmware back over the one that run
            # put on, and checks it is that one by the run's own record of it.
            ctx.evidence.mkdir(parents=True, exist_ok=True)
            for name in ("commit-report.json", "stage-report.json"):
                kept = directory / "live" / name
                if kept.is_file() and not (ctx.evidence / name).exists():
                    shutil.copy2(kept, ctx.evidence / name)
            if not state.is_done("D5") or state.input("startup_unproven"):
                # That run never put ESPHome's bootloader on - begun by 1.2.0 -
                # and its commit put that version's startup firmware on.
                ctx.state.set_input("bootloader_kept", "sonoff")
                ctx.state.set_input("startup_unproven", True)
            return "committed"
        return tasmota_missing(ctx, running)  # its latest run says otherwise
    return tasmota_missing(ctx, running)


def leaves_tasmota_direct(ctx: Context) -> bool:
    """Whether a device on Tasmota, going to ESPHome, can skip the hub: only with
    ESPHome's bootloader on it already, as the hub is where a device gets it. One
    that will not say goes through the hub, which is right for any device; and so
    does one with a temporary table, which only the hub's Tasmota can replace."""
    kind = kinds.of_run(ctx.state)
    if images.layout_of(kind).needs_temporary:
        return False
    chip = images.chip_of(kind)
    bootloader = images.published_bootloader(chip)
    try:
        found = ctx.device.sha_region(chip.bootloader_at, len(bootloader), timeout=30)
    except (DeviceError, OSError):
        found = None
    if found == hashlib.sha256(bootloader).hexdigest():
        return True
    output.say("    Its bootloader is not this tool's, so it goes by way of the firmware that")
    output.say("    installs one, as every device this tool converts ends with the same.")
    return False


# ESPHome started ten times in a row, each stopped within a minute, takes that
# for a boot loop and starts in safe mode: Wi-Fi and its update service, no
# page and no relay, as the mover has. Five minutes on, it restarts as itself.
ESPHOME_SAFE_MODE_WAIT = 400.0


def left_on_esphome(ctx: Context) -> bool:
    """Whether this computer's latest run of the device that wrote to it left
    it on ESPHome, checked (I2): one past that, the mover is gone for good."""
    mac = ctx.state.input("device_mac")
    for directory in reversed(runs.run_directories()):
        if directory.resolve() == ctx.dir.resolve() or records.mac_of_run(directory) != mac:
            continue
        try:
            state = RunState.open(directory)
        except (OSError, ValueError):
            continue
        if wrote_nothing(state):
            continue
        return runs.run_firmware(state) == "esphome" and state.is_done("I2")
    return False


def esphome_safe_mode(ctx: Context, running: str) -> str:
    """What a device that answers as the mover runs: ESPHome in safe mode, if
    a run left it on ESPHome, waited out until it restarts as itself - taken
    for part way, it was sent the startup firmware, even to stay on ESPHome."""
    if not left_on_esphome(ctx):
        return running
    host = ctx.state.input("device_ip")
    output.say("    It answers only ESPHome's update service, though this computer left it on")
    output.say("    ESPHome: ESPHome in safe mode, after starting ten times in a row, each")
    output.say("    stopped within a minute. It restarts as itself within five minutes.")
    output.say("    Waiting for it - leave it switched on...")
    started = time.monotonic()
    while time.monotonic() - started < ESPHOME_SAFE_MODE_WAIT:
        found = unlock.identify(ctx, host)
        if found == "esphome-login":
            unlock.ask_esphome_login(ctx, host)
            found = unlock.identify(ctx, host)
        if found == "esphome":
            output.say(f"    ESPHome answered after {time.monotonic() - started:.0f}s.")
            return "esphome"
        time.sleep(5)
    raise Abort(
        "the device answers only ESPHome's update service, and has not started ESPHome "
        f"in {ESPHOME_SAFE_MODE_WAIT / 60:.0f} minutes. Leave it switched on for ten "
        "minutes, then run the same command again")


def wrote_nothing(state: RunState) -> bool:
    """Whether a run wrote nothing to the device: no step that writes done or
    part way, and each that failed stopped before writing."""
    steps = state.data.get("steps", {})
    return all(state.status(step) == PENDING
               or (state.status(step) == FAILED and steps.get(step, {}).get("nothing_written"))
               for step in evidence.DESTRUCTIVE_STEPS)


# Tasmota's own safeboot, as a build of Tasmota's own says it is - "15.6.0
# (release-safeboot)" - where this tool's startup firmware, built from the
# same source here, says "15.6.0(safeboot)". Tasmota's own runs only on
# Tasmota's layout: no route puts it in Sonoff's slots, only G1 into Tasmota's
# safeboot partition. Its page is all it serves: no commands, no Information.
OFFICIAL_SAFEBOOT_RE = re.compile(r"\(\s*[a-z0-9]+-safeboot\s*\)", re.IGNORECASE)


def tasmota_missing(ctx: Context, running: str) -> str:
    """"safeboot" for a device part way by the look of it - a safeboot answering -
    that no run of this computer's left past the commit, and whose safeboot is
    Tasmota's own: a converted device, on Tasmota's layout, not starting its
    Tasmota (its own upload of another firmware failed part way, say, or it
    was switched to its safeboot). Taken for part way, it went down steps that
    need Sonoff's layout, and stopped at D4."""
    if running != "midway":
        return running
    try:
        version = ctx.device.firmware_version() if ctx.device.is_safeboot() else ""
    except (DeviceError, OSError):
        version = ""
    if not OFFICIAL_SAFEBOOT_RE.search(version or ""):
        return running
    output.say(f"    It runs Tasmota's own safeboot ({version}), so is on Tasmota's layout: its Tasmota")
    output.say("    is started again, or put back, first.")
    return "safeboot"


