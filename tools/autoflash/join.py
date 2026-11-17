"""The device onto the Wi-Fi, and found there: waiting for it to come back on the
home network, giving it the Wi-Fi through its own network when it cannot,
and how good its signal is.
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import (
    device,
    hostsys,
    interrupt,
    names,
    output,
    prompts,
    setup_network,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort
from tools.espressif.safeboot.prepare_imprinted_bundle import ask_credentials, ask_password, bundle_ssid

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Below this the startup firmware may struggle to rejoin on its own after E3.
WEAK_SIGNAL_DBM = -75


class DeviceFinder:
    """Where the device is now: its known address, or wherever its hardware address is.

    The hardware (MAC) address of the device's Wi-Fi is the same under every
    firmware, so once recorded it finds the device after a restart onto a new
    address, so nobody has to reserve one in their router.
    """

    SWEEP_EVERY = 30.0

    def __init__(self, ctx: Context) -> None:
        self.ctx = ctx
        self.last_sweep = 0.0

    def poll(self) -> str | None:
        current = self.ctx.state.input("device_ip")
        mac = self.ctx.state.input("device_mac")
        if current and hostsys.ping_ok(current):
            seen = hostsys.neighbour_table().get(current)
            if not mac or seen in (None, mac):
                return current
        if mac and current and time.monotonic() - self.last_sweep > self.SWEEP_EVERY:
            self.last_sweep = time.monotonic()
            found = hostsys.find_by_mac(mac, current)
            if found:
                self.ctx.set_device_ip(found)
                return found
        return None


def locate_device(ctx: Context, *, timeout: float = 300.0, setup: bool = False) -> str:
    """Wait for the device on the home network; ask only if it cannot be found.

    With `setup`, the device may instead be waiting on its own setup network,
    which this computer could not see (not every system can scan): offered as
    a way on, it takes the person through joining it by hand.
    """
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        found = ctx.finder.poll()
        if found:
            output.progress_clear()
            output.say(f"    The device is on your network at {found}.")
            return found
        output.progress(f"looking for the device on your network... {time.monotonic() - started:.0f}s")
        time.sleep(5)
    output.progress_clear()
    other = None
    if setup:
        other = (f"It is showing its own Wi-Fi network ({names.setup_network_hint(ctx)})",
                 lambda: configure_device_wifi(ctx, None, NOT_JOINED))
    found = prompts.wait_or_ask(ctx.state.input("device_ip"), what="the device", timeout=60.0, other=other)
    ctx.set_device_ip(found)
    return found


# Waiting on something only a person can do - join a network, say - has no
# time limit: they may have walked away, and the run should still be there
# when they come back.  Ctrl-C stops it.  Nobody comes back to an unattended
# run, so that one gives up.
UNATTENDED_ACTION_LIMIT = 900.0


def user_action_limit() -> float:
    return UNATTENDED_ACTION_LIMIT if prompts.UNATTENDED else float("inf")


# While this computer waits to be joined to a device's own network, whether the
# device has come back on the home network by itself - its router back after
# an outage, which it raised its network during - is looked at this often.
BACK_LOOK_EVERY = 15.0


class BackByItself(Exception):
    """The device came back on the home network by itself while this computer
    waited to be joined to its own network: there is nothing to set up."""


def wait_until_joined(network: str, marker: str | None = None, back: Callable[[], Any] | None = None) -> None:
    """Block until this computer has joined the device's network.

    With `marker`, the page must mention it (the bridge).  Without, only the web
    port is touched: Tasmota scans for networks on every page request in setup
    mode, which drops this computer off its network.  With `back`, whether the
    device is on the home network again: once it is, BackByItself.
    """
    next_look = BACK_LOOK_EVERY

    def tick(elapsed: float) -> None:
        nonlocal next_look
        if back is not None and elapsed >= next_look:
            next_look = elapsed + BACK_LOOK_EVERY
            if back():
                output.progress_clear()
                raise BackByItself
        output.progress(f"waiting for this computer to join {network}... {elapsed:.0f}s")

    limit = user_action_limit()
    with interrupt.asking():  # waiting on the person: Ctrl-C stops at once
        joined = (setup_network.wait_for_page(marker, timeout=limit, tick=tick) if marker
                  else setup_network.wait_for_port(timeout=limit, tick=tick))
    if not joined:
        output.progress_clear()
        raise Abort(f"this computer did not join {network} within {limit / 60:.0f} minutes")
    output.progress_clear()
    output.event("joined", network=network)
    output.say(f"    Connected to {network}.")


# Why this computer was sent to a device's own network, as the report gives it
# for each stage (report_data.stages): the firmware raising it was never given
# the home Wi-Fi's details - the tool's own gap; was given them but did not
# join with them - the details, the hand-over or the home network; or lost its
# settings - a power cut, or switched off and on 7 times.
NOT_GIVEN, NOT_JOINED, SETTINGS_LOST = "not given", "not joined", "settings lost"
OWN_NETWORK_REASONS = (NOT_GIVEN, NOT_JOINED, SETTINGS_LOST)
# The run's record of each time, its reason, in turn: for the report
# (report.own_network_joined), and by step (runner.execute).
OWN_NETWORK = "own_networks"


def note_own_network(ctx: Context, why: str) -> None:
    """Recorded, for the report: this computer sent to a device's own network,
    and why (OWN_NETWORK_REASONS)."""
    joins = ctx.state.input(OWN_NETWORK)
    ctx.state.set_input(OWN_NETWORK, [*(joins if isinstance(joins, list) else []), why])


def forget_own_network(ctx: Context) -> None:
    """The last note_own_network taken back: this computer was never sent there."""
    joins = ctx.state.input(OWN_NETWORK)
    if isinstance(joins, list) and joins:
        ctx.state.set_input(OWN_NETWORK, joins[:-1])


def configure_device_wifi(ctx: Context, network: str | None, why: str) -> str:
    """Give Tasmota, waiting on its setup network, the bundle's Wi-Fi details;
    `why` it is there, for the report (OWN_NETWORK_REASONS).

    The operator joins the setup network; the details are sent from here.  Tasmota
    saves them only after it has connected with them, so a failure leaves the
    device waiting to be set up, never lost.
    """
    network = network or names.setup_network_hint(ctx)
    credentials = ctx.wifi(why=[
        "Tasmota is waiting on its own network for them; this sends them for you.",
    ])
    with output.until_done():   # the instructions, and the sending, go once it has the details
        output.event("join", network=network)
        prompts.section("Join the device's setup network", [
            f"In {hostsys.wifi_menu_hint()}, join:",
            f"    {network}      (open - no password)",
            "Close any setup page that opens: it can drop this computer off.",
            "It restarts every three minutes to retry your Wi-Fi; if dropped, rejoin.",
        ])
        # The page keeps the device scanning for networks, which can drop this
        # computer off; its network vanishes for a few seconds as it restarts.
        wait_until_joined(network)
        note_own_network(ctx, why)
        outcome = "manual"
        if credentials is not None:
            name_with_the_wifi(ctx)
            ssid, password = credentials
            for attempt in range(1, SETUP_SEND_ATTEMPTS + 1):
                output.tell(f"    Sending the details for '{ssid}'; stay on its network for about 20 s...")
                try:
                    outcome, address = setup_network.send_wifi(ssid, password)
                except setup_network.SetupError as exc:
                    outcome, address = "unknown", None
                    output.tell(f"    {exc}")
                if outcome == "saved" or attempt == SETUP_SEND_ATTEMPTS:
                    break
                if outcome == "rejected":
                    if prompts.UNATTENDED:
                        break  # nobody to type others: on to the device's page, below
                    output.tell(f"    Could not connect to '{ssid}' with those - type them again, name exactly:")
                    ssid, password = ask_credentials()
                elif setup_network_gone(ctx, network):
                    # No word, as it took them: tested, saved and restarted onto the
                    # Wi-Fi before its answer was asked for, its network gone with
                    # it (seen in the fault suite). Sent again, the run would wait
                    # for this computer to join a network that is no more.
                    outcome = "gone"
                    break
                else:
                    output.tell(f"    No word from the device; sending them again (attempt {attempt + 1} "
                         f"of {SETUP_SEND_ATTEMPTS}).")
                    time.sleep(5)
                wait_until_joined(network)  # dropped off it meanwhile, joined again
    if credentials is not None:
        if outcome == "saved":
            output.tell(f"    The device connected to '{ssid}' and is restarting onto it"
                + (f" at {address}." if address else "."))
            if address:
                ctx.set_device_ip(address)
        elif outcome == "rejected":
            output.tell(f"    The device could not connect to '{ssid}' with those details.")
        elif outcome == "gone":
            output.tell(f"    No word, but its network went: looking for it on '{ssid}'.")
        else:
            output.tell("    No word from the device. If it doesn't show on your Wi-Fi, run this again.")
    if outcome in ("rejected", "manual"):
        prompts.section("Set the Wi-Fi up on the device's page instead", [
            f"Staying on its network, browse to http://{setup_network.SETUP_HOST}, choose your Wi-Fi,",
            "type its password and save. When it says it connected:",
        ])
    output.event("home")
    prompts.section("Back to your usual Wi-Fi", [
        "Rejoin it now if this computer hasn't by itself.",
    ])
    return locate_device(ctx, timeout=600.0)


# How long the device's setup network must stay gone, nothing answering on it,
# to be taken as gone - not a moment's drop as it tests the Wi-Fi.
SETUP_GONE_AFTER = 15.0


def setup_network_gone(ctx: Context, network: str) -> bool:
    """Whether the device's setup network has gone: nothing answering on it for
    SETUP_GONE_AFTER, and not listed by a scan that sees any network (one that
    sees none cannot say)."""
    started = time.monotonic()
    while time.monotonic() - started < SETUP_GONE_AFTER:
        if setup_network.port_open(timeout=2.0):
            return False
        time.sleep(2.0)
    visible = prompts.scan_wifi_ssids(timeout=10.0)
    prefixes = (network,) if network.split()[0] == network else names.setup_networks(ctx)
    return bool(visible) and not any(name.startswith(prefixes) for name in visible)


# Tasmota's own name, from its defaults (tasmota.ino): what its settings
# erased - the 7x power-cycle reset - leave it with.
TASMOTA_DEFAULT_NAME_RE = re.compile(r"^tasmota-[0-9A-F]{6}-\d{4}$")


def name_with_the_wifi(ctx: Context) -> None:
    """A Tasmota on its setup network that has lost its name - its settings
    erased, as switching it off and on 7 times does - named again as it
    saves the Wi-Fi it is given: the name this run gives a Tasmota there
    (autoflash-recovery-CDEF part way through, tasmota-abcdef once F4 has
    named it), in the restart onto the Wi-Fi, which costs no other.  One
    with a name of its own keeps it."""
    if not ctx.state.input("device_mac"):
        return
    client = device.Device(setup_network.SETUP_HOST, timeout=8.0)
    try:
        # The name it goes by: Hostname gives the setting, which for the
        # default is the format it is made from (%s-%04d).
        status = client.command("Status 5")
    except (DeviceError, OSError):
        return
    net = status.get("StatusNET") if isinstance(status, dict) else None
    current = net.get("Hostname") if isinstance(net, dict) else None
    if not (isinstance(current, str) and TASMOTA_DEFAULT_NAME_RE.match(current)):
        return
    name = names.final_name(ctx, "tasmota") if ctx.state.is_done("F4") else names.our_setup_network(ctx)
    if client.at_next_save([f"Hostname {name}"]):
        output.say(f"    It lost its name with its settings ({current}): it is {name} again once it has the Wi-Fi.")


def check_device_network(ctx: Context) -> dict[str, Any]:
    """The device must be on the network the startup firmware will join.

    After E3 the startup firmware rejoins Wi-Fi by itself, with only the details
    in the bundle.  The device's own connection proves a network name works from
    where it stands; the bundle must name that network.  The password cannot be
    checked - Tasmota never reports it.
    """
    wanted = bundle_ssid(ctx.manifest_path)
    if wanted is None:
        output.say("This bundle's Wi-Fi details are not readable, so the network check is skipped.")
        return {"network_check": "skipped"}
    status = ctx.device.command("Status 11")
    wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
    connected = wifi.get("SSId")
    signal = wifi.get("Signal")
    output.private(wanted, "wifi-name")
    output.private(connected, "wifi-name")
    if not connected:
        raise Abort(
            "the device did not say which Wi-Fi network it is on, so the network the firmware "
            "to come joins cannot be checked. Nothing was written"
        )
    output.transient_begin()
    while connected != wanted:
        prompts.section("The firmware to come is set up for another Wi-Fi network", [
            f"The device is on '{connected}', but the firmware to come would join",
            f"'{wanted}' and never come back. Nothing has been written.",
        ])
        output.log_only("Later the device rejoins the Wi-Fi with only the startup firmware's details.")
        if not prompts.ask_yes_no(f"Set it up for '{connected}' instead, with its password", default=True):
            raise Abort(f"the device is on '{connected}' but the firmware to come would join '{wanted}'")
        ctx.adopt_wifi(connected, ask_password(connected))
        wanted = bundle_ssid(ctx.manifest_path)
    output.transient_end()
    output.say(f"Network check: the device is on '{connected}', the network the startup "
        f"firmware will join (signal {signal} dBm).")
    warn_if_weak_signal(ctx, signal, ["After the next stage the startup firmware has",
                                      "to rejoin on its own, and a weak signal makes that less certain."])
    return {"network": connected, "signal_dbm": signal}


def device_signal(device: device.Device) -> int | None:
    """The Wi-Fi signal in dBm that the Tasmota on the device reports, or None
    when it does not say."""
    try:
        status = device.command("Status 11")
    except (DeviceError, OSError):
        return None
    wifi = status.get("StatusSTS", {}).get("Wifi", {}) if isinstance(status, dict) else {}
    signal = wifi.get("Signal")
    return signal if isinstance(signal, int) else None


def note_signal(ctx: Context, signal: int | None, then: list[str]) -> int | None:
    """Say a reading of the device's Wi-Fi signal, and ask about a weak one."""
    if signal is not None:
        output.say(f"Wi-Fi signal at the device: {signal} dBm.")
        warn_if_weak_signal(ctx, signal, then)
    return signal


