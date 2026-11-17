"""Running the route: each step in turn, with its retries, its evidence when
it was cut off, and what is said when the run stops.
"""

from __future__ import annotations

import errno
import re
import time
import traceback
from pathlib import Path
from typing import TYPE_CHECKING

from tools.autoflash import (
    __version__,
    device,
    errors,
    esphome,
    firewall,
    hostsys,
    interrupt,
    join,
    kinds,
    names,
    output,
    prompts,
    report,
    restart,
    routes,
    runs,
    safety_locks,
    unlock,
)
from tools.autoflash.device import DeviceError
from tools.autoflash.errors import Abort, NothingWritten, StoppedPartWay
from tools.autoflash.phases import PhaseError, DeviceNeverConnected
from tools.autoflash.prompts import QuitRun
from tools.autoflash.routes import update
from tools.autoflash.routes.espressif import tasmota
from tools.autoflash.runs import ROOT
from tools.autoflash.state import ATTEMPTED, DONE, FAILED, PENDING, RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context

def asked_of_the_run(state: RunState) -> tuple[list[str], int]:
    """What a run should never need, so far: why this computer was sent to a
    device's own network each time (join.note_own_network), and how many
    times the device was switched off and on (arrival.power_cycle)."""
    joins = state.input(join.OWN_NETWORK)
    return list(joins) if isinstance(joins, list) else [], sum((state.input("power_cycles") or {}).values())


def execute(ctx: Context, step: str) -> int:
    ctx.last_runner = None
    started, waited = time.monotonic(), output.waited()
    joined, cycled = asked_of_the_run(ctx.state)
    # The step under way, for a crash that escapes it to say where it was.
    ctx.running_step = step
    try:
        code = _execute(ctx, step)
        ctx.running_step = None   # done with it: an error after it is none of its
        return code
    finally:
        # Each kept against the step it happened in, over all its tries:
        # where they keep being needed (report_data.stages). Of the own
        # networks, the first one's reason: what brought the step to one.
        joins, cycles = asked_of_the_run(ctx.state)
        captures = ctx.state.data.get("captures", {})
        if len(joins) > len(joined) and f"{step}.{join.OWN_NETWORK}" not in captures:
            ctx.state.capture(f"{step}.{join.OWN_NETWORK}", joins[len(joined)])
        if cycles > cycled:
            ctx.state.capture(f"{step}.power_cycles", (captures.get(f"{step}.power_cycles") or 0) + cycles - cycled)
        # For the report, however the stage ended: its time without the
        # keyboard waits, and how long a Berry phase kept the device busy.
        ctx.state.capture(f"{step}.seconds",
                          round(time.monotonic() - started - (output.waited() - waited)))
        # And the keyboard's own: where people take their time.
        ctx.state.capture(f"{step}.waited", round(output.waited() - waited))
        busy = getattr(ctx.last_runner, "busy_seconds", None)
        if busy is not None:
            # The longest of its tries: how near the device came to stopping.
            before = ctx.state.data.get("captures", {}).get(f"{step}.device_busy_seconds")
            ctx.state.capture(f"{step}.device_busy_seconds",
                              max(busy, before) if isinstance(before, (int, float)) else busy)


