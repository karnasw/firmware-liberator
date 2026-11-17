#!/usr/bin/env python3
"""Attended runs in the emulators: the tool run as a person sits through it,
every question asked and answered as a scenario's answer set says
(tools/repo/unattended_run.py --attended), and the screen judged at every
answer and at the end - what the fault suites cannot see, as they run the
tool unattended.

A scenario is a JSON file in tools/espressif/emulator/attended/ (an S60's,
in the Espressif emulator) or tools/beken/emulator/attended/ (an S61's):

    {"about": "what it shows",
     "start": "fresh" | a saved device (unattended-test.sh --from),
     "prep": [...], "args": [...], "size": "100x40", "minutes": 30,
     "answers": [{"when": regex, "type": keys, "times": n, "optional": bool}, ...],
     "expect": {"exit": 0, "result": "Converted", "screen": true, "shows": [regex, ...],
                "never": [regex, ...]},
     "again": {"answers": [...], "args": [...], "expect": {...}}}

`again`: the tool run once more on the device as the first run left it, with
no --new - a run stopped part way, offered back ("did not finish") - judged
as the first is.

`prep`, before the run, on the device (Espressif only): ["convert", FIRMWARE]
first, if at all (the device converted to it, unattended); "sudo forgets" (sudo
remembers no password in the emulator, so it asks again at every use - the
end of the run too); ["tasmota password", PASSWORD] (Tasmota's web password
set); ["locked esphome", "login" | "login and updates"] (this project's
ESPHome with a web login, admin and "test-login-1", and the made-up API
encryption key LOCKED_KEY; and with "updates", taking only encrypted
updates). An answer is typed at its question only (unattended_run.Answer);
one `optional` need not be asked. In place of `type`, an answer can switch
the device ("power": "off") or its home Wi-Fi's router ("router": "off", or
on the Beken emulator "on NEW-PASSWORD", the password changed), move the
computer onto the device's own network or back ("wifi": "join", "home"), or
signal the run ("signal": "TERM").
`command`: "readout" for a readout, not a conversion; `api_enough`: true
for the API beside the emulator to have enough readouts of the device's
product already (AUTOFLASH_API_ENOUGH), so a readout of it is not needed.

The verdict, from the recording alone (judge):
    PASS              ended as expected, every answer typed, nothing wrong
    FAIL-RESULT       ended otherwise: its exit code, or its report's Result
                      (the recorder's result.txt, from the run's log)
    FAIL-UNANSWERED   it ended at a question no answer covers: new, or reworded
    FAIL-UNUSED       an answer never typed: its question no longer asked
    FAIL-ANSWER       an answer that switches the device or the router, or
                      moves the computer's Wi-Fi, failed to: the network not on
                      the air to join, say
    FAIL-QUESTION     as a question was answered: no "(q to quit)" on it, a
                      row the terminal wrapped itself, or a question shown
                      twice - an earlier try left on the screen
    FAIL-SCREEN       the screen it ended on: its stages (unattended_run.stage_problems),
                      its report and how it ended (end_problems, with what the
                      run put on the clipboard), a row the terminal wrapped
                      itself, or a line `expect`'s `shows` wants not on it, or
                      one its `never` wants nowhere: on the screen at any
                      answer, or at the end - a key pressed by accident,
                      echoed

    tools/espressif/emulator/attended_suite.py list
    tools/espressif/emulator/attended_suite.py run [NAME ...] [--workers N] [--first-instance N] --device MAC

Its results, and each run's recording, go to captures/emulation/attended/<time>/.
Run on a computer with the emulators (docs/emulator.md); each scenario runs
on an emulator of its own, --workers at once - the S61's after, one at a
time - numbered from --first-instance (61) to one per worker, each started
afresh: a run uses no more instance numbers than it has workers.
"""

from __future__ import annotations

import argparse
import base64
import codecs
import concurrent.futures
import datetime
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
from tools.autoflash import kinds, records  # noqa: E402
from tools.repo import emulator_stats, unattended_run  # noqa: E402

SCENARIOS = {"espressif": ROOT / "tools/espressif/emulator/attended",
             "beken": ROOT / "tools/beken/emulator/attended"}
