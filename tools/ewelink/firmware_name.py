"""The firmware name a device's stock updater checks, learned without the device.

A Sonoff update file starts with a wrapper naming the firmware it is for
(FWSW-E32S60-S60-ESP32C3FN4). The updater refuses a file with another name, and
the names vary too much to guess, so they are read: from the file eWeLink offers
the device, or else from the filenames of the public update files, which carry
them. eWeLink goes by the device's id, not its model, so it can only say this for
a device in the account, which is all a run needs. Only the first bytes of an
offered file are fetched, and neither its link nor the device's id is logged or kept.

learn() never raises: a run goes on without the name. Nothing here sends anything
to a device.
"""

from __future__ import annotations

import json
import os
import re
import struct
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from tools.autoflash.hostsys import urlopen
from tools.ewelink import offered_firmware

# The public files' folder listed at one commit, the one
# tools/espressif/emulator/public_dump.py fetches its files from.
PUBLIC_COMMIT = "4d3556605da2ce101382a01b6fb7d84e68c3df2a"
PUBLIC_LIST = ("https://api.github.com/repos/openshwprojects/FlashDumps/git/trees/"
               f"{PUBLIC_COMMIT}:IoT/zOTAz/eWeLink")
TIMEOUT = 5.0   # a little for a run to wait for something it can do without

HEAD_SIZE = 1024   # enough for the outer header and several records
FOTA_MAGIC = 0x1BEEF11E
FOTA_HEADER_SIZE = 16
RECORDS_AT = 0x18
RECORD_SIZE = 76
NAME_SIZE = 32
A_NAME = re.compile(r"[A-Z][A-Z0-9_]*(-[A-Z0-9_]+)*")
# SONOFF_S60TPG-UK_(FWSW-E32S60-S60-ESP32C3FN4)_ESP32-C3_v1.2.0_user1.bin; a
# firmware of several parts names each, joined by a +.
PUBLIC_FILE = re.compile(r"SONOFF_(?P<model>.+?)_(\(.*\)_)?\((?P<names>[^()]+)\)_[^_]+_v[0-9.]+_user1\.bin")
# What the stock firmware calls itself when it says its chip: SN-ESP32C3-S60-01.
STOCK_MODEL = re.compile(r"SN-(?P<chip>[A-Z0-9]+)-(?P<model>[A-Z0-9]+)-[0-9]+")
# One file for a run of gangs: M5-1C-3C is the M5-1C-86, M5-2C-86 and M5-3C-120.
GANGS = re.compile(r"(?P<base>[A-Z0-9]+)-(?P<low>[0-9])C-(?P<high>[0-9])C")
A_GANG = re.compile(r"(?P<base>[A-Z0-9]+)-(?P<gang>[0-9])C(-[A-Z0-9]+)*")
CHIPS = ((re.compile(r"ESP32(C[0-9]|S[0-9]|H[0-9])"), None),
         # Sonoff's own names also spell it with a letter O: ESP32DOWDR2V3.
         (re.compile(r"ESP32D[0O]WD"), "ESP32D0WD"),
         (re.compile(r"ESP8266"), None),
         (re.compile(r"BK[0-9]{4}"), None),
         (re.compile(r"BL[0-9]{3}"), None))


@dataclass(frozen=True)
class Learned:
    """A firmware name, the chip it implies ("" when it implies none), and where it came from."""
    name: str
    chip: str
    source: str


def chip_of(name: str) -> str:
    """The chip a firmware name implies, by its last part: ESP32C3, ESP32D0WD,
    BK7238. "" for a name that says none, as the oldest do (FWSW-0185), and
    for a part that is not a device's main chip (a screen's HMIX120)."""
    last = name.rsplit("-", 1)[-1]
    for pattern, chip in CHIPS:
        found = pattern.match(last)
        if found:
            return chip or found.group()
    return ""


def main_name(names: Iterable[str]) -> str:
    """Of a file's names, the one for the device's main chip. A file can also
    hold a screen's or a meter's firmware, listed before or after it."""
    names = list(names)
    return next((name for name in names if chip_of(name)), names[0] if names else "")


def names_in_file(head: bytes) -> list[str]:
    """The firmware names in the first bytes of an update file; none for a file
    without a wrapper, as ESP8266 ones are. A Beken file has the same wrapper
    after an outer header, whose size its bytes 12 and 13 add to 16."""
    base = 0
    if len(head) >= FOTA_HEADER_SIZE and struct.unpack_from("<I", head)[0] == FOTA_MAGIC:
        base = FOTA_HEADER_SIZE + struct.unpack_from("<H", head, 12)[0]
    if len(head) < base + 2:
        return []
    names = []
    for index in range(head[base + 1]):
        record = head[base + RECORDS_AT + index * RECORD_SIZE:][:NAME_SIZE]
        name = record.split(b"\0", 1)[0].decode("ascii", "replace")
        if not A_NAME.fullmatch(name):
            break
        names.append(name)
    return names


