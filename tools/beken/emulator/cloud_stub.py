#!/usr/bin/env python3
"""The emulated Beken device's eWeLink: the local stub (tools/ewelink/stub_cloud),
served on its router's address for the device and for the tool alike, as
beken-emu.sh device runs it (device-net.sh).

    python3 -I -B cloud_stub.py --state DIR --bind ADDRESS --flash DIR/flash.bin

Dispatch is plaintext on :80, as the S61's factory dispatch URL is; the IoT
WebSocket and the account API are TLS on :443, under a CA of our own made once
into DIR (ca.crt, with the leaf beside it), which the device is made to trust
(cloud_online.StockHooks' trust-anchor swap) and the tool is given
(SSL_CERT_FILE). The device's identity is what it says as it registers; its
hardware address, which eWeLink's record carries, is read from its flash.

Its model is what the firmware it first started holds, not what it says as it
registers: that comes from its sealed factory data, so a flash rebuilt on the
S61's settings for another model (rebuild_dump.py) says it is an S61.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import ssl
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parent)]   # the emulator's modules, and the Beken tools
sys.path.insert(0, str(HERE.parents[1] / "ewelink" / "stub_cloud"))
import backup  # noqa: E402
import cloud_online  # noqa: E402  (its compact JSON and certificates; needs the emulator image)
import fota_wrap  # noqa: E402
import stock_app  # noqa: E402
import stock_points  # noqa: E402
import stub_server as ss  # noqa: E402

sys.path.insert(0, str(HERE.parents[2]))
from tools.autoflash import kinds  # noqa: E402

# The S61's region of its own data, where the factory record keeps its MAC.
REGION = 0x3E0000
HEARTBEAT = 145


def certificates(state: Path, address: str) -> tuple[Path, Path]:
    """The leaf and its key in `state`, made with their CA for `address` - and
    made again for another (a device started on another network): the device
    checks the leaf names the address it dialled."""
    leaf, key, made_for = state / "leaf.crt", state / "leaf.key", state / "certs-for"
    try:
        current = made_for.read_text(encoding="utf-8").strip() == address
    except OSError:
        current = False
    if not (current and leaf.exists() and key.exists() and (state / "ca.crt").exists()):
        made = state / "certs.part"
        made.mkdir(exist_ok=True)
        cloud_online._make_certs(made, address)
        for name in ("ca.crt", "leaf.crt", "leaf.key"):
            os.replace(made / name, state / name)
        shutil.rmtree(made)
        made_for.write_text(address + "\n", encoding="utf-8")
    return leaf, key


def model_in(app: bytes) -> str:
    """What a stock app calls itself, if it holds that once."""
    found = set(re.findall(rb"SN-BK[0-9]+-[A-Z0-9]+-[0-9]+", app))
    return found.pop().decode("ascii") if len(found) == 1 else ""


def device(flash: Path, state: Path) -> dict:
    """The account record's part the device's register does not give. Its model is
    kept in `state` the first time, from the Sonoff firmware it started on: the
    stub may next start with another firmware on the flash."""
    kept = state / "model"
    data = flash.read_bytes() if flash.exists() else b""
    try:
        model = kept.read_text(encoding="utf-8").strip()
    except OSError:
        try:
            model = model_in(stock_points.app_of_flash(data))
        except ValueError:
            model = ""
        kept.write_text(model + "\n", encoding="utf-8")
    kind = next((kind for kind in kinds.EVERY if model and kind.stock_model == model), kinds.SONOFF_S61)
    record = {"model": kind.stock_model, "productModel": kind.example_product_model, "name": kind.name}
    try:
        record["staMac"] = backup.factory_mac(data[REGION:REGION + 0x1000])
    except ValueError:
        pass
    return record


def offered(flash: Path, state: Path, model: str) -> SimpleNamespace | None:
    """The update file the stub offers the device, as eWeLink offers a real one
    its official file (query_ota.latest_offer): the device's own stock app as the
    RBL its bootloader applies, in the FOTA container its updater takes, named
    as its kind's are. A readout reverts with it, a run back to Sonoff with no
    backup fetches it. Made once into `state`, from the Sonoff firmware the device
    first started on: it may next start with another firmware on the flash.
    None where the flash holds no stock app this can read."""
    kept, kept_version = state / "offered.fota", state / "offered.version"
    if not (kept.exists() and kept_version.exists()):
        kind = next((kind for kind in kinds.EVERY if model and kind.stock_model == model), None)
        try:
            image, version = stock_app.stock_rbl(flash.read_bytes())
        except (OSError, ValueError):
            return None
        if kind is None:
            return None
        kept.write_bytes(fota_wrap.wrap(image, name=kind.wrapper_model, version=version))
        kept_version.write_text(version + "\n", encoding="utf-8")
    return SimpleNamespace(version=kept_version.read_text(encoding="utf-8").strip(), data=kept.read_bytes())


def remember(stub: ss.Stub, path: Path, model: str = "") -> None:
    """What devices have said of themselves, kept in `path` across restarts: an
    eWeLink account keeps its devices while they run another firmware. Their
    model is the record's `model`, whatever they say."""
    try:
        stub.reported.update(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    heard = stub.heard

    def heard_and_kept(message: dict) -> None:
        heard(message)
        for seen in stub.reported.values():
            if model and seen.get("model"):
                seen["model"] = model
        part = path.with_suffix(".part")
        part.write_text(json.dumps(stub.reported), encoding="utf-8")
        os.replace(part, path)
    stub.heard = heard_and_kept


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--bind", required=True)
    parser.add_argument("--flash", type=Path, required=True)
    args = parser.parse_args(argv)
    leaf, key = certificates(args.state, args.bind)
    record = device(args.flash, args.state)
    update = offered(args.flash, args.state, record["model"])
    stub = ss.Handler.stub = ss.Stub(args.bind, 443, HEARTBEAT, record, update)
    ss.log(f"offering {update.version} as the latest firmware" if update else "offering no firmware")
    remember(stub, args.state / "stub-reported.json", record["model"])
    # The S61 parses the port as a string; the tool takes either.
    stub.dispatch_response = lambda: {"error": 0, "reason": "", "IP": args.bind, "domain": args.bind,
                                      "port": "443"}
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(str(leaf), str(key))
    servers = [ss.PlainServer((args.bind, 80), ss.Handler), ss.TLSServer((args.bind, 443), ss.Handler, context)]
    ss.log(f"stub listening on {args.bind}:80 (dispatch) and :443 (TLS)")
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, daemon=True).start()
    servers[0].serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
