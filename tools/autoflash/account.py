"""The device in its eWeLink account: finding one of a kind this tool converts on
this network, and the steps a run from Sonoff's firmware starts with - the
only part of a run that reaches eWeLink.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Callable, TYPE_CHECKING

from tools.ewelink import discover_ewelink, firmware_name
from tools.autoflash import (detect, esphome, hostsys, join, kinds, openbeken, output, records, prompts, runs,
                             setup_network)
from tools.autoflash.errors import Abort, NothingWritten
from tools.ewelink.query_ota import ota_endpoints

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def step_pair(ctx: Context) -> dict[str, Any]:
    """A1 - find the device: one of a kind this tool converts in the owner's
    eWeLink account, answering on this network, asking only when there is a
    choice to make."""
    ip = ctx.state.input("device_ip")
    if not ip:
        output.say("Signing in to eWeLink to see which supported devices are in the account...")
        ip = find_account_device(ctx, reached(ctx.account_devices()))
    ctx.state.set_input("device_ip", ip)
    account = getattr(ctx, "account_device", None)
    if account:
        ctx.state.set_input("device_kind", account.kind)
    mac = hostsys.mac_of(ip) or (account.mac if account else None)
    if mac and records.MAC_RE.match(mac):
        ctx.state.set_input("device_mac", mac)
        runs.file_device(ctx, mac, account)
    return {"device_ip": ip, "device_mac": mac}


@dataclass(frozen=True)
class AccountDevice:
    """A device in the owner's eWeLink account of a kind this tool converts:
    what the account says about it.  `kind`: its kinds.Kind's key."""
    device_id: str
    name: str
    model: str
    online: bool | None
    mac: str | None
    ssid: str | None
    kind: str


@dataclass(frozen=True)
class Sighting:
    """Where an account's device was seen on this network: `ip`, the address
    its hardware address answers at, or None where it was not seen."""
    device: AccountDevice
    ip: str | None


def keep_account_private(devices: list[dict[str, Any]]) -> None:
    """Keep what the account says about each device out of the log: its id,
    the name it was given, its hardware address and its network."""
    for device in devices:
        params = device.get("params") or {}
        output.private(device.get("deviceid"), "device-id")
        name = str(device.get("name") or "")
        generic = {"plug", "smart", "socket", *kinds.name_words(),
                   str(device.get("productModel") or "").lower()}
        if not set(name.lower().split()) <= generic:  # "Sonoff S60" names nobody
            output.private(name, "device-name")
        output.private(params.get("staMac"), "mac")
        output.private(params.get("ssid"), "wifi-name")


def account_device(device: dict[str, Any]) -> AccountDevice | None:
    """The account's record of a device, if it is of a kind this tool
    converts; its LAN key is left out."""
    product = str(device.get("productModel") or "").strip().upper()
    family = str((device.get("extra") or {}).get("model") or "")
    kind = kinds.for_account(product, family)
    if kind is None:
        return None
    params = device.get("params") or {}
    mac = params.get("staMac")
    online = device.get("online")
    return AccountDevice(
        device_id=str(device.get("deviceid") or ""),
        name=str(device.get("name") or "").strip() or kind.name,
        model=product or kind.short,
        online=online if isinstance(online, bool) else None,
        mac=hostsys.normalise_mac(str(mac)) if mac else None,
        ssid=str(params.get("ssid") or "") or None,
        kind=kind.key,
    )


def match_devices(devices: list[AccountDevice], neighbours: dict[str, str]) -> list[Sighting]:
    """Each account device against the network: `neighbours` is {address:
    hardware address}, and a device is where the hardware address its record
    gives answers - the account's own, so nothing else can be mistaken for it."""
    by_mac = {mac: address for address, mac in neighbours.items()}
    return [Sighting(device, by_mac.get(device.mac or "")) for device in devices]


def describe_device(device: AccountDevice) -> str:
    """Its name in the app, and its model unless that is the name already."""
    if device.name.upper() == device.model.upper():
        return f"'{device.name}'"
    return f"'{device.name}' ({device.model})"


def ready(sighting: Sighting) -> bool:
    """Found, and ready to convert from here: on this network and online in
    eWeLink, through which the conversion is sent."""
    return bool(sighting.ip) and sighting.device.online is True


