"""The route a run takes: from what the device runs to the firmware asked for,
as a list of steps.  Settled once at the run's start, then kept in STEPS
for the run.

Every route begins with A0, what has to be in place.  A device still on
Sonoff's firmware is then found in its eWeLink account (A1), which says
what kind of device it is, and only then is the firmware chosen (A3): which
firmware a device can have is its chip family's.  A converted device says its
kind by what it answers as, so its route is whole from the start.

The steps are a chip family's (FAMILIES, by the kind of device's family): a
package under here with the same handful of names - FIRMWARES, ONLY_FOR
(the firmware only some kinds may have), SOURCES and
steps_for(firmware, source) for its routes, none of them beginning with the
steps shared here, and what the runner needs to know of each step:
POINT_OF_NO_RETURN, DESTRUCTIVE_STEPS, RETRY_STEPS, ERASED_CHECK_STEPS,
STAGE_NAMES, TARGET_STAGE_NAMES, PLAIN_DESCRIPTION, ATTEMPTED_EVIDENCE,
STEP_VERIFIERS (each step its verifier doubts is done again, unasked),
SONOFF_FROM_BACKUP (whether going back to Sonoff's firmware needs
the device's own backup; where it does, sonoff_without_backup(ctx, source)
says what it can use instead once chosen, or None), RESUME_CHECK, what a resumed run checks first (or None),
and published_version(kind, firmware), the release of this tool's own build
of `firmware` it installs, if any, which a device on an earlier release of it
is offered as an update (routes/update.py).
"""

from __future__ import annotations

from types import ModuleType
from typing import Any, Callable, NamedTuple, TYPE_CHECKING

from tools.autoflash import account, detect, kinds, output, records, prompts, runs
from tools.autoflash.errors import Abort, Finished
from tools.autoflash.routes import beken, espressif, update
from tools.autoflash.routes import source
from tools.autoflash.routes.espressif import source as espressif_source
from tools.autoflash.state import RunState

if TYPE_CHECKING:
    from tools.autoflash.context import Context

Steps = list[tuple[str, str, Callable[["Context"], dict[str, Any]]]]

# Each chip family's routes, by the name a kinds.Kind gives its family.
FAMILIES: dict[str, ModuleType] = {"espressif": espressif, "beken": beken}
# What a run may install, every family's together: for the command line, and
# for naming one.  Which a device can have is firmwares_of's.
FIRMWARES = {name: label for family in FAMILIES.values() for name, label in family.FIRMWARES.items()}


def firmwares_of(kind: kinds.Kind) -> dict[str, str]:
    """What a device of `kind` may run: its family's FIRMWARES, less those
    its family keeps to other kinds (ONLY_FOR)."""
    family = FAMILIES[kind.family]
    return {name: label for name, label in family.FIRMWARES.items()
            if kind.key in family.ONLY_FOR.get(name, (kind.key,))}

# The steps of a run from Sonoff's firmware a run of 1.x recorded: one with no
# route recorded that has these done began from Sonoff's, for Tasmota.
LEGACY_STOCK_STEPS = ("A5", "A1", "A2", "C2", "C3", "D1")
# Where a run from Tasmota to ESPHome keeps its choice of route (route_source).
LEAVES_DIRECT = "leaves_tasmota_direct"


def family_of(state: RunState) -> ModuleType:
    """The family whose steps a run takes: its kind's (kinds.of_run)."""
    return FAMILIES[kinds.of_run(state).family]


def step_prerequisites(ctx: Context) -> dict[str, Any]:
    """A0 - what has to be in place first, for anyone who skipped the README."""
    if any(ctx.state.is_done(step) for step in ctx.state.data["steps"] if step != "A0"):
        return {"shown": False}  # a run from before this step existed
    output.transient_begin()
    if runs.run_source(ctx.state) == "sonoff":
        needed = [
            "- The eWeLink app on your phone, signed in (a throwaway account is fine).",
            "- Your device set up in the app and connected to Wi-Fi.",
        ]
    else:
        needed = ["- Your device connected to Wi-Fi, as it is now."]
    prompts.section("Before you start, you'll need:", needed + ["- This computer on the same network as the device."])
    prompts.wait_for_key()
    output.transient_end()  # read: the list leaves the screen, the stage line stays
    return {"shown": True}


