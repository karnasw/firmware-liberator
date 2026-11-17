"""A readout of an Espressif device the tool does not convert yet: one with a
chip there is a bridge for (an ESP32-C3 or a classic ESP32).

Its Sonoff updater is sent the bridge alone, wrapped under the firmware name
eWeLink gives the device: the bridge goes in the spare app slot, reads the
whole flash, and has already chosen Sonoff's slot again for the next start, so
switching the device off and on puts it back as it was, settings and pairing
kept. Only what was in the spare slot is lost (often the Sonoff version before
the last update).

The copy is anonymised (tools/espressif/scrub.py): its Wi-Fi passwords taken
out, as far as its settings show them, and its eWeLink identity replaced by a
made-up one, so it still starts in the emulator as the device did. The copy
kept on the computer, as the device's Sonoff backup, is the device's own,
untouched.

Each stage leans on the conversion's own helpers: the bridge sent as the
conversion sends it (tools/espressif/serve_tasmota_ota.py), and found as it is
found (routes/espressif/installer.py).
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import firewall, hostsys, names, output, records, prompts, readout, runs, safety_locks
from tools.autoflash import setup_network
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.routes.espressif import installer
from tools.autoflash.runs import ROOT
from tools.espressif import build_vendor_ota, chips, device_facts, dump_stock_flash, flashmap, scrub

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The chips the bridge runs on, as a firmware name says them (firmware_name.chip_of).
BRIDGE_CHIPS = {"ESP32C3": chips.ESP32C3, "ESP32D0WD": chips.ESP32}
READ_TRIES = 3
# Where the bridge was read through: on the home network, or its own.
BRIDGE_HOST = "readout_bridge"
BACK_ON_SONOFF_WAIT = 300.0


def read_and_anonymise(ctx: Context, manifest: dict[str, Any], chip: chips.Chip, name: str, version: str,
                       device: dict[str, Any], *, known: bool = False,
                       skippable: bool = False) -> tuple[dict[str, bytes], bool]:
    """Read the device's whole flash through the bridge (read_out), keep it as
    its Sonoff backup, and anonymise it. The files to offer, and whether the
    device answered as Sonoff's firmware again. `skippable`: one device of
    several, which its question may pass by."""
    flash, efuse, info, report = read_out(ctx, chip, name, version, str(device.get("deviceid") or ""),
                                          known=known, skippable=skippable)
    manifest["read"] = True
    manifest["firmware"] = firmware_of(flash)
    manifest["facts"] = device_facts.lines(flash, efuse, device_facts.parse_info(info) if info else None)
    manifest["flash"] = {"size": report["size"], "sha256": report["sha256"],
                         "matches_device": report.get("matches_device")}
    saved, fresh = records.save_backup(flash, efuse, report, version, kind=readout.describe(device))
    if fresh:
        output.say(f"    Kept on this computer too, in {runs.display_path(saved)}: private, as it holds")
        output.say("    your Wi-Fi password. Going back to Sonoff's firmware later can use it.")
    scrubbed, blanked = scrub.wifi(flash)
    scrubbed, replaced = scrub.identity(scrubbed)
    manifest["wifi_blanked"], manifest["identity_replaced"] = blanked, replaced
    files = {"flash.bin": scrubbed, "efuse.bin": efuse, "info.txt": info or b"",
             "report.json": json.dumps(report, indent=2).encode()}
    readout.restoring()
    return files, back_on_sonoff(ctx)


# The bridge's own project name: in the spare slot, where a readout put it.
BRIDGE_PROJECT = "autoflash_bridge"


def firmware_of(flash: bytes) -> dict[str, Any]:
    """What every genuine readout of this model and firmware version shares,
    read before anything is taken out, for the website to tell readouts that
    agree from one that does not: its partition table and each Sonoff app's
    build. Not the owner's settings, nor what lies past an app in its slot,
    which differ from device to device."""
    try:
        table = [[p.label, p.type, p.subtype, p.offset, p.size] for p in flashmap.partitions(flash)]
        slots = flashmap.app_slots(flash)
    except ValueError:
        return {}
    apps = []
    for slot in slots:
        image = flash[slot.offset:slot.offset + slot.size]
        described, built = dump_stock_flash.app_description(image), dump_stock_flash.build_sha256(image)
        if described and built and described["project"] != BRIDGE_PROJECT:
            apps.append({"project": described["project"], "version": described["version"], "elf_sha256": built})
    return {"table": hashlib.sha256(json.dumps(table).encode()).hexdigest(),
            "apps": sorted(apps, key=lambda app: app["elf_sha256"])}


def read_out(ctx: Context, chip: chips.Chip, name: str, version: str, device_id: str, *,
             known: bool = False, skippable: bool = False) -> tuple[bytes, bytes, bytes | None, dict]:
    """Send the bridge to the account's device `device_id`, wait for it, and
    read the whole flash through it. `known`: a kind the tool converts, which the bridge has run on before."""
    app = chip.bridge_app()
    wrapped = ctx.dir / "autoflash-bridge-readout.ota"
    wrapped.write_bytes(build_vendor_ota.build_wrapper(app, version=build_vendor_ota.app_version(app), model=name))
    output.transient_begin()
    # No word against a power cut here, as the Beken readout has: the reader
    # goes in the spare firmware slot, and the device starts Sonoff's again
    # whenever it is switched off and on, part way or not.
    prompts.warning_box("Before going on", [
        "Only reads the device; its Sonoff firmware, settings and pairing stay.",
        "A small reader goes in its spare firmware slot, replacing what is there.",
        "At the end you switch it off and on, and it starts its Sonoff firmware.",
        *([] if known else [
            "New to this tool: if the reader cannot start, it may need a serial cable.",
            "Only go ahead with a device you can afford to lose."]),
    ])
    if not prompts.require_word(prompts.CONSENT_WORD, skip=skippable):
        raise NothingWritten("skipped as asked, nothing was sent to it" if skippable else
                             "not confirmed - nothing has been sent to the device")
    output.transient_end()
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    # Before anything else: a rule a run stopped part way left is taken over
    # here, and removed as this run ends.
    firewall.ensure_firewall(ctx)
    device_ip = ctx.state.input("device_ip")
    if device_ip and installer.bridge_answers(device_ip):
        # A readout stopped part way left it there: read through it.
        output.say("The reader is on the device already, from a readout stopped part way.")
        ctx.state.set_input(readout.STATE_KEY, "sent")
        return read_through(ctx, device_ip)
    with safety_locks.lifted(ROOT, "RECOVERY_LOCK"):
        code = hostsys.run_tool(hostsys.python_script(
            "tools/espressif/serve_tasmota_ota.py",
            "--listen-ip", ctx.listen_ip(), "--listen-port", str(port),
            "--device-ip", ctx.state.input("device_ip"), "--firmware", str(wrapped),
            "--email", ctx.email(), "--region", ctx.ewelink_region(), "--expected-current-version", version,
            "--device-id", device_id,
            "--i-understand-stock-has-no-automatic-rollback", "--readout-model", name, unbuffered=True),
            interactive=True, env=ctx.helper_env())
    safety_locks.say_lock_back(ROOT, "RECOVERY_LOCK")
    ctx.state.set_input(readout.STATE_KEY, "sent")
    return read_through(ctx, find_bridge(ctx, sent=code == 0))


def read_through(ctx: Context, host: str) -> tuple[bytes, bytes, bytes | None, dict]:
    """The whole flash, efuses and what the bridge at `host` says of the chip."""
    ctx.state.set_input(BRIDGE_HOST, host)   # where it is restarted from, once read
    problem = ""
    # Its start an event, which the fault suites time a fault in the read
    # from, and in the run's log, which a wait on screen is not.
    output.event("phase", phase="read")
    output.log_only("Reading the device's memory...")
    with output.working("Reading the device's memory - about a minute..."):
        for _ in range(READ_TRIES):
            try:
                flash, efuse, report = dump_stock_flash.read_dump(host)
                break
            except OSError as exc:
                problem = str(exc)
                output.say(f"    The read did not come through: {problem}")
        else:
            flash = None
    if flash is None:
        raise Abort(f"the device's memory could not be read ({problem}). Switch it off and on: it starts "
                    "its Sonoff firmware again")
    for line in dump_stock_flash.summary(report):
        output.log_only(f"    {line}")
    return flash, efuse, dump_stock_flash.read_info(host), report


def find_bridge(ctx: Context, *, sent: bool) -> str:
    """Where the bridge answers: on the home network, or on its own once this
    computer has joined it. Abort if the device went back to Sonoff instead."""
    output.say("Waiting for it to start...")
    address = installer.bridge_on_home_network(ctx, timeout=120.0)
    if address:
        return address
    if installer.bridge_answers(setup_network.SETUP_HOST):
        return setup_network.SETUP_HOST
    if installer.back_on_sonoff(ctx):
        raise Abort("the device is still on its Sonoff firmware, so nothing was read: "
                    + ("it took the reader but did not start it. " if sent else
                       "the reader was not delivered. ")
                    + "It works as before")
    network = prompts.find_ssid(names.bridge_networks(ctx)) or names.our_setup_network(ctx)
    installer.ask_to_join_bridge(network, what="reader")
    found = installer.wait_for_bridge(ctx, network.split()[0], gone_back=lambda: installer.back_on_sonoff(ctx))
    if found == installer.BACK_ON_SONOFF:
        raise Abort("the device went back to its Sonoff firmware before it could be read. It works as before")
    return found or setup_network.SETUP_HOST


def back_on_sonoff(ctx: Context) -> bool:
    """The device back on its Sonoff firmware: restarted by the bridge (4.4.6),
    else switched off and on by the person, and waited for. Whether Sonoff's
    firmware answered."""
    host = ctx.state.input(BRIDGE_HOST)
    # On its own network, the person has their usual Wi-Fi to join again anyway.
    if host and host != setup_network.SETUP_HOST and installer.restart_into_sonoff(ctx, host):
        ctx.state.set_input(readout.STATE_KEY, "read")
        output.event("phase", phase="back-done")
        output.say("    It is back on its Sonoff firmware.")
        return True
    output.transient_begin()
    output.event("phase", phase="back")
    with output.asking_to_switch_now(key=True):
        prompts.section("Read. Now put it back on its Sonoff firmware", [
            "Switch it off for five seconds, then on (a wall switch at its breaker).",
            "If this computer joined the device's network, rejoin your usual Wi-Fi.",
        ])
    prompts.wait_for_key("Press any key once it is switched back on")
    output.transient_end()
    started = time.monotonic()
    answered = False
    with output.working("Waiting for its Sonoff firmware to answer..."):
        while time.monotonic() - started <= BACK_ON_SONOFF_WAIT:
            if installer.back_on_sonoff(ctx):
                answered = True
                break
            output.progress(f"waiting for its Sonoff firmware... {time.monotonic() - started:.0f}s")
            time.sleep(3.0)
    if not answered:
        output.progress_clear()
        output.tell("    If the eWeLink app doesn't show it yet, switch it off and on once more.")
        return False
    output.progress_clear()
    ctx.state.set_input(readout.STATE_KEY, "read")
    output.event("phase", phase="back-done")
    output.say("    It is back on its Sonoff firmware.")
    return True
