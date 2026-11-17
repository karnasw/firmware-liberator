"""Prove the router redirects the device's update download, and nothing else.

The rule is specific to the device's address, so the only way to test it from
this computer is to borrow that address for a few seconds while the device is
unplugged. Two connections are made to the vendor's update server:

  1. from the device's address - must NOT reach the vendor (it is redirected to
     this computer, where nothing is listening yet, so it is refused);
  2. from this computer's own address - must still reach the vendor.

The same test as tools/espressif/check_ota_firewall.sh, made to run on Linux, macOS and
Windows. Adding and removing the borrowed address needs administrator rights,
asked for through the system's own prompt.
"""

from __future__ import annotations

import socket
from typing import Callable

from . import hostsys


class RedirectTestError(RuntimeError):
    pass


def reaches(source: str, target: str, port: int, timeout: float = 6.0) -> bool:
    """True when a TCP connection from `source` to `target:port` completes."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(timeout)
    try:
        probe.bind((source, 0))
        probe.connect((target, port))
        return True
    except OSError:
        return False
    finally:
        probe.close()


def run(
    *,
    interface: str,
    device_ip: str,
    own_ip: str,
    vendor_ip: str,
    vendor_port: int,
    log: Callable[[str], None] = print,
    elevated: Callable[[list[str]], int] = hostsys.elevated,
    connect: Callable[[str, str, int], bool] = reaches,
) -> None:
    """Raise RedirectTestError unless both checks pass.  Always gives the address back."""
    if device_ip in hostsys.local_addresses():
        raise RedirectTestError(f"{device_ip} is already one of this computer's addresses")
    if hostsys.ping_ok(device_ip):
        raise RedirectTestError(
            f"something still answers at {device_ip} - switch the device off first, or two "
            "devices would share one address"
        )
    add, remove = hostsys.alias_commands(interface, device_ip)
    if elevated(add) != 0:
        raise RedirectTestError(f"could not borrow {device_ip}: {hostsys.display(add)}")
    try:
        log("Test 1 of 2: a connection from the device's address is redirected")
        if connect(device_ip, vendor_ip, vendor_port):
            raise RedirectTestError(
                "a connection from the device's address reached Sonoff's server, so "
                "the router rule is not catching it"
            )
        log("  PASS - it did not reach Sonoff's server")
        log("Test 2 of 2: this computer's own connections are left alone")
        if not connect(own_ip, vendor_ip, vendor_port):
            raise RedirectTestError(
                "this computer could not reach Sonoff's server either, so the test "
                "cannot tell the rule apart from a network problem"
            )
        log("  PASS - this computer still reaches Sonoff's server")
    finally:
        if elevated(remove) == 0:
            log(f"Gave {device_ip} back.")
        else:
            log(f"Could not give {device_ip} back - run: {hostsys.display(remove)}")
