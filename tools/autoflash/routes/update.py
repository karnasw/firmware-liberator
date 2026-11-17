"""This tool's firmware updated on a device it converted: from an earlier release
of the image published for the device's kind to this one, sent as the firmware
takes any update of its own, the device's settings kept.  The way a fix
reaches a device that nothing else builds for: an ESPHome no dashboard has
adopted, or an OpenBeken carrying this tool's driver and flash layout, which
upstream's own builds do not.  Official Tasmota updates itself, from its own
page; it is offered the release this tool installs too, so that running the
tool again updates any device it converted.

Each family sends the image its own way and checks it arrived; what is
shared is here: which device is offered the update, and what it says once it
has it.  One is offered it only when its firmware says it is this tool's
for its kind at an earlier version (RELEASE).  An ESPHome is also passed
over where its API is encrypted, which ours never is, or where it is a build
this project did not publish (published_builds): one adopted into an
ESPHome dashboard, with a key or without, is that dashboard's to update - the
published image would take away its changes and any key.  Its owner can ask
for that (--replace-own-build, replace_own_build).
"""

from __future__ import annotations

import re
import time
from typing import Any, Callable, TYPE_CHECKING

from tools.autoflash import detect, esphome, join, kinds, openbeken, output, prompts, runs, unlock
from tools.autoflash.errors import Abort

if TYPE_CHECKING:
    from tools.autoflash.context import Context


def older(version: str, than: str) -> bool:
    """Whether `version` (1.4.0, or OpenBeken's 1.18.320-1: the release, then
    this project's build of it) is an earlier release than `than`; False where
    either is not one of ours, all numbers."""
    try:
        return tuple(map(int, re.split(r"[.-]", version))) < tuple(map(int, re.split(r"[.-]", than)))
    except ValueError:
        return False


# What a device whose own build is being replaced runs, in place of a release:
# the same version as the published one, often, so the version cannot tell them.
OWN_BUILD = "your own build"


def published_builds(kind: kinds.Kind) -> set[str]:
    """When each ESPHome this project published for `kind` was built, as its API
    says.  Any other build is someone's own, whatever version it gives: the same
    config built by a dashboard says the same version, and need not have a key."""
    # Each family's images are its package's, and each package imports this.
    from tools.autoflash.routes.beken import images as beken_images
    from tools.autoflash.routes.espressif import images as espressif_images
    pin = (beken_images if kind.family == "beken" else espressif_images).PINS[kind.key]["esphome"]
    return {pin["built"], *pin.get("earlier_builds", ())}


def esphome_release(ctx: Context) -> str | None:
    """The release of this tool's ESPHome for the run's kind the device runs
    (esphome.api_device_info), or None: another project's, an encrypted one, or
    a build this project did not publish."""
    host = ctx.state.input("device_ip")
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        return None
    kind = kinds.of_run(ctx.state)
    if not info or info["project_name"] not in kind.esphome_projects:
        return None
    if info["built"] == ctx.state.input("replaced_build"):
        return OWN_BUILD
    if info["encrypted"] or info["built"] not in published_builds(kind):
        return None
    return str(info["project_version"])


def openbeken_release(ctx: Context) -> str | None:
    """The release of this tool's OpenBeken the device runs: the version its
    build string gives (each is built at its release's), where its driver
    says it is on the run's kind of device - or None."""
    host = ctx.state.input("device_ip")
    found = openbeken.info(host) if host else None
    if not found or openbeken.kind(host, found) is not kinds.of_run(ctx.state):
        return None
    return openbeken.version(found)


def tasmota_release(ctx: Context) -> str | None:
    """The release of official Tasmota the device runs, as its version says
    (16.0.0(release-tasmota32)) - or None: anyone's own build, its
    safeboot, or nothing answering."""
    found = re.fullmatch(r"(\d+(?:\.\d+)+)\s*\(release-tasmota32\)", ctx.device.firmware_version())
    return found.group(1) if found else None


# How each firmware this tool installs says which release of it the device runs.
RELEASE: dict[str, Callable[["Context"], str | None]] = {"esphome": esphome_release,
                                                          "openbeken": openbeken_release,
                                                          "tasmota": tasmota_release}


def earlier_release(ctx: Context, firmware: str, published: str | None) -> str | None:
    """The release the device runs, where that is this tool's `firmware` for its
    kind, older than the `published` one; else None."""
    if firmware not in RELEASE or not published or (firmware == "esphome" and ctx.esphome_firmware):
        return None
    release = RELEASE[firmware](ctx)
    return release if release is not None and older(release, published) else None


def outdated(ctx: Context, firmware: str, published: str | None) -> bool:
    """Whether the device runs an earlier release of `firmware` than the
    `published` one: recorded with both versions (update_from, update_to)."""
    release = earlier_release(ctx, firmware, published)
    if release is None:
        return False
    ctx.state.set_input("update_from", release)
    ctx.state.set_input("update_to", published)
    return True


def is_own_build(ctx: Context) -> bool:
    """Whether the device's ESPHome is this project's for its kind but not a build
    it published, or has an API that will not say without a key: adopted into a
    dashboard, most likely."""
    host = ctx.state.input("device_ip")
    kind = kinds.of_run(ctx.state)
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key) if host else None
    except esphome.ApiLocked:
        return True
    return bool(info and info["project_name"] in kind.esphome_projects
                and (info["encrypted"] or info["built"] not in published_builds(kind)))


