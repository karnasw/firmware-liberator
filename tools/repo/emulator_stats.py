#!/usr/bin/env python3
"""Each emulator run's outcome, for the admin view's emulator figures
(website/admin/emulator.js): how many runs, of which device and firmware, how
many passed, and what failed - each fault with its length, why each run that
stopped did by the tool's own classes, the emulator image's tag - never a
run's report or log, which stay on the machine that ran it.

    emulator_stats.py send DIR...    send what the suites in DIR kept unsent
    emulator_stats.py check          whether this computer can send: its
                                     credentials, and the admin view taking them
    emulator_stats.py push [HOST...] this computer's credentials copied to each
                                     HOST (ssh), else to EMULATOR_STATS_HOSTS's

Every suite of every chip family - fault, attended and the final-firmware
check alike - calls began() as a batch begins, with how many scenarios its
suite has for the device and how many it runs (what the view's share of a
device's scenarios passed is of), and record() (fault_run, attended_run,
check_run) for each scenario's run as it writes its own result, saying
whether the scenario had run in that folder before (`rerun`): a batch runs
what has not passed yet, and its re-runs would read as a worse pass rate.
Each is kept in the suite's results folder (UNSENT, UNSENT_BATCHES) and sent
at once, beside the suite, so the figures fill as runs end and a machine
thrown away loses none sent. send() as the batch ends sends whatever is
left. A run that could not be sent (no
network, no credentials) goes with the next one, or by `send`, up to the admin
view's OLDEST_DAYS. AUTOFLASH_EMU_NO_STATS=1 keeps and sends none, as in the
tests (tests/no_network.py).

Sent with Access's service token for the suites, which never expires and can
send runs and do nothing else. tools/repo/cloudflare_settings.py makes and
rotates it, and writes it to CREDENTIALS, one file a computer that every
checkout on it reads - a new worktree too - and pushes it to each machine
EMULATOR_STATS_HOSTS names (.env.infra: your own machines, never committed).
A file of its own: not .env, the tool's, nor .env.infra, the maintainer's
tokens that change the project's services, which stays on the maintainer's
computer. The environment's EMULATOR_STATS_CLIENT_ID and
EMULATOR_STATS_CLIENT_SECRET win over it. Without either nothing is sent,
said as the first batch begins, so that a machine is not thrown away with
its runs unsent, and again as each ends.
"""

from __future__ import annotations

import datetime as dt
import functools
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import __version__, credentials, kinds  # noqa: E402

# What Access's application for the suites covers (cloudflare_settings.py
# UPLOAD_APP): every path the suites use, the runs' and any to come.
SCOPE_URL = "https://admin.firmwareliberator.com/v1/emulator"
UPLOAD_URL = SCOPE_URL + "/runs"
# Each kind sent: its file of those kept, its path under the scope, its body's key.
KINDS = (("emulator-stats-unsent.jsonl", "/runs", "runs"), ("emulator-stats-batches-unsent.jsonl", "/batches", "batches"))
# The credentials, one file a computer, readable by its owner alone; where
# push writes them on another machine, under the same name in its home.
CREDENTIALS_PATH = ".config/firmware-liberator/emulator.env"
CREDENTIALS = Path.home() / CREDENTIALS_PATH
CLIENT_ID = "EMULATOR_STATS_CLIENT_ID"
CLIENT_SECRET = "EMULATOR_STATS_CLIENT_SECRET"
# How a token no computer has is had again: Access shows a secret only as it
# makes one, so it is given a new one, and the old stops.
ROTATE = ("python3 tools/repo/cloudflare_settings.py rotate emulator-stats (a new secret, written on the "
          "computer it runs on and copied to EMULATOR_STATS_HOSTS)")
# The machines push copies the credentials to: ssh's names for them,
# space-separated, from the environment or .env.infra.
HOSTS = "EMULATOR_STATS_HOSTS"
INFRA_FILE = ROOT / "tools" / "repo" / ".env.infra"
# The runs and batches kept to send, in each suite's results folder.
UNSENT, UNSENT_BATCHES = KINDS[0][0], KINDS[1][0]
# Runs a request at most: the admin view's MAX_RUNS (a test holds them alike).
CHUNK = 500
TIMEOUT = 30.0
FIRMWARE_FILE = ROOT / "artifacts" / "firmware.json"
# One suite run's id, the same for every run it records: a batch.
BATCH = secrets.token_hex(8)

