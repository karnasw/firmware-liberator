"""The ports this computer serves the device on, and the firewall rules that
let the device reach them for the run, closed again when it ends.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tools.autoflash import hostsys, interrupt, output, prompts
from tools.autoflash.errors import Abort
from tools.autoflash.hostsys import FirewallRule

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# Ports the bridge may be served on, in order of preference.  The upgrade
# command names whichever is chosen, and the device fetches from that port:
# 8088 matches the vendor OTA port, but the research probe was served on
# 8765.  The phase servers have their own ports, so the two never share one.
DELIVERY_PORTS = (8088, *range(8090, 8100))
# Ports the migration phase servers (serve_safeboot_migration.py) may listen
# on, in order of preference.  Nothing on the device keeps the port: each phase
# starts its own server and sends the device a loader URL that names it.
PHASE_PORTS = (8089, *range(8100, 8110))


def choose_port(ctx: Context, key: str, candidates: tuple[int, ...], serving: str) -> int:
    """The first of `candidates` free on this computer, recorded as `key`.

    Chosen afresh each time, so a port that was busy last run is tried again.
    Raises Abort - before anything reaches the device - when none is free.
    """
    port = hostsys.first_free_port(ctx.listen_ip(), candidates)
    if port is None:
        raise Abort(
            f"ports {candidates[0]} and {candidates[1]}-{candidates[-1]} are all in use on this "
            f"computer, so there is nowhere to serve {serving} from. Stop whatever is "
            "using them and run this again - nothing was sent to the device"
        )
    if port != candidates[0]:
        output.say(f"Port {candidates[0]} is in use on this computer; serving {serving} on {port} instead.")
    ctx.state.set_input(key, str(port))
    return port


def firewall_ports(ctx: Context) -> list[int]:
    """The ports the device connects to this computer on: bridge delivery and phases."""
    return sorted({int(ctx.state.input("delivery_port") or DELIVERY_PORTS[0]),
                   int(ctx.state.input("phase_port") or PHASE_PORTS[0])})


def emulator_password_hint() -> list[str]:
    """Inside the emulator (tools/espressif/emulator) only: the password sudo asks for
    there, so nobody has to look it up.  A real computer's is never known."""
    known = os.environ.get("AUTOFLASH_EMULATOR_SUDO_PASSWORD")
    if not known or hostsys.IS_WINDOWS:
        return []
    return [f"In the emulator, the password is: {known}"]


def recorded_rules(ctx: Context) -> list[FirewallRule]:
    return [FirewallRule.from_json(item) for item in ctx.state.input("firewall_rules") or []]


def ensure_firewall(ctx: Context) -> None:
    """Let the device's connections through this computer's firewall, once per address.

    The rule admits only the device's address on the two ports the tool listens
    on, and is recorded so it can be removed when the tool exits, however it
    exits; a resumed run adds it again.  How it went is kept for the run's
    report (firewall_outcome: one of report_data.FIREWALL_OUTCOMES).
    """
    device_ip = ctx.state.input("device_ip")
    ports = firewall_ports(ctx)
    rules = recorded_rules(ctx)
    if not device_ip or any(r.device_ip == device_ip and set(ports) <= set(r.ports) for r in rules):
        return
    # Other security software, or an organisation's policy: logged, and shown if
    # the device then cannot connect (firewall_hint) - except a policy that
    # ignores rules added here, which is certain to stop it, so is shown now.
    advice = hostsys.firewall_advice(device_ip, ports)
    for line in advice:
        output.say(line)
    if hostsys.local_rules_ignored(device_ip):
        ctx.state.set_input("firewall_outcome", "managed")
        prompts.section("This computer's firewall is managed by an organisation", [
            "It ignores rules added here: ask whoever manages it, or use another one.",
            "Carrying on: if the device is blocked, the next step says so.",
        ])
        output.log_only("Group Policy has it ignore rules added on the computer itself.")
        return
    kind = hostsys.active_firewall()
    if kind is None:
        ctx.state.set_input("firewall_outcome", "none")
        return
    program = hostsys.python_program() if kind in ("windows", "macos") else None
    add, _ = hostsys.firewall_commands(kind, device_ip, ports, program)
    prompt_note = ["Windows will ask whether to allow the change - choose Yes."] if hostsys.IS_WINDOWS else [
        "You may be asked for this computer's password.", *emulator_password_hint()]
    # The macOS firewall allows programs, not ports.
    what = ("It lets Python accept incoming connections." if kind == "macos"
            else f"It lets in only {device_ip}, to Python, on TCP ports {', '.join(map(str, ports))}."
            if kind == "windows"
            else f"It lets in only {device_ip}, on TCP ports {', '.join(map(str, ports))}.")
    output.transient_begin()  # the note, and the system's password question
    prompts.section("Let the device through this computer's firewall", [
        f"This computer's firewall ({kind}) would block the device: adding a rule",
        "that is removed when this tool exits, however it exits.",
        what,
        *prompt_note,
    ])
    if hostsys.IS_WINDOWS:
        output.log_only("This window need not be run as administrator: only the rule asks for it.")
    try:
        with interrupt.deferred():  # added and recorded, or neither
            rule = hostsys.open_firewall(device_ip, ports)
            if rule is not None:
                rules.append(rule)
                ctx.state.set_input("firewall_rules", [r.to_json() for r in rules])
    except OSError:
        ctx.state.set_input("firewall_outcome", "failed")
        output.transient_end()
        prompts.section("The firewall rule could not be added", [
            "Add it yourself in another window, and remove it afterwards:",
            f"  {hostsys.display(add)}",
            "Carrying on: if the device is blocked, the next step says so.",
            *(["It needs administrator rights; on a work computer, ask whoever manages it."]
              if hostsys.IS_WINDOWS else []),
        ])
        return
    output.transient_end()
    ctx.state.set_input("firewall_outcome", "there" if rule is not None and rule.detail.get("preexisting") else "added")
    if rule is not None:
        output.say("The rule was already there, so it is left in place afterwards."
            if rule.detail.get("preexisting") else
            "The rule was there from an earlier run that stopped as it was added; "
            "it is removed afterwards." if rule.detail.get("left_by_earlier_run") else
            "Firewall rule added.")
        if rule.detail.get("paused"):
            output.say("Python had been blocked from networks here, most likely by a \"Cancel\" in")
            output.say("Windows Security once. That block is paused while this runs, and put back")
            output.say("as it was afterwards.")


