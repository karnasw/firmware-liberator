#!/usr/bin/env python3
"""Move the firmware the tool installs to a new Tasmota or ESPHome release.

    python3 tools/repo/update_firmware.py check
        what firmware.json pins, against each project's latest stable release;
        exits 1 when either is behind

    python3 tools/repo/update_firmware.py due [--today YYYY-MM-DD]
        the release of each project now due by the settled-release rule
        (SETTLE_DAYS below), as JSON: what the firmware-update workflow takes

    python3 tools/repo/update_firmware.py tasmota [VERSION]
        pin that official Tasmota release (default: the latest) - its ESP32-C3
        app, Safeboot and factory image, from the release's page on GitHub -
        once each is shown to fit the layout the conversion builds

    python3 tools/repo/update_firmware.py esphome [VERSION] [--mover] [--new-bootloader]
        build devices/sonoff-s60/esphome.yaml with that ESPHome release (default: the
        latest) in ESPHome's own Docker image, check it, and publish it in
        artifacts/; --mover builds the mover too; --every-device builds every
        device's, as the weekly update does

    python3 tools/repo/update_firmware.py esphome [VERSION] --device sonoff-s61
        the same for a Beken device's ESPHome (devices/sonoff-s61/esphome.yaml, on
        LibreTiny): the rt_ota image its build makes, checked and published

    python3 tools/repo/update_firmware.py esphome [VERSION] --device KIND
        the same for another Espressif device's ESPHome (KIND's folder's esphome.yaml)

    python3 tools/repo/update_firmware.py esp32 [tasmota] [bridge] [recovery] [mover] [template] [tables]
        what the conversion of any device with a classic ESP32 needs, each in a
        Docker image: official Tasmota's ESP32 files pinned from the release the
        ESP32-C3's are, the bridge and the recovery Safeboot built from this
        repository's sources, the mover and its bootloader with ESPHome

    python3 tools/repo/update_firmware.py esp32c3 [bridge] [mover]
        the same for the ESP32-C3: its bridge, built from this repository's
        sources and wrapped for the S60's stock updater, and its mover

    python3 tools/repo/update_firmware.py openbeken [VERSION] [--device sonoff-s61] [--every-device]
        build OpenBeken for a Beken device - upstream at that stable release (default:
        the commit pinned, else the latest release), this project's patches (the 4 MB
        table, the hook before an OTA's restart), its driver and the device's
        table for it - in this project's build image, check it, and publish it
        in artifacts/

    python3 tools/repo/update_firmware.py publishable BASE HEAD
        that HEAD is one commit on BASE changing only what an update does
        (PUBLISHABLE below): the firmware-update workflow's check before it
        pushes one

Each of the others writes firmware.json, the published files and SHA256SUMS, and lists what
is left to do by hand. Nothing is pushed or committed. docs/updating-firmware.md
is the whole procedure, and what each check is for.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tools.autoflash import imprint, kinds, tasmota_settings  # noqa: E402
from tools.autoflash.routes.espressif import images  # noqa: E402
from tools.espressif import build_vendor_ota, chips, layouts  # noqa: E402
from tools.beken import rbl, uf2  # noqa: E402
from tools.espressif.safeboot.safeboot_migration import (  # noqa: E402
    APP0_OFFSET,
    APP0_SIZE,
    SAFEBOOT_OFFSET,
    SAFEBOOT_SIZE,
    OFFICIAL_PARTITIONS,
    TABLE_OFFSET,
    TABLE_SIZE,
    TARGET_TABLE_SHA256,
    parse_partition_sector,
    partition_tuple,
    sha256_bytes,
    target_table,
    validate_native_image,
)

FIRMWARE_JSON = ROOT / "artifacts" / "firmware.json"
# Beside the files it lists, which it names bare: `sha256sum -c` runs in artifacts/.
SHA256SUMS = ROOT / "artifacts" / "SHA256SUMS"
GITIGNORE = ROOT / ".gitignore"
TASMOTA_REPO = "arendst/Tasmota"
ESPHOME_REPO = "esphome/esphome"
ESPHOME_DOCKER = "ghcr.io/esphome/esphome"
# The ESP32-C3 files of a Tasmota release, by the name the bundle knows each by.
TASMOTA_FILES = {
    "factory": "tasmota32c3.factory.bin",
    "safeboot": "tasmota32c3-safeboot.bin",
    "app": "tasmota32c3.bin",
}
# What a chip's devices share is published under a name that says the chip, as
# a device's own says the device, so that the two chips' are told apart by
# name; tests/repo/test_firmware_pins.py holds every published file to it.
CHIP_FILES = {
    "mover": "artifacts/autoflash-mover-{chip}-{version}.bin",
    "bootloader": "artifacts/esphome-{chip}-bootloader-{version}.bin",
    "bridge": "artifacts/autoflash-bridge-{chip}-{version}{suffix}",
    "recovery": "artifacts/autoflash-recovery-safeboot-{chip}-imprintable.bin",
}


def chip_file(chip: chips.Chip, name: str, **fields: str) -> str:
    """Where `name`, shared by `chip`'s devices, is published; a field not given
    is left to fill ({version})."""
    return CHIP_FILES[name].format_map({"version": "{version}", **fields, "chip": chip.key})


def mover_config(chip: chips.Chip) -> str:
    """A chip's mover, from its own folder: it is the same for any device on it."""
    return f"devices/{chip.key}/mover.yaml"


# Each ESPHome build: its config, the file it is published as, and its name in
# its section of firmware.json (the S60's for the image, the ESP32-C3's for the mover).
ESPHOME_BUILDS = {
    "image": ("devices/sonoff-s60/esphome.yaml", "artifacts/esphome-sonoff-s60-imprintable-{version}.bin", "esphome"),
    "mover": (mover_config(chips.ESP32C3), chip_file(chips.ESP32C3, "mover"), "mover"),
}
BOOTLOADER_FILE = chip_file(chips.ESP32C3, "bootloader")
# Downloads ESPHome's builds need (ESP-IDF and its tools, a gigabyte or so),
# kept between builds. captures/ is never committed.
BUILD_CACHE = ROOT / "captures" / "esphome-build"
# The Beken devices' firmware: each ends on OpenBeken or on ESPHome built on
# LibreTiny, both rt_ota images the stock bootloader applies from the download
# slot (devices/sonoff-s61/). OpenBeken is built from upstream at a pinned
# commit with this project's patches (tools/beken/openbeken/: the 4 MB table,
# offered upstream, and a hook before an OTA's restart, which the driver uses),
# this project's driver (OPENBEKEN_DRIVER, beside the patches) and the device's
# table for it, in a build image of this project's (build.Dockerfile): the
# SDK's own toolchain wants a newer GLIBC than OpenBeken's image has. Each
# device: its table and its ESPHome config, and the files their builds are
# published as.
OPENBEKEN_REPO = "openshwprojects/OpenBK7231T_App"
OPENBEKEN_DIR = ROOT / "tools" / "beken" / "openbeken"
# Each patch, and where in the clone it applies: OpenBeken's own tree, or the SDK it clones.
OPENBEKEN_PATCHES = {
    "openbk7231t_app-bk7238-4m-flash.patch": ".",
    "openbk7231t_app-ota-before-reboot-hook.patch": ".",
    "beken_freertos_sdk-4m-pack-profile.patch": "sdk/beken_freertos_sdk",
}
# The driver's source and its header, the first the one with its start-up and commands.
OPENBEKEN_DRIVER = ("drv_autoflash.c", "drv_autoflash.h")
OPENBEKEN_BUILDER = "autoflash-openbeken-build"
OPENBEKEN_BUILD_CACHE = ROOT / "captures" / "openbeken-build"
READOUT_BUILD = "readout"
BEKEN_BUILDS = {
    "sonoff-s61": {
        "openbeken": ("devices/sonoff-s61/drv_autoflash_sonoff_s61.c", "artifacts/openbeken-sonoff-s61-{version}.rbl"),
        "esphome": ("devices/sonoff-s61/esphome.yaml", "artifacts/esphome-sonoff-s61-{version}.rbl"),
    },
    "sonoff-basic-1gs": {
        "openbeken": ("devices/sonoff-basic-1gs/drv_autoflash_sonoff_basic_1gs.c",
                      "artifacts/openbeken-sonoff-basic-1gs-{version}.rbl"),
        "esphome": ("devices/sonoff-basic-1gs/esphome.yaml", "artifacts/esphome-sonoff-basic-1gs-{version}.rbl"),
    },
    # Not a device: the OpenBeken a readout of a Beken device the tool does not
    # convert yet installs for a few minutes (tools/autoflash/readout_beken.py). Its
    # table sets no pins, so nothing is driven on hardware nobody has mapped.
    READOUT_BUILD: {
        "openbeken": ("tools/beken/openbeken/drv_autoflash_readout.c", "artifacts/openbeken-readout-{version}.rbl"),
    },
}
# What the S60's config reaches into ESPHome for, beyond its documented
# options: its last on_boot lambda saves the Wi-Fi under the one fixed key an
# ESPHome build with no network of its own looks in (devices/sonoff-s60/esphome.md says
# why). An ESPHome that changes either is one the lambda must be changed for.
ESPHOME_WIFI_SOURCE = "/esphome/esphome/components/wifi"
ESPHOME_INTERNALS = {
    "wifi_component.cpp": ("88491487UL",),
    "wifi_component.h": ("struct SavedWifiSettings {", "char ssid[33];", "char password[65];"),
}


class UpdateError(RuntimeError):
    pass


# -- what is pinned, and what is out ------------------------------------------

def load_pins() -> dict[str, Any]:
    return json.loads(FIRMWARE_JSON.read_text(encoding="utf-8"))


def save_pins(pins: dict[str, Any]) -> None:
    FIRMWARE_JSON.write_text(json.dumps(pins, indent=2) + "\n", encoding="utf-8")


def github(path: str) -> Any:
    """GitHub's API, read without signing in (or with GITHUB_TOKEN, if set)."""
    request = urllib.request.Request(f"https://api.github.com/{path}",
                                     headers={"Accept": "application/vnd.github+json",
                                              "User-Agent": "Autoflash-firmware-update"})
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def download(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "Autoflash-firmware-update"})
    with urllib.request.urlopen(request, timeout=300) as response:
        return response.read()


def stable_release(repo: str, tag: str | None) -> dict[str, Any]:
    """A release that is neither a draft nor a pre-release: that tag's, or the latest."""
    release = github(f"repos/{repo}/releases/" + (f"tags/{tag}" if tag else "latest"))
    if release.get("draft") or release.get("prerelease"):
        raise UpdateError(f"{repo} {release['tag_name']} is not a stable release")
    return release


