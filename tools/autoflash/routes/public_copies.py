"""Sonoff's own update file for each kind of device, as the FlashDumps project
keeps them in public: the last place either chip family takes Sonoff's
firmware from, after the device's own backup and the file eWeLink offers the
device.  Each is pinned by name and hash, at one commit, so what is fetched is
what was checked here; it is then checked as eWeLink's file is (the Espressif
route's sonoff_firmware.checked_app, the Beken route's images.checked).
"""

from __future__ import annotations

import hashlib
import urllib.parse

from tools.autoflash import hostsys, kinds, output
from tools.autoflash.runs import ROOT
from tools.ewelink import firmware_name

AT = f"https://raw.githubusercontent.com/openshwprojects/FlashDumps/{firmware_name.PUBLIC_COMMIT}/IoT/zOTAz/eWeLink/"
# Each kind's update file at that commit, by its name and SHA-256: the newest
# that names the kind's firmware (Kind.wrapper_model). The M5s share one.
COPIES: dict[str, tuple[str, str]] = {
    "sonoff-s60": ("SONOFF_S60TPG-UK_(FWSW-E32S60-S60-ESP32C3FN4)_ESP32-C3_v1.2.0_user1.bin",
                   "43620d7645b9cfd188181b0ac0d3e86934193ebd3dcd0f6bb1bc3d6b5046f68c"),
    "sonoff-s61": ("SONOFF_S61s_(FWSW-01-S61-BK7238)_BK7238_v1.1.0_user1.bin",
                   "fd5d81c79fea1501d347f38c0ea1856d12688b4b750d86e7ad6c0a2ecf16740e"),
    "sonoff-pow-elite": ("SONOFF_POWR316D_(FWSW-E32POWP-POWPRO-ESP32D0WDV3)_ESP32_v1.2.0_user1.bin",
                         "90a4d06b8bc59e1127bdeaefaf089323aecc426fc8f8406d9cb307abf1240946"),
    "sonoff-powct": ("SONOFF_POWCT_(FWSW-E32POWCT-POWCT-ESP32D0WDV3)_ESP32_v1.3.1_user1.bin",
                     "d6e82566f1fae5312b1a589ba6879f2186a3e357d292ad58707c13c1a4629498"),
    "sonoff-th-elite": ("SONOFF_THR320D_(FWSW-E32THR3-THR3-ESP32D0WDV3)_ESP32_v1.3.0_user1.bin",
                        "ec8251c25abe772518acbe8fe7d72b9a73398a0e409c621b7336dfd5afde2d2f"),
    "sonoff-dualr3": ("SONOFF_DUALR3-Family_(FWSWES-E32DR3-DUALR3-ESP32D0WDV3)_ESP32_v1.6.1_user1.bin",
                      "4f14b1298061ee6669fd5fcbfa89baf6c2ef3daafa8c575736f38088179240b2"),
    "sonoff-basic-1gs": ("SONOFF_BASIC-1GS_(FWSW-01-BASICR5-BK7238)_BK7238_v1.0.4_user1.bin",
                         "0df1f696e9c03b076fda5b6ef103b9a658ba3ec495afa2b013586c1c40e32d25"),
    "sonoff-nspanel": ("SONOFF_NSPanel_(FWPNL-E32NSP-NSPANEL-ESP32D0WDV3+FWPNL-E32NSP-NSPANEL-HMIX120"
                       "+FWPNL-E32NSP-NSPANEL-HMIX86)_ESP32_v1.6.0_user1.bin",
                       "6bef88e7055239e0c1335d32bf10ec237839425629b05d60228325f497b35845"),
    "sonoff-basicr4": ("SONOFF_BASICR4_(FWSW-E32R4-BASICR4-ESP32C3)_ESP32-C3_v1.1.0_user1.bin",
                       "cd4bbf2b08d9918a920d81c3ed2cef6e546673fd2c280106fd6852528866ac4a"),
    "sonoff-minir4": ("SONOFF_MINIR4_(FWSW-E32R4-MINIR4-ESP32D0WDR2V3)_ESP32_v1.2.0_user1.bin",
                      "c881ddd667bc9d8c6905acf30051a1f2e4270c4be2dcfcfa39a5bfdd137c6ec0"),
    **dict.fromkeys(("sonoff-m5-1c", "sonoff-m5-2c", "sonoff-m5-3c"), (
        "SONOFF_M5-1C-3C_(FWSWES-E32T5-T5-ESP32D0WDV3)_ESP32_v1.3.0_user1.bin",
        "f149baf000eb226c27babd1ea2a4bef6bcc3ca2973d9c57063106730112f4e6b")),
}
# Where a copy fetched is kept, under its own name: fetched once.
KEPT = ROOT / "captures" / "public-copies"
# What the run says it took Sonoff's firmware from, when from here.
SOURCE = "the public copy of Sonoff's update file (FlashDumps)"
TIMEOUT = 120.0


class Unavailable(Exception):
    """No public copy of the kind's update file could be had: none known, not
    fetched, or not the one pinned."""


def fetch(kind: kinds.Kind) -> bytes:
    """The kind's update file, as pinned: kept from an earlier fetch, else
    fetched and kept. Unavailable, saying why, where it cannot be had."""
    try:
        name, digest = COPIES[kind.key]
    except KeyError:
        raise Unavailable(f"no public copy of the {kind.name}'s update file is known") from None
    path = KEPT / name
    data = path.read_bytes() if path.is_file() else b""
    if hashlib.sha256(data).hexdigest() == digest:
        return data
    output.say(f"Fetching the public copy of Sonoff's update file for the {kind.name}...")
    try:
        data = hostsys.download(AT + urllib.parse.quote(name), timeout=TIMEOUT, say=output.detail)
    except OSError as exc:
        raise Unavailable(f"the public copy of the {kind.name}'s update file could not be fetched: {exc}") from exc
    if hashlib.sha256(data).hexdigest() != digest:
        raise Unavailable(f"the public copy of the {kind.name}'s update file is not the one this tool knows "
                          "(its hash differs): refused")
    KEPT.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return data
