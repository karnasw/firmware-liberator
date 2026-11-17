#!/usr/bin/env python3
"""Fetch one owned device's LAN key without storing eWeLink credentials."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, TypeVar

try:
    from tools.autoflash.hostsys import urlopen
    from tools.autoflash.prompts import ask_secret
except ModuleNotFoundError:  # running from inside tools/
    from autoflash.hostsys import urlopen
    from autoflash.prompts import ask_secret


# Public client credentials used by the open-source SonoffLAN integration at
# commit 3470f9133d4a3dcee520242f0da07114ad9d6bf4. These are application
# credentials, not user credentials. They may be rotated by eWeLink.
APP_ID = "R8Oq3y0eSZSYdKccHlrQzT1ACCOUT9Gv"
APP_SECRET = "1ve5Qk9GXfUhKAn1svnKwpAlxXkMarru"
API = {
    "cn": "https://cn-apia.coolkit.cn",
    "as": "https://as-apia.coolkit.cc",
    "us": "https://us-apia.coolkit.cc",
    "eu": "https://eu-apia.coolkit.cc",
}

# A --stub-cloud run points every region at the local cloud stub instead of
# coolkit (self-hosting; see docs/cloud-stub.md). The value is a full
# base URL, e.g. https://apia.autoflash-stub.test; trust it by setting SSL_CERT_FILE
# to a bundle holding the stub's CA. Unset in a normal run: coolkit is used.
_STUB_API_BASE = os.environ.get("AUTOFLASH_EWELINK_API_BASE", "").rstrip("/")
if _STUB_API_BASE:
    API = {region: _STUB_API_BASE for region in API}


def dispatch_server(region: str) -> tuple[str, int]:
    """Where a device of an account in `region` asks which server to connect to,
    as a paired device keeps it (frame_params' dis_server and dis_port). A stand-in
    for eWeLink (AUTOFLASH_EWELINK_API_BASE) answers devices at its disp. name."""
    if _STUB_API_BASE:
        host = urllib.parse.urlsplit(_STUB_API_BASE).hostname or ""
        return "disp." + host.split(".", 1)[-1], 443
    return f"{region}-dispd.coolkit.{'cn' if region == 'cn' else 'cc'}", 443


# Set by the fault suite on the real cloud (wait_for_turn). Unset in a normal run.
_TURN_FILE = os.environ.get("AUTOFLASH_EMULATOR_CLOUD_TURN", "")


# A certificate this computer does not trust, from a site that has a good one -
# even after Windows was asked for the root it needs (hostsys.urlopen): almost
# always something in between reading encrypted connections.
INSPECTED = ("{what} certificate was not trusted - security software, or a work or "
             "school network, that inspects encrypted connections does this. Try another "
             "network, or pause that software while you convert the device")


class EwelinkError(RuntimeError):
    pass


class SessionEnded(EwelinkError):
    """eWeLink no longer takes the access token: another sign-in to the
    account ended it (a helper tool's, or the app's), or it expired."""


# What eWeLink answers a request with an access token it no longer takes.
TOKEN_REFUSED = (401, 402)


# Every country calling code (ITU-T E.164). None is the start of another, so a
# phone number's is the one its digits begin with.
CALLING_CODES = frozenset("""
    1 7 20 27 30 31 32 33 34 36 39 40 41 43 44 45 46 47 48 49 51 52 53 54 55 56
    57 58 60 61 62 63 64 65 66 81 82 84 86 90 91 92 93 94 95 98
    211 212 213 216 218 220 221 222 223 224 225 226 227 228 229 230 231 232 233
    234 235 236 237 238 239 240 241 242 243 244 245 246 247 248 249 250 251 252
    253 254 255 256 257 258 260 261 262 263 264 265 266 267 268 269 290 291 297
    298 299 350 351 352 353 354 355 356 357 358 359 370 371 372 373 374 375 376
    377 378 379 380 381 382 383 385 386 387 389 420 421 423 500 501 502 503 504
    505 506 507 508 509 590 591 592 593 594 595 596 597 598 599 670 672 673 674
    675 676 677 678 679 680 681 682 683 685 686 687 688 689 690 691 692 850 852
    853 855 856 880 886 960 961 962 963 964 965 966 967 968 970 971 972 973 974
    975 976 977 992 993 994 995 996 998
""".split())


def calling_code(number: str) -> str | None:
    """The country calling code a +-prefixed phone number begins with."""
    for length in (1, 2, 3):
        if number[1:1 + length] in CALLING_CODES:
            return "+" + number[1:1 + length]
    return None


# An email address whose domain ends in a top-level domain: letters, or an
# internationalised one's xn-- form.
EMAIL = re.compile(r"[^@\s]+@(?:[^\W_](?:[\w-]*[^\W_])?\.)+(?:[^\W\d_]{2,}|xn--[a-z0-9-]+)", re.I)


def is_email(text: str) -> bool:
    return EMAIL.fullmatch(text.strip()) is not None


def phone_number(text: str) -> str | None:
    """`text` as eWeLink keeps a phone-number account (+447700900123), or None
    when it is not one: a number written with its country code, as +44 or
    0044, spaces, dashes, dots and brackets allowed."""
    number = re.sub(r"[\s().-]", "", text)
    if number.startswith("00"):
        number = "+" + number[2:]
    if re.fullmatch(r"\+[1-9]\d{6,14}", number) and calling_code(number):
        return number
    return None


def account_name(text: str) -> str:
    """An eWeLink account as given - an email, or a phone number in any of the
    ways phone_number() takes - written the one way it is signed in with."""
    return phone_number(text) or text.strip()


def signed_login_body(account: str, password: str, country_code: str) -> tuple[bytes, dict[str, str]]:
    """The sign-in for an account that is an email, or a phone number with its
    country code; a phone number's own country code is sent in place of
    `country_code`, which only hints at the region for an email."""
    # Preserve insertion order and default json.dumps separators to match the
    # request body covered by the HMAC signature.
    if "@" in account:
        payload = {"password": password, "countryCode": country_code, "email": account.strip()}
    else:
        phone = phone_number(account)
        if phone is None:
            raise EwelinkError("the eWeLink account is neither an email address nor a phone "
                               "number with its country code (as +44 7700 900123)")
        payload = {"password": password, "countryCode": calling_code(phone), "phoneNumber": phone}
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(APP_SECRET.encode("utf-8"), body, hashlib.sha256).digest()
    headers = {
        "Authorization": "Sign " + base64.b64encode(signature).decode("ascii"),
        "Content-Type": "application/json",
        "X-CK-Appid": APP_ID,
        "User-Agent": "autoflash-research/0.1",
    }
    return body, headers


def api_json(url: str, *, body: bytes | None = None, headers: dict[str, str] | None = None, timeout: float = 10.0) -> dict[str, Any]:
    request = urllib.request.Request(url, data=body, headers=headers or {}, method="POST" if body is not None else "GET")
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        raise EwelinkError(f"eWeLink HTTP error {exc.code}") from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLCertVerificationError):
            raise EwelinkError(INSPECTED.format(what="eWeLink's")) from None
        raise EwelinkError(f"eWeLink connection failed: {exc.reason}") from None
    except json.JSONDecodeError:
        raise EwelinkError("eWeLink returned a non-JSON response") from None


def wait_for_turn() -> None:
    """In the fault suite, whose emulated devices are all one eWeLink device, a
    sign-in ends another run's and can reach another's device: so a run leaves
    this file and signs in once the suite has removed it, giving it the turn."""
    if _TURN_FILE:
        turn = Path(_TURN_FILE)
        turn.parent.mkdir(parents=True, exist_ok=True)
        turn.touch()
        while turn.exists():
            time.sleep(0.5)


def login_session(account: str, password: str, country_code: str, region: str) -> tuple[str, dict[str, Any]]:
    wait_for_turn()
    body, headers = signed_login_body(account, password, country_code)
    response = api_json(API[region] + "/v2/user/login", body=body, headers=headers)
    if response.get("error") == 10004:
        region = response.get("data", {}).get("region", "")
        if region not in API:
            raise EwelinkError("eWeLink redirected to an unknown region")
        response = api_json(API[region] + "/v2/user/login", body=body, headers=headers)
    if response.get("error") != 0:
        raise EwelinkError("eWeLink login failed: " + str(response.get("msg", "unknown error")))
    token = response.get("data", {}).get("at")
    if not token:
        raise EwelinkError("eWeLink login response did not contain an access token")
    return region, response["data"]


T = TypeVar("T")


class Account:
    """One sign-in to an eWeLink account, made when first needed and used
    for everything after: each sign-in ends the one before it, and eWeLink
    turns away an account signing in too often. `sign_in` gives the region
    and session (login_session's); it is called again only once eWeLink has
    ended the last."""

    def __init__(self, sign_in: Callable[[], tuple[str, dict[str, Any]]],
                 held: tuple[str, dict[str, Any]] | None = None):
        self._sign_in = sign_in
        self.held = held

    def session(self) -> tuple[str, dict[str, Any]]:
        if self.held is None:
            self.held = self._sign_in()
        return self.held

    def use(self, work: Callable[[str, dict[str, Any]], T]) -> T:
        """`work(region, session)`, again with a new sign-in if eWeLink had
        ended this one."""
        try:
            return work(*self.session())
        except SessionEnded:
            self.held = None
            return work(*self.session())

    def end(self) -> None:
        self.held = None


# A sign-in handed to a helper tool (query_ota.py, serve_tasmota_ota.py) by the
# run that started it, as EWELINK_PASSWORD is: a sign-in of the helper's own
# would end the run's. Only what the helper uses of it: never on a command line.
SESSION_ENV = "EWELINK_SESSION"


def session_env(region: str, session: dict[str, Any]) -> str:
    return json.dumps({"region": region, "at": session["at"],
                       "user": {"apikey": (session.get("user") or {}).get("apikey", "")}})


def given_session() -> tuple[str, dict[str, Any]] | None:
    """The sign-in the run that started this helper handed it, if any."""
    try:
        given = json.loads(os.environ.get(SESSION_ENV) or "null")
        if given["region"] in API and isinstance(given["at"], str) and given["at"]:
            return given["region"], {"at": given["at"], "user": dict(given.get("user") or {})}
    except (ValueError, TypeError, KeyError):
        pass
    return None


def login(account: str, password: str, country_code: str, region: str) -> tuple[str, str]:
    region, session = login_session(account, password, country_code, region)
    return region, session["at"]


def get_devices(region: str, token: str) -> list[dict[str, Any]]:
    query = urllib.parse.urlencode({"num": 0})
    response = api_json(
        API[region] + "/v2/device/thing?" + query,
        headers={
            "Authorization": "Bearer " + token,
            "User-Agent": "autoflash-research/0.1",
        },
    )
    if response.get("error") in TOKEN_REFUSED:
        raise SessionEnded("eWeLink ended the sign-in: " + str(response.get("msg", "token refused")))
    if response.get("error") != 0:
        raise EwelinkError("Could not retrieve owned devices: " + str(response.get("msg", "unknown error")))
    things = response.get("data", {}).get("thingList", [])
    return [item["itemData"] for item in things if "deviceid" in item.get("itemData", {})]


def target_id_from_mdns(path: Path) -> str:
    capture = json.loads(path.read_text(encoding="utf-8"))
    ids: set[str] = set()
    for packet in capture.get("packets", []):
        for record in packet.get("records", []):
            if record.get("type") != "TXT":
                continue
            for value in record.get("value", []):
                if value.startswith("id="):
                    ids.add(value[3:])
    if len(ids) != 1:
        raise EwelinkError(f"Expected one device ID in mDNS capture, found {len(ids)}")
    return ids.pop()


def select_device(devices: list[dict[str, Any]], device_id: str) -> dict[str, Any]:
    matches = [device for device in devices if device.get("deviceid") == device_id]
    if len(matches) != 1:
        raise EwelinkError(f"Expected one matching owned device, found {len(matches)}")
    device = matches[0]
    if not device.get("devicekey"):
        raise EwelinkError("Matching device did not include a devicekey")
    return device


def write_secret(path: Path, device: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = {
        "deviceid": device["deviceid"],
        "devicekey": device["devicekey"],
        "name": device.get("name"),
        "productModel": device.get("productModel"),
        "uiid": device.get("extra", {}).get("uiid"),
    }
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, indent=2, sort_keys=True)
            stream.write("\n")
    finally:
        os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="the eWeLink account: its email, or its phone number "
                        "with the country code; prompted locally when omitted")
    parser.add_argument("--country-code", default="+44")
    parser.add_argument("--region", choices=sorted(API), default="eu")
    parser.add_argument("--device-id", help="defaults to the ID in the saved mDNS capture")
    parser.add_argument("--mdns-capture", type=Path, default=Path("captures/mdns-1.1.1.json"))
    parser.add_argument("--output", type=Path, default=Path("captures/device-key.json"))
    args = parser.parse_args()

    email = args.email or input("eWeLink email or phone number: ").strip()
    password = os.environ.get("EWELINK_PASSWORD") or ask_secret(
        "eWeLink password (not stored): "
    )
    if not email or not password:
        print("The account and password are required.", file=sys.stderr)
        return 2

    try:
        device_id = args.device_id or target_id_from_mdns(args.mdns_capture)
        region, token = login(email, password, args.country_code, args.region)
        device = select_device(get_devices(region, token), device_id)
        write_secret(args.output, device)
    except EwelinkError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""  # Minimize the lifetime of the credential reference.

    print(f"Saved the matching device key to {args.output} with mode 0600.")
    print("The account password and cloud access token were not stored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