# The file of runs kept, read and written under _kept; one sending at a time,
# under _sending; and the folders whose runs a send under way did not take
# yet, which it then sends too (_in_background).
_kept = threading.Lock()
_sending = threading.Lock()
_waiting: set[Path] = set()


def off() -> bool:
    """Whether runs are neither kept nor sent: AUTOFLASH_EMU_NO_STATS."""
    return os.environ.get("AUTOFLASH_EMU_NO_STATS", "").strip().lower() in ("1", "yes", "true", "on")


def scope_url() -> str:
    """Where runs and batches go: under the admin view's SCOPE_URL, or a
    local one a test gives."""
    return os.environ.get("AUTOFLASH_EMU_STATS_URL") or SCOPE_URL


def _parsed(path: Path) -> dict[str, str]:
    try:
        return credentials.parse(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def client() -> tuple[str, str] | None:
    """The service token's id and secret, from the environment or
    CREDENTIALS; None without both."""
    kept = _parsed(CREDENTIALS)
    found = [os.environ.get(name) or kept.get(name) for name in (CLIENT_ID, CLIENT_SECRET)]
    return (found[0], found[1]) if all(found) else None


def write_credentials(client_id: str, secret: str) -> Path:
    """The token's id and secret written to CREDENTIALS, for this computer's
    owner alone (600, in a folder of 700)."""
    CREDENTIALS.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    made = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    text = (f"# The emulator suites' service token (tools/repo/emulator_stats.py), written by "
            f"tools/repo/cloudflare_settings.py on {made}.\n{CLIENT_ID}={client_id}\n{CLIENT_SECRET}={secret}\n")
    new = CREDENTIALS.with_name(CREDENTIALS.name + ".new")
    descriptor = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as kept:
        os.fchmod(kept.fileno(), 0o600)
        kept.write(text)
    os.replace(new, CREDENTIALS)
    return CREDENTIALS


def hosts() -> list[str]:
    """The machines push copies the credentials to: EMULATOR_STATS_HOSTS."""
    return (os.environ.get(HOSTS) or _parsed(INFRA_FILE).get(HOSTS) or "").split()


def push(to: list[str]) -> list[str]:
    """This computer's CREDENTIALS copied to each machine in `to`, over ssh,
    as the same file there (600, replaced whole): a line for each that
    failed, [] when all took it."""
    try:
        text = CREDENTIALS.read_text(encoding="utf-8")
    except OSError:
        return [f"no {CREDENTIALS} here to copy: {ROTATE}"]
    # The secret goes on ssh's input, never on a command line.
    script = (f"umask 077 && mkdir -p \"$HOME/{Path(CREDENTIALS_PATH).parent}\" && "
              f"cat > \"$HOME/{CREDENTIALS_PATH}.new\" && mv -f \"$HOME/{CREDENTIALS_PATH}.new\" \"$HOME/{CREDENTIALS_PATH}\"")
    failed = []
    for host in to:
        try:
            done = subprocess.run(["ssh", "-o", "BatchMode=yes", host, script], input=text, capture_output=True,
                                  text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            failed.append(f"{host}: {error}")
            continue
        if done.returncode:
            failed.append(f"{host}: ssh ended {done.returncode}: {done.stderr.strip()[-200:]}")
    return failed


@functools.cache
def revision() -> tuple[str | None, bool]:
    """The checkout's commit, and whether it has changes beside it - so a run
    of work in progress is not taken for that commit's."""
    try:
        head = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True,
                              timeout=10, check=True).stdout.strip()
        changed = subprocess.run(["git", "-C", str(ROOT), "status", "--porcelain", "--untracked-files=no"],
                                 capture_output=True, text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None, False
    return (head[:12] if re.fullmatch(r"[0-9a-f]{40}", head) else None), bool(changed)


@functools.cache
def firmware(device: str) -> dict[str, str]:
    """What this checkout installs on `device` (a kind's key), by name and
    version, as artifacts/firmware.json pins it: its family's entries for its
    chip and for the device itself."""
    kind = kinds.find(device)   # ready or not: the suites run devices before they are
    try:
        pinned = json.loads(FIRMWARE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if kind is None:
        return {}
    family = pinned.get(kind.family) or {}
    found = {}
    for entry in (family.get(kind.chip) or {}, family.get(kind.key) or {}):
        for name, about in entry.items():
            if isinstance(about, dict) and isinstance(about.get("version"), str):
                found[name] = about["version"][:40]
    return found


@functools.cache
def image(family: str) -> str | None:
    """The tag of `family`'s emulator image as this checkout builds it
    (tools/<family>/emulator/image.sh tag, a hash of what it is built from):
    so runs before and after a change to the emulator can be told apart."""
    try:
        tag = subprocess.run([str(ROOT / "tools" / family / "emulator" / "image.sh"), "tag"], capture_output=True,
                             text=True, timeout=30, check=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return tag if re.fullmatch(r"[0-9a-f]{8,64}", tag) else None


# At most this many stopped runs a scenario's run says why of (the admin view's
# STOPPED_MOST): a fault suite's scenario gives up after a few.
STOPPED_MOST = 16


def stopped_by(outcomes: Any) -> list[str]:
    """Why each of a scenario's runs that stopped did, in order, as the run
    itself classed it (its report's stopped error, failures.classify): from
    the outcomes each run kept (fault_verdicts.run_outcomes). A scenario that
    passed on its second run says why the first stopped; a run the person
    stopped (a scenario's Ctrl-C) did not stop of itself, and is left out."""
    said = [outcome["stopped"]["error"] for outcome in outcomes or ()
            if isinstance(outcome, dict) and outcome.get("result") != "cancelled"
            and isinstance(outcome.get("stopped"), dict)
            and re.fullmatch(r"[a-z][a-z0-9-]{0,31}", str(outcome["stopped"].get("error")))]
    return said[:STOPPED_MOST]


def error_of(said: Any) -> str | None:
    """The exception a suite's own error was, by class alone - never its
    message, which can name the machine's paths: "FileNotFoundError(2, ...)"
    is FileNotFoundError."""
    found = re.match(r"([A-Za-z_][A-Za-z0-9_.]{0,63})\(", str(said or ""))
    return found.group(1) if found else None


def _short(text: Any, most: int = 64) -> str | None:
    """`text` as the admin view takes it: printable ASCII, at most `most`."""
    if text is None or text == "":
        return None
    said = re.sub(r"[^\x20-\x7e]", "?", str(text))[:most]
    return said or None


def verdict_of(said: str) -> str:
    """A suite's verdict as one word: "FAIL-SCREEN: shown ..." is FAIL-SCREEN."""
    head = str(said).split(":", 1)[0].strip().upper().replace(" ", "-")
    return head[:40] if re.fullmatch(r"[A-Z][A-Z0-9-]{0,39}", head[:40]) else "ERROR"


def _whole(value: Any, low: int, high: int) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if low <= number <= high else None


def run_row(*, family: str, suite: str, device: str, scenario: str, verdict: str, cloud: str,
            route: str | None = None, step: str | None = None, fault: Any = None, arg: Any = None,
            at: Any = None, tries: Any = None, seconds: Any = None, workers: Any = None, rerun: bool = False,
            outcomes: Any = None, error: Any = None, starved: Any = False,
            finished: dt.datetime | None = None) -> dict[str, Any]:
    """One run, as the admin view takes it (emulator.js RUN_FIELDS): with the
    fault's argument (`arg`, an outage's length), why each of its runs that
    stopped did (stopped_by, from `outcomes`), the suite's own error by class
    (error_of), whether the computer was starved of memory, and the
    emulator image's tag."""
    head, dirty = revision()
    when = (finished or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    return {
        "id": secrets.token_hex(16), "finished": when.strftime("%Y-%m-%dT%H:%M:%SZ"), "batch": BATCH,
        "suite": suite, "family": family, "device": device, "scenario": _short(scenario, 200) or "?",
        "route": _short(route), "step": _short(step), "fault": _short(fault), "arg": _short(arg),
        "at": _whole(at, 0, 100),
        "verdict": verdict_of(verdict), "rerun": bool(rerun), "tries": _whole(tries, 0, 99), "seconds": _whole(seconds, 0, 172_800),
        "cloud": cloud, "tool": __version__, "revision": head, "dirty": dirty, "firmware": firmware(device),
        "workers": _whole(workers, 1, 256), "stopped_by": stopped_by(outcomes), "error": error_of(error),
        "starved": bool(starved), "image": image(family),
    }


def _keep(folder: Path, name: str, row: dict[str, Any]) -> None:
    """`row` kept in `folder`'s file `name`, and sent at once, beside the suite."""
    folder.mkdir(parents=True, exist_ok=True)
    with _kept, open(folder / name, "a", encoding="utf-8") as kept:
        kept.write(json.dumps(row) + "\n")
    threading.Thread(target=_in_background, args=(folder,), daemon=True).start()


def record(folder: Path, **run: Any) -> None:
    """A scenario's run kept in `folder` (its suite's results) and sent at
    once, beside the suite: run_row()'s arguments."""
    if not off():
        _keep(folder, UNSENT, run_row(**run))


# Which list a batch ran: every scenario of its suite, a routine run's
# (fault_suite.py list quick), or some other part of it.
TIERS = ("full", "quick", "subset")


def tier(listed: Any, every: Any, quick: Any) -> str:
    """Which of TIERS a batch's list of scenario ids is, of its suite's every
    and quick ones."""
    listed = set(listed)
    return "full" if listed >= set(every) else "quick" if listed == set(quick) else "subset"


# Whether this process has said it has no credentials: a suite may begin a
# batch for each device.
_told_unsent = False


def began(folder: Path, *, family: str, suite: str, device: str, total: int, planned: int, cloud: str,
          workers: Any = None, tier: str = "full") -> None:
    """A batch begun, kept in `folder` and sent: its suite's scenarios for the
    device (`total`), how many it runs (`planned`), and which list it ran
    (`tier`, one of TIERS): a routine run's coverage is not a gap."""
    global _told_unsent
    if off():
        return
    if not _told_unsent and client() is None:
        _told_unsent = True
        print(f"Emulator stats: not sent - this computer has no emulator-stats token ({CREDENTIALS} missing). "
              f"Runs are kept in {folder}.\n"
              f"  To add it: {ROTATE}\n"
              f"  Then here: python3 tools/repo/emulator_stats.py send {folder}", file=sys.stderr, flush=True)
    head, dirty = revision()
    _keep(folder, UNSENT_BATCHES, {
        "batch": BATCH, "started": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "family": family, "device": device, "suite": suite, "total": max(1, int(total)), "planned": int(planned),
        "cloud": cloud, "tool": __version__, "revision": head, "dirty": dirty, "workers": _whole(workers, 1, 256),
        "tier": tier if tier in TIERS else "subset"})


def _in_background(folder: Path) -> None:
    """`folder`'s runs sent, unless a send is under way: that one sends them
    as it ends. Nothing is said: what is left is said as the batch ends."""
    _waiting.add(folder)
    while folder in _waiting:
        if not _sending.acquire(blocking=False):
            return
        try:
            _waiting.discard(folder)
            _send(folder)
        finally:
            _sending.release()


def fault_run(folder: Path, *, family: str, device: str, scenario: dict[str, Any], verdict: str, tries: Any,
              seconds: Any, cloud: str, workers: Any, rerun: bool, outcomes: Any = None, error: Any = None,
              starved: Any = False) -> None:
    """A fault suite's scenario run, kept: `scenario` as its suite's scenarios()
    yields it (id, route, step, fault, arg, at), as every family's does; how
    each of its runs ended (fault_verdicts.run_outcomes), the suite's own error
    and whether it was starved of memory, as its result row has them."""
    record(folder, family=family, suite="fault", device=device, scenario=scenario["id"],
           route=scenario.get("route"), step=scenario.get("step"), fault=scenario.get("fault"),
           arg=scenario.get("arg"), at=scenario.get("at"), verdict=verdict, tries=tries, seconds=seconds,
           cloud=cloud, workers=workers, rerun=rerun, outcomes=outcomes, error=error, starved=starved)


def attended_run(folder: Path, *, family: str, device: str, name: str, verdict: str, seconds: Any,
                 workers: Any) -> None:
    """An attended run, kept: always on the stub (attended_suite.py), and each
    a first try, as the suite runs every scenario it is given."""
    record(folder, family=family, suite="attended", device=device, scenario=name, verdict=verdict,
           seconds=seconds, cloud="stub", workers=workers)


def check_run(folder: Path, *, family: str, device: str, route: str, passed: bool, seconds: Any,
              cloud: str) -> None:
    """One conversion of the final-firmware check (tools/repo/emulator_check.py),
    kept: `route` as "start>firmware", the same for every family."""
    record(folder, family=family, suite="check", device=device, scenario=f"check:{route}", route=route,
           verdict="PASS" if passed else "FAIL", seconds=seconds, cloud=cloud, workers=2)


def _post(path: str, key: str, rows: list[dict[str, Any]], token: tuple[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(scope_url() + path, data=json.dumps({key: rows}).encode(), method="POST", headers={
        "Content-Type": "application/json", "CF-Access-Client-Id": token[0], "CF-Access-Client-Secret": token[1]})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.loads(response.read())


def _read(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def send(folder: Path) -> str:
    """The runs `folder` keeps, sent, after any send under way: a line saying
    how it went."""
    if off():
        return "Emulator stats: off (AUTOFLASH_EMU_NO_STATS)"
    with _sending:
        _waiting.discard(folder)   # all it keeps is sent now
        return _send(folder)


def _id(key: str, row: dict[str, Any]) -> str:
    """How the admin view names a run or batch in its answer."""
    if key == "runs":
        return str(row.get("id"))
    return "/".join(str(row.get(name)) for name in ("batch", "family", "device", "suite"))


def _send(folder: Path) -> str:
    """send()'s work, with _sending held: the batches kept, then the runs.
    Those taken, and those the admin view refuses (which it always would),
    are no longer kept; the rest are, for next time. Each file is replaced
    whole, so a process cut off part way loses none; those kept while this
    one sends stay for the next."""
    sent: dict[str, int] = {}
    refused: list[str] = []
    left = 0
    problem = None
    token = client()
    for name, path_under, key in reversed(KINDS):
        path = folder / name
        with _kept:
            rows = _read(path)
        if not rows:
            continue
        if token is None:
            left += len(rows)
            continue
        done: set[str] = set()
        for start in range(0, len(rows), CHUNK):
            try:
                answer = _post(path_under, key, rows[start:start + CHUNK], token)
            except (OSError, ValueError) as error:
                problem = (f"the admin view answered {error.code}" if isinstance(error, urllib.error.HTTPError)
                           else str(error))
                break
            taken = set(answer.get("taken") or [])
            done |= taken
            sent[key] = sent.get(key, 0) + len(taken)
            for one in answer.get("refused") or []:
                done.add(one.get("id"))
                refused.append(f"{one.get('id')}: {one.get('why')}")
        with _kept:
            kept = [row for row in _read(path) if _id(key, row) not in done]
            if kept:
                new = path.with_name(path.name + ".new")
                new.write_text("".join(json.dumps(row) + "\n" for row in kept), encoding="utf-8")
                os.replace(new, path)
            else:
                path.unlink(missing_ok=True)
        left += len(kept)
    if token is None and left:
        return (f"Emulator stats: {left} kept, not sent - no {CLIENT_ID} and {CLIENT_SECRET} "
                f"({CREDENTIALS}: tools/repo/cloudflare_settings.py writes it)")
    if not sent and not refused and not left:
        return "Emulator stats: none to send"
    said = f"Emulator stats: {sent.get('runs', 0)} sent" + (f", {sent['batches']} batches" if sent.get("batches") else "")
    if refused:
        said += f", {len(refused)} refused ({'; '.join(refused[:3])})"
    if left:
        said += f", {left} kept to send later" + (f" ({problem})" if problem else "")
    return said


def main(argv: list[str]) -> int:
    if argv[:1] == ["send"] and len(argv) > 1:
        for folder in argv[1:]:
            print(f"{folder}: {send(Path(folder))}")
        return 0
    if argv[:1] == ["push"]:
        to = argv[1:] or hosts()
        if not to:
            print(f"No machines named: give them, or set {HOSTS} in tools/repo/.env.infra", file=sys.stderr)
            return 2
        failed = push(to)
        for line in failed:
            print(f"Not copied: {line}")
        print(f"Copied to {len(to) - len(failed)} of {len(to)}")
        return 1 if failed else 0
    if argv == ["check"]:
        token = client()
        if token is None:
            print(f"No {CLIENT_ID} and {CLIENT_SECRET}, in the environment or {CREDENTIALS}: "
                  "python3 tools/repo/cloudflare_settings.py apply writes them, and push copies them here")
            return 1
        request = urllib.request.Request(scope_url() + "/runs", method="GET", headers={
            "CF-Access-Client-Id": token[0], "CF-Access-Client-Secret": token[1]})
        try:
            urllib.request.urlopen(request, timeout=TIMEOUT)
        except urllib.error.HTTPError as error:
            # 405: past Access and the view's own check, to a path that takes POST alone.
            if error.code == 405:
                print("The admin view takes this computer's runs")
                return 0
            print(f"The admin view refuses this computer's runs ({error.code}): "
                  "python3 tools/repo/cloudflare_settings.py says why")
            return 1
        except OSError as error:
            print(f"The admin view could not be reached: {error}")
            return 1
        print("The admin view answered as it never should: python3 tools/repo/cloudflare_settings.py says why")
        return 1
    print(__doc__.split("\n\n")[1], file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
