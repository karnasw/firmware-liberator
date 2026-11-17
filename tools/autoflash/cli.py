#!/usr/bin/env python3
"""Convert a device over the air: from its stock eWeLink firmware to Tasmota or
ESPHome, from either of those to the other, or back to its stock firmware.

This orchestrates the reviewed tools in tools/.  It reimplements no validator,
no Berry phase and no safety gate: every destructive action still runs the same
audited program, behind the same locks, judged by the same host-side evidence.
What it removes is console pasting, transcription error and lost place.

Every step records itself in captures/<device>/autoflash-state.json, so an
interrupted run resumes where it stopped.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import argparse  # noqa: E402
import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import signal  # noqa: E402

from tools.autoflash import (  # noqa: E402
    __version__,
    awake,
    credentials,
    firewall,
    hostsys,
    interrupt,
    join,
    output,
    records,
    prompts,
    readout,
    report,
    report_upload,
    routes,
    runner,
    runs,
    signup,
)
from tools.autoflash.context import Context  # noqa: E402
from tools.autoflash.errors import Abort  # noqa: E402
from tools.autoflash.hostsys import FirewallRule  # noqa: E402
from tools.autoflash.prompts import QuitRun  # noqa: E402
from tools.autoflash.routes.beken import images as beken_images  # noqa: E402
from tools.autoflash.routes.espressif import bundle, identity, installer, sonoff_firmware  # noqa: E402
from tools.autoflash.routes.espressif import images as espressif_images  # noqa: E402
from tools.autoflash.state import FAILED, PENDING, RunState  # noqa: E402
from tools.espressif.safeboot.prepare_imprinted_bundle import forget_stored_password  # noqa: E402



def adopt_older_runs() -> None:
    """Move runs kept straight under captures/ to their device's directory, when
    they know its MAC.  Those that do not stay where they are, still listed."""
    pointer = runs.read_current()
    for directory in [d for d in runs.run_directories() if d.parent == records.CAPTURES]:
        try:
            mac = RunState.open(directory).input("device_mac")
            if not mac or not records.MAC_RE.match(mac):
                continue
            target = records.runs_dir(mac) / directory.name
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            directory.rename(target)
        except (OSError, ValueError):
            continue  # left where it was; it still works from there
        if pointer == directory.name:
            runs.remember_current(runs.run_name(target))


def run_is_complete(directory: Path) -> bool:
    try:
        state = RunState.open(directory)
    except (OSError, ValueError):
        return False
    if state.input(readout.STATE_KEY):
        return True  # a readout converts nothing, so has nothing to carry on
    steps = routes.steps_of(state)
    if not steps:
        return state.input("firmware") is not None  # it already ran what was asked
    return state.is_done(steps[-1][0])


def describe_run(directory: Path) -> str:
    """Where a run stopped, briefly: the device, the step, when, and how far in."""
    import datetime as dt

    state = RunState.open(directory)
    where = state.input("device_ip") or "a device not yet found"
    mac = records.mac_of_run(directory) or state.input("device_mac")
    if mac and state.input("device_ip"):
        where += f", MAC {mac}"
    # The step after the last one done: a run of an earlier version has no
    # record of a step added since, which is not where it stopped.
    steps = [step for step, _, _ in routes.steps_of(state)]
    done = [index for index, step in enumerate(steps) if state.is_done(step)]
    pending = next((step for step in steps[done[-1] + 1 if done else 0:] if not state.is_done(step)), None)
    # Named as its stage line named it: no step ids on screen.
    stage = f"stage {steps.index(pending) + 1}, {routes.stage_name_of(state, pending)}" if pending else ""
    try:
        when = dt.datetime.fromisoformat(state.data.get("updated_utc", "")).astimezone()
        today = when.date() == dt.date.today()
        stamp = when.strftime("%H:%M today" if today else "%d %b %H:%M")
    except ValueError:
        stamp = "an unknown time"
    # Where it carries on: after a stop between stages the next one, after
    # one part way through the same stage again.  "Stopped at" the next
    # stage read as one stage late after a stop between them.
    details = [f"stopped {stamp}", f"carries on at {stage}"] if pending else ["finished", stamp]
    if pending and state.input("stopped_by") == "window":
        details.append("when its window was closed")
    if routes.past_the_point_of_no_return(state):
        details.append("past the point of no return")
    if not state.input("device_ip"):
        return f"a run that had not found its device yet ({', '.join(details)})"
    return f"the device at {where} ({', '.join(details)})"


def run_to_look_at(device_ip: str | None) -> str | None:
    """The run `status` and `cleanup` report on: the latest one of the device
    at `device_ip` when one is given, else the current run, else the latest."""
    directories = runs.run_directories()
    if device_ip:
        def at_address(directory: Path) -> bool:
            try:
                return RunState.open(directory).input("device_ip") == device_ip
            except (OSError, ValueError):
                return False
        matching = [d for d in directories if at_address(d)]
        return runs.run_name(matching[-1]) if matching else None
    return runs.read_current() or (runs.run_name(directories[-1]) if directories else None)


def wrote_to_device(directory: Path) -> bool:
    """Whether a run wrote to its device: a step that writes begun, unless it
    failed before writing."""
    try:
        state = RunState.open(directory)
    except (OSError, ValueError):
        return False
    steps = state.data.get("steps", {})
    return any(state.status(step) != PENDING
               and not (state.status(step) == FAILED and steps.get(step, {}).get("nothing_written"))
               for step in routes.family_of(state).DESTRUCTIVE_STEPS)


def superseded(directory: Path, directories: list[Path]) -> bool:
    """Whether a later run of the same device (in `directories`, oldest first)
    wrote to it: the device is no longer as this run left it, so carrying this
    one on would take it for what it was - seen asking for the Wi-Fi to
    leave a Tasmota a later run had already replaced."""
    def device(run: Path) -> str | None:
        try:
            return records.mac_of_run(run) or RunState.open(run).input("device_mac")
        except (OSError, ValueError):
            return None
    mac = device(directory)
    later = directories[directories.index(directory) + 1:]
    return bool(mac) and any(device(run) == mac and wrote_to_device(run) for run in later)


def start_new_run(device_ip: str | None) -> str:
    name = runs.new_run_name(device_ip)
    runs.remember_current(name)
    return name


# The answer to the unfinished-run menu.  The menu is answered before the run's
# log exists and leaves the screen once answered, so it is held here and
# written into the log when it opens - otherwise a device left part converted by
# "start a new device" leaves no trace of how.
RUN_CHOICE: list[str] = []


def log_run_choice() -> None:
    for line in RUN_CHOICE:
        output.say(line)
    RUN_CHOICE.clear()


def resolve_run_name(
    requested: str | None, force_new: bool, device_ip: str | None, ask: bool = False
) -> str:
    """Work out which conversion this is, without asking for an identifier.

    One in-flight operation, remembered in a well-known place.  With `ask` - a
    `run` - an unfinished conversion is offered back rather than resumed
    silently, so starting the next device is never mistaken for finishing the
    last.  Nothing opaque is ever typed by the operator.
    """
    if requested:
        runs.remember_current(requested)
        return requested
    if force_new:
        return start_new_run(device_ip)

    directories = runs.run_directories()
    unfinished = [d for d in directories if not run_is_complete(d) and not superseded(d, directories)]
    if device_ip and not ask:
        # Not asked, for one device: only a run of that device is carried on.
        unfinished = [d for d in unfinished if RunState.open(d).input("device_ip") == device_ip]
    if ask:
        # Offered back only once it has written to its device: one stopped
        # before - at a question, say, after "Convert another device" - loses
        # nothing by starting afresh, and asking about it would be noise.
        unfinished = [d for d in unfinished if wrote_to_device(d)]
    if not unfinished:
        return start_new_run(device_ip)

    pointer = runs.read_current()
    # Most recently touched first, so the likely answer is option 1.
    unfinished.sort(key=lambda d: (runs.run_name(d) != pointer, -d.stat().st_mtime))
    if not ask:
        chosen = unfinished[0]
        output.say(f"Continuing {describe_run(chosen)}.")
        RUN_CHOICE.append(f"Continuing {describe_run(chosen)}, the most recent unfinished run.")
        runs.remember_current(runs.run_name(chosen))
        return runs.run_name(chosen)

    while unfinished:
        # The menu leaves the screen once answered; after a removal it is
        # shown again, not stacked under the old one.
        output.transient_begin(before_start=True)
        any_past = any(routes.past_the_point_of_no_return(RunState.open(d)) for d in unfinished)
        keep = "one" if len(unfinished) == 1 else "ones"
        options = [f"Continue {describe_run(d)}" for d in unfinished] + [
            f"Start a new conversion (keep the unfinished {keep} for later)",
            "Remove an unfinished run",
        ]
        chosen = prompts.ask_choice(
            "A conversion did not finish last time. What now",
            options,
            help_lines=(
                ["A device past the point of no return no longer runs Sonoff: finish it soon."]
                if any_past else []
            ),
        )
        if chosen < len(unfinished):
            output.transient_end()
            RUN_CHOICE.append(f"Chosen at the start: continue {describe_run(unfinished[chosen])}.")
            runs.remember_current(runs.run_name(unfinished[chosen]))
            return runs.run_name(unfinished[chosen])
        if chosen == len(unfinished):
            output.transient_end()
            RUN_CHOICE.append("Chosen at the start: a new conversion. Left unfinished, on record:")
            RUN_CHOICE.extend(f"    {describe_run(d)}" for d in unfinished)
            return start_new_run(device_ip)
        target = unfinished[0] if len(unfinished) == 1 else unfinished[prompts.ask_choice(
            "Which run should be removed",
            [describe_run(d)[0].upper() + describe_run(d)[1:] for d in unfinished],
        )]
        if remove_run(target):
            unfinished.remove(target)
        output.transient_end()  # the menu and the removal: shown again, or gone
    return start_new_run(device_ip)


def remove_run(directory: Path) -> bool:
    """Delete one run's record, after removing any firewall rule it left behind."""
    state = RunState.open(directory)
    description = describe_run(directory)
    if routes.past_the_point_of_no_return(state):
        prompts.section("This device is part-converted", [
            "It no longer runs Sonoff's firmware; once its record is removed, this tool",
            "cannot finish it.",
        ])
        output.log_only("It will not show up as a new device either: finishing it would mean doing the "
                        "remaining steps by hand.")
    if not prompts.ask_yes_no(f"Remove the record of {description}", default=False):
        return False
    rules = [FirewallRule.from_json(item) for item in state.input("firewall_rules") or []]
    if rules:
        output.say("It left a temporary firewall rule letting that device connect to this")
        output.say("computer. Removing it needs administrator rights, so you may be asked")
        output.say("for this computer's password.")
        for line in firewall.emulator_password_hint():
            output.say(line)
        left = [rule for rule in rules if not hostsys.close_firewall(rule)]
        if left:
            output.transient_end()  # this has to stay on screen
        for rule in left:
            for line in hostsys.undo_commands(rule):
                output.tell("Remove it yourself: " + line)
    shutil.rmtree(directory)
    if runs.read_current() == runs.run_name(directory):
        runs.CURRENT_POINTER.unlink(missing_ok=True)
    output.say(f"Removed the record of {description}.")
    return True


