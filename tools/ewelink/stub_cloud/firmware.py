#!/usr/bin/env python3
"""The update file the stub offers a device, as eWeLink offers the latest
firmware: the Sonoff app of the device's own stock dump, made to run under QEMU
and trust the stub as every Sonoff app the emulator runs is (patch_stock.py),
in Sonoff's update wrapper. A run with no backup fetches it to put the device
back on Sonoff's firmware, as it fetches the real file from eWeLink.

emu.sh makes it on your computer, where patch_stock has what it needs, into
the instance's state, and the stub reads it from there (read()):

    python -I tools/ewelink/stub_cloud/firmware.py STOCK_FLASH OUT --trust-ca CA [--product DUALR3]
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from tools.autoflash import kinds  # noqa: E402
from tools.espressif import build_vendor_ota, flashmap  # noqa: E402
from tools.espressif.emulator import patch_stock  # noqa: E402
from tools.ewelink.stub_cloud.device_identity import frame_params  # noqa: E402

FILE_NAME = "user1.bin"


@dataclass(frozen=True)
class Update:
    version: str
    data: bytes


def version_tuple(version: str) -> tuple[int, ...]:
    try:
        return tuple(int(part) for part in version.split("."))
    except ValueError:
        return ()


def kind_of(model: str | None, product: str | None) -> kinds.Kind | None:
    """The kind of a device whose firmware calls itself `model`, eWeLink's
    product model for it `product`: by the product model first, as the tool
    takes it, for a kind whose firmware's name is not its own (the DualR3's,
    E32-2SW-P0); else by that name."""
    return (next((kind for kind in kinds.EVERY if kind.product_of(product)), None)
            or next((kind for kind in kinds.EVERY if model and kind.stock_model == model), None))


def update_of(flash: bytes, ca_pem: bytes | None, product: str | None = None) -> Update:
    """The update file for the device whose stock flash this is, eWeLink's
    product model for it `product` where known."""
    patched = patch_stock.patch(flash, ca_pem)
    slot = next(slot for slot in flashmap.app_slots(patched)
                if patch_stock.holds_sonoff_firmware(patched[slot.offset:slot.offset + slot.size]))
    app = patched[slot.offset:slot.offset + slot.size]
    length = build_vendor_ota.parse_esp_image(app)["parsed_image_size"]
    app = app[:length]
    kind = kind_of(frame_params(flash).get("model"), product)
    if kind is None:
        raise ValueError("the dump's model is not a kind this tool knows")
    version = build_vendor_ota.app_version(app) or "1.0.0"
    return Update(version, build_vendor_ota.build_wrapper(app, version=version, model=kind.wrapper_model))



def read(path: Path) -> Update | None:
    """The file emu.sh made, or None where there is none."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    version = data[2:10].split(b"\0", 1)[0].decode("ascii", "replace")
    return Update(version, data) if version_tuple(version) else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("flash", type=Path, help="the device's stock dump")
    parser.add_argument("output", type=Path)
    parser.add_argument("--trust-ca", type=Path, default=None, help="the stub's CA, for the firmware to trust")
    parser.add_argument("--product", default="", help="eWeLink's product model for the device, e.g. DUALR3")
    args = parser.parse_args(argv)
    update = update_of(args.flash.read_bytes(), args.trust_ca.read_bytes() if args.trust_ca else None, args.product)
    args.output.write_bytes(update.data)
    args.output.chmod(0o644)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