def step_choose(ctx: Context) -> dict[str, Any]:
    """A3 - which firmware the device is to have, now that A1 has said what kind
    of device it is: the one given on the command line, kept from an earlier
    run, or asked for from its family's; and the route it takes from here.
    A converted device's is asked at the same point, after the prerequisites
    (choose_converted)."""
    if runs.run_source(ctx.state) != "sonoff":
        return choose_converted(ctx)
    kind = kinds.of_run(ctx.state)
    family = FAMILIES[kind.family]
    kept = ctx.state.input("firmware")
    if kept:
        apply_target(kept, "sonoff", family)
        return {"firmware": kept, "asked": False}
    given = ctx.firmware_given
    offered = firmwares_of(kind)
    choices = [name for name in offered if name != "sonoff"]
    if given == "sonoff":
        raise Abort("the device already runs Sonoff's firmware: there is nothing to convert")
    if given and given not in choices:
        raise Abort(f"this device cannot go to {FIRMWARES[given]}: a {kind.name} takes "
                    f"{' or '.join(offered[name] for name in choices)}")
    firmware = given or ask_firmware("sonoff", choices)
    ctx.state.set_input("firmware", firmware)
    output.say(f"Converting the device to {FIRMWARES[firmware]}.")
    apply_target(firmware, "sonoff", family)
    return {"firmware": firmware, "asked": not given}


def choose_converted(ctx: Context) -> dict[str, Any]:
    """A3 on a device this tool converted: the firmware settle_route left to
    be chosen here (ctx.firmware_choice) - given, or asked - and the route
    from here; or the one chosen at the run's start, carried on."""
    pending = ctx.firmware_choice
    if pending is None:
        firmware = ctx.state.require("firmware")[0]
    else:
        ctx.firmware_choice = None
        firmware, _ = choose_route(ctx, pending)
    apply_target(firmware, route_source(ctx.state), family_of(ctx.state))
    return {"firmware": firmware, "asked": pending is not None and not ctx.firmware_given}


# What every run begins with, whatever the device: from Sonoff's firmware,
# found through eWeLink first.  The family's own steps follow once the
# firmware is chosen.
STOCK_START: Steps = [
    ("A0", "Before you start", step_prerequisites),
    ("A1", "Pair the device in eWeLink", account.step_pair),
    ("A3", "Choose the firmware", step_choose),
]
START: Steps = [STOCK_START[0], STOCK_START[2]]
STAGE_NAMES = {"A0": "Confirming prerequisites", "A1": "Discovering the device", "A3": "Choosing the firmware"}
PLAIN_DESCRIPTION = {
    "A0": "lists what needs to be in place before starting",
    "A1": "gets the device onto your network through the eWeLink app",
    "A3": "asks which firmware the device is to have",
}


def steps_for(firmware: str | None, source: str = "sonoff", family: ModuleType = espressif) -> Steps:
    """The steps from `source` firmware to `firmware` on a device of `family`:
    from Sonoff's firmware, the shared start, then the family's once the
    firmware is chosen (None: not yet)."""
    if source == "sonoff":
        return STOCK_START + (family.steps_for(firmware, source) if firmware else [])
    if firmware == source:
        return []
    return START + (family.steps_for(firmware, source) if firmware else [])


def update_available(ctx: Context) -> str | None:
    """What `status` says of an update for the run's device, or None: none, or
    the device not answering.  It only asks: nothing is recorded."""
    firmware = recorded_firmware(ctx.state)
    published = family_of(ctx.state).published_version(kinds.of_run(ctx.state), firmware) if firmware else None
    try:
        release = update.earlier_release(ctx, firmware, published)
    except (OSError, RuntimeError):
        return None
    if release is None:
        return None
    return f"{FIRMWARES[firmware]} {release} is on the device, and {published} is available: run the tool again"


def recorded_firmware(state: RunState) -> str | None:
    """The firmware a run's record says it installs, None if not chosen yet:
    a run of 1.x, with steps done but no choice recorded, was for Tasmota -
    an Espressif device's; a device of another family has no such runs.  A run
    from a converted device stopped before its A3 chose has done no more than
    the steps before."""
    firmware = state.input("firmware")
    before = {step for step, _, _ in START}
    started = (any(state.is_done(step) for step in state.data["steps"] if step not in before)
               if runs.run_source(state) != "sonoff" else any(map(state.is_done, LEGACY_STOCK_STEPS)))
    if not firmware and "tasmota" in family_of(state).FIRMWARES and started:
        firmware = "tasmota"
    return firmware


def steps_of(state: RunState) -> Steps:
    """A run's route, as its record has it: its firmware once chosen, else
    the shared start alone."""
    return steps_for(recorded_firmware(state), route_source(state), family_of(state))