def cmd_status(ctx: Context) -> int:
    output.say(f"Device:    {ctx.run}")
    output.say(f"Address:   {ctx.state.input('device_ip') or '(not set)'}")
    output.say(f"Manifest:  {ctx.manifest_path}")
    output.say(f"State:     {ctx.state.path}")
    available = routes.update_available(ctx)
    if available:
        output.say(f"Update:    {available}")
    output.say("")
    output.say(ctx.state.summary([(step, title) for step, title, _ in routes.STEPS]))
    captures = ctx.state.data.get("captures", {})
    if captures:
        output.say("")
        output.say("Captures:")
        for key, value in sorted(captures.items()):
            output.say(f"  {key}: {json.dumps(value)}")
    return 0


def cmd_step(ctx: Context, step: str) -> int:
    if step not in routes.STEP_INDEX:
        output.say(f"unknown step {step}; known: {', '.join(s for s, _, _ in routes.STEPS)}")
        return 2
    if ctx.dry_run and step not in routes.FAMILY.READ_ONLY_STEPS:
        # As a run's dry run stops before it (runner.run_steps).
        output.say(f"[dry-run] not doing {step} ({routes.STEP_INDEX[step][0]}): it writes.")
        return 0
    join.wait_while_device_busy(ctx)
    return runner.execute_with_retries(ctx, step)


