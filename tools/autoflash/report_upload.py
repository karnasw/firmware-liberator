"""Each run's report, sent to the project's API when the run ends
(website/api/reports.js), so how well the tool converts each device can be
counted: what report_data.py makes, never anything of the person, their
computer or their network. A readout sends one too, for each device, as its
readout ends (begin_readout, send_readout): one kind of readout report,
however many devices a sitting reads out.

- On by default; off with --no-report, AUTOFLASH_NO_REPORT=1 (the environment
  or .env), in the emulator, and for a dry or simulated run. The first time
  the tool runs on a computer it says so, once.
- The API asks each report for its proof of work, made for the report's
  random id before the report exists (proof_of_work.py): begun as the run
  begins, in a process of its own at the lowest priority, so it is done
  long before the run ends and never slows it.
- A report is kept on this computer (QUEUE) before it is sent, and removed
  only once the API has answered for it, so a run whose network was down, or
  whose window was closed, has its report sent at the next start. KEEP at
  most, each for KEEP_DAYS.
- Sending happens beside the run, as its report is read: nothing waits on
  it, and nothing of it is said on the screen, only in the run's log.
"""

from __future__ import annotations

import atexit
import datetime as dt
import json
import os
import secrets
import subprocess
import sys
import threading
import urllib.request
from pathlib import Path
from typing import Any, TYPE_CHECKING

from tools.autoflash import __version__, hostsys, interrupt, output, proof_of_work, report_data
from tools.autoflash.runs import ROOT

if TYPE_CHECKING:
    from tools.autoflash.context import Context

UPLOAD_URL = "https://api.firmwareliberator.com/v1/reports"
# A report's ID, random: 64 bits, 16 hex digits - short, as a run that failed
# shows it to paste into an issue, and still unlikely to come again (about
# one in 30 million among a million reports), as the API takes a report sent
# twice once, by its ID.
REPORT_ID_BYTES = 8
# The work the Worker asks of each report (reports.js's REPORT_PROOF_BITS).
PROOF_BITS = 24
QUEUE = ROOT / "captures" / "reports"
NOTICE = QUEUE / "notice-shown"
KEEP = 99
KEEP_DAYS = 30
# How long sending waits on a report's work at most, and on the API.
WORK_WAIT = 300.0
SEND_TIMEOUT = 30.0
# How long the tool, exiting, waits for a report being sent: no longer, as
# one not sent is sent at the next start.
EXIT_WAIT = 5.0
WHAT_IS_SENT = "https://firmwareliberator.com/docs/what-the-tool-sends"
# Said the first time on a computer, with WHAT_IS_SENT linked under it.
NOTICE_TEXT = "Anonymous reports of each run help improve the tool; --no-report stops them."


def upload_url() -> str:
    """Where reports go: the API, but for a local one a test gives."""
    return os.environ.get("AUTOFLASH_REPORTS_URL") or UPLOAD_URL


def enabled(ctx: Context) -> bool:
    """Whether this run sends its report."""
    args = getattr(ctx, "args", None)
    if any(getattr(args, flag, False) for flag in ("no_report", "dry_run", "simulate_backup_failure",
                                                   "simulate_bad_image", "simulate_unknown_table")):
        return False
    if os.environ.get("AUTOFLASH_NO_REPORT", "").strip().lower() in ("1", "yes", "true", "on"):
        return False
    return not hostsys.emulated()


def start_work(report_id: str) -> subprocess.Popen[str]:
    """The report's proof of work, begun in a process of its own at the lowest priority."""
    command = [sys.executable, "-m", "tools.autoflash.proof_of_work", proof_of_work.report_subject(report_id),
               str(PROOF_BITS)]
    extra: dict[str, Any] = {}
    if hostsys.IS_WINDOWS:
        # Below normal priority, and no window of its own (Windows' values,
        # as subprocess names them there alone).
        extra["creationflags"] = (getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0x4000)
                                  | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))
    process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, **extra)
    atexit.register(stop, process)
    return process


def stop(process: subprocess.Popen[str]) -> None:
    """A proof's process, ended with the tool: its work is done again next time."""
    if process.poll() is None:
        process.kill()


def proof_of(process: subprocess.Popen[str] | None, timeout: float = WORK_WAIT) -> str | None:
    """The work the process made, or None if it has not in `timeout`."""
    if process is None:
        return None
    try:
        made, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        return None
    made = (made or "").strip()
    return made if process.returncode == 0 and made else None


