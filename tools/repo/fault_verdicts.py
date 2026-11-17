"""What both fault suites judge a run by beyond its steps being done: the
end-of-run checks the report of a converting run gives, the readout.tar a
readout keeps, and the screen each run left. A run whose steps all passed can
still end with a relay that did not switch, or a check that never got to run,
and say so only there; or leave a stage's failed try on the screen
(docs/testing.md#the-fault-suites)."""
from __future__ import annotations

import io
import json
import lzma
import re
import shutil
import tarfile
from collections.abc import Iterable
from pathlib import Path

from tools.autoflash import report_upload
from tools.repo import unattended_run


def run_outcomes(captures: Path, since: float) -> list[dict]:
    """How each run of the tool under `captures` (an emulator's captures/)
    ended since `since` (seconds, the epoch's), oldest first: each run's line in
    its folder's report_upload.OUTCOMES, which the run itself wrote as it
    ended. A run killed outright wrote none."""
    found = []
    for path in Path(captures).glob(f"*/*/runs/*/{report_upload.OUTCOMES}"):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                said = json.loads(line)
            except ValueError:
                continue
            if isinstance(said, dict) and isinstance(said.get("at"), (int, float)) and said["at"] >= since:
                found.append(said)
    return sorted(found, key=lambda said: said["at"])

# As the run's log has them: the report's answer (tools/autoflash/report.py,
# checks_passed), up to the gap after it, in either column; and the line its
# check said of the relay.
CHECKS_PASSED = re.compile(r"^\[[\d:]+\] .*?\b(?:Checks passed|Checks): +(\S.*?)(?: {2,}| *$)", re.M)
RELAY = re.compile(r"^\[[\d:]+\] +Relay: +(.+?) *$", re.M)

# The answers a run ending on each firmware may give in the emulator: "Pass",
# or as a release before 3.0.0 said it, "True". On Tasmota, ESPHome and
# OpenBeken the check needs only the firmware, which the emulator runs: it
# switches the relay, so anything but a pass is a failure - "Not run" there is
# a check never tried. Back on Sonoff's it switches the relay through eWeLink,
# the stub's or the real one, by the device id the device's record keeps: "Not
# run" is a device with none (one converted before it was kept), which the
# emulator cannot change. "Fail" ("False") is a failure everywhere: the relay
# did not switch, or could not be checked, as when eWeLink had not shown the
# device online yet.
PASSED = {"Pass", "True"}
PASSING = {"tasmota": PASSED, "esphome": PASSED, "openbeken": PASSED, "sonoff": {*PASSED, "Not run"}}


def last_run(log: str) -> str:
    """The last run in a run log: one log holds every run carried on in it."""
    return log[log.rfind("=== run started"):] if "=== run started" in log else log


def last_run_events(events: list[dict]) -> list[dict]:
    """The last run's events (output.EVENTS) of a run folder's: from its start."""
    starts = [at for at, item in enumerate(events) if item["event"] == "run"]
    return events[starts[-1]:] if starts else events


def advised_to_switch(events: list[dict]) -> bool:
    """Whether a run's events (output.EVENTS) end with advice to switch the
    device off and on: since its last step began - as it stopped, not in a step
    it went on from - and not an ask to do it then, which it waited on."""
    began = max((at for at, item in enumerate(events) if item["event"] == "began"), default=-1)
    return any(item["event"] == "switch" and not item.get("now") for item in events[began + 1:])


# What a run says of an error the tool did not see coming (runner.unexpected).
UNEXPECTED = "something unexpected went wrong"


def unexpected(logs: Iterable[str], outcomes: Iterable[dict]) -> bool:
    """Whether a run of this checkout's tool met an error it did not see
    coming - a bug, whatever the fault, though the next run may convert the
    device (FAIL-UNEXPECTED): said in its log (`logs`, the words of this
    checkout's runs), or its own outcome classing its stop a crash, as a run
    does whatever its words (fault_verdicts.run_outcomes). A run the suite
    killed, as its crash fault does, says neither."""
    return (any(UNEXPECTED in text for text in logs)
            or any((said.get("stopped") or {}).get("error") == "crash" for said in outcomes))


# The report's Checks answer as each run's outcome has it (report_data.checks_word),
# and as the run log's report says it.
SAID = {"passed": "Pass", "failed": "Fail", "not-run": "Not run"}


def checks_problem(log: str, firmware: str, metered: bool = False, outcome: dict | None = None) -> str | None:
    """What the end-of-run checks of the last run in `log` said, when they did
    not pass for a device on `firmware`: "Checks passed: Fail; Relay: did not
    switch". None when they passed, or the run gave no report (a device already
    on the firmware asked for has nothing to check). `metered`: the emulated
    device's meter measures a load while its relay is on, so where the check
    reads the meter (all but Sonoff's firmware) it must confirm the switch.

    Read from the run's own outcome (`outcome`, its last line of
    fault_verdicts.run_outcomes, which a run that reported wrote as it did),
    as the run judged its checks; from the report's words in the log only for
    a run that kept no checks there - an earlier release's."""
    run = last_run(log)
    checks = (outcome or {}).get("checks")
    if isinstance(checks, dict) and unattended_run.LOGGED_RESULT.search(run):
        answer = SAID.get(checks.get("answer"), str(checks.get("answer")))
        said = f"Relay: {checks.get('relay') or 'not said'}"
        if answer not in PASSING.get(firmware, {*PASSED, "Not run"}):
            return f"Checks passed: {answer}; {said}"
        if metered and firmware != "sonoff" and not checks.get("meter_confirms"):
            return f"the meter did not confirm the switch; {said}"
        return None
    answers = CHECKS_PASSED.findall(run)
    relay = RELAY.findall(run)
    said = f"Relay: {relay[-1] if relay else 'not said'}"
    if not answers:
        return None
    if answers[-1] not in PASSING.get(firmware, {*PASSED, "Not run"}):
        return f"Checks passed: {answers[-1]}; {said}"
    if metered and firmware != "sonoff" and not (relay and relay[-1].endswith("(meter confirms)")):
        return f"the meter did not confirm the switch; {said}"
    return None