def read_head(url: str, deviceid: str) -> bytes:
    """The first bytes of an offered file. Sonoff's server answers an empty 400
    unless asked as a device asks, with its id, a time and a signature; it does
    not check the last two. Only these bytes are read, even from a server that
    ignores the range and starts sending the whole file."""
    request = urllib.request.Request(offered_firmware.as_device(url, deviceid),
                                     headers={"Range": f"bytes=0-{HEAD_SIZE - 1}",
                                                    "User-Agent": "autoflash-research/0.1"})
    with urlopen(request, timeout=TIMEOUT) as reply:
        return reply.read(HEAD_SIZE)


def name_offered(offers: list[dict[str, Any]], deviceid: str,
                 read: Callable[[str, str], bytes] = read_head) -> str:
    """The name in the file eWeLink offers this device, read with the device's
    own id: its user1.bin, as user2.bin is the same firmware for the other slot.
    "" with nothing offered, or no id to ask with."""
    if not deviceid:
        return ""
    for offer in offers:
        files = offer.get("binList") or []
        for item in sorted(files, key=lambda file: file.get("name") != "user1.bin")[:1]:
            url = str(item.get("downloadUrl", ""))
            if urllib.parse.urlsplit(url).scheme in ("http", "https"):
                name = main_name(names_in_file(read(url, deviceid)))
                if name:
                    return name
    return ""


def public_files() -> list[str]:
    """The public update files' names."""
    request = urllib.request.Request(PUBLIC_LIST, headers={"Accept": "application/vnd.github+json",
                                                           "User-Agent": "autoflash-research/0.1"})
    with urlopen(request, timeout=TIMEOUT) as reply:
        return [str(item.get("path", "")) for item in json.load(reply).get("tree", [])]


def same_model(product: str, listed: str) -> bool:
    """Whether a file's model is the product's, whole words only: S60TPG is
    S60TPG-UK, but POWR316D is not POWR3, a different device on another chip."""
    product, listed = product.upper(), listed.upper()
    return bool(product) and (product == listed or product.startswith(listed + "-")
                              or listed.startswith(product + "-"))


def in_gangs(product: str, listed: str) -> bool:
    """Whether a file for a run of gangs is the product's: M5-1C-3C is M5-2C-86."""
    gangs, gang = GANGS.fullmatch(listed.upper()), A_GANG.fullmatch(product.upper())
    return bool(gangs and gang and gangs.group("base") == gang.group("base")
                and gangs.group("low") <= gang.group("gang") <= gangs.group("high"))


def name_listed(files: Iterable[str], product: str, stock_model: str) -> str:
    """The name among public filenames for this device: by eWeLink's product
    model, then by a file for a run of gangs it is in, or else by the model its
    stock firmware names, on the same chip. ""
    unless exactly one name fits: a wrong one would only mislead the reader."""
    listed = []
    for file in files:
        found = PUBLIC_FILE.fullmatch(file)
        if found:
            listed.append((found.group("model"), main_name(found.group("names").split("+"))))
    for fitting in (same_model, in_gangs):
        fits = {name for model, name in listed if fitting(product, model)}
        if len(fits) == 1:
            return fits.pop()
    stock = STOCK_MODEL.fullmatch(stock_model)
    if not stock:
        return ""
    fits = {name for _, name in listed
            if name.count("-") == 3 and name.split("-")[2] == stock.group("model")
            and chip_of(name).startswith(stock.group("chip"))}
    return fits.pop() if len(fits) == 1 else ""


def learn(offers: list[dict[str, Any]], deviceid: str, product: str,
          stock_model: str) -> tuple[Learned | None, list[str]]:
    """A device's firmware name if it can be learned, and what went wrong on the
    way, for the log: only an exception's type, as its text could hold a link or
    the device's id. A run on the emulator's cloud stub asks nothing beyond the
    stub. `deviceid` must be the device's own: nothing here makes one up."""
    problems = []
    try:
        name = name_offered(offers, deviceid)
        if name:
            return Learned(name, chip_of(name), "eWeLink's update file"), problems
    except Exception as exc:  # whatever goes wrong: a run needs none of this
        problems.append(f"eWeLink's update file could not be read ({type(exc).__name__})")
    if not os.environ.get("AUTOFLASH_EWELINK_API_BASE"):
        try:
            name = name_listed(public_files(), product, stock_model)
            if name:
                return Learned(name, chip_of(name), "the public file list, by model"), problems
        except Exception as exc:  # as above
            problems.append(f"the public file list could not be read ({type(exc).__name__})")
    return None, problems