def route_source(state: RunState) -> str:
    """The source whose steps a run takes: what it left from, but for the choice
    settled once at its start between Tasmota's two ways to ESPHome (a run begun
    before there was a choice, past L1, took the one without the hub)."""
    source = runs.run_source(state)
    if source == "tasmota" and state.input("firmware") == "esphome":
        direct = state.input(LEAVES_DIRECT)
        if direct or (direct is None and state.is_done("L1")):
            return espressif.DIRECT_FROM_TASMOTA
    return source


# This run's route: Tasmota from Sonoff on an Espressif device until it is known
# (apply_target).  FAMILY is the module whose tables the runner reads.
FAMILY = espressif
STEPS = steps_for("tasmota")
STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}
ROUTE: tuple[str | None, str] = ("tasmota", "sonoff")


def apply_target(firmware: str | None, source: str = "sonoff", family: ModuleType = espressif) -> None:
    global STEPS, STEP_INDEX, ROUTE, FAMILY
    ROUTE = (firmware, source)
    FAMILY = family
    STEPS = steps_for(firmware, source, family)
    STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}


def go_on_instead(after: str, firmware: str, source: str) -> None:
    """The run goes on to `firmware` instead, from where it is - `after`, the
    step under way - on the route from `source`: the steps that route has
    and the run has not taken, after it. Numbered on from there, as the run
    showed them; a run carried on takes that route from the start, its
    steps done passed over."""
    global STEPS, STEP_INDEX, ROUTE
    names = [step for step, _, _ in STEPS]
    taken = STEPS[:names.index(after) + 1]
    STEPS = taken + [entry for entry in steps_for(firmware, source, FAMILY) if entry[0] not in set(names)]
    STEP_INDEX = {step: (title, handler) for step, title, handler in STEPS}
    ROUTE = (firmware, source)


def continue_with(steps: Steps) -> None:
    """Steps the run goes on to after its route's last: a device converted on
    an earlier release of its firmware, updated once done
    (runner.bring_up_to_date).  The run's loop takes STEPS by index, so it
    reaches them; each is added once."""
    for step, title, handler in steps:
        if step not in STEP_INDEX:
            STEPS.append((step, title, handler))
            STEP_INDEX[step] = (title, handler)


def past_the_point_of_no_return(state: RunState) -> bool:
    """Whether a run has written to its device: past its family's POINT_OF_NO_RETURN."""
    return state.is_done(family_of(state).POINT_OF_NO_RETURN)


def sonoff_backup_exists(ctx: Context) -> bool:
    """Whether this computer holds the device's Sonoff backup: what going back
    to Sonoff's firmware needs."""
    mac = ctx.state.input("device_mac")
    return bool(mac and records.find_backup(mac))


class NothingToDo(Finished):
    """The device already runs the firmware asked for: found before any stage,
    or chosen at A3 (runner.run_steps)."""


class FirmwareChoice(NamedTuple):
    """A converted device's firmware, still to be chosen at A3 (settle_route):
    what it answers as (`running`, "outdated" on an earlier release of
    `updating`), and what it can go to."""
    running: str
    updating: str
    choices: list[str]


def settle_firmware(ctx: Context) -> str | None:
    """Which firmware this run installs (settle_route); None until A3 chooses."""
    return settle_route(ctx)[0]


def nothing_to_convert(ctx: Context, source: str) -> None:
    """Asked for what it runs: nothing to do, and nothing left unfinished."""
    ctx.state.set_input("source", source)
    ctx.state.set_input("firmware", source)
    own = source == "esphome" and update.is_own_build(ctx)
    raise NothingToDo(f"The device already runs {detect.NAMES[source]}: there is nothing to convert."
                      + (" It is a build of its own, adopted into an ESPHome builder perhaps, so update it from "
                         "there. To install this project's ESPHome over it, losing its config changes and any "
                         "API key, run this again with --replace-own-build." if own else ""))


