#!/usr/bin/env python3
"""Derive the stub's account-API view of a device from its own flash dump.

The LAN-control key the tool would normally fetch from the cloud is already in
the flash we booted (self-hosting; see docs/cloud-stub.md), so the stub
reads it straight from there. Emits the JSON `stub_server.py --device-json`
expects. Run inside the instance container against the read-only stock image,
writing to the container's own /run (not /state, which is on your disk), so the
key never leaves that network:

    python3 -I -B device_identity.py /secrets/stock-flash.bin --out /run/autoflash-emu/stub-device.json
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.autoflash import kinds  # noqa: E402
from tools.espressif import ais_records, flashmap  # noqa: E402
from tools.espressif.inspect_stock_nvs import parse  # noqa: E402

MAC = re.compile(r"[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}")


def frame_params(flash: bytes) -> dict:
    """The identity in Sonoff's settings, as frame_params holds it; on the ais
    SDK (the BASICR4's, the MINIR4's), which has none, as its factory record
    holds it (tools/espressif/ais_records.py): its factory key is taken for the
    LAN key too, which no ais device has been seen using yet."""
    partition = flashmap.nvs(flash)
    names, vals = parse(flash[partition.offset:partition.offset + partition.size])
    for (ns, key), value in vals.items():
        if names[ns] == "frame_params" and key == "content":
            return json.loads(value.data.split(b"\x00", 1)[0])
    for (ns, key), value in vals.items():
        if names[ns] == "factory" and key == "info" and ais_records.is_factory(value.data):
            record = value.data
            field = {name: ais_records.text(record, ais_records.FACTORY_FIELDS, name).decode()
                     for name in ais_records.FACTORY_FIELDS}
            return {"deviceid": field["deviceid"], "apikey": field["factory_apikey"],
                    "encrypt_key": field["factory_apikey"], "model": field["device_model"],
                    "uiid": struct.unpack_from("<H", record, ais_records.FACTORY_UIID)[0]}
    raise SystemExit("neither frame_params nor a factory record found in NVS")


def identity(flash: bytes, lan_key_field: str, product_model: str = "", sta_mac: str = "") -> dict:
    fp = frame_params(flash)
    # Ready or not: the stub serves whichever test copy the emulator runs.
    kind = kinds.for_account(product_model, fp.get("model"), among=kinds.EVERY)
    lan_key = fp.get(lan_key_field)
    if not lan_key:
        raise SystemExit(f"frame_params has no `{lan_key_field}` for the LAN key")
    device = {
        "deviceid": fp["deviceid"],
        "apikey": fp["apikey"],
        # The LAN key: the device encrypts its LAN advert with it, which the tool
        # opens for the device's Wi-Fi signal. In this device's dump apikey and
        # encrypt_key hold the same value, and either opens it; --lan-key-field
        # chooses for a device where they differ.
        "devicekey": lan_key,
        "model": fp.get("model", kinds.SONOFF_S60.stock_model),
        "uiid": fp.get("uiid", 190),
        "name": f"{kind.short if kind else 'Device'} (stub)",
        # Not here: the firmware version, which the stub reports as the device
        # gives it.
    }
    # The product model is eWeLink's name for the device, given when it was
    # paired, and is not in the dump: only passed in, from what eWeLink said.
    if product_model:
        if not (kind and kind.product_of(product_model)):
            raise SystemExit(f"{product_model!r} is not a product model of this device")
        device["productModel"] = product_model
    # Its Wi-Fi MAC, which eWeLink's record of it carries (params.staMac) and
    # the tool first looks for it on the network by. The device never says it to
    # the stub, so it is the MAC of the device being run, passed in.
    if sta_mac:
        if not MAC.fullmatch(sta_mac):
            raise SystemExit(f"{sta_mac!r} is not a MAC address")
        device["staMac"] = sta_mac.replace("-", ":").upper()
    return device


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("flash", type=Path, help="whole-flash dump to read")
    parser.add_argument("--lan-key-field", choices=("apikey", "encrypt_key"),
                        default="apikey", help="NVS field holding the LAN key")
    parser.add_argument("--product-model", default="",
                        help="eWeLink's product model for this device (S60TPG, say), "
                             "which the dump does not record")
    parser.add_argument("--sta-mac", default="",
                        help="the device's Wi-Fi MAC, which eWeLink's record of it carries")
    parser.add_argument("--out", type=Path, default=None, help="write here (0600) instead of stdout")
    args = parser.parse_args(argv)
    data = identity(args.flash.read_bytes(), args.lan_key_field, args.product_model.strip().upper(),
                    args.sta_mac.strip())
    text = json.dumps(data, indent=2)
    if args.out:
        args.out.write_text(text)
        args.out.chmod(0o600)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
