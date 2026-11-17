"""Where the conversion's messages go.

Everything is written to the run's log file, with the time it happened.  What
the terminal shows depends on the mode:

- concise (the default for a run): one line per stage, redrawn in place as it
  goes from "underway", a spinner turning beside it, to "complete" (the time
  it took is the log's and the report's), plus anything the person running
  it has to read or answer;
- verbose: every message, as the tool has always printed them.

Before a run starts, and for the commands that only report (status, list,
cleanup), everything is shown: there is nothing to condense.
"""

from __future__ import annotations

import codecs
import collections
import contextlib
import datetime
import ipaddress
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Callable, NamedTuple

from . import __version__, interrupt

IS_WINDOWS = platform.system() == "Windows"

GREEN, RED, YELLOW, BOLD, RESET = "\x1b[32m", "\x1b[31m", "\x1b[33m", "\x1b[1m", "\x1b[0m"
DIM = "\x1b[2m"   # an aside: how to open a link, how to quit
# Every row on screen starts this far in, clear of the terminal's left edge.
MARGIN = " "
PURPLE = "\x1b[38;5;135m"  # #af5fff: link titles, opposite the green on the colour wheel
MAGENTA = "\x1b[35m"        # the same where only the basic colours are: rounded, 135 comes out blue

_lock = threading.RLock()
# What the run log says as a Ctrl-C arrives; the fault suites look for it.
CTRL_C_LOGGED = "Ctrl-C pressed"
# What the run did, beside its log, one JSON object a line - {"at": seconds since
# the epoch, "event": ..., and the event's own fields} - for a program (the fault
# suites) to follow it by, not by its words, which change:
#   run      a run began (version); ended: it ended
#   began    a step began (step); done: it ended done; again: a later step found it
#            had not taken, and it is done again (step, restarted: the device first)
#   phase    a point within a step or readout that has no step of its own (phase:
#            PHASES)
#   resent   the upgrade command sent again, the device not having asked for its
#            firmware (attempt; tools/ewelink/ota_push.py)
#   join     the person was asked to join a device's network (network); joined:
#            this computer has; home: asked to go back to their own Wi-Fi
#   switch   the person was asked to switch the device off and on: now, while the
#            run waits (now: true, asking_to_switch_now; key: true where it waits
#            for a key once it is done), or once it has stopped (any other line it
#            logs that SWITCH_OFF_AND_ON matches)
EVENTS = "events.jsonl"
PHASES = frozenset({"backup", "settings-reset", "template", "own-layout", "read", "read-done", "back",
                    "back-done", "serving"})
# Advice to switch the device off and on, however worded: each message saying it
# matches, though wrapped over two lines (tests/autoflash/test_output.py holds
# them to it).
SWITCH_OFF_AND_ON = re.compile(r"switch (the device|it) off( and| for [^.]*?then) on", re.IGNORECASE)

# Colour and hyperlink sequences, which take no room on screen.
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\]8;;[^\x1b]*\x1b\\")

# Words that mark a helper tool's line as the reason it failed.
PROBLEM_RE = re.compile(r"error|refus|fail|warning|interrupted|did not|denied|invalid|unable|timed? ?out",
                        re.IGNORECASE)


class _State:
    log: IO[str] | None = None
    log_path: Path | None = None
    concise = False
    stage: str | None = None          # "Stage 5 - Installing Tasmota", as logged
    stage_prefix = ""                 # "Stage  5", coloured with the outcome
    stage_screen = ""                 # the same, number and name padded into columns
    name_width = 0                    # the longest stage name, so the columns line up
    most_stages = 0                   # the most the run may have, its numbers' column as wide
    rows = 0                          # rows finished on screen, less those taken off again (mark)
    status = "Underway"               # what the stage line says while it runs: "Retrying" for a step done again
    underway_row: int | None = None   # where the stage's line was left on the screen, above what it showed
    waited = False                    # the stage's clock drawn under its last note since: a new note stands apart
    next_retrying = False             # the next stage begun is a step done again (retrying)
    stage_mark: Mark | None = None    # the screen as the stage began (mark)
    blocks: list[tuple[int, bool]] = []  # the open blocks (transient_begin), innermost last:
                                      # `rows` as each began, and whether a blank row was last
    pending_prompt: str | None = None  # a question on screen, waiting for its answer
    pending_answer = ""
    stage_started = 0.0
    waiting_since: float | None = None  # when the keyboard began to be waited for
    stage_waited = 0.0                # seconds of it in this stage, left out of its clock
    run_waited = 0.0                  # and in the whole run, left out of its time
    live = 0                          # characters of an unfinished line on screen
    live_is_stage = False
    stream_fresh = True               # a helper's output is at the start of a row
    after_stage = False               # a stage line was the last on screen: a gap before what follows
    after_blank = False               # a blank line was the last on screen
    draws_notice = False              # a first Ctrl-C's notice is drawn under the stage line
    notice_rows = 0                   # rows of it on screen now
    working: str | None = None        # a wait outside any stage, its spinner drawn (working())
    working_started = 0.0
    paused = False                    # waiting for the keyboard: draw nothing
    held = False                      # between questions that belong together: no clock
    ticker: threading.Thread | None = None
    keys_shown: list | None = None    # the terminal's settings while it echoes keys (_hide_keys)
    cursor_hidden = False
    tool_tail: collections.deque = collections.deque(maxlen=40)  # the last helper's last lines
    stop_ticker = threading.Event()
    switching_now: dict | None = None  # what is logged asks for it now: the event's fields (asking_to_switch_now)
    last_logged = ""                  # the last line logged, whitespace collapsed (_note_switch_advice)


_s = _State()


# ------------------------------------------------------------------ privacy
# The log is meant to be shared when asking for help, so what identifies the
# person, their network or their device is replaced before it is written: the
# values the run learns (private()), and anything shaped like one of them
# (_PATTERNS) - a helper's output can carry them before the run knows them.
# The screen is left alone: it is theirs.  Addresses on the home network stay,
# as they are needed to follow a run and identify nobody.

_private: dict[str, str] = {}         # value -> the label it is replaced with