RESULTS = ROOT / "captures/emulation/attended"
FIRST_INSTANCE = 61
SIZE = (100, 40)
# The made-up key of the locked ESPHome builds (build_locked).
LOCKED_KEY = base64.b64encode(bytes(range(32))).decode()
LOCKED_LOGIN = ("admin", "test-login-1")
QUIT_FIRST = [{"when": "Which device is this run for|Press any key", "type": "q\r"}]
DEVICE = "192.168.60.50"


def scenarios() -> dict[str, tuple[str, dict[str, Any]]]:
    """Every scenario, by name: its family's, and what its file says."""
    found = {}
    for family, folder in SCENARIOS.items():
        for path in sorted(folder.glob("*.json")):
            found[path.stem] = (family, json.loads(path.read_text(encoding="utf-8")))
    return found


# ---------------------------------------------------------------- judging


def screen_at(raw: bytes, upto: int, size: tuple[int, int] = SIZE) -> unattended_run.Screen:
    screen = unattended_run.Screen(*size)
    screen.feed(codecs.decode(raw[:upto], "utf-8", errors="replace"))
    return screen


def question_problems(screen: unattended_run.Screen, wrapped_before: int) -> list[str]:
    """What is wrong with the screen as a question was answered."""
    problems = []
    rows = [row for row in screen.render(colour=False).splitlines() if row.strip()]
    asked = unattended_run.question(screen)
    if "(q to quit)" not in asked:
        problems.append(f"no (q to quit) at {asked.strip().splitlines()[-1].strip()[:60]!r}")
    if screen.wrapped > wrapped_before:
        problems.append("a row the terminal wrapped itself")
    since = max((index for index, row in enumerate(rows) if unattended_run.STAGE_ROW.match(row)), default=-1)
    shown = [row.split("(q to quit)")[0].strip() for row in rows[since + 1:] if "(q to quit)" in row]
    twice = sorted({question for question in shown if shown.count(question) > 1})
    if twice:
        problems.append(f"shown twice: {twice[0][:60]!r}")
    return problems


def judge(scenario: dict[str, Any], raw: bytes, answers: list[dict[str, Any]], code: int,
          size: tuple[int, int] = SIZE, result: str | None = None, clipboard: str | None = None) -> list[str]:
    """The verdicts of a run, from its recording (screen.raw), the answers as
    the recorder kept them (answers.json, with typed_at), its exit code, its
    report's Result (result.txt) and what it put on the clipboard
    (clipboard.txt; "" for nothing, None where that is not known): empty
    for a PASS."""
    expect = scenario.get("expect", {})
    verdicts = []
    final = screen_at(raw, len(raw), size)
    text = final.render(colour=False)
    asked = unattended_run.question(final)
    if unattended_run.waiting(asked) and "(q to quit)" in asked:
        waiting = next(row for row in reversed(asked.splitlines()) if "(q to quit)" in row)
        verdicts.append(f"FAIL-UNANSWERED: {waiting.strip()[:70]!r}")
    if code != expect.get("exit", 0):
        verdicts.append(f"FAIL-RESULT: exit {code}, not {expect.get('exit', 0)}")
    if expect.get("result") and not (result or "").startswith(expect["result"]):
        verdicts.append(f"FAIL-RESULT: Result {result or 'none'!r}, not {expect['result']!r}")
    # Keyed as the recorder keeps them: an answer that switches the device or
    # signals the run types nothing.
    given = {(item["when"], item.get("type", "")): item for item in scenario["answers"]}
    for answer in answers:
        if not answer["typed_at"] and not given.get((answer["when"], answer.get("type", "")), {}).get("optional"):
            verdicts.append(f"FAIL-UNUSED: {answer['when']!r}")
        for why in answer.get("failed", []):
            verdicts.append(f"FAIL-ANSWER: {answer['when']!r}: {why}")
    wrapped = 0
    # A key typed anywhere (a Ctrl-C as a stage shows) answers no question.
    for at in sorted(at for answer in answers if not answer.get("anywhere") for at in answer["typed_at"]):
        screen = screen_at(raw, at, size)
        for problem in question_problems(screen, wrapped):
            verdicts.append(f"FAIL-QUESTION: {problem}")
        wrapped = screen.wrapped
    if expect.get("screen", True):
        # The window as it was left, not its scrollback: a run gone on to
        # another device scrolls the last one up into it, with blank rows.
        window = "\n".join(text.rstrip("\n").split("\n")[-size[1]:])
        verdicts += [f"FAIL-SCREEN: {problem}" for problem in unattended_run.stage_problems(window)]
        verdicts += [f"FAIL-SCREEN: {problem}" for problem in unattended_run.end_problems(text, clipboard)]
        if final.wrapped > wrapped:
            verdicts.append("FAIL-SCREEN: a row the terminal wrapped itself, after the last answer")
    for unwanted in expect.get("never", []):
        seen = [screen_at(raw, at, size).render(colour=False) for answer in answers for at in answer["typed_at"]]
        if any(re.search(unwanted, shown, re.M) for shown in [*seen, text]):
            verdicts.append(f"FAIL-SCREEN: shown: {unwanted!r}")
    for wanted in expect.get("shows", []):
        if not re.search(wanted, text, re.M):
            verdicts.append(f"FAIL-SCREEN: not shown: {wanted!r}")
    return verdicts