def _execute(ctx: Context, step: str) -> int:
    title, handler = routes.STEP_INDEX[step]
    routes.begin_stage(step)
    ctx.stage_begun = True   # a report is worth asking for (cli.main)
    output.say(f"{step}  {title}")
    output.event("began", step=step)
    try:
        # Its errors are the step's: seen in the fault suite, an Abort from
        # it crashed the run.
        set_up_if_erased(ctx, step)
        if step in routes.FAMILY.DESTRUCTIVE_STEPS:
            ctx.state.mark(step, ATTEMPTED, nothing_written=False)
        captured = handler(ctx)
    except QuitRun:
        output.stage_end("stopped")
        return 130  # main() says so, once it has cleaned up
    except errors.GoBack as back:
        captures = ctx.state.data.get("captures", {})
        times = int(captures.get(f"{back.step}.sent_again") or 0) + 1
        host = ctx.state.input("device_ip")
        if times > GO_BACK_LIMIT and not captures.get(f"{back.step}.restarted") and host \
                and restart.restart(host):
            # Once for a step: the device, answering but not taking it, restarted
            # as switching it off and on would, and the step sent afresh.
            ctx.state.capture(f"{back.step}.restarted", True)
            ctx.state.capture(f"{back.step}.sent_again", 0)
            ctx.state.mark(step, PENDING)
            ctx.state.mark(back.step, PENDING, note=back.reason)
            output.stage_end(back.outcome)
            output.log_only(f"{back.step} was sent {times - 1} times: the device is restarted, and "
                            f"{back.step} done again.")
            output.tell(f"{back.reason}: restarted the device; doing stage {routes.stage_number(back.step)} again.")
            output.event("again", step=back.step, restarted=True)
            ctx.go_back = back.step
            return 0
        if times > GO_BACK_LIMIT:
            ctx.state.mark(step, FAILED, error=back.reason)
            output.stage_end("stopped")
            message = (f"{back.reason}, after it was sent {times} times. Switch the device off and on, "
                       "then run the same command again")
            ctx.last_failure, ctx.last_failed_step = message, step
            ctx.last_failure_class = None
            output.tell(f"FAILED: {message}")
            return 1
        ctx.state.capture(f"{back.step}.sent_again", times)
        ctx.state.mark(step, PENDING)
        ctx.state.mark(back.step, PENDING, note=back.reason)
        output.stage_end(back.outcome)
        output.log_only(f"Doing {back.step} again.")
        output.tell(f"{back.reason}: doing stage {routes.stage_number(back.step)} again.")
        output.event("again", step=back.step, restarted=False)
        ctx.go_back = back.step
        return 0
    except errors.Finished as done:
        ctx.state.mark(step, FAILED, error=str(done), nothing_written=True)
        output.stage_end("complete")
        if str(done):   # a readout the run turned into has said its own (readout.from_run)
            output.tell("")
            output.tell(str(done))
        ctx.finished = done
        return 0
    except DeviceNeverConnected as exc:
        ctx.state.mark(step, FAILED, error=str(exc))
        output.stage_end("stopped")
        prompts.section(f"Stage {routes.stage_number(step)} could not start", firewall.firewall_hint(ctx))
        ctx.last_failure = str(exc)
        ctx.last_failure_class = None
        ctx.last_failed_step = step
        output.tell(f"FAILED: {exc}")
        return 1
    except (Abort, DeviceError, ValueError, PhaseError) as exc:
        # A phase that ended with no result may have got as far as writing:
        # where the device can show whether the step finished, the step stays
        # "attempted" and the next run asks it.
        if not (isinstance(exc, (PhaseError, StoppedPartWay)) and step in routes.FAMILY.ATTEMPTED_EVIDENCE):
            ctx.state.mark(step, FAILED, error=str(exc),
                           **({"nothing_written": True} if isinstance(exc, NothingWritten) else {}))
        output.stage_end("stopped")
        ctx.last_failure = str(exc)
        ctx.last_failure_class = getattr(exc, "failure", None)
        ctx.last_failed_step = step
        ctx.crash_site = None
        output.tell(f"FAILED: {exc}")
        return 1
    except Exception as exc:  # noqa: BLE001 - recorded and explained, not a bare traceback
        # The device or the network dropping a request part way - a reset, a
        # reply cut off, no answer - is no surprise, and worth another go.
        message = connection_lost(exc) if isinstance(exc, errors.NETWORK_ERRORS) else unexpected(exc)
        # As above: whether it got as far as writing is for the device to say.
        if step not in routes.FAMILY.ATTEMPTED_EVIDENCE:
            ctx.state.mark(step, FAILED, error=message)
        output.stage_end("stopped")
        ctx.last_failure = message
        ctx.last_failure_class = None
        ctx.last_failed_step = step
        # Where in the tool's code, for its report: no surprise from the network.
        ctx.crash_site = None if isinstance(exc, errors.NETWORK_ERRORS) else crash_site(exc)
        output.tell(f"FAILED: {message}")
        return 1
    # A step may end its stage otherwise: "skipped" or "already done".
    outcome = captured.pop("stage_outcome", "complete") if captured else "complete"
    ctx.state.mark(step, DONE, **{"result": captured})
    for key, value in (captured or {}).items():
        ctx.state.capture(f"{step}.{key}", value)
    output.stage_end(outcome)
    output.say(f"{step} done")
    output.event("done", step=step)
    return 0


