"""Supla GUI-Generic, written from the hub (S0, S1, S2): the image the
builder gave checked before anything is written, written beside the Berry
Tasmota with Supla's memory layout, then set up through its own network.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import hostsys, join, kinds, output, plans, prompts, setup_network, supla
from tools.autoflash.errors import Abort
from tools.autoflash.routes.espressif import hub

if TYPE_CHECKING:
    from tools.autoflash.context import Context

BUILDER = "https://gui-generic-builder-new.supla.io/"
# GUI-Generic's own network, raised when it has no Wi-Fi to join, and its page there.
OWN_NETWORK_PREFIX = "SUPLA-"
OWN_PAGE = "http://192.168.4.1"


def image_path(ctx: Context) -> Path:
    """The builder's file: given (--supla), kept from the run's start, or asked for."""
    if ctx.supla_firmware:
        return ctx.supla_firmware
    kept = ctx.state.input("supla_image")
    if kept:
        return Path(kept)
    try:
        answer = prompts.ask_text(
            "Path to the GUI-Generic firmware",
            help_lines=[f"Build GUI-Generic for the device at {BUILDER}",
                        "(ESP32-C3, the S60's template), and give the .bin or the .zip it downloads."],
            example="~/Downloads/GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin.zip")
    except prompts.NoOneToAnswer:
        raise Abort("an unattended run to Supla needs its firmware: --supla PATH") from None
    return Path(answer.strip().strip("'\"")).expanduser().resolve()


def checked_image(ctx: Context) -> tuple[bytes, str]:
    """The GUI-Generic app, checked; on a run that checked it before, the
    same one, unchanged since."""
    path = image_path(ctx)
    try:
        image, name = supla.load(path)
        supla.check(image, name)
    except (supla.ImageError, OSError) as exc:
        raise Abort(f"the Supla firmware cannot be used: {exc}") from exc
    kept = ctx.state.input("supla_sha256")
    if kept and kept != hashlib.sha256(image).hexdigest():
        raise Abort(f"{name} has changed since this run checked it: give the same file, or "
                    "start again with --new")
    return image, name


def step_image(ctx: Context) -> dict[str, Any]:
    """S0 - the GUI-Generic image checked, before anything is written: an
    ESP32-C3 app that fits Supla's slots, passes its own integrity check and
    is GUI-Generic; and built with the device's template, where it has one."""
    image, name = checked_image(ctx)
    path = image_path(ctx)
    kind = kinds.of_run(ctx.state)
    matches = supla.template_matches(image, kind)
    if matches is False:
        raise Abort(f"{name} was built with another device's template: build it with the "
                    f"{kind.short}'s template, or set its pins by hand, and give it again")
    if matches is None:
        output.tell(f"    {name} carries no device template: set the {kind.short}'s pins in")
        output.tell("    GUI-Generic once it runs, or it will switch nothing.")
    ctx.state.set_input("supla_image", str(path))
    ctx.state.set_input("supla_sha256", hashlib.sha256(image).hexdigest())
    version = supla.builder_version(name)
    output.say(f"Supla GUI-Generic {version or ''} checked: {name}".replace("  ", " "))
    return supla.describe(image) | {"file": name, "builder_version": version, "template": matches}


def step_supla_install(ctx: Context) -> dict[str, Any]:
    """S1 - GUI-Generic into ota_0 from the temporary Tasmota, every block
    read back; then Supla's layout and the switch to it (plans.supla).

    The first half of the app is written by one plan and the rest by a
    second; a run that finished the first goes on from the second, which
    finds its table written already if it got that far."""
    image, name = checked_image(ctx)
    signal = join.note_signal(ctx, join.plug_signal(ctx.device), [
        "Once Supla is written, it starts with its own network for you to",
        "set it up through; a weak signal makes joining your Wi-Fi after that less certain."])
    try:
        first, second = plans.supla(image, supla.table())
    except plans.PlanError as exc:
        raise Abort(f"the Supla firmware cannot be written: {exc}") from exc
    prompts.section("Installing Supla", quiet=True, body=[
        f"{name} goes into the half of the device's memory Tasmota is not running",
        "from. Every block is read back and checked against the file; only then is",
        "the memory layout given room for Supla's settings, and the device switches",
        "to Supla and restarts.",
    ])
    if ctx.dry_run:
        output.say(f"[dry-run] would install {name}")
        return {"dry_run": True, "image": name}
    sha = hashlib.sha256(image).hexdigest()
    if ctx.state.input("supla_first_half") != sha:
        hub.run_plan(ctx, first)
        ctx.state.set_input("supla_first_half", sha)
    return hub.run_plan(ctx, second) | {"image": name, "size": len(image), "signal_dbm": signal}