# The reports kept to send.
def queued() -> list[Path]:
    return sorted(QUEUE.glob("*.json"), key=lambda path: path.stat().st_mtime)


def keep(item: dict[str, Any]) -> None:
    QUEUE.mkdir(parents=True, exist_ok=True)
    path = QUEUE / f"{item['report']['id']}.json"
    part = path.with_suffix(".part")
    part.write_text(json.dumps(item), encoding="utf-8")
    os.replace(part, path)


def forget(report_id: str) -> None:
    (QUEUE / f"{report_id}.json").unlink(missing_ok=True)


def prune(today: dt.date) -> list[dict[str, Any]]:
    """The reports kept, newest KEEP of the last KEEP_DAYS, oldest first: the
    rest deleted, and any file that is not one."""
    kept = []
    for path in queued():
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
            made = dt.date.fromisoformat(item["made"])
            item["report"]["id"]
        except (OSError, ValueError, KeyError, TypeError):
            path.unlink(missing_ok=True)
            continue
        if (today - made).days > KEEP_DAYS:
            path.unlink(missing_ok=True)
            continue
        kept.append(item)
    for item in kept[:-KEEP]:
        forget(item["report"]["id"])
    return kept[-KEEP:]


def post(reports: list[dict[str, Any]], url: str | None = None) -> dict[str, Any]:
    """Send reports, as the API takes them: its answer."""
    body = json.dumps({"reports": reports}).encode()
    request = urllib.request.Request(url or upload_url(), data=body, method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"Autoflash/{__version__}"})
    with hostsys.urlopen(request, timeout=SEND_TIMEOUT) as response:
        return json.loads(response.read().decode("utf-8"))


# One sending at a time: the start's, of those kept before, and the end's.
SENDING = threading.Lock()


def send_kept(today: dt.date, waiting: dict[str, subprocess.Popen[str]] | None = None) -> None:
    """Every report kept, its work made where it has none, sent; each the API
    answered for (accepted, a duplicate, or rejected) forgotten, the rest kept."""
    with SENDING:
        sending(today, waiting or {})


def sending(today: dt.date, waiting: dict[str, subprocess.Popen[str]]) -> None:
    ready = []
    for item in prune(today):
        report_id = item["report"]["id"]
        if not item.get("proof"):
            item["proof"] = proof_of(waiting.get(report_id) or start_work(report_id))
            if not item["proof"]:
                continue
            keep(item)
        ready.append({**item["report"], "proof": item["proof"]})
    if not ready:
        return
    try:
        answer = post(ready)
    except (OSError, ValueError) as error:   # an HTTPError is an OSError
        output.log_only(f"Reports not sent this time ({len(ready)} kept to send later): {error}")
        return
    done = [*answer.get("accepted", []), *answer.get("duplicate", []),
            *(item.get("id") for item in answer.get("rejected", []))]
    for report_id in done:
        if isinstance(report_id, str):
            forget(report_id)
    for item in answer.get("rejected", []):
        output.log_only(f"A report was turned away: {item.get('why')}")
    output.log_only(f"Reports sent: {len(answer.get('accepted', []))} taken, {len(answer.get('later', []))} "
                    "kept for another day.")


def in_background(work: Any) -> threading.Thread:
    """`work` beside the run, never in its way; waited for a moment at exit."""
    def run() -> None:
        try:
            work()
        except Exception as error:  # noqa: BLE001 - a report is never worth stopping a run for
            output.log_only(f"Reports: {type(error).__name__}: {error}")
    thread = threading.Thread(target=run, name="reports", daemon=True)
    thread.start()
    atexit.register(thread.join, EXIT_WAIT)
    return thread


def begin(ctx: Context) -> None:
    """At a conversion run's start: its report's id and work begun, any kept
    from before sent, and the notice said the first time on this computer.
    What its report counts of this run alone starts again here."""
    hostsys.asked_for_admin = hostsys.security_note_shown = False
    interrupt.reset_carried_on()
    if not enabled(ctx):
        return
    ctx.report_id = secrets.token_hex(REPORT_ID_BYTES)
    try:
        ctx.report_work = start_work(ctx.report_id)
    except OSError as error:   # made at the end, or the next start, instead
        output.log_only(f"The report's work could not be begun: {error}")
    announce(ctx)