_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Secrets in JSON, whatever else is in the line.
    (re.compile(r'("(?:apikey|devicekey|at|rt|token|password|pass|pwd)"\s*:\s*)"[^"]*"', re.I),
     r'\1"<redacted>"'),
    (re.compile(r"\bsign=[0-9a-fA-F]+"), "sign=<signature>"),
    (re.compile(r"[\w.%+-]+@[\w-]+(?:\.[\w-]+)+"), "<email>"),
    # A phone number with its country code, as an eWeLink account made with
    # one is signed in with (and eWeLink's answer repeats).
    (re.compile(r"(?<![\w+])\+[1-9]\d{6,14}\b"), "<phone>"),
    (re.compile(r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b"), "<mac>"),
    # An eWeLink device id; and a converted device's name, which ends with the
    # MAC's (tasmota-abcdef, esphome-abcdef, openbeken-abcdef, ESPHome's
    # friendly name as its page shows it: esphome abcdef), and Tasmota's
    # default one; and ESPHome's friendly name as earlier builds gave it
    # (Sonoff S60 abcdef).
    (re.compile(r"\b100[0-9a-f]{7}\b"), "<device-id>"),
    (re.compile(r"\btasmota-[0-9A-Fa-f]{6}-\d{4}\b"), "<device-name>"),
    (re.compile(r"\b(?:tasmota|esphome|openbeken)[- ][0-9a-f]{6}\b", re.I), "<device-name>"),
    (re.compile(r"\bSonoff[- ]S\d+[- ][0-9a-f]{6}\b", re.I), "<device-name>"),
    (re.compile(r"(\[sudo\] password for )\S+?(:)"), r"\1<user>\2"),
]
_IPV4 = re.compile(r"(?<![\w.-])\d{1,3}(?:\.\d{1,3}){3}(?![\w.(-])")


def private(value: object, label: str) -> None:
    """Keep `value` out of the log from now on, written as <label> instead."""
    text = str(value or "").strip()
    if len(text) < 3:  # too short to replace without mangling ordinary words
        return
    with _lock:
        _private[text] = label


def scrub(text: str) -> str:
    """`text` as the log may hold it: see private()."""
    for value in sorted(_private, key=len, reverse=True):
        text = text.replace(value, f"<{_private[value]}>")
    for pattern, replacement in _PATTERNS:
        text = pattern.sub(replacement, text)
    text = _IPV4.sub(_public_ip, text)
    home = str(Path.home())
    if len(home) > 1:
        text = text.replace(home, "~")
    return text


def _public_ip(match: re.Match[str]) -> str:
    try:
        address = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    return "<public-ip>" if address.is_global else match.group(0)


# --------------------------------------------------------------------- setup


def start(log_path: Path | None, *, verbose: bool) -> None:
    """Begin a run: open its log, and condense the terminal unless verbose.

    Nothing of an earlier run in the same process carries over: a question
    it left waiting (stopped at it), or its ticker, if it never finished.
    Either would draw on this run's screen as if it were that one's - a
    rewind erasing rows above it for a question long gone (seen in the
    tests, a stage line erased), the spinner drawn on a screen that is no
    terminal."""
    _stop_ticker()
    with _lock:
        _s.pending_prompt, _s.pending_answer = None, ""
        _s.live, _s.live_is_stage, _s.notice_rows, _s.working = 0, False, 0, None
        if log_path is not None:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            _s.log = log_path.open("a", encoding="utf-8")
            _s.log_path = log_path
            _s.last_logged = ""
            _write_log(f"=== run started, tool version {__version__}{' (verbose)' if verbose else ''} ===")
            event("run", version=__version__)
        _s.concise = not verbose
        _s.paused = False
        _s.rows, _s.blocks, _s.stage_mark = 0, [], None
        _s.status, _s.next_retrying = "Underway", False
        _s.name_width = _s.most_stages = 0   # the run's own route sets them (set_stage_width)
        _s.run_waited = 0.0
        _s.waiting_since = None   # a question left unanswered is not this run's wait
        if _s.concise and sys.stdout.isatty() and _s.ticker is None:
            # Keeps the stage clock moving through waits that print nothing.
            _s.stop_ticker.clear()
            _s.ticker = threading.Thread(target=_tick_forever, daemon=True)
            _s.ticker.start()
        _s.draws_notice = _s.concise and sys.stdout.isatty() and colour_ok()
        interrupt.shown_by(_notice_text)
        interrupt.logged_by(_log_ctrl_c)
        if _s.concise:
            _hide_keys()


def finish() -> None:
    """End cleanly: no half-drawn line left behind, and the log closed."""
    interrupt.shown_by(None)
    interrupt.logged_by(None)
    _s.draws_notice = False
    _stop_ticker()
    # A run ended with a stage still under way - stopped by a signal no
    # handler ends it for, as SIGTERM's exit is - shows it stopped, its
    # question and notes taken off, as one stopped by Ctrl-C does.
    stage_end("stopped")
    with _lock:
        _break()
        _show_keys()
        if _s.log is not None:
            _write_log("=== run ended ===")
            event("ended")
            _s.log.close()
        _s.log = _s.log_path = None
        _s.concise = False
        _s.stage = _s.underway_row = None
        _s.waited = False
        _s.blocks = []
        _s.next_retrying = False
        _s.name_width = 0
        _s.after_stage = False
        _s.after_blank = False


def _stop_ticker() -> None:
    """The stage clock's thread ended, if one runs.  Not under the lock: a
    tick waits for it, and would hold the join up."""
    _s.stop_ticker.set()
    if _s.ticker is not None:
        _s.ticker.join(2.0)
        _s.ticker = None


def move_log(move: Callable[[], Path]) -> None:
    """Close the log while `move` moves its directory, then carry on in the
    log at the path it returns - Windows will not move a directory with a
    file open in it."""
    with _lock:
        if _s.log is not None:
            _s.log.close()
            _s.log = None
        path = move()
        if _s.log_path is not None:
            _s.log = path.open("a", encoding="utf-8")
            _s.log_path = path


def log_path() -> Path | None:
    return _s.log_path


def event(name: str, **fields: object) -> None:
    """One of EVENTS's events, beside the run's log; nothing outside a run."""
    with _lock:
        path = events_path()
        if path is not None:
            write_event(path, name, **fields)


def events_path() -> Path | None:
    """The run's events file (EVENTS): for a helper it runs to add its own."""
    return _s.log_path.parent / EVENTS if _s.log_path is not None else None


def write_event(path: Path, name: str, **fields: object) -> None:
    """One event (EVENTS) appended to the events file `path`: a whole line in one
    write, so a helper's and the run's never split each other."""
    line = json.dumps({"at": round(time.time(), 3), "event": name, **fields}) + "\n"
    try:
        with path.open("a", encoding="utf-8") as events:
            events.write(line)
    except OSError:
        pass  # the run goes on without it, as without a log line


def events(run_dir: Path) -> list[dict]:
    """The events (EVENTS) of the runs whose log is in `run_dir`, oldest first:
    none from a release before they were written, or a line cut off."""
    try:
        lines = (run_dir / EVENTS).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    found = []
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict) and isinstance(item.get("event"), str):
            found.append(item)
    return found


def concise() -> bool:
    return _s.concise


