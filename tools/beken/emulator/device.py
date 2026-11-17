#!/usr/bin/env python3
"""The emulated Beken device: on until switched off, through every firmware it
is converted to and back, as beken-emu.sh device runs it (device-net.sh).

    python device.py --state DIR [--tap tap0] [--own-tap tap1]

Its flash is DIR/flash.bin: the public dump the first time, with the home
Wi-Fi's details added to its store, as a device provisioned in the app has
them; from then on what the device left there. Each boot starts from the
bootloader, which applies an image waiting in the download slot as a real
device's does, and every restart the firmware asks for is a boot of its own,
from the flash as it was left.

Sonoff's firmware needs the emulator's help to come online
(cloud_online.StockHooks), at places found in the app that starts
(stock_points.py): an app they cannot be found in is another firmware, which
is left alone.

Its board (board.py) gives it a power meter, which measures a load while
the relay is on.

DEVICE_TRACE=ADDR,ADDR (hex) logs each call there to the serial output.

The power switch is DIR/power: "off" stops the device and keeps its flash,
"on" starts it again (beken-emu.sh power). The home Wi-Fi's access point is
switched by DIR/router, read as the device runs (beken-emu.sh router): "off"
takes it off the air, "on" brings it back, "on PASSWORD" with another
password, as a router given a new one - a device on the old one cannot join.

With --own-tap, a station is on the air too, joining the network the device
raises of its own (ESPHome's when it cannot join the home one), its traffic
on that TAP device: the computer's Wi-Fi there (computer/bin/wifi). What the
station sees is written to DIR/air/: `network`, the name of the device's own
network while it is on the air, and `station`, the MAC the computer takes
there; and `home`, the home Wi-Fi's name while its access point is on. Each
time the computer joins that network or goes home it writes DIR/joins (`wifi
join`, `wifi home`): the station joins it only while the computer is on it,
afresh at each join, as a laptop does when its network is chosen (one it had
joined earlier may have been raised again since, forgetting it), and leaves
it when the computer goes home. It hears it all the while, as a laptop lists
a network it is not on; a device has a client on its network only while one
has joined it. The
serial output is DIR/serial.log, and what the emulator's Wi-Fi and network
did - each peer joining, a lease, a frame refused - DIR/net.log.
"""
from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]   # the emulator's modules, and the Beken tools
import board  # noqa: E402
import cloud_online  # noqa: E402  (needs the emulator image)
import stage_apply  # noqa: E402
import stock_points  # noqa: E402
from src.main import build_emulator  # noqa: E402
from unicorn import UC_HOOK_CODE  # noqa: E402
from unicorn.arm_const import UC_ARM_REG_LR, UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3  # noqa: E402

SSID, PASSWORD = cloud_online.SSID, cloud_online.PW
APP_ENTRY = stock_points.APP_BASE
KIND = "sonoff-s61"   # whose board it is: the dump's
PEER_MAC = b"\x02\x00\x00\x00\x00\x01"
# The computer's Wi-Fi on the device's own network: the station's MAC, which the
# computer's end of that network takes (DIR/air/station).
STATION_MAC = b"\x02\x00\x00\x00\x00\x02"


def trace(mu, address: int) -> None:
    """Log each call at `address` (DEVICE_TRACE): its first four arguments - as
    strings where they point at one - and where it returns to."""
    def text(value: int) -> str:
        try:
            raw = bytes(mu.mem_read(value, 32)).split(b"\0")[0]
        except Exception:
            return "%#x" % value
        return repr(raw.decode()) if raw and all(32 <= c < 127 for c in raw) else "%#x" % value

    def called(uc, at, size, user):
        args = [text(uc.reg_read(r)) for r in (UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3)]
        print("[trace] %#x(%s) from %#x" % (at, ", ".join(args), uc.reg_read(UC_ARM_REG_LR)), flush=True)
    mu.hook_add(UC_HOOK_CODE, called, None, address, address)


class NetLog:
    """The emulator's Wi-Fi and network lines, to a file as they come, each
    timed: kept in memory, a long run's grew without end."""

    def __init__(self, path: Path):
        self.file = open(path, "a", encoding="utf-8", buffering=1)

    def append(self, line: object) -> None:
        self.file.write("%s %s\n" % (time.strftime("%H:%M:%S"), line))


def write(path: Path, text: str) -> None:
    """`path` holding `text`, whole for any reader at any moment."""
    part = path.with_suffix(".part")
    part.write_text(text, encoding="utf-8")
    os.replace(part, path)


