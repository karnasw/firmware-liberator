#!/usr/bin/env python3
"""Read a stock device's whole flash over Wi-Fi, then put it back to stock.

This is not the conversion. It installs the conversion's bridge into the device's
spare app slot, only reads the device's whole memory through it, and saves that
under captures/flash-readout/. Switching the device off and on afterwards
boots its Sonoff firmware again, as it was.

What it costs: the spare slot is overwritten, so whatever was there - often
the Sonoff version the device had before its last update - is gone. The Sonoff
firmware it is running, its settings and its pairing are kept.

    python3 tools/espressif/flash_readout.py
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import hostsys, kinds, output, safety_locks  # noqa: E402
from tools.autoflash.output import PROBLEM_RE  # noqa: E402
from tools.autoflash.prompts import (  # noqa: E402
    QuitRun,
    ask_choice,
    ask_secret,
    ask_text,
    find_ssid,
    section,
    warning_box,
)
from tools.ewelink.discover_ewelink import discover  # noqa: E402
from tools.espressif import chips  # noqa: E402
from tools.espressif.dump_stock_flash import is_bridge, save_dump  # noqa: E402
from tools.autoflash.account import find_ewelink_devices  # noqa: E402
from tools.autoflash.firewall import DELIVERY_PORTS  # noqa: E402
from tools.autoflash.names import (  # noqa: E402
    LEGACY_BRIDGE_PASSWORD,
    LEGACY_BRIDGE_PREFIX,
    LEGACY_SETUP_PREFIX,
    SETUP_SSID_PREFIX,
)
from tools.autoflash.runs import SETTINGS_PATH  # noqa: E402

# The conversion's own bridge, the one it sends, as firmware.json pins it.
READOUT_OTA = Path(chips.ESP32C3.pin("bridge")["file"])
READOUT_SHA256 = chips.ESP32C3.pin("bridge")["sha256"]
CONFIRM_WORD = "readout"
BRIDGE_AP_HOST = "192.168.4.1"
STAGES = 6
STOCK_LAN_PORT = 8081


class Stop(RuntimeError):
    pass


def stage(number: int, title: str) -> None:
    print(f"\n[{number}/{STAGES}] {title}")


def check_bridge_file(path: Path) -> None:
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise Stop(f"cannot read {path}: {exc}") from exc
    if digest != READOUT_SHA256:
        raise Stop(f"{path} does not match its pinned SHA-256")


def choose_device(device_ip: str | None) -> str:
    if device_ip:
        return device_ip
    while True:
        print("Looking for Sonoff devices on your network...")
        devices = find_ewelink_devices()
        if len(devices) == 1:
            print(f"Found {devices[0][1]} at {devices[0][0]}.")
            return devices[0][0]
        if devices:
            index = ask_choice("Which device should be read", [f"{name} at {ip}" for ip, name in devices])
            return devices[index][0]
        if ask_choice("No Sonoff device answered. Check it is on, with LAN Control on in eWeLink",
                      ["Look again"]) == 0:
            continue


def account_email(given: str | None) -> str:
    if given:
        return given
    try:
        last = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("ewelink_email")
    except (OSError, ValueError):
        last = None
    return ask_text("eWeLink account email", default=last)


def query_device(device_ip: str, email: str, env: dict[str, str], work: Path) -> tuple[str, kinds.Kind]:
    """The device's stock version and its kind, after making sure it is one
    this tool converts."""
    report = discover(8.0, None, device_ip)
    if not report["packets"]:
        raise Stop(f"the device at {device_ip} did not answer; check LAN Control is on in eWeLink")
    mdns = work / "mdns.json"
    mdns.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    meta = work / "ota-metadata.json"
    try:
        completed = subprocess.run(
            hostsys.python_script("tools/ewelink/query_ota.py", "--mdns-capture", str(mdns),
                                  "--email", email, "--output", str(meta)),
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        if completed.returncode != 0:
            last = (completed.stderr or completed.stdout).strip().splitlines()[-1:]
            raise Stop("the eWeLink query failed" + (f": {last[0]}" if last else ""))
        query = json.loads(meta.read_text(encoding="utf-8"))["query"]
    finally:
        # Its download links may work as access tokens.
        meta.unlink(missing_ok=True)
    model, version = str(query.get("model", "")), str(query["version"])
    kind = kinds.for_stock_model(model)
    if kind is None:
        raise Stop(f"{device_ip} is a {model or 'device of unknown model'}, not one this tool converts")
    return version, kind


def confirmed(version: str, control: bool = False) -> bool:
    if control:
        opening = [
            "This replaces the device's spare app slot with a conversion bridge, only",
            "to see whether it starts. Nothing is read, and nothing is installed",
            "after it. The",
        ]
    else:
        opening = ["This replaces the device's spare app slot with the bridge, only to read from. The"]
    warning_box("Before going on", [
        *opening,
        f"Sonoff {version} it is running, its settings and its eWeLink pairing stay;",
        "whatever is in the spare slot (often an older Sonoff version) is lost.",
        "Until you switch the device off and on at the end, eWeLink cannot control it.",
        "As with every first install on a stock device, there is no automatic",
        "rollback if the new firmware failed to start at all.",
    ])
    question = f"Type {CONFIRM_WORD.upper()} to go ahead - anything else stops, changing nothing: "
    return input(question).strip().lower() == CONFIRM_WORD


def deliver(device_ip: str, listen_ip: str, port: int, email: str, version: str,
            work: Path, env: dict[str, str], firmware: Path = READOUT_OTA,
            verbose: bool = False) -> int:
    """Run the reviewed sender with the recovery lock lifted only for it.

    Its output is kept in the run's sender.log. On screen, the per-request
    HTTP lines become one progress line and the cloud's running commentary is
    left out, unless in verbose mode; anything reporting a problem is shown.
    """
    lock = ROOT / safety_locks.DIR / "RECOVERY_LOCK"
    moved = ROOT / safety_locks.DIR / "RECOVERY_LOCK.owner-authorized"
    hostsys.replace_file(lock, moved)
    try:
        process = subprocess.Popen(
            hostsys.python_script(
                "tools/espressif/serve_tasmota_ota.py",
                "--listen-ip", listen_ip, "--listen-port", str(port),
                "--device-ip", device_ip, "--firmware", str(firmware),
                "--email", email, "--expected-current-version", version,
                "--mdns-capture", str(work / "mdns.json"),
                "--i-understand-stock-has-no-automatic-rollback", unbuffered=True),
            cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
        total = (ROOT / firmware).stat().st_size if (ROOT / firmware).is_file() else 0
        progress = SenderProgress(total)
        with open(work / "sender.log", "w", encoding="utf-8") as log:
            for line in process.stdout:
                log.write(output.scrub(line))  # kept shareable, as the run's log is
                if verbose:
                    print(line, end="")
                    continue
                kind = sender_line_kind(line)
                if kind == "served":
                    progress.update(line)
                elif kind == "show":
                    progress.finish()
                    print(line, end="")
        progress.finish()
        return process.wait()
    finally:
        hostsys.replace_file(moved, lock)


SERVED_RE = re.compile(r"Served /\S+ bytes ([\d,]+)-([\d,]+)")
REQUEST_RE = re.compile(r'^\s*\[[\d.]+\] "GET [^"]*" (\d{3})')


def is_cloud_chatter(line: str) -> bool:
    """A sender line about routine cloud messages, rather than a problem."""
    return line.lstrip().startswith("Cloud:") and not PROBLEM_RE.search(line)


def sender_line_kind(line: str) -> str:
    """"served" for a delivered range, "hide" for routine chatter, else "show"."""
    if SERVED_RE.search(line):
        return "served"
    request = REQUEST_RE.match(line)
    if request:
        return "hide" if request.group(1).startswith("2") else "show"
    return "hide" if is_cloud_chatter(line) else "show"


class SenderProgress:
    """One line, redrawn in place, for how far the device has got."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.reached = 0
        self.drawn = False

    def update(self, line: str) -> None:
        match = SERVED_RE.search(line)
        self.reached = max(self.reached, int(match.group(2).replace(",", "")) + 1)
        done = (f"{100 * min(self.reached, self.total) // self.total}%" if self.total
                else f"{self.reached:,} bytes")
        sys.stdout.write(f"\r  The device is downloading: {done}   ")
        sys.stdout.flush()
        self.drawn = True

    def finish(self) -> None:
        if self.drawn:
            sys.stdout.write("\n")
            sys.stdout.flush()
            self.drawn = False