def terminal_size(fallback: tuple[int, int] = (80, 24)) -> os.terminal_size:
    """shutil.get_terminal_size(), the fallback taken for a side the terminal
    reports as 0 (a pseudo-terminal nobody sized): Python 3.11 does that
    itself, 3.10 hands back the 0 - and every row then wraps at 20 columns."""
    columns, lines = shutil.get_terminal_size(fallback)
    return os.terminal_size((columns or fallback[0], lines or fallback[1]))


def colour_ok() -> bool:
    """ANSI colour only on a real terminal that understands it."""
    if os.environ.get("NO_COLOR") or not sys.stdout.isatty():
        return False
    if IS_WINDOWS:
        # Windows Terminal and current consoles understand ANSI; the classic
        # console does only once virtual-terminal mode is on.
        return bool(os.environ.get("WT_SESSION")) or _enable_windows_vt()
    return True


def _enable_windows_vt() -> bool:
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except (AttributeError, OSError):
        return False


def links_ok() -> bool:
    """Whether the terminal takes OSC 8 links.

    The classic Windows console, macOS Terminal and the Linux console are
    left out: they show or drop the sequence rather than make a link.
    """
    if not colour_ok() or os.environ.get("TERM") in ("dumb", "linux"):
        return False
    if os.environ.get("TERM_PROGRAM") == "Apple_Terminal":
        return False
    return not IS_WINDOWS or bool(os.environ.get("WT_SESSION"))


def hyperlink(url: str, title: str | None = None, width: int = 0) -> str:
    """The URL as a link the terminal can open on a click (OSC 8).

    The URL itself is always shown, after the title if there is one, so a
    terminal that does not take the sequence still has it to pick out or
    copy.  Only the URL is the link: the title stays outside it, coloured
    wherever colour is, as some terminals repaint link text in their own
    colour; a URL with no title is coloured itself, so it looks a link as the
    rest do.  `width`: the title padded to it, so the URLs of a list line up.
    """
    link = f"\x1b]8;;{url}\x1b\\{url}\x1b]8;;\x1b\\" if links_ok() else url
    return (f"{styled(title + ':', BOLD, _purple())}{' ' * (width - len(title))} {link}" if title
            else styled(link, BOLD, _purple()))


def _purple() -> str:
    """The link titles' purple, or the terminal's magenta where it has only
    the basic colours (tmux or screen named plainly, the Linux console)."""
    if IS_WINDOWS or os.environ.get("COLORTERM") in ("truecolor", "24bit"):
        return PURPLE
    return PURPLE if "256" in os.environ.get("TERM", "") else MAGENTA


def click_hint() -> str:
    """How to open a link here, or nothing where clicking is not known to work."""
    if not links_ok():
        return ""
    return "Cmd+click to open" if platform.system() == "Darwin" else "Ctrl+click to open"


def show_link(url: str, title: str | None = None, indent: str = "    ") -> None:
    """A link on a line of its own: clickable on screen, plain in the log."""
    show_links([(url, title)], indent)


def show_links(links: list[tuple[str, str | None]], indent: str = "    ", *, hint: bool = False) -> None:
    """Links one to a line, their URLs lined up, and with hint=True how to
    open them underneath."""
    width = max((len(title) for _, title in links if title), default=0)
    with _lock:
        for url, title in links:
            _write_log(indent + (f"{title}:{' ' * (width - len(title))} {url}" if title else url))
            _print(indent + hyperlink(url, title, width))
        how = click_hint() if hint else ""
        if how:
            _print(indent + styled(f"({how})", DIM))


def styled(text: str, *codes: str) -> str:
    return "".join(codes) + text + RESET if codes and colour_ok() else text


# ------------------------------------------------------------------ messages


def say(message: str = "", *, show: bool = False) -> None:
    """A message for the run's log; on screen too in verbose mode, or with show."""
    (on_screen if show else detail)(message)


def tell(message: str = "") -> None:
    """A message the person running the tool needs to see, in every mode."""
    say(message, show=True)


def detail(text: str = "") -> None:
    """Logged always; on screen only in verbose mode, or outside a run."""
    with _lock:
        _write_log(text)
        if not _s.concise:
            _print(text)


def show(text: str = "", *codes: str) -> None:
    """Logged and always on screen: things to read, do or answer."""
    with _lock:
        _write_log(text)
        _print(styled(text, *codes))


on_screen = show  # for say(), whose `show` is its flag


def log_only(text: str) -> None:
    with _lock:
        _write_log(text)


@contextlib.contextmanager
def asking_to_switch_now(*, key: bool = False):
    """What is said within asks the person to switch the device off and on while
    the run waits on - for a key once it is done, with `key`: its "switch"
    event says "now" (and "key")."""
    with _lock:
        _s.switching_now = {"now": True, **({"key": True} if key else {})}
        try:
            yield
        finally:
            _s.switching_now = None


def before_input(prompt: str = "") -> None:
    """Called just before reading the keyboard: the question must start on its own line."""
    with _lock:
        _break()
        _settle_stage_line()
        _show_keys()
        _gap("?")
        # A question stands apart from the options or text above it.
        if not _s.after_blank:
            print("", flush=True)
            _s.after_blank = True
            _counted(1)
        _s.paused = True
        if _s.waiting_since is None:
            _s.waiting_since = time.monotonic()
        _s.pending_prompt, _s.pending_answer = prompt, ""
        if prompt:
            _write_log(f"? {prompt.strip()}")


def question_row(text: str, tail: str = "") -> str:
    """The row question() would end on for `text`, for redrawing it in place
    (a countdown's), with nothing else done: question() itself readies the
    terminal for an answer, which would undo a single-key read's mode."""
    with _lock:
        body = text + tail
        rows = _fit(body.rstrip(" ")).split("\n")
        return rows[-1] + body[len(body.rstrip(" ")):]


def question(text: str, tail: str = "", hint: str = "") -> str:
    """before_input(), with the question laid out for the screen: every row
    but its last written here, the last returned for input() to show.
    `tail` - ": ", say - follows the question on screen but not in the log.
    `hint` - how to quit, say - goes on a row of its own under it, dimmed."""
    with _lock:
        before_input(f"{text} {hint}" if hint else text)
        body = text + (f"\n{styled(hint, DIM)}" if hint else "") + tail
        rows = _fit(body.rstrip(" ")).split("\n")
        rows[-1] += body[len(body.rstrip(" ")):]
        for row in rows[:-1]:
            sys.stdout.write(row + "\n")
        sys.stdout.flush()
        _s.pending_prompt = "\n".join(rows)
        return rows[-1]