def cmd_run(ctx: Context) -> int:
    # Asleep part way, this computer would leave the device waiting on it.
    output.say(f"This computer is {awake.keep_awake()} while the run works.")
    try:
        return runner.run_steps(ctx)
    finally:
        # However the run ends - finished, failed, quit, Ctrl-C or stopped.
        runs.forget_run_secrets(ctx.dir)
        awake.let_sleep()


def cmd_readout(ctx: Context, saved: Path | None) -> int:
    """Read out a device in the eWeLink account, any kind, to help add it; or
    send a readout kept from before (--send)."""
    if saved:
        output.tell(f"Sent, thank you! Its reference is {readout.send_saved(saved)}.")
        return 0
    ctx.readouts = []
    report_upload.announce(ctx)   # each readout sends its report (readout.run)
    runner.say_what_to_expect()
    output.say("Signing in to eWeLink to see which devices are in the account...")
    readout.lay_out_stages()
    readout.begin("account")
    try:
        devices = ctx.account_devices()
        if not devices:
            raise Abort("a readout needs the device in an eWeLink account, and none was found in this one")
    except BaseException:
        output.stage_end("stopped")
        raise
    output.stage_end("complete")
    given = ctx.args.device_ip
    if given:
        said = readout.run(ctx, readout.device_at(given, devices), given)
    elif getattr(ctx.args, "all", False):
        said = readout.run_all(ctx, devices)
    else:
        said = readout.read_out_chosen(ctx, devices)
    readout.end(ctx, said)
    return 0