def stock_answers(device_ip: str) -> bool:
    """True when the Sonoff firmware's LAN service is listening at `device_ip`."""
    try:
        with socket.create_connection((device_ip, STOCK_LAN_PORT), timeout=3):
            return True
    except OSError:
        return False


def find_bridge(device_ip: str, mac: str | None, *, timeout: float = 120.0,
                probe: Callable[[str], bool] = is_bridge,
                by_mac: Callable[[str, str], str | None] = hostsys.find_by_mac,
                clock: Callable[[], float] = time.monotonic,
                sleep: Callable[[float], None] = time.sleep) -> str | None:
    """The bridge's address on the home network, if it joined it.

    It joins with the device's own hardware address, so it is usually where the
    device was; if the router moved it, it is found by that address.
    """
    started = clock()
    while clock() - started < timeout:
        if probe(device_ip):
            return device_ip
        moved = by_mac(mac, device_ip) if mac else None
        if moved and moved != device_ip and probe(moved):
            return moved
        sleep(3.0)
    return None


def wait_for_stock(device_ip: str, timeout: float = 180.0) -> bool:
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        if any(ip == device_ip for ip, _ in find_ewelink_devices(timeout=5.0)):
            return True
    return False


def run(args: argparse.Namespace) -> int:
    print(__doc__.split("\n\n")[1])
    print("Nothing on the device changes until you type READOUT.")

    stage(1, "Checking this computer")
    firmware, probe, name = READOUT_OTA, is_bridge, "the bridge"
    check_bridge_file(ROOT / firmware)
    if not (ROOT / safety_locks.DIR / "RECOVERY_LOCK").exists():
        raise Stop("RECOVERY_LOCK is not in place; refusing to start from an unsafe state")
    print(f"{name[0].upper()}{name[1:]} and safety lock: OK")

    stage(2, "Finding the device")
    device_ip = choose_device(args.device_ip)
    mac = hostsys.mac_of(device_ip)
    listen_ip = args.listen_ip or hostsys.local_ip_towards(device_ip)
    port = hostsys.first_free_port(listen_ip, DELIVERY_PORTS)
    if port is None:
        raise Stop(f"ports {DELIVERY_PORTS[0]} and {DELIVERY_PORTS[1]}-{DELIVERY_PORTS[-1]} are all in use")

    stage(3, "Asking the device what it is running")
    email = account_email(args.email)
    env = {**os.environ, "EWELINK_PASSWORD": ask_secret(f"eWeLink password for {email}: ")}
    work = ROOT / "captures" / "flash-readout" / f"run-{time.strftime('%Y%m%d-%H%M%S')}"
    work.mkdir(parents=True, exist_ok=True)
    version, kind = query_device(device_ip, email, env, work)
    print(f"{kind.name} at {device_ip}{f' ({mac})' if mac else ''}, running Sonoff {version}.")
    if not confirmed(version, args.control):
        print("Stopped. Nothing was sent to the device.")
        return 1

    rule = None
    try:
        stage(4, f"Installing {name}")
        if args.verbose or hostsys.local_rules_ignored(device_ip):
            for line in hostsys.firewall_advice(device_ip, [port]):
                print(line)
        if hostsys.active_firewall():
            print("Adding a temporary firewall rule so the device can fetch the firmware;")
            print("Windows will ask for permission - choose Yes." if hostsys.IS_WINDOWS
                  else "you may be asked for this computer's password.")
            try:
                rule = hostsys.open_firewall(device_ip, [port])
            except OSError as exc:
                print(f"Could not add it ({exc}); carrying on.")
            if rule is not None and rule.detail.get("paused") and args.verbose:
                print("Python had been blocked from networks here (a \"Cancel\" in Windows")
                print("Security, most likely): paused while this runs, put back afterwards.")
        code = deliver(device_ip, listen_ip, port, email, version, work, env, firmware, args.verbose)
        env.pop("EWELINK_PASSWORD", None)
        if code == 2:
            raise Stop("the sender refused before contacting the device; nothing was sent")

        stage(5, f"Checking {name} started" if args.control else "Reading the device's memory")
        print(f"Waiting for {name} to start and join your Wi-Fi...")
        host = find_bridge(device_ip, mac, probe=probe)
        while host is None:
            # Any device's: its MAC is not known for certain here.  Also by the
            # names of a readout build from before 4.4.4 (--firmware).
            network = find_ssid((SETUP_SSID_PREFIX, LEGACY_SETUP_PREFIX, LEGACY_BRIDGE_PREFIX))
            if network is None and stock_answers(device_ip):
                raise Stop(
                    f"the device restarted on its Sonoff firmware, so {name} is not running"
                    f"{'' if args.control else ' and nothing was read'}. The device is working as "
                    "before (only its spare slot changed); there is no need to switch it off and "
                    f"on. The sender's output is in {(work / 'sender.log').relative_to(ROOT)}")
            if network is not None and args.control:
                host = f"its own network, {network}"
                break
            if network is None:
                section(f"Neither {name} nor the Sonoff firmware is answering", [
                    "The device may still be starting up.",
                ])
                ask_choice("Type 1 to look again", ["Look again"])
                host = find_bridge(device_ip, mac, timeout=30.0, probe=probe)
                continue
            section("The bridge did not join your Wi-Fi, but its own network is up", [
                f"Connect this computer to the Wi-Fi network {network}",
                (f"(password {LEGACY_BRIDGE_PASSWORD}), then come back here."
                 if network.startswith(LEGACY_BRIDGE_PREFIX) else "(open), then come back here."),
            ])
            ask_choice("Type 1 once this computer is on that network",
                       [f"I've joined {network} - read the device"])
            host = BRIDGE_AP_HOST if is_bridge(BRIDGE_AP_HOST) else None
            if host is None:
                print(f"Nothing answers as the bridge at {BRIDGE_AP_HOST} yet.")
        if args.control:
            print(f"{name[0].upper()}{name[1:]} started on this device ({host}): the Sonoff updater")
            print("accepted it and it runs.")
        elif save_dump(host, args.label or f"stock-{version}", version) != 0:
            raise Stop("the dump was not complete or not well formed. The bridge is "
                       f"still running: retry with python3 tools/espressif/dump_stock_flash.py --host {host}")
    finally:
        if rule is not None and not hostsys.close_firewall(rule):
            print("Remove the temporary firewall rule yourself:")
            for line in hostsys.undo_commands(rule):
                print("  " + line)

    stage(6, "Putting the device back to stock")
    print("Switch the device off at the wall, wait five seconds, and switch it on again.")
    ask_choice("Type 1 once the device is switched back on", ["It's switched back on"])
    print("Waiting for the Sonoff firmware to answer...")
    if wait_for_stock(device_ip):
        print(f"The device is answering as an eWeLink device at {device_ip} again: back on stock.")
    else:
        print(f"Nothing has answered at {device_ip} yet. Check the eWeLink app; if the device")
        print("is missing, switch it off and on once more.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--device-ip", help="the device's address, instead of looking for it")
    parser.add_argument("--email", help="the eWeLink account the device is in")
    parser.add_argument("--listen-ip", help="this computer's address as the device sees it")
    parser.add_argument("--label", help="name for the saved dump (default: stock-<version>)")
    parser.add_argument("--control", action="store_true",
                        help="only see whether the bridge starts on this device: nothing is read")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="also show the cloud's messages while the firmware is delivered")
    args = parser.parse_args(argv)
    try:
        return run(args)
    except Stop as exc:
        print(f"\nStopped: {exc}.", file=sys.stderr)
        return 1
    except (QuitRun, KeyboardInterrupt, EOFError):
        print("\nStopped.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