ATTEMPTS = 3
RETRY_PAUSE = 10.0
# A step a later one found had not taken on the device (errors.GoBack) is done
# again this many times in a run; one that still does not take stops the run.
GO_BACK_LIMIT = 2
# A router restart takes a minute or two.  The device gives up on a step within
# about a minute - busier for longer, it stops answering for good (its energy
# meter's readings pile up) - so this computer does the waiting, between tries.
DEVICE_BACK_WAIT = 300.0


def execute_with_retries(ctx: Context, step: str) -> int:
    for attempt in range(1, ATTEMPTS + 1):
        mark = output.mark()
        code = execute(ctx, step)
        if step not in ("E3", "B4"):  # they count their own tries (COMMIT_ATTEMPTS, BACKUP_TRIES)
            ctx.state.capture(f"{step}.attempts", attempt)
        if code != 1 or step not in routes.FAMILY.RETRY_STEPS or attempt == ATTEMPTS:
            return code
        # A step the device is asked about later (ATTEMPTED_EVIDENCE) keeps no
        # error in its record, so what it failed with is taken from this run.
        error = str(ctx.state.data.get("steps", {}).get(step, {}).get("error", "")
                    or ctx.last_failure)
        if not errors.TRANSIENT_RE.search(error):
            return code
        output.tell(f"    That may have been the network - trying again in {RETRY_PAUSE:.0f}s "
             f"(attempt {attempt + 1} of {ATTEMPTS}).")
        time.sleep(RETRY_PAUSE)
        wait_for_device_back(ctx, step)
        # The try that failed off the screen, and the note that it is tried
        # again: the stage keeps one line, as one that passed first time does.
        # The log keeps them, and the report the number of tries.
        output.rewind(mark)
        output.retrying()
    return code


DEVICE_BACK_HINT_AFTER = 45.0
ERASED_LOOK_EVERY = 30.0
# Well over the time a Tasmota restarting onto the Wi-Fi takes to answer there.
ERASED_SETTLE = 60.0


def answers_within(device: device.Device, seconds: float) -> bool:
    """Whether the device answers within `seconds`, asked every few."""
    started = time.monotonic()
    while (elapsed := time.monotonic() - started) < seconds:
        output.progress(f"waiting for the device, in case it is starting up... {elapsed:.0f}s")
        if device.answers(timeout=3.0):
            output.progress_clear()
            return True
        time.sleep(2.0)
    output.progress_clear()
    return False


def set_up_if_erased(ctx: Context, step: str) -> bool:
    """A Tasmota not on the Wi-Fi but on its own setup network has lost its
    settings - switched off and on 7 times, say, or an earlier run's advice
    taken late - so it is set up through that network again; and, past F4, as
    its template went with them, F4 sets it up again too.  True when it was:
    the step then goes on with the device back.  Seen in the fault suite: G1
    asked for Wi-Fi details for a device it could not reach, run after run."""
    # Every route reaches these steps on such a Tasmota: from stock through D1,
    # from ESPHome or back to Sonoff through the mover and the startup firmware.
    if step not in routes.FAMILY.ERASED_CHECK_STEPS or not ctx.state.input("device_mac"):
        return False
    if ctx.state.input("device_ip") and ctx.device.answers(timeout=3.0):
        return False
    network = prompts.find_ssid(names.setup_networks(ctx), timeout=10)
    if not network:
        return False
    # A Tasmota starting up shows a network for a moment - ESP_ and its
    # access-point MAC - before it is on the Wi-Fi.  Seen in the fault suite:
    # one D1 had just restarted onto the Wi-Fi was taken for one that lost its
    # settings, and the run waited 15 minutes for a network gone at once.  So
    # it is given the time to start; not answering then, with its network
    # still there, it has lost them.
    if ctx.state.input("device_ip") and answers_within(ctx.device, ERASED_SETTLE):
        return False
    network = prompts.find_ssid(names.setup_networks(ctx), timeout=10)
    if not network:
        return False
    output.progress_clear()
    output.tell("    Its settings were erased: setting it up again through its own network.")
    join.configure_device_wifi(ctx, network, join.SETTINGS_LOST)
    if ctx.state.is_done("F4"):
        device = ctx.device
        device.wait_reachable(timeout=120)
        template = kinds.of_run(ctx.state).tasmota_template()
        if not tasmota.template_in_place(device, template, names.final_name(ctx, "tasmota")):
            output.tell("    Its template went with its settings: Tasmota is set up again.")
            ctx.state.capture("F4.again_after_erase", True)
            tasmota.step_configure(ctx)
    return True