def forget_all_secrets() -> list[str]:
    """Delete every run's secret files, and the bundle's stored password."""
    removed = [item for directory in runs.run_directories() for item in runs.forget_run_secrets(directory)]
    if forget_stored_password((ROOT / bundle.DEFAULT_MANIFEST).resolve()):
        removed.append("the Wi-Fi password in the startup firmware")
    return removed


def cmd_cleanup(ctx: Context) -> int:
    """Remove this run's temporary firewall rules, and every stored secret."""
    if firewall.recorded_rules(ctx):
        firewall.close_firewall_rules(ctx)
    else:
        output.say("This run added no firewall rules.")
    removed = forget_all_secrets()
    if removed:
        output.say("Removed stored secrets:")
        for item in removed:
            output.say(f"  {item}")
    else:
        output.say("No stored secrets were found.")
    if records.backups():
        output.say(f"Kept the Sonoff firmware backups in {runs.display_path(records.DEVICES)}/*/{records.BACKUP};")
        output.say("they hold the Wi-Fi password too. Delete them yourself if you will not want them.")
    return 0


def refused_for_another_run(command: str, ctx: Context) -> bool:
    """Another run working on this device - or, to clean up, on any - is asked
    about first: before this one's log, report, firewall rule or safety locks
    are touched, which are the other run's too. True when refused (and said)."""
    mac = ctx.state.input("device_mac")
    if command in ("run", "step") and mac:
        try:
            records.hold(mac, runs.run_name(ctx.dir))
        except records.DeviceBusy as busy:
            output.tell(f"Stopped before anything was done: {runs.busy_elsewhere(mac, busy)}.")
            return True
    if command == "cleanup":
        busy = records.busy()
        if busy:
            output.tell(f"Not cleaning up: a run is converting a device right now ({'; '.join(busy)}), and "
                 "its firewall rule and secrets are in use. Let it finish, or stop it first, then "
                 "run this again.")
            return True
    return False


def main() -> int:
    try:
        try:
            return _main()
        finally:
            # However it ended - finished, failed, quit or Ctrl-C - leave a blank
            # line between the last message and the shell's prompt.
            print(flush=True)
    except BrokenPipeError:
        # Piped into head or less, which stopped reading: quiet, as any command
        # is.  Python writes what is left as it exits, so that goes nowhere.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 1


