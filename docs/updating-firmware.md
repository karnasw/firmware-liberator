# Updating the firmware the tool installs

A converted device ends on official Tasmota, OpenBeken or this project's
ESPHome. Each follows its project's releases, a little behind: the
`firmware-update` workflow takes a release once it has settled, tests it and
releases it [by itself](#by-itself). [By hand](#by-hand) takes the same steps.

## What follows a release, and what does not

| Firmware | On the device | Follows | Pinned in |
|---|---|---|---|
| Official Tasmota: `tasmota32c3.bin`, `tasmota32c3-safeboot.bin` (and `tasmota32c3.factory.bin`, for its partition table); the classic ESP32's `tasmota32*.bin`, at the same release | for good | every Tasmota release | [`firmware.json`](../artifacts/firmware.json) |
| ESPHome for each device (`artifacts/esphome-*`) | for good | every ESPHome release, every device's built together (`esphome --every-device`) | `firmware.json` |
| ESPHome's bootloader (`artifacts/esphome-*-bootloader-*.bin`) | for good | only when it has to change | `firmware.json` |
| The mover (`artifacts/autoflash-mover-*.bin`) | a minute or two | only when its config changes | `firmware.json` |
| OpenBeken for each Beken device (`artifacts/openbeken-*.rbl`) | for good | every OpenBeken release, every Beken device's built together (`openbeken --every-device`), its six scenarios run | `firmware.json` |
| The Berry Tasmota, the recovery Safeboot, the settings template, the bridge | minutes | no | [`artifacts/README.md`](../artifacts/README.md) says where, and how each is changed |

A new pin also reaches devices already converted: run on a device with an
earlier release of its firmware, the tool offers the pinned one as an update.
So treat a change of pin as a change to devices in use too
([Changing firmware later](what-happens.md#changing-firmware-later)). A device
adopted into Home Assistant is left alone: `firmware.json` records when each
published ESPHome image was built (`built`, `earlier_builds`), and the tool
updates only a device that reports one of those times, which a build made in
Home Assistant does not.

The official Tasmota files are not kept in this repository: each run downloads
them from the release's page on GitHub and checks them against
`firmware.json`. The ESPHome and OpenBeken builds are kept in `artifacts/`,
and the run writes an Espressif device's Wi-Fi into a copy of its ESPHome.

`firmware.json` lays out both chip families alike, `espressif` and `beken`.
A chip (`espressif.esp32c3`, `espressif.esp32`) holds what every device on it
shares: official Tasmota, the mover and its bootloader, the bridge, the
recovery Safeboot. A device (`espressif.sonoff-s60`, `beken.sonoff-s61`) holds
its own: its ESPHome, its OpenBeken, its partition table. `beken.readout` is
the OpenBeken a [readout](readouts.md) installs.
`tests/repo/test_firmware_pins.py` holds the two families to that one shape.

**The interim firmware**, everything a device runs only part way through a
conversion, stays as it is: each part has been through every fault the suite
puts in its way, a change means all of that again, and none of it is left on a
device. The Tasmota it is made from is `INTERIM_TASMOTA_COMMIT` in
`tools/espressif/safeboot/safeboot_migration.py`, which need not be the
official release's. The recovery Safeboot saves the Wi-Fi in its own release's
settings record, and the official release moves that record on to its own
layout, as Tasmota does on any upgrade, as long as `CFG_HOLDER` is the same in
both, which the update checks. A Beken device's interim firmware is its
OpenBeken image itself, with a marker sent after it, so it changes with every
OpenBeken update ([below](#3-openbeken)).

**The bridge** changes only when its source in `archive/bridge` does, as a new
version. `python3 tools/repo/update_firmware.py esp32c3 bridge` publishes the
ESP32-C3's, wrapped for the S60's stock updater, and `esp32 bridge` the
classic ESP32's. Each is built in Docker and replaces the file before it in
`artifacts/`.

**The bootloader** is written in about a second, and a power cut in that
second leaves a device that has to be opened, as only this write and the
layout table's can ([what-happens.md](what-happens.md)). So a new ESPHome
release brings a new app, and the published bootloader stays unless the new
one fixes something an S60 needs (an ESP32-C3 with no secure boot or flash
encryption); the bootloader of ESP-IDF 5.x starts the apps of later 5.x
releases. Replacing it is a decision of its own: `--new-bootloader`, then the
whole fault suite and real devices.

**A file's name says what it belongs to.** A chip's file names the chip
(`autoflash-mover-esp32-1.1.0.bin`), and a device's own names the device
(`esphome-sonoff-s60-imprintable-*.bin`). The ESP32-C3's bootloader, recovery
Safeboot and settings template were published before the rule, and published
files never change, so each keeps its name until it is next built
([`artifacts/README.md`](../artifacts/README.md)); `update_firmware.py` names
the next by the rule (`esphome-esp32c3-bootloader-*`). Built again, take the
file out of `UNTIL_REBUILT` in `tests/repo/test_firmware_pins.py`, which lets
each old name stand only while that exact build is published, so a release
cannot carry a rebuild under the old name. Each chip's mover is built from
`devices/<chip>/mover.yaml`, which the same tests hold.

## By itself

Each week `.github/workflows/firmware-update.yml` asks which release of
Tasmota, ESPHome and OpenBeken has **settled**
(`python3 tools/repo/update_firmware.py due`). A release has settled when it
is

1. stable: never a beta, a pre-release or a development build;
2. the newest of its series (Tasmota's `16.0.x`, ESPHome's month,
   `2026.10.x`; each OpenBeken release, which has no point releases); and
3. old enough: past a first wait, and then either followed by a newer series
   or past a longer one (`SETTLE_MIN_DAYS` and `SETTLE_DAYS` in
   `tools/repo/update_firmware.py`, beside the rule itself).

The waits let a fix come first: a fix for a bad release has come within days
or not at all. Tasmota 14.4.0 was followed by 14.4.1 in four days and 15.0.0
by 15.0.1 in one, each `.0` withdrawn. For Tasmota, a release every two months
or so, the rule means the longer wait; for ESPHome, a series a month, it means
the month before's last point release once the new month's `.0` is out:
always a month behind, on a series that has had its fixes. A release listed
under `hold` in `firmware.json` (`"hold": {"tasmota": ["16.1.0"]}`) is never
taken.

The workflow makes what is due into one pull request: the update for every
device, a newly added one included ([steps 1 to 4](#1-tasmota) below, with the
tool's version raised a minor version), then the unit tests and, for a new
OpenBeken, its six scenarios ([step 5](#5-test)).

The update is built by the releases' own tools (ESPHome's image, PlatformIO's
downloads, OpenBeken's makefiles), so the job that builds it holds a token
that can only read. A second job, which runs none of them, pushes it, and only
if it is one commit on `main` that changes nothing but the firmware, its pins,
the devices' configs and pages, the notes and the version line
(`update_firmware.py publishable`), and the leak scan finds nothing in it. A
pull request opened with a workflow's own token starts no workflow, so this
one starts the lint, leak-scan and emulator-check workflows on its branch, and
the checks `main` requires, every device's emulator check among them, are
there.

A pull request whose tests passed **merges itself after a wait, which is the
release**, unless it is labelled `hold`; it says the date (`MERGE_AFTER_DAYS`
in the workflow). One whose tests or required checks failed is labelled
`tests-failed` and never merged by the workflow: fix it on its branch, or
close it and list the release under `hold`. Only a pull request the workflow
opened itself, from a `firmware/` branch of this repository, is merged: the
label alone, which anyone with triage rights could give a fork's pull request,
is not enough. It is merged only if `main` has not moved on since it was made
and tested; otherwise it is closed, and the next week's run makes it again.

The same run opens an issue if the pinned Tasmota release is no longer
published, as each conversion downloads its files, and takes its series'
newest release at once where there is one. It opens another when nothing has
reached `main` for some weeks: GitHub switches a public repository's scheduled
workflows off after 60 days with no activity. Once a month a dry run builds
again what is pinned, so a step that has broken is found before a release
needs it.

Run by hand from the Actions tab, `tasmota`, `esphome` or `openbeken` takes a
given release whether settled or not, and `dry_run` makes and tests the update
but pushes, opens and merges nothing. `python3 tools/repo/update_firmware.py
check` says what is pinned and what is latest.

## By hand

Work on a branch from `main`. Each step below is one commit.

### 1. Tasmota

```sh
python3 tools/repo/update_firmware.py tasmota            # the latest release
python3 tools/repo/update_firmware.py tasmota 16.0.0     # or that one
```

It reads the release from GitHub (refusing a draft or pre-release) and its
tag's commit, downloads its three ESP32-C3 files, and checks that:

- the factory image's partition table, with the entry for the device's Sonoff
  identity added, is byte for byte the one the conversion writes
  (`TARGET_TABLE_SHA256`), and the Safeboot and the app in it are the
  release's own files, at the offsets the conversion writes them to;
- the Safeboot fits its partition and the app fits app0, each a valid ESP32-C3
  image;
- `CFG_HOLDER` in the release's `my_user_config.h` is the one the recovery
  Safeboot's settings record has (`tools/autoflash/tasmota_settings.py`).

Then it writes the release, its commit and each file's size and SHA-256 into
`firmware.json`, the classic ESP32's files of that release with them.
`--dry-run` checks without writing.

Where it stops:

- **Another partition table**: Tasmota has changed its layout. The repartition
  (`TARGET_PARTITIONS`, the Berry phases in
  `tools/espressif/safeboot/repartition/berry/`) was reviewed for this one; a
  new layout is a new design, not an update.
- **Another `CFG_HOLDER`**: the release would start without the Wi-Fi the
  recovery Safeboot saved, and every conversion would end on its setup
  network. The recovery Safeboot would have to be rebuilt from that release
  (`artifacts/README.md`).
- **A file too large**: the Safeboot or the app no longer fits Tasmota's own
  layout, which Tasmota would have changed first.

### 2. ESPHome

ESPHome is built for every device: on ESP-IDF for an Espressif one, and on
LibreTiny for a Beken one, where it is an rt_ota image like OpenBeken's.

#### For an Espressif device

```sh
python3 tools/repo/update_firmware.py esphome            # the S60's, with the latest release
python3 tools/repo/update_firmware.py esphome 2026.10.0  # or with that one
python3 tools/repo/update_firmware.py esphome 2026.10.0 --device sonoff-pow-elite   # another device's
```

A change to `devices/sonoff-s60/esphome.yaml` is published the same way, naming
the release already pinned, so that only the config changes.

It needs Docker: the firmware is built in ESPHome's own image for that release
(`ghcr.io/esphome/esphome:<release>`), pinned by its digest, so the build does
not depend on the computer building it. In turn, the command:

- checks the parts of ESPHome that `devices/sonoff-s60/esphome.yaml` reaches
  into beyond its options - the fixed key its last `on_boot` lambda saves the
  Wi-Fi under, and the layout of what it saves
  ([devices/sonoff-s60/esphome.md](../devices/sonoff-s60/esphome.md) says
  why) - are as they were; if not, the lambda must be checked first;
- raises the config's `project` version (`2026.10.0-2` is this project's
  second build on ESPHome 2026.10.0, the number starting again at 1 on a new
  release), sets its `package_import_url` to the release it will come out in
  (the top of `CHANGELOG.md`, or the next after it), and builds it. A build
  that no release of the tool has published yet keeps its version and its
  file's name, as OpenBeken's does ([below](#3-openbeken)), with no changelog
  line of its own. A device adopted into Home Assistant keeps to that release
  until its owner names a newer one, and the release workflow refuses a
  version whose configs name a release that is not out, so the command sets
  every device's config to it, one not ready yet too; rebuild them if the
  version changes first;
- checks the app is one for the device's chip that fits Sonoff's slot, says it
  is that ESPHome release, holds each Wi-Fi placeholder once and untouched,
  takes a Wi-Fi written in, and holds no path from the computer that built it;
- compares its bootloader with the published one. If they differ (a new
  ESP-IDF), the published one is kept and the command says so
  ([above](#what-follows-a-release-and-what-does-not)).

Then it publishes the image in `artifacts/` (removing the one before), and
updates its line in `SHA256SUMS`, the config's version and `firmware.json`.
`--mover` builds and publishes the mover the same way, only for a change to
its config, a minor version on unless no release has published the one
before. `--dry-run` builds and checks, and publishes nothing. ESP-IDF and
its tools are downloaded once, to `captures/esphome-build/`.

An ESPHome build is not byte for byte repeatable, as it holds the time it was
built and where, so what pins a build is the Docker image it was made in, in
`firmware.json`. The bootloader is repeatable: built again with the release
that made the published one, it comes out the same.

#### For a Beken device

```sh
python3 tools/repo/update_firmware.py esphome 2026.10.0 --device sonoff-s61
```

This builds `devices/sonoff-s61/esphome.yaml` in ESPHome's image for the
release, as the S60's is built, and takes `image_bk7238_app.ota.rbl`. It
checks the image is an rt_ota app that fits the slot, says its version,
project and ESPHome release, and has the Wi-Fi action in it, with the build's
download partition where the stock bootloader looks. Its first boot reads the
device's Wi-Fi and calibration from the stock store, so the image holds no
placeholders and nothing is written into it. The config's version is raised
and the image published as the S60's is, with its docs, pinned under `beken`
in `firmware.json`. The Basic Gen5's is built the same way, from
`devices/sonoff-basic-1gs/` (`--device sonoff-basic-1gs`).

### 3. OpenBeken

OpenBeken is built for the Beken devices only: an rt_ota image the device's
stock bootloader applies from its download slot
([devices/sonoff-s61/](../devices/sonoff-s61/README.md)), pinned under `beken`
in `firmware.json`, by device.

A change to what the driver does on first boot reaches only devices converted
after it: an updated device keeps its settings, so the import does not run
again. A fix that updated devices need goes in what the driver does on every
start ([the driver's notes](../tools/beken/openbeken/README.md#updating)).

```sh
python3 tools/repo/update_firmware.py openbeken                       # again on what is pinned
python3 tools/repo/update_firmware.py openbeken 1.18.321              # moved to a stable release
```

`openbeken` builds the S61's unless `--device` names another; `--every-device`
builds every Beken device's and the readout's on one release. It clones
[OpenBK7231T_App](https://github.com/openshwprojects/OpenBK7231T_App) at the
commit pinned (or the release given; with none pinned, its latest release) and
its SDK, applies this project's patches
([`tools/beken/openbeken/`](../tools/beken/openbeken/): the 4 MB table,
offered upstream, and the hook before an OTA's restart that the driver uses),
and adds the driver (`drv_autoflash.c` and `drv_autoflash.h` there) and the
device's table for it (`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`) to the
build and to start-up, as [its notes](../tools/beken/openbeken/README.md) say.
It builds `OpenBK7238` with `FLASH_SIZE=4M` in this project's build image
(`tools/beken/openbeken/build.Dockerfile`, Ubuntu 24.04: the SDK's own
toolchain wants a newer GLIBC than OpenBeken's image has).

It checks the image is an rt_ota app of the version asked, fits the download
slot, and holds each command the driver registers. Then it publishes it,
recording the OpenBeken and SDK commits, the OpenBeken release the commit is
(or the latest before it, which the README's badge shows), the hashes of the
driver, the table and the patches, and the build image, and writes its docs.

It moves only to a stable release (`openbeken VERSION`). The version comes
from what it builds: `1.18.320-2` is this project's second build on OpenBeken
1.18.320, the number starting again at 1 on a new release. A build that no
release of the tool has published yet (no release tag's `firmware.json` pins
its file) is built again under its own version, the change folded into it,
with no changelog line of its own.

### 4. The docs

Each update writes what it changed, and `tests/repo/test_firmware_pins.py`
fails while any of it is missing:

- `artifacts/README.md`: the image's row (its name and SHA-256), and the
  ESPHome release and ESP-IDF version that build it;
- `NOTICES.md`: the image's name, ESPHome release and ESP-IDF version;
- `devices/sonoff-s60/esphome.md`: the ESPHome release that built the current
  image;
- for another Espressif device's ESPHome (`--device KIND`), its row in
  `artifacts/README.md` and its name in `NOTICES.md`;
- for a Beken device's ESPHome, the same, with its LibreTiny version, in its
  own page (`devices/sonoff-s61/esphome.md`);
- for its OpenBeken: its row in `artifacts/README.md`, and its name and the
  OpenBeken and SDK commits in `NOTICES.md`;
- `CHANGELOG.md`: a line for each firmware, with a link to its release notes,
  under the release to come - a new section, a minor version on, once the one
  above has been released, with `tools/autoflash/__init__.py` raised to it.

Left to write by hand, as the command lists when it ends: everything for a
device's first build, which has no passage to rewrite; the mover's or bootloader's own rows (`--mover`,
`--new-bootloader`); and, for a config changed and built with the release
already pinned, what the change does, in `CHANGELOG.md` and
`devices/sonoff-s60/esphome.md`.

Each passage is found and rewritten, and if one is not found nothing is
changed; `--no-docs` leaves them all to be written by hand. The README's
badges read `firmware.json` on `main`, and change by themselves.

### 5. Test

**Unit tests**, in a checkout with no emulator state linked in:

```sh
python3 tests/run.py
```

**The emulator**, from just before the final firmware: nothing before it
changes, and it was tested by the fault suite when it last did. Two public
devices ([emulator.md](emulator.md#public-devices)), one after E3 on the way
to Tasmota and one after D5 on the way to ESPHome, are taken to their final
firmware (F2 to H1, and I1 and I2), and the devices that makes are then taken
each to the other (L1 to I2, and X1 to H1): every step that writes, starts,
checks or runs code on the final firmware, in four or five minutes on two
emulators, each going on to its second run as soon as its first is done. The
devices hold nothing of Sonoff's, so no cloud is needed, real or stub, nor an
account:

```sh
python -I tools/espressif/emulator/public_devices.py test
```

All four must convert. That is the S60's check; another device's is
`python -I tools/repo/emulator_check.py DEVICE`. The `emulator-check` workflow
runs the same for each device a change could affect, with the unit tests, on
GitHub's own runners, from the published public devices and emulator image,
whenever the tool or its firmware changes. No faults are put in the way: they
test the tool's own recovery, which a new final firmware does not change.
Where the recovery leans on the firmware's own behaviour - a major release (16
to 17, say), or release notes touching its Wi-Fi, settings, updates or Berry -
run those few scenarios of the fault suite too: F4's reset, G1, `seven-times`,
and the network gone at L1 and X1
([testing.md](testing.md#the-espressif-fault-suite)).

**A new OpenBeken build**, above all on a new OpenBeken release, is run on the
S61 in the Beken emulator before it is published; the weekly update does so
itself. The driver and its hook before an OTA's restart lean on OpenBeken's
own workings: where it keeps its settings and its Wi-Fi's (`0x3e1000`, and its
flash variables at `0x1e5000`), and how its HTTP OTA ends in a restart. A
commit can move either with the build still passing and the image still
holding each command, and the interim firmware would then leave its settings
behind for ESPHome or a later OpenBeken to find. So the starting devices are
made again with the new image, then six scenarios run, each doing once
something the driver or the hook does: the Wi-Fi taken from Sonoff's firmware,
the interim firmware named, fetching ESPHome and erasing itself, kept as
OpenBeken, each way back to Sonoff's firmware, and installed from ESPHome. A
pass also checks the names the device asked DHCP for and, after ESPHome, that
the interim's settings are gone
([testing.md](testing.md#the-beken-fault-suite)):

```sh
tools/beken/emulator/beken-emu.sh dump            # the public S61 dump, if not fetched yet
tools/beken/emulator/fault_suite.py prep
tools/beken/emulator/fault_suite.py list openbeken > openbeken.txt
rm -rf captures/emulation/beken/suite-openbeken   # no pass on the image before counts
AUTOFLASH_BEKEN_EMU_SUITE_DIR=captures/emulation/beken/suite-openbeken AUTOFLASH_BEKEN_EMU_WORKERS=2 \
    tools/beken/emulator/fault_suite.py batch openbeken.txt
```

All six must pass.

**A real device**, converted from Sonoff to each firmware and between them,
before the release: an emulator does not show real flash, radios or timings
([testing.md](testing.md#real-devices)).

### 6. Release

The update raised the version in `tools/autoflash/__init__.py` to the
changelog's; merging to `main` publishes the release
(`.github/workflows/release.yml`), with the firmware files attached, once the
tests, lint and leak scan pass on `main` again.

Runs left part way by the release before carry on with the new firmware by
themselves: the bundle is built again for the new pins, at A5 or as a run
carried on past A5 starts, and a step whose checks were made with the old
bundle does them again. A run that had already installed the official app (F2)
finishes on the release it began with, using its bundle or, if this version
must build that again (1.2.0's, from another startup firmware), a bundle of
the same release. It reads the app back against that release's files as
usual, and says so: `firmware.json` keeps each release pinned before
(`earlier`) by its files, and `update_firmware.py tasmota` adds the one it
replaces.