class Device:
    def __init__(self, state: Path, tap: str, own_tap: str | None = None):
        self.state, self.tap, self.own_tap = state, tap, own_tap
        self.flash_path = state / "flash.bin"
        self.power_path = state / "power"
        self.router_path = state / "router"
        self.air = state / "air"
        self.home = self.station = None   # the access point, and the station on the device's own network
        self.router_was: tuple[bool, str] | None = None
        self.joins_path = state / "joins"
        self.joins_was: str | None = None
        self.stopping = False
        self.netlog = NetLog(state / "net.log")

    def prepare(self) -> None:
        """The flash, the first time: the dump with the Wi-Fi in its store."""
        if self.flash_path.exists():
            return
        flash = bytearray(Path(cloud_online.DUMP).read_bytes())
        stage_apply.stage(flash, None, {"wifi-ssid": SSID, "wifi-pass": PASSWORD})
        part = self.flash_path.with_suffix(".part")
        part.write_bytes(flash)
        os.replace(part, self.flash_path)

    def powered(self) -> bool:
        try:
            return self.power_path.read_text(encoding="utf-8").strip() != "off"
        except OSError:
            return True

    def router(self) -> tuple[bool, str]:
        """Whether the home Wi-Fi's access point is on, and its password."""
        try:
            words = self.router_path.read_text(encoding="utf-8").split(maxsplit=1)
        except OSError:
            words = []
        on = not words or words[0] != "off"
        return on, (words[1].strip() if on and len(words) > 1 else PASSWORD.decode())

    def follow_router(self) -> None:
        """The access point as DIR/router has it: off the air, or on with its
        password. A new password restarts it, as a router's does: every
        device joined to it has to join again."""
        now = self.router()
        if now == self.router_was or self.home is None:
            return
        on, password = now
        if self.router_was is not None:
            print("[device] the home Wi-Fi is %s" % (("on, its password " + repr(password)) if on else "off"),
                  flush=True)
        if self.router_was is not None and on and password != self.router_was[1]:
            self.home.set_on_air(False)
        self.home.passphrase = password
        self.home.set_on_air(on)
        if on:
            write(self.air / "home", SSID.decode() + "\n")
        else:
            (self.air / "home").unlink(missing_ok=True)
        self.router_was = now

    def follow_air(self) -> None:
        """DIR/air/network: the device's own network's name while the station
        hears it, and gone when it does not; and the station on it while the
        computer is (DIR/joins), joined afresh each time the computer joins."""
        if self.station is None:
            return
        try:
            joins = self.joins_path.read_text(encoding="utf-8")
        except OSError:
            joins = ""
        if joins != self.joins_was:
            joining = joins.startswith("join")
            self.station.set_joining(joining)
            if joining and self.joins_was is not None:
                self.station.device_reset()
        self.joins_was = joins
        bss = self.station.bss
        network = self.air / "network"
        if bss is not None:
            write(network, bss["ssid"] + "\n")
        else:
            network.unlink(missing_ok=True)

    def save(self, emu) -> None:
        flash = bytes(emu.physical_flash if emu.physical_flash is not None else emu.raw_flash)
        part = self.flash_path.with_suffix(".part")
        part.write_bytes(flash)
        os.replace(part, self.flash_path)

    def stock_points(self, flash: bytes):
        """Where the help goes in the app `flash` holds, as the bootloader has
        just left it, or None if that is another firmware than Sonoff's."""
        try:
            return stock_points.find(stock_points.app_of_flash(flash))
        except stock_points.PointError as exc:
            self.netlog.append("[device] no help for this app: %s" % exc)
            return None

    def boot(self):
        emu = build_emulator(str(self.flash_path), chip="BK7238", with_boot=True, only_uart=True,
                             save_decrypted=False, wifi=True, log=self.netlog.append)
        emu.flash_writethrough = True
        emu.setup()
        board.attach(emu, KIND)
        emu.net_sink = self.netlog.append
        self.home = emu.attach_wifi_peer(role="ap", ssid=SSID.decode(), passphrase=PASSWORD.decode(), channel=1,
                                         peer_mac=PEER_MAC, tap=self.tap)
        self.router_was = None
        self.air.mkdir(exist_ok=True)
        self.follow_router()
        self.joins_was = None
        if self.own_tap:
            self.station = emu.attach_wifi_peer(role="sta", channel=1, peer_mac=STATION_MAC, tap=self.own_tap)
            write(self.air / "station", ":".join("%02x" % b for b in STATION_MAC) + "\n")
        ca = (self.state / "ca.crt").read_bytes()
        hooks = cloud_online.StockHooks(emu, SSID, PASSWORD, ca, cloud_online.FORCE_AT, self.netlog)
        hooks.active = False

        def app_starts(mu, address, size, user):
            points = self.stock_points(bytes(emu.physical_flash if emu.physical_flash is not None else emu.raw_flash))
            hooks.active = points is not None
            if hooks.active:
                hooks.attach(points)
                hooks.boot()
            print("[device] the app starts: %s" % ("Sonoff's firmware" if hooks.active else "another firmware"),
                  flush=True)
        emu.mu.hook_add(UC_HOOK_CODE, app_starts, None, APP_ENTRY, APP_ENTRY)
        for address in filter(None, os.environ.get("DEVICE_TRACE", "").split(",")):
            trace(emu.mu, int(address, 16))
        return emu

    def run_once(self) -> None:
        """One boot, until the firmware restarts, the power goes off or the
        container stops; the flash kept as it was left."""
        emu = self.boot()
        thread = threading.Thread(target=emu.run, daemon=True)
        thread.start()
        resets = emu.reset_count
        why = None
        while why is None:
            time.sleep(0.5)
            self.follow_router()
            self.follow_air()
            if self.stopping:
                why = "stopped"
            elif not self.powered():
                why = "switched off"
            elif emu.reset_count != resets:
                why = "restarted"
            elif not thread.is_alive():
                why = "halted"
        emu.stop()
        thread.join(10)
        self.save(emu)
        emu.close_network()
        self.home = self.station = None
        (self.air / "network").unlink(missing_ok=True)
        print("[device] %s; its flash is kept" % why, flush=True)
        if why == "halted":
            time.sleep(5)   # not round again at once, should it halt at every boot

    def run(self) -> None:
        self.prepare()
        while not self.stopping:
            if not self.powered():
                time.sleep(0.5)
                continue
            self.run_once()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--tap", default="tap0")
    parser.add_argument("--own-tap", help="a station on the device's own network, its traffic on this TAP device")
    parser.add_argument("--prepare", action="store_true", help="only make the flash, the first time")
    args = parser.parse_args(argv)
    device = Device(args.state, args.tap, args.own_tap)
    if args.prepare:
        device.prepare()
        return 0

    def stop(*_):
        device.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    device.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