def admin_password() -> str:
    """This computer's password for sudo (hostsys.password_asker): the one
    given in advance if sudo takes it, else asked for in this tool's own
    words, so every row on screen is its own.  An unattended run has nobody
    to ask, so it says what to give instead (OSError, as for a refused one)."""
    given = credentials.get("AUTOFLASH_SUDO_PASSWORD")
    if given is None and prompts.UNATTENDED:
        # Inside the emulator (tools/espressif/emulator) its password is known.
        given = os.environ.get("AUTOFLASH_EMULATOR_SUDO_PASSWORD")
    if given is not None:
        if hostsys.check_sudo_password(given) is None:
            return given
        output.tell("    The sudo password given in advance (AUTOFLASH_SUDO_PASSWORD) was not accepted.")
    if prompts.UNATTENDED:
        raise OSError("sudo needs this computer's password, and an unattended run cannot "
                      "ask for it: put it in .env as AUTOFLASH_SUDO_PASSWORD")
    return prompts.ask_admin_password()


def stopped_report(ctx: Context, code: int | None, ending: str | None) -> str | None:
    """How a run that did not finish ended, for its report - "failed" or
    "crashed" (report.report_ending) - when its report is to be asked for:
    not once it has been, and not for a run stopped before any stage began
    (a mistyped address, say), which says nothing of how the tool does.  A
    crash always: that is the tool's own."""
    reported = report.report_ending(code, ending)
    if not reported or ctx.reported:
        return None
    if ending != "crashed" and not getattr(ctx, "stage_begun", False):
        return None
    return reported


def issue_alone(ctx: Context, code: int | None, ending: str | None) -> bool:
    """Whether a run that failed with no report to offer (stopped_report:
    before any stage began) is still given the way to open an issue, which a
    report's offer carries. Never one stopped by choice, nor one that finished."""
    return (report.report_ending(code, ending) == "failed" and not ctx.reported
            and stopped_report(ctx, code, ending) is None)


def stopped_by(ctx: Context, code: int | None, ending: str | None) -> str | None:
    """How the person stopped the run, for its report (report_data.STOPPED_BY),
    or None for one they did not stop, or stopped before any stage began
    (which run, say), as a failure that early sends none either."""
    if ending != "cancelled" and code != 130:
        return None
    if not getattr(ctx, "stage_begun", False) or getattr(ctx, "reported", False):
        return None
    if ending == "cancelled":   # Ctrl-C, or the window closed
        return "window-closed" if ctx.state.input("stopped_by") == "window" else "ctrl-c"
    return getattr(ctx, "stopped_by", None) or "quit"


def handle_signals() -> None:
    """Being stopped, or the terminal being closed, must still run the cleanup
    in the finally blocks: the stored secrets and the firewall rule.  SIGPIPE
    is left as Python sets it, so a device that hangs up on a write raises
    BrokenPipeError, which the code talking to it handles, rather than ending
    the run with no cleanup (main handles a closed pipe on the output)."""
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    # Ctrl-C asks before it stops a run part way; the window closing stops it,
    # and is remembered (tools/autoflash/interrupt.py).
    interrupt.install()


