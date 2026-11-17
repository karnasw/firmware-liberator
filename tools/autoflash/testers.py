"""A kind whose status is "Testers wanted" (devices/status.json): converted in
the emulator, or added from its official file and a published pin map, but no
real one reported yet.  The go-ahead before a run's first
write says so, the same words for every such kind.  A proven kind's go-ahead
says nothing of it.
"""

from __future__ import annotations

from tools.autoflash import kinds, output, runs

# Where the log is, for a run with none open.
LOG_ELSEWHERE = "run.log, in this run's folder under captures/"


def wanted(kind: kinds.Kind) -> bool:
    """Whether a run on this kind asks for testers."""
    return kind.status is kinds.Status.TESTERS_WANTED


def notice(kind: kinds.Kind) -> list[str]:
    """What the go-ahead before the first write says of a kind wanting testers."""
    if not wanted(kind):
        return []
    return [
        "",
        "Not yet proven on a real device of this kind. This run's log:",
        runs.display_path(output.log_path()) if output.log_path() else LOG_ELSEWHERE,
    ]