# ---------------------------------------------------------------- running


def run_answers(scenario: dict[str, Any], folder: Path, name: str = "answers-given.json") -> Path:
    """The scenario's answers as the recorder takes them (no `optional`)."""
    path = folder / name
    path.write_text(json.dumps([{key: value for key, value in item.items() if key != "optional"}
                                for item in scenario["answers"]], indent=1), encoding="utf-8")
    return path


def docker(*args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, check=check)


def in_container(n: int, code: str) -> str:
    """Python in emulator `n`'s container, as the tool runs there."""
    import os
    done = docker("exec", "-u", f"{os.getuid()}:{os.getgid()}", "-w", "/work", f"autoflash-emu-{n}",
                  "/opt/venv/bin/python", "-c", code)
    return (done.stdout + done.stderr).strip()


def sudo_forgets(n: int, stop: threading.Event) -> None:
    """sudo remembering no password in emulator `n`, as soon as it is up."""
    while not stop.is_set():
        if docker("exec", "-u", "root", f"autoflash-emu-{n}", "true").returncode == 0:
            docker("exec", "-u", "root", f"autoflash-emu-{n}", "sh", "-c",
                   'echo "Defaults timestamp_timeout=0" > /etc/sudoers.d/zz-no-cache'
                   " && chmod 440 /etc/sudoers.d/zz-no-cache")
            return
        time.sleep(0.5)


def build_locked(variant: str) -> Path:
    """This project's S60 ESPHome with a web login and the made-up API key -
    taking only encrypted updates too, for "login and updates" - built as the
    published one is, with the emulator's Wi-Fi written in. Kept in
    captures/emulation/attended/ for the next run."""
    from tools.autoflash import imprint
    from tools.repo import update_firmware
    config = (ROOT / "devices/sonoff-s60/esphome.yaml").read_text(encoding="utf-8")
    release = re.search(r'version: "(.+?)"', config).group(1)
    built = RESULTS / f"locked-{variant.replace(' ', '-')}-{release}.ota.bin"
    if built.exists():
        return built
    config = config.replace("\napi:\n", f'\napi:\n  encryption:\n    key: "{LOCKED_KEY}"\n', 1)
    config = config.replace("web_server:\n  version: 3\n", "web_server:\n  version: 3\n  auth:\n"
                            f"    username: {LOCKED_LOGIN[0]}\n    password: {LOCKED_LOGIN[1]}\n", 1)
    if "updates" in variant:
        config = config.replace("ota:\n  - platform: esphome\n", "ota:\n  - platform: esphome\n    encryption: {}\n", 1)
    pins = json.loads((ROOT / "artifacts/firmware.json").read_text(encoding="utf-8"))
    image = next(entry["builder"] for entry in _entries(pins)
                 if entry.get("config") == "devices/sonoff-s60/esphome.yaml" and entry.get("builder"))
    # On the disk, not in /tmp - its ESP-IDF is gigabytes, and a small /tmp
    # filled up - and kept, so the next build has it.
    work = RESULTS / "esphome-build"
    work.mkdir(parents=True, exist_ok=True)
    path = work / "esphome.yaml"
    path.write_text(config, encoding="utf-8")
    app, _ = update_firmware.build(image.split("@")[0], path, release, work)
    built.parent.mkdir(parents=True, exist_ok=True)
    built.write_bytes(imprint.imprint(app, "PICSimLabWifi", ""))
    return built