def tag_commit(repo: str, tag: str) -> str:
    ref = github(f"repos/{repo}/git/ref/tags/{tag}")["object"]
    while ref["type"] == "tag":  # an annotated tag points at its commit through itself
        ref = github(f"repos/{repo}/git/tags/{ref['sha']}")["object"]
    return ref["sha"]


# -- which release is due ------------------------------------------------------
#
# A release is taken once it has settled: the newest of its series (Tasmota's
# major.minor, ESPHome's month), at least SETTLE_MIN_DAYS old, and either
# followed by a newer series or SETTLE_DAYS old. A fix for a bad release has
# come within days or not at all (Tasmota 14.4.0 to .1 in 4, 15.0.0 to .1 in
# 1, each .0 withdrawn), so this lets it come first; for ESPHome it means the
# month before's last point release, once the new month's .0 is out.
# firmware.json's "hold" names releases never to take.
SETTLE_MIN_DAYS = 7
SETTLE_DAYS = 14
PROJECTS = {"tasmota": TASMOTA_REPO, "esphome": ESPHOME_REPO, "openbeken": OPENBEKEN_REPO}


def version_key(project: str, tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.removeprefix("v").split("."))


def series(project: str, tag: str) -> tuple[int, ...]:
    # OpenBeken has no point releases: each is a series of its own, settled once
    # a newer one has begun or SETTLE_DAYS have passed.
    return version_key(project, tag) if project == "openbeken" else version_key(project, tag)[:2]


def stable_releases(repo: str) -> list[dict[str, Any]]:
    """Every stable release: its tag and when it was published."""
    found = []
    for page in range(1, 4):
        batch = github(f"repos/{repo}/releases?per_page=100&page={page}")
        found += [{"tag": release["tag_name"],
                   "published": dt.datetime.fromisoformat(release["published_at"].replace("Z", "+00:00"))}
                  for release in batch if not release["draft"] and not release["prerelease"]
                  and release.get("published_at")]
        if len(batch) < 100:
            break
    return found


def settled(project: str, releases: list[dict[str, Any]], today: dt.datetime) -> list[str]:
    """The tags settled by `today`, oldest first."""
    out = []
    published = [release for release in releases if release["published"] <= today]
    for release in published:
        own = series(project, release["tag"])
        newest = max((r for r in published if series(project, r["tag"]) == own),
                     key=lambda r: version_key(project, r["tag"]))
        if newest is not release:
            continue
        age = (today - release["published"]).days
        followed = any(series(project, r["tag"]) > own for r in published)
        if age >= SETTLE_MIN_DAYS and (followed or age >= SETTLE_DAYS):
            out.append(release["tag"])
    return sorted(out, key=lambda tag: version_key(project, tag))


def pinned_version(pins: dict[str, Any], project: str) -> str:
    if project == "openbeken":
        return openbeken_pinned(pins)
    if project == "tasmota":
        return pins["espressif"]["esp32c3"]["tasmota"]["version"]
    return pins["espressif"][kinds.SONOFF_S60.key]["esphome"]["esphome"]


def due(project: str, releases: list[dict[str, Any]], today: dt.datetime, pinned: str,
        hold: list[str]) -> dict[str, Any]:
    """What is due for one project: the newest settled release newer than the
    pin, unless held. Where the pinned release is gone (withdrawn, as Tasmota
    has withdrawn a release days after making it), its series' newest release
    at once."""
    # A tag that is not a release number (a nightly, say) is no release here.
    releases = [release for release in releases if re.fullmatch(r"v?\d+(\.\d+)+", release["tag"])]
    tags = {release["tag"].removeprefix("v") for release in releases}
    missing = pinned not in tags
    candidates = [tag.removeprefix("v") for tag in settled(project, releases, today)]
    if missing:
        candidates += [release["tag"].removeprefix("v") for release in releases
                       if series(project, release["tag"]) == series(project, pinned)]
    newer = sorted({tag for tag in candidates if tag not in hold
                    and version_key(project, tag) > version_key(project, pinned)},
                   key=lambda tag: version_key(project, tag))
    latest = max(tags, key=lambda tag: version_key(project, tag)) if tags else None
    return {"pinned": pinned, "pinned_missing": missing, "latest": latest,
            "due": newer[-1] if newer else None}


def built_versions(pins: dict[str, Any], name: str) -> dict[tuple[str, str], str]:
    """The version of each device's `name` build ("esphome", "openbeken"), by
    family and device: what tells whether an update raised any."""
    return {(family, owner): section[name]["version"] for family in ("espressif", "beken")
            for owner, section in pins.get(family, {}).items() if "version" in section.get(name, {})}


def openbeken_release_of(pin: dict[str, Any]) -> str:
    """The OpenBeken release a pin is built on."""
    return pin.get("release") or pin["version"].split("-")[0]


def openbeken_pinned(pins: dict[str, Any]) -> str:
    """The oldest release a Beken device's OpenBeken is built on: what a newer one is newer than."""
    releases = [openbeken_release_of(section["openbeken"]) for section in pins["beken"].values() if "openbeken" in section]
    return min(releases, key=lambda release: version_key("openbeken", release))


def command_due(args: argparse.Namespace) -> int:
    pins = load_pins()
    today = (dt.datetime.fromisoformat(args.today).replace(tzinfo=dt.timezone.utc) if args.today
             else dt.datetime.now(dt.timezone.utc))
    report = {project: due(project, stable_releases(repo), today, pinned_version(pins, project),
                           pins.get("hold", {}).get(project, []))
              for project, repo in PROJECTS.items()}
    for project, state in report.items():
        print(f"{project}: pinned {state['pinned']}{' (GONE from its releases)' if state['pinned_missing'] else ''}, "
              f"latest {state['latest']}, due {state['due'] or 'nothing'}", file=sys.stderr)
    print(json.dumps(report))
    return 0


def command_check(_args: argparse.Namespace) -> int:
    pins = load_pins()
    tasmota_latest = stable_release(TASMOTA_REPO, None)["tag_name"].removeprefix("v")
    esphome_latest = stable_release(ESPHOME_REPO, None)["tag_name"]
    behind = False
    tasmota = pinned_version(pins, "tasmota")
    print(f"Tasmota: pinned {tasmota}, latest {tasmota_latest}" + ("" if tasmota == tasmota_latest else "  <- update"))
    behind |= tasmota != tasmota_latest
    # Each device's ESPHome follows the release; a mover and a bootloader are built again only when they need to be.
    for owner, section in pins["espressif"].items():
        for name, pin in section.items():
            if "esphome" not in pin:
                continue
            late = name == "esphome" and pin["esphome"] != esphome_latest
            behind |= late
            print(f"ESPHome {owner} {name}: {Path(pin['file']).name}, built with {pin['esphome']}, "
                  f"latest {esphome_latest}" + ("  <- update" if late else ""))
    openbeken_latest = stable_release(OPENBEKEN_REPO, None)["tag_name"]
    behind |= openbeken_latest != openbeken_pinned(pins)
    print(f"OpenBeken: built on {openbeken_pinned(pins)}, latest {openbeken_latest}"
          + ("" if openbeken_latest == openbeken_pinned(pins) else "  <- update"))
    if behind:
        print("\nSee docs/updating-firmware.md.")
    return 1 if behind else 0


# -- Tasmota -------------------------------------------------------------------

def check_tasmota_files(files: dict[str, bytes], chip: chips.Chip = chips.ESP32C3) -> None:
    """Refuse a release whose files for `chip` would not fit the conversion.

    The conversion writes the partition table it was reviewed with (the
    factory image's, with the entry for the device's Sonoff identity added:
    safeboot_migration.target_table), the Safeboot into its partition and the
    app into app0, and at the end compares each with these files.
    """
    factory, safeboot, app = files["factory"], files["safeboot"], files["app"]
    table = factory[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE]
    try:
        entries = [partition_tuple(entry) for entry in parse_partition_sector(table)["entries"]]
        written = target_table(table)
    except ValueError:
        entries, written = [], b""
    if entries != list(OFFICIAL_PARTITIONS) or sha256_bytes(written) != TARGET_TABLE_SHA256:
        raise UpdateError("the factory image's partition table is not the one the conversion "
                          "writes: the repartition itself would have to be reviewed again")
    if factory[SAFEBOOT_OFFSET:SAFEBOOT_OFFSET + len(safeboot)] != safeboot:
        raise UpdateError("the factory image holds another Safeboot than the release's")
    if factory[APP0_OFFSET:APP0_OFFSET + len(app)] != app:
        raise UpdateError("the factory image holds another app than the release's")
    try:
        validate_native_image(safeboot, SAFEBOOT_SIZE, "the Safeboot", chip)
        validate_native_image(app, APP0_SIZE, "the app", chip)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc


def cfg_holder(my_user_config: str) -> int:
    match = re.search(r"^#define\s+CFG_HOLDER\s+(\d+)", my_user_config, re.MULTILINE)
    if not match:
        raise UpdateError("my_user_config.h has no CFG_HOLDER")
    return int(match.group(1))