def after_input() -> None:
    """The keyboard has been read: the stage line may be drawn again."""
    with _lock:
        if _s.pending_prompt is not None:
            # The question and the answer echoed after it, then Enter.
            _counted(_rows(_s.pending_prompt + _s.pending_answer))
        _s.pending_prompt, _s.pending_answer = None, ""
        _s.after_blank = False  # the answer's line
        _s.paused = False
        if _s.concise:
            _hide_keys()
        if _s.waiting_since is not None:
            waited = time.monotonic() - _s.waiting_since
            _s.stage_waited += waited
            _s.run_waited += waited
            _s.waiting_since = None


def after_secret(shown: str) -> None:
    """after_input() for a password: `shown` is what the screen holds after
    the prompt - asterisks only, never the password."""
    _s.pending_answer = shown
    after_input()


def answered(answer: str) -> None:
    """Record a non-secret answer in the log."""
    _s.pending_answer = answer
    after_input()
    log_only(f"  -> {answer}")


# ------------------------------------------------------- answered questions


def transient_begin(*, before_start: bool = False) -> None:
    """Start a block - a note and its question - to take off the screen once
    answered, so a finished stage leaves just its line.  Only on a terminal
    that can move the cursor back up; the log keeps everything regardless.

    `before_start`: a question asked before start(), which has no log yet and
    does not know whether the run will be verbose.

    A block begun inside another is taken off on its own, and the outer
    one, with whatever is left of it, when that ends.
    """
    with _lock:
        if not _drawn_over(before_start=before_start):
            return
        began = _s.rows
        _break()  # the stage line, if drawn, is taken away and drawn again too
        _s.blocks.append((began, _s.after_blank))


def transient_keep() -> None:
    """Close the innermost block, leaving it on the screen: what it showed is
    now why the stage stopped (until_done)."""
    with _lock:
        if _s.blocks:
            _s.blocks.pop()


@contextlib.contextmanager
def until_done():
    """A block - instructions, and what is said while they are followed - shown
    while something is waited for, and taken off the screen once it is done:
    joining a network, say.  Left on the screen if the wait stops the stage
    instead, as it then says why."""
    transient_begin()
    try:
        yield
    except BaseException:
        transient_keep()
        raise
    transient_end()


def transient_end() -> None:
    """Take the innermost block since transient_begin() off the screen."""
    with _lock:
        if not _s.blocks:
            return
        began, blank = _s.blocks.pop()
        # Taller than the window (VS Code's terminal panel, say), it stays whole.
        if _erase(_s.rows - began):
            _s.after_blank = blank  # the screen as the block found it
        if _s.stage is not None and not _s.paused:
            _draw_stage()


# Rows a window needs for the run's questions, and what a stage shows on the
# way, to be taken off the screen once done with: more than the window holds
# has to stay (transient_end, stage_end), and the stages with most to say
# show up to about 20.  A shorter one is waited for to grow: 31, so that a
# window opened at 30 rows (Windows Terminal's) is, as one at 24 (macOS's) is.
MIN_ROWS = 31
# Rows for the report at the end, up to about 38, to be seen whole as well: a
# shorter window is only told so.  Most screens have them with the window
# maximised; a window opened afresh is often 24 or 30.
GOOD_ROWS = 40


def start_at_top() -> None:
    """The run starts at the top of the window, with all of it to use: what
    the window showed is scrolled up into its scrollback, one row a newline,
    not cleared, so it can still be scrolled back to.  A blank row is left
    at the top, as above everything else the run shows.  Only on a terminal
    that can move the cursor back up.

    Not in VS Code's terminal: once the command that started the run is out
    of sight it pins that command over the window's top rows (its sticky
    scroll), covering the run's first.  There the run starts under it, after
    the blank row alone."""
    if not (sys.stdout.isatty() and colour_ok()):
        return
    vscode = os.environ.get("TERM_PROGRAM") == "vscode"
    with _lock:
        sys.stdout.write("\n" if vscode else "\n" * terminal_size((100, 24)).lines + "\x1b[H\n")
        sys.stdout.flush()
        _s.after_blank = True


def window_rows(*, before_start: bool = False) -> int | None:
    """The window's height in rows, where things are taken off the screen;
    None where nothing is.  `before_start`: as for transient_begin()."""
    return terminal_size((100, 24)).lines if _drawn_over(before_start=before_start) else None


def short_window(*, before_start: bool = False) -> int | None:
    """The window's height in rows, when answered questions are being taken
    off the screen but the window is too short for that to always work.
    `before_start`: as for transient_begin()."""
    if not _drawn_over(before_start=before_start):
        return None
    rows = terminal_size((100, 24)).lines
    return rows if rows < MIN_ROWS else None


class Mark(NamedTuple):
    """Where the screen had got to (mark): the rows finished on it, whether
    the stage's line or a blank row was last, and how many blocks were open."""
    rows: int
    after_stage: bool
    after_blank: bool
    blocks: int


def mark() -> Mark | None:
    """Where the screen has got to, for rewind() to go back to: before a
    question that may be asked again, or a try of a step that may be tried
    again.  None where nothing can be taken off the screen - outside a block
    (transient_begin), only a concise run's own screen can be."""
    with _lock:
        if not (_s.blocks or _drawn_over()):
            return None
        _break()
        return Mark(_s.rows, _s.after_stage, _s.after_blank, len(_s.blocks))


def rewind(to: Mark | None) -> bool:
    """Take everything shown since mark() off the screen - a wrong answer and
    its question, or a try that failed - so that what comes again does not
    pile up under it until it is taller than the window and cannot be taken
    away at all.  A block begun since goes with it; one open at the mark
    stays, though it began on the same row (a question asked as its block
    began: seen, a yes/no question answered wrongly first was left on the
    screen once answered).  True when it was; the log keeps it regardless."""
    with _lock:
        if to is None:
            return False
        if not _erase(_s.rows - to.rows):
            return False
        _s.blocks = _s.blocks[:to.blocks]
        _s.after_stage, _s.after_blank = to.after_stage, to.after_blank
        return True


def _drawn_over(*, before_start: bool = False) -> bool:
    """Whether rows can be taken off the screen again: on a terminal that can
    move the cursor back up, showing a concise run (or, `before_start`, a
    question before one)."""
    return (_s.concise or before_start) and sys.stdout.isatty() and colour_ok()