def _entries(node: Any):
    """Every dict in firmware.json, however deep."""
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _entries(value)
    elif isinstance(node, list):
        for value in node:
            yield from _entries(value)


def run_environment(scenario: dict[str, Any], mac: str) -> dict[str, str]:
    """The environment the scenario's run goes on in: the stub lists the
    device under its product model, as fault_suite.device_product() gives it,
    and the API is told it has enough of that product where the scenario says
    (`api_enough`), as fault_suite.enough. Without the first, the stub takes
    the product from this checkout's earlier runs (emu.sh), and on one with
    none lists no product: a readout then sent no model, which the API
    refused (400)."""
    import os
    from tools.autoflash import kinds, records
    environment = dict(os.environ)
    product = (records.read_device(mac).get("product_model") or kinds.SONOFF_S60.example_product_model).upper()
    environment.setdefault("AUTOFLASH_EMU_STUB_PRODUCT_MODEL", product)
    if scenario.get("api_enough"):
        environment["AUTOFLASH_API_ENOUGH"] = product
    return environment


def wrapper(n: int, scenario: dict[str, Any], *more: str, mac: str) -> list[str]:
    readout = ["--readout"] if scenario.get("command") == "readout" else []
    return [str(ROOT / "tools/espressif/emulator/unattended-test.sh"), f"--instance={n}", f"--device={mac}",
            "--stub-cloud", *readout, *more]


def prepare(n: int, scenario: dict[str, Any], folder: Path, log, mac: str) -> bool:
    """The device as the scenario starts it, before its run: from where it says,
    then the tool quit at its first question with the emulator kept, for
    what `prep` does to the device. False when that could not be done."""
    start = scenario.get("start", "fresh")
    actions = [action for action in scenario.get("prep", []) if action != "sudo forgets"]
    first = ["--fresh"] if start == "fresh" else [f"--from={start}"]
    if not actions:
        return True   # its run starts it, from where it says
    if actions[0][0] == "convert":
        # Converted first, unattended: a device saved on one firmware, wanted on another.
        code = subprocess.call(wrapper(n, {}, *first, "--timeout=40", "--", "--new", "--firmware",
                                       actions[0][1], mac=mac), stdout=log, stderr=subprocess.STDOUT)
        log.write(f"PREP converted to {actions[0][1]}: exit {code}\n")
        docker("stop", f"autoflash-emu-{n}")
        actions, first = actions[1:], []
        if code != 0:
            return False
        if not actions:
            return True
    quit_first = folder / "quit-first.json"
    quit_first.write_text(json.dumps(QUIT_FIRST), encoding="utf-8")
    subprocess.call(wrapper(n, {}, *first, "--keep", "--timeout=8", f"--answers={quit_first}",
                            "--", "--new", mac=mac), stdout=log, stderr=subprocess.STDOUT)
    for action in actions:
        kind, value = action
        if kind == "tasmota password":
            for _ in range(12):   # the device may still be starting
                # As the tool sends Tasmota a command: a plain request was hung up on.
                said = in_container(n, "from tools.autoflash.device import Device; "
                                    f"print(Device('{DEVICE}').command('Backlog WebPassword {value}; Restart 1', "
                                    "timeout=20))")
                log.write(f"PREP tasmota password: {said}\n")
                if "Traceback" not in said:   # Backlog answers {}
                    break
                time.sleep(10)
            else:
                return False
            time.sleep(30)   # restarted: its settings saved
        elif kind == "locked esphome":
            image = build_locked(value)
            target = ROOT / f"captures/emulation/state-{n}/work/locked.ota.bin"
            shutil.copyfile(image, target)
            said = in_container(n, f"from tools.autoflash import esphome_ota; esphome_ota.upload('{DEVICE}', "
                                "open('locked.ota.bin', 'rb').read()); print('sent')")
            target.unlink(missing_ok=True)
            log.write(f"PREP locked esphome ({value}): {said}\n")
            # A minute on its new release, or a power cut takes the device back to the one it had.
            time.sleep(90)
            check = in_container(n, "import urllib.request, urllib.error\ntry:\n urllib.request.urlopen("
                                 f"'http://{DEVICE}/', timeout=5); print(200)\n"
                                 "except urllib.error.HTTPError as e: print(e.code)\nexcept Exception: print('down')")
            log.write(f"PREP its page answers: {check}\n")
            if check != "401":
                return False
        else:
            raise ValueError(f"no such prep: {kind}")
    log.flush()
    docker("stop", f"autoflash-emu-{n}")
    return True