def _main() -> int:
    hostsys.password_asker = admin_password
    handle_signals()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"Autoflash {__version__}")
    parser.add_argument("command", choices=["run", "status", "step", "list", "cleanup", "readout", "email"])
    parser.add_argument("step_id", nargs="?")
    parser.add_argument("--run", help=argparse.SUPPRESS)  # power-user escape hatch; never needed normally
    parser.add_argument("--new", action="store_true", help="start a fresh run, never resume")
    parser.add_argument("--manifest", type=Path, default=bundle.DEFAULT_MANIFEST)
    parser.add_argument("--device-ip")
    parser.add_argument(
        "--listen-ip",
        help="workstation address the device fetches from; derived from the device's "
        "address when omitted",
    )
    parser.add_argument("--email", help="the eWeLink account the device is in: its email, or its phone "
                        "number with the country code (+44 7700 900123)")
    parser.add_argument("--web-user", help="Tasmota web username, for a Tasmota built with another than admin")
    parser.add_argument("--web-password",
                        help="the device's Tasmota web password, if it has one (asked for otherwise; never stored)")
    parser.add_argument("--esphome-ota-password",
                        help="the device's ESPHome OTA update password, if it has one (asked for otherwise; never stored)")
    parser.add_argument("--esphome-web-user",
                        help="the user name of the device's ESPHome web login, if it has one (with --esphome-web-password)")
    parser.add_argument("--esphome-web-password",
                        help="the password of the device's ESPHome web login (asked for otherwise; never stored)")
    parser.add_argument("--esphome-api-key",
                        help="the device's ESPHome API encryption key, if it has one: for its Wi-Fi details, "
                        "and its updates where they are encrypted (asked for otherwise; never stored)")
    parser.add_argument("--send", type=Path, metavar="FILE",
                        help="with readout: send a readout kept from before (readout.tar in its run)")
    parser.add_argument("--all", action="store_true",
                        help="with readout: every device in the eWeLink account, each sent on its own, "
                        "without asking which (as its first choice does)")
    parser.add_argument("--wifi", action="store_true",
                        help="type the Wi-Fi details instead of reading them from the device")
    parser.add_argument("--no-backup", action="store_true",
                        help="keep no backup of the Sonoff firmware (it holds the Wi-Fi password)")
    parser.add_argument("--firmware", choices=list(routes.FIRMWARES),
                        help="which firmware to install, instead of being asked at the start")
    parser.add_argument("--replace-own-build", action="store_true",
                        help="install this project's ESPHome over a build of its config made elsewhere, such as "
                        "by an ESPHome builder, losing that build's config changes and any API key")
    parser.add_argument("--supla", metavar="PATH",
                        help="install Supla GUI-Generic from this file, as gui-generic-builder-new.supla.io gave it "
                        "(the .zip, as it is)")
    # An ESPHome built by hand (firmware.ota.bin), instead of the published one.
    parser.add_argument("--esphome", metavar="PATH", help=argparse.SUPPRESS)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-report", action="store_true",
                        help="send the project no report of how the run went "
                             "(docs/what-the-tool-sends.md says what one holds)")
    parser.add_argument("--unattended", action="store_true",
                        help="nobody at the keyboard: go ahead past the point of no return "
                        "without typing PROCEED, take the eWeLink details from .env, and "
                        "stop at any question that has no safe answer")
    parser.add_argument("--env-file", type=Path,
                        help="answers given in advance (default: .env, if there is one); "
                        "see .env.example")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="show every action as it happens (it is all in the run's log either way)")
    parser.add_argument("--simulate-backup-failure", action="store_true",
                        help=argparse.SUPPRESS)  # testing: the backup's reads fail until it asks what now
    parser.add_argument("--simulate-no-sonoff-firmware", action="store_true",
                        help=argparse.SUPPRESS)  # testing: Sonoff's firmware not to be had, without a backup
    parser.add_argument("--simulate-no-identity", action="store_true",
                        help=argparse.SUPPRESS)  # testing: the device keeps no Sonoff identity, in no account
    parser.add_argument("--simulate-bad-image", nargs="?", const="B2", metavar="STEP",
                        help=argparse.SUPPRESS)  # testing: the image STEP sends is refused by the device
    parser.add_argument("--simulate-unknown-table", action="store_true",
                        help=argparse.SUPPRESS)  # testing: a Beken official file's table is not one known
    parser.add_argument("--bridge", help=argparse.SUPPRESS)  # research: archive/docs/reference/automatic-wifi.md
    args = parser.parse_args()
    if args.replace_own_build and (args.esphome or args.firmware not in (None, "esphome")):
        parser.error("--replace-own-build installs the published ESPHome, so goes with no other firmware")
    if args.supla and (args.esphome or args.replace_own_build or args.firmware not in (None, "supla")):
        parser.error("--supla installs Supla, so goes with no other firmware")
    prompts.UNATTENDED = args.unattended
    installer.SIMULATED_BACKUP_FAILURES = 2 * installer.BACKUP_ROUNDS if args.simulate_backup_failure else 0
    sonoff_firmware.SIMULATE_NONE = args.simulate_no_sonoff_firmware
    identity.SIMULATE_NONE = args.simulate_no_identity
    spoil, _, times = (args.simulate_bad_image or "").partition(":")
    for family_images in (beken_images, espressif_images):
        family_images.SPOIL_STEP, family_images.SPOIL_TIMES = spoil or None, int(times or 1)
    beken_images.UNKNOWN_TABLE = args.simulate_unknown_table
    if args.command == "email":
        return signup.change()

    try:
        given = credentials.load(args.env_file)
        try:
            records.take_over_earlier_names()
        except records.EarlierNames as exc:
            raise Abort(str(exc)) from exc
        if args.run:
            args.run = records.earlier_run_name(args.run)
        adopt_older_runs()
        if args.command == "run" and not args.unattended:
            output.start_at_top()
        if args.command == "run" and not args.verbose and not args.unattended:
            prompts.wait_for_taller_window()  # before anything else is shown
        if args.command == "list":
            args.run = args.run or "-"
        elif args.command in ("status", "cleanup") and not args.run:
            # Report on the latest run; never start a new one just to look.
            args.run = run_to_look_at(args.device_ip)
            if not args.run:
                output.say(f"No conversion recorded for the device at {args.device_ip}."
                           if args.device_ip else "No conversions recorded yet.")
                return 0
        elif args.command == "readout":
            args.run = start_new_run(args.device_ip)  # never a conversion carried on
        else:
            args.run = resolve_run_name(args.run, args.new, args.device_ip,
                                        ask=args.command == "run" and not args.unattended)
        ctx = Context(args)
        routes.apply_target(routes.recorded_firmware(ctx.state), routes.route_source(ctx.state),
                            routes.family_of(ctx.state))  # a run settles it at its start
    except (QuitRun, KeyboardInterrupt):
        # q, or Ctrl-C, at a question before the run starts: which run, say.
        output.say("Stopped. Nothing was changed.")
        return 130
    except (Abort, ValueError, OSError) as exc:
        output.say(f"REFUSING: {exc}")
        return 2

    if refused_for_another_run(args.command, ctx):
        return 2

    if args.command == "list":
        directories = runs.run_directories()
        if not directories:
            output.say("No conversions recorded yet.")
            return 0
        for directory in directories:
            state = RunState.open(directory)
            steps = routes.steps_of(state)
            done = sum(1 for step, _, _ in steps if state.is_done(step))
            mark = "verified" if run_is_complete(directory) else "in progress"
            output.say(f"  {runs.run_name(directory):<55} {done}/{len(steps)} steps  {mark}"
                f"  {state.input('device_ip') or ''}")
        for backup in records.backups():
            output.say(f"  Sonoff backup: {backup.describe()} in {runs.display_path(backup.directory)}")
        return 0

    if args.command == "status":
        return cmd_status(ctx)
    if args.command == "cleanup":
        return cmd_cleanup(ctx)
    if args.command == "step":
        if not args.step_id:
            output.say("step requires a step id")
            return 2
        output.start(ctx.dir / "run.log", verbose=args.verbose)
        code = None
        try:
            code = cmd_step(ctx, args.step_id)
            return code
        finally:
            if code == 130:
                firewall.say_quit(ctx)
            else:
                firewall.release_firewall(ctx)
            output.finish()
    code = read_out(ctx) if args.command == "readout" else convert(ctx, given)
    while code == 0 and not args.send:
        command = what_next(args.command)
        if command is None:
            break
        args.command = command
        ctx = next_device(args, ctx)
        # Either at the top of the window: a readout under the last device's
        # left its "Readout saved in" above the next one's.
        output.start_at_top()
        code = read_out(ctx) if command == "readout" else convert(ctx, given)
    signup.ask_on_exit()
    return code