def _erase(rows: int) -> bool:
    """Take the last `rows` finished rows off the screen, the cursor left at
    the start of the first.  Not when there are as many as the window has: the
    cursor cannot go up past its top, so erasing would leave their start, and
    the stage line above it, stranded."""
    if rows >= terminal_size((100, 24)).lines:
        return False
    if _s.pending_prompt is not None:
        # A question still waiting for its answer - the run stopped at it, by
        # a signal that never let it be answered: its rows, the one the cursor
        # is on and any it took above, go first, or the rows above it would be
        # erased from under it and it left below their blank rows.
        above = _rows(_s.pending_prompt + _s.pending_answer) - 1
        sys.stdout.write("\r\x1b[2K" + "\x1b[1F\x1b[2K" * above)
        _s.pending_prompt, _s.pending_answer = None, ""
    if _s.live:
        sys.stdout.write(_clear_notice() + "\r\x1b[K")  # a stage line drawn since, and its notice
        _s.live = 0
    elif _s.notice_rows:
        sys.stdout.write(_clear_notice())   # a Ctrl-C notice under the notes
    # Up a row and erase it, row by row.  Not one erase-to-end from the top:
    # when the rows start on the screen's first that clears the whole screen,
    # which tmux (scroll-on-clear) and some terminals first copy into the
    # scrollback - the rows would stay there.
    sys.stdout.write("\x1b[1F\x1b[2K" * rows)
    sys.stdout.flush()
    _s.rows -= rows
    if _s.underway_row is not None and _s.rows <= _s.underway_row:
        # The stage's line left above what it showed is gone with it: drawn
        # live again (_draw_stage), with nothing under it for a note to stand
        # apart from - else what comes after the stage's outcome gets two blank
        # rows, its own and _gap's.
        _s.underway_row, _s.waited = None, False
    return True


# ------------------------------------------------------------------- stages


@contextlib.contextmanager
def hold():
    """No stage clock drawn meanwhile: a question, the wait while its answer is
    checked, and the question again.  The clock would land between them, on a
    row the block does not count."""
    with _lock:
        before, _s.held = _s.held, True
        _settle_stage_line()
    try:
        yield
    finally:
        with _lock:
            _s.held = before


@contextlib.contextmanager
def working(text: str):
    """A wait outside any stage - the report being put together after the
    last one, a readout's - shown as `text` and the stages' spinner on a row
    of its own, below a blank one, and taken away once it is over.  On a
    terminal in concise mode only: elsewhere, or within a stage, whose own
    spinner turns, `text` is said as a line, as any other."""
    with _lock:
        shown = _s.concise and _s.ticker is not None and _s.stage is None
    if not shown:
        say(text)
    with _lock:
        if shown:
            _break()
            if not _s.after_blank:
                _print("")
            _s.working, _s.working_started = text, time.monotonic()
            _draw_working()
    try:
        yield
    finally:
        with _lock:
            if shown:
                _s.working = None
                if _s.live and not _s.live_is_stage:
                    sys.stdout.write("\r" + " " * _s.live + "\r")
                    sys.stdout.flush()
                    _s.live = 0


def set_stage_width(width: int, most: int = 0) -> None:
    """The longest stage name in the run, and the most stages it may have, so
    every stage line's name and status line up."""
    _s.name_width, _s.most_stages = width, most


def stage_width() -> int:
    """The stage names' column, as set_stage_width() left it."""
    return _s.name_width


def most_stages() -> int:
    """The most stages the run may have, as set_stage_width() left it."""
    return _s.most_stages


def stage_begin(number: int, name: str) -> None:
    """No total: a run from Sonoff's firmware learns its route only part way
    through, and every stage line says the same either way."""
    with _lock:
        label = f"Stage {number} - {name}"
        if _s.stage == label:
            return  # already begun: keep its line and its clock
        _break()
        _s.stage_mark = Mark(_s.rows, _s.after_stage, _s.after_blank, len(_s.blocks)) if _drawn_over() else None
        _s.status, _s.next_retrying = ("Retrying" if _s.next_retrying else "Underway"), False
        _s.underway_row, _s.waited = None, False
        digits = len(str(max(_s.most_stages, number)))
        _s.stage = label
        _s.stage_prefix = f"Stage {number:>{digits}}"
        _s.stage_screen = f"{_s.stage_prefix} - {name:<{_s.name_width}}"
        _s.stage_started = time.monotonic()
        _s.stage_waited = 0.0
        _write_log(f"--- {_s.stage}")
        if _s.concise:
            if sys.stdout.isatty():
                _draw_stage()
            else:
                _print(f"{_s.stage_screen} - {_s.status}", stage=True)
        else:
            _print("")
            _print(f"== {_s.stage}", stage=True)


# Outcomes of a stage that passed: all it showed is taken off the screen.
PASSED = ("complete", "already done", "skipped")


def retrying() -> None:
    """The next stage begun is a step done again - tried again after it
    failed, or gone back to (GoBack): its line says "Retrying" while it runs,
    not "Underway", so the person sees why it is there again."""
    with _lock:
        _s.next_retrying = True


def stage_end(outcome: str) -> None:
    """outcome: "complete", "stopped", "already done" or "skipped" (a stage
    the run does not need, passed over).  Any block - a note and its
    question - still open is taken off first: one cut short by Ctrl-C or q
    would otherwise stay, the stage's underway line with it above its last.

    A stage that passed leaves just its line: what it showed while under
    way - notes, instructions, a part that went again - is taken off the
    screen; the log, and the report, keep what matters of it.  One that
    stopped leaves everything: that is why it stopped."""
    with _lock:
        if _s.stage is None:
            return
        while _s.blocks:
            transient_end()
        _settle_stage_line()   # a stage that stopped leaves it, above why
        if outcome in PASSED and _s.stage_mark is not None and _s.rows > _s.stage_mark.rows:
            # Taller than the window, it has to stay.
            if _erase(_s.rows - _s.stage_mark.rows):
                _s.after_stage, _s.after_blank = _s.stage_mark.after_stage, _s.stage_mark.after_blank
        elapsed = _elapsed(_stage_seconds())
        timing = f" ({elapsed})" if outcome == "complete" else ""
        line = f"{_s.stage} - {outcome}{timing}"   # its time in the log and the report, not on screen
        _write_log(line)
        if _s.concise:
            colour = {"complete": GREEN, "already done": GREEN, "skipped": GREEN,
                      "stopped": RED}.get(outcome)
            word = outcome.capitalize()
            plain = f"{_s.stage_screen} - {word}"
            text = plain if not colour else (
                styled(_s.stage_prefix, colour) + _s.stage_screen[len(_s.stage_prefix):]
                + " - " + styled(word, colour))
            if _s.live and _s.live_is_stage:
                # Replace the "underway" line in place.
                first, _, rest = _fit(text).partition("\n")
                pad = " " * max(0, _s.live - len(ANSI_RE.sub("", first)))
                sys.stdout.write(_clear_notice() + "\r" + first + pad + ("\n" + rest if rest else "") + "\n")
                sys.stdout.flush()
                _counted(_rows(first + ("\n" + rest if rest else "")))
                _s.live = 0
                _s.after_stage = True
                _s.after_blank = False
            else:
                _print(text, stage=True)
        _s.stage, _s.stage_mark, _s.underway_row = None, None, None