def wait_for_device_back(ctx: Context, step: str | None = None) -> None:
    """Before a step is tried again, wait for a device running Tasmota to answer:
    after a router restart it is back within a minute or two."""
    if not ctx.state.input("device_ip") or not ctx.state.is_done("D1"):
        return
    device = ctx.device
    host = ctx.state.input("device_ip")
    esphome_route = runs.run_firmware(ctx.state) == "esphome"

    def back() -> bool:
        # On the way to ESPHome the device may already have switched to it.
        return device.answers(timeout=3.0) or (esphome_route and esphome.is_esphome(host, 3.0))

    if back():
        return
    output.tell("    The device is not answering yet - waiting for it.")
    started = time.monotonic()
    looked = started
    hinted = False
    while (now := time.monotonic()) - started < DEVICE_BACK_WAIT:
        # Part way through, the device runs a Tasmota holding your Wi-Fi details,
        # which keeps trying them and never opens a network of its own - all but
        # F4's official Tasmota, which may not have them yet (F4 looks for its
        # setup network itself), and one that has lost its settings, looked for.
        if not hinted and step != "F4" and now - started > DEVICE_BACK_HINT_AFTER:
            output.progress_clear()
            output.tell("    Waiting for it to rejoin your Wi-Fi - check your router and its power.")
            hinted = True
        if step and now - looked >= ERASED_LOOK_EVERY:
            looked = now
            if set_up_if_erased(ctx, step):
                return
        output.progress(f"waiting for the device... {now - started:.0f}s")
        if back():
            output.progress_clear()
            output.tell("    It is answering again.")
            return
        time.sleep(3.0)
    output.progress_clear()
    output.tell(f"    It has not answered for {DEVICE_BACK_WAIT / 60:.0f} minutes - trying anyway.")


def confirm_skip(ctx: Context, step: str, title: str) -> bool:
    """Trust the checklist only as far as the device agrees with it: a step the
    device disagrees with is done again, unasked, as an interrupted step not
    shown finished is - every step checks the device before it writes anything,
    and refuses if it is not as expected, and the device's own record of what a
    step wrote changes under no later step.  Asked, nobody could answer better."""
    verifier = routes.FAMILY.STEP_VERIFIERS.get(step)
    if verifier is None:
        output.say(f"[skip] {step} {title} (recorded as done)")
        return True
    try:
        ok, message = verifier(ctx)
    except (DeviceError, Abort, OSError, ValueError) as exc:
        output.say(f"[skip] {step} {title} (recorded as done; could not re-check: {exc})")
        return True
    if ok:
        output.say(f"[skip] {step} {title} (done, and the device still agrees)")
        return True
    output.say(f"{step} is recorded as done, but {message}: doing it again.")
    return False


def stopped_mid_send(ctx: Context) -> bool:
    """Whether a run stopped while it sent the device its first firmware
    through eWeLink, the go-ahead given: the device may have the command. One
    stopped so stays away from eWeLink a few minutes (seen on the real cloud),
    and a run started at once does not find it."""
    return (ctx.state.status(routes.FAMILY.POINT_OF_NO_RETURN) == ATTEMPTED
            and bool(getattr(ctx, "consented", False)))


def tell_how_to_carry_on() -> None:
    output.tell("")
    if hostsys.IS_WINDOWS:
        output.tell("Fix the cause, then carry on with the same command again:")
        output.tell("    start.cmd (double-click it, or .\\start.cmd in a terminal)")
    else:
        output.tell("Fix the cause, then run the same command again: sh start.sh")
    # The run's log is named once, under the way to open an issue that follows
    # (report.ask_for_an_issue).