def command_tasmota(args: argparse.Namespace) -> int:
    release = stable_release(TASMOTA_REPO, f"v{args.version}" if args.version else None)
    tag = release["tag_name"]
    version = tag.removeprefix("v")
    commit = tag_commit(TASMOTA_REPO, tag)
    print(f"Tasmota {version}: {release['html_url']} (commit {commit[:10]})")
    assets = {asset["name"]: asset for asset in release["assets"]}
    files: dict[str, bytes] = {}
    for key, name in TASMOTA_FILES.items():
        if name not in assets:
            raise UpdateError(f"the release has no {name}")
        data = download(assets[name]["browser_download_url"])
        if len(data) != assets[name]["size"]:
            raise UpdateError(f"{name} came down as {len(data):,} bytes, not {assets[name]['size']:,}")
        files[key] = data
        print(f"  {name}: {len(data):,} bytes, SHA-256 {sha256_bytes(data)}")
    check_tasmota_files(files)
    print(f"  the partition table is the conversion's; the Safeboot fits with "
          f"{SAFEBOOT_SIZE - len(files['safeboot']):,} bytes to spare, the app with "
          f"{APP0_SIZE - len(files['app']):,}")
    # The recovery Safeboot saves the Wi-Fi in its own release's settings
    # record; the official release takes that record, and moves it on to its
    # own layout, only if CFG_HOLDER is the same. Otherwise it starts from its
    # defaults, with no Wi-Fi, and raises its setup network.
    holder = cfg_holder(download(f"https://raw.githubusercontent.com/{TASMOTA_REPO}/{commit}/"
                                 "tasmota/my_user_config.h").decode("utf-8"))
    if holder != tasmota_settings.CFG_HOLDER:
        raise UpdateError(f"CFG_HOLDER is {holder}, not {tasmota_settings.CFG_HOLDER}: the "
                          "release would not take the Wi-Fi the recovery Safeboot saves")
    print(f"  CFG_HOLDER {holder}, as the recovery Safeboot's record has it")

    pins = load_pins()
    chip = espressif_section(pins, chips.ESP32C3.key)
    old = chip["tasmota"]
    previous = old["version"]
    # The release before is kept, by its files: a run that installed it, left
    # part way, finishes on it (safeboot_migration.earlier_release).
    earlier = [entry for entry in old.get("earlier", []) if entry["version"] != version]
    if previous != version:
        earlier.insert(0, {key: old[key] for key in ("version", "tag", "commit", "files")})
    texts, release_version = tasmota_doc_texts(old["version"], version) if not args.no_docs and previous != version \
        else ({}, None)
    chip["tasmota"] = {
        "version": version,
        "tag": tag,
        "commit": commit,
        "cfg_holder": holder,
        "files": {key: {"name": TASMOTA_FILES[key], "size": len(data), "sha256": sha256_bytes(data)}
                  for key, data in files.items()},
        "earlier": earlier,
    }
    if args.dry_run:
        print("\nChecked; firmware.json left as it was (--dry-run).")
        return 0
    # Every other chip a device is on takes the same release.
    for other, take in OTHER_CHIPS_TASMOTA.items():
        if other in pins["espressif"]:
            print(f"{other}:")
            take(pins)
    save_pins(pins)
    write_texts(texts)
    print(f"\nfirmware.json now pins Tasmota {version} (was {previous}).")
    if args.no_docs:
        left_to_do([f"CHANGELOG.md: the official Tasmota installed is {version}, with a link to its release notes"])
    elif release_version:
        print(f"CHANGELOG.md says so, under {release_version}.")
    left_to_do(["README.md and docs/: any line naming the Tasmota release installed",
                "python3 -m unittest discover -s tests",
                "the emulator runs in docs/updating-firmware.md, then a real device"])
    return 0


# -- ESPHome -------------------------------------------------------------------

def docker(*args: str, capture: bool = True) -> str:
    result = subprocess.run(["docker", *args], check=False, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.STDOUT if capture else None)
    if result.returncode:
        raise UpdateError(f"docker {' '.join(args[:2])} failed" + (f":\n{result.stdout}" if capture else ""))
    return result.stdout or ""


def esphome_builder(version: str) -> str:
    """ESPHome's own image for that release, pulled, by its digest."""
    image = f"{ESPHOME_DOCKER}:{version}"
    docker("pull", "--quiet", image)
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", image))
    digest = next((entry.split("@", 1)[1] for entry in digests if entry.startswith(ESPHOME_DOCKER + "@")), None)
    if not digest:
        raise UpdateError(f"{image} has no digest to pin")
    return f"{image}@{digest}"


def check_esphome_internals(builder: str) -> None:
    for name, needles in ESPHOME_INTERNALS.items():
        text = docker("run", "--rm", "--entrypoint", "cat", builder, f"{ESPHOME_WIFI_SOURCE}/{name}")
        for needle in needles:
            if needle not in text:
                raise UpdateError(
                    f"ESPHome's {name} no longer has '{needle}': the last on_boot lambda of "
                    "devices/sonoff-s60/esphome.yaml must be checked against it (devices/sonoff-s60/esphome.md)")


def next_minor(version: str) -> str:
    major, minor, _patch = (int(part) for part in version.split("."))
    return f"{major}.{minor + 1}.0"


def build_version(release: str, current: dict[str, Any], *, folded: bool = False) -> str:
    """This project's build on an upstream release, as a distribution numbers
    its package of one: 2026.10.0-1, the number counting its builds on that
    release. `current` is the pin it replaces; `folded`, it has never been
    released, so a build on the same release takes its version."""
    built = re.fullmatch(rf"{re.escape(release)}-(\d+)", current.get("version", ""))
    if built and folded:
        return current["version"]
    return f"{release}-{int(built.group(1)) + 1 if built else 1}"


def published_by_a_release(path: str) -> bool:
    """Whether a release of the tool has published the file `path` in
    artifacts/: the firmware.json of a release's tag pins it. With no release
    tags to look in - a shallow clone, say - it may have been: a version
    published is never built again under its own name."""
    tags = git("tag", "--list", "v*", cwd=ROOT).split()
    if not tags:
        return True
    return any(f'"{path}"' in subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:artifacts/firmware.json"],
                                             capture_output=True, text=True, encoding="utf-8").stdout
               for tag in tags)


def unreleased(pin: dict[str, Any]) -> bool:
    """Whether `pin`'s build is one no release of the tool has published: built
    again, it keeps its version, as a version never released is never raised."""
    return bool(pin) and not published_by_a_release(pin["file"])


PROJECT_VERSION = re.compile(r'(^  project:\n(?:    .*\n)*?    version: ")([^"]+)(")', re.MULTILINE)


def config_version(config: str) -> str:
    match = PROJECT_VERSION.search(config)
    if not match:
        raise UpdateError("the config has no esphome: project: version:")
    return match.group(2)


def with_config_version(config: str, version: str) -> str:
    config_version(config)
    return PROJECT_VERSION.sub(lambda match: match.group(1) + version + match.group(3), config, count=1)


# The release the config names for Home Assistant to adopt it from.  A release
# tag, never a branch: published, it cannot change, so nothing pushed here later
# reaches a device adopted from it unless its owner names a newer one.
ADOPTION_RELEASE = re.compile(r"^(  package_import_url: \S+@)\S+$", re.MULTILINE)


def adoption_tag() -> str:
    """The tag of the release to come, which every config is adopted from."""
    return f"v{release_to_come(CHANGELOG.read_text(encoding='utf-8'))}"


def as_built(config: str, version: str) -> str:
    """The config as built at this version: named so, and adopted from the
    release it comes out in."""
    tag = adoption_tag()
    return ADOPTION_RELEASE.sub(lambda match: match.group(1) + tag, with_config_version(config, version))


def adopt_every_config_from_the_release() -> None:
    """Every device's ESPHome config adopted from the release to come, built now
    or not: the release workflow holds each one to the release's tag
    (release_notes.py --adoption-tags), a device not ready yet's too."""
    tag = adoption_tag()
    for path in sorted(ROOT.glob("devices/*/esphome.yaml")):
        config = path.read_text(encoding="utf-8")
        adopted = ADOPTION_RELEASE.sub(lambda match: match.group(1) + tag, config)
        if adopted != config:
            path.write_text(adopted, encoding="utf-8")


def app_description(image: bytes) -> dict[str, str]:
    """What ESP-IDF's app description says, at its fixed place in an app."""
    desc = image[32:32 + 256]
    field = lambda start, end: desc[start:end].split(b"\0", 1)[0].decode("ascii", "replace")  # noqa: E731
    return {"version": field(16, 48), "project": field(48, 80), "esp_idf": field(112, 144).removeprefix("v")}


def build(builder: str, config_path: Path, version: str, workdir: Path) -> tuple[bytes, bytes]:
    """Build one config at this version: its app and bootloader."""
    config = as_built(config_path.read_text(encoding="utf-8"), version)
    for name in included(config):
        # Named from where the config is - shared ones up in devices/, as the
        # movers' restart - and built beside it, under its own name.
        source = config_path.parent / name
        if not source.is_file():
            raise UpdateError(f"{config_path.name} includes {name}, which is not there")
        shutil.copyfile(source, workdir / source.name)
        config = config.replace(f"    - {name}\n", f"    - {source.name}\n")
    (workdir / config_path.name).write_text(config, encoding="utf-8")
    print(f"  building {config_path.name} {version} (minutes; a first build downloads ESP-IDF)...")
    docker("run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/config",
           "-v", f"{workdir}:/config", builder, "compile", config_path.name)
    name = re.search(r"^  name: (\S+)", config, re.MULTILINE).group(1)
    out = workdir / ".esphome" / "build" / name / "build"
    return (out / "firmware.ota.bin").read_bytes(), (out / "bootloader" / "bootloader.bin").read_bytes()


def included(config: str) -> list[str]:
    """The files an ESPHome config's esphome: includes: names: not a system
    header in angle brackets (<nvs.h>), which the build's own paths give."""
    found = re.search(r"^  includes:\n((?:    - .+\n)+)", config, re.MULTILINE)
    return re.findall(r"^    - ([^<\s]\S*)", found.group(1), re.MULTILINE) if found else []


def check_esphome_app(image: bytes, label: str, release: str, chip: chips.Chip = chips.ESP32C3) -> dict[str, str]:
    try:
        images.check_app(image, label, chip)
        if not imprint.has_placeholders(image):
            raise UpdateError(f"{label} has no untouched Wi-Fi placeholders")
        imprint.imprint(image, "Autoflash-check", "a-password")  # each found once, and writable
    except (images.ImageError, imprint.ImprintError) as exc:
        raise UpdateError(str(exc)) from exc
    if imprint.find_build_paths(image):
        raise UpdateError(f"{label} holds paths from the computer that built it")
    described = app_description(image)
    if described["version"] != release:
        raise UpdateError(f"{label} says it is ESPHome {described['version']}, not {release}")
    return described


def replace_sums_line(sums: str, old: str | None, new: str, data: bytes) -> str:
    """SHA256SUMS with the old file's line made the new one's (or the new one added)."""
    line = f"{sha256_bytes(data)}  {new}"
    lines = sums.splitlines()
    if old is None:
        # A file built again under its own name keeps its place.
        at = next((index for index, entry in enumerate(lines) if entry.endswith(f"  {new}")), None)
        if at is not None:
            lines[at] = line
            return "\n".join(lines) + "\n"
    lines = [entry for entry in lines if not entry.endswith(f"  {new}")]
    if old is not None:
        at = next((index for index, entry in enumerate(lines) if entry.endswith(f"  {old}")), None)
        if at is not None:
            lines[at] = line
            return "\n".join(lines) + "\n"
    return "\n".join([*lines, line]) + "\n"


def with_ignore_exception(gitignore: str, old: str, new: str) -> str:
    """.gitignore letting the new file in where it let the old one: firmware is
    left out (*.bin), but for the files published, each named."""
    lines = gitignore.splitlines()
    if f"!{new}" in lines:
        return gitignore
    if f"!{old}" in lines:
        lines[lines.index(f"!{old}")] = f"!{new}"
    else:
        last = max(index for index, line in enumerate(lines) if line.startswith("!artifacts/"))
        lines.insert(last + 1, f"!{new}")
    return "\n".join(lines) + "\n"