def progress(text: str = "") -> None:
    """What a stage is doing right now, redrawn in place; never logged.

    Verbose mode only: in concise mode the stage line and its clock already
    say the stage is under way, and anything to act on is shown in full.
    """
    with _lock:
        if _s.concise:
            return
        if not sys.stdout.isatty():
            return
        if _s.live and _s.live_is_stage:
            _break()
        _gap(text)
        line = _clip(f"{MARGIN}    {text}")
        sys.stdout.write("\r" + line + " " * max(0, _s.live - len(line)))
        sys.stdout.flush()
        _s.live, _s.live_is_stage = len(line), False


def progress_clear() -> None:
    """The waiting is over: take the progress text away."""
    with _lock:
        if _s.concise:
            return
        if _s.live and not _s.live_is_stage:
            sys.stdout.write("\r" + " " * _s.live + "\r")
            sys.stdout.flush()
            _s.live = 0


def progress_keep() -> None:
    """Leave the last progress line on screen (verbose) and move on."""
    with _lock:
        if not _s.concise and _s.live and not _s.live_is_stage:
            sys.stdout.write("\n")
            sys.stdout.flush()
            _s.live = 0


def tick() -> None:
    """Redraw the stage line so its spinner turns during a long wait."""
    with _lock:
        if _s.concise and _s.stage is not None and not _s.paused and not _s.held and sys.stdout.isatty():
            _draw_stage()


def raw_progress(text: str) -> None:
    """For writers that draw their own progress with \\r and finish with \\n."""
    if text == "\n":
        progress_keep()
    elif text.startswith("\r"):
        progress(text.strip())




# ------------------------------------------------------------- subprocesses


def stream(process: subprocess.Popen) -> int:
    """Copy a child's output to the log (and the screen when verbose) until it exits."""
    assert process.stdout is not None
    source = process.stdout
    _s.tool_tail.clear()

    def pump() -> None:
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        pending = ""
        while True:
            chunk = os.read(source.fileno(), 4096)
            if not chunk:
                break
            text = decoder.decode(chunk)
            with _lock:
                if not _s.concise:
                    if _s.live:
                        _break()
                    _gap(text)
                    sys.stdout.write(_margin_stream(text))
                    sys.stdout.flush()
                    _s.after_blank = text.endswith("\n\n")
                pending += text
                while "\n" in pending:
                    line, pending = pending.split("\n", 1)
                    # A child redrawing a progress line: keep only what it ended on.
                    _keep(_last_redraw(line))
        if pending.strip():
            with _lock:
                _keep(_last_redraw(pending))

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    try:
        while process.poll() is None:
            tick()
            time.sleep(1.0)
    except BaseException:
        # The run stopping (Ctrl-C twice, the window closed): the child, in a
        # process group of its own, is not told by the terminal, so is told here.
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
        raise
    finally:
        reader.join(5.0)
    return process.wait()


def tool_said(text: str) -> bool:
    """Whether the last helper tool's output (its kept tail) mentions `text`."""
    return any(text in line for line in _s.tool_tail)


def tool_problems(limit: int = 3) -> list[str]:
    """The last helper tool's own words on what went wrong: its lines that read
    like an error, or failing that its last few lines."""
    lines = [line for line in _s.tool_tail if line.strip()]
    problems = [line for line in lines if PROBLEM_RE.search(line)]
    return [line.strip() for line in (problems or lines)[-limit:]]


def _last_redraw(line: str) -> str:
    """What a line redrawn with "\\r" ended on.  Its own line ending comes off
    first: "\\r\\n" on Windows, which would otherwise leave nothing."""
    if line.endswith("\r"):
        line = line[:-1]
    return line.split("\r")[-1]


def _keep(line: str) -> None:
    _write_log("  | " + line)
    _s.tool_tail.append(line)


# ------------------------------------------------------------------ helpers


def elapsed(seconds: float) -> str:
    """48s, or 2m 05s."""
    return _elapsed(seconds)