def what_next(last: str) -> str | None:
    """After a conversion or a readout that finished, what is next, in the
    same sitting: another device converted, or one read out - "run" or
    "readout" - with no command to type again.  None for nothing: q, or
    Ctrl-C.  Asked only of someone at the keyboard."""
    if prompts.UNATTENDED:
        return None
    output.tell("")
    options = (["Convert another device", "Read out a device"] if last == "run"
               else ["Convert a device", "Read out another device"])
    try:
        return ("run", "readout")[prompts.ask_choice("What next", options)]
    except QuitRun:
        return None
    except KeyboardInterrupt:
        signup.stopped()
        return None


def next_device(args: argparse.Namespace, previous: Context) -> Context:
    """The next device's run: a new one, found afresh - not the address the
    last was given - with the eWeLink account the last one signed in to."""
    args.run, args.device_ip, args.new = start_new_run(None), None, True
    ctx = Context(args)
    ctx.keep_sign_in(previous)
    routes.apply_target(routes.recorded_firmware(ctx.state), routes.route_source(ctx.state),
                        routes.family_of(ctx.state))
    return ctx


def read_out(ctx: Context) -> int:
    """A readout, from its log's start to its end (cmd_readout)."""
    output.start(ctx.dir / "run.log", verbose=ctx.args.verbose)
    try:
        return cmd_readout(ctx, ctx.args.send)
    except QuitRun:
        output.tell(prompts.STOPPED_AS_ASKED)
        return 130
    except KeyboardInterrupt:
        signup.stopped()
        output.tell(readout.stopped_by_ctrl_c(ctx))
        return 130
    except (Abort, OSError, ValueError) as exc:
        output.tell(f"FAILED: {exc}")
        # One that reached a device ends with its report, as a conversion that
        # failed does: each device's ending so far, then the way to open an issue.
        if readout.readouts(ctx):
            readout.end(ctx, "", failed=True)
        return 1
    finally:
        firewall.release_firewall(ctx)
        runs.forget_run_secrets(ctx.dir)
        output.finish()


