"""What each fault has found: for each fault and length in a fault suite's
results, here and kept from other machines (scenario_times.kept), how many
scenarios ran it and which failed - so a length that never finds anything
the others do not can be seen, and one that does is kept (`fault_suite.py
found`, docs/testing.md#the-fault-suites).

A failure counts once a scenario has it in any results file, though a fix
later made it pass: that it failed is what the fault found.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# Results that say nothing of what the fault leads to: a pass, a fault that
# never landed or landed outside its step, a computer that could not reach
# eWeLink, nothing to change, an emulator that never started, and the suite's
# own error (the Espressif suite's verdicts in `verdict`, the Beken suite's in
# `result`).
UNTESTED = frozenset({"PASS", "MISTIMED", "NOT-FIRED", "NO-INTERNET", "NO-TRAFFIC", "NO-STEP", "NOT-READY",
                      "HARNESS-ERROR", "ERROR"})
# How many failed scenarios each fault's line names.
EXAMPLES = 3
# A scenario id's fault, as fault_suite.sid writes it: the step, with @10 or @90
# for a moment other than 40%, then the fault and its argument.
FAULT = re.compile(r":[^:]*?(@\d+)?:([a-z]+)-([^:]+)$")


def rows(*roots: Path) -> list[dict[str, Any]]:
    """Every row in the suites' results files in `roots` (as scenario_times
    reads them), a row copied twice from one machine counted once."""
    seen, found = set(), []
    for path in sorted(path for root in roots for path in Path(root).glob("*/results.jsonl")):
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                continue
            key = (row["id"], row.get("finished") or row.get("at"), row.get("host"), row.get("instance"))
            if key not in seen:
                seen.add(key)
                found.append(row)
    return found


def fault_of(scenario_id: str) -> tuple[str, str, str] | None:
    """The fault, its argument and its moment ("" for 40%) of a scenario id."""
    match = FAULT.search(scenario_id)
    return (match[2], match[3], match[1] or "") if match else None


def verdict(row: dict[str, Any]) -> str:
    return row.get("verdict") or row.get("result") or "HARNESS-ERROR"


def found(every: Iterable[dict[str, Any]]) -> list[str]:
    """For each fault, its length and moment: how many scenarios ran it, and
    those that ever failed, with a few named."""
    ran: dict[tuple[str, str, str], set[str]] = {}
    failed: dict[tuple[str, str, str], dict[str, str]] = {}
    for row in every:
        fault, said = fault_of(row["id"]), verdict(row)
        if fault is None or (said in UNTESTED and said != "PASS"):
            continue
        ran.setdefault(fault, set()).add(row["id"])
        if said not in UNTESTED:
            failed.setdefault(fault, {}).setdefault(row["id"], said)

    def order(fault: tuple[str, str, str]):
        name, arg, at = fault
        return name, (0, int(arg), "") if arg.isdigit() else (1, 0, arg), at

    lines = []
    for fault in sorted(ran, key=order):
        bad = failed.get(fault, {})
        named = ", ".join(f"{i} {v}" for i, v in sorted(bad.items())[:EXAMPLES])
        lines.append(f"{fault[0] + '-' + fault[1] + fault[2]:22} {len(ran[fault]):5} run {len(bad):4} failed"
                     + (f"  e.g. {named}" if named else ""))
    return lines
