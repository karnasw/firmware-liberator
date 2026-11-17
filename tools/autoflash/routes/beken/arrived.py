"""The device on its new firmware: started, on the Wi-Fi, and answering as what
was installed (K1 OpenBeken, J1 OpenBeken as the interim firmware, K2
ESPHome, K3 Sonoff's own firmware) - and, on OpenBeken, the device's own data
backed up (B4), which only OpenBeken can read out.

A device that has not come back on its new firmware is the person's to switch
off and on, then looked for again (arrival.until_back); an unattended run
stops there, saying to.
"""

from __future__ import annotations

import time
from typing import Any, TYPE_CHECKING

from tools.autoflash import detect, esphome, join, kinds, names, openbeken, output, records, runs
from tools.autoflash.errors import Abort
from tools.autoflash.routes import arrival
from tools.autoflash.routes.arrival import NotBack, NotTaken, until_back
from tools.autoflash.routes.beken import images, onward
from tools.beken import backup

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The bootloader applies the image and the new firmware joins the Wi-Fi within
# a minute or two: after three, whatever was under way has finished, and the
# person is asked to switch the device off and on (until_back).
ARRIVAL_WAIT = 180.0
# Tries at reading the device's own data before going on without a backup: the
# pause, then up to ARRIVAL_WAIT for the device to answer, lets a dropped network
# come back between them.
BACKUP_TRIES = 3
BACKUP_PAUSE = 10.0
# An OpenBeken's uptime set against the time since it fetched an image: this
# much shorter, and it started after the fetch, not before (step_esphome_check).
RESTART_SLACK = 10

def step_openbeken_check(ctx: Context, wait: float = ARRIVAL_WAIT, *, interim: bool = False) -> dict[str, Any]:
    """K1 - OpenBeken started from the image, and on the home Wi-Fi: it
    answers at the device's address (or wherever its hardware address is now)
    as the OpenBeken the tool installs on this kind of device - and, as its
    driver says, not as the interim firmware; J1 (`interim`) the
    same, as the interim firmware, which it is named to the person as.  One
    that says otherwise is told what it is: the device's own OpenBeken, then
    restarted, so the name it keeps follows; the interim firmware (the
    marker B2 sends after it cut off, say) not restarted, so it is never
    named wrongly on the network, only not renamed for its few minutes."""
    shown = images.INTERIM_NAME if interim else "OpenBeken"
    kind = kinds.of_run(ctx.state)
    started = time.monotonic()

    # Sent by Sonoff's firmware (B2): one still answering after a restart
    # did not take the image.
    sent_by = "B2" if ctx.state.is_done("B2") else None

    def check(host: str, restarted: bool) -> dict[str, Any] | None:
        found = openbeken.info(host, timeout=3.0)
        if found and openbeken.kind(host, found, timeout=3.0) is kind:
            answer = openbeken.is_interim(host, timeout=3.0)
            if answer is not None:
                return {**found, "interim": answer}
        if restarted and sent_by and detect.identify(host, timeout=3.0) == "sonoff":
            raise NotTaken
        return None

    def look(restarted: bool) -> dict[str, Any]:
        nonlocal started
        started = time.monotonic()
        try:
            return join.wait_for(ctx, lambda host: check(host, restarted), f"{shown} on your Wi-Fi", timeout=wait)
        except Abort as exc:
            raise NotBack from exc

    title = shown[0].upper() + shown[1:]
    found = until_back(ctx, look, title, (
        f"{title} has not answered on your Wi-Fi. The device installs it as it restarts, then reads its "
        "Wi-Fi from the device's own store and joins; if it has not, switch the device off and on, then run "
        "the same command again to check it"), sent_by=sent_by)
    seconds = round(time.monotonic() - started, 1)
    version = openbeken.version(found)
    settled = found["interim"] == interim
    if interim:
        # Not the device's name for good: the firmware chosen names it.
        output.say(f"    {title} {version} answered after {seconds}s.")
    if not settled:
        onward.set_interim(ctx, interim, (
            "It started without the marker sent after it (its update cut off at the very end): it is the "
            "interim firmware now, under the name it started with." if interim else
            "It had the interim firmware's name: given its own."), restart=not interim)
    if interim:
        return {"seconds": seconds, "version": version, **({} if settled else {"told": "interim"})}
    if not settled:
        found = openbeken.info(ctx.state.input("device_ip")) or {}
    name = str(found.get("shortName") or "")
    ctx.state.set_input("device_name", name)
    output.say(f"    OpenBeken {version} answered after {seconds}s, named {name!r}.")
    # Whether its first start carried the meter's factory calibration over, as
    # the report says: as K2 reads ESPHome's.
    meter: dict[str, Any] = {}
    if kind.has_meter:
        meter["calibration"] = openbeken.calibration_state(ctx.state.input("device_ip"))
        output.say(f"    Its meter calibration: {meter['calibration'] or 'not said'}.")
    return {"seconds": seconds, "version": version, "name": name, **({} if settled else {"told": "not interim"}),
            **meter}


