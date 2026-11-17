"""From Sonoff's firmware to the interim Tasmota: the installer (the
bridge) sent through eWeLink, the backup read through it, and Tasmota
uploaded through it - steps C2, C3 and D1.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, TYPE_CHECKING

from tools.espressif import build_vendor_ota, chips, device_facts, dump_stock_flash, layouts
from tools.autoflash import (
    account,
    device,
    firewall,
    hostsys,
    interrupt,
    join,
    kinds,
    names,
    output,
    plans,
    records,
    prompts,
    runs,
    safety_locks,
    setup_network,
    tasmota_settings,
    testers,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.runs import ROOT
from tools.autoflash.state import ATTEMPTED, DONE, FAILED
from tools.espressif.safeboot.safeboot_migration import pinned_artifacts
from tools.autoflash.routes import arrival
from tools.autoflash.routes.espressif import images
from tools.ewelink import ota_push

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The bridge: joins the home network the stock firmware was on, raising its own
# only if that fails, gives Tasmota its Wi-Fi at D1, and lets the device's whole
# flash be read first, for the backup - and starts even once an upload into
# Sonoff's slot was cut off (from 4.4.3), where 4.4.2 would not, leaving a device
# only a serial cable could recover; named, from 4.4.4, as the tool's.  Named,
# like every bridge from 4.4.1 on, by the version its file is wrapped with
# (the updater is told build_vendor_ota.ANNOUNCED_VERSION); the file by the
# kind of device its wrapper is for (kinds.SONOFF_S60's).
BRIDGE_OTA = Path(chips.ESP32C3.pin("bridge")["file"])


# What the sender says when the upgrade command never went out
# (serve_tasmota_ota.NOT_SENT).
BRIDGE_NOT_SENT = "The upgrade command was not sent"
# The run's input holding the address the device has downloaded the installer
# from, so a later send it never asks for is not put down to the firewall there.
DOWNLOADED_FROM = "bridge_downloaded_from"


def bridge_file(ctx: Context) -> Path:
    """The installer as the device's stock updater takes it: the S60's as it is
    published (or another given by hand); any other kind's made from the app
    for its chip, wrapped for its model. The wrapper holds nothing of the device."""
    kind = kinds.of_run(ctx.state)
    if kind is kinds.SONOFF_S60 or ctx.research_bridge:
        return ctx.bridge
    try:
        app = chips.of(kind.chip).bridge_app()
    except (OSError, ValueError) as exc:
        raise Abort(f"{exc}: refusing to send it") from exc
    wrapped = build_vendor_ota.build_wrapper(app, version=build_vendor_ota.app_version(app), model=kind.wrapper_model)
    path = ctx.dir / f"autoflash-bridge-{kind.key}.ota"
    path.write_bytes(wrapped)
    return path


def bridge_failure_advice(ctx: Context, reported: list[str], started: bool) -> list[str]:
    """What to do about a send of the installer that failed, by how far it got."""
    if started:
        return [
            "The device started downloading it from this computer, but stopped part",
            "way - most likely its Wi-Fi dropped: a router restarting, or a weak",
            "signal. It keeps its Sonoff firmware, so nothing is lost; run this",
            "again once its Wi-Fi is steady.",
        ]
    if output.tool_said(f"error {ota_push.BUSY}"):
        return [
            "The device turned the installer down: it is still busy with an update",
            "that was cut off part way. It keeps its Sonoff firmware, so nothing is",
            "lost: switch the device off and on, wait for it to show online in the",
            "eWeLink app, and run this again.",
        ]
    if output.tool_said("offline in eWeLink"):
        return [
            "eWeLink does not see the device, so the installer cannot be sent. It",
            "keeps its Sonoff firmware, so nothing is lost: switch the device off",
            "and on, wait for it to show online in the eWeLink app, and run this",
            "again.",
        ]
    if output.tool_said(BRIDGE_NOT_SENT):
        # Seen against the real cloud: eWeLink's servers dropping connections,
        # and the run blamed this computer's firewall for a device never asked.
        if any("refusing" in line.lower() for line in reported):
            return ["Nothing was sent to the device, so it keeps its Sonoff firmware."]
        return [
            "This computer's connection to eWeLink failed before the device was told",
            "to fetch the installer - eWeLink's servers, or this computer's",
            "internet, having trouble. Nothing was sent, so the device keeps its",
            "Sonoff firmware: run this again in a few minutes.",
        ]
    if ctx.state.input(DOWNLOADED_FROM) == ctx.listen_ip():
        # Seen in the fault suite: told it was the firewall, three times over,
        # with half the file already served to the same device from here.
        return [
            "The device did not ask for the installer this time, though it downloaded",
            "part of it from this computer earlier, so a firewall is not the cause.",
            "eWeLink's message most likely did not reach it. It keeps its Sonoff",
            "firmware, so nothing is lost: switch the device off and on, wait for it",
            "to show online in the eWeLink app, and run this again.",
        ]
    return firewall.firewall_hint(ctx)


def step_bridge(ctx: Context) -> dict[str, Any]:
    """C - stock to recovery bridge.  First write; no rollback below this line."""
    device_ip, version = ctx.state.require("device_ip", "stock_version")
    listen_ip = ctx.listen_ip()
    email = ctx.email()
    device_id = account.known_device_id(ctx)
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    firewall.ensure_firewall(ctx)
    env = ctx.helper_env()

    mac = ctx.state.input("device_mac")
    backup = records.find_backup(mac) if mac else None
    if backup:
        kept = f"Its Sonoff firmware is backed up already ({backup.taken or 'earlier'})."
    elif getattr(getattr(ctx, "args", None), "no_backup", False):
        kept = "No backup of its Sonoff firmware is kept (--no-backup)."
    else:
        kept = "Its Sonoff firmware is backed up to this computer first."
    if not getattr(ctx, "consented", False):  # once a run: not again when sent again (resend_if_back_on_sonoff)
        output.transient_begin()
        prompts.warning_box("Before going on", [
            "Its Sonoff firmware is replaced: the eWeLink app no longer controls it.",
            prompts.DO_NOT_CUT_POWER,
            kept,
            "Only go ahead with a device you can afford to lose.",
            *testers.notice(kinds.of_run(ctx.state)),
        ])
        if not prompts.require_word(prompts.CONSENT_WORD):
            raise NothingWritten("not confirmed - nothing has been written to the device")
        output.transient_end()  # agreed: the note and the question have done their job
        ctx.consented = True

    while True:
        started = time.monotonic()
        with safety_locks.lifted(ROOT, "RECOVERY_LOCK"):
            code = hostsys.run_tool(
                hostsys.python_script(
                    "tools/espressif/serve_tasmota_ota.py",
                    "--listen-ip", listen_ip, "--listen-port", str(port),
                    "--device-ip", device_ip,
                    "--firmware", str(bridge_file(ctx)),
                    "--email", email, "--region", ctx.ewelink_region(), "--expected-current-version", version,
                    "--device-id", device_id,
                    "--i-understand-stock-has-no-automatic-rollback",
                    *(["--events", str(output.events_path())] if output.events_path() else []),
                    # Each line as it comes, so the run's log has when eWeLink said what.
                    unbuffered=True),
                interactive=True,
                env=env,
            )
        safety_locks.say_lock_back(ROOT, "RECOVERY_LOCK")
        if code == 0:
            return {"seconds": round(time.monotonic() - started, 1)}
        # The sender can fail after the device already has everything - the cloud
        # reporting it offline is exactly what a successful install looks like.
        installed, reason = evidence_bridge_installed(ctx)
        if installed:
            output.say(f"  {reason}")
            return {"seconds": round(time.monotonic() - started, 1), "confirmed_by": "bridge network"}
        reported = output.tool_problems()
        # Some of it served means the device reached this computer: not the
        # firewall, but its Wi-Fi going part way (a router restarting, say).
        begun = output.tool_said("Served /user") or output.tool_said("did not request every")
        if begun:
            ctx.state.set_input(DOWNLOADED_FROM, listen_ip)
        advice = bridge_failure_advice(ctx, reported, begun)
        if not prompts.UNATTENDED and not begun and needs_power_cycle(ctx):
            # Switched off and on, then sent again, as the Beken route's B2 does.
            arrival.power_cycle(ctx, "The installer was not delivered", (
                *(["The sender reported:", *(f"  {line}" for line in reported), ""] if reported else []),
                *advice), "send it again")
            device_id = str(records.read_device(ctx.state.input("device_mac") or "").get("ewelink_id") or "")
            if not device_id or arrival.online_in_ewelink(ctx, device_id):
                continue
        prompts.section("The installer was not delivered", [
            *(["The sender reported:", *(f"  {line}" for line in reported), ""] if reported else []),
            *advice,
        ])
        raise Abort("the installer was not fully delivered" if begun else "the installer was not delivered")


def needs_power_cycle(ctx: Context) -> bool:
    """Whether the installer was not delivered for want of the device being
    switched off and on: busy with an update cut off part way
    (ota_push.busy), away from eWeLink since, or not asking for it though it
    fetched part of it from here before."""
    return (output.tool_said(f"error {ota_push.BUSY}") or output.tool_said("offline in eWeLink")
            or ctx.state.input(DOWNLOADED_FROM) == ctx.listen_ip())


def bridge_on_home_network(ctx: Context, *, timeout: float = 60.0, tasmota: bool = False) -> str | None:
    """The bridge's address on the home network, if it joined it - or, with
    `tasmota`, TASMOTA_HOME if Tasmota is there instead (reach_bridge).

    Bridge v4 and later join the network the stock firmware was using.  It has
    the device's own hardware address there, so it is found where the device was.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        address = ctx.finder.poll()
        if address:
            body = setup_network.page(address, timeout=3.0)
            if setup_network.is_bridge_page(body):
                output.progress_clear()
                return address
            if tasmota and setup_network.is_tasmota_page(body) and tasmota_at_home(ctx):
                return TASMOTA_HOME
        output.progress(f"looking for the installer on your network... {time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()
    return None


# Sent again at most this many times in a run, as the commit is tried: a device
# whose power keeps going is told about, not sent the installer for ever.
BRIDGE_SENDS = 3


def back_on_sonoff(ctx: Context) -> bool:
    """Whether the device answers as Sonoff's own firmware (arrival.sonoff_answers)."""
    host = ctx.state.input("device_ip") or ctx.finder.poll()
    return bool(host) and arrival.sonoff_answers(ctx, host)


def bridge_answers(host: str) -> bool:
    body = setup_network.page(host, timeout=2.0)
    return setup_network.is_bridge_page(body)


def has_fallback(host: str) -> bool:
    """Whether the bridge at `host` has Sonoff's firmware to start next: not
    where its status says it has none (an upload into Sonoff's slot cut off part
    way). Taken to have it where the status cannot be read: a restart asked of
    it then is refused or fails, and the person is asked instead."""
    status = setup_network.page(host, "/status", timeout=3.0)
    return status is None or "fallback: none" not in status


def restart_bridge(host: str) -> bool:
    """Have the bridge at `host` start the device again into the firmware chosen
    for its next start - Sonoff's, which it chose as it started - as switching
    it off and on would (bridge 4.4.6). Whether it said it would: not one
    before 4.4.6, nor one refusing (an upload under way, or no Sonoff firmware
    to start)."""
    request = urllib.request.Request(f"http://{host}/restart", data=b"", method="POST",
                                     headers={"User-Agent": "Autoflash/1"})
    try:
        with urllib.request.urlopen(request, timeout=10.0) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def restart_into_sonoff(ctx: Context, host: str, wait: float = 120.0) -> bool:
    """The bridge at `host` restarted into Sonoff's firmware, waited for: whether
    Sonoff's firmware answered there in `wait` seconds. False at once where the
    bridge would not restart."""
    if not restart_bridge(host):
        return False
    output.event("phase", phase="back")
    output.say("    Restarting it into its Sonoff firmware...")
    started = time.monotonic()
    while time.monotonic() - started < wait:
        time.sleep(3.0)
        if arrival.sonoff_answers(ctx, host):
            return True
    return False


def reach_bridge(ctx: Context, *, tasmota: bool = False) -> str:
    """The bridge's address: setup_network.SETUP_HOST when this computer is on
    the bridge's own network, else where it joined the home network.  Asks the
    person to join the bridge's network only when it raised one.

    With `tasmota` (D1), Tasmota found instead, installed already - by a run
    cut off after the upload, or an upload that took though its answer was
    lost (a weak signal, seen in the fault suite): TASMOTA_HOME once it
    answers on the home network, its address noted (it joins by itself with
    the Wi-Fi the installer gave it), or TASMOTA_THERE on its own network -
    named as the installer was (tasmota_named_alone), by the same name."""
    if bridge_answers(setup_network.SETUP_HOST):
        return setup_network.SETUP_HOST  # joined at C3, or by hand
    if tasmota and setup_network.tasmota_answers():
        return TASMOTA_THERE
    address = bridge_on_home_network(ctx, tasmota=tasmota)
    if address == TASMOTA_HOME:
        return address
    if address:
        output.say(f"    The installer joined your network at {address}.")
        return address
    # What it asks, and what is said while it is done, goes once the bridge is
    # reached: the stage's other notes are no taller for it.
    with output.until_done():
        return join_bridge(ctx, tasmota=tasmota)


def join_bridge(ctx: Context, *, tasmota: bool = False) -> str:
    """reach_bridge's wait for the bridge's own network, asked to be joined.

    The installer hands the device back to Sonoff's firmware when it is switched
    off and on before its work is done - by design, so a power cut never
    strands it.  Waiting for the installer then waits for ever: looked for
    before the wait, and during it."""
    asked = said_back = False
    asked_at = None   # where the screen was as the person was asked to join it
    while True:
        address = resend_if_back_on_sonoff(ctx)
        if address:
            return address
        bridge = prompts.find_ssid(names.bridge_networks(ctx)) or names.our_setup_network(ctx)
        if not asked:
            join.note_own_network(ctx, join.NOT_JOINED)   # it could not join the home network
            asked_at = output.mark()
            ask_to_join_bridge(bridge, tasmota=tasmota and bool(ctx.state.input("tasmota_named_alone")))
            asked = True

        def gone_back() -> bool:
            return getattr(ctx, "bridge_sends", 0) < BRIDGE_SENDS and back_on_sonoff(ctx)

        found = wait_for_bridge(ctx, bridge.split()[0], gone_back=gone_back, tasmota=tasmota)
        if found != BACK_ON_SONOFF:
            return found or setup_network.SETUP_HOST
        # Back on Sonoff's firmware, the bridge's network has gone with it:
        # the instructions to join it are taken off, and given again once
        # the installer, sent again, has made it again.  Seen in the fault
        # suite: kept, they stood above the installer sent again, 27 rows.
        output.rewind(asked_at)
        asked = False
        if not said_back:   # once: each time after says nothing new
            output.tell("    While waiting, the device went back to its Sonoff firmware.")
            said_back = True
        else:
            output.say("    While waiting, the device went back to Sonoff's own firmware again.")


def resend_if_back_on_sonoff(ctx: Context) -> str | None:
    """The installer sent again while the device is back on Sonoff's firmware, up
    to BRIDGE_SENDS in a run; its address once it joined the home network."""
    sends = getattr(ctx, "bridge_sends", 0)
    # A send that failed left the device on Sonoff's firmware, whether or not it
    # answers here this moment (offline in eWeLink, it can be slow to): the
    # next goes ahead - never on to asking for the installer's network.
    failed = False
    while sends < BRIDGE_SENDS and (failed or back_on_sonoff(ctx)):
        failed = False
        if not ctx.state.input("stock_version"):
            # A run started on the installer has none of eWeLink's details.
            raise Abort("the device is back on Sonoff's own firmware, so the installer has to "
                        "be sent again through eWeLink: run this again with --new")
        sends += 1
        ctx.bridge_sends = sends
        output.tell(f"    Back on its Sonoff firmware: sending the installer again (attempt {sends} of {BRIDGE_SENDS}).")
        output.log_only("    It goes back to it when switched off and on before the installer has done its "
                        "work, so nothing is lost.")
        ctx.state.mark("C2", FAILED, error="redo: the device went back to Sonoff's firmware")
        try:
            ctx.state.mark("C2", DONE, result=step_bridge(ctx))
        except Abort as exc:
            # Cut off part way, the device keeps Sonoff's firmware: another go,
            # while there are goes left.
            if sends == BRIDGE_SENDS:
                raise
            output.tell(f"    {exc}.")
            failed = True
            continue
        address = bridge_on_home_network(ctx, timeout=120.0)
        if address:
            output.say(f"    The installer joined your network at {address}.")
            return address
    return None


def ask_to_join_bridge(bridge: str, *, tasmota: bool = False, what: str = "installer") -> None:
    """Have the person join the network the bridge has made: `what` the bridge
    is to them - the installer, or a readout's reader."""
    if bridge.startswith(names.LEGACY_BRIDGE_PREFIX):
        # An earlier release's installer: its network has a password.
        copied = hostsys.copy_to_clipboard(names.LEGACY_BRIDGE_PASSWORD)
        joining = [f"    network   {bridge}",
                   f"    password  {names.LEGACY_BRIDGE_PASSWORD}"
                   + ("      (on your clipboard)" if copied else "")]
    else:
        joining = [f"    {bridge}      (open - no password)"]
    output.event("join", network=bridge)
    prompts.section(f"Join the {what}'s Wi-Fi network", [
        f"In {hostsys.wifi_menu_hint()}, join:",
        *joining,
        *(["Tasmota, if it was sent, shows the same network name."] if tasmota else []),
        "Your internet drops while joined; this carries on by itself.",
        f"Not listed yet? Wait: the {what} may join your Wi-Fi instead.",
        "Until joined, the network goes for up to 20 seconds every three minutes.",
    ])
    # A wired connection, if this computer has one, stays connected; the
    # network goes as the bridge tries the home Wi-Fi again.
    output.log_only("    A wired connection can stay connected meanwhile.")


# The steps after the backup: once any has started, the installer it reads
# through may be gone.
AFTER_BACKUP = ("D1", "D4", "E1", "E2", "E3", "F2", "F4", "G1", "H1")


def bridge_info(ctx: Context) -> dict[str, str]:
    """What the installer's GET /info says, for the log, looked for without
    asking anyone: nothing from one before 4.4.5, or one not answering, and the
    run carries on just the same."""
    try:
        host = (setup_network.SETUP_HOST if bridge_answers(setup_network.SETUP_HOST)
                else bridge_on_home_network(ctx, timeout=10.0))
        body = dump_stock_flash.read_info(host) if host else None
    except OSError:
        return {}
    return device_facts.parse_info(body) if body else {}


def note_hardware(ctx: Context, info: dict[str, str], backup: records.Backup | None) -> None:
    """The chip's revision, the flash chip's ID and its size, as the installer
    says them (or the size from the backup), kept for the run's report: a
    batch with another flash chip, or a new revision, shows in the figures."""
    revision, flash_id, size = info.get("chip revision", ""), info.get("flash id", "").lower(), info.get("flash chip size", "")
    if not size.isdigit() and backup is not None:
        size = str((backup.directory / "flash.bin").stat().st_size)
    ctx.state.set_input("hardware", {
        "revision": revision if re.fullmatch(r"\d{1,3}\.\d{1,3}", revision) else None,
        "flash_id": flash_id if re.fullmatch(r"[0-9a-f]{6}", flash_id) else None,
        "flash_mb": int(size) // 0x100000 if size.isdigit() and 0 < int(size) // 0x100000 <= 128 else None,
    })


def log_device_facts(ctx: Context, directory: Path, shown: bool, info: dict[str, str] | None = None) -> None:
    """What the backup and the installer say about the device, in the log, to add
    a device from a report of one: on the screen too when the device is one to report."""
    efuse = directory / "efuse.bin"
    facts = device_facts.lines((directory / "flash.bin").read_bytes(), efuse.read_bytes() if efuse.exists() else None,
                               info)
    product = ctx.state.input("product_model")
    for line in ([f"eWeLink product: {product}"] if product else []) + facts:
        (output.tell if shown else output.log_only)(f"    {line}")


def require_known_layout(ctx: Context) -> None:
    """Stop before anything is written over Sonoff's firmware when the device's
    partition table is not one this tool has a plan for. A device of a kind known
    only from its firmware may be laid out differently from the one it was
    tried on; until D1 the installer falls back to Sonoff's firmware on a power
    cycle, so here that costs nothing."""
    mac = ctx.state.input("device_mac")
    backup = records.find_backup(mac) if mac else None
    try:
        if backup:
            table = (backup.directory / "flash.bin").read_bytes()[plans.TABLE_ADDRESS:plans.TABLE_ADDRESS + plans.SECTOR]
        else:
            table = dump_stock_flash.read_table(reach_bridge(ctx))
    except OSError as exc:
        output.log_only(f"    The device's memory layout could not be read here ({exc}); its checks later still stand.")
        return
    known = hashlib.sha256(table).hexdigest() == layouts.of(kinds.of_run(ctx.state).layout).table_sha256
    info = bridge_info(ctx)
    note_hardware(ctx, info, backup)
    if backup:
        log_device_facts(ctx, backup.directory, shown=not known, info=info)
    else:
        for line in device_facts.info_lines(info):
            output.log_only(f"    {line}")
    if not known:
        raise Abort("this device's memory is not laid out as this tool expects, so nothing has been written "
                    "over its Sonoff firmware: switch it off and on to go back to it")


def step_backup(ctx: Context) -> dict[str, Any]:
    """C3 - the device's Sonoff firmware, kept whole, before D1 writes over it.
    Its stage is "skipped" when no backup could be taken, as the conversion
    does not need one."""
    if any(ctx.state.status(step) in (DONE, ATTEMPTED) for step in AFTER_BACKUP):
        output.say("    The device is past the installer already, so there is nothing to back up from.")
        outcome = "not taken"
    else:
        skip = backup_not_needed(ctx)
        if skip:
            outcome, message = skip
            output.say(f"    {message}")
        else:
            outcome = back_up_stock(ctx, reach_bridge(ctx))
        require_known_layout(ctx)
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": records.BACKUP_OUTCOMES[outcome][0]}


def step_berry_tasmota(ctx: Context) -> dict[str, Any]:
    """D1/D2 - Berry-capable Tasmota through the bridge."""
    image = images.interim_of(kinds.of_run(ctx.state))
    output.say("Looking for the device's Wi-Fi network...")
    bridge = prompts.find_ssid(names.bridge_networks(ctx))
    setup = None if bridge else prompts.find_ssid(names.setup_networks(ctx))
    if setup:
        # Resuming after the upload: Tasmota is already installed and waiting.
        output.say(f"    Tasmota is already installed and waiting on {setup}.")
        names.note_setup_network(ctx, setup)
        ip = join.configure_device_wifi(ctx, setup, tasmota_why(ctx))
        return {"device_ip": ip, "device_name": setup, "resumed": True}

    address = reach_bridge(ctx, tasmota=True)
    if address == TASMOTA_HOME:
        output.say("    Tasmota is already installed, and joined your network by itself.")
        return {"device_ip": ctx.state.input("device_ip"), "upload_route": "home network", "wifi": "preset",
                "device_name": ctx.state.input("device_name"), "resumed": True}
    if address == TASMOTA_THERE:
        # Run again after the upload: Tasmota is waiting on its own network -
        # the one the installer had, by the same name, when it was named so.
        network = names.our_setup_network(ctx) if ctx.state.input("tasmota_named_alone") else None
        output.say(f"    Tasmota is already installed and waiting on {network or 'its own network'}.")
        ip = join.configure_device_wifi(ctx, network, tasmota_why(ctx))
        return {"device_ip": ip, "device_name": ctx.state.input("device_name"), "resumed": True}
    if address == setup_network.SETUP_HOST:
        if not ctx.state.input("tasmota_wifi_preset"):
            give_tasmota_the_wifi(ctx, address)  # its name, and the Wi-Fi if proven
        upload_tasmota(ctx, image)
        if ctx.state.input("tasmota_wifi_preset"):
            # An earlier try, cut off, had the bridge save Tasmota's settings
            # with your Wi-Fi: Tasmota joins it by itself, with no setup network
            # of its own, even sent through the bridge's.
            output.say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
            output.event("home")
            prompts.section("Back to your usual Wi-Fi", [
                "Rejoin it now if this computer hasn't by itself.",
            ])
            return finish_tasmota_setup(ctx, route="bridge network", wait=240.0)
        return finish_tasmota_setup(ctx, route="bridge network")
    preset = give_tasmota_the_wifi(ctx, address)
    upload_tasmota(ctx, image, host=address)
    if preset:
        found = tasmota_on_home_network(ctx)
        if found:
            return {"device_ip": found, "upload_route": "home network", "wifi": "preset",
                    "device_name": ctx.state.input("device_name")}
        return finish_tasmota_setup(ctx, route="home network", wait=900.0,
                                    advice=("Tasmota has not appeared on your network", quick_power_cycle_hint(ctx)))
    return finish_tasmota_setup(ctx, route="home network")


def backup_not_needed(ctx: Context) -> tuple[str, str] | None:
    """Why no backup is to be read, if none is, as (outcome, message): one
    taken already, or none wanted."""
    if ctx.state.input("stock_backup"):
        return ctx.state.input("backup_outcome") or "existing", "Backed up before this run was resumed."
    if getattr(getattr(ctx, "args", None), "no_backup", False):
        return "not wanted", "No backup of the Sonoff firmware (--no-backup)."
    known = ctx.state.input("device_mac")
    existing = records.find_backup(known) if known else None
    if existing:
        # The first backup is the original; a device converted again keeps it.
        ctx.state.set_input("stock_backup", runs.display_path(existing.directory))
        return "existing", f"Its Sonoff firmware is backed up already, in {runs.display_path(existing.directory)}."
    return None


# Rounds of two reads at the backup before the person is asked what now: the
# reads only read, so another round is safe, and is what they would choose.
BACKUP_ROUNDS = 2
# Testing only (run --simulate-backup-failure): how many backup reads still
# to fail.  Every round's (2 * BACKUP_ROUNDS), so the menu that follows can be tried.
SIMULATED_BACKUP_FAILURES = 0
BACKUP_RETRY, BACKUP_WITHOUT = 0, 1


def read_for_backup(host: str) -> tuple[bytes, bytes, dict[str, Any]]:
    global SIMULATED_BACKUP_FAILURES
    if SIMULATED_BACKUP_FAILURES > 0:
        SIMULATED_BACKUP_FAILURES -= 1
        raise OSError("simulated failure (--simulate-backup-failure)")
    return dump_stock_flash.read_dump(host)


def backup_failed(problem: str) -> int:
    """Ask what to do about a backup that failed twice: BACKUP_RETRY or
    BACKUP_WITHOUT; Quit stops the run, which carries on from here next time."""
    options = ["Try again", "Carry on without a backup"]
    output.transient_begin()
    prompts.section("The Sonoff firmware could not be backed up", [
        f"Reading the device's memory failed {2 * BACKUP_ROUNDS} times: {problem}.",
        "Carrying on without one is possible, but not recommended.",
    ])
    if prompts.UNATTENDED:
        prompts.answered_unattended("What now", options[BACKUP_WITHOUT])
        choice = BACKUP_WITHOUT
    else:
        choice = prompts.ask_choice("What now", options, help_lines=[
            "q leaves the device on the installer: run this again to carry on from",
            "here, or switch the device off and on to go back to Sonoff.",
        ])
    output.transient_end()
    return choice


def back_up_stock(ctx: Context, host: str) -> str:
    """Keep the device's whole flash, Sonoff firmware and all, before D1 writes over it.

    The bridge only reads it.  Taken automatically, under the consent typed at
    C2; if it fails the person decides whether to carry on without one, and an
    unattended run carries on.  Returns how it went: a devices.BACKUP_OUTCOMES key.
    The caller, step_backup, has already asked backup_not_needed.
    """
    known = ctx.state.input("device_mac")
    output.event("phase", phase="backup")
    output.say("Backing up the device's Sonoff firmware - about a minute...")
    first = True
    rounds = reads = 0
    while True:
        problem = ""
        for _ in range(2):
            reads += 1
            ctx.state.capture("C3.attempts", reads)   # its retries, for the report, as B4's are
            if not first:
                # Read where the installer is now: when the home network goes
                # it raises its own, where it is read once this computer has
                # joined that - not again at the address it had (seen in the
                # fault suite: both reads timed out there, and an unattended
                # run went on without a backup).
                host = reach_bridge(ctx)
            first = False
            try:
                flash, efuse, report = read_for_backup(host)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 404:
                    # A bridge from before 4.4.2, given with --bridge.
                    output.say("    This installer cannot read the device's memory, so no backup is kept.")
                    return "unreadable"
                problem = str(exc)
            except OSError as exc:  # a short transfer included
                problem = str(exc)
            output.say(f"    The backup did not come through: {problem}")
        else:
            rounds += 1
            if rounds < BACKUP_ROUNDS:
                output.say("    Trying the backup again...")
                continue
            rounds = 0
            if backup_failed(problem) == BACKUP_RETRY:
                output.say("    Trying the backup again...")
                continue
            output.say("    Carrying on without a backup.")
            return "failed"
        break

    mac = records.mac_from_efuse(efuse)
    if known and records.folder_name(known) != records.folder_name(mac):
        output.say(f"    The network reported {known} for the device; its chip says {mac}, which is used.")
    if hasattr(ctx, "file_under_device"):
        ctx.file_under_device(mac)  # if A1 could not tell its MAC
    out, _ = records.save_backup(flash, efuse, report, ctx.state.input("stock_version"),
                               kind=kinds.of_run(ctx.state).name)
    ctx.state.set_input("stock_backup", runs.display_path(out))
    for line in dump_stock_flash.summary(report):
        output.say(f"    {line}")
    output.say(f"    Saved in {runs.display_path(out)}, under the device's MAC (private: it holds the")
    output.say("    Wi-Fi password).")
    if not dump_stock_flash.is_good(report):
        output.tell("    The backup may not restore (not a whole, bootable flash): see report.json.")
    return "saved"


# What wait_for_bridge says when the device went back to Sonoff's firmware meanwhile.
BACK_ON_SONOFF = "back on Sonoff's firmware"
# What reach_bridge says when Tasmota answers instead, installed already: on
# the installer's network (by the same name), or on the home network.
TASMOTA_THERE = "Tasmota on the installer's network"
TASMOTA_HOME = "Tasmota on the home network"
GONE_BACK_EVERY = 30.0
# Nothing at all at the device's address this long, and the person is told to
# switch it off and on: Sonoff's firmware, its Wi-Fi gone while it started, does
# not try it again (seen after an outage), and a restart is safe here - the
# installer carries on (4.4.3), or the device starts Sonoff's firmware again.
NOTHING_THERE_HINT_AFTER = 120.0


def wait_for_bridge(ctx: Context, network: str, *, timeout: float | None = None,
                    gone_back: Callable[[], bool] | None = None, tasmota: bool = False) -> str | None:
    with interrupt.asking():  # waiting on the person: Ctrl-C stops at once
        return _wait_for_bridge(ctx, network, timeout=timeout, gone_back=gone_back, tasmota=tasmota)


def _wait_for_bridge(ctx: Context, network: str, *, timeout: float | None = None,
                     gone_back: Callable[[], bool] | None = None, tasmota: bool = False) -> str | None:
    """Wait for this computer to join the bridge's network, or for the bridge to
    turn up on the home network instead.  The bridge's address on the home
    network, or None once this computer has joined the bridge's own - or,
    with `tasmota`, TASMOTA_THERE or TASMOTA_HOME once Tasmota answers on it
    or on the home network instead (reach_bridge).

    A bridge on a weak signal can take longer to join the home network than
    the first look allows; it then never raises its own network, so waiting
    only for that would wait forever.
    """
    if timeout is None:
        timeout = join.user_action_limit()
    started = time.monotonic()
    looked = started
    heard = started  # the last time anything answered at the device's address
    hinted = False
    while time.monotonic() - started < timeout:
        host = ctx.state.input("device_ip")
        if host and hostsys.ping_ok(host):
            heard = time.monotonic()
        elif not hinted and time.monotonic() - heard >= NOTHING_THERE_HINT_AFTER:
            hinted = True
            output.progress_clear()
            with output.asking_to_switch_now():
                output.tell("    No answer from the device: switch it off and on - it is safe now.")
        if gone_back is not None and time.monotonic() - looked >= GONE_BACK_EVERY:
            looked = time.monotonic()
            if gone_back():
                output.progress_clear()
                return BACK_ON_SONOFF
        if tasmota and setup_network.tasmota_answers():
            output.progress_clear()
            return TASMOTA_THERE
        body = setup_network.page(setup_network.SETUP_HOST, timeout=2.0)
        if setup_network.is_bridge_page(body):
            output.progress_clear()
            output.event("joined", network=network)
            output.say(f"    Connected to {network}.")
            return None
        address = ctx.finder.poll()
        if address:
            body = setup_network.page(address, timeout=3.0)
            if setup_network.is_bridge_page(body):
                output.progress_clear()
                return address
            if tasmota and setup_network.is_tasmota_page(body) and tasmota_at_home(ctx):
                return TASMOTA_HOME
        output.progress(f"waiting for this computer to join {network}, or the installer "
                        f"to join your Wi-Fi... {time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()
    raise Abort(f"this computer did not join {network} within {timeout / 60:.0f} minutes, "
                "and the installer did not appear on your network")


# Tasmota with Wi-Fi details that do not work keeps retrying them and never
# opens its setup network.  Its quick power-cycle reset is the way out:
# settings.ino UpdateQuickPowerCycle, 7 boots in a row each under
# POWER_CYCLE_TIME (8 s), erases its settings and it starts as new.
def quick_power_cycle_hint(ctx: Context) -> list[str]:
    return [
        "It was given the Wi-Fi details the installer had just joined with, so",
        "this is unusual. If a few more minutes pass and it still has not appeared:",
        "",
        "  Switch the device off and on 7 times in a row, leaving it on for only",
        "  2-3 seconds each time, then leave it on.",
        "",
        "That makes Tasmota clear its settings and open its setup network",
        f"({names.setup_network_hint(ctx)}). This carries on from there by itself.",
    ]


def give_tasmota_the_wifi(ctx: Context, address: str) -> bool:
    """Have bridge v5 save Tasmota's settings with the Wi-Fi it joined with -
    and named as every firmware part way through is (autoflash-recovery-CDEF),
    until F4's reset gives it Tasmota's own name.  True when they have the
    Wi-Fi: on its own network the bridge has joined none, and 4.4.4 saves them
    without (tasmota_named_alone) - Tasmota then makes its own network under
    that name, not its default (tasmota-ABCDEF-1234)."""
    chip = chips.of(kinds.of_run(ctx.state).chip)
    template, reason = tasmota_settings.load_template(ROOT, pinned_artifacts(chip)["bluetooth"]["sha256"], chip)
    alone = address == setup_network.SETUP_HOST
    if template is None:
        if not alone:
            output.say(f"    Tasmota will need its Wi-Fi given by hand: {reason}.")
        return False
    if ctx.state.input("device_mac"):
        template = tasmota_settings.named(template, names.our_setup_network(ctx))
    elif alone:
        return False  # nothing to give it: the Wi-Fi is given on its network, as ever
    try:
        answer = setup_network.send_tasmota_settings(template, host=address,
                                                     layout=tasmota_settings.layout_query())
    except setup_network.SetupError as exc:
        if not alone:  # an installer before 4.4.4 refuses them there: Tasmota's own name
            output.say(f"    Tasmota will need its Wi-Fi given by hand: {exc}")
        return False
    if setup_network.SETTINGS_WITH_WIFI not in answer:
        ctx.state.set_input("tasmota_named_alone", True)
        output.say(f"    Tasmota will start as {names.our_setup_network(ctx)}, and make its own network by that name.")
        return False
    # Kept for the run: saved in the bridge, they outlast an upload cut off.
    ctx.state.set_input("tasmota_wifi_preset", True)
    ctx.state.set_input("tasmota_named_alone", False)
    output.say("    Tasmota will start with your Wi-Fi details, so it joins by itself.")
    return True


def tasmota_on_home_network(ctx: Context, *, timeout: float = 240.0) -> str | None:
    """Tasmota's address once it has started and joined, at the device's own MAC."""
    output.say("Waiting for Tasmota to start and join your network...")
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        address = tasmota_at_home(ctx)
        if address:
            return address
        output.progress(f"waiting for Tasmota on your network... {time.monotonic() - started:.0f}s")
        time.sleep(3.0)
    output.progress_clear()
    return None


def tasmota_at_home(ctx: Context) -> str | None:
    """Tasmota's address if it answers on the home network now, at the device's
    own MAC - the bridge's page, there before it, is not Tasmota's."""
    address = ctx.finder.poll()
    body = setup_network.page(address, timeout=3.0) if address else None
    if not setup_network.is_tasmota_page(body):
        return None
    output.progress_clear()
    ctx.set_device_ip(address)
    try:
        status = device.Device(address).command("Status 5")
        name = status.get("StatusNET", {}).get("Hostname") if isinstance(status, dict) else None
    except (DeviceError, OSError):
        name = None
    if name and not name.startswith(names.SETUP_SSID_PREFIX):  # a name it keeps
        ctx.state.set_input("device_name", name)
    output.say(f"    Tasmota is on your network at {address}" + (f" as {name}." if name else "."))
    return address


def upload_tasmota(ctx: Context, image: Path, host: str = setup_network.SETUP_HOST) -> None:
    payload = images.as_sent(ctx, "D1", image.read_bytes())
    output.say(f"    Sending Tasmota ({len(payload):,} bytes) - about a minute...")
    started = time.monotonic()
    try:
        setup_network.upload_to_bridge(payload, host=host)
    except setup_network.SetupError as exc:
        raise Abort(f"{exc}. The device still has the installer; run this again") from exc
    output.say(f"    Upload verified in {time.monotonic() - started:.0f}s; the device is restarting into Tasmota.")


def tasmota_or_its_network(ctx: Context, *, timeout: float,
                           networks: tuple[str, ...] | None = None) -> tuple[str, str] | None:
    """("network", its name) once Tasmota's setup network shows - one of
    `networks`, setup_networks' without - or ("home", its address) if Tasmota
    is on the home network instead: with settings the bridge saved earlier, it
    joins by itself and raises none."""
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        name = prompts.find_ssid(networks or names.setup_networks(ctx), timeout=8.0)
        if name:
            output.progress_clear()
            return "network", name
        address = tasmota_at_home(ctx)
        if address:
            return "home", address
        output.progress(f"looking for Tasmota's setup network... {time.monotonic() - started:.0f}s")
        time.sleep(4.0)
    output.progress_clear()
    return None


def tasmota_why(ctx: Context) -> str:
    """Why Tasmota, sent by the installer, waits on its own network, for the
    report: it was never given the Wi-Fi, or did not join with it."""
    return join.NOT_JOINED if ctx.state.input("tasmota_wifi_preset") else join.NOT_GIVEN


def finish_tasmota_setup(ctx: Context, *, route: str, wait: float = 120.0,
                         advice: tuple[str, list[str]] | None = None) -> dict[str, Any]:
    """Tasmota found on the home network, or set up through its own.

    `advice`: a section's title and lines, shown while Tasmota or its setup
    network is waited for - what to do if neither appears - and taken off the
    screen once one does: it has done its job.  Left if neither does, as it is
    then still what to do."""
    output.transient_begin()
    if advice:
        prompts.section(*advice)
    try:
        found = _wait_for_tasmota(ctx, wait)
    except BaseException:
        output.transient_keep()
        raise
    if found:
        output.transient_end()
    else:
        output.transient_keep()
    if found and found[0] == "home":
        return {"device_ip": found[1], "upload_route": route, "wifi": "preset",
                "device_name": ctx.state.input("device_name")}
    network = found[1] if found else None
    if network and network.startswith(names.TASMOTA_AP_PREFIX):
        names.note_setup_network(ctx, network)
        output.say(f"    This device is {network} - the same name shows in your router.")
    ip = join.configure_device_wifi(ctx, network, tasmota_why(ctx))
    named = network if network != names.our_setup_network(ctx) else ctx.state.input("device_name")
    return {"device_ip": ip, "device_name": named, "upload_route": route}


def _wait_for_tasmota(ctx: Context, wait: float) -> tuple[str, str] | None:
    """finish_tasmota_setup's wait: Tasmota on the home network, or its setup
    network; None if neither appears within `wait` seconds."""
    output.say("")
    output.say("Waiting for Tasmota to start and make its setup network...")
    networks = names.setup_networks(ctx)
    if ctx.state.input("tasmota_named_alone"):
        # Named as the installer was, Tasmota's network has the installer's
        # name: looked for once the installer has gone, so that its network,
        # still listed for a moment, is not taken for Tasmota's.
        installer_gone(timeout=60.0)
        networks = (names.our_setup_network(ctx), *networks)
    return tasmota_or_its_network(ctx, timeout=wait, networks=networks)


def installer_gone(*, timeout: float) -> None:
    """Wait, up to `timeout`, until the installer no longer answers on its own
    network: restarting into Tasmota once the upload is verified."""
    started = time.monotonic()
    while time.monotonic() - started < timeout and bridge_answers(setup_network.SETUP_HOST):
        output.progress(f"waiting for the installer to restart into Tasmota... "
                        f"{time.monotonic() - started:.0f}s")
        time.sleep(2.0)
    output.progress_clear()


def evidence_bridge_installed(ctx: "Context") -> tuple[bool | None, str]:
    """Only the bridge serves its page, and only it broadcasts that network.

    Bridge 4.4.1 raises its own network only when it cannot join the home one,
    so it is looked for on the home network first.
    """
    address = bridge_on_home_network(ctx, timeout=60.0)
    if address:
        return True, (
            f"The installer firmware is answering on your network at {address},\n"
            "  so this step did finish."
        )
    bridge = prompts.find_ssid(names.bridge_networks(ctx))
    if bridge:
        return True, (
            f"The device is broadcasting a Wi-Fi network called '{bridge}'.\n"
            "  At this stage only the installer firmware does that, so this step did finish."
        )
    return None, (
        "No installer network is visible from this computer. That does not mean\n"
        "  it failed - the device may still be starting up, or this computer's Wi-Fi\n"
        "  may be busy or switched off."
    )