def connection_lost(exc: BaseException) -> str:
    """A request the network or the device dropped: in full in the log, in a line on screen."""
    output.log_only(traceback.format_exc().rstrip())
    return f"the connection to the device was lost ({type(exc).__name__}: {exc})"


def crash_site(exc: BaseException) -> str | None:
    """Where in this tool's own code an unexpected error came from, for the
    run's report: its kind and the last of the tool's lines it passed
    ("KeyError at tools/autoflash/report.py:212") - code alone, never a value."""
    tools = (runs.ROOT / "tools").resolve()
    site = None
    for frame in traceback.extract_tb(exc.__traceback__):
        try:
            path = Path(frame.filename).resolve().relative_to(tools.parent)
        except ValueError:
            continue
        if path.parts[0] == "tools" and frame.lineno:
            site = f"{path.as_posix()}:{frame.lineno}"
    kind = type(exc).__name__
    return f"{kind} at {site}" if site and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,59}", kind) else None


def unexpected(exc: BaseException) -> str:
    """An error nothing here anticipated: in full in the log, in a line on screen."""
    output.log_only(traceback.format_exc().rstrip())
    # No surprise, and only the person can put it right. Seen in the fault
    # suite, writing the backup.
    if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT, errno.EFBIG):
        return f"this computer has no room to save what the run needs ({exc}): free some disk space"
    return f"something unexpected went wrong: {type(exc).__name__}: {exc}"


def stop_here_after_ctrl_c(step: str) -> bool:
    """At the end of a stage, a Ctrl-C pressed during it is asked about: the
    run stops here only if the person says so - by itself it carries on."""
    if not interrupt.pending():
        return False
    if not prompts.ask_after_ctrl_c([
        "Stopping here is safe: this stage is done and recorded.",
    ], "Carrying on to the next stage"):
        output.log_only(f"Stopped after {step}, as asked after Ctrl-C.")
        # Said on the screen too, as a Ctrl-C at any other moment is: the
        # person is told how to carry on, not only that it was tidied up.
        output.tell("")
        output.tell(f"{prompts.STOPPED_AS_ASKED} Run this again to carry on from the next stage.")
        return True
    output.say(f"Carrying on after {step}: Ctrl-C was not confirmed.")
    return False


def rerun_on_tasmota(ctx: Context) -> int:
    """A device kept on Tasmota: its meter calibrated, as F4 would have, if it
    is not - a device converted before the tool did, or whose calibration was
    lost with its settings.  One calibrated by hand is left as it is."""
    try:
        unlock.unlock_tasmota(ctx)
    except Abort as refused:
        output.tell(f"Stopped before anything was done: {refused}.")
        return 2
    output.tell("Checking its meter's calibration...")
    tasmota.calibrate_tasmota_meter(ctx, show=True)
    return 0


def bring_up_to_date(ctx: Context) -> bool:
    """Whether a device the run's route has just taken to its firmware goes on
    to that firmware's update, in the same run: when it runs an earlier
    release than this version installs - begun by an earlier version, which
    had installed its own release before the run stopped (official Tasmota,
    from F2 on, must stay the release it began with to the end), or a run
    stopped in an update it had begun.  The update's steps are added to the
    route (routes.continue_with), after what the family needs first
    (PREPARE_UPDATE: Tasmota's bundle built again).  Nothing is asked: the
    firmware was chosen, and this release is what choosing it gets."""
    firmware = routes.ROUTE[0] or runs.run_firmware(ctx.state)
    steps = getattr(routes.FAMILY, "UPDATE_STEPS", {}).get(firmware)
    if not steps or ctx.dry_run or any(step in routes.STEP_INDEX for step, _, _ in steps):
        return False
    published = routes.FAMILY.published_version(kinds.of_run(ctx.state), firmware)
    try:
        if not update.outdated(ctx, firmware, published):
            return False
    except (DeviceError, OSError, RuntimeError, ValueError):
        return False   # not answering as it: the conversion's own checks have said it is there
    prepare = getattr(routes.FAMILY, "PREPARE_UPDATE", {}).get(firmware)
    if prepare is not None and not prepare(ctx):
        return False
    old, new = update.versions(ctx)
    output.say(f"The device has {routes.FIRMWARES[firmware]} {old}: updating it to {new}, "
               "the release this version installs.")
    routes.continue_with(steps)
    return True


