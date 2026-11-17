"""How long each fault-suite scenario took when it last ran, so a batch can
start the longest first.

A batch ends when its last scenario does: one of half an hour started near the
end keeps a machine going alone while the others are done. Started first, the
long ones run beside the short ones, and the machines' end times are close
enough to share out again.

Taken from every results file a machine has kept, one to a suite's folder
(`<root>/<suite>/results.jsonl`; the Beken suite's, in beken/, are a level
further down, so each family reads its own): a row's own `seconds`, or for a
row without (the Espressif suite's before it recorded them) the time since the
row before it from the same emulator instance, whose worker runs its scenarios
one after another.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

# A gap between two rows of one instance longer than this is a pause between
# batches, not a scenario (seconds).
LONGEST = 2 * 3600
# A typical scenario, where none has been timed on this computer: the median of
# the full run of 2026-11-08 (seconds).
TYPICAL = 300.0


def when(row: dict[str, Any]) -> dt.datetime | None:
    stamp = row.get("finished") or row.get("at")
    try:
        return dt.datetime.fromisoformat(stamp) if stamp else None
    except ValueError:
        return None


# Where results copied back from other machines are kept, one folder a copy,
# laid out as captures/emulation is (docs/testing.md#the-fault-suites).
KEPT = "kept"


def kept(emulation: Path, family: str = "") -> list[Path]:
    """The copies kept below `emulation`, each where `family`'s suite folders
    are in it ("beken" for the Beken suite's, a level further down)."""
    return [copy / family if family else copy for copy in sorted(Path(emulation, KEPT).glob("*/"))]


def times(*roots: Path) -> dict[str, float]:
    """Each scenario id in the suites' folders in `roots`, and how long it took
    the last time it ran."""
    found: dict[str, tuple[dt.datetime, float]] = {}
    for path in sorted(path for root in roots for path in Path(root).glob("*/results.jsonl")):
        previous: dict[Any, dt.datetime] = {}
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or "id" not in row:
                continue
            finished, instance = when(row), row.get("instance")
            seconds = row.get("seconds")
            if seconds is None and finished and instance in previous:
                gap = (finished - previous[instance]).total_seconds()
                seconds = gap if 0 < gap < LONGEST else None
            if finished and instance is not None:
                previous[instance] = finished
            if isinstance(seconds, (int, float)) and seconds > 0 and finished \
                    and (row["id"] not in found or found[row["id"]][0] <= finished):
                found[row["id"]] = (finished, float(seconds))
    return {sid: seconds for sid, (_, seconds) in found.items()}


def fault_seconds(scenario: dict[str, Any]) -> float:
    """How long a scenario's fault lasts, where its length is its argument
    (an outage of 180 s): what one never run takes beyond the usual."""
    arg = scenario.get("arg")
    return float(arg) if isinstance(arg, (int, float)) else 0.0


def lengths(took: dict[str, float],
            estimate: Callable[[dict[str, Any]], float] = fault_seconds) -> Callable[[dict[str, Any]], float]:
    """How long a scenario is taken to last: what it took last; one never run,
    the typical one (TYPICAL, where none has run) and its fault's length."""
    typical = statistics.median(took.values()) if took else TYPICAL
    return lambda sc: took.get(sc["id"], typical + estimate(sc))


def longest_first(scenarios: Iterable[dict[str, Any]], took: dict[str, float],
                  estimate: Callable[[dict[str, Any]], float] = fault_seconds) -> list[dict[str, Any]]:
    """`scenarios` in the order to start them: by how long each took last, the
    longest first (lengths). Those that took as long keep their order."""
    length = lengths(took, estimate)
    return sorted(scenarios, key=lambda sc: -length(sc))


def shares(scenarios: Iterable[dict[str, Any]], took: dict[str, float], workers: list[int],
           estimate: Callable[[dict[str, Any]], float] = fault_seconds) -> list[tuple[list[dict[str, Any]], float]]:
    """`scenarios` shared out between machines running `workers` emulators
    each, so that they end together: the longest first, each to the machine
    that would then end soonest. Each share, longest first, and about how long
    it takes (seconds)."""
    if not workers or min(workers) < 1:
        raise ValueError("each machine runs one emulator or more")
    length = lengths(took, estimate)
    found: list[list[dict[str, Any]]] = [[] for _ in workers]
    load = [0.0] * len(workers)
    for sc in sorted(scenarios, key=lambda sc: -length(sc)):
        machine = min(range(len(workers)), key=lambda n: ((load[n] + length(sc)) / workers[n], n))
        found[machine].append(sc)
        load[machine] += length(sc)
    return [(share, load[n] / workers[n]) for n, share in enumerate(found)]


def write_shares(ids_file: str, workers: list[int], every: Iterable[dict[str, Any]],
                 took: dict[str, float]) -> list[str]:
    """`ids_file`'s scenarios (its first word on each line, as `list` writes
    it) shared out (shares), each share written beside it as `ids_file`.1,
    .2, ...; what to say of each."""
    known = {sc["id"]: sc for sc in every}
    ids = [line.split()[0] for line in Path(ids_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    unknown = [i for i in ids if i not in known]
    if unknown:
        raise ValueError(f"not scenarios of this suite: {', '.join(unknown[:5])}")
    said = []
    for n, (share, seconds) in enumerate(shares([known[i] for i in ids], took, workers), 1):
        path = Path(f"{ids_file}.{n}")
        path.write_text("".join(sc["id"] + "\n" for sc in share), encoding="utf-8")
        said.append(f"{path}: {len(share)} scenarios on {workers[n - 1]} emulators, "
                    f"about {seconds / 3600:.1f} h")
    return said