def _elapsed(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s" if seconds >= 60 else f"{seconds}s"


def _waiting_now() -> float:
    """Seconds the keyboard has been waited for, if it is being waited for now."""
    return time.monotonic() - _s.waiting_since if _s.waiting_since is not None else 0.0


def _stage_seconds() -> float:
    """How long the stage has been working: its time less the time spent
    waiting for answers, so a stage's clock is the tool's time, not the
    person's."""
    return time.monotonic() - _s.stage_started - _s.stage_waited - _waiting_now()


def waited() -> float:
    """Seconds this run has spent waiting for answers, to leave out of its time."""
    with _lock:
        return _s.run_waited + _waiting_now()


def _tick_forever() -> None:
    while not _s.stop_ticker.wait(SPIN_EVERY):
        _tick()


def _notice_drawn() -> bool:
    """Whether the ticker draws a Ctrl-C notice now: read from the signal
    handler, so without the lock the interrupted code may hold."""
    return (_s.concise and _s.ticker is not None and _s.stage is not None and not _s.paused
            and not _s.held and not _s.blocks)


def _notice_rows() -> list[str]:
    """A first Ctrl-C's notice as rows for the screen, laid out as the other
    notes to read before going on (prompts.warning_box): a blank row, the
    title highlighted, the text set in under it."""
    rows = interrupt.notice()
    if not rows:
        return []
    title, *rest = rows
    return ["", styled(_clip(f"{MARGIN}{title}"), BOLD, YELLOW), *(_clip(f"{MARGIN}    {row}") for row in rest)]


def _notice_text() -> str | None:
    """What a first Ctrl-C writes out where the ticker does not draw it: an
    unfinished line ended first.  Its rows are counted as any others, so a
    block it lands in takes it off with the rest (seen: written uncounted
    before the first stage, it stayed for the whole run).  Called from the
    signal handler, so it takes no lock: it only adds to the count."""
    if _notice_drawn():
        return None
    rows = _notice_rows()
    if not rows:
        return None
    start = ""
    if _s.live:
        # As _break(): a stage line stays, a wait's goes and is drawn again
        # under the notice.
        start = "\n" if _s.live_is_stage else "\r" + " " * _s.live + "\r"
        _counted(1 if _s.live_is_stage else 0)
        _s.live = 0
    _counted(len(rows))
    _s.after_blank = False
    return start + "".join(f"{row}\n" for row in rows)


def _tick() -> None:
    """The stage's spinner, a turn on.  Not while a block is open to be taken
    away again: drawn under its rows - while what it shows is still being
    worked out, say (Windows' security software takes PowerShell a few
    seconds) - the spinner's line would be a second stage line in the block."""
    with _lock:
        if (_s.concise and _s.stage is not None and not _s.paused and not _s.held
                and not _s.blocks):
            _draw_stage()
        elif _s.working and not _s.paused and not _s.held and not _s.blocks:
            _draw_working()


# The stage line's spinner, a turn every SPIN_EVERY seconds: braille where the
# terminal's font has it, else plain characters (spinner_frames).
BRAILLE_FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
PLAIN_FRAMES = "|/-\\"
SPIN_EVERY = 0.1


def spinner_frames() -> str:
    """Braille, unless the terminal cannot show it: an encoding without it,
    the Linux console's font, or Windows' classic console (Windows Terminal
    has it)."""
    try:
        BRAILLE_FRAMES.encode(sys.stdout.encoding or "ascii")
    except (UnicodeEncodeError, LookupError):
        return PLAIN_FRAMES
    if os.environ.get("TERM") == "linux" or (IS_WINDOWS and not os.environ.get("WT_SESSION")):
        return PLAIN_FRAMES
    return BRAILLE_FRAMES


def _stage_line(spinner: str = "") -> tuple[str, str]:
    """The stage's underway line, plain and as shown: with `spinner`, and a
    space after it so the terminal's cursor sits clear of it."""
    tail = f" {spinner} " if spinner else ""
    line = f"{MARGIN}{_s.stage_screen} - {_s.status}{tail}"
    shown = f"{MARGIN}{_s.stage_screen} - {styled(_s.status, BOLD)}{tail}"
    if _clip(line) != line:
        line = shown = _clip(line)
    return line, shown


def _rows_up_to_stage_line() -> int | None:
    """How many rows up from the cursor the stage under way's line is, left
    above what it shows (_break), when it can be redrawn there: nothing drawn at
    the cursor, a terminal that moves the cursor, and the line still in the
    window - with room for a Ctrl-C notice under the cursor, which may scroll
    it - as the cursor cannot go up into the scrollback."""
    if _s.stage is None or _s.underway_row is None or _s.live or not (sys.stdout.isatty() and colour_ok()):
        return None
    up = _s.rows - _s.underway_row
    if up < 1 or up + len(interrupt.notice()) >= terminal_size((100, 24)).lines:
        return None
    return up


def _settle_stage_line() -> None:
    """The stage's line above what it shows, without its spinner: nothing
    turns while a question waits, or once the stage is over."""
    up = _rows_up_to_stage_line()
    if up is not None:
        sys.stdout.write(_clear_notice() + f"\x1b[{up}A\r{_stage_line()[1]}\x1b[K\x1b[{up}B\r")
        sys.stdout.flush()


def _draw_stage() -> None:
    frames = spinner_frames()
    spinner = frames[int(_stage_seconds() / SPIN_EVERY) % len(frames)]
    line, shown = _stage_line(spinner)
    if _s.live and not _s.live_is_stage:
        _break()
    up = _rows_up_to_stage_line()
    if up is not None:
        # One line a stage: the one left above what it shows turns, in place,
        # the cursor back under the last note after.  A Ctrl-C notice goes
        # under the notes, the cursor's empty row the gap above it.
        rows = _notice_rows()[1:] if _s.draws_notice else []
        notice = "".join(f"\n{row}\x1b[K" for row in rows) + (f"\x1b[{len(rows)}A\r" if rows else "")
        clear = _clear_notice() if len(rows) < _s.notice_rows else ""
        sys.stdout.write(clear + f"\x1b[{up}A\r{shown}\x1b[K\x1b[{up}B\r" + notice)
        sys.stdout.flush()
        _s.notice_rows = len(rows)
        _s.waited = True   # time has passed: a note said next stands apart (_print)
        return
    # Below it, a first Ctrl-C's notice while it lasts (interrupt.notice),
    # the cursor back at the end of the stage line after it.
    # Each row written over the last draw's, not erased first, so it does
    # not flicker; the rows go once it has fewer.
    rows = _notice_rows() if _s.draws_notice else []
    notice = "".join(f"\n{row}\x1b[K" for row in rows)
    if rows:
        notice += f"\x1b[{len(rows)}A\r" + shown
    clear = _clear_notice() if len(rows) < _s.notice_rows else ""
    sys.stdout.write(clear + "\r" + shown + " " * max(0, _s.live - len(line)) + notice)
    sys.stdout.flush()
    _s.live, _s.live_is_stage = len(line), True
    _s.notice_rows = len(rows)


def _draw_working() -> None:
    frames = spinner_frames()
    spinner = frames[int((time.monotonic() - _s.working_started) / SPIN_EVERY) % len(frames)]
    line = _clip(f"{MARGIN}{_s.working} {spinner} ")
    sys.stdout.write("\r" + line + " " * max(0, _s.live - len(line)))
    sys.stdout.flush()
    _s.live, _s.live_is_stage = len(line), False


def _clear_notice() -> str:
    """What takes the Ctrl-C notice off the rows under the stage line, row
    by row: an erase to the end of the screen could take more."""
    rows, _s.notice_rows = _s.notice_rows, 0
    return "\x1b[1E\x1b[2K" * rows + f"\x1b[{rows}F" if rows else ""


def _hide_keys() -> None:
    """No cursor, and no echo of keys pressed, while nothing asks: an Enter
    would move the stage line down a row, and the spinner left on the row
    above would stay there.  What is typed is dropped before the next
    question anyway (prompts.discard_typed_ahead).  The echo on POSIX
    terminals only: Windows' console shows nothing typed until a read."""
    if not _s.cursor_hidden and sys.stdout.isatty() and colour_ok():
        sys.stdout.write("\x1b[?25l")
        sys.stdout.flush()
        _s.cursor_hidden = True
    if IS_WINDOWS or _s.keys_shown is not None:
        return
    import termios
    try:
        fd = sys.stdin.fileno()
        if not os.isatty(fd):
            return
        shown = termios.tcgetattr(fd)
        hidden = list(shown)
        hidden[3] &= ~termios.ECHO
        termios.tcsetattr(fd, termios.TCSANOW, hidden)
    except (OSError, ValueError, termios.error):
        return  # no terminal to keep quiet
    _s.keys_shown = shown


def _show_keys() -> None:
    """The cursor back, and the terminal echoing keys as _hide_keys() found it."""
    if _s.cursor_hidden:
        sys.stdout.write("\x1b[?25h")
        sys.stdout.flush()
        _s.cursor_hidden = False
    shown, _s.keys_shown = _s.keys_shown, None
    if shown is None:
        return
    import termios
    try:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSANOW, shown)
    except (OSError, ValueError, termios.error):
        pass  # the terminal has gone