def build_time(firmware: bytes, label: str) -> str:
    """When ESPHome says `firmware` was built, as its API will say it: how the
    tool tells a published image from a dashboard's build of the same config."""
    found = set(re.findall(rb"20\d\d-\d\d-\d\d \d\d:\d\d:\d\d [+-]\d{4}", firmware))
    if len(found) != 1:
        raise UpdateError(f"{label} holds {len(found)} build times, not one")
    return found.pop().decode("ascii")


def builds_record(firmware: bytes, label: str, old: dict[str, Any], version: str) -> dict[str, Any]:
    """An ESPHome image's build time, and those of the earlier versions: a device
    on one of those is offered this one.  A rebuild of a version replaces it."""
    earlier = list(old.get("earlier_builds", []))
    if old.get("built") and old.get("version") != version and old["built"] not in earlier:
        earlier.append(old["built"])
    return {"built": build_time(firmware, label), "earlier_builds": earlier}


def publish(section: dict[str, Any], key: str, file: str, data: bytes, record: dict[str, Any]) -> None:
    """`data` as `file` in artifacts/, pinned under `key` of `section` (a part of
    firmware.json) with `record`, in SHA256SUMS and let past .gitignore; the
    file it replaces removed, unless another key still names it."""
    old = section.get(key, {}).get("file", "")
    (ROOT / file).write_bytes(data)
    if old and old != file and (ROOT / old).exists() and not any(
            other.get("file") == old for name, other in section.items() if name != key):
        (ROOT / old).unlink()
    SHA256SUMS.write_text(replace_sums_line(SHA256SUMS.read_text(encoding="utf-8"),
                                            Path(old).name if old and old != file else None, Path(file).name, data),
                          encoding="utf-8")
    GITIGNORE.write_text(with_ignore_exception(GITIGNORE.read_text(encoding="utf-8"), old, file),
                         encoding="utf-8")
    section[key] = {"file": file, **record, "size": len(data), "sha256": sha256_bytes(data)}
    print(f"  published {file}: {len(data):,} bytes, SHA-256 {sha256_bytes(data)}")