def firewall_hint(ctx: Context) -> list[str]:
    """What to say when the device never reached this computer."""
    device_ip = ctx.state.input("device_ip") or "DEVICE_IP"
    lines = [
        "The device never reached this computer: most often this computer's",
        "firewall, or a changed address.",
    ]
    kind = hostsys.active_firewall()
    ports = firewall_ports(ctx)
    if kind:
        program = hostsys.python_program() if kind in ("windows", "macos") else None
        add, _ = hostsys.firewall_commands(kind, device_ip, ports, program)
        lines += [f"A {kind} firewall is on; this lets the device in:", f"  {hostsys.display(add)}"]
    advice = hostsys.firewall_advice(device_ip if device_ip != "DEVICE_IP" else None, ports, after_failure=True)
    if advice:
        lines += ["", *advice]
    return lines


def close_firewall_rules(ctx: Context) -> None:
    """Remove every rule this run added, and say what is left, if anything."""
    rules = recorded_rules(ctx)
    if not rules:
        return
    output.transient_begin()  # gone once the rule is
    output.tell("")
    output.tell("Removing the temporary firewall rule; Windows will ask again - choose Yes."
                if hostsys.IS_WINDOWS else "Removing the temporary firewall rule; you may be asked for your password.")
    for line in emulator_password_hint():
        output.tell(line)
    left: list[FirewallRule] = []
    for rule in rules:
        if not hostsys.close_firewall(rule):
            left.append(rule)
    ctx.state.set_input("firewall_rules", [r.to_json() for r in left])
    output.transient_end()
    if left:
        prompts.section("Remove the temporary firewall rules yourself", [
            line for r in left for line in hostsys.undo_commands(r)
        ])
    else:
        output.say("The temporary firewall rules have been removed.")


def release_firewall(ctx: Context) -> bool:
    """On the way out, however the tool is leaving: remove the firewall rule.

    The usual exits have removed it already; this catches Ctrl-C, being
    stopped, and anything unexpected.  Nothing listens on its ports once the
    tool has exited, so it has no reason to stay, and a resumed run adds it
    again.  True when no rule is left behind.
    """
    try:
        if recorded_rules(ctx):
            close_firewall_rules(ctx)
        return not recorded_rules(ctx)
    except (KeyboardInterrupt, OSError) as exc:
        # Ctrl-C at the password prompt, or no terminal left to ask on.
        left = recorded_rules(ctx)
        output.tell("")
        output.tell("The temporary firewall rule was not removed"
             + (" (Ctrl-C)" if isinstance(exc, KeyboardInterrupt) else f" ({exc})") + "; remove it with:")
        for rule in left:
            for line in hostsys.undo_commands(rule):
                output.tell("    " + line)
        return False


def say_quit(ctx: Context) -> None:
    """The last word after the operator chose to quit, once everything is tidied:
    the run's secrets are gone and the firewall rule is removed. Nothing more is
    claimed: a quit can leave the device part way (on the installer, say), which
    running again carries on from."""
    if release_firewall(ctx):
        output.tell("")
        output.tell(f"{prompts.STOPPED_AS_ASKED} Run this again to carry on.")