def warn_if_weak_signal(ctx: Context, signal: Any, then: list[str]) -> None:
    """A weak signal, asked about before going on; `then` says what has to join
    the Wi-Fi on its own afterwards.  Asked once a run: a weak signal agreed to
    is not asked about again at the next reading."""
    if not (isinstance(signal, int) and signal < WEAK_SIGNAL_DBM):
        return
    if ctx.state.input("weak_signal_agreed"):
        output.say("    The signal is still weak; carrying on, as agreed.")
        return
    output.transient_begin()
    prompts.section("The device's Wi-Fi signal is weak", [
        f"It reports {signal} dBm. Move it nearer the access point if you can.",
    ])
    output.log_only(" ".join(then))
    if not prompts.ask_yes_no("Carry on anyway", default=False):
        raise Abort("stopped for a weak Wi-Fi signal; nothing was written")
    ctx.state.set_input("weak_signal_agreed", True)
    output.transient_end()


# While a device is waited for on the home network, its own network - raised once
# it has failed to join for a minute or two - is looked for this often: up, the
# device cannot join, and waiting the rest of the time out gains nothing.
OWN_NETWORK_LOOK_EVERY = 30.0


class OwnNetworkUp(Abort):
    """A device waited for raised its own network instead: it cannot join."""

    def __init__(self, what: str, network: str) -> None:
        super().__init__(f"{what} did not join your Wi-Fi and has raised its own network, {network}")
        self.network = network