def run_espressif(n: int, name: str, scenario: dict[str, Any], folder: Path, mac: str) -> tuple[int, Path | None]:
    """The scenario on emulator `n`: its exit code, and where its recording is."""
    with open(folder / "harness.log", "a", encoding="utf-8") as log:
        if not prepare(n, scenario, folder, log, mac):
            docker("stop", f"autoflash-emu-{n}")   # kept for what prep was to do
            return 125, None
        start = scenario.get("start", "fresh")
        first = [] if scenario.get("prep") and any(a != "sudo forgets" for a in scenario["prep"]) else (
            ["--fresh"] if start == "fresh" else [f"--from={start}"])
        stop = threading.Event()
        if "sudo forgets" in scenario.get("prep", []):
            threading.Thread(target=sudo_forgets, args=(n, stop), daemon=True).start()
        command = wrapper(n, scenario, *first, f"--timeout={scenario.get('minutes', 30)}",
                          f"--size={scenario.get('size', '100x40')}",
                          f"--answers={run_answers(scenario, folder)}", "--", "--new", *scenario.get("args", []),
                          mac=mac)
        log.write("--- " + " ".join(command[1:]) + "\n")
        log.flush()
        out = subprocess.run(command, capture_output=True, text=True, env=run_environment(scenario, mac))
        stop.set()
        log.write(out.stdout + out.stderr)
        docker("stop", f"autoflash-emu-{n}")
    found = re.search(r"Exit code (-?\d+)", out.stdout)
    where = re.search(r"On this computer: (\S+)", out.stdout)
    return (int(found.group(1)) if found else out.returncode), (ROOT / where.group(1) if where else None)


def run_again(n: int, scenario: dict[str, Any], folder: Path, mac: str) -> tuple[int, Path | None]:
    """The tool once more on emulator `n`'s device as the first run left it:
    no --new, so a run left unfinished is offered back."""
    again = scenario["again"]
    with open(folder / "harness.log", "a", encoding="utf-8") as log:
        command = wrapper(n, {"command": scenario.get("command")} | again, f"--timeout={again.get('minutes', 30)}",
                          f"--size={scenario.get('size', '100x40')}",
                          f"--answers={run_answers(again, folder, 'again-answers-given.json')}", "--",
                          *again.get("args", []), mac=mac)
        log.write("--- " + " ".join(command[1:]) + "\n")
        log.flush()
        out = subprocess.run(command, capture_output=True, text=True, env=run_environment(scenario | again, mac))
        log.write(out.stdout + out.stderr)
        docker("stop", f"autoflash-emu-{n}")
    found = re.search(r"Exit code (-?\d+)", out.stdout)
    where = re.search(r"On this computer: (\S+)", out.stdout)
    return (int(found.group(1)) if found else out.returncode), (ROOT / where.group(1) if where else None)


