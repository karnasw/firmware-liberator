"""Whether this computer has the processor time to start another emulated
device. One started on a machine already overloaded has its own connections time
out with nothing wrong: on 2026-11-08 unit tests run beside a batch took the
laptop to a load of 28 on 6 cores, and fresh devices never reached eWeLink. So
both fault suites wait, before starting a scenario, while the machine is that
busy; what is running already carries on.
"""
from __future__ import annotations

import os
import time
from typing import Callable

# Overloaded above this many runnable tasks a core, over the last minute. A
# batch with an emulator on each core keeps the load near one a core: the line
# is well above that, and well under what starved the devices.
PER_CORE = 1.5
# The longest a scenario is held back (seconds): load from something else that
# does not go away must not stop the batch for good.
LONGEST = 1800.0
POLL = 10.0


def overloaded(cores: int | None = None) -> bool:
    """Whether the last minute's load is above PER_CORE for each core."""
    try:
        load = os.getloadavg()[0]
    except OSError:   # not kept here (Windows)
        return False
    return load > PER_CORE * (cores or os.cpu_count() or 1)


def wait_for_room(say: Callable[[str], object] = print) -> float:
    """Waits while the machine is overloaded, up to LONGEST, saying so once;
    how long it waited (seconds)."""
    started = time.monotonic()
    if not overloaded():
        return 0.0
    say(f"cpu_load: the load is above {PER_CORE:g} a core; the next scenario waits for it to fall")
    while overloaded():
        if time.monotonic() - started >= LONGEST:
            say(f"cpu_load: still overloaded after {LONGEST / 60:.0f} minutes; starting the next scenario anyway")
            break
        time.sleep(POLL)
    return time.monotonic() - started