# Two rows, each within an 80-column window past the margin.
WHAT_TO_EXPECT = ("Usually under 10 minutes; some stages show little for a minute or two.",
                  "Keep the device powered and this computer on until the last stage completes.")


def say_what_to_expect() -> None:
    """Above a run's first stage, a conversion's or a readout's (cli.cmd_readout):
    how long it takes, what its stages are like and what to leave alone meanwhile."""
    output.tell("")
    for row in WHAT_TO_EXPECT:
        output.tell(row)
    output.tell("")


def run_steps(ctx: Context) -> int:
    started = time.monotonic()
    safety_locks.restore_left_locks(ROOT)  # a resumed run skips A5, which looks too
    say_what_to_expect()
    try:
        if ctx.state.input("device_mac"):
            runs.hold_device(ctx, ctx.state.input("device_mac"))  # a run carried on: its device is known
        firmware, _ = routes.settle_route(ctx, defer=True)
        unlock.unlock_tasmota(ctx)
    except routes.NothingToDo as nothing:
        output.tell(str(nothing))
        return rerun_on_tasmota(ctx) if runs.run_source(ctx.state) == "tasmota" else 0
    except Abort as refused:
        output.tell(f"Stopped before anything was done: {refused}.")
        return 2
    routes.apply_target(firmware, routes.route_source(ctx.state), routes.family_of(ctx.state))
    ctx.state.set_input("stopped_by", None)
    resumed = any(ctx.state.is_done(step) for step, _, _ in routes.STEPS)
    if not resumed:
        # For its report, when another version carries it on: one begun before
        # this was recorded says nothing.
        ctx.state.set_input("begun_on", __version__)
    ctx.run_started, ctx.run_resumed = started, resumed
    if resumed and routes.FAMILY.RESUME_CHECK is not None:
        try:
            routes.FAMILY.RESUME_CHECK(ctx)
        except Abort as refused:
            output.tell(f"Stopped before anything was done: {refused}.")
            return 2
    join.wait_while_device_busy(ctx)
    # By index: A3 lengthens the route as it chooses the firmware.
    index = 0
    # Where the screen was as each step began, for going back to it (GoBack).
    marks: dict[str, output.Mark | None] = {}
    redone: set[str] = set()   # steps gone back to, done again: "Retrying" on their line
    brought_up = False
    while True:
        if index >= len(routes.STEPS):
            # The route done: a device on an earlier release of its firmware
            # goes on to that firmware's update, once (bring_up_to_date).
            if brought_up or not bring_up_to_date(ctx):
                break
            brought_up = True
        step, title, _ = routes.STEPS[index]
        if ctx.dry_run and step not in routes.FAMILY.READ_ONLY_STEPS:
            output.say(f"[dry-run] stopping before {step} ({title}), the first step that writes.")
            break
        index += 1
        marks[step] = output.mark()
        if step in redone:
            redone.discard(step)
            output.retrying()
        if ctx.state.is_done(step):
            routes.begin_stage(step)
            if confirm_skip(ctx, step, title):
                output.stage_end("already done")
                continue
        if ctx.state.status(step) == ATTEMPTED:
            # Under its own stage line, like every other stage; the check, and
            # any question, are taken off the screen once settled.
            routes.begin_stage(step)
            output.transient_begin()
            output.say("")
            output.log_only(f"{step} was interrupted last time.")
            output.tell("Interrupted last time - checking whether it finished...")
            finished, reason = None, ""
            check = routes.FAMILY.ATTEMPTED_EVIDENCE.get(step)
            if check is not None:
                try:
                    finished, reason = check(ctx)
                except (DeviceError, Abort, OSError, ValueError) as exc:
                    finished, reason = None, f"The check itself could not run: {exc}"
                # The verdict is the stage's to act on ("already done", or done
                # again); only a check that could not run is the person's to read.
                (output.tell if finished is None and reason.startswith("The check itself")
                 else output.log_only)(f"    {reason}")
            if finished is True:
                # The device itself shows the step completed: nothing to decide.
                output.transient_end()
                ctx.state.mark(step, DONE, note="the device showed it had completed")
                output.stage_end("already done")
                continue
            # Not shown to have finished: it is done again, unasked, attended
            # or not - every step checks the device before it writes anything
            # and refuses if it is not as expected.  Asked, "it did finish"
            # for a step that had not left the run waiting for good on a
            # firmware never sent.
            output.say(f"    Doing {step} again: the device does not show it finished.")
            output.transient_end()
            # What it left on the device may still be under way (onward.serve_to_device).
            ctx.redoing_interrupted = {*getattr(ctx, "redoing_interrupted", ()), step}
        code = execute_with_retries(ctx, step)
        if code == 0 and getattr(ctx, "finished", False):
            firewall.close_firewall_rules(ctx)
            # Kept on Tasmota, chosen at A3: as when it was asked for (above).
            if isinstance(ctx.finished, routes.NothingToDo) and runs.run_source(ctx.state) == "tasmota":
                return rerun_on_tasmota(ctx)
            return 0
        back = getattr(ctx, "go_back", None)
        if code == 0 and back:
            ctx.go_back = None
            # The stages since that step off the screen, and why it is done
            # again: they are drawn afresh, as if the first time.  Only if it
            # still does not take does the run stop, saying why (execute).
            output.rewind(marks.get(back))
            here, index = index, next(i for i, (name, _, _) in enumerate(routes.STEPS) if name == back)
            # Each step from that one to this is done again, and says so.
            redone.update(name for name, _, _ in routes.STEPS[index:here])
            continue
        if code == 0 and stop_here_after_ctrl_c(step):
            ctx.stopped_by = "ctrl-c"   # for its report (cli.stopped_by)
            firewall.close_firewall_rules(ctx)
            return 130
        if code == 0 and step == routes.FAMILY.POINT_OF_NO_RETURN and ctx.research_bridge:
            prompts.section("Research bridge installed - stopping here", [
                f"{ctx.bridge.name} is on the device. Join the bridge's network",
                f"({names.our_setup_network(ctx)}, open) and run its research tool, e.g.:",
                "  python tools/espressif/inspect_stock_nvs.py",
                "Then switch the device off and on: it goes back to its Sonoff",
                "firmware. Or run this tool again to carry on the conversion.",
            ])
            firewall.close_firewall_rules(ctx)
            return 0
        if code == 130:
            return code  # the operator chose to quit; main() cleans up and says so
        if code != 0:
            firewall.close_firewall_rules(ctx)
            tell_how_to_carry_on()
            return code
    output.say("")
    firewall.close_firewall_rules(ctx)
    output.tell("")
    if ctx.dry_run:
        output.show("Dry run complete: nothing was written to the device.", output.GREEN)
        return 0
    updated = runs.run_source(ctx.state) == "outdated"
    firmware = routes.ROUTE[0] or runs.run_firmware(ctx.state)
    if ctx.state.input("put_back"):
        # Sonoff's firmware could not be put together (sonoff.put_back): why is
        # under R1's line.
        output.show(f"Sonoff's firmware could not be put back, so the device is back on "
                    f"{routes.FIRMWARES[firmware]}.", output.YELLOW)
    else:
        output.show("Update complete!" if updated else "Conversion complete!", output.GREEN)
    if firmware == "sonoff" and not updated:
        output.tell("It runs its own Sonoff firmware again, settings kept, back in the eWeLink app.")
    else:
        running = (f"{routes.FIRMWARES[firmware]} {update.versions(ctx)[1]}" if updated else
                   f"{'official ' if firmware == 'tasmota' else ''}{routes.FIRMWARES[firmware]}")
        url = report.device_page_url(ctx.state.input("device_ip"))
        # A colon leads into its link below; "..." read as still under way.
        output.tell(f"The device is running {running}{':' if url else '.'}")
        if url:
            output.show_link(url, "Your device")
    report.invite_report(ctx, time.monotonic() - started - output.waited(), resumed)
    return 0