class OwnNetworkWatch:
    """Whether the device's own network, one of `networks`, has come up: looked
    for at most every OWN_NETWORK_LOOK_EVERY seconds of the wait, the first a
    period in - the wait's own clock, `elapsed`, so it reads none of its own."""

    def __init__(self, networks: tuple[str, ...]) -> None:
        self.networks = networks
        self.next_look = OWN_NETWORK_LOOK_EVERY

    def up(self, elapsed: float) -> str | None:
        if elapsed < self.next_look:
            return None
        self.next_look = elapsed + OWN_NETWORK_LOOK_EVERY
        return prompts.find_ssid(self.networks, timeout=10)


def wait_for(ctx: Context, check: Callable[[str], Any], what: str, timeout: float = 240.0,
             own_network: tuple[str, ...] = ()) -> Any:
    """Wait for `check(address)` to answer, finding the device again by its
    hardware address if it came back on a new address.  With `own_network`,
    the names the device's own network can have: that coming up ends the
    wait at once (OwnNetworkUp, an Abort)."""
    started = time.monotonic()
    watch = OwnNetworkWatch(own_network) if own_network else None
    while (elapsed := time.monotonic() - started) < timeout:
        host = ctx.state.input("device_ip")
        found = host and check(host)
        if found:
            output.progress_clear()
            return found
        moved = ctx.finder.poll()
        if moved and moved != host:
            continue
        network = watch.up(elapsed) if watch else None
        if network:
            output.progress_clear()
            raise OwnNetworkUp(what, network)
        output.progress(f"waiting for {what}... {elapsed:.0f}s")
        time.sleep(3)
    output.progress_clear()
    raise Abort(f"{what} did not answer within {timeout / 60:.0f} minutes")


