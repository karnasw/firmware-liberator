"""What a step that was cut off left on the device: each destructive step's
evidence that it finished, judged from what the device answers as - and, for a
step recorded done, whether that still holds.
"""

from __future__ import annotations

import time
from typing import Callable, TYPE_CHECKING

from tools.autoflash import detect, runs
from tools.autoflash.routes import update
from tools.autoflash.routes.beken import images, stock

if TYPE_CHECKING:
    from tools.autoflash.context import Context

# The steps that write to the device, which happen once.
DESTRUCTIVE_STEPS = {"B2", "J2", "J4", "O1", "O2", "E1", "E2", "U1", "U3"}


# How long a device just switched on may take to answer, before its record is judged.
ANSWER_WAIT = 60.0


def still_installed(ctx: Context, wait: float = ANSWER_WAIT) -> tuple[bool, str]:
    """Whether an onward step recorded done still holds: not where the device
    answers as the firmware it was leaving. A device not answering yet, or
    answering as the new one, leaves the record be."""
    deadline = time.monotonic() + wait
    while True:
        installed, message = evidence_installed(ctx)
        if installed is not None or time.monotonic() >= deadline:
            return installed is not False, message[0].lower() + message[1:].rstrip(".")
        time.sleep(5)


def evidence_installed(ctx: Context) -> tuple[bool | None, str]:
    """Whether a step cut off got its image onto the device: the device answers as
    the firmware the run installs, or still as the one it left - the interim
    firmware, on a run from Sonoff's that has one (J2)."""
    host = ctx.state.input("device_ip")
    firmware, source = runs.run_firmware(ctx.state), runs.run_source(ctx.state)
    if source == "sonoff" and firmware in images.INTERIM:
        source = "interim"
    leaving = detect.NAMES.get(source, source)
    running = detect.identify(host, timeout=3.0) if host else None
    if running == firmware or (firmware == "sonoff" and running is None and not detect.answers_as_tasmota(host)):
        return (True, f"The device answers as {detect.NAMES[firmware]}." if running == firmware
                else "The device answers as neither OpenBeken nor ESPHome: Sonoff's firmware is back.")
    if running == source:
        return False, f"The device still answers as {leaving}: the image was not applied."
    return None, "The device does not answer as the new firmware yet - it may still be installing it."


# What a step cut off part way may have left: the device says, by what it answers as.
ATTEMPTED_EVIDENCE: dict[str, Callable[["Context"], tuple[bool | None, str]]] = {
    "B2": stock.evidence_pushed,
    "J2": evidence_installed,
    "J4": evidence_installed,
    "O1": evidence_installed,
    "O2": evidence_installed,
    "E1": evidence_installed,
    "E2": evidence_installed,
    "U1": update.evidence_updated,
    "U3": update.evidence_updated,
}
