"""What differs from one Espressif chip to the next: where its bootloader goes,
what Tasmota's files for it are called, and the firmware published for it.

The conversion is otherwise the same on each: Tasmota's partition table for a
4 MB ESP32-C3 and for a 4 MB classic ESP32 are byte for byte one table.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.espressif.analyze_vendor_ota import WRAPPER_SIZE

ROOT = Path(__file__).resolve().parents[2]
FIRMWARE = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))


@dataclass(frozen=True)
class Chip:
    key: str             # as kinds.Kind.chip, and tasmota.arch() on the device
    label: str           # what a person calls it
    chip_id: int         # in an ESP image's header
    bootloader_at: int   # where its ROM looks for the bootloader
    tasmota_prefix: str  # tasmota32c3.bin, tasmota32.bin
    bundle: str          # the firmware bundle's folder under captures/
    config_version: int  # Tasmota's settings: its chip family
    # The Tasmota build it runs part way through: one with Berry, small enough
    # for a stock slot. On an ESP32 not the Bluetooth build, which keeps so much
    # memory for Bluetooth that Berry runs out holding a bootloader to write.
    interim_build: str

    @property
    def pins(self) -> dict[str, Any]:
        """What firmware.json pins for it: what every device on it shares."""
        return FIRMWARE["espressif"].get(self.key, {})

    def pin(self, name: str) -> dict[str, Any]:
        try:
            return self.pins[name]
        except KeyError:
            raise ValueError(f"artifacts/firmware.json pins no {name} for the {self.label}") from None

    def file(self, name: str) -> Path:
        """A file published in artifacts/, as firmware.json pins it."""
        return ROOT / self.pin(name)["file"]

    def bundle_file(self, name: str) -> Path:
        """Where a run's firmware bundle keeps one of its files."""
        return ROOT / "captures" / self.bundle / name

    def bridge_app(self) -> bytes:
        """The bridge's app, as firmware.json pins it: published wrapped for the
        S60 on the ESP32-C3 (.ota), bare on the others. Raises ValueError for
        a file that is not the one pinned."""
        pin = self.pin("bridge")
        data = (ROOT / pin["file"]).read_bytes()
        if hashlib.sha256(data).hexdigest() != pin["sha256"]:
            raise ValueError(f"{pin['file']} is not the file firmware.json pins")
        return data[WRAPPER_SIZE:] if pin["file"].endswith(".ota") else data

    @property
    def interim_name(self) -> str:
        """The Tasmota a device runs part way through, in the bundle."""
        return f"{self.tasmota_prefix}-{self.interim_build}.bin"


ESP32C3 = Chip("esp32c3", "ESP32-C3", 5, 0x0, "tasmota32c3", "safeboot-recovery-migration", 4, "bluetooth")
ESP32 = Chip("esp32", "ESP32", 0, 0x1000, "tasmota32", "safeboot-recovery-migration-esp32", 1, "display")
ALL = (ESP32C3, ESP32)


def of(key: str) -> Chip:
    for chip in ALL:
        if chip.key == key:
            return chip
    raise ValueError(f"no such chip: {key}")


def by_id(chip_id: int) -> Chip | None:
    return next((chip for chip in ALL if chip.chip_id == chip_id), None)