# Sending Wi-Fi details through a device's own network: tried this many times
# while this computer is still joined to it, as other sends to the device are.
SETUP_SEND_ATTEMPTS = 3


def on_a_device_network() -> bool:
    """Whether this computer is on a device's own network now - joined to it
    already, before being asked, say: something answers at its address."""
    return setup_network.page(setup_network.SETUP_HOST, "/", timeout=2.0) is not None


def send_through_own_network(network: str, ssid: str, password: str,
                             back: Callable[[], Any] | None = None) -> None:
    """ESPHome's (or the mover's) page given the details, up to
    SETUP_SEND_ATTEMPTS times, once this computer has joined its network;
    BackByItself if `back` finds the device on the home network first."""
    output.event("join", network=network)
    prompts.section("Join the device's setup network", [
        f"In {hostsys.wifi_menu_hint()}, join:",
        f"    {network}      (open - no password)",
        "If a page opens by itself, close it: this sends the details for you.",
    ])
    for attempt in range(1, SETUP_SEND_ATTEMPTS + 1):
        wait_until_joined(network, back=back)
        try:
            setup_network.send_esphome_wifi(ssid, password)
        except setup_network.SetupError as exc:
            if attempt == SETUP_SEND_ATTEMPTS:
                raise Abort(f"the Wi-Fi details could not be sent to the device: {exc}") from exc
            output.tell(f"    {exc}; sending them again (attempt {attempt + 1} of {SETUP_SEND_ATTEMPTS}).")
            time.sleep(5)
            continue
        output.tell(f"    Sent. The device joins '{ssid}' now.")
        return