def settle_route(ctx: Context, defer: bool = False) -> tuple[str | None, str]:
    """What the device runs now and which firmware it is to have: kept from the
    run's start, given on the command line, or found out and asked - once,
    before anything is done.  A device this tool converted is found on the
    network by what it answers as, and says its kind; one still on Sonoff's
    firmware is found through eWeLink at A1, and its firmware chosen at A3,
    so for it the firmware is None here.  `defer`: a converted device's is
    chosen at A3 too, after the prerequisites, as one from Sonoff's is
    (choose_converted) - None here, what it was given checked already."""
    kept, running = ctx.state.input("firmware"), ctx.state.input("source")
    given = ctx.firmware_given
    if kept and given and given != kept:
        raise Abort(
            f"this device is being converted to {FIRMWARES[kept]}, and a conversion cannot "
            f"switch to {FIRMWARES[given]} part way. Carry on without "
            f"{'--esphome' if ctx.esphome_firmware else '--supla' if ctx.supla_firmware else '--firmware'}, "
            "or start again with --new"
        )
    if kept:
        return kept, running or "sonoff"
    if running == "sonoff":
        return None, "sonoff"  # found in eWeLink already; the firmware is chosen at A3
    started = [step for step in LEGACY_STOCK_STEPS if ctx.state.is_done(step)]
    if started:
        # Begun by 1.x, before there was a choice: from Sonoff's, for Tasmota.
        ctx.state.set_input("source", "sonoff")
        ctx.state.set_input("firmware", "tasmota")
        return "tasmota", "sonoff"
    running = source.find_current_firmware(ctx)
    if running == "readout":
        # A readout stopped part way left a device of no kind on its readout
        # firmware: whatever was asked for, its Sonoff firmware goes back.
        from tools.autoflash import readout_beken
        raise NothingToDo(readout_beken.put_back(ctx, ctx.state.require("device_ip")[0]))
    updating = running
    published = family_of(ctx.state).published_version(kinds.of_run(ctx.state), running)
    if update.outdated(ctx, running, published) or (
            ctx.replace_own_build and update.replace_own_build(ctx, running, published)):
        running = "outdated"
    if running == "sonoff":
        if given == "sonoff":
            nothing_to_convert(ctx, "sonoff")
        ctx.state.set_input("source", "sonoff")
        return None, "sonoff"
    # Tasmota stays a choice on Tasmota: its meter checked (rerun_on_tasmota).
    offered = firmwares_of(kinds.of_run(ctx.state))
    choices = [name for name in offered if name != running or running == "tasmota"]
    if given:
        if given == running:
            nothing_to_convert(ctx, running)
        if given not in offered:
            raise Abort(f"this device cannot go to {FIRMWARES[given]}: a {kinds.of_run(ctx.state).name} "
                        f"takes {' or '.join(offered.values())}")
    pending = FirmwareChoice(running, updating, choices)
    if defer:
        # Its source known from here (the prerequisites it is shown, its web
        # password): what it runs, an update's as the firmware it updates.
        ctx.state.set_input("source", updating)
        ctx.firmware_choice = pending
        return None, updating
    return choose_route(ctx, pending)


def choose_route(ctx: Context, pending: FirmwareChoice) -> tuple[str, str]:
    """A converted device's firmware, given or asked (settle_route), recorded
    with where it goes from."""
    running, updating, choices = pending
    if ctx.firmware_given:
        firmware = ctx.firmware_given
    else:
        firmware = ask_firmware(running, choices,
                                (updating, *update.versions(ctx)) if running == "outdated" else None)
        if firmware == running:
            nothing_to_convert(ctx, running)
    if firmware == "sonoff" and family_of(ctx.state).SONOFF_FROM_BACKUP and not sonoff_backup_exists(ctx):
        # Offered whatever the device, as most have its identity somewhere: where
        # it comes from is found now, before anything is written.
        without = family_of(ctx.state).sonoff_without_backup(ctx, updating if running == "outdated" else running)
        if without is None:
            raise Abort("this device cannot go back to Sonoff's firmware: there is no backup of it on this "
                        "computer, it keeps no Sonoff identity, and its eWeLink account has no record of it")
        output.say(f"There is no backup of the device's Sonoff firmware on this computer: going back to it "
                   f"uses {without}.")
    if running == "outdated" and firmware != updating:
        running = updating  # left as any of its firmware is
    ctx.state.set_input("source", running)
    ctx.state.set_input("firmware", firmware)
    if (running, firmware) == ("tasmota", "esphome"):
        ctx.state.set_input(LEAVES_DIRECT, espressif_source.leaves_tasmota_direct(ctx))
    if running == "outdated":
        output.say("Updating the device's {} from {} to {}.".format(FIRMWARES[firmware], *update.versions(ctx)))
        return firmware, running
    # Part way, there is no firmware to name: only where the run goes.
    output.say(f"Converting the device from {detect.NAMES[running]} to {FIRMWARES[firmware]}."
               if running not in ("midway", "committed", "installer")
               else f"Converting the device to {FIRMWARES[firmware]}.")
    return firmware, running