def replace_own_build(ctx: Context, firmware: str, published: str | None) -> bool:
    """--replace-own-build: whether to send the published ESPHome over the device's
    own build, its owner having agreed to what that loses; recorded as an update
    is, with the build it replaces.  False from a build this project published,
    which needs nothing of the kind.  Refused for all but this project's ESPHome
    for the device's kind: another's may have other pins or another flash layout."""
    host = ctx.state.input("device_ip")
    kind = kinds.of_run(ctx.state)
    if firmware != "esphome" or not published or not host:
        raise Abort(f"--replace-own-build is for this project's ESPHome, and the device does not answer as that"
                    f"{'' if firmware == 'sonoff' else f': it runs {detect.NAMES[firmware]}'}")
    try:
        info = esphome.api_device_info(host, key=ctx.esphome_key)
    except esphome.ApiLocked as locked:
        if not unlock.ask_esphome_key(ctx, host, wifi=True):
            raise Abort(f"--replace-own-build needs the device's API encryption key, "
                        f"{'and the one given is not it' if locked.wrong else 'and none was given'}: "
                        f"{unlock.ESPHOME_KEY_HINT}") from None
        info = esphome.api_device_info(host, key=ctx.esphome_key)
    if not info or info["project_name"] not in kind.esphome_projects:
        raise Abort(f"--replace-own-build only replaces this project's ESPHome for a {kind.name}, and the "
                    f"device says it is {(info or {}).get('project_name') or 'no project'}")
    if not info["encrypted"] and info["built"] in published_builds(kind):
        return False
    with output.until_done():   # agreed, it has done its job; declined, it says why the run stops
        replace_agreed(published)
    ctx.state.set_input("replaced_build", info["built"])
    ctx.state.set_input("update_from", OWN_BUILD)
    ctx.state.set_input("update_to", published)
    return True


def replace_agreed(published: str) -> None:
    """replace_own_build's warning, and its owner's go-ahead; Abort if not given."""
    prompts.warning_box("Replacing your own build", [
        f"This installs this project's ESPHome {published} over the device's build.",
        "Your config changes and API encryption key are lost; remove and re-add",
        "it in Home Assistant. A renamed relay reverts, which may switch it.",
    ])
    if not prompts.require_word(prompts.CONSENT_WORD):
        raise Abort("not confirmed")


def versions(ctx: Context) -> tuple[str, str]:
    """The release the device runs and the one it is updated to."""
    return ctx.state.input("update_from") or "", ctx.state.input("update_to") or ""


def release(ctx: Context) -> str | None:
    """The release of the run's firmware the device says it runs now."""
    return RELEASE[runs.run_firmware(ctx.state)](ctx)


def evidence_updated(ctx: Context) -> tuple[bool | None, str]:
    """Whether the update got onto the device, by the version it answers with."""
    old, new = versions(ctx)
    name = detect.NAMES[runs.run_firmware(ctx.state)]
    version = release(ctx)
    if version == new:
        return True, f"The device runs {name} {new}, so the update did finish."
    if version == old:
        return False, f"The device still runs {name} {old}: it is safe to send the update again."
    return None, f"The device does not answer as {name}."


# How often the device is asked its release while it confirms an update (seconds):
# more often than it can restart, so a restart in between is seen.
CONFIRM_POLL = 5.0


def confirmed_by(ctx: Context, name: str, old: str, new: str, confirm: float, wait: float) -> None:
    """Returns once the device has answered with `new` for `confirm` seconds on
    end; stops the run where it goes back to `old`, goes unanswered for
    `wait` + `confirm` seconds, or keeps restarting (three times as long in all)."""
    started = held_since = time.monotonic()
    lost_since: float | None = None
    while True:
        time.sleep(CONFIRM_POLL)
        now = release(ctx)
        if now == old:
            raise Abort(f"the device went back to {name} {old}: the update did not hold. Running the same "
                        "command again sends it again")
        if now != new:
            if lost_since is None:
                output.say(f"    The device stopped answering before {name} {new} had confirmed itself;")
                output.say("    waiting for it to answer again, then a minute more...")
                lost_since = time.monotonic()
            held_since = None
        elif held_since is None:
            held_since, lost_since = time.monotonic(), None
        elif time.monotonic() - held_since >= confirm:
            return
        if (lost_since is not None and time.monotonic() - lost_since >= wait + confirm) \
                or time.monotonic() - started >= 3 * (wait + confirm):
            raise Abort(f"{name} {new} was not seen running for a minute after it was sent: switch the "
                        "device off and on, then run the same command again")


def still_updated(ctx: Context) -> tuple[bool, str]:
    """An update recorded done is doubted where the device still runs the release
    it had (STEP_VERIFIERS): its new one never started, and is sent again."""
    updated, message = evidence_updated(ctx)
    return updated is not False, message[0].lower() + message[1:].rstrip(".")


def wait_for_update(ctx: Context, wait: float = 180.0, confirm: float = 0.0) -> dict[str, Any]:
    """The device started the new release and joined the Wi-Fi; then, for
    `confirm` seconds, still runs it: an ESPHome that takes an update back if
    it restarts before then (the S60's) has kept it.

    Kept only once it has answered with the new release, asked every
    CONFIRM_POLL seconds, for `confirm` seconds on end. One it did not answer
    may have restarted (a power cut, say) before it had confirmed it: the time
    starts again once it answers, so done is never said of a device not seen to
    have kept it."""
    old, new = versions(ctx)
    name = detect.NAMES[runs.run_firmware(ctx.state)]
    output.say(f"Waiting for the device to start {name} {new} and join your Wi-Fi...")
    started = time.monotonic()
    join.wait_for(ctx, lambda _host: release(ctx) == new, f"{name} {new}", timeout=wait)
    seconds = round(time.monotonic() - started, 1)
    if confirm:
        output.say("    Waiting a minute while it confirms itself, so a power cut now")
        output.say("    cannot take it back...")
        confirmed_by(ctx, name, old, new, confirm, wait)
    output.say(f"    {name} {new} answered after {seconds:.0f}s.")
    return {"seconds": seconds, "from": old, "to": new}