def step_interim_check(ctx: Context, wait: float = ARRIVAL_WAIT) -> dict[str, Any]:
    """J1 - the interim firmware (OpenBeken, images.INTERIM) started and on
    the home Wi-Fi, as K1 - there only to back the device up, then fetch the
    firmware chosen - and saying it is the interim firmware (AutoflashInterim)."""
    return step_openbeken_check(ctx, wait, interim=True)


def step_backup(ctx: Context) -> dict[str, Any]:
    """B4 - the device's own data, read out of it through OpenBeken's
    /api/flash - the firmware chosen, or the interim one: the factory record
    with its MAC and radio calibration, the stock store with its Wi-Fi, meter
    calibration and eWeLink identity, and the system store - and, with them,
    Sonoff's firmware as the image that puts it back (from the public dump).
    Kept on this computer, never overwritten; the device keeps its own.
    """
    mac = ctx.state.input("device_mac")
    if not mac:
        return {"outcome": "not taken", "stage_outcome": "skipped"}
    outcome = _back_up(ctx, mac)
    ctx.state.set_input("backup_outcome", outcome)
    return {"outcome": outcome, "stage_outcome": records.BACKUP_OUTCOMES[outcome][0]}


def _read_region(host: str) -> bytes:
    """The device's own data, read through OpenBeken."""
    start, size = records.BEKEN_REGION

    def progress(done: int, total: int) -> None:
        output.progress(f"reading the device's own data: {100 * done // total}%")
    return openbeken.read_flash(host, start, size, progress=progress)


def _read_region_with_retries(ctx: Context, host: str) -> bytes:
    """_read_region, BACKUP_TRIES times before its OSError is let through."""
    for attempt in range(1, BACKUP_TRIES):
        ctx.state.capture("B4.attempts", attempt)
        try:
            return _read_region(host)
        except OSError as exc:
            output.progress_clear()
            output.say(f"    The device's own data could not be read ({exc}) - trying again in "
                       f"{BACKUP_PAUSE:.0f}s (attempt {attempt + 1} of {BACKUP_TRIES}).")
        time.sleep(BACKUP_PAUSE)
        started = time.monotonic()
        while (waited := time.monotonic() - started) < ARRIVAL_WAIT and not openbeken.info(host, timeout=3.0):
            output.progress(f"waiting for the device to answer... {waited:.0f}s")
            time.sleep(3)
    ctx.state.capture("B4.attempts", BACKUP_TRIES)
    return _read_region(host)


def note_flash(ctx: Context, matched: bool) -> None:
    """Whether the flash is laid out as the stock firmware's table says, for the
    next person to read in the log: the first the tool can tell, as the stock
    firmware does not say. On a smaller flash, reads this high would not find
    this device's factory record."""
    start = records.BEKEN_REGION[0]
    ctx.state.capture("B4.flash_matched", matched)
    output.log_only(f"    Flash: the device's own factory record was {'' if matched else 'not '}read at 0x{start:x}: "
                    f"it {'matches' if matched else 'does not match'} the table in its firmware")


def _back_up(ctx: Context, mac: str) -> str:
    """How the backup went, a records.BACKUP_OUTCOMES key: as the Espressif
    route's C3 (installer.backup_not_needed) asks, none wanted before one kept.
    Unlike C3, a failed read carries on without asking: C3's is the last before
    Sonoff's firmware is written over, while the device keeps what B4 reads.
    Only a run whose route has B4 (steps_for) reads it again: one that
    installs OpenBeken or starts from it, not one from ESPHome to Sonoff's
    firmware."""
    if getattr(getattr(ctx, "args", None), "no_backup", False):
        output.say("    No backup of the device's own data (--no-backup).")
        return "not wanted"
    existing = records.find_backup(mac)
    if existing:
        output.say(f"    The device's own data is backed up already, in {runs.display_path(existing.directory)}.")
        return "existing"
    kind = kinds.of_run(ctx.state)
    host = ctx.state.input("device_ip")
    try:
        region = _read_region_with_retries(ctx, host)
    except OSError as exc:
        output.progress_clear()
        # Not asked about, as an Espressif backup that fails is: a Beken device
        # keeps this data.
        output.tell(f"    The device's data could not be read ({exc}); going on without a backup. "
                    "The device keeps it.")
        output.say("    A later run that installs OpenBeken, or starts from it, tries the backup again.")
        return "failed"
    finally:
        output.progress_clear()
    try:
        found = backup.factory_mac(region)
    except ValueError as exc:
        output.tell(f"    What was read is not the device's factory record ({exc}); no backup kept.")
        note_flash(ctx, False)
        return "unreadable"
    if found.lower() != mac.lower():
        output.tell(f"    The data read names another device ({found}); no backup kept.")
        note_flash(ctx, False)
        return "unreadable"
    note_flash(ctx, True)
    try:
        stock, stock_version = images.stock_rbl(kind, ctx)
    except Abort as exc:
        output.tell(f"    {exc}: its data is backed up without Sonoff's firmware.")
        stock, stock_version = None, str(ctx.state.input("stock_version") or "")
    directory, saved = records.save_beken_backup(mac, region, stock, stock_version, kind=kind.name)
    if not saved:
        return "existing"
    beside = " and Sonoff's firmware" if stock is not None else ""
    output.say(f"    Backed up the device's own data ({len(region):,} bytes){beside} to "
               f"{directory.relative_to(records.CAPTURES.parent)}.")
    return "saved"


