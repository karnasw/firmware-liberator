#!/usr/bin/env python3
"""The firmware each device ends on, converted on its emulator: the same check
for every device the tool converts, so a new one has it by being added.

    python -I tools/repo/emulator_check.py list [--changed-since COMMIT]
        as JSON, the devices to check and the emulator image each needs: those a
        change since COMMIT could affect, or every one that has something to run
        from (NOTHING_TO_RUN names those that have not)
    .venv/bin/python -I tools/repo/emulator_check.py DEVICE [--instances 1,2]
        its check: each final firmware installed, then each device taken to the
        other, two ways side by side (a way goes on as soon as its own run is
        done), on the two emulators numbered: two devices checked at once on one
        computer each need their own. Nothing here needs a secret or an
        account: an S60 starts from the published public devices, any other
        Espressif device from its public dump on the cloud stub, an S61 from
        upstream's public dump, and a Beken device with no public dump from a
        flash rebuilt from the S61's and its official update file
        (rebuilt_flash).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import kinds  # noqa: E402
from tools.repo import emulator_stats  # noqa: E402

# Each family's emulator: the image a device's job waits for, as its workflow
# publishes it, and the variable that says where the tool pulls it from.
EMULATORS = {
    "espressif": {"image_workflow": "emulator-image.yml", "image_tag": "tools/espressif/emulator/image.sh tag",
                  "image_name": "autoflash-emu", "published_variable": "AUTOFLASH_EMU_PUBLISHED"},
    "beken": {"image_workflow": "beken-emulator-image.yml", "image_tag": "tools/beken/emulator/image.sh tag",
              "image_name": "autoflash-beken-emu", "published_variable": "AUTOFLASH_BEKEN_EMU_PUBLISHED"},
}
# What every device's conversion runs on, whichever changed.
SHARED = ("tools/autoflash/", "tools/ewelink/", "tools/repo/emulator_check.py", "artifacts/",
          ".github/workflows/emulator-check.yml")
LOGS = ROOT / "captures/emulation/device-check"
# Devices with nothing for an emulator to run: no public dump, nor a flash to
# rebuild from an official update file. A device is here by decision:
# tests/repo/test_emulator_check.py fails on one that has neither something to
# run from nor a place here.
NOTHING_TO_RUN: set[str] = set()
# The Beken device whose public dump gives a rebuilt flash its bootloader,
# radio data and settings (rebuilt_flash).
REBUILT_FROM = "sonoff-s61"
# That dump, as upstream BekenEmulator publishes it and beken-emu.sh dump
# fetches it into folders.FIRMWARES, by its name and SHA-256.
S61_DUMP = ("Sonoff_S61s_EUPlug_WBBK_01P_V1.3.bin", "0f858068b7b6c4107119e6afdf7a6eb05349234e726028602dbcf63b92ad6df3")
# An Espressif device's two ways, each a final firmware then the other.
ESPRESSIF_WAYS = (("tasmota", "esphome"), ("esphome", "tasmota"))


def family_of(kind: kinds.Kind) -> str:
    return "beken" if kind.chip.startswith("bk") else "espressif"


def affected(kind: kinds.Kind, changed: list[str]) -> bool:
    """Whether a change to these paths could change how `kind` converts."""
    family = family_of(kind)
    # A chip's folder holds what every device on it shares: its mover.
    own = (f"devices/{kind.key}/", f"devices/{kind.chip}/", f"tools/{family}/", *SHARED)
    return any(path.startswith(own) for path in changed)


def changed_since(commit: str) -> list[str] | None:
    """The paths changed since `commit`; None where git cannot say, as for a
    first push, whose `before` is all zeros."""
    found = subprocess.run(["git", "-C", str(ROOT), "diff", "--name-only", commit, "HEAD"],
                           capture_output=True, text=True, check=False)
    return found.stdout.split() if found.returncode == 0 else None


def jobs(changed: list[str] | None) -> list[dict[str, str]]:
    return [{"device": kind.key, "family": family_of(kind), **EMULATORS[family_of(kind)]}
            for kind in kinds.READY if kind.key not in NOTHING_TO_RUN and (changed is None or affected(kind, changed))]


def check_public_dump(kind: kinds.Kind, instances: list[int]) -> int:
    """An Espressif device from its public dump, on the cloud stub."""
    from tools.espressif.emulator import public_dump, public_devices

    public_dump.make(kind.key)
    dump = public_dump.DUMPS.get(kind.key) or public_dump.UPDATES[kind.key]
    stub = ("--stub-cloud", f"--device={dump.mac}")

    folder = LOGS / kind.key
    emulator_stats.began(folder, family=kind.family, suite="check", device=kind.key,
                         total=sum(map(len, ESPRESSIF_WAYS)), planned=sum(map(len, ESPRESSIF_WAYS)), cloud="stub",
                         workers=len(instances))

    def way(instance: int, firmwares: tuple[str, ...]) -> int:
        saved = None
        for firmware in firmwares:
            keep = f"check-{kind.key}-{instance}-{firmware}"
            log = folder / f"{instance}-{firmware}.log"
            began = time.time()
            converted, said = public_devices.run(instance, saved, firmware, keep, log,
                                               extra=(*stub, *(() if saved else ("--fresh",))))
            start = f"on {saved.rsplit('-', 1)[1]}" if saved else "from Sonoff's"
            print(f"{'PASS' if converted else 'FAIL'}  {kind.key} {start} to {firmware}: {said}", flush=True)
            emulator_stats.check_run(folder, family=kind.family, device=kind.key,
                                     route=f"{saved.rsplit('-', 1)[1] if saved else 'sonoff'}>{firmware}",
                                     passed=converted, seconds=round(time.time() - began), cloud="stub")
            if not converted:
                return 1
            saved = keep
        return 0

    try:
        return sum(public_devices.side_by_side([lambda i=instance, w=firmwares: way(i, w) for firmwares, instance
                                              in zip(ESPRESSIF_WAYS, instances)]))
    finally:
        print(emulator_stats.send(folder), flush=True)
        for instance in instances:
            for firmware in ("tasmota", "esphome"):
                subprocess.run(["rm", "-rf", str(public_devices.SAVED / f"check-{kind.key}-{instance}-{firmware}")],
                               check=False)


def rebuilt(kind: kinds.Kind) -> bool:
    """Whether a Beken device's check starts from a rebuilt flash: any but the
    S61, whose public dump it is rebuilt from."""
    return kind.family == "beken" and kind.key != REBUILT_FROM


def rebuilt_flash(kind: kinds.Kind) -> Path:
    """The whole stock flash of a device with no public dump, as rebuild_dump.py
    makes it from the public S61 dump (beken-emu.sh dump fetched it) and the
    device's official update file (fetched here), each checked against its
    pinned hash. Both are Sonoff's, and so is the flash made from them: it is
    kept in captures/ only. Made again at each check, in a second or two, so
    it is never older than what it is made from."""
    import hashlib

    from tools.autoflash.routes import public_copies
    from tools.beken import rebuild_dump
    from tools.beken.emulator import folders

    name, digest = S61_DUMP
    dump = (folders.FIRMWARES / name).read_bytes()
    if hashlib.sha256(dump).hexdigest() != digest:
        raise SystemExit(f"{folders.FIRMWARES / name} is not the public dump this tool pins")
    try:
        flash = rebuild_dump.rebuild(dump, public_copies.fetch(kind))
    except public_copies.Unavailable as exc:
        raise SystemExit(f"{exc}.") from exc
    out = folders.REBUILT / f"{kind.key}.bin"
    out.parent.mkdir(parents=True, exist_ok=True)
    part = out.with_name(f"{out.name}.{os.getpid()}.part")
    part.write_bytes(flash)
    part.replace(out)   # whole, for a check already started from it
    print(f"{kind.name}: a whole flash rebuilt from the public S61 dump and its official update file, "
          f"in {out.relative_to(ROOT)}.", flush=True)
    return out


def check_beken(kind: kinds.Kind, instances: list[int]) -> int:
    """A Beken device: the S61 from upstream's public dump, any other from its
    rebuilt flash. Its logs are kept as an Espressif device's are (LOGS)."""
    subprocess.run([str(ROOT / "tools/beken/emulator/beken-emu.sh"), "dump"], check=True)
    env = {name: value for name, value in os.environ.items() if name != "AUTOFLASH_BEKEN_EMU_FLASH"}
    env["AUTOFLASH_BEKEN_EMU_SUITE_DIR"] = str(LOGS / kind.key)
    if rebuilt(kind):
        env["AUTOFLASH_BEKEN_EMU_FLASH"] = str(rebuilt_flash(kind))
        env["AUTOFLASH_BEKEN_EMU_FLASH_DEVICE"] = kind.key   # whose runs its emulator stats are
    return subprocess.call([str(ROOT / "tools/beken/emulator/fault_suite.py"), "check",
                            "--instances", ",".join(map(str, instances))], env=env)


