"""From Sonoff's firmware: what the device reports of itself (B1), and the new
firmware sent to it through eWeLink (B2) - the one write Sonoff's firmware
takes, after which the device's stock bootloader applies the image and the new
firmware starts, reading the device's Wi-Fi from the stock store it leaves untouched.
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import account, detect, firewall, join, kinds, output, prompts, records, runs, testers
from tools.autoflash.errors import Abort, NothingWritten, StoppedPartWay
from tools.autoflash.routes import arrival
from tools.autoflash.routes.beken import images
from tools.beken import fota_wrap, interim_marker
from tools.ewelink import ota_push
from tools.ewelink.get_device_key import EwelinkError
from tools.ewelink.query_ota import latest_offer, ota_identity

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The stock updater fetches the file in ranges, at its own pace: a few minutes.
DELIVERY_WAIT = 300.0


def note_official_file(ctx: Context, kind: kinds.Kind, reported_version: str) -> dict[str, Any]:
    """What the device is known by, checked and logged for the next person: its
    official update file's partition table, firmware name and version. Refused
    here (images.official), a run writes nothing. Stock firmware says nothing
    of its flash, so this table is the only word on it until OpenBeken has read
    the device's own data where the table has it (arrived._back_up)."""
    found = images.official(kind, ctx)
    output.log_only(f"    Official update file: {found.name}, version {found.version}, from {found.source}; "
                    f"the device reports {reported_version}")
    output.log_only(f"    Its partition table: {found.layout.key}: " + ", ".join(map(str, found.table)))
    output.log_only(f"    Flash size: the stock firmware does not say; the table is for "
                    f"{found.layout.flash_size // 0x100000} MB")
    return {"official_name": found.name, "official_version": found.version, "layout": found.layout.key}


def step_identify(ctx: Context) -> dict[str, Any]:
    """B1 - what the device reports of itself, through eWeLink: its model and
    firmware, which must be the kind's."""
    device = account.account_record(ctx)
    try:
        identity = ota_identity(device)
    except EwelinkError as exc:
        raise Abort(str(exc)) from exc
    product = str(device.get("productModel") or "").strip().upper()
    # As A2 knows an Espressif device's: by its product model first, so a kind
    # that shares its stock model with another (product_model_decides) is told apart.
    kind = kinds.for_account(product, identity["model"])
    name = account.note_firmware_name(kind, offered_latest(ctx, identity), identity["deviceid"], product,
                                      identity["model"])
    if kind is None:
        raise Abort(f"the device is a {identity['model']}, which this tool does not convert yet. Nothing was written")
    official = note_official_file(ctx, kind, identity["version"])
    ctx.state.set_input("device_kind", kind.key)
    ctx.state.set_input("stock_version", identity["version"])
    ctx.state.set_input("product_model", product)
    prompts.section("What the device reported", quiet=True, body=[
        f"Model            {identity['model']}",
        f"Product          {product or 'not given'}",
        f"Firmware now     {identity['version']}",
        "",
        "The device fetches its new firmware straight from this computer, as its",
        "own updates come, and its bootloader installs it on the next start.",
    ])
    # Its Wi-Fi signal, as it last told eWeLink: read before anything is
    # written, while moving it costs nothing, as A2 reads an Espressif device's.
    signal = account.account_signal(device)
    join.note_signal(ctx, signal, [
        "Nothing has been written yet. After the conversion the device has to",
        "rejoin your Wi-Fi on its own; a weak signal makes that less certain."])
    return {"stock_version": identity["version"], "model": identity["model"], "product": product,
            "signal_dbm": signal, "firmware_name": name, **official}


def offered_latest(ctx: Context, identity: dict[str, str]) -> dict[str, Any]:
    """eWeLink's offer of the latest firmware to this device, as query_ota.latest_offer
    gives it, to read the firmware name from: {"problem": ...} where eWeLink is not reached."""
    try:
        return ctx.ewelink.use(lambda region, session: latest_offer(region, session["at"], identity))
    except EwelinkError as exc:
        return {"problem": type(exc).__name__}


def first_firmware(ctx: Context) -> tuple[str, str]:
    """The firmware B2 sends, and its name to the person: the interim
    firmware, for a run to a firmware that has one (images.INTERIM)."""
    chosen = runs.run_firmware(ctx.state)
    firmware = images.first_firmware(chosen)
    return firmware, images.INTERIM_NAME if firmware != chosen else detect.NAMES[firmware]


def payload(ctx: Context, firmware: str, image: bytes) -> bytes:
    """What B2 puts in the download slot: the image - and, for the interim
    firmware, the marker after it, by which it knows itself as that
    (tools/beken/interim_marker.py).  A run to OpenBeken sends the image alone."""
    image = images.as_sent(ctx, "B2", image)
    return interim_marker.with_marker(image) if firmware != runs.run_firmware(ctx.state) else image


def answers_as(ctx: Context) -> str:
    """What the device answers as once B2's firmware has started (detect.identify)."""
    firmware = first_firmware(ctx)[0]
    return "interim" if firmware != runs.run_firmware(ctx.state) else firmware