def command_esphome(args: argparse.Namespace) -> int:
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    device, chip = espressif_section(pins, kinds.SONOFF_S60.key), espressif_section(pins, chips.ESP32C3.key)
    sections = {"image": device, "mover": chip, "bootloader": chip}
    old_image = dict(device["esphome"])
    builder = esphome_builder(release)
    print(f"ESPHome {release}: {builder}")
    check_esphome_internals(builder)
    print("  the Wi-Fi internals the S60's config relies on are as they were")
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    built: dict[str, tuple[str, bytes, bytes, dict[str, str]]] = {}
    for key in ["image", *(["mover"] if args.mover else [])]:
        # A build no release has published is built again under its own version.
        if key == "image":
            version = build_version(release, old_image, folded=unreleased(old_image))
        else:
            version = chip[key]["version"] if unreleased(chip[key]) else next_minor(chip[key]["version"])
        app, bootloader = build(builder, ROOT / ESPHOME_BUILDS[key][0], version, BUILD_CACHE)
        described = check_esphome_app(app, f"the {key} built", release)
        built[key] = (version, app, bootloader, described)
        print(f"  {key} {version}: {len(app):,} bytes, ESP-IDF {described['esp_idf']}, "
              f"{images.SLOT_SIZE - len(app):,} bytes to spare in the slot")
    version, app, bootloader, described = built["image"]
    pinned_bootloader = (ROOT / chip["bootloader"]["file"]).read_bytes()
    new_bootloader = bootloader != pinned_bootloader
    if new_bootloader and not args.new_bootloader:
        print(f"  this build's bootloader differs from the published one "
              f"(ESP-IDF {described['esp_idf']}, published: {chip['bootloader']['esp_idf']}): "
              "the published one is kept (--new-bootloader replaces it)")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    replaced = bool(args.new_bootloader and new_bootloader)
    texts, release_version = ({}, None) if args.no_docs else esphome_doc_texts(
        old_image, {"file": ESPHOME_BUILDS["image"][1].format(version=version), "version": version,
                    "esphome": release, "esp_idf": described["esp_idf"], "sha256": sha256_bytes(app)},
        # One folded into its version says what changed in that version's own line.
        changelog=not getattr(args, "every", False) and version != old_image["version"])

    for key, (version, app, _bootloader, described) in built.items():
        config_path = ROOT / ESPHOME_BUILDS[key][0]
        config_path.write_text(as_built(config_path.read_text(encoding="utf-8"), version), encoding="utf-8")
        publish(sections[key], ESPHOME_BUILDS[key][2], ESPHOME_BUILDS[key][1].format(version=version), app,
                {"config": ESPHOME_BUILDS[key][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder,
                 **(builds_record(app, "the ESPHome built", old_image, version) if key == "image" else {})})
    version, _app, bootloader, described = built["image"]
    if args.new_bootloader and new_bootloader:
        publish(chip, "bootloader", BOOTLOADER_FILE.format(version=version), bootloader,
                {"config": ESPHOME_BUILDS["image"][0], "version": version, "esphome": release,
                 "esp_idf": described["esp_idf"], "builder": builder})
    elif not new_bootloader:
        print("  the bootloader is byte for byte the published one: kept as it is")
    save_pins(pins)
    adopt_every_config_from_the_release()
    print("\nfirmware.json, SHA256SUMS, .gitignore and artifacts/ updated: git add the new "
          "files, and git rm the old.")
    by_hand = []
    if args.no_docs:
        by_hand += ["artifacts/README.md: each changed file's row (name, SHA-256)",
                    "NOTICES.md: the files' names, the ESPHome release and ESP-IDF version",
                    "devices/sonoff-s60/esphome.md: the ESPHome release that built the current image",
                    f"CHANGELOG.md: ESPHome is built with {release}"]
    else:
        write_texts(texts)
        print("artifacts/README.md, NOTICES.md and devices/sonoff-s60/esphome.md say so"
              + (f", and CHANGELOG.md ({release_version})." if release_version else "."))
        if args.mover or replaced:
            by_hand.append("the mover's or bootloader's own rows and paragraphs, which only the image's are written for")
        if old_image["esphome"] == release:
            by_hand.append("what the change to the config does, in CHANGELOG.md and devices/sonoff-s60/esphome.md")
    left_to_do(by_hand + ["python3 -m unittest discover -s tests (tests/repo/test_firmware_pins.py checks the docs)",
                          "the emulator runs in docs/updating-firmware.md, then a real device"])
    return 0


# -- the Beken devices' firmware ---------------------------------------------------


def beken_section(pins: dict[str, Any], device: str) -> dict[str, Any]:
    return pins.setdefault("beken", {}).setdefault(device, {})


def espressif_section(pins: dict[str, Any], owner: str) -> dict[str, Any]:
    """What firmware.json pins for one Espressif chip (what its devices share) or device."""
    return pins.setdefault("espressif", {}).setdefault(owner, {})


def git(*args: str, cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=str(cwd), check=False, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if result.returncode:
        raise UpdateError(f"git {' '.join(args[:2])} failed in {cwd.name}:\n{result.stdout}")
    return result.stdout.strip()


def openbeken_sources(device: str) -> list[str]:
    """What a device's OpenBeken is built with beyond upstream and the patches:
    the driver, then the device's table for it."""
    return ([(OPENBEKEN_DIR / name).relative_to(ROOT).as_posix() for name in OPENBEKEN_DRIVER]
            + [BEKEN_BUILDS[device]["openbeken"][0]])


def module_init(source: str) -> str:
    """The driver's one start-up function, `void Something_Init(void)`."""
    found = re.findall(r"^void (\w+_Init)\(void\)", source, re.MULTILINE)
    if len(found) != 1:
        raise UpdateError("the driver must define one `void <Name>_Init(void)` to be called at start-up")
    return found[0]


def module_commands(source: str) -> list[str]:
    """The commands the driver registers: each must be in the image built."""
    return re.findall(r'CMD_RegisterCommand\("(\w+)"', source)


def with_module(makefile: str, main: str, sources: list[str], init: str) -> tuple[str, str]:
    """platforms/obk_main.mk and src/user_main.c with the driver and the device's
    table (`sources`, in src/driver/) built and run at start-up, as
    tools/beken/openbeken/README.md says to add them: listed beside the other
    drivers, and called once the config is loaded."""
    anchor = "OBKM_SRC  += $(OBK_SRCS)driver/drv_main.c\n"
    if makefile.count(anchor) != 1:
        raise UpdateError("platforms/obk_main.mk no longer lists driver/drv_main.c once: where the driver goes")
    call = "\tCFG_InitAndLoad();\n"
    if main.count(call) != 1:
        raise UpdateError("src/user_main.c no longer calls CFG_InitAndLoad() once: where the driver's start goes")
    return (makefile.replace(anchor, anchor + "".join(f"OBKM_SRC  += $(OBK_SRCS)driver/{name}\n" for name in sources)),
            main.replace(call, call + f"\n\textern void {init}(void);\n\t{init}();\n"))


def openbeken_ref(release: str | None, current: dict[str, Any]) -> str:
    """What to build: a stable release, as OpenBeken moves only to one (a tag it
    has not published as stable is refused); else the commit pinned, for a change
    of this project's alone; else, with none pinned, its latest release."""
    if release or not current:
        return stable_release(OPENBEKEN_REPO, release)["tag_name"]
    return current["app_commit"]


def openbeken_source(ref: str) -> Path:
    """A fresh clone of OpenBeken at `ref`, its SDK with it, with this
    project's patches applied (tools/beken/openbeken/, OPENBEKEN_PATCHES)."""
    source = OPENBEKEN_BUILD_CACHE / "OpenBK7231T_App"
    if source.exists():
        # The SDK's toolchain unpacks read-only: made writable as it is removed.
        def writable_then(remove, path, _exc):
            os.chmod(Path(path).parent, 0o700)
            remove(path)
        shutil.rmtree(source, onerror=writable_then)
    OPENBEKEN_BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    print(f"  cloning {OPENBEKEN_REPO} at {ref[:12]} and its SDK (a minute or two)...")
    git("clone", "--quiet", f"https://github.com/{OPENBEKEN_REPO}.git", str(source), cwd=OPENBEKEN_BUILD_CACHE)
    git("checkout", "--quiet", ref, cwd=source)
    git("submodule", "update", "--init", "--depth=1", "sdk/beken_freertos_sdk", cwd=source)
    for patch, where in OPENBEKEN_PATCHES.items():
        git("apply", str(OPENBEKEN_DIR / patch), cwd=source / where)
    return source


def openbeken_release(source: Path) -> str:
    """The OpenBeken release a clone's commit is, or is the latest before it."""
    return git("describe", "--tags", "--abbrev=0", "HEAD", cwd=source)


def openbeken_builder() -> str:
    """This project's build image, built from build.Dockerfile: named by the
    base image's digest and the Dockerfile's hash, which is what pins a build."""
    dockerfile = OPENBEKEN_DIR / "build.Dockerfile"
    base = re.search(r"^FROM (\S+)", dockerfile.read_text(encoding="utf-8"), re.MULTILINE).group(1)
    docker("pull", "--quiet", base)
    docker("build", "--quiet", "-t", OPENBEKEN_BUILDER, "-f", str(dockerfile), str(OPENBEKEN_DIR))
    digests = json.loads(docker("image", "inspect", "--format", "{{json .RepoDigests}}", base))
    pinned = next((entry for entry in digests if entry.startswith(base.split(":")[0] + "@")), base)
    return f"{pinned} with {dockerfile.relative_to(ROOT)} {sha256_bytes(dockerfile.read_bytes())[:16]}"


def build_openbeken(source: Path, version: str) -> bytes:
    """OpenBeken for the BK7238 with the 4 MB table, at `version`: the rt_ota image."""
    # Through a shell: the SDK's makefiles find their toolchain by $PWD, which
    # only a shell sets.
    run = ["run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp", "-v", f"{source}:/app",
           OPENBEKEN_BUILDER, "sh", "-c"]
    print("  building OpenBeken (a minute or two)...")
    docker(*run, "cd /app && make APP_NAME=OpenBK7238 prebuild_OpenBK7238")
    docker(*run, f"cd /app && make APP_VERSION={version} APP_NAME=OpenBK7238 FLASH_SIZE=4M OpenBK7238")
    return (source / "output" / version / f"OpenBK7238_4M_{version}.rbl").read_bytes()


def check_rbl(image: bytes, label: str, version: str | None, needles: list[bytes]) -> bytes:
    """An rt_ota app image the stock bootloader applies, unpacked and checked:
    it fits the download slot, says it is `version` (where the build names it
    so), and holds each of `needles` - what the build was to put in it."""
    try:
        header = rbl.header(image)
        firmware = rbl.unpack(image)
    except rbl.RblError as exc:
        raise UpdateError(f"{label} is not an rt_ota image: {exc}") from exc
    if header.name != "app":
        raise UpdateError(f"{label} is an rt_ota {header.name!r} image, not an app")
    if version is not None and header.version != version:
        raise UpdateError(f"{label} says it is {header.version}, not {version}")
    if len(image) > uf2.DOWNLOAD_SIZE:
        raise UpdateError(f"{label} is {len(image):,} bytes; the download slot holds {uf2.DOWNLOAD_SIZE:,}")
    for needle in needles:
        if needle not in firmware:
            raise UpdateError(f"{label} does not hold {needle.decode('ascii', 'replace')!r}: not built as meant")
    return firmware


def command_openbeken(args: argparse.Namespace) -> int:
    file_pattern = BEKEN_BUILDS[args.device]["openbeken"][1]
    sources = openbeken_sources(args.device)
    pins = load_pins()
    section = beken_section(pins, args.device)
    current = section.get("openbeken", {})
    wanted = openbeken_ref(args.version, current)
    driver = (ROOT / sources[0]).read_text(encoding="utf-8")
    init, commands = module_init(driver), module_commands(driver)
    builder = openbeken_builder()
    source = openbeken_source(wanted)
    commit = git("rev-parse", "HEAD", cwd=source)
    release = openbeken_release(source)
    # A build no release has published is built again under its own version:
    # a version never released is never raised.
    folded = bool(current) and not published_by_a_release(current["file"])
    version = build_version(release, current, folded=folded)
    print(f"OpenBeken {version} for the {args.device}: {OPENBEKEN_REPO} at {commit[:7]}, built in {builder}")
    for path in sources:
        target = source / "src" / "driver" / Path(path).name
        if target.exists():
            raise UpdateError(f"OpenBeken has a src/driver/{target.name} of its own: the driver's would replace it")
        shutil.copyfile(ROOT / path, target)
    makefile_path, main_path = source / "platforms" / "obk_main.mk", source / "src" / "user_main.c"
    makefile, main = with_module(makefile_path.read_text(encoding="utf-8"), main_path.read_text(encoding="utf-8"),
                                 [Path(path).name for path in sources if path.endswith(".c")], init)
    makefile_path.write_text(makefile, encoding="utf-8")
    main_path.write_text(main, encoding="utf-8")
    image = build_openbeken(source, version)
    firmware = check_rbl(image, "the OpenBeken built", version, [command.encode("ascii") for command in commands])
    sdk_commit = git("rev-parse", "HEAD", cwd=source / "sdk" / "beken_freertos_sdk")
    print(f"  OpenBeken {version}: {len(image):,} bytes ({len(firmware):,} unpacked), the driver's "
          f"{', '.join(commands)} in it, {uf2.DOWNLOAD_SIZE - len(image):,} bytes to spare in the download slot")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    record = {"file": file_pattern.format(version=version), "version": version, "release": release,
              "app_commit": commit, "sdk_commit": sdk_commit, "sha256": sha256_bytes(image)}
    # Worked out before anything is written, so a passage not found changes nothing.
    # One folded into its version says what changed in that version's own line.
    texts = {} if args.no_docs or not current else openbeken_doc_texts(
        args.device, current, record, changelog=not getattr(args, "every", False) and version != current["version"])
    publish(section, "openbeken", record["file"], image, {
        "version": version, "release": release, "app_commit": commit, "sdk_commit": sdk_commit,
        "sources": {path: sha256_bytes((ROOT / path).read_bytes()) for path in sources},
        "patches": {patch: sha256_bytes((OPENBEKEN_DIR / patch).read_bytes()) for patch in OPENBEKEN_PATCHES},
        "builder": builder})
    save_pins(pins)
    write_texts(texts)
    print("\nfirmware.json, SHA256SUMS, .gitignore and artifacts/ updated: git add the new file, and git rm the old.")
    left_to_do([*([] if texts else ["artifacts/README.md: the file's row (name, SHA-256)",
                                    "NOTICES.md: the file's name, the OpenBeken commit and the SDK's",
                                    "CHANGELOG.md: the device's OpenBeken is this version"]),
                "python3 tests/run.py (tests/repo/test_firmware_pins.py checks the docs)",
                "the emulator: tools/beken/emulator/fault_suite.py prep, then the six scenarios "
                "(docs/updating-firmware.md)"])
    return 0


def command_openbeken_every(args: argparse.Namespace) -> int:
    """Every Beken device's OpenBeken, built on the one release: what the weekly
    update runs, so none is left on an older one."""
    release = stable_release(OPENBEKEN_REPO, args.version)["tag_name"]
    pins = load_pins()
    before, versions = openbeken_pinned(pins), built_versions(pins, "openbeken")
    for device in BEKEN_BUILDS:
        print(f"\n== {device}")
        code = command_openbeken(argparse.Namespace(**{**vars(args), "device": device, "version": release, "every": True}))
        if code:
            return code
    # As for ESPHome, every build folded into its unreleased version adds no line.
    if not args.dry_run and not args.no_docs and built_versions(load_pins(), "openbeken") != versions:
        texts: dict[Path, str] = {}
        version = changelog_with(moved("OpenBeken", release, before), texts)
        write_texts(texts)
        print(f"CHANGELOG.md says so, under {version}.")
    return 0


def openbeken_doc_texts(device: str, old: dict[str, Any], new: dict[str, Any],
                        changelog: bool = True) -> dict[Path, str]:
    """artifacts/README.md's row and NOTICES.md for a Beken device's new OpenBeken
    image, and the changelog's line unless the caller writes one for every device."""
    readme = with_row(ARTIFACTS_README.read_text(encoding="utf-8"), old, new)
    notices = NOTICES.read_text(encoding="utf-8")
    if f"`{old['file']}`" not in notices:
        raise UpdateError(f"could not find {old['file']} in NOTICES.md to update: nothing was changed; --no-docs "
                          "leaves the docs to be written by hand")
    # Every device's OpenBeken is the one commit and SDK: each is named there by them.
    notices = notices.replace(f"`{old['file']}`", f"`{new['file']}`")
    notices = notices.replace(old["app_commit"], new["app_commit"]).replace(old["sdk_commit"], new["sdk_commit"])
    texts = {ARTIFACTS_README: readme, NOTICES: notices}
    if changelog:
        found = kinds.find(device)
        changelog_with(moved("OpenBeken", new["release"], openbeken_release_of(old),
                             found.name if found else "readout build"), texts)
    return texts


def build_libretiny(builder: str, config_path: Path, version: str, workdir: Path) -> tuple[bytes, dict[str, Any], str]:
    """Build one LibreTiny config at this version: its rt_ota image, the board
    (its layout) the build used, and the LibreTiny version."""
    config = as_built(config_path.read_text(encoding="utf-8"), version)
    (workdir / config_path.name).write_text(config, encoding="utf-8")
    print(f"  building {config_path.name} {version} (minutes; a first build downloads LibreTiny and its SDK)...")
    docker("run", "--rm", "-u", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/config",
           "-v", f"{workdir}:/config", builder, "compile", config_path.name)
    name = re.search(r"^  name: (\S+)", config, re.MULTILINE).group(1)
    out = workdir / ".esphome" / "build" / name / ".pioenvs" / name
    board = json.loads((out / "board.json").read_text(encoding="utf-8"))
    libretiny = next(path.name for path in out.glob("*_lt*.uf2")).rsplit("_lt", 1)[1].removesuffix(".uf2")
    return (out / "image_bk7238_app.ota.rbl").read_bytes(), board, libretiny


def command_esphome_beken(args: argparse.Namespace) -> int:
    config_path, file_pattern = BEKEN_BUILDS[args.device]["esphome"]
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    section = beken_section(pins, args.device)
    current = section.get("esphome", {})
    config = (ROOT / config_path).read_text(encoding="utf-8")
    # A build no release has published is built again under its own version.
    version = build_version(release, current, folded=unreleased(current))
    builder = esphome_builder(release)
    print(f"ESPHome {release} for the {args.device}: {builder}")
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    image, board, libretiny = build_libretiny(builder, ROOT / config_path, version, BUILD_CACHE)
    project = re.search(r"^    name: (\S+)", config, re.MULTILINE).group(1)
    firmware = check_rbl(image, "the ESPHome built", None,
                         [version.encode("ascii"), project.encode("ascii"), release.encode("ascii"),
                          b"autoflash_wifi"])
    layout = board.get("flash", {})
    if layout.get("download") != f"0x{uf2.DOWNLOAD_OFFSET:06X}+0x{uf2.DOWNLOAD_SIZE:X}":
        raise UpdateError(f"the build's download partition is {layout.get('download')}, not the stock "
                          f"bootloader's slot at 0x{uf2.DOWNLOAD_OFFSET:x}: the config's platformio_options")
    print(f"  ESPHome {version}: {len(image):,} bytes, LibreTiny {libretiny}, "
          f"{uf2.DOWNLOAD_SIZE - len(image):,} bytes to spare in the download slot")
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    new = {"file": file_pattern.format(version=version), "version": version, "esphome": release,
           "libretiny": libretiny, "sha256": sha256_bytes(image)}
    # Worked out before anything is written, so a passage not found changes nothing.
    # One folded into its version says what changed in that version's own line.
    texts = {} if args.no_docs or not current else beken_esphome_doc_texts(
        args.device, current, new, changelog=not getattr(args, "every", False) and version != current["version"])[0]
    (ROOT / config_path).write_text(as_built(config, version), encoding="utf-8")
    publish(section, "esphome", new["file"], image,
            {"config": config_path, "version": version, "esphome": release, "libretiny": libretiny,
             "builder": builder, **builds_record(firmware, "the ESPHome built", section.get("esphome", {}), version)})
    save_pins(pins)
    write_texts(texts)
    adopt_every_config_from_the_release()
    print("\nfirmware.json, SHA256SUMS, .gitignore, the config's version and artifacts/ updated: "
          "git add the new file, and git rm the old.")
    left_to_do([*([] if texts else ["artifacts/README.md: the file's row (name, SHA-256)",
                                    "NOTICES.md: the file's name, the ESPHome release and LibreTiny version",
                                    f"devices/{args.device}/esphome.md: the ESPHome release that built the current image",
                                    "CHANGELOG.md: the device's ESPHome is this version"]),
                "python3 tests/run.py (tests/repo/test_firmware_pins.py checks the docs)",
                "the emulator: tools/beken/emulator/fault_suite.py prep, then the scenarios it should pass"])
    return 0


# -- the other Espressif devices' firmware -----------------------------------------
# What a device of each chip needs is the same but for its ESPHome. Pinned in
# firmware.json under "espressif", by the chip's key, and each device's own
# (its ESPHome, its partition table) under its kind's, as Beken's are.
ESP32_INTERIM_TAG = "v15.6.0"  # the release the settings template's layout is checked against
ESP32_INTERIM = chips.ESP32.interim_name  # small enough for the smallest stock slot, a TH Elite's
BRIDGE_SOURCE = ROOT / "archive" / "bridge"
FIRMWARE_BUILDER = "autoflash-firmware-build"
FIRMWARE_BUILD_CACHE = ROOT / "captures" / "firmware-build"
SAFEBOOT_ENV = "tasmota32-safeboot"


def firmware_builder() -> str:
    docker("build", "-q", "-t", FIRMWARE_BUILDER, "-f",
           str(ROOT / "tools/espressif/firmware-build.Dockerfile"), str(ROOT / "tools/espressif/safeboot/repartition"))
    return FIRMWARE_BUILDER


def in_builder(script: str, *volumes: str) -> None:
    """Run `script` in the build image, its toolchains kept between builds."""
    cache = FIRMWARE_BUILD_CACHE / "platformio"
    cache.mkdir(parents=True, exist_ok=True)
    mounts = [arg for volume in (f"{cache}:/root/.platformio", *volumes) for arg in ("-v", volume)]
    docker("run", "--rm", *mounts, firmware_builder(), "sh", "-ec", script)


def esp32_tasmota(pins: dict[str, Any]) -> None:
    """Official Tasmota's ESP32 files, of the release the ESP32-C3's are pinned to."""
    release = pins["espressif"]["esp32c3"]["tasmota"]
    names = {"factory": "tasmota32.factory.bin", "safeboot": "tasmota32-safeboot.bin", "app": "tasmota32.bin"}
    base = f"https://github.com/{TASMOTA_REPO}/releases/download"
    files = {key: download(f"{base}/{release['tag']}/{name}") for key, name in names.items()}
    check_tasmota_files(files, chips.ESP32)
    section = espressif_section(pins, chips.ESP32.key)
    section["tasmota"] = {"version": release["version"], "tag": release["tag"], "commit": release["commit"],
                          "files": {key: {"name": names[key], "size": len(data), "sha256": sha256_bytes(data)}
                                    for key, data in files.items()}}
    url = f"{base}/{ESP32_INTERIM_TAG}/{ESP32_INTERIM}"
    interim = download(url)
    try:
        validate_native_image(interim, 0x1A9000, "the interim Tasmota", chips.ESP32)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    section["interim"] = {"url": url, "size": len(interim), "sha256": sha256_bytes(interim)}
    print(f"  Tasmota {release['version']}, and {ESP32_INTERIM} of {ESP32_INTERIM_TAG} for part way through: pinned")


# The Tasmota of each chip but the ESP32-C3's, by its section in firmware.json:
# each follows the ESP32-C3's release when the tasmota command moves it.
OTHER_CHIPS_TASMOTA = {"esp32": esp32_tasmota}


def built_bridge(env: str, chip: chips.Chip) -> tuple[bytes, str]:
    """The bridge's app for `chip`, built from archive/bridge, and its version."""
    source = FIRMWARE_BUILD_CACHE / "bridge"
    shutil.rmtree(source, ignore_errors=True)
    shutil.copytree(BRIDGE_SOURCE, source)
    in_builder(f"cd /src && pio run -e {env} && chmod -R a+rwX /src", f"{source}:/src")
    app = (source / ".pio/build" / env / "firmware.bin").read_bytes()
    try:
        validate_native_image(app, 0x1F0000, "the bridge", chip)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    return app, app_description(app)["version"]


def esp32_bridge(pins: dict[str, Any]) -> None:
    app, version = built_bridge("autoflash-bridge-esp32", chips.ESP32)
    publish(espressif_section(pins, chips.ESP32.key), "bridge",
            chip_file(chips.ESP32, "bridge", version=version, suffix=".bin"), app,
            {"version": version})


def esp32c3_bridge(pins: dict[str, Any]) -> None:
    """Wrapped for the S60's stock updater, as the file is sent; for another
    kind of device the run wraps the same app again for its model."""
    app, version = built_bridge("autoflash-bridge", chips.ESP32C3)
    try:
        wrapped = build_vendor_ota.build_wrapper(app, version=version)
    except ValueError as exc:
        raise UpdateError(str(exc)) from exc
    publish(espressif_section(pins, chips.ESP32C3.key), "bridge",
            chip_file(chips.ESP32C3, "bridge", version=version, suffix=".ota"), wrapped, {"version": version})


def esp32_recovery(pins: dict[str, Any]) -> None:
    from tools.espressif.safeboot import configure_imprintable_safeboot as configure
    from tools.espressif.safeboot.safeboot_migration import INTERIM_TASMOTA_COMMIT
    work = FIRMWARE_BUILD_CACHE / "safeboot"
    work.mkdir(parents=True, exist_ok=True)
    (work / "user_config_override.h").write_bytes(configure.render_header(persist_settings=True))
    in_builder(
        "cd /work && { [ -d t/.git ] || { git init -q t && git -C t remote add origin "
        "https://github.com/arendst/Tasmota.git; }; } && cd t && "
        f"git fetch -q --depth 1 origin {INTERIM_TASMOTA_COMMIT} && git reset -q --hard FETCH_HEAD && git clean -qfdx -e .pio && "
        "git apply /patches/volatile-wifi-defaults.patch && git apply /patches/persist-settings.patch && "
        f"cp /work/user_config_override.h tasmota/ && pio run -e {SAFEBOOT_ENV} && chmod -R a+rwX /work",
        f"{work}:/work", f"{ROOT}/tools/espressif/safeboot/repartition/recovery-safeboot:/patches:ro")
    image = imprint.redact_build_paths((work / "t/.pio/build" / SAFEBOOT_ENV / "firmware.bin").read_bytes())
    check = work / "recovery.bin"
    check.write_bytes(image)
    code = subprocess.call([sys.executable, str(ROOT / "tools/espressif/safeboot/validate_imprintable_safeboot.py"),
                            str(check), "--chip-id", str(chips.ESP32.chip_id)],
                           env={**os.environ, "PYTHONPATH": str(ROOT)})
    if code:
        raise UpdateError("the recovery Safeboot is not publishable")
    publish(espressif_section(pins, chips.ESP32.key), "recovery", chip_file(chips.ESP32, "recovery"), image,
            {"tasmota_commit": INTERIM_TASMOTA_COMMIT})


def mover(pins: dict[str, Any], chip: chips.Chip, *, with_bootloader: bool) -> None:
    """A chip's mover, at its config's version, with the ESPHome the S60's is
    pinned to; and its bootloader, where the mover's is the chip's."""
    release = pinned_version(pins, "esphome")
    builder = esphome_builder(release)
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    config = ROOT / mover_config(chip)
    version = config_version(config.read_text(encoding="utf-8"))
    app, bootloader = build(builder, config, version, BUILD_CACHE)
    described = check_esphome_app(app, "the mover built", release, chip)
    room = APP0_OFFSET - 0x20000
    if len(app) > room:
        raise UpdateError(f"the mover is {len(app):,} bytes: {room:,} fit in front of Tasmota")
    print(f"  the mover {version}: {len(app):,} bytes, {room - len(app):,} to spare in front of Tasmota")
    section = espressif_section(pins, chip.key)
    record = {"config": mover_config(chip), "version": version, "esphome": release,
              "esp_idf": described["esp_idf"], "builder": builder}
    publish(section, "mover", chip_file(chip, "mover", version=version), app, record)
    if with_bootloader:
        publish(section, "bootloader", chip_file(chip, "bootloader", version=version), bootloader, record)


def esp32_mover(pins: dict[str, Any]) -> None:
    mover(pins, chips.ESP32, with_bootloader=True)


def esp32c3_mover(pins: dict[str, Any]) -> None:
    """The ESP32-C3's bootloader is the S60 ESPHome image's (BOOTLOADER_FILE),
    published with it: only the mover here."""
    mover(pins, chips.ESP32C3, with_bootloader=False)


def esp32_template(pins: dict[str, Any]) -> None:
    """The settings template of the interim Tasmota: its record as it starts with
    no settings, read from its /dl on an emulated device into
    captures/firmware-build/ (docs/adding-a-device.md), with nothing of any network."""
    record = (FIRMWARE_BUILD_CACHE / "template-esp32.bin").read_bytes()
    template = tasmota_settings.make_template(record)
    found = tasmota_settings.problems(template, chips.ESP32.config_version)
    if found:
        raise UpdateError("the settings template is not usable: " + "; ".join(found))
    name = tasmota_settings.template_path(ROOT, chips.ESP32).relative_to(ROOT).as_posix()
    publish(espressif_section(pins, chips.ESP32.key), "settings_template", name, template, {})


def esp32_tables(pins: dict[str, Any]) -> None:
    """Sonoff's partition tables for the layouts that are not the S60's, from
    the public dumps of devices on them: they hold nothing of any one device."""
    from tools.espressif.emulator import public_dump
    for key, dump in (("sonoff-nspanel", "sonoff-nspanel"), ("sonoff-th-elite", "sonoff-th-elite")):
        layout = layouts.of(key)
        table = public_dump.fetched(public_dump.DUMPS[dump])[TABLE_OFFSET:TABLE_OFFSET + TABLE_SIZE]
        if sha256_bytes(table) != layout.table_sha256:
            raise UpdateError(f"the public dump's table is not the {key} layout's")
        publish(espressif_section(pins, key), "table", layout.table_file, table, {})


ESP32_PARTS = {"tables": esp32_tables, "template": esp32_template, "tasmota": esp32_tasmota, "bridge": esp32_bridge, "recovery": esp32_recovery, "mover": esp32_mover}
ESP32C3_PARTS = {"bridge": esp32c3_bridge, "mover": esp32c3_mover}
CHIP_PARTS = {"esp32": ESP32_PARTS, "esp32c3": ESP32C3_PARTS}


def command_chip(args: argparse.Namespace) -> int:
    parts = CHIP_PARTS[args.command]
    unknown = set(args.parts) - set(parts)
    if unknown:
        raise UpdateError(f"no such part: {', '.join(sorted(unknown))}")
    pins = load_pins()
    for part in args.parts or parts:
        print(f"{part}:")
        parts[part](pins)
        save_pins(pins)
    left_to_do(["artifacts/README.md: each file's row (name, SHA-256)",
                "NOTICES.md: the files' names and sources",
                "python3 tests/run.py, then a conversion in the emulator"])
    return 0


def esphome_command(kind: kinds.Kind) -> Callable[[argparse.Namespace], int] | None:
    """The command that builds `kind`'s ESPHome, by what it is, so that a device
    added is built with the others; None for a kind none can build."""
    if kind is kinds.SONOFF_S60:
        return command_esphome
    if kind.key in BEKEN_BUILDS:
        return command_esphome_beken
    if kind.chip.startswith("esp32"):
        return command_esphome_kind
    return None


def command_esphome_every(args: argparse.Namespace) -> int:
    """Every device's ESPHome, built with the one release: what the weekly update
    runs, so none is left on an older one."""
    pins = load_pins()
    before, versions = pinned_version(pins, "esphome"), built_versions(pins, "esphome")
    for kind in kinds.READY:
        command = esphome_command(kind)
        if command is None:
            raise UpdateError(f"no way to build {kind.name}'s ESPHome: esphome_command says how each is built")
        print(f"\n== {kind.name}")
        code = command(argparse.Namespace(**{**vars(args), "device": None if command is command_esphome else kind.key,
                                             "every": True}))
        if code:
            return code
    # One line for them all, as for Tasmota and OpenBeken: each device's own build
    # writes none when it is one of every device's. Every build folded into its
    # version, never released, says nothing new: that version's line says it.
    if not args.dry_run and not args.no_docs and built_versions(load_pins(), "esphome") != versions:
        release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
        texts: dict[Path, str] = {}
        version = changelog_with(moved("ESPHome", release, before), texts)
        write_texts(texts)
        print(f"CHANGELOG.md says so, under {version}.")
    return 0


def command_esphome_kind(args: argparse.Namespace) -> int:
    """ESPHome for an Espressif device other than the S60, from its folder."""
    kind = kinds.find(args.device)
    chip = chips.of(kind.chip)
    release = stable_release(ESPHOME_REPO, args.version)["tag_name"]
    pins = load_pins()
    section = espressif_section(pins, kind.key)
    config_path = ROOT / kind.folder / "esphome.yaml"
    config = config_path.read_text(encoding="utf-8")
    current = section.get("esphome", {})
    # A build no release has published is built again under its own version.
    version = build_version(release, current, folded=unreleased(current))
    builder = esphome_builder(release)
    print(f"ESPHome {release} for the {kind.name}: {builder}")
    check_esphome_internals(builder)
    BUILD_CACHE.mkdir(parents=True, exist_ok=True)
    app, _bootloader = build(builder, config_path, version, BUILD_CACHE)
    described = check_esphome_app(app, "the ESPHome built", release, chip)
    if args.dry_run:
        print("\nBuilt and checked; nothing published (--dry-run).")
        return 0
    file = f"artifacts/esphome-{kind.key}-imprintable-{version}.bin"
    # Worked out before anything is written, so a passage not found changes nothing.
    texts = {} if args.no_docs or not current else kind_esphome_doc_texts(
        current, {"file": file, "sha256": sha256_bytes(app)})
    config_path.write_text(as_built(config, version), encoding="utf-8")
    publish(section, "esphome", file, app,
            {"config": f"{kind.folder}/esphome.yaml", "version": version, "esphome": release,
             "esp_idf": described["esp_idf"], "builder": builder,
             **builds_record(app, "the ESPHome built", current, version)})
    save_pins(pins)
    write_texts(texts)
    adopt_every_config_from_the_release()
    left_to_do([*([] if texts else ["artifacts/README.md: the file's row (name, SHA-256)",
                                    "NOTICES.md: the file's name"]),
                "python3 tests/run.py, then a conversion in the emulator"])
    return 0


# -- what is written about it ----------------------------------------------------
#
# Each update writes what it changes into the docs the tests hold to it
# (tests/repo/test_firmware_pins.py), and its line in CHANGELOG.md: under the
# section not yet released, or a new one, a minor version on (raising
# tools/autoflash/__init__.py), so that merging it is the release. --no-docs
# leaves them for a person to write.
CHANGELOG = ROOT / "CHANGELOG.md"
VERSION_FILE = ROOT / "tools/autoflash/__init__.py"
ARTIFACTS_README = ROOT / "artifacts/README.md"
NOTICES = ROOT / "NOTICES.md"
ESPHOME_README = ROOT / "devices/sonoff-s60/esphome.md"


def released(version: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "rev-parse", "-q", "--verify", f"refs/tags/v{version}"],
                          capture_output=True).returncode == 0


def bullet(text: str) -> str:
    return textwrap.fill(text, width=76, initial_indent="- ", subsequent_indent="  ",
                         break_long_words=False, break_on_hyphens=False)


def release_to_come(changelog: str) -> str:
    """The version of the next release: the top section of CHANGELOG.md, or a
    minor version on where that is out already."""
    version = re.search(r"^## (\d+\.\d+\.\d+)\n", changelog, re.MULTILINE).group(1)
    return next_minor(version) if released(version) else version


RELEASE_PAGES = {"Tasmota": f"https://github.com/{TASMOTA_REPO}/releases/tag/v{{}}",
                 "ESPHome": f"https://github.com/{ESPHOME_REPO}/releases/tag/{{}}",
                 "OpenBeken": f"https://github.com/{OPENBEKEN_REPO}/releases/tag/{{}}"}


def moved(firmware: str, new: str, old: str, device: str | None = None) -> str:
    """A firmware's changelog line, the one form for each: every device's, or one's."""
    page = RELEASE_PAGES[firmware]
    name = f"{firmware} for the {device}" if device else firmware
    if new == old:
        return f"{name} rebuilt with [{new}]({page.format(new)})."
    return f"{name} moved to [{new}]({page.format(new)}), from [{old}]({page.format(old)})."


def changelog_with(entry: str, texts: dict[Path, str]) -> str:
    """CHANGELOG.md and tools/autoflash/__init__.py, in `texts`, with the entry
    under the release to come; that release's version."""
    text = texts.get(CHANGELOG) or CHANGELOG.read_text(encoding="utf-8")
    heading = re.search(r"^## (\d+\.\d+\.\d+)\n", text, re.MULTILINE)
    version = release_to_come(text)
    if version != heading.group(1):
        text = text[:heading.start()] + f"## {version}\n\n{bullet(entry)}\n\n" + text[heading.start():]
    else:
        following = re.search(r"^## ", text[heading.end():], re.MULTILINE)
        end = heading.end() + following.start() if following else len(text)
        text = text[:heading.end()] + text[heading.end():end].rstrip("\n") + "\n" + bullet(entry) + "\n\n" + text[end:]
    texts[CHANGELOG] = text
    texts[VERSION_FILE] = re.sub(r'^__version__ = "[^"]+"', f'__version__ = "{version}"',
                                 VERSION_FILE.read_text(encoding="utf-8"), count=1, flags=re.MULTILINE)
    return version


def write_texts(texts: dict[Path, str]) -> None:
    for path, text in texts.items():
        path.write_text(text, encoding="utf-8")


def substituted(text: str, pattern: str, replacement, what: str) -> str:
    new, count = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    if not count:
        raise UpdateError(f"could not find {what} to update: nothing was changed; --no-docs leaves the docs "
                          "to be written by hand")
    return new


def with_row(readme: str, old: dict[str, Any], new: dict[str, Any]) -> str:
    """artifacts/README.md with the old file's row saying the new file: its name and SHA-256."""
    old_name, new_name = Path(old["file"]).name, Path(new["file"]).name
    return substituted(readme, rf"^\| `{re.escape(old_name)}` \|.*$",
                       lambda row: row.group(0).replace(old_name, new_name).replace(old["sha256"], new["sha256"]),
                       f"{old_name}'s row in artifacts/README.md")


def tasmota_doc_texts(old: str, new: str) -> tuple[dict[Path, str], str]:
    texts: dict[Path, str] = {}
    version = changelog_with(moved("Tasmota", new, old), texts)
    return texts, version


def esphome_doc_texts(old: dict[str, Any], new: dict[str, Any],
                      changelog: bool = True) -> tuple[dict[Path, str], str | None]:
    """artifacts/README.md, NOTICES.md, devices/sonoff-s60/esphome.md and the
    changelog for a new image: the same config with a new ESPHome release, or,
    with the same release, a change to the config, which a person describes -
    worked out before anything is written, so a passage not found changes
    nothing."""
    readme = with_row(ARTIFACTS_README.read_text(encoding="utf-8"), old, new)
    readme = substituted(readme, r"(\(its `project` version is the one in the file name\) with ESPHome )\S+( and\s+ESP-IDF )[\d.]+",
                         lambda m: f"{m.group(1)}{new['esphome']}{m.group(2)}{new['esp_idf']}",
                         "the ESPHome release that builds the image, in artifacts/README.md")
    texts = {ARTIFACTS_README: readme}
    notices = NOTICES.read_text(encoding="utf-8")
    notices = substituted(notices, r"`artifacts/esphome-sonoff-s60-imprintable-[\d.-]+\.bin` is built with ESPHome(\s+)\S+ and ESP-IDF [\d.]+",
                          lambda m: f"`{new['file']}` is built with ESPHome{m.group(1)}{new['esphome']} and ESP-IDF {new['esp_idf']}",
                          "the image's paragraph in NOTICES.md")
    texts[NOTICES] = substituted(notices, r"\[ESPHome \S+ release\]\(https://github.com/esphome/esphome/tree/\S+\)",
                                 f"[ESPHome {new['esphome']} release](https://github.com/esphome/esphome/tree/{new['esphome']})",
                                 "the image's source release in NOTICES.md")
    texts[ESPHOME_README] = substituted(ESPHOME_README.read_text(encoding="utf-8"), r"ESPHome \S+ built the current",
                                        f"ESPHome {new['esphome']} built the current", "devices/sonoff-s60/esphome.md's release")
    if not changelog:
        return texts, None
    version = changelog_with(moved("ESPHome", new["esphome"], old["esphome"], "S60"), texts)
    return texts, version


def beken_esphome_doc_texts(device: str, old: dict[str, Any], new: dict[str, Any],
                            changelog: bool = True) -> tuple[dict[Path, str], str | None]:
    """artifacts/README.md, NOTICES.md, the device's esphome.md and the changelog
    for a Beken device's new ESPHome image."""
    readme = with_row(ARTIFACTS_README.read_text(encoding="utf-8"), old, new)
    readme = substituted(readme, rf"built with ESPHome {re.escape(old['esphome'])} on LibreTiny {re.escape(old['libretiny'])}",
                         f"built with ESPHome {new['esphome']} on LibreTiny {new['libretiny']}",
                         "the ESPHome release that builds the image, in artifacts/README.md")
    texts = {ARTIFACTS_README: readme}
    notices = NOTICES.read_text(encoding="utf-8")
    start = notices.find(f"`{old['file']}`")
    if start < 0:
        raise UpdateError(f"could not find {old['file']} in NOTICES.md to update: nothing was changed; --no-docs "
                          "leaves the docs to be written by hand")
    # From its own paragraph on: the S60's names the same project's releases.
    paragraph = substituted(notices[start:], rf"`{re.escape(old['file'])}` is built with ESPHome(\s+)\S+ and(\s+)LibreTiny \S+",
                            lambda m: f"`{new['file']}` is built with ESPHome{m.group(1)}{new['esphome']} and"
                                      f"{m.group(2)}LibreTiny {new['libretiny']}",
                            "the image's paragraph in NOTICES.md")
    paragraph = substituted(paragraph, r"\[ESPHome \S+ release\]\(https://github.com/esphome/esphome/tree/\S+\)",
                            f"[ESPHome {new['esphome']} release](https://github.com/esphome/esphome/tree/{new['esphome']})",
                            "the image's source release in NOTICES.md")
    paragraph = substituted(paragraph, r"\[LibreTiny \S+\]\(https://github.com/libretiny-eu/libretiny/tree/\S+\)",
                            f"[LibreTiny {new['libretiny']}](https://github.com/libretiny-eu/libretiny/tree/v{new['libretiny']})",
                            "the image's LibreTiny in NOTICES.md")
    texts[NOTICES] = notices[:start] + paragraph
    page = ROOT / "devices" / device / "esphome.md"
    text = substituted(page.read_text(encoding="utf-8"), rf"ESPHome \S+ built the current image, `{re.escape(old['file'])}`,",
                       f"ESPHome {new['esphome']} built the current image, `{new['file']}`,", f"{page.name}'s release")
    texts[page] = substituted(text, r"update_firmware.py esphome \S+ --device",
                              f"update_firmware.py esphome {new['esphome']} --device", f"{page.name}'s command")
    if not changelog:
        return texts, None
    version = changelog_with(moved("ESPHome", new["esphome"], old["esphome"], kinds.find(device).name), texts)
    return texts, version


def kind_esphome_doc_texts(old: dict[str, Any], new: dict[str, Any]) -> dict[Path, str]:
    """artifacts/README.md's row and NOTICES.md's name for another Espressif
    device's new ESPHome image. The release and ESP-IDF that build it are the
    S60's, which its own update writes."""
    notices = NOTICES.read_text(encoding="utf-8")
    if f"`{old['file']}`" not in notices:
        raise UpdateError(f"could not find {old['file']} in NOTICES.md to update: nothing was changed; --no-docs "
                          "leaves the docs to be written by hand")
    return {ARTIFACTS_README: with_row(ARTIFACTS_README.read_text(encoding="utf-8"), old, new),
            NOTICES: notices.replace(f"`{old['file']}`", f"`{new['file']}`")}


def left_to_do(items: list[str]) -> None:
    print("\nLeft to do (docs/updating-firmware.md):")
    for item in items:
        print(f"  - {item}")


# What the weekly update's commit may change: the firmware, its pins and
# sums, the devices' configs and pages, the notes, and the version's one
# line - never code. The firmware-update workflow builds it in a job that
# runs the releases' own build tools and so is given no write access; the job
# that pushes it checks it against this first (`publishable`).
PUBLISHABLE = re.compile(r"artifacts/[^/]+|devices/[^/]+/[^/]+\.(?:yaml|md)|CHANGELOG\.md|NOTICES\.md|\.gitignore"
                         r"|tools/autoflash/__init__\.py")
VERSION_LINE = re.compile(r'[-+]__version__ = "\d+\.\d+\.\d+"')


def unpublishable(base: str, head: str, cwd: Path = ROOT) -> list[str]:
    """Why `head` is not an update to push on top of `base`: one commit on it,
    changing only what PUBLISHABLE allows. Empty if it is."""
    if git("rev-parse", "--verify", f"{head}^", cwd=cwd) != git("rev-parse", "--verify", base, cwd=cwd):
        return [f"{head} is not one commit on {base}"]
    problems = [f"{path}: an update never changes it"
                for path in git("diff", "--name-only", "--no-renames", base, head, cwd=cwd).splitlines()
                if not PUBLISHABLE.fullmatch(path)]
    version = git("diff", "-U0", base, head, "--", "tools/autoflash/__init__.py", cwd=cwd).splitlines()
    problems += [f"tools/autoflash/__init__.py: {line!r} is more than the version"
                 for line in version if line[:1] in "+-" and line[:3] not in ("+++", "---")
                 and not VERSION_LINE.fullmatch(line)]
    return problems


def command_publishable(args: argparse.Namespace) -> int:
    problems = unpublishable(args.base, args.head)
    for problem in problems:
        print(f"  {problem}", file=sys.stderr)
    if problems:
        print("Not pushed: the update changes what it should not.", file=sys.stderr)
        return 1
    print(f"{args.head} changes only what an update does.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="pinned against latest").set_defaults(run=command_check)
    publishable = commands.add_parser("publishable", help="that HEAD is an update to push on BASE: "
                                                          "one commit, changing only what an update does")
    publishable.add_argument("base")
    publishable.add_argument("head")
    publishable.set_defaults(run=command_publishable)
    due_parser = commands.add_parser("due", help="what the settled-release rule takes now, as JSON")
    due_parser.add_argument("--today", help="as on this date (YYYY-MM-DD), for a look ahead")
    due_parser.set_defaults(run=command_due)
    tasmota = commands.add_parser("tasmota", help="pin an official Tasmota release")
    tasmota.add_argument("version", nargs="?", help="e.g. 16.0.0 (default: the latest)")
    tasmota.add_argument("--dry-run", action="store_true", help="check it, change nothing")
    tasmota.add_argument("--no-docs", action="store_true", help="leave CHANGELOG.md to be written by hand")
    tasmota.set_defaults(run=command_tasmota)
    build_parser = commands.add_parser("esphome", help="build and publish ESPHome with a release")
    build_parser.add_argument("version", nargs="?", help="e.g. 2026.10.0 (default: the latest)")
    build_parser.add_argument("--mover", action="store_true", help="build and publish the mover too")
    build_parser.add_argument("--new-bootloader", action="store_true",
                              help="publish this build's bootloader where it differs (see the docs first)")
    build_parser.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    build_parser.add_argument("--no-docs", action="store_true", help="leave the docs to be written by hand")
    build_parser.add_argument("--device", choices=sorted(kind.key for kind in kinds.EVERY if kind is not kinds.SONOFF_S60),
                              help="another device's ESPHome instead of the S60's")
    build_parser.add_argument("--every-device", action="store_true",
                              help="every device's ESPHome, as the weekly update builds them")
    build_parser.set_defaults(run=command_esphome)
    esp32 = commands.add_parser("esp32", help="what any device with a classic ESP32 needs")
    esp32.add_argument("parts", nargs="*", help=f"of {', '.join(ESP32_PARTS)} (default: all of them)")
    esp32.set_defaults(run=command_chip)
    esp32c3 = commands.add_parser("esp32c3", help="what the ESP32-C3's devices need beyond the other commands")
    esp32c3.add_argument("parts", nargs="*", help=f"of {', '.join(ESP32C3_PARTS)} (default: all of them)")
    esp32c3.set_defaults(run=command_chip)
    openbeken = commands.add_parser("openbeken", help="build and publish OpenBeken for a Beken device")
    openbeken.add_argument("version", nargs="?", help="e.g. 1.18.321, a stable release to move to (default: "
                                                      "the commit pinned, else the latest release)")
    openbeken.add_argument("--device", choices=sorted(BEKEN_BUILDS), default="sonoff-s61")
    openbeken.add_argument("--dry-run", action="store_true", help="build and check, publish nothing")
    openbeken.add_argument("--no-docs", action="store_true", help="leave the docs to be written by hand")
    openbeken.add_argument("--every-device", action="store_true",
                           help="every Beken device's OpenBeken, on one release, as the weekly update builds them")
    openbeken.set_defaults(run=command_openbeken)
    args = parser.parse_args(argv)
    if args.command == "openbeken" and args.every_device:
        args.run = command_openbeken_every
    if args.command == "esphome" and args.every_device:
        if args.device or args.mover:
            parser.error("--every-device builds every device's own ESPHome: not with --device or --mover")
        args.run = command_esphome_every
    elif args.command == "esphome" and args.device:
        args.run = command_esphome_beken if args.device in BEKEN_BUILDS else command_esphome_kind
    if shutil.which("docker") is None and args.command in ("esphome", "openbeken", "esp32", "esp32c3"):
        print("Firmware is built in Docker images: Docker is needed.", file=sys.stderr)
        return 2
    try:
        return args.run(args)
    except UpdateError as exc:
        print(f"\nStopped: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
