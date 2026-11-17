"""A fault scenario's earlier attempts, kept when it is run again. A batch run
again used to write over a scenario's logs, so how its first attempt failed was
gone by the time it was looked at; both fault suites keep them instead."""
from __future__ import annotations

from pathlib import Path


def keep_earlier(path: Path) -> Path | None:
    """An earlier attempt's log or folder moved aside for the next, numbered by
    attempt (`.1` the first); where it went, or None with nothing there."""
    if not path.exists():
        return None
    n = 1
    while path.with_name(f"{path.name}.{n}").exists():
        n += 1
    return path.rename(path.with_name(f"{path.name}.{n}"))