def set_up_through_own_network(ctx: Context, what: str, check: Callable[[str], Any],
                               networks: tuple[str, ...]) -> tuple[str, str] | None:
    """A device on ESPHome (or the mover) that cannot join the home network, set
    up through its own: it raises one after a while away from the home one,
    whose page takes Wi-Fi details. The details this run has go first - right
    after a router outage, say - and new ones are asked for only if the device
    does not join with them. `networks`: what its network can be called, its
    name now first. The details it joined with, or None when its network is
    not to be seen; BackByItself when, before this computer has joined its
    network, the device is back on the home one with the details it has: it
    raised its network while the router was off, say."""
    network = prompts.find_ssid(networks, timeout=30)
    if not network and on_a_device_network():
        network = networks[0]  # joined already: carried on through it
    if not network:
        return None
    note_own_network(ctx, NOT_JOINED)
    output.tell(f"    {what} cannot join your Wi-Fi, so it has raised its own network, {network}.")
    ssid, password = ctx.wifi(why=[
        f"{what} can't join your Wi-Fi; this sends it them through its network.",
    ])
    def back() -> bool:
        host = ctx.state.input("device_ip")
        if host and check(host):
            return True
        moved = ctx.finder.poll()
        return bool(moved and moved != host and check(moved))

    try:
        send_through_own_network(network, ssid, password, back=back)
    except BackByItself:
        forget_own_network(ctx)
        output.tell(f"    {what} is back on your Wi-Fi by itself: its network is not needed.")
        raise
    for round_ in (1, 2):
        if round_ == 2:
            send_through_own_network(network, ssid, password)
        output.event("home")
        prompts.section("Back to your usual Wi-Fi", [
            "Rejoin it now if this computer hasn't by itself.",
        ])
        try:
            wait_for(ctx, check, what, timeout=300.0)
            return ssid, password
        except Abort:
            if round_ == 2 or prompts.UNATTENDED:
                raise Abort(f"{what} did not join '{ssid}' with the details sent through its own "
                            "network. Check the network's name and password, then run this again") from None
        output.tell(f"    {what} did not join '{ssid}' with those details: check its name and password.")
        output.transient_begin()
        prompts.section("The Wi-Fi details the device should use", [
            "The 2.4 GHz network the device should be on, sent through its own network:",
        ])
        ssid, password = ask_credentials()
        output.transient_end()
    raise AssertionError("unreachable")


# A step keeps the device busy for about a minute at most, retrying included.
BUSY_DEVICE_WAIT = 120.0


def wait_while_device_busy(ctx: Context) -> None:
    """Give a device still running a stopped step the time to finish it.

    A migration step keeps the device busy, answering nothing else, until it
    succeeds or gives up - for a while after this computer stopped waiting
    (Ctrl-C, say).  Run again at once, the device looks unreadable, and the
    run would go on as if it were.  It is told apart by answering ping.
    """
    host = ctx.state.input("device_ip")
    if (not host or not ctx.state.is_done("D1") or ctx.state.is_done(ctx.steps[-1][0])
            or ctx.state.is_done("I1")):
        return  # not running Tasmota yet, finished, or running ESPHome
    device = ctx.device
    if device.answers(timeout=3.0) or not hostsys.ping_ok(host):
        return  # answering, or not on the network at all: the steps say which
    output.tell("    The device is still finishing the step that was stopped (a minute or two).")
    started = time.monotonic()
    while time.monotonic() - started < BUSY_DEVICE_WAIT:
        output.progress(f"waiting for the device to finish... {time.monotonic() - started:.0f}s")
        if device.answers(timeout=3.0):
            output.progress_clear()
            output.tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    output.tell("    Still no answer. If a step fails, switch it off and on, then run again.")