def run_beken(n: int, name: str, scenario: dict[str, Any], folder: Path, mac: str) -> tuple[int, Path | None]:
    """The scenario on Beken emulator `n`: a fresh S61, then the tool on the
    computer on its Wi-Fi (beken-emu.sh computer), its eWeLink sign-in typed."""
    emu = str(ROOT / "tools/beken/emulator/beken-emu.sh")
    state = ROOT / f"captures/emulation/beken/state-{n}"
    with open(folder / "harness.log", "a", encoding="utf-8") as log:
        subprocess.call([emu, "--instance", str(n), "--fresh", "device"], stdout=log, stderr=subprocess.STDOUT,
                        timeout=1200)
        (state / "work/captures").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(run_answers(scenario, folder), state / "work/captures/answers.json")
        command = [emu, "--instance", str(n), "computer", "tools/repo/unattended_run.py",
                   "--size", scenario.get("size", "100x40"), "--timeout", str(scenario.get("minutes", 60)),
                   "--attended", "--answers", "captures/answers.json", "--save", "captures/unattended/attended",
                   *(["--command", "readout"] if scenario.get("command") == "readout" else []),
                   "--", "--new", *scenario.get("args", [])]
        log.write("--- " + " ".join(command[1:]) + "\n")
        log.flush()
        out = subprocess.run(command, capture_output=True, text=True)
        log.write(out.stdout + out.stderr)
        for container in docker("ps", "--format", "{{.Names}}").stdout.split():
            if container.startswith(f"autoflash-beken-emu-{n}"):
                docker("stop", "-t", "30", container)
    found = re.search(r"Exit code (-?\d+)", out.stdout)
    return (int(found.group(1)) if found else out.returncode), state / "work/captures/unattended/attended"


def judged(scenario: dict[str, Any], folder: Path, code: int, recording: Path | None,
           prefix: str = "") -> list[str]:
    """A run's verdicts, its recording kept in `folder` (names `prefix`ed)."""
    if recording is None or not (recording / "screen.raw").exists():
        return [f"HARNESS: no run to judge (exit {code}); see harness.log"]
    for item in ("screen.raw", "screen.txt", "answers.json", "result.txt", "clipboard.txt"):
        (folder / f"{prefix}{item}").unlink(missing_ok=True)
        if (recording / item).exists():
            shutil.copyfile(recording / item, folder / f"{prefix}{item}")
    size = tuple(int(part) for part in scenario.get("size", "100x40").split("x"))
    result, copied = folder / f"{prefix}result.txt", folder / f"{prefix}clipboard.txt"
    # The recorder's stand-in keeps what was copied: no file, nothing was.
    return judge(scenario, (folder / f"{prefix}screen.raw").read_bytes(),
                 json.loads((folder / f"{prefix}answers.json").read_text(encoding="utf-8")), code, size,
                 result.read_text(encoding="utf-8").strip() if result.exists() else None,
                 copied.read_text(encoding="utf-8") if copied.exists() else "")


def device_of(family: str, mac: str | None) -> kinds.Kind:
    """The kind of device a family's scenarios run on: the S60 copy's, by its
    record's product model, or the S61 the Beken emulator runs."""
    if family == "beken":
        return kinds.SONOFF_S61
    return kinds.for_account((records.read_device(mac) if mac else {}).get("product_model"), None) or kinds.SONOFF_S60


