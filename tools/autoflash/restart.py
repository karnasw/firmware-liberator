"""Restarting a device that still answers but has stopped getting anywhere,
as switching it off and on would, so a run need not ask anyone to.

Each firmware a run leaves a device on part way has a way, asked of whatever
answers at its address (detect.identify):

- Tasmota, its startup firmware (Tasmota's safeboot) and the interim one
  included: the command Restart 1;
- the mover, from 1.2.0: POST /restart (devices/mover_routes.h);
- OpenBeken, the interim and the readout firmware included: reboot.

Not the installer (the bridge): it restarts into Sonoff's firmware, which
leaves a conversion part way, so it is restarted only where that is meant
(routes/source.py, readout_espressif.py). Nor Sonoff's own firmware, which has
no such command over the cloud or the local network (docs/espressif-research.md,
"No restart command"), or this project's ESPHome.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request

from tools.autoflash import detect, openbeken, output
from tools.autoflash.device import Device, DeviceError

TASMOTA = {"tasmota", "hub", "startup"}
OPENBEKEN = {"openbeken", "interim", "readout"}
MOVER = {"mover"}
# How long a device restarted is given to answer again.
BACK_WAIT = 120.0


def ask(host: str, running: str) -> bool:
    """Ask the `running` firmware at `host` to restart: whether it said it would."""
    if running in TASMOTA:
        try:
            said = Device(host, timeout=10).command("Restart 1", timeout=10)
        except (DeviceError, OSError):
            return False
        return isinstance(said, dict) and "Restart" in said
    if running in MOVER:
        request = urllib.request.Request(f"http://{host}/restart", data=b"", method="POST",
                                         headers={"User-Agent": "Autoflash/1"})
        try:
            with urllib.request.urlopen(request, timeout=10) as reply:
                return reply.status == 200
        except (urllib.error.URLError, OSError):
            return False   # a mover before 1.2.0 has no restart
    return False


def restart(host: str) -> bool:
    """The device at `host` restarted, answering again: whether it was. False
    at once where what answers there has no way to be restarted."""
    running = detect.identify(host)
    if running in OPENBEKEN:
        output.say(f"    Restarting the device ({detect.NAMES.get(running, running)})...")
        return openbeken.restart(host, wait=BACK_WAIT)
    if running not in TASMOTA | MOVER or not ask(host, running):
        return False
    output.say(f"    Restarting the device ({detect.NAMES.get(running, running)})...")
    time.sleep(5.0)   # it answers before it restarts
    deadline = time.monotonic() + BACK_WAIT
    while time.monotonic() < deadline:
        if detect.identify(host) is not None:
            return True
        time.sleep(3.0)
    return False