def convert(ctx: Context, given: list[str]) -> int:
    """A conversion, from its log's start to its end (cmd_run), its report
    asked for if it stopped."""
    args = ctx.args
    output.start(ctx.dir / "run.log", verbose=args.verbose)
    log_run_choice()
    if args.unattended:
        output.say("Unattended: PROCEED is taken as typed, yes/no questions take their "
            "default, and any other question stops the run.")
    if given:
        output.say(f"Given in advance ({runs.display_path(args.env_file or credentials.ENV_FILE)}): "
            f"{', '.join(given)}")
    if credentials.renamed:
        output.say("Old names, still read for now - rename them: " + ", ".join(
            f"{old} is now {credentials.RENAMED[old]}" for old in credentials.renamed))
    code = None  # 130 only when the operator chose to quit, not for Ctrl-C
    ending = None  # "crashed", or "cancelled" by the operator: see run_result()
    report_upload.begin(ctx)
    try:
        code = cmd_run(ctx)
        return code
    except QuitRun:
        output.stage_end("stopped")
        code = 130
        return code
    except Exception as exc:  # noqa: BLE001 - recorded and explained, not a bare traceback
        message = runner.unexpected(exc)
        ctx.last_failure = message
        ctx.last_failure_class = None
        # Where it happened: the step under way, or none - between steps, or
        # after the last, not the first one left to do.
        ctx.last_failed_step = getattr(ctx, "running_step", None) or ""
        ctx.crash_site = runner.crash_site(exc)
        output.stage_end("stopped")
        output.tell(f"FAILED: {message}")
        runner.tell_how_to_carry_on()
        code, ending = 1, "crashed"
        return code
    except KeyboardInterrupt:
        signup.stopped()
        closed = interrupt.window_closed()
        try:
            ctx.state.set_input("stopped_by", "window" if closed else "ctrl-c")
        except Exception:  # noqa: BLE001 - nothing must stop the run stopping
            pass
        if closed:
            output.log_only("The run's window was closed: stopped at once.")
        output.stage_end("stopped")
        output.tell("")
        # Every step checks the device first.
        output.tell("Stopped with Ctrl-C. Running again is safe: it picks up where it left off.")
        if runner.stopped_mid_send(ctx):
            output.tell("It was mid-update through eWeLink: wait about 3 minutes before running again.")
        ending = "cancelled"
        return 130
    finally:
        if code == 130:
            firewall.say_quit(ctx)
        else:
            firewall.release_firewall(ctx)
        reported = stopped_report(ctx, code, ending)
        if reported:
            try:
                report.invite_stopped_report(ctx, reported)
            except Exception as exc:  # noqa: BLE001 - never let the report hide how it ended
                output.log_only(f"The report could not be made: {type(exc).__name__}: {exc}")
        elif issue_alone(ctx, code, ending):
            report.ask_for_an_issue()
        elif how := stopped_by(ctx, code, ending):
            try:
                report.send_cancelled_report(ctx, how)
            except Exception as exc:  # noqa: BLE001 - never let the report hide how it ended
                output.log_only(f"The report could not be made: {type(exc).__name__}: {exc}")
        output.finish()


if __name__ == "__main__":
    raise SystemExit(main())