def step_push(ctx: Context) -> dict[str, Any]:
    """B2 - the new firmware, sent to the device through eWeLink - the interim
    firmware, on a run that has one.  The first write: once the device has it, its bootloader replaces Sonoff's
    firmware on the next start, and there is no going back but the way this
    tool offers (Sonoff's firmware fetched again, from the public dump)."""
    kind = kinds.of_run(ctx.state)
    firmware, shown = first_firmware(ctx)
    device_ip, stock_version = ctx.state.require("device_ip", "stock_version")
    device = account.account_record(ctx)
    image, version = images.image(ctx, firmware)
    chosen = runs.run_firmware(ctx.state)
    if chosen != firmware:
        # The firmware the interim one fetches (J2), checked now as the Espressif
        # route's A5 checks every file it sends: refused after this, it would
        # leave the device on the interim firmware.
        images.image(ctx, chosen)
    container = fota_wrap.wrap(payload(ctx, firmware, image), name=kind.wrapper_model, version=images.FOTA_VERSION)
    listen_ip = ctx.listen_ip()
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    firewall.ensure_firewall(ctx)
    # Checked again here: nothing reaches the device but past this, a resumed run too.
    images.official(kind, ctx)
    if not getattr(ctx, "consented", False):  # once a run: not again when sent again (errors.GoBack)
        mac = ctx.state.input("device_mac")
        backup = records.find_backup(mac) if mac else None
        # As C2 says it, of the backup B4 takes once the new firmware runs (arrived._back_up).
        if getattr(getattr(ctx, "args", None), "no_backup", False):
            kept = "No backup of its own data is kept (--no-backup)."
        elif backup:
            kept = f"Its own data is backed up already ({backup.taken or 'earlier'})."
        else:
            kept = "Its own data is backed up to this computer once the new firmware runs."
        output.transient_begin()
        prompts.warning_box("Before going on", [
            "Its Sonoff firmware is replaced: the eWeLink app no longer controls it.",
            prompts.DO_NOT_CUT_POWER,
            kept,
            "Only go ahead with a device you can afford to lose.",
            *testers.notice(kind),
        ])
        if not prompts.require_word(prompts.CONSENT_WORD):
            raise NothingWritten("not confirmed - nothing has been written to the device")
        output.transient_end()  # agreed: the note and the question have done their job
        ctx.consented = True

    def check(found: dict[str, Any], model: str) -> None:
        # Only the device A1 found, as it was at B1: another device on the
        # account, or a device updated since, is never sent this.
        if model != kind.stock_model:
            raise EwelinkError(f"the device's model is {model}, not the {kind.name}'s {kind.stock_model}")
        reported = str((found.get("params") or {}).get("fwVersion"))
        if reported != stock_version:
            raise EwelinkError(f"the device reports firmware {reported}, not the {stock_version} it reported at B1")

    while True:
        output.say(f"Sending {shown} {version} ({len(image):,} bytes; {len(container):,} wrapped) through eWeLink...")
        started = time.monotonic()
        delivered = ota_push.deliver(
            account=ctx.ewelink, device_id=device["deviceid"], firmware=container, version=images.FOTA_VERSION,
            # One file, not the two an Espressif device is offered (one for each app
            # slot): a Beken device has one app, which its bootloader replaces.
            listen_ip=listen_ip, listen_port=port, device_ip=device_ip, names=("user1.bin",),
            wait=DELIVERY_WAIT, check_device=check, say=output.say, live=False,
            event=output.event)
        if not delivered.sent:
            raise Abort(f"the firmware was not sent: {delivered.error or 'eWeLink did not take the command'}. "
                        f"{ota_push.NOT_SENT}")
        if not ota_push.busy(delivered.error):
            break
        # Busy with an update cut off part way: switched off and on, then sent
        # again, as the Espressif route's C2 does (installer.step_bridge).
        if prompts.UNATTENDED:
            raise Abort(arrival.BUSY)
        arrival.power_cycle(ctx, "The firmware was turned down", arrival.BUSY_LINES, "send it again")
        if not arrival.online_in_ewelink(ctx, device["deviceid"]):
            raise Abort(arrival.BUSY)
    if not delivered.complete:
        # Sent, and some of it may be on the device: the device says what it runs
        # when the step is asked about (evidence_pushed). A power cut part way
        # is the usual cause, not something unexpected.
        raise StoppedPartWay(
            f"the delivery stopped part way: the device fetched {delivered.bytes_served:,} of "
            f"{len(container):,} bytes ({delivered.error or 'it stopped asking'}). It keeps its Sonoff firmware: "
            "switch it off and on, wait until it shows online in the eWeLink app - a few minutes after a "
            "delivery cut off - then run the same command again")
    seconds = round(time.monotonic() - started, 1)
    output.say(f"    The device has the whole image after {seconds}s; its bootloader installs it as it restarts.")
    return {"seconds": seconds, "firmware": firmware, "version": version, "bytes": len(container)}


def evidence_pushed(ctx: Context) -> tuple[bool | None, str]:
    """Whether a push cut off got through: the device answers as the firmware it
    was sent (it restarted into it), or as nothing this tool knows yet."""
    host = ctx.state.input("device_ip")
    shown = first_firmware(ctx)[1]
    running = detect.identify(host, timeout=3.0) if host else None
    if running == answers_as(ctx):
        return True, f"The device answers as {shown}: the firmware was delivered and installed."
    if running == "sonoff":
        return False, "The device still answers as Sonoff's firmware: the delivery did not finish."
    return None, "The device does not answer as the new firmware yet - it may still be installing it."
