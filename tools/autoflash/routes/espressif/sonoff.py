"""Back to Sonoff's own firmware, from the device's backup - or, with none on
this computer, from Sonoff's official firmware and the device's identity (R1, R2).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from tools.autoflash import kinds, output, plans, records, runs
from tools.autoflash.errors import Abort, NothingWritten
from tools.autoflash.routes import arrival
from tools.autoflash.routes.espressif import hub, identity, sonoff_firmware
from tools.espressif import sonoff_identity
from tools.autoflash.routes.espressif import images

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def sonoff_backup(ctx: Context) -> bytes:
    """The device's own Sonoff backup, checked to be this device's."""
    mac = ctx.state.input("device_mac")
    backup = records.find_backup(mac) if mac else None
    if backup is None:
        raise Abort("there is no backup of this device's Sonoff firmware on this computer")
    try:
        return (backup.directory / "flash.bin").read_bytes()
    except OSError as exc:
        raise Abort(f"the backup could not be read: {exc}") from exc


def rebuilt(ctx: Context, kind: kinds.Kind) -> tuple[bytes, str]:
    """For a device with no backup on this computer, what its backup would have
    held: Sonoff's firmware for it, its layout, and settings holding its Sonoff
    identity and the Wi-Fi it is on now. The identity is the one the device kept -
    in its settings, or in the sector at the top of its flash, which outlasts
    the startup firmware wiping the settings on the way here - else rebuilt
    from its eWeLink account record. Says which."""
    layout = images.layout_of(kind)
    kept = identity.from_settings(ctx, layout) or identity.kept_in_sector(ctx)
    found = kept or identity.from_account(ctx, kind)
    if found is None:
        raise NothingWritten(
            "there is no backup of this device's Sonoff firmware on this computer, the device keeps "
            "no Sonoff identity, and its eWeLink account has no record of it to rebuild one from")
    where = ("the Sonoff identity the device kept" if kept else
             "its eWeLink account's record of it (its id, key, model and kind)")
    try:
        app, source = sonoff_firmware.official_app(ctx, kind, found, layout)
        settings = sonoff_identity.nvs(found, identity.nvs_size(layout), ctx.wifi())
        flash = sonoff_firmware.flash_of(images.sonoff_table(kind), settings, app, layout)
    except (sonoff_firmware.FirmwareError, sonoff_identity.IdentityError, images.ImageError) as exc:
        raise NothingWritten(f"Sonoff's firmware cannot be put back without a backup: {exc}") from exc
    output.say("There is no backup of the device's Sonoff firmware on this computer, so it")
    output.say(f"is put together: Sonoff's firmware from {source}, and its identity from")
    output.say(f"{where}, with the Wi-Fi it is on now.")
    ctx.state.capture("R1.rebuilt", {"firmware": source, "identity": "kept" if kept else "account"})
    ctx.state.capture("R1.identity", "kept" if kept else "account")
    return flash, where


def put_back(ctx: Context, why: str) -> dict[str, Any]:
    """R1 with no way to put Sonoff's firmware together: the device, on the
    interim Tasmota here, goes back to the firmware it came from - ESPHome's
    to ESPHome, the rest to Tasmota (one found part way, too) - rather than
    being left part way. Its record says so (put_back) for the report and the
    end of the run; and that it is part way now, so a run carried on goes on
    there."""
    from tools.autoflash import routes  # the routes import this module
    came = runs.run_source(ctx.state)
    firmware = "esphome" if came == "esphome" else "tasmota"
    ctx.state.set_input("put_back", {"came_from": came, "why": why})
    ctx.state.set_input("firmware", firmware)
    ctx.state.set_input("source", "midway")
    routes.go_on_instead("R1", firmware, "midway")
    # Rather than being left part way; the run ends by saying so too (runner).
    output.tell(f"    Sonoff's firmware cannot be put back ({why}): going back to {routes.FIRMWARES[firmware]} "
                "instead.")
    return {"stage_outcome": "stopped", "put_back": firmware}


def step_sonoff_restore(ctx: Context) -> dict[str, Any]:
    """R1 - the device's own Sonoff firmware and settings, from its backup, beside
    the Tasmota it runs; the boot selection set to it last, then a restart.
    Two plans, as Sonoff's firmware is more than one may write. With no backup
    on this computer, they write what it would have held (rebuilt)."""
    kind = kinds.of_run(ctx.state)
    layout = images.layout_of(kind)
    mac = ctx.state.input("device_mac")
    if mac and records.find_backup(mac):
        backup, source = sonoff_backup(ctx), "backup"
        ctx.state.capture("R1.identity", "backup")
    else:
        try:
            backup, source = rebuilt(ctx, kind)
        except NothingWritten as exc:
            return put_back(ctx, str(exc))
    try:
        running = out_of_sonoffs_slot(ctx, backup, kind)
        first, second = plans.back_to_sonoff(backup, running=running, layout=layout)
    except plans.PlanError as exc:
        raise Abort(f"the backup cannot be written back: {exc}") from exc
    hub.run_plan(ctx, first)
    return hub.run_plan(ctx, second) | {"from": source}


def out_of_sonoffs_slot(ctx: Context, backup: bytes, kind: kinds.Kind) -> int:
    """The slot Tasmota runs from, once it is not the one Sonoff's firmware goes
    back in. A device that ran Sonoff's firmware from its second slot has it there
    in its backup, where the hub runs: Tasmota sent again goes in the other."""
    device = ctx.device
    running = hub.running_slot(device)
    if running != plans.stock_slot(backup, images.layout_of(kind)):
        return running
    output.say("The device's Sonoff firmware goes back in the half of its memory Tasmota")
    output.say("runs from, so Tasmota moves to the other half first.")
    booted, uptime = device.started_at(), device.uptime_seconds()
    hub.upload_or_prompt(ctx, images.interim_of(kind), "Berry Tasmota")
    device.wait_for_reboot(timeout=300, started_at=booted, before=uptime)
    moved = hub.running_slot(device)
    if moved == running:
        raise Abort(f"the device is still running from slot {running} after being sent Tasmota again")
    return moved


def step_sonoff_check(ctx: Context, wait: float = arrival.SONOFF_WAIT) -> dict[str, Any]:
    """R2 - Sonoff's firmware started, on the Wi-Fi it had and back in eWeLink,
    as the Beken route's K3 finds it (arrival.back_on_sonoff)."""
    return arrival.back_on_sonoff(ctx, wait=wait)
