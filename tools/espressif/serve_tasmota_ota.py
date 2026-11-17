#!/usr/bin/env python3
"""Serve a validated wrapped ESP app to one owned device.

This is the live, state-changing end of the research workflow.  It remains
blocked by ``RECOVERY_LOCK`` and also requires an explicit no-rollback
acknowledgement.  Building or validating an image never invokes this tool.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import ipaddress
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in (None, ""):  # run as a script: the repository root, for `tools.`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.autoflash import hostsys, kinds, output  # noqa: E402
from tools.autoflash.prompts import ask_secret
from tools.espressif.analyze_vendor_ota import WRAPPER_SIZE, analyze
from tools.espressif import chips
from tools.espressif.build_vendor_ota import ANNOUNCED_VERSION, DEFAULT_MAX_PAYLOAD_SIZE, announce
from tools.ewelink.get_device_key import Account, EwelinkError, given_session, login_session, target_id_from_mdns
from tools.ewelink.ota_push import NOT_SENT, FirmwareServer, deliver, parse_single_range  # noqa: F401  (the server, for its tests)


SAFETY_FLAG = "--i-understand-stock-has-no-automatic-rollback"


def is_bridge(firmware: bytes, chip_id: int) -> bool:
    """Whether a wrapped file holds exactly the bridge firmware.json pins for its chip."""
    chip = chips.by_id(chip_id)
    try:
        app = chip.bridge_app() if chip else None
    except (OSError, ValueError):
        return False
    return app is not None and hashlib.sha256(firmware[WRAPPER_SIZE:]).digest() == hashlib.sha256(app).digest()


def report_failure(exc: BaseException, sent: bool) -> None:
    print(f"ERROR: {exc}", file=sys.stderr)
    if not sent:
        print(NOT_SENT, file=sys.stderr)


def main():
    abandoned = hostsys.parent_gone()   # as it starts: its parent may go before it serves
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--listen-ip", required=True)
    parser.add_argument("--listen-port", type=int, default=8088)
    parser.add_argument("--device-ip", required=True)
    parser.add_argument("--firmware", type=Path, required=True)
    parser.add_argument("--email", required=True,
                        help="the eWeLink account: its email, or its phone number with the country code")
    parser.add_argument("--country-code", default="+44")
    parser.add_argument("--region", default="eu")
    parser.add_argument("--expected-current-version", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--device-id", help="the device's eWeLink id")
    target.add_argument("--mdns-capture", type=Path, help="an eWeLink discovery report naming the device")
    parser.add_argument("--wait", type=float, default=120.0)
    parser.add_argument("--events", type=Path, metavar="FILE",
                        help="the run's events file, for what the send does (autoflash output.EVENTS)")
    parser.add_argument(SAFETY_FLAG, action="store_true")
    parser.add_argument("--readout-model", metavar="NAME",
                        help="send the bridge, wrapped as NAME, to a device of a kind this tool does not "
                             "convert yet, only to read it (tools/autoflash/readout_espressif.py): nothing else is sent so")
    args = parser.parse_args()

    recovery_lock = Path(__file__).resolve().parent / "RECOVERY_LOCK"
    if recovery_lock.exists():
        print(f"Refusing: recovery lock is active at {recovery_lock}", file=sys.stderr)
        return 2
    if not args.i_understand_stock_has_no_automatic_rollback:
        print(f"Refusing to run without {SAFETY_FLAG}", file=sys.stderr)
        return 2
    listen_address = ipaddress.ip_address(args.listen_ip)
    if listen_address.version != 4 or not listen_address.is_private:
        print("Refusing: --listen-ip must be a private IPv4 LAN address", file=sys.stderr)
        return 2
    device_address = ipaddress.ip_address(args.device_ip)
    if device_address.version != 4 or not device_address.is_private:
        print("Refusing: --device-ip must be a private IPv4 LAN address", file=sys.stderr)
        return 2

    try:
        firmware = args.firmware.read_bytes()
    except OSError as exc:
        print(f"Refusing: could not read firmware: {exc}", file=sys.stderr)
        return 2
    try:
        report = analyze(firmware)
    except ValueError as exc:
        print(f"Refusing invalid OTA file: {exc}", file=sys.stderr)
        return 2
    wrapper = report["wrapper"]
    esp = report["esp_image"]
    well_formed = (
        wrapper["magic_valid"] and wrapper["header_reserved_zero"]
        and wrapper["header_crc32_valid"] and wrapper["payload_offset_valid"]
        and wrapper["payload_size_valid"] and wrapper["payload_crc32_valid"]
        and wrapper["record_crc32_valid"] and wrapper["record_reserved_zero"]
        and wrapper["actual_payload_size"] <= DEFAULT_MAX_PAYLOAD_SIZE
        and esp["checksum_valid"] and esp["sha256_valid"] is not False and esp["trailing_bytes"] == 0
    )
    if args.readout_model:
        # Any model, but only the bridge itself: it reads, and hands the device
        # back to its own firmware when switched off and on.
        valid = well_formed and wrapper["model"] == args.readout_model and is_bridge(firmware, esp["chip_id"])
    else:
        valid = well_formed and any(
            esp["chip_id"] == chips.of(kind.chip).chip_id and wrapper["model"] == kind.wrapper_model
            for kind in kinds.ALL if kind.family == "espressif")
    if not valid:
        print("Refusing: firmware failed the complete wrapper/native-image preflight", file=sys.stderr)
        return 2
    firmware = announce(firmware)
    print(f"Firmware: {args.firmware} ({len(firmware):,} bytes), sent as {ANNOUNCED_VERSION}, "
          f"SHA-256 {hashlib.sha256(firmware).hexdigest()}")

    password = os.environ.get("EWELINK_PASSWORD") or ask_secret("eWeLink password: ")

    def check(device: dict[str, Any], model: str) -> None:
        # Only a device of a kind this tool converts, and only a file wrapped
        # for it: another Sonoff on the same account must never be sent it.
        kind = kinds.for_account(device.get("productModel"), model)
        if kind is None and args.readout_model:
            pass  # the bridge alone, checked above, to a device not yet known
        elif kind is None:
            raise EwelinkError(
                f"Device model is {model}, not one this tool converts; refusing"
            )
        elif wrapper["model"] != kind.wrapper_model:
            raise EwelinkError(
                f"The file is wrapped for {wrapper['model']}, not the {kind.name}'s "
                f"{kind.wrapper_model}; refusing"
            )
        version = str((device.get("params") or {}).get("fwVersion"))
        if version != args.expected_current_version:
            raise EwelinkError(
                f"Device reports {version}, expected "
                f"{args.expected_current_version}; refusing"
            )

    def sign_in() -> tuple[str, dict[str, Any]]:
        # The password's copy goes as soon as it has been used: one sign-in.
        nonlocal password
        try:
            return login_session(args.email, password, args.country_code, args.region)
        finally:
            password = ""

    # The run's own sign-in where it handed one over, else one of this tool's.
    delivered = deliver(account=Account(sign_in, held=given_session()), device_id=args.device_id or target_id_from_mdns(args.mdns_capture), firmware=firmware,
                        version=ANNOUNCED_VERSION, listen_ip=args.listen_ip, listen_port=args.listen_port,
                        device_ip=args.device_ip, wait=args.wait, check_device=check,
                        event=functools.partial(output.write_event, args.events) if args.events else None,
                        abandoned=abandoned)
    if delivered.error and not delivered.interrupted:
        report_failure(EwelinkError(delivered.error), delivered.sent)
    elif delivered.interrupted:
        print("\n  Interrupted before the firmware was fully delivered.", file=sys.stderr)
    if not delivered.sent and delivered.error:
        return 1
    print(f"\nUnique firmware bytes served: {delivered.bytes_served:,}/{len(firmware):,}")
    if not delivered.sent or delivered.bytes_served == 0:
        print("Device did not connect; no firmware bytes were transferred.")
    elif not delivered.complete:
        print("WARNING: the device did not request every firmware byte.")
    return 0 if delivered.error is None and delivered.complete else 1


if __name__ == "__main__":
    raise SystemExit(main())