def cut_off(sighting: Sighting) -> bool:
    """On this network while eWeLink shows it offline: the device cannot reach
    the internet, so the upgrade command, which only reaches it through
    eWeLink, would never arrive."""
    return bool(sighting.ip) and sighting.device.online is False



def why_not_found(sighting: Sighting) -> str:
    """What to do about an account device that is not on this network, or that
    is but cannot be converted from here."""
    device = sighting.device
    if cut_off(sighting):
        # The conversion goes through eWeLink; once converted it needs no cloud.
        return (f"{describe_device(device)} is at {sighting.ip} but is offline in eWeLink: "
                "let it reach the internet for this run. If it can already, switch it off and on.")
    if device.online is False:
        # After an update cut off part way it can stay away from eWeLink until it restarts.
        return (f"{describe_device(device)} is offline in eWeLink: check it is on, on your Wi-Fi and "
                "allowed online; then switch it off and on.")
    network = f" (it is on '{device.ssid}')" if device.ssid else ""
    return (f"{describe_device(device)} is online in eWeLink but not seen here{network}: join this "
            "computer to the same network.")


def sight_devices(devices: list[AccountDevice]) -> list[Sighting]:
    """Look for the account's devices on this network by the hardware address
    each one's record gives: where this computer last saw it, if it still
    answers there, else by a sweep of this computer's networks. Sonoff's
    firmware need answer nothing more - eWeLink's local discovery only with
    LAN Control on in the app."""
    macs = {device.mac for device in devices if device.mac}
    seen = {address: mac for address, mac in hostsys.neighbour_table().items()
            if mac in macs and hostsys.ping_ok(address)}
    if macs - set(seen.values()):
        try:
            near = list(hostsys.lan_networks()) or [hostsys.local_ip_towards("192.0.2.1")]
            seen.update({address: mac for address, mac in hostsys.sweep(near).items() if mac in macs})
        except OSError:
            pass  # no network at all: nothing seen
    return match_devices(devices, seen)


# A device that has just started shows offline in eWeLink for a while, answering
# here or not yet: how long to wait for it to catch up before asking.
CLOUD_CATCH_UP = 60.0
# A device that did not answer is looked for again this many times, this far
# apart, before the person is asked what now: one just plugged in, or a busy
# Wi-Fi, answers a moment later, and looking again is what they would choose.
LOOK_AGAIN = 2
LOOK_AGAIN_PAUSE = 15.0


def looking_again(looked: int) -> bool:
    """Whether to look again by itself, having looked `looked` times more;
    waits LOOK_AGAIN_PAUSE first, saying so."""
    if looked >= LOOK_AGAIN:
        return False
    output.say(f"    Not found yet - looking again in {LOOK_AGAIN_PAUSE:.0f}s.")
    output.progress("not found yet - looking again shortly...")
    time.sleep(LOOK_AGAIN_PAUSE)
    output.progress_clear()
    return True