def announce(ctx: Context) -> None:
    """At a run's start, a conversion's or a readout's: the notice said the
    first time on this computer, and any report kept from before sent."""
    if not enabled(ctx):
        return
    if not NOTICE.exists():
        output.tell(NOTICE_TEXT)
        output.show_link(WHAT_IS_SENT, "What is sent")
        try:
            QUEUE.mkdir(parents=True, exist_ok=True)
            NOTICE.write_text(f"{dt.date.today().isoformat()}\n", encoding="utf-8")
        except OSError:
            pass   # said again next time: no harm
    if any(QUEUE.glob("*.json")):
        in_background(lambda: send_kept(dt.date.today()))


def begin_readout(ctx: Context) -> None:
    """At each device's readout's start (readout.run): its report's id, and
    its work begun as a conversion's is."""
    ctx.readout_report_id = None
    if not enabled(ctx):
        return
    ctx.readout_report_id = secrets.token_hex(REPORT_ID_BYTES)
    try:
        ctx.readout_report_work = start_work(ctx.readout_report_id)
    except OSError as error:   # made as it is sent instead
        ctx.readout_report_work = None
        output.log_only(f"The readout's report's work could not be begun: {error}")


def send_readout(ctx: Context, became: Any, seconds: float) -> threading.Thread | None:
    """As a device's readout ends, however it ends: its report kept, then sent
    beside the run, with any kept from before. One a readout."""
    report_id = getattr(ctx, "readout_report_id", None)
    if not report_id or not enabled(ctx):
        return None
    ctx.readout_report_id = None
    try:
        keep({"report": report_data.readout_payload(report_id, became, seconds), "proof": None,
              "made": dt.date.today().isoformat()})
    except Exception as error:  # noqa: BLE001 - a report is never worth spoiling a readout for
        output.log_only(f"The readout's report could not be kept: {type(error).__name__}: {error}")
        return None
    became.report_id = report_id   # on its report on the screen, and in an issue's address
    work = getattr(ctx, "readout_report_work", None)
    return in_background(lambda: send_kept(dt.date.today(), {report_id: work}))


# Each run's outcome, a line a run that ended with a report, in its folder:
# what the emulators' fault suites read of why each run stopped, as the run
# itself classed it (report_data.outcome), whether reports are sent or not.
# A line a run, as a run carried on by the next writes in the same folder.
OUTCOMES = "outcomes.jsonl"


def keep_outcome(ctx: Context, ending: str, hardware: dict[str, str] | None = None) -> None:
    """The run's outcome added to its folder's OUTCOMES: when, its result,
    where it stopped and with what class (the words stay in run.log), and,
    for one that converted, its end-of-run checks: the report's answer, and
    whether the meter confirmed the relay switched (None where it has none)."""
    state = getattr(ctx, "state", None)
    if state is None:
        return   # a run that never began one
    try:
        result, stopped = report_data.outcome(ctx, ending)
        line = {"at": round(dt.datetime.now(dt.timezone.utc).timestamp(), 3), "result": result,
                "stopped": {"step": stopped["step"], "error": stopped["error"]} if stopped else None}
        if hardware is not None:
            from tools.autoflash import report   # which sends through this module
            relay = hardware.get("Relay")
            line["checks"] = {"answer": report_data.checks_word(hardware), "relay": relay,
                              "meter_confirms": relay == report.METER_CONFIRMS if "Power meter" in hardware else None}
        with open(state.path.parent / OUTCOMES, "a", encoding="utf-8") as kept:
            kept.write(json.dumps(line) + "\n")
    except Exception as error:  # noqa: BLE001 - as the report, never worth spoiling a run's end for
        output.log_only(f"The run's outcome could not be kept: {type(error).__name__}: {error}")


def send(ctx: Context, ending: str, seconds: float, resumed: bool,
         hardware: dict[str, str] | None = None) -> threading.Thread | None:
    """At a run's end: its outcome kept (keep_outcome), and its report kept,
    then sent beside the run, with any kept from before. `ending`:
    "converted", "failed", "crashed" or "cancelled"."""
    keep_outcome(ctx, ending, hardware)
    report_id = getattr(ctx, "report_id", None)
    if not report_id or not enabled(ctx):
        return None
    ctx.report_id = None   # one report a run
    try:
        payload = report_data.payload(ctx, report_id, seconds, resumed, ending, hardware)
        keep({"report": payload, "proof": None, "made": dt.date.today().isoformat()})
    except Exception as error:  # noqa: BLE001 - a report is never worth spoiling a run's end for
        output.log_only(f"The run's report could not be kept: {type(error).__name__}: {error}")
        return None
    ctx.last_report_id = report_id   # on its report on the screen, and in an issue's address
    return in_background(lambda: send_kept(dt.date.today(), {report_id: getattr(ctx, "report_work", None)}))