def run_one(n: int, name: str, family: str, scenario: dict[str, Any], results: Path, mac: str,
            workers: int = 1) -> str:
    folder = results / name
    folder.mkdir(parents=True, exist_ok=True)
    run = run_beken if family == "beken" else run_espressif
    started = time.monotonic()
    try:
        code, recording = run(n, name, scenario, folder, mac)
    except Exception as exc:  # noqa: BLE001 - one scenario's trouble is its own; the rest go on
        code, recording = 125, None
        (folder / "harness.log").open("a", encoding="utf-8").write(f"HARNESS: {type(exc).__name__}: {exc}\n")
        docker("stop", f"autoflash-emu-{n}")
    verdicts = judged(scenario, folder, code, recording)
    if not verdicts and scenario.get("again") and family == "espressif":
        try:
            code, recording = run_again(n, scenario, folder, mac)
        except Exception as exc:  # noqa: BLE001 - as above
            code, recording = 125, None
            (folder / "harness.log").open("a", encoding="utf-8").write(f"HARNESS: {type(exc).__name__}: {exc}\n")
        verdicts = [f"{verdict.split(':')[0]}: again: {verdict.split(': ', 1)[1]}"
                    for verdict in judged(scenario["again"] | {"size": scenario.get("size", "100x40")},
                                          folder, code, recording, "again-")]
    verdict = "PASS" if not verdicts else verdicts[0].split(":")[0]
    # Kept beside every batch's folder, so one not sent goes with the next.
    emulator_stats.attended_run(RESULTS, family=family, device=device_of(family, mac).key, name=name,
                                verdict=verdict, seconds=round(time.monotonic() - started), workers=workers)
    line = f"{verdict:<16} {name:<28} {time.monotonic() - started:5.0f}s"
    (folder / "verdict.txt").write_text("\n".join(verdicts or ["PASS"]) + "\n", encoding="utf-8")
    return line + "".join(f"\n    {verdict}" for verdict in verdicts)


def run_all(chosen: list[str], found: dict[str, tuple[str, dict[str, Any]]], results: Path, mac: str | None,
            workers: int, first: int) -> list[str]:
    """Each chosen scenario, `workers` at once (the S61's one at a time,
    after), each on an emulator numbered from `first` that no scenario under
    way is using: a result line for each, printed as it comes."""
    def one(name: str, free: queue.SimpleQueue[int], at_once: int) -> str:
        n = free.get()
        try:
            return run_one(n, name, *found[name], results, mac, at_once)
        finally:
            free.put(n)

    lines = []
    # The S61's on their own, after the S60's: its emulator is slow, and run
    # beside three S60s a readout's device was not found in time.
    for family, at_once in (("espressif", max(1, workers)), ("beken", 1)):
        planned = [name for name in chosen if found[name][0] == family]
        if planned:
            emulator_stats.began(RESULTS, family=family, suite="attended", device=device_of(family, mac).key,
                                 total=sum(1 for each, _ in found.values() if each == family), planned=len(planned),
                                 cloud="stub", workers=at_once)
        free: queue.SimpleQueue[int] = queue.SimpleQueue()
        for n in range(first, first + at_once):
            free.put(n)
        with concurrent.futures.ThreadPoolExecutor(at_once) as pool:
            jobs = [pool.submit(one, name, free, at_once) for name in chosen if found[name][0] == family]
            for job in concurrent.futures.as_completed(jobs):
                lines.append(job.result())
                print(lines[-1], flush=True)
    return lines


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["list", "run"])
    parser.add_argument("names", nargs="*", help="the scenarios to run (default: all)")
    parser.add_argument("--workers", type=int, default=1, help="scenarios at once, each on an emulator of its own")
    parser.add_argument("--first-instance", type=int, default=FIRST_INSTANCE,
                        help="the first emulator instance it uses, one more for each worker")
    parser.add_argument("--device", help="the backed-up S60 the Espressif scenarios start from, its MAC "
                        "as emu.sh --device takes it")
    args = parser.parse_args()
    found = scenarios()
    if args.command == "list":
        for name, (family, scenario) in found.items():
            print(f"{name:<28} {family:<10} {scenario.get('about', '')}")
        return 0
    chosen = args.names or list(found)
    unknown = [name for name in chosen if name not in found]
    if unknown:
        parser.error(f"no such scenario: {', '.join(unknown)}")
    if not args.device and any(found[name][0] == "espressif" for name in chosen):
        parser.error("an S60's scenarios need --device: the backed-up device the emulator runs a copy of")
    results = RESULTS / datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    results.mkdir(parents=True)
    lines = run_all(chosen, found, results, args.device, args.workers, args.first_instance)
    (results / "results.txt").write_text("\n".join(sorted(lines)) + "\n", encoding="utf-8")
    print(emulator_stats.send(RESULTS), flush=True)
    print(f"Results: {results.relative_to(ROOT)}/results.txt")
    return 0 if all(line.startswith("PASS") for line in lines) else 1


if __name__ == "__main__":
    raise SystemExit(main())
