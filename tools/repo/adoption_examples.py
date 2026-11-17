#!/usr/bin/env python3
"""The configs docs/home-assistant-adoption.md shows, put to ESPHome as lint.py
does the configs firmware.json builds: what ESPHome itself would refuse, in the
ESPHome image the firmware pins.  So is the short config for every device.

The release a guide's config names may not be out yet, so each package is read
from this checkout instead of GitHub.

    python3 -m tools.repo.adoption_examples
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

from tools.repo.lint import ROOT, esphome_command

GUIDE = Path("docs/home-assistant-adoption.md")
PACKAGE = re.compile(r"github://adamsthws/firmware-liberator/(\S+?)@\S+")
KEY = "your device's own key"
MADE_UP_KEY = "E" * 42 + "A="   # as long as a real one; ESPHome refuses all zeros
SECRETS = "wifi_ssid: made-up-network\nwifi_password: made-up-password\n"


def examples(guide: str) -> list[str]:
    """The guide's YAML blocks."""
    return re.findall(r"^```yaml\n(.*?)^```", guide, re.DOTALL | re.MULTILINE)


def from_this_checkout(example: str) -> str:
    """`example`, its package read from /config, with a key ESPHome accepts."""
    return PACKAGE.sub(r"!include /config/\1", example).replace(KEY, MADE_UP_KEY)


def short_configs(devices: list[str]) -> dict[str, str]:
    """The short config the builder writes, for each of `devices` (sonoff-s60)."""
    return {f"short-{device}.yaml": f"packages:\n  device: !include /config/devices/{device}/esphome.yaml\n"
            for device in devices}


def main() -> int:
    devices = sorted(path.parent.name for path in (ROOT / "devices").glob("*/esphome.yaml"))
    configs = short_configs(devices)
    for number, example in enumerate(examples((ROOT / GUIDE).read_text(encoding="utf-8")), 1):
        configs[f"guide-{number}.yaml"] = from_this_checkout(example)
    with tempfile.TemporaryDirectory() as folder:
        where = Path(folder)
        (where / "common").mkdir()
        (where / "secrets.yaml").write_text(SECRETS, encoding="utf-8")
        for device in devices:   # as the guide's "using your own config" saves it
            (where / "common" / f"{device}.yaml").write_bytes((ROOT / "devices" / device / "esphome.yaml").read_bytes())
        for name, config in configs.items():
            (where / name).write_text(config, encoding="utf-8")
        where.chmod(0o755)
        failed = False
        for name in configs:   # one by one, so that a refusal names its config
            result = subprocess.run(esphome_command([f"/examples/{name}"], mounts=[f"{where}:/examples:ro"]),
                                    cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                    errors="replace")
            if result.returncode:
                print(f"{name}:\n{configs[name]}\n{result.stdout}")
                failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