# What a readout keeps (docs/readouts.md, "What is in one"), by chip family:
# each .bin in it xz-compressed, and the firmware eWeLink offers under offered/.
READOUT_FILES = {"espressif": ("manifest.json", "flash.bin.xz", "efuse.bin.xz", "info.txt", "report.json"),
                 "beken": ("manifest.json", "flash.bin.xz", "info.txt")}
# The flash of every device the emulators run: 4 MB.
WHOLE_FLASH = 0x400000


def readout_problems(data: bytes, family: str) -> list[str]:
    """What a readout.tar lacks of a whole readout of a `family` device, as
    docs/readouts.md lists it, unsent from an unattended run: [] for none."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            files = {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}
    except (tarfile.TarError, OSError) as exc:
        return [f"not a tar: {exc}"]
    problems = [f"no {name}" for name in READOUT_FILES[family] if name not in files]
    if list(files)[:1] != ["manifest.json"]:
        problems.append("manifest.json is not first")
    if not any(name.startswith("offered/") for name in files):
        problems.append("no offered firmware")
    try:
        manifest = json.loads(files.get("manifest.json") or b"")
    except ValueError:
        return problems + ["manifest.json is not JSON"]
    if manifest.get("read") is not True:
        problems.append("the manifest says the memory was not read")
    if "contact" in manifest:
        problems.append("a contact in it, from an unattended run")
    flash = manifest.get("flash") or {}
    if family == "espressif" and flash.get("matches_device") is not True:
        problems.append("the flash read does not match the device's own hash")
    try:
        size = len(lzma.decompress(files["flash.bin.xz"])) if "flash.bin.xz" in files else None
    except lzma.LZMAError:
        return problems + ["flash.bin.xz is not xz"]
    if size is not None and (size != WHOLE_FLASH or flash.get("size") != size):
        problems.append(f"flash.bin is {size:,} bytes, the manifest says {flash.get('size')}: "
                        f"not the whole {WHOLE_FLASH:,}")
    return problems


def first_stage_problems(data: bytes, runs: str) -> list[str]:
    """What a readout.tar lacks of the first stage alone of a device that runs
    `runs` now (detect.NAMES' key) - the firmware eWeLink offers it, its
    memory not read - unsent from an unattended run: [] for none."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            files = {member.name: tar.extractfile(member).read() for member in tar.getmembers() if member.isfile()}
    except (tarfile.TarError, OSError) as exc:
        return [f"not a tar: {exc}"]
    if list(files)[:1] != ["manifest.json"]:
        return ["manifest.json is not first"]
    problems = [] if any(name.startswith("offered/") for name in files) else ["no offered firmware"]
    problems += [f"{name} in it, from a device not on Sonoff's firmware" for name in files
                 if name != "manifest.json" and not name.startswith("offered/")]
    try:
        manifest = json.loads(files["manifest.json"])
    except ValueError:
        return problems + ["manifest.json is not JSON"]
    if manifest.get("read") is not False:
        problems.append("the manifest does not say the memory was not read")
    if manifest.get("runs") != runs:
        problems.append(f"the manifest says it runs {manifest.get('runs')}, not {runs}")
    if "contact" in manifest:
        problems.append("a contact in it, from an unattended run")
    return problems


# As tools/repo/unattended_run.py prints the screen a run ended on, in the
# output the fault suites keep of each run.
SCREEN = re.compile(r"^The screen at the end of the run \(\d+x\d+\):\n=+\n(?P<screen>.*?)^=+$", re.M | re.S)


def screen_problems(output: str) -> list[str]:
    """What each run in `output` - every recorded run of a scenario, as its
    log keeps them - left of a stage on its screen that it should have taken
    away (unattended_run.stage_problems): [] for nothing, or no screen."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", output)  # printed in colour unless NO_COLOR is set
    return [problem for found in SCREEN.finditer(plain)
            for problem in unattended_run.stage_problems(found["screen"])]


# As tools/repo/unattended_run.py says where it kept a run's recording.
RECORDING = re.compile(r"^Kept in (?P<folder>\S+?)/?: screen\.ansi", re.M)


def keep_recordings(output: str, work: Path, folder: Path) -> list[Path]:
    """Each run's recording in `output` - every run of a scenario, as its log
    keeps them - copied from the emulated computer's tree `work` into `folder`,
    run-1.raw, run-2.raw...: every byte each run drew, to replay (cat it, or
    feed it to unattended_run.Screen), where the log keeps only the screen it
    ended on.  The emulator's own copies go when its next scenario begins."""
    kept = []
    for number, found in enumerate(RECORDING.finditer(output), 1):
        where = Path(found["folder"])   # relative to the tree, or /work/... inside the emulator
        source = work / (where.relative_to("/work") if where.is_absolute() else where) / "screen.raw"
        if source.is_file():
            folder.mkdir(parents=True, exist_ok=True)
            kept.append(Path(shutil.copy(source, folder / f"run-{number}.raw")))
    return kept