def _rows(text: str) -> int:
    """Screen rows `text` takes once printed, long lines wrapping."""
    width = max(1, terminal_size((100, 20)).columns)
    plain = ANSI_RE.sub("", text)
    return sum(max(1, -(-len(line) // width)) for line in plain.split("\n"))


def _break() -> None:
    """Finish an unfinished line so the next output starts on a fresh one."""
    if not _s.live and _s.notice_rows:
        sys.stdout.write(_clear_notice())   # a Ctrl-C notice under the notes (_draw_stage)
    if _s.live:
        if _s.live_is_stage and _s.underway_row is not None and _s.rows > _s.underway_row:
            # Left once above what the stage showed already, but out of the
            # cursor's reach (_rows_up_to_stage_line), so its clock went on
            # under the last note: taken off again, not left under each note,
            # which would take two rows a note and say nothing new.
            sys.stdout.write(_clear_notice() + "\r\x1b[K")
            _s.waited = True
        elif _s.live_is_stage:
            # The underway line stays, without its spinner: only the stage
            # under way now has one, on the line drawn for it last.
            _s.underway_row = _s.rows
            line, shown = _stage_line()
            sys.stdout.write(_clear_notice() + "\r" + shown + " " * max(0, _s.live - len(line)) + "\n")
            _counted(1)
            _s.after_stage = True
            _s.after_blank = False
        else:
            sys.stdout.write("\r" + " " * _s.live + "\r")
        sys.stdout.flush()
        _s.live = 0


def _print(text: str, *, stage: bool = False) -> None:
    _break()
    if _s.waited and not stage and ANSI_RE.sub("", text).strip() and not _s.after_blank:
        # Said after a wait - what came next, not more of the note above: a
        # blank row between them, as a section has above it.
        print("", flush=True)
        _s.after_blank = True
        _counted(1)
    _s.waited = False
    if _s.after_blank and not ANSI_RE.sub("", text).strip():
        # Never two blank rows in a row: a block taken off the screen leaves
        # the blank row above it, and what comes next may start with its own.
        return
    if stage:
        _s.after_stage = False  # stage lines follow one another with no gap
    else:
        _gap(text)
    text = _fit(text)
    print(text, flush=True)
    _s.after_blank = not ANSI_RE.sub("", text).strip()
    _counted(_rows(text))
    if stage:
        _s.after_stage = True


def _counted(rows: int) -> None:
    """`rows` more finished on the screen, for taking them off again."""
    _s.rows += rows


def _screen_width() -> int | None:
    """Columns a row may fill after the margin, or None when not a terminal.
    One short of the edge: some consoles start a new row on reaching it."""
    if not sys.stdout.isatty():
        return None
    return max(20, terminal_size((100, 20)).columns - 1 - len(MARGIN))


def _clip(line: str) -> str:
    """A row redrawn in place, cut to fit: wrapped, it could not be redrawn."""
    width = _screen_width()
    if width is None or len(line) <= width + len(MARGIN):
        return line
    return line[: width + len(MARGIN) - 3] + "..."


def _fit(text: str) -> str:
    """Text for the screen: each row after the margin, long ones wrapped to
    the terminal's width."""
    width = _screen_width()
    rows = []
    for line in text.split("\n"):
        for row in (_wrap(line, width) if width else [line]):
            rows.append(MARGIN + row if ANSI_RE.sub("", row).strip() else row)
    return "\n".join(rows)


def _wrap(line: str, width: int) -> list[str]:
    """One line as rows of at most `width` visible characters, broken between
    words.  A row carrying on from the one above starts under its text - past
    a leading indent, or past a "Label:" set in from the edge - so lists and
    lined-up labels stay lined up.  A word longer than a row (a long URL) is
    never broken: it starts a row, at the edge if need be, and only past the
    terminal's width does the terminal itself wrap it."""
    plain = ANSI_RE.sub("", line)
    if len(plain) <= width:
        return [line]
    lead = len(line) - len(line.lstrip(" "))
    label = re.match(r"\s+[^\s:][^:]{0,30}:\s+(?=\S)", plain)
    hang = len(label.group(0)) if label else lead
    if hang > width // 2:
        hang = lead if lead <= width // 2 else 0
    rows: list[str] = []
    row, used, started = " " * lead, lead, False
    for word in line[lead:].split(" "):
        size = len(ANSI_RE.sub("", word))
        if started and size and used + 1 + size > width:
            rows.append(row.rstrip(" "))
            indent = hang if hang + size <= width else 0  # a long URL: as much of it on one row as can be
            row, used = " " * indent + word, indent + size
        elif started:
            row, used = row + " " + word, used + 1 + size
        else:
            row, used, started = row + word, used + size, True
    rows.append(row)
    return rows


def _margin_stream(text: str) -> str:
    """A helper's raw output, each row started at the margin."""
    out = []
    for char in text:
        if _s.stream_fresh and char not in "\r\n":
            out.append(MARGIN)
            _s.stream_fresh = False
        out.append(char)
        if char in "\r\n":
            _s.stream_fresh = True
    return "".join(out)


def _gap(text: str) -> None:
    """One blank line between a stage's line and whatever is shown under it -
    a message, a question, a helper's output - unless that starts with one."""
    if not _s.after_stage:
        return
    _s.after_stage = False
    if ANSI_RE.sub("", text).strip("\r\n\t "):
        print("", flush=True)
        _s.after_blank = True
        _counted(1)


def _log_ctrl_c() -> None:
    """The log says a Ctrl-C came when it came, though the stage may fail before
    anything is asked about it. From the signal handler, so without the lock the
    interrupted code may hold: one line in one append, and _write_log flushes whole
    lines, so neither can split the other's."""
    if _s.log_path is None:
        return
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    try:
        log = os.open(_s.log_path, os.O_WRONLY | os.O_APPEND)
        try:
            os.write(log, f"[{stamp}] {CTRL_C_LOGGED}\n".encode())
        finally:
            os.close(log)
    except OSError:
        pass  # the log being moved, say: the run goes on without this line


def _write_log(text: str) -> None:
    if _s.log is None:
        return
    stamp = datetime.datetime.now().strftime("%H:%M:%S")
    for line in scrub(str(text)).split("\n"):
        _s.log.write(f"[{stamp}] {line}\n")
        _note_switch_advice(line)
    _s.log.flush()


def _note_switch_advice(line: str) -> None:
    """A "switch" event for advice to switch the device off and on in `line`, or
    begun in the line before: a message wrapped by hand breaks it over two
    (arrival.NOT_BACK_LINES)."""
    line = " ".join(line.split())
    before = _s.last_logged
    _s.last_logged = line
    if any(found.end() > len(before) for found in SWITCH_OFF_AND_ON.finditer(f"{before} {line}")):
        event("switch", **(_s.switching_now or {}))
