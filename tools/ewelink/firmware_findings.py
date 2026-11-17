"""What Sonoff's own firmware says about the hardware it drives, read from its
app image: findings a readout or a backup logs, so a device nobody here has
seen says what its board holds from the first report of it.

Each finding comes from one detector, and each detector from evidence seen in a
firmware whose device is known: the names it carries of the chips it talks to,
the tasks it runs. A new kind of hardware is recognised everywhere once a
detector is added for it here. String evidence works on any chip whose app is
stored in plain bytes (every Espressif one so far); a finding says what the
firmware can drive, not that the board has it, and is a lead to check, not a
fact.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Finding:
    what: str    # the hardware or behaviour, a few words
    detail: str  # what was seen, and where it was first


Detector = Callable[[bytes], "Finding | None"]
DETECTORS: list[Detector] = []


def detector(function: Detector) -> Detector:
    DETECTORS.append(function)
    return function


def _has(app: bytes, *names: bytes) -> list[str]:
    return [name.decode() for name in names if name in app]


# The chips a firmware names, by what they are: each seen in a firmware of a
# device known to have one.
CHIPS = {
    "power meter": (b"CSE7759", b"CSE7761", b"BL0939", b"BL0937", b"BL0942", b"HLW8012", b"HLW8032", b"CSE7766"),
    "temperature sensor": (b"DS18B20", b"AM2301", b"MS01", b"SI7021", b"SHT3X", b"SHT4X", b"AHT20"),
}
CHIPS_SEEN = {"power meter": "CSE7759 on the S60 and POW Elite, CSE7761 on the POWCT and Dual R3, BL0939 on the Dual R3",
              "temperature sensor": "DS18B20, AM2301 and MS01 on the TH Elite"}


def _chips(kind: str) -> Detector:
    def found(app: bytes) -> Finding | None:
        names = _has(app, *CHIPS[kind])
        return Finding(kind, f"names {', '.join(names)} (seen: {CHIPS_SEEN[kind]})") if names else None
    found.__name__ = kind.replace(" ", "_")
    return found


for _kind in CHIPS:
    detector(_chips(_kind))


@detector
def sonoff_sdk(app: bytes) -> Finding | None:
    """Which of Sonoff's SDKs it is built on: where its settings and identity are."""
    if b"ais_factory" in app:
        return Finding("Sonoff's ais SDK", "identity and Wi-Fi in its factory and pair records "
                       "(tools/espressif/ais_records.py), as the BASICR4's and MINIR4's")
    if b"components/esf/" in app:
        return Finding("Sonoff's esf SDK", "identity and Wi-Fi in frame_params, as the S60's")
    return None


@detector
def overheat_lock(app: bytes) -> Finding | None:
    """A task that watches a temperature input and stops updates while it reads hot."""
    if b"TEMP_EVENT" in app and b"TEMPERATURE_CHECK" in app:
        return Finding("overheat lock", "a TEMPERATURE_CHECK task posting TEMP_EVENT: on the BASICR4 it reads a "
                       "thermistor on an ADC input, and while it reads hot updates are refused (error 407)")
    return None


@detector
def analog_input(app: bytes) -> Finding | None:
    """It reads an ADC input in millivolts: a thermistor, a light sensor, a battery."""
    if b"esp_adc_cal" in app or b"adc_cali" in app:
        return Finding("analog input", "calibrates its ADC (esp_adc_cal): reads a voltage, as the BASICR4's "
                       "thermistor and the NSPanel's are read")
    return None


@detector
def external_ram(app: bytes) -> Finding | None:
    if re.search(rb"spiram|esp_psram", app, re.IGNORECASE):
        return Finding("external RAM", "uses PSRAM: the emulator needs the kind's psram_mb, as the MINIR4 and "
                       "NSPanel do")
    return None


@detector
def matter(app: bytes) -> Finding | None:
    if b"esp_matter" in app or b"CHIPoBLE" in app:
        return Finding("Matter", "a Matter stack: Sonoff's Matter devices seen so far are locked with secure boot")
    return None


def findings(app: bytes) -> list[Finding]:
    """Every detector's finding on this app image, in the order they are listed."""
    return [found for found in (detect(app) for detect in DETECTORS) if found is not None]