def reached(devices: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The account's devices, as ctx.account_devices gives them; Abort where
    eWeLink could not be reached, as every step from Sonoff's firmware goes
    through it."""
    if devices is None:
        raise Abort("eWeLink could not be reached, and a device on Sonoff's firmware is found and converted "
                    "through it. Nothing was written: check this computer's internet connection, then run the "
                    "same command again")
    return devices


def find_account_device(ctx: Context, devices: list[dict[str, Any]]) -> str:
    """The address of the device to convert, from the account's on this network."""
    started = time.monotonic()
    looked = 0
    while True:
        supported = [device for device in map(account_device, devices) if device is not None]
        if not supported:
            output.transient_begin()
            others = [f"- '{str(d.get('name') or '').strip() or d.get('deviceid')}' "
                      f"({d.get('productModel') or 'unknown model'})" for d in devices]
            prompts.section(f"No supported device in the eWeLink account {ctx.email()}", [
                *(["It has:", *others] if others else ["It has no devices."]),
                "",
                "Add the device in the eWeLink app, or use the account it is in.",
            ])
            choice = prompts.ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the device's address myself",
                *(["Read out my device, to help add support for it"] if devices else []),
            ])
            output.transient_end()
            if choice == 3:
                from tools.autoflash import readout  # it builds on the routes, which import this
                readout.from_run(ctx, devices)
        else:
            output.say("Looking for it on your network...")
            sightings = sight_devices(supported)
            found = [sighting for sighting in sightings if ready(sighting)]
            if len(found) == 1:
                # The stage line completing says it all; the log keeps which device.
                output.say(f"    Found {describe_device(found[0].device)} at {found[0].ip}.")
                ctx.account_device = found[0].device
                return str(found[0].ip)
            if found:
                output.transient_begin()
                choice = prompts.ask_choice(
                    "More than one device in the account is on this network. Which one is this run for",
                    [f"{describe_device(sighting.device)} at {sighting.ip}" for sighting in found],
                )
                output.transient_end()
                ctx.account_device = found[choice].device
                return str(found[choice].ip)
            waited = time.monotonic() - started
            if all(sighting.device.online is False for sighting in sightings) and waited < CLOUD_CATCH_UP:
                # Offline in eWeLink: usually a device that has only just started -
                # plugged in as the run began - and is seconds from joining the
                # Wi-Fi and the cloud. Answering here already, or not yet.
                if all(map(cut_off, sightings)):
                    output.say(f"    It is on this network but eWeLink shows it offline; checking again ({waited:.0f}s)...")
                else:
                    output.say(f"    eWeLink shows it offline - just switched on, perhaps; checking again ({waited:.0f}s)...")
                output.progress(f"waiting for eWeLink to see the device online... {waited:.0f}s")
                time.sleep(10.0)
                output.progress_clear()
                devices = reached(ctx.account_devices())
                continue
            if looking_again(looked):
                looked += 1
                devices = reached(ctx.account_devices())
                continue
            output.transient_begin()
            if all(map(cut_off, sightings)):
                # Found here: only its own internet access is in the way.
                title, note = "Your device is on this network but cannot reach the internet", []
            else:
                title, note = ("Your device was not found on this network",
                               hostsys.security_software_note())
            prompts.section(title, [
                *(f"- {why_not_found(sighting)}" for sighting in sightings),
                *([""] + note if note else []),
            ])
            choice = prompts.ask_choice("What now", [
                "Look again",
                "Use a different eWeLink account",
                "Type the device's address myself",
            ])
            output.transient_end()
        if choice == 2:
            output.transient_begin()  # gone once answered, as the menu was
            ip = prompts.ask_ipv4(
                "The device's address",
                help_lines=["Its address is in your router's device list, or the eWeLink app's device info."],
                check=device_at({device.mac for device in supported if device.mac}),
            )
            output.transient_end()
            return ip
        if choice == 1:
            ctx.forget_account()
        devices = reached(ctx.account_devices())  # its online status may have changed


def device_at(macs: set[str]) -> Callable[[str], list[str]]:
    """For a typed address: what is wrong with it, unless the hardware address
    answering there is one of `macs`, those the account gives its devices. An
    address that only answers a ping - the router, say - would stop the run
    further on, with nothing to show what was wrong."""
    def check(address: str) -> list[str]:
        output.tell(f"    Checking that {address} is your device...")
        mac = hostsys.mac_of(address)
        if mac and hostsys.normalise_mac(mac) in macs:
            return []
        if mac:
            return [f"{address} is another device: its hardware address is not one eWeLink has for "
                    "this account's supported devices."]
        return [f"Nothing answered at {address}: check the device is switched on, and the address."]
    return check


def find_ewelink_devices(timeout: float = 6.0) -> list[tuple[str, str]]:
    """(address, name) for each eWeLink device answering mDNS on this network."""
    try:
        report = discover_ewelink.discover(timeout)
    except OSError:
        return []
    found: dict[str, str] = {}
    for packet in report.get("packets", []):
        source = packet.get("source")
        if not source or "records" not in packet:
            continue
        label = found.get(source, "an eWeLink device")
        for record in packet["records"]:
            if record.get("type") == "TXT":
                for value in record.get("value") or []:
                    if str(value).startswith("id="):
                        label = f"eWeLink device {str(value)[3:]}"
        found[source] = label
    return sorted(found.items())


class NotOnSonoff(NothingWritten):
    """The device answers, on another firmware than Sonoff's: `firmware`, as
    detect.NAMES has it. A readout says so as an answer, not a failure."""

    def __init__(self, firmware: str, message: str) -> None:
        super().__init__(message)
        self.firmware = firmware