def ask_firmware(source: str, choices: list[str], outdated: tuple[str, str, str] | None = None) -> str:
    """The firmware to install: the choices by name alone, with a word only on
    what is particular to this device - part way, on Tasmota's safeboot, or
    without its template.  On an earlier release of
    the ESPHome, OpenBeken or Tasmota this tool installs (`outdated`: which,
    the release it runs and this one), that firmware's option is its update."""
    output.transient_begin()
    body = (["Left part way through a conversion, it can go on to any of these."]
            if source in ("midway", "committed", "installer", "interim") else
            ["Its Tasmota goes back on first, settings kept; or it goes on to another."]
            if source == "safeboot" else
            ["Its Tasmota has no template, so switches nothing; Tasmota here adds it."]
            if source == "untemplated" else [])
    # With nothing particular to say, the question alone: a heading would repeat it.
    if body:
        prompts.section("Choose the device's new firmware", body)
    labels = {"tasmota": "Tasmota" if source != "tasmota" else "Tasmota, as it is (its meter calibrated, if it is not)",
              "openbeken": "OpenBeken", "esphome": "ESPHome",
              "supla": "Supla GUI-Generic (from a file you build at gui-generic-builder-new.supla.io)",
              "sonoff": "Sonoff (eWeLink)"}
    if outdated:
        # An update says so on its option alone, keeping the settings as one does.
        labels[outdated[0]] = f"{FIRMWARES[outdated[0]]}, update to {outdated[2]}"
    try:
        choice = prompts.ask_choice("Which firmware should the device have", [labels[name] for name in choices])
    except prompts.NoOneToAnswer:
        output.transient_end()
        raise Abort("an unattended run needs to be told which firmware: "
                    + " or ".join(f"--firmware {name}" for name in choices)) from None
    output.transient_end()
    return choices[choice]


def stage_name(step: str) -> str:
    return _stage_name(step, STEP_INDEX[step][0], ROUTE[0], FAMILY)


def stage_name_of(state: RunState, step: str) -> str:
    """A step's stage name on a run's own route, as its record has it."""
    title = next(title for name, title, _ in steps_of(state) if name == step)
    return _stage_name(step, title, recorded_firmware(state), family_of(state))


def _stage_name(step: str, title: str, firmware: str | None, family: ModuleType) -> str:
    named = family.TARGET_STAGE_NAMES.get(firmware, {})
    if step in named:
        return named[step]
    return STAGE_NAMES.get(step) or family.STAGE_NAMES.get(step, title)


def stage_layout() -> tuple[int, int]:
    """The longest stage name the run may show, and the most stages it may
    have.  The rest of the route is known only once A3 has chosen the
    firmware - from Sonoff's firmware, once A1 has found the kind of device -
    so every stage line is laid out for the longest route there could be:
    from Sonoff's, on any device, or the readout A1 may turn it into
    (readout.from_run); from a converted device, in its family."""
    turns_into: list[str] = []
    if ROUTE[1] == "sonoff":
        from tools.autoflash import readout  # it builds on the routes
        possible = [(firmware, family, steps_for(firmware, "sonoff", family)) for family in FAMILIES.values()
                    for firmware in family.FIRMWARES if firmware != "sonoff"]
        turns_into = [name for _, name in readout.STAGES]
    else:
        sources = set(FAMILY.SOURCES) - {"sonoff"} | {getattr(FAMILY, "DIRECT_FROM_TASMOTA", "outdated")}
        possible = [(firmware, FAMILY, steps_for(firmware, start, FAMILY))
                    for firmware in FAMILY.FIRMWARES for start in sources if routable(firmware, start)]
    return (max(*(len(name) for name in turns_into),
                *(len(_stage_name(name, title, firmware, family))
                  for firmware, family, steps in possible for name, title, _ in steps)),
            max(len(steps) for _, _, steps in possible))


def routable(firmware: str, source: str) -> bool:
    """Whether the family has a route from `source` to `firmware`."""
    try:
        FAMILY.steps_for(firmware, source)
    except ValueError:
        return False
    return True


def plain_description(step: str, title: str) -> str:
    return PLAIN_DESCRIPTION.get(step) or FAMILY.PLAIN_DESCRIPTION.get(step, title.lower())


def stage_number(step: str) -> int:
    """The number a step's stage line shows: the screen names a stage by it,
    never by the step's code (C2), which the person cannot map to one."""
    return next(index for index, (name, _, _) in enumerate(STEPS, 1) if name == step)


def begin_stage(step: str) -> None:
    number = stage_number(step)
    output.set_stage_width(*stage_layout())
    output.stage_begin(number, stage_name(step))
