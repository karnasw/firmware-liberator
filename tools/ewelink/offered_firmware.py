"""The firmware files eWeLink offers a device, whole: for a readout of it
(tools/autoflash/readout.py), where they are a second version of its firmware
beside the one the device runs.

eWeLink offers a device the latest firmware when asked as one that is behind
(query_ota.latest_offer), and its server sends a file only to a request made as
a device makes it: with the device's own id, a time and a signature, the last
two unchecked. Neither a file's link, which may act as a token, nor the
device's id is logged or kept. fetch() never raises: a readout goes on without.
"""

from __future__ import annotations

import hashlib
import re
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from tools.autoflash.hostsys import urlopen

# More than any Sonoff update file, which is at most a slot of a 16 MB flash.
MAX_SIZE = 8 * 1024 * 1024
MAX_FILES = 4
TIMEOUT = 60.0
# Tries at each file, a pause between: one that does not come whole leaves the
# readout's offer unlike every other device's, so it can never agree.
ATTEMPTS = 3
RETRY_PAUSE = 2.0


@dataclass(frozen=True)
class Offered:
    """One file of an offer: its name (user1.bin), the version offered, its bytes."""
    name: str
    version: str
    data: bytes

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.data).hexdigest()


def as_device(url: str, deviceid: str) -> str:
    """The link as the device asks for it: its id, a time and a signature added."""
    asked = urllib.parse.urlsplit(url)
    extra = urllib.parse.urlencode({"deviceid": deviceid, "ts": 1, "sign": 0})
    return asked._replace(query=f"{asked.query}&{extra}" if asked.query else extra).geturl()


def download(url: str, deviceid: str) -> bytes:
    request = urllib.request.Request(as_device(url, deviceid), headers={"User-Agent": "autoflash-research/0.1"})
    with urlopen(request, timeout=TIMEOUT) as reply:
        data = reply.read(MAX_SIZE + 1)
    if len(data) > MAX_SIZE:
        raise ValueError("larger than any update file")
    return data


def fetch(offers: list[dict[str, Any]], deviceid: str,
          get: Callable[[str, str], bytes] = download,
          skip: frozenset[str] = frozenset(),
          wait: Callable[[float], None] = time.sleep) -> tuple[list[Offered], list[str]]:
    """Each file eWeLink's offers name, fetched whole (ATTEMPTS tries), and what
    went wrong on the way, for the log: only an exception's type, as its text
    could hold a link. `skip`: the SHA-256 digests of files not to fetch (held
    already), by the digest eWeLink gives each."""
    if not deviceid:
        return [], ["no device id to ask with"]
    found: list[Offered] = []
    problems: list[str] = []
    for offer in offers:
        for item in offer.get("binList") or []:
            url, name = str(item.get("downloadUrl", "")), str(item.get("name") or "firmware.bin")
            if str(item.get("digest") or "").lower() in skip:
                continue
            if urllib.parse.urlsplit(url).scheme not in ("http", "https") or len(found) >= MAX_FILES:
                continue
            # eWeLink's digest is the file's SHA-256: one not matching it is
            # cut short or not the file offered, and would stand for an offer
            # no other device sees.
            digest = str(item.get("digest") or "").lower()
            data, why = b"", ""
            for attempt in range(ATTEMPTS):
                if attempt:
                    wait(RETRY_PAUSE)
                try:
                    data = get(url, deviceid)
                except Exception as exc:  # whatever goes wrong: a readout needs none of it
                    data, why = b"", type(exc).__name__
                    continue
                if data and re.fullmatch(r"[0-9a-f]{64}", digest) and hashlib.sha256(data).hexdigest() != digest:
                    data, why = b"", "not the file its digest names"
                    continue
                break
            if why and not data:
                problems.append(f"{name}: {why}, {ATTEMPTS} tries")
                continue
            if data and all(data != kept.data for kept in found):
                found.append(Offered(name.rsplit("/", 1)[-1], str(offer.get("version") or ""), data))
    return found, problems