def already_converting(device_ip: str) -> None:
    """Stop with the real reason when the device no longer runs Sonoff's firmware.

    eWeLink shows a device part way through a conversion, or converted, as
    offline, and eWeLink's record of Sonoff's firmware on it is out of date;
    asking eWeLink about it would send its owner the wrong way.
    """
    body = setup_network.page(device_ip, timeout=3.0)
    if body is None:
        return
    if setup_network.is_bridge_page(body):
        raise Abort(
            f"the device at {device_ip} is on this tool's installer from an earlier run: run this "
            "again and continue it, or switch the device off and on to go back to Sonoff"
        )
    if "tasmota" in body.lower():
        raise NotOnSonoff(
            "tasmota",
            f"{device_ip} is running Tasmota, not Sonoff's firmware: if it is part way through "
            "a conversion, run this again and continue it"
        )
    for firmware, answers in (("esphome", esphome.is_esphome), ("openbeken", openbeken.is_openbeken)):
        if answers(device_ip, 3.0):
            raise NotOnSonoff(
                firmware,
                f"{device_ip} is running {detect.NAMES[firmware]}, not Sonoff's firmware: it is "
                "converted already. Run this again, and the tool finds it on your network as a converted device")


def note_firmware_name(kind: kinds.Kind | None, latest: dict[str, Any], deviceid: str, product: str, model: str) -> str:
    """Log the firmware name the device's stock updater checks, if it can be
    learned, for a report of a device the tool does not know yet. It changes
    nothing in how a known kind converts, and a run goes on without it.
    `latest` is what query_ota saved of eWeLink's offer of the latest firmware."""
    learned, problems = firmware_name.learn(latest.get("offers") or [], deviceid, product, model)
    if latest.get("problem"):
        problems.insert(0, f"eWeLink's offer could not be read ({latest['problem']})")
    for problem in problems:
        output.log_only(f"    Firmware name: {problem}")
    if learned is None:
        output.log_only("    Firmware name: not learned")
        return ""
    output.log_only(f"    Firmware name: {learned.name}, from {learned.source}; "
                    f"its chip: {learned.chip or 'not named'}")
    if kind is not None and learned.name != kind.wrapper_model:
        # The run's report says so (report_data.firmware_name_info): nothing to ask of anyone.
        output.log_only(f"    Firmware name: {learned.source} says {learned.name}, but this tool wraps "
                    f"firmware for this device as {kind.wrapper_model}. The conversion goes on: if "
                    "the device turns the update down, its firmware stays as it is.")
    return learned.name


@dataclass(frozen=True)
class Asked:
    """What eWeLink says of the device: its firmware's version and model,
    eWeLink's product model, any update offered, and the latest firmware
    eWeLink would give it."""
    version: str
    model: str
    product: str
    endpoints: list[Any]
    latest: dict[str, Any]
    deviceid: str


def ask_account(ctx: Context, device_id: str, device_ip: str | None = None) -> Asked:
    """Ask eWeLink what a device is and runs, by its id in the account.
    Nothing is sent to the device. With its address, what answers there is
    checked first (already_converting): eWeLink's record of a device that has
    left Sonoff's firmware is out of date."""
    if device_ip:
        already_converting(device_ip)
    return _ask(ctx, "--device-id", device_id)


def _ask(ctx: Context, *query_args: str) -> Asked:
    """Run query_ota and read what it wrote."""
    meta = ctx.dir / "ota-metadata.json"
    if hostsys.run_tool(hostsys.python_script("tools/ewelink/query_ota.py", *query_args,
                                      "--email", ctx.email(), "--region", ctx.ewelink_region(),
                                      "--output", str(meta), "--latest-offer"),
                interactive=True, env=ctx.helper_env()) != 0:
        reasons = output.tool_problems(1)
        raise Abort("the eWeLink query failed" + (f": {reasons[0]}" if reasons else ""))
    try:
        metadata = json.loads(meta.read_text(encoding="utf-8"))
        version = str(metadata["query"]["version"])
        model = str(metadata["query"].get("model", ""))
        product = str(metadata.get("product") or "")
        endpoints = ota_endpoints(metadata["response"]["data"]["otaInfoList"])
        latest = metadata.get("latest_offer") or {}
        deviceid = str(metadata["query"].get("deviceid", ""))
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise Abort(f"could not read the OTA metadata just written: {exc}") from exc
    finally:
        # Its download links may work as access tokens; what is needed is now read.
        meta.unlink(missing_ok=True)
    return Asked(version, model, product, endpoints, latest, deviceid)


