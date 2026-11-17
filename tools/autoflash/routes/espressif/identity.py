"""The device's Sonoff identity, wherever it is (tools/espressif/sonoff_identity.py):
kept on its way to Tasmota (K1), taken off Tasmota's layout as it leaves (L1),
and found again on its way back to Sonoff's firmware (R1).
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

from tools.autoflash import hostsys, kinds, output, plans, records
from tools.autoflash.errors import Abort
from tools.autoflash.routes.espressif import hub
from tools.autoflash.state import PENDING
from tools.espressif import flashmap, sonoff_identity
from tools.espressif.safeboot.safeboot_migration import OFFICIAL_TABLE_SHA256
from tools.espressif.sonoff_identity import Identity, IdentityError
from tools.ewelink.get_device_key import dispatch_server
from tools.autoflash.routes.espressif import images

if TYPE_CHECKING:
    from tools.autoflash.context import Context
    from tools.espressif.layouts import Layout


# Testing (cli.py --simulate-no-identity): a device that keeps no Sonoff identity
# - in its settings or its sector - and is in no eWeLink account, as one
# another tool converted and that was then removed from the app.
SIMULATE_NONE = False


def from_backup(ctx: Context) -> Identity | None:
    """The identity in the device's Sonoff backup on this computer."""
    mac = ctx.state.input("device_mac")
    backup = records.find_backup(mac) if mac else None
    if backup is None:
        return None
    try:
        flash = (backup.directory / "flash.bin").read_bytes()
        partition = flashmap.nvs(flash)
        return sonoff_identity.from_nvs(flash[partition.offset:partition.offset + partition.size])
    except (OSError, ValueError, KeyError):
        return None


def nvs_size(layout: Layout) -> int:
    return next(size for label, _, _, _, size, _ in layout.partitions() if label == "nvs")


def from_settings(ctx: Context, layout: Layout) -> Identity | None:
    """The identity in the settings of a device on Sonoff's layout, read through
    the Berry Tasmota: Sonoff's own, kept by ESPHome, or put there as it left
    Tasmota. A read that fails raises: a device that did not answer may keep one."""
    if SIMULATE_NONE:
        return None
    return sonoff_identity.from_nvs(ctx.device.read_nvs(layout.nvs, nvs_size(layout)))


def kept_in_sector(ctx: Context) -> Identity | None:
    """The identity in the sector at the top of the device's flash, found by what
    the sector says it is, whatever the table: on Tasmota's layout the table's
    entry for it may have gone with a reflash, which is said. On Sonoff's
    layout it is the end of ota_1, past any firmware written there: the
    updates that bring a device back to the hub write only as far as their
    image, so it is still there when the startup firmware has wiped the
    settings (its persist-settings.patch), and its checksum shows it whole.
    A read that fails raises, as from_settings."""
    if SIMULATE_NONE:
        return None
    device = ctx.device
    header = device.read_region(sonoff_identity.SECTOR_AT, sonoff_identity.HEADER.size)
    if header[:len(sonoff_identity.MAGIC)] != sonoff_identity.MAGIC:
        return None
    length = sonoff_identity.HEADER.unpack(header)[2]
    sector = header + device.read_region(sonoff_identity.SECTOR_AT + len(header),
                                         min(length, sonoff_identity.SECTOR_SIZE - len(header)))
    identity = sonoff_identity.from_sector(sector)
    if identity is not None and device.sha_region(plans.TABLE_ADDRESS, plans.SECTOR) == OFFICIAL_TABLE_SHA256:
        output.say("    The device's partition table no longer has the entry for the sector that")
        output.say("    keeps its Sonoff identity (Tasmota written over it whole, say), but the")
        output.say("    sector itself is there.")
    return identity


def account_record(ctx: Context, identity: Identity | None = None) -> dict[str, Any] | None:
    """The device's record in its eWeLink account: by its id where that is known,
    else by its Wi-Fi MAC, which the record carries."""
    if SIMULATE_NONE:
        return None
    devices = ctx.account_devices() or []
    mac = hostsys.normalise_mac(str(ctx.state.input("device_mac") or ""))
    for device in devices:
        params = device.get("params") or {}
        if (identity and device.get("deviceid") == identity.deviceid) or (
                mac and params.get("staMac") and hostsys.normalise_mac(str(params["staMac"])) == mac):
            return device
    return None


def from_account(ctx: Context, kind: kinds.Kind) -> Identity | None:
    """The identity rebuilt from the device's eWeLink account record."""
    device = account_record(ctx)
    if device is None:
        return None
    try:
        return sonoff_identity.from_account(device, dispatch_server(ctx.ewelink_region()),
                                            encrypted=images.chip_of(kind).key == "esp32")
    except IdentityError as exc:
        output.tell(f"    The device's eWeLink record cannot rebuild its identity: {exc}.")
        return None


def for_tasmota(ctx: Context, layout: Layout) -> tuple[Identity | None, str]:
    """The identity to keep on the way to Tasmota, and where it came from: the
    backup on this computer, else the device's own settings."""
    identity = from_backup(ctx)
    if identity is not None:
        return identity, "its Sonoff backup on this computer"
    identity = from_settings(ctx, layout)
    return identity, "its own settings"


def step_keep_identity(ctx: Context) -> dict[str, Any]:
    """K1 - the device's Sonoff identity, kept in the sector at the top of its
    memory from the hub: on the way to Tasmota before the commit takes
    Sonoff's settings, and on the way to ESPHome, which keeps them but loses
    them as it is left (kept_in_sector). From there the device can go back to
    Sonoff's firmware with no backup on the computer."""
    if ctx.state.status("E3") != PENDING or ctx.state.status("I1") != PENDING:
        # A run begun before this step was, carried on past the hub: there is
        # no Berry Tasmota on Sonoff's layout left to write it from.
        output.say("Left out: this run was begun by an earlier version of this tool, and is past the hub.")
        return {"skipped": "begun before this step, and past the hub"}
    kind = kinds.of_run(ctx.state)
    layout = images.layout_of(kind)
    hub.sonoff_layout_back(ctx)
    identity, where = for_tasmota(ctx, layout)
    if identity is None:
        output.tell("    No Sonoff identity or backup here: going back to Sonoff will need eWeLink.")
        return {"kept": False}
    output.say(f"Keeping the device's Sonoff identity, from {where}, in the last sector of its memory,")
    output.say("so that it can go back to Sonoff's firmware without its backup.")
    try:
        plan = plans.keep_identity(sonoff_identity.to_sector(identity), layout)
    except (IdentityError, plans.PlanError) as exc:
        raise Abort(f"the identity cannot be kept: {exc}") from exc
    return hub.run_plan(ctx, plan) | {"kept": True, "from": where}