def supla_answers(address: str) -> bool:
    """Whether GUI-Generic's page answers at `address`."""
    page = setup_network.page(address, "/", timeout=3.0)
    return bool(page) and any(marker.decode() in page for marker in supla.MARKERS)


def step_supla_check(ctx: Context, wait: float = 240.0) -> dict[str, Any]:
    """S2 - Supla started, and set up through its own network.

    GUI-Generic has no way to be given the Wi-Fi before it starts: it raises
    its own network, whose page takes the Wi-Fi and Supla's server.  Once
    that is done the device is looked for on the home network by its
    hardware address."""
    if ctx.dry_run:
        output.say("[dry-run] would wait for Supla's own network")
        return {"dry_run": True}
    output.say("Waiting for the device to start Supla...")
    network = prompts.wait_for_ssid(OWN_NETWORK_PREFIX, timeout=120)
    prompts.section("Set up Supla", [
        f"In {hostsys.wifi_menu_hint()}, join:",
        f"    {network or 'the network whose name starts with ' + OWN_NETWORK_PREFIX}      (open - no password)",
        "",
        f"Open {OWN_PAGE} in a browser and give it:",
        "    - your Wi-Fi's name and password",
        "    - your Supla server and the email of your Supla account (cloud.supla.org's,",
        "      or your own server's)",
        "Save; the device restarts and joins your Wi-Fi. Then put this computer back",
        "on your Wi-Fi.",
    ])
    prompts.wait_for_key("Press any key once that is done, or q to quit...")
    try:
        found = join.wait_for(ctx, supla_answers, "Supla on your Wi-Fi", timeout=wait)
    except Abort:
        if not network:
            # Neither its network nor its page: not shown to be running at all,
            # as when it restarts over and over.  Not a conversion to call complete.
            raise Abort(
                "Supla was written and checked, but neither its own network "
                f"({OWN_NETWORK_PREFIX}...) nor Supla on your Wi-Fi has been seen. Switch the "
                "device off and on, and run the same command again to look for it again"
            ) from None
        output.tell("Supla is written and checked, but has not been seen on your Wi-Fi yet. If it")
        output.tell(f"was not set up, its own network ({OWN_NETWORK_PREFIX}...) is still there to set it up through;")
        output.tell("your router's list of devices gives its address once it has joined.")
        return {"network": network, "seen": False}
    host = ctx.state.input("plug_ip")
    output.say(f"    Supla answered at {host}.")
    return {"network": network, "seen": bool(found)}


def evidence_supla_installed(ctx: Context) -> tuple[bool | None, str]:
    """S1, cut off: the temporary Tasmota answering means the plug did not
    switch; Supla's network or page means it did."""
    host = ctx.state.input("plug_ip")
    if host and supla_answers(host):
        return True, "The device is running Supla, so the install did finish."
    if host and ctx.device.reachable() and ctx.device.current_ota() == 1:
        return False, ("The plug is still running the temporary Tasmota, so it did not switch;\n"
                       "  it is safe to do this step again.")
    network = prompts.find_ssid(OWN_NETWORK_PREFIX)
    if network:
        return True, f"The device is broadcasting '{network}': Supla is running, so the install did finish."
    return None, "The device neither runs Supla nor answers as the temporary Tasmota."