def device_id(ctx: Context, mac: str | None = None) -> str:
    """The device's eWeLink id: the one A1 found or this computer keeps for
    it, else by its Wi-Fi MAC (`mac`, else the run's) in the account's
    records. "" where none is found."""
    found = getattr(ctx, "account_device", None)
    if found is not None and found.device_id:
        return found.device_id
    mac = hostsys.normalise_mac(str(mac or ctx.state.input("device_mac") or ""))
    kept = str(records.read_device(mac).get("ewelink_id") or "") if mac else ""
    if kept or not mac:
        return kept
    for device in ctx.account_devices() or []:
        sta_mac = (device.get("params") or {}).get("staMac")
        if sta_mac and hostsys.normalise_mac(str(sta_mac)) == mac:
            return str(device.get("deviceid") or "")
    return ""


def known_device_id(ctx: Context) -> str:
    """device_id, which a step that reaches the device through eWeLink needs:
    Abort where none is found."""
    found_id = device_id(ctx)
    if not found_id:
        raise Abort("the device's eWeLink device id is not known: start again with --new, so the device is found "
                    "in the account first")
    return found_id


def account_record(ctx: Context) -> dict[str, Any]:
    """The device's record in the owner's eWeLink account, found by its id
    (device_id: A1's, this computer's, or by its Wi-Fi MAC there)."""
    found_id = known_device_id(ctx)
    devices = ctx.account_devices()
    if devices is None:
        raise Abort("eWeLink could not be reached, and the device's firmware can only be sent through it")
    for device in devices:
        if device.get("deviceid") == found_id:
            return device
    raise Abort(f"the device is no longer in the eWeLink account {ctx.email()}; add it again in the app")


def account_signal(device: dict[str, Any]) -> int | None:
    """The Wi-Fi signal the device last reported to eWeLink (its rssi), in dBm."""
    rssi = (device.get("params") or {}).get("rssi")
    return rssi if isinstance(rssi, int) and not isinstance(rssi, bool) and -120 < rssi < 0 else None


def step_discover(ctx: Context) -> dict[str, Any]:
    """A2/A3/A4 - identify the device and read its OTA metadata, through
    eWeLink: Sonoff's firmware need answer nothing on the network."""
    device_ip = ctx.state.require("device_ip")[0]
    record = account_record(ctx)
    asked = ask_account(ctx, str(record.get("deviceid") or ""), device_ip)
    version, model, product = asked.version, asked.model, asked.product
    endpoints = asked.endpoints
    kind = kinds.for_account(product, model)
    name = note_firmware_name(kind, asked.latest, asked.deviceid, product, model)
    if kind is None:
        raise Abort(
            f"{device_ip} is a {model or 'device of unknown model'}, which this tool does not "
            "convert yet. Nothing was written"
        )
    ctx.state.set_input("device_kind", kind.key)

    # Our upgrade command names this computer as the download address and
    # carries our firmware's hash, so the device fetches our image directly
    # whether or not Sonoff is also offering it one.  We never redirect
    # Sonoff's own download, so no router rule is involved in any case.
    #
    # Earlier versions added a router DNAT rule whenever an update was on
    # offer, as a guard against the device fetching Sonoff's genuine update in
    # the same window.  That guard was dropped; the "Getting in" note in
    # docs/what-happens.md and the DNAT recipe in
    # archive/docs/reference/ota-findings.md record how to reinstate it.
    if endpoints:
        offered = f"{endpoints[0][0]} port {endpoints[0][1]} (not used - we deliver directly)"
    else:
        offered = "none - this is the latest Sonoff firmware"
    prompts.section("What the device reported", quiet=True, body=[
        f"Model            {model}",
        f"Product          {product or 'not given'}",
        f"Firmware now     {version}",
        f"Update offered   {offered}",
        "",
        "The device fetches its new firmware straight from this computer, so no",
        "router rule is needed.",
    ])
    # The earliest reading of a run from Sonoff's firmware, as it last told
    # eWeLink: before anything is written, while moving the device costs nothing.
    signal = join.note_signal(ctx, account_signal(record), [
        "Nothing has been written yet. The conversion restarts the device",
        "several times, and each time it has to rejoin your Wi-Fi on its own;",
        "a weak signal makes that less certain."])
    ctx.state.set_input("stock_version", version)
    ctx.state.set_input("product_model", product)
    return {"stock_version": version, "update_offered": bool(endpoints), "model": model,
            "signal_dbm": signal, "firmware_name": name}
