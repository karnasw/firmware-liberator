"""A readout of a Beken (BK7238) device the tool does not convert yet.

A BK7238 has one app area, not two, so reading its whole flash means
installing firmware that replaces Sonoff's for a few minutes, then putting
Sonoff's back. This installs a pin-free OpenBeken (artifacts/openbeken-readout,
which drives nothing on an unmapped device), reads the flash through it, and
has it fetch the device's own Sonoff firmware back - the file eWeLink offers
the device, the same the stock bootloader would apply.

Nothing is written until the offered file's partition table is checked to be
the 4 MB BK7238 one the readout OpenBeken is built for: on any other table its
AutoflashRevertToStock would refuse, and the download slot could be wrong.

Each stage leans on the conversion's own helpers, with their retries: the
firmware is delivered through eWeLink (tools/ewelink/ota_push), the flash read
in paced, retried chunks (tools/autoflash/openbeken.read_flash), the revert
served until the device has every byte (routes/beken/onward.serve_to_device).
The copy is anonymised (tools/beken/scrub.py) before anything is sent.

Proven end to end on the emulated S61 - the round trip, the device back
online in eWeLink, the flash anonymised - and not yet run on a real Beken
device.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import firewall, hostsys, openbeken, output, records, prompts, readout, runs, safety_locks
from tools.autoflash.errors import Abort, NothingWritten, StoppedPartWay
from tools.autoflash.routes import arrival
from tools.autoflash.routes.beken import images, onward
from tools.autoflash.runs import ROOT
from tools.beken import fota_wrap, layouts, rbl, scrub
from tools.ewelink import ota_push

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The readout OpenBeken, as firmware.json pins it: the one image for any BK7238.
PIN = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))["beken"]["readout"]["openbeken"]
WHOLE_FLASH = 0x400000
DELIVERY_WAIT = 300.0
# How long to wait for the readout OpenBeken, then Sonoff's firmware, to answer.
ARRIVE_WAIT = 300.0
SONOFF_WAIT = 300.0
REVERT_URL_NAME = onward.IMAGE_NAME   # firmware.rbl


class Stock:
    """The device's own Sonoff firmware, from the file eWeLink offers it: the
    rt_ota RBL its bootloader applies, and the 4 MB table that file holds."""

    def __init__(self, rbl_image: bytes, version: str, layout: "layouts.Layout") -> None:
        self.rbl, self.version, self.layout = rbl_image, version, layout


def stock_from_offered(updates) -> Stock | None:
    """The offered file that is a whole stock image on a table this tool knows:
    its RBL, to revert with, and the table. None if none qualifies - then the
    device cannot be read out, and only what it reported is sent."""
    for item in updates:
        try:
            images = fota_wrap.unwrap(item.data)
            if len(images) != 1:
                continue
            rbl_image = images[0][2]
            app = rbl.unpack(rbl_image)
            layout, _table = layouts.known(app)
        except (ValueError, rbl.RblError, layouts.LayoutError):
            continue
        return Stock(rbl_image, images[0][1] or item.version, layout)
    return None


def readout_image() -> bytes:
    """The readout OpenBeken's rt_ota RBL, checked against its pin."""
    data = (ROOT / PIN["file"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != PIN["sha256"]:
        raise Abort(f"{PIN['file']} is not the file firmware.json pins: refusing to send it")
    return data


def deliver_readout(ctx: Context, name: str, version: str, device: dict[str, Any]) -> None:
    """Send the readout OpenBeken to the device through eWeLink, wrapped under
    the firmware name its updater expects. The first write."""
    container = fota_wrap.wrap(readout_image(), name=name, version=fota_wrap.ANNOUNCED_VERSION)
    device_ip = ctx.state.require("device_ip")[0]
    port = firewall.choose_port(ctx, "delivery_port", firewall.DELIVERY_PORTS, "the firmware")
    firewall.ensure_firewall(ctx)

    def check(found: dict[str, Any], model: str) -> None:
        reported = str((found.get("params") or {}).get("fwVersion"))
        if reported != version:
            raise ota_push.EwelinkError(f"the device reports firmware {reported}, not the {version} it reported")

    while True:
        output.say("Sending the readout firmware through eWeLink...")
        with safety_locks.lifted(ROOT, "RECOVERY_LOCK"):
            delivered = ota_push.deliver(
                account=ctx.ewelink, device_id=device["deviceid"], firmware=container,
                version=fota_wrap.ANNOUNCED_VERSION, listen_ip=ctx.listen_ip(), listen_port=port, device_ip=device_ip,
                names=("user1.bin",), wait=DELIVERY_WAIT, check_device=check, say=output.say, live=False,
                event=output.event)
        safety_locks.say_lock_back(ROOT, "RECOVERY_LOCK")
        if not delivered.sent:
            raise NothingWritten(f"the readout firmware was not sent: {delivered.error or 'eWeLink did not take it'}. "
                                 f"{ota_push.NOT_SENT}")
        if not ota_push.busy(delivered.error):
            break
        # As a conversion's B2 and C2: switched off and on, then sent again.
        if prompts.UNATTENDED:
            raise Abort(arrival.BUSY)
        arrival.power_cycle(ctx, "The firmware was turned down", arrival.BUSY_LINES, "send it again")
        if not arrival.online_in_ewelink(ctx, device["deviceid"]):
            raise Abort(arrival.BUSY)
    if not delivered.complete:
        # A power cut or the Wi-Fi going part way, as a conversion's B2
        # (stock.py): the device keeps Sonoff's firmware.
        raise StoppedPartWay(
            f"the delivery stopped part way: the device fetched {delivered.bytes_served:,} of "
            f"{len(container):,} bytes ({delivered.error or 'it stopped asking'}). It keeps its Sonoff firmware: "
            "switch it off and on, wait until it shows online in the eWeLink app, then run the readout again")
    ctx.state.set_input("readout_sent", True)
    output.say("    The device has the whole image; its bootloader installs it as it restarts.")


def wait_for_openbeken(ctx: Context, wait: float = ARRIVE_WAIT) -> str:
    """The address OpenBeken answers at, once it has started and joined the
    Wi-Fi. Abort if the device stayed on, or went back to, Sonoff's firmware."""
    output.say("Waiting for the readout firmware to start and join your Wi-Fi...")

    def look(host: str) -> str | None:
        return host if openbeken.info(host, timeout=3.0) else None

    found = arrival.until_back(ctx, lambda restarted: _poll(ctx, look, wait),
                               "The readout firmware", (
        "The readout firmware has not answered on your Wi-Fi. It installs as the device restarts, then reads "
        "the device's Wi-Fi from its stock store and joins; if it has not, switch the device off and on, then "
        "run this again"))
    output.say(f"    OpenBeken answered at {found}.")
    return found


def _poll(ctx: Context, look, wait: float):
    """look(host) until it returns, at the device's address or wherever its
    hardware address moved to; NotBack if the time runs out."""
    import tools.autoflash.join as join
    try:
        return join.wait_for(ctx, look, "the readout firmware on your Wi-Fi", timeout=wait)
    except Abort as exc:
        raise arrival.NotBack from exc


def read_whole_flash(ctx: Context, host: str) -> bytes:
    """The device's whole flash, read through OpenBeken (paced, retried chunks)."""
    def progress(done: int, total: int) -> None:
        output.progress(f"reading the device's memory: {100 * done // total}%")

    # A read cut off - the device switched off, the Wi-Fi gone - leaves it on
    # the readout firmware, which a readout run again finds and reads from
    # (read_out): a stop to act on, not an error nobody foresaw.
    again = ("It is still on the readout firmware: switch it off and on, then run the readout again to read it "
             "and put Sonoff's firmware back")
    try:
        # Its start an event, which the fault suites time a fault in the read
        # from, and in the run's log, which a wait on screen is not.
        output.event("phase", phase="read")
        output.log_only("Reading the device's memory...")
        with output.working("Reading the device's memory - a few minutes..."):
            flash = openbeken.read_flash(host, 0, WHOLE_FLASH, progress=progress)
    except OSError as exc:
        raise Abort(f"the connection to the device was lost as its memory was read: {exc}. {again}",
                    failure="device-lost") from exc
    finally:
        output.progress_clear()
    if len(flash) != WHOLE_FLASH:
        raise Abort(f"the connection to the device was lost as its memory was read: only {len(flash):,} of "
                    f"{WHOLE_FLASH:,} bytes were read. {again}", failure="device-lost")
    output.event("phase", phase="read-done")
    output.say(f"    Read {len(flash):,} bytes.")
    return flash


def revert_to_sonoff(ctx: Context, host: str, found: Stock) -> None:
    """Have OpenBeken fetch the device's own Sonoff firmware back into the
    stock download slot - its AutoflashRevertToStock checks the slot first -
    and the bootloader applies it on the restart that follows."""
    output.event("phase", phase="back")
    output.say("Putting the device's Sonoff firmware back...")

    def fetch(url: str) -> None:
        try:
            openbeken.command(host, f"AutoflashRevertToStock {url}")
        except openbeken.CommandError as exc:
            raise Abort(f"the readout firmware would not fetch Sonoff's firmware back: {exc}. Its data is "
                        "read; run the readout again to put Sonoff's firmware back") from exc

    onward.serve_to_device(ctx, found.rbl, f"Sonoff's firmware {found.version}", fetch,
                         unstick=lambda: openbeken.restart(host))


def wait_for_sonoff(ctx: Context, device_id: str, wait: float = SONOFF_WAIT) -> bool:
    """Wait for Sonoff's firmware to start and show online in eWeLink again.
    Whether it did; the readout is kept and sent either way."""
    from tools.ewelink import relay as ewelink_relay
    started = time.monotonic()
    with output.working("Waiting for Sonoff's firmware to start and rejoin eWeLink..."):
        while time.monotonic() - started < wait:
            devices = ctx.account_devices() if device_id else None
            online = any(found.get("deviceid") == device_id and found.get("online") for found in devices or [])
            if online and ewelink_relay.answers(account=ctx.ewelink, device_id=device_id):
                break
            output.progress(f"waiting for eWeLink to show the device online... {time.monotonic() - started:.0f}s")
            time.sleep(10)
        else:
            online = False
    if online:
        output.progress_clear()
        output.event("phase", phase="back-done")
        output.say(f"    eWeLink shows the device online again after {round(time.monotonic() - started)}s.")
        return True
    output.progress_clear()
    output.tell("    If the eWeLink app doesn't show it yet, switch it off and on once more.")
    return False


def read_out(ctx: Context, name: str, version: str, device: dict[str, Any],
             found: Stock, *, skippable: bool = False) -> tuple[bytes, dict[str, str], bool]:
    """Install the readout firmware, read the whole flash, put Sonoff back.
    The whole flash, what OpenBeken said of itself, and whether Sonoff answered."""
    output.transient_begin()
    prompts.warning_box("Before going on", [
        "Its Sonoff firmware is swapped for a small reader for a few minutes, then",
        "put back, its settings and eWeLink pairing kept.",
        prompts.DO_NOT_CUT_POWER,
        "A reader that can't start may need a serial cable.",
        "Only go ahead with a device you can afford to lose.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD, skip=skippable):
        raise NothingWritten("skipped as asked, nothing was sent to it" if skippable else
                             "not confirmed - nothing has been sent to the device")
    output.transient_end()
    device_ip = ctx.state.require("device_ip")[0]
    # From here until Sonoff's firmware is fetched back, the reader may stand
    # in its place: what a Ctrl-C ends by saying (readout.stopped_by_ctrl_c).
    ctx.state.set_input(readout.STATE_KEY, readout.REPLACED)
    if openbeken.is_readout(device_ip):
        # A readout stopped part way left it there: eWeLink cannot reach the
        # device on it, and has no need to.
        output.say("The readout firmware is on the device already, from a readout stopped part way.")
        host = device_ip
    else:
        deliver_readout(ctx, name, version, device)
        host = wait_for_openbeken(ctx)
    info = openbeken.info(host) or {}
    flash = read_whole_flash(ctx, host)
    mac = ctx.state.input("device_mac")
    if mac:
        directory, saved = records.save_beken_backup(mac, flash[records.BEKEN_REGION[0]:], found.rbl, found.version,
                                                   kind="a device being added")
        if saved:
            output.say(f"    The device's own data is kept on this computer, in {runs.display_path(directory)}.")
    readout.restoring()
    revert_to_sonoff(ctx, host, found)
    ctx.state.set_input(readout.STATE_KEY, "read")
    back = wait_for_sonoff(ctx, device["deviceid"])
    return flash, {"openbeken": str(info.get("build") or ""), "chipset": str(info.get("chipset") or "")}, back


def read_and_anonymise(ctx: Context, manifest: dict[str, Any], name: str, version: str, device: dict[str, Any],
                       found: Stock, *, skippable: bool = False) -> tuple[dict[str, bytes], bool]:
    """Read the device's whole flash through the readout OpenBeken, putting
    Sonoff's firmware back (read_out), and anonymise it. The files to offer,
    and whether the device answered as Sonoff's firmware again. `skippable`:
    one device of several, which its question may pass by."""
    manifest["layout"] = found.layout.key
    flash, info, back = read_out(ctx, name, version, device, found, skippable=skippable)
    manifest["read"] = True
    # What every genuine readout of it shares: its table, and the Sonoff
    # firmware it was put back on (the offered file's, by its hash).
    manifest["firmware"] = {"layout": found.layout.key, "stock_version": found.version,
                            "stock_sha256": hashlib.sha256(found.rbl).hexdigest()}
    manifest["flash"] = {"size": len(flash), "sha256": hashlib.sha256(flash).hexdigest()}
    manifest["device"] = info
    scrubbed, changed = scrub.scrub(flash)
    manifest["anonymised"] = changed
    return {"flash.bin": scrubbed, "info.txt": (json.dumps(info) + "\n").encode()}, back


def saved_stock(mac: str | None) -> Stock | None:
    """Sonoff's firmware as a readout kept it, beside the device's backup."""
    kept = images.backup_copy(mac)
    return Stock(*kept) if kept else None


def put_back(ctx: Context, host: str) -> str:
    """A device a readout stopped part way left on the readout firmware, at
    `host`: its Sonoff firmware put back - as the readout kept it with the
    backup, else the file eWeLink offers it - and what the run ends by
    saying. Abort if neither is to be had."""
    from tools.autoflash import account, readout
    from tools.ewelink import offered_firmware
    output.say("The device is on the readout firmware, left by a readout stopped part way: "
               "putting its Sonoff firmware back.")
    mac = ctx.state.input("device_mac") or hostsys.mac_of(host)
    device_id = str(records.read_device(mac).get("ewelink_id") or "") if mac else ""
    if not device_id:
        device_id = str(readout.device_at(host, ctx.account_devices() or []).get("deviceid") or "")
    found = saved_stock(mac)
    if found is None and device_id:
        asked = account.ask_account(ctx, device_id)
        updates, problems = offered_firmware.fetch(asked.latest.get("offers") or [], asked.deviceid)
        for problem in problems:
            output.log_only(f"    Offered firmware: {problem}")
        found = stock_from_offered(updates)
    if found is None:
        raise Abort("the device is on the readout firmware, and its Sonoff firmware was found neither with a "
                    "backup on this computer nor in what eWeLink offers it")
    revert_to_sonoff(ctx, host, found)
    if device_id and wait_for_sonoff(ctx, device_id):
        return (f"Sonoff's firmware {found.version} is back on the device. To send in its readout, "
                "run the readout again.")
    return (f"Sonoff's firmware {found.version} was put back on the device, but eWeLink has not shown it online "
            "yet: switch it off and on.")