def check_device(kind: kinds.Kind, instances: list[int]) -> int:
    if family_of(kind) == "beken":
        return check_beken(kind, instances)
    from tools.espressif.emulator import public_dump
    if kind.key in public_dump.DUMPS or kind.key in public_dump.UPDATES:
        return check_public_dump(kind, instances)
    # The public devices, made from a device's own backup, hold nothing of Sonoff's.
    devices = str(ROOT / "tools/espressif/emulator/public_devices.py")
    numbers = ["--instances", ",".join(map(str, instances))]
    fetched = (ROOT / "captures/emulation/saved/public-pre-final-esphome").exists() or \
        subprocess.call([sys.executable, "-I", devices, *numbers, "fetch"]) == 0
    return subprocess.call([sys.executable, "-I", devices, *numbers, "test"]) if fetched else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("device", help="list, or a device's key")
    parser.add_argument("--changed-since", help="list: only what a change since this commit could affect")
    parser.add_argument("--instances", default="1,2", help="the two emulators to use (default 1,2)")
    args = parser.parse_args(argv)
    if args.device == "list":
        print(json.dumps(jobs(changed_since(args.changed_since) if args.changed_since else None)))
        return 0
    kind = next((kind for kind in kinds.READY if kind.key == args.device and kind.key not in NOTHING_TO_RUN), None)
    if kind is None:
        parser.error(f"no such device: {args.device}; list says which there are")
    missing = missing_packages(kind)
    if missing:
        print(missing, file=sys.stderr)
        return 2
    return check_device(kind, [int(n) for n in args.instances.split(",")][:2])


# What an Espressif device's check needs beyond the standard library: ESP-IDF's
# NVS generator, which its public dump is made with (public_dump, through
# rewrite_nvs and sanitise_device) and emu.sh asks for too. In requirements/ci.txt.
ESPRESSIF_PACKAGES = ("esp_idf_nvs_partition_gen",)


def missing_packages(kind: kinds.Kind) -> str | None:
    """What to install for `kind`'s check, said before anything is made: a
    package missing stopped one part way through making its public dump, a
    traceback for an answer. None where nothing is missing."""
    import importlib.util
    lacking = [name for name in ESPRESSIF_PACKAGES if family_of(kind) == "espressif"
               and importlib.util.find_spec(name) is None]
    if not lacking:
        return None
    return (f"The {kind.key} check needs {', '.join(lacking)}, which {sys.executable} lacks. Run it with the "
            f"checkout's .venv, made as CI makes it:\n    python3 -m venv .venv && .venv/bin/pip install "
            "--require-hashes --no-deps -r requirements/ci.txt\n    .venv/bin/python -I tools/repo/emulator_check.py "
            f"{kind.key}")


if __name__ == "__main__":
    sys.exit(main())
