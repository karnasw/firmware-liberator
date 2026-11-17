#!/usr/bin/env python3
"""Query genuine eWeLink OTA metadata without triggering an upgrade."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: the repository root, for `tools.`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.autoflash.prompts import ask_secret  # noqa: E402
from tools.ewelink.get_device_key import (
    API,
    TOKEN_REFUSED,
    Account,
    EwelinkError,
    SessionEnded,
    api_json,
    get_devices,
    given_session,
    login_session,
    select_device,
    target_id_from_mdns,
)


VERSION_BEHIND_ALL = "0.0.1"  # older than any firmware, so eWeLink offers the latest


def ota_identity(device: dict[str, Any]) -> dict[str, str]:
    model = (
        device.get("extra", {}).get("model")
        or device.get("model")
        or device.get("productModel")
    )
    version = device.get("params", {}).get("fwVersion")
    if not model or not version:
        raise EwelinkError(
            "Cloud device record did not contain OTA model and firmware version - "
            "check eWeLink shows the device online, and that it is allowed internet access"
        )
    return {"deviceid": device["deviceid"], "model": model, "version": version}


def query_ota(region: str, token: str, identity: dict[str, str]) -> dict[str, Any]:
    body = json.dumps({"deviceInfoList": [identity]}).encode("utf-8")
    response = api_json(
        API[region] + "/v2/device/ota/query",
        body=body,
        headers={
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "User-Agent": "autoflash-research/0.1",
        },
    )
    if response.get("error") in TOKEN_REFUSED:
        raise SessionEnded("eWeLink ended the sign-in: " + str(response.get("msg", "token refused")))
    if response.get("error") != 0:
        raise EwelinkError("OTA metadata query failed: " + str(response.get("msg", "unknown error")))
    return response


def latest_offer(region: str, token: str, identity: dict[str, str]) -> dict[str, Any]:
    """eWeLink's offer of the latest firmware, which it makes only to a device it
    takes to be behind: for reading the firmware name from the file, and not an
    update on offer to this device. Asked with the device's own id alone, as a
    made-up one is offered an unrelated file. Failing costs a run nothing, so it
    is only noted: {"offers": [...]} or {"problem": ...}."""
    try:
        reply = query_ota(region, token, {**identity, "version": VERSION_BEHIND_ALL})
        return {"offers": list(reply["data"]["otaInfoList"])}
    except SessionEnded:
        raise  # signed in to again, and asked again (get_device_key.Account)
    except Exception as exc:  # whatever goes wrong: a run needs none of this
        return {"problem": type(exc).__name__}


def ota_endpoints(ota_list: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Extract only non-sensitive host/port pairs (informational; the
    conversion delivers firmware directly and does not redirect these)."""
    endpoints: set[tuple[str, int]] = set()
    for ota in ota_list:
        for item in ota.get("binList", []):
            parsed = urllib.parse.urlparse(str(item.get("downloadUrl", "")))
            if not parsed.hostname or parsed.scheme not in {"http", "https"}:
                continue
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            endpoints.add((parsed.hostname, port))
    return sorted(endpoints)


def private_json(path: Path, value: Any) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
    finally:
        os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", help="the eWeLink account: its email, or its phone number "
                        "with the country code; prompted locally when omitted")
    parser.add_argument("--country-code", default="+44")
    parser.add_argument("--region", choices=sorted(API), default="eu")
    parser.add_argument("--device-id")
    parser.add_argument("--mdns-capture", type=Path, default=Path("captures/mdns-1.1.1.json"))
    parser.add_argument("--output", type=Path, default=Path("captures/ota-metadata-1.2.0.json"))
    parser.add_argument("--latest-offer", action="store_true",
                        help="also save the offer of the latest firmware, to read its firmware name from")
    args = parser.parse_args()

    email = args.email or input("eWeLink email or phone number: ").strip()
    password = os.environ.get("EWELINK_PASSWORD") or ask_secret(
        "eWeLink password (not stored): "
    )
    if not email or not password:
        print("The account and password are required.", file=sys.stderr)
        return 2
    def ask(region: str, session: dict[str, Any]) -> dict[str, Any]:
        token = session["at"]
        device = select_device(get_devices(region, token), device_id)
        identity = ota_identity(device)
        response = query_ota(region, token, identity)
        # productModel names the region: S60TPG (UK) or S60TPF (Europe).
        saved = {"query": identity, "product": device.get("productModel"), "response": response}
        if args.latest_offer:
            offered = response.get("data", {}).get("otaInfoList")
            saved["latest_offer"] = {"offers": offered} if offered else latest_offer(region, token, identity)
        return saved

    try:
        device_id = args.device_id or target_id_from_mdns(args.mdns_capture)
        # The run's own sign-in where it handed one over, else one of this tool's.
        account = Account(lambda: login_session(email, password, args.country_code, args.region),
                          held=given_session())
        saved = account.use(ask)
        identity, response = saved["query"], saved["response"]
        private_json(args.output, saved)
    except EwelinkError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    finally:
        password = ""

    ota_list = response.get("data", {}).get("otaInfoList", [])
    print(f"Saved OTA metadata to {args.output} with mode 0600.")
    print(
        f"Current device firmware: {identity['model']} "
        f"version {identity['version']}"
    )
    print(f"Available OTA records: {len(ota_list)}")
    for ota in ota_list:
        print(f"Version: {ota.get('version')} | files: {len(ota.get('binList', []))}")
    endpoints = ota_endpoints(ota_list)
    if endpoints:
        print("Vendor OTA endpoint(s) (informational - not used; we deliver directly):")
        for host, port in endpoints:
            print(f"  Vendor OTA host: {host} | port: {port} | protocol: TCP")
    else:
        print("Vendor OTA endpoint: not present in OTA metadata")
    print("No upgrade command was sent.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