def step_esphome_check(ctx: Context, wait: float = ARRIVAL_WAIT) -> dict[str, Any]:
    """K2 - ESPHome started from the image, and on the home Wi-Fi, reading its
    calibration from the device's own store.

    OpenBeken fetches ESPHome (J2, or O1) and restarts by itself to install
    it: one answering that has started since - its uptime below what it was
    before the fetch, or than the time since - its bootloader did not take
    the image, and the run goes back to send it again (until_back), no
    restart to ask for.  Both, as the emulated device's clock runs fast: there
    its uptime outgrows the time since, but not on a real device.  And its
    uptime falling below the highest seen here, as the device that had just
    fetched it restarts: an image sent again soon after the last was refused
    goes from a device only just up, so its uptime before is small, and the
    emulated device's soon outgrows that too (seen in the fault suite: the
    second refusal in a row waited out, and a switch off and on asked for)."""
    started = time.monotonic()
    captures = ctx.state.data.get("captures", {})
    sent_by = next((step for step in ("J2", "O1")
                    if ctx.state.is_done(step) and isinstance(captures.get(f"{step}.fetched_at"), int)), None)

    highest: list[int] = []   # the highest uptime OpenBeken has answered with here
    has_relay = kinds.of_run(ctx.state).has_relay

    def restarted_since_sent(uptime: int) -> bool:
        before = captures.get(f"{sent_by}.uptime_before")
        return ((isinstance(before, int) and uptime < before)
                or (bool(highest) and uptime < highest[0])
                or uptime < time.time() - captures[f"{sent_by}.fetched_at"] - RESTART_SLACK)

    def check(host: str) -> bool:
        # Its relay answering too, where it has one: ESPHome's web server is up
        # before its switch is.
        if esphome.is_esphome(host, timeout=3.0) and (not has_relay or esphome.relay(host) is not None):
            return True
        if sent_by:
            uptime = (openbeken.info(host, timeout=3.0) or {}).get("uptime_s")
            if isinstance(uptime, int):
                if restarted_since_sent(uptime):
                    raise NotTaken
                highest[:] = [max([uptime, *highest])]
        return False

    def look(_restarted: bool) -> dict[str, Any]:
        nonlocal started
        started = time.monotonic()
        try:
            join.wait_for(ctx, check, "ESPHome on your Wi-Fi", timeout=wait, own_network=names.esphome_networks(ctx))
            return {}
        except Abort as exc:
            # As the Espressif route's I2: one that cannot join your Wi-Fi raises
            # its own network after a while, where it takes the details.
            try:
                if join.set_up_through_own_network(ctx, "ESPHome", esphome.is_esphome, names.esphome_networks(ctx)):
                    return {"set_up_through": "its own network"}
            except join.BackByItself:
                return {}
            raise NotBack from exc

    found = until_back(ctx, look, "ESPHome", (
        "ESPHome has not answered on your Wi-Fi, and its own network "
        f"({names.esphome_network_hint(ctx)}) is not to be seen either. The device installs it as it "
        "restarts, then reads its Wi-Fi from the device's own store and joins; if it has not, "
        "switch the device off and on, then run the same command again to check it"), sent_by=sent_by)
    seconds = round(time.monotonic() - started, 1)
    host = ctx.state.input("device_ip")
    if not kinds.of_run(ctx.state).has_meter:
        output.say(f"    ESPHome answered after {seconds}s.")
        return {"seconds": seconds, **found}
    calibration = esphome.calibration_state(host)
    output.say(f"    ESPHome answered after {seconds}s; its meter calibration: {calibration or 'not said'}.")
    return {"seconds": seconds, "calibration": calibration, **found}


def step_sonoff_check(ctx: Context, wait: float = arrival.SONOFF_WAIT) -> dict[str, Any]:
    """K3 - Sonoff's firmware started, on the Wi-Fi and back in eWeLink, as
    the Espressif route's R2 finds it (arrival.back_on_sonoff)."""
    return arrival.back_on_sonoff(ctx, leaving=("openbeken", "interim", "esphome"), wait=wait)
