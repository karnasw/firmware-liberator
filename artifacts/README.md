# Published firmware artifacts

The files the tool installs or reads, each pinned in [`SHA256SUMS`](SHA256SUMS)
(`sha256sum -c SHA256SUMS` here) and in [`firmware.json`](firmware.json).
Per-device credentials and flash captures belong in the Git-ignored
`captures/`, never here. [`../NOTICES.md`](../NOTICES.md) gives each
third-party file's source and licence, and
[`../docs/updating-firmware.md`](../docs/updating-firmware.md) how each file
is rebuilt and named. Earlier files are in `git log --all -- artifacts/`.

## The files

| File | Purpose | SHA-256 |
|---|---|---|
| `autoflash-bridge-esp32c3-4.4.6.ota` | [The bridge](#the-bridge) for an ESP32-C3, wrapped for the stock updater with the S60's model | `3f1a871231bcec9cb78a5222f4eb46bca0c8ab9726987a9927c0bdd241be3a38` |
| `autoflash-bridge-esp32-4.4.6.bin` | The bridge for a classic ESP32, as a plain app the run wraps for each device's model | `3e27f0f649bac9b7ba4df98d5487bb0953d34fbab7a398d223a6468847bd8b09` |
| `tasmota32c3-bluetooth.bin` | [The Berry-capable Tasmota](#the-interim-tasmota-and-its-settings-templates) an ESP32-C3 runs for a while on its original partition layout: the exact file the successful migrations used, from Tasmota's moving `install/firmware` branch ([source](../NOTICES.md#tasmota-derived-firmware)) | `a92cfadaff5a3c824f790471ba31464acd2076a24ff1fcad2de28574c5641708` |
| `tasmota-settings-template-15.6.0.0.bin` | Tasmota's default settings with no Wi-Fi in them, for the bridge to fill in at D1 on an ESP32-C3 | `d63f03cb99540407037f36dc8155ec9b02d24425d3b02c52299713233e98f4a5` |
| `tasmota-settings-template-esp32-15.6.0.0.bin` | The same for a classic ESP32 | `7c790d6d445756cf7124841136a676741fbad33020706a8dd477e3dc51fbef49` |
| `autoflash-recovery-safeboot-imprintable.bin` | [The recovery Safeboot](#the-recovery-safeboot) for an ESP32-C3, with no credentials in it; it saves the Wi-Fi for the official Tasmota installed after it | `ece36fd964c1443ef9fb74af87a0a257501c9e5ecfb1fb7a1c3aff1744bc29fd` |
| `autoflash-recovery-safeboot-esp32-imprintable.bin` | The same for a classic ESP32 | `f1a25251f7756303e3d65061f9429d1f4da81968b89737d3d5d7364ea959860c` |
| `autoflash-mover-esp32c3-1.2.0.bin` | [The mover](#the-mover) for an ESP32-C3, a small ESPHome the run uses for a minute or two when a device leaves Tasmota | `111de916da3c43845938d10f0bd348def91ba5acf7d72d90a6d13fb526baf776` |
| `autoflash-mover-esp32-1.1.0.bin` | The mover for a classic ESP32 | `5a4ae1ff9c0d1052ff7aab8965252749eac395fb4a1ae6b3b6f1c379513ccbcf` |
| `esphome-sonoff-s60-bootloader-1.2.0.bin` | [ESPHome's bootloader](#esphome-for-espressif-devices) for an ESP32-C3: it replaces Sonoff's when ESPHome is installed, so ESPHome can roll back an update that fails to start | `3792bb665916d69c410c9fd10f69d1a6968e61f2a9e4969829a1c49f9ea6f433` |
| `esphome-esp32-bootloader-1.1.0.bin` | ESPHome's bootloader for a classic ESP32 | `b52ccf9f4602c83db673d44e84d391c44fed6d24329d4fefb9fc0d72a95a0d4f` |
| `esphome-sonoff-s60-imprintable-2026.10.0-2.bin` | [ESPHome for the S60](#esphome-for-espressif-devices), installed instead of the final Tasmota when ESPHome is chosen, with placeholders for the device's Wi-Fi | `78a9796bdc169e1f208e2ebe93766d9be9a3c69ab33d3b9bdc79e05b3188fcab` |
| `esphome-sonoff-pow-elite-imprintable-2026.10.0-2.bin` | ESPHome for the POW Elite, the same way | `1a462e946c051e8c735cdb53f0b8100e28203c488f697e5a548d8a2c29d4cc0d` |
| `esphome-sonoff-powct-imprintable-2026.10.0-3.bin` | ESPHome for the POW Ring (POWCT), the same way | `d75f9b4e4a7ed58a95cb1ad6a08fa4a0bfce5f03af3b65443fa924506a11c31f` |
| `esphome-sonoff-th-elite-imprintable-2026.10.0-2.bin` | ESPHome for the TH Elite, the same way | `6783373bce2d8941b7e20080a85360a24f6a57e72e57ad72a7a2d511f3983d0a` |
| `esphome-sonoff-dualr3-imprintable-2026.10.0-2.bin` | ESPHome for the Dual R3, the same way | `3923e646cea3bd5e39692e2a92b39234677ebd94f0454d1487554fbfa0712c2a` |
| `sonoff-s60-partition-table.bin` | [Sonoff's partition table](#sonoffs-partition-tables) of the S60, and of every device on its layout | `f63f66bbf23b9e291c7eb5dcf24be820190dacf4bf52af515d9664526a4f4daf` |
| `sonoff-nspanel-partition-table.bin` | Sonoff's partition table of the NSPanel: the S60's without its unused `reserve` entry | `3c6436bcbd529015fcb3fd98565b7753199910b3dba30c9d6ccce77d08e09d70` |
| `sonoff-th-elite-partition-table.bin` | Sonoff's partition table of the TH Elite, with a larger NVS and smaller app slots | `36d7dc76c5d2459ea174e48aaa19ff93dcb8130c43b124bba409d78b9c01c1b6` |
| `openbeken-sonoff-s61-1.18.320-2.rbl` | [OpenBeken for the S61](#beken-devices), with the 4 MB table, the hook before an OTA's restart, the tool's OpenBeken driver and the S61's table for it; a run to ESPHome sends it as the interim firmware, with a marker for its app after it | `4faef4259545557d2eee7e12499cb24c89c51e443cb987d7ad90d5f7d0bf68c1` |
| `esphome-sonoff-s61-2026.10.0-3.rbl` | ESPHome for the S61, on LibreTiny. Nothing is written into it: its first boot reads the device's Wi-Fi and meter calibration from the stock store | `f1a5e519ba079e4d45a8a77f7ca81cadb8398e21fd1914dda86addbac84f59f7` |
| `openbeken-sonoff-basic-1gs-1.18.320-1.rbl` | OpenBeken for the Basic Gen5, built as the S61's with that device's table for the driver | `5129951d1e89b06f4c5c7e39a0d01b081010d338c562d78c6e422f7175965952` |
| `esphome-sonoff-basic-1gs-2026.10.0-2.rbl` | ESPHome for the Basic Gen5, built as the S61's less its meter. No real one has been converted | `92f00bd1476d44110c63f61d1da955c804e81e79f5f0eefec85083595fb8fa0d` |
| `openbeken-readout-1.18.320-1.rbl` | OpenBeken for a [readout](../docs/readouts.md) of a Beken device the tool does not convert yet (`tools/autoflash/readout.py`): the S61's, with a table that sets no pins, so nothing is driven on hardware nobody has mapped. It joins the device's Wi-Fi, serves the whole flash to be read, and takes Sonoff's firmware back | `811a8437129eea92b4c29c60e5c56e623fd32ca7b1629625d91e295a901bd572` |

## The bridge

The bridge is the one-shot firmware the stock updater installs. It joins the
home Wi-Fi the stock firmware was on, lets the run back up the whole flash,
gives Tasmota its settings, and restarts the device into Sonoff's firmware on
`POST /restart`. It starts even after an upload into Sonoff's slot was cut off
part way, and its status page then says `fallback: none`. Failing to join, it
raises its own open network, `autoflash-recovery-XXXX` (the last two bytes of
the device's Wi-Fi MAC), and tries the home one again every three minutes
while nobody is joined to it. On a device whose partition table is at 0xC000
(the BASICR4's) it only reads, as nothing but Sonoff's firmware starts there
yet. [`../archive/bridge/README.md`](../archive/bridge/README.md) has its
source and endpoints. 4.4.6 has been tested in the emulator, and bridges up to
4.4.5 on real devices too.

Its code is the same for every device; the wrapper names the model. A bridge
has one version, in its wrapper, its app description and its status page
(`bridge 4.4.6`); `tools/espressif/build_vendor_ota.py` refuses any other. The
next is 4.4.7. The stock updater is told 9.9.9 (`ANNOUNCED_VERSION`), as the
S61's images are sent. The stock 1.2.0 updater does not compare versions (a
device on 1.2.0 took bridge v5 wrapped as `9.9.9`), but a later Sonoff
firmware might refuse anything older than itself, so the version must stay
above every Sonoff release, in at most seven characters.
`tools/repo/update_firmware.py esp32c3 bridge` and `esp32 bridge` build and
publish each chip's. Up to 4.4.5 the file named the S60
(`autoflash-bridge-sonoff-s60-*.ota`); the run still recognises a device left
on an earlier bridge ([old names](../docs/device-names.md#old-names)).

## The interim Tasmota and its settings templates

A classic ESP32's interim Tasmota is not kept here: the run downloads it from
Tasmota's release, as `firmware.json` pins it. Each settings template is its interim Tasmota's defaults, captured from a
converted device with `tools/espressif/capture_tasmota_template.py` and tested
on one with `--verify` (the classic ESP32's through `update_firmware.py esp32
template`). Its Wi-Fi names, passwords, last access point and addresses are
blank or Tasmota's defaults. It fits only that Tasmota (the ESP32-C3's Berry
build is 15.6.0.1, the same settings layout as 15.6.0); if that, or the
Tasmota the interim firmware is made from (15.6.0), changes, capture it again
(`tools/autoflash/tasmota_settings.py`). The official release a device ends on
can change without it. The ESP32-C3's next is named
`tasmota-settings-template-esp32c3-<version>.bin` (`template_path`).

## The recovery Safeboot

It is built from the pinned Tasmota source with
`tools/espressif/safeboot/repartition/recovery-safeboot/volatile-wifi-defaults.patch`
and `persist-settings.patch`. On its first boot after the repartition it wipes
NVS (by then only what Sonoff's firmware left and part of an earlier Tasmota
record), saves its settings with the Wi-Fi where Tasmota reads them, and reads
them back, so the official Tasmota installed next joins by itself. It logs
whether that worked, which the run records at F2, and never touches settings
already there. The record holds only what a Safeboot sets, so F4 starts with
`Reset 4`, which rebuilds the rest from official Tasmota's defaults and keeps
the Wi-Fi. It goes by `autoflash-recovery-XXXX` and saves that name, so the
Tasmota after it keeps it until `Reset 4`. Its log lines start "Autoflash
recovery"; the run still reads those of the earlier
`s60-recovery-safeboot-imprintable.bin` ("S60 recovery", `s60-recovery-NNNN`).

It holds placeholder bytes instead of real Wi-Fi credentials and **must not be
flashed as published**: the private bundle-preparation path must imprint and
validate a per-device copy first.

In `tools/espressif/safeboot/`, `configure_imprintable_safeboot.py` writes the
ESP32-C3's header with the placeholders, `build_recovery_safeboot.sh` builds
it, `sanitise_safeboot.py` blanks the build machine's paths, and
`validate_imprintable_safeboot.py` must say it is publishable. It is then
published as `autoflash-recovery-safeboot-esp32c3-imprintable.bin`, with
`PUBLISHED_RECOVERY_FILE` in `safeboot_migration.py` and its row here changed;
`tests/repo/test_firmware_pins.py` fails until they are.
`update_firmware.py esp32 recovery` builds the classic ESP32's. A rebuild
matches the published file but for its build date and time; built in a
directory whose path is not 38 characters long, everything after the first
such path is a few bytes along.

## The mover

The mover is a small ESPHome that fits in front of Tasmota in Sonoff's layout and takes the
next firmware as an ordinary ESPHome update. Two minutes away from the home
Wi-Fi it raises its own network, whose page takes new Wi-Fi details.
`GET /mover` tells the run it is the mover while its update service is busy or
stuck, and `POST /restart` restarts it without anyone's help. It drives no
pins, so one build serves every device with its chip and 4 MB of flash. It
goes by `autoflash-recovery-XXXX` and holds Wi-Fi placeholders. Each chip's is
built from `devices/<chip>/mover.yaml` ([the ESP32-C3's](../devices/esp32c3/mover.yaml))
with the ESPHome release `firmware.json` records.

## ESPHome for Espressif devices

The S60's image is built from
[`../devices/sonoff-s60/esphome.yaml`](../devices/sonoff-s60/esphome.yaml)
(its `project` version is the one in the file name) with ESPHome 2026.10.0 and
ESP-IDF 5.5.6, as `esphome compile sonoff_s60.yaml`, in ESPHome's own Docker
image for that release (`firmware.json` pins it by digest), and the file
published is `firmware.ota.bin`. Every other Espressif device's is built the
same way from its own `esphome.yaml`. `2026.10.0-1` would be this project's
first build on ESPHome 2026.10.0; OpenBeken's are numbered alike. To publish a
new one, change the config and run `tools/repo/update_firmware.py esphome`.

Each image holds `AUTOFLASH-IMPRINT-` placeholders for the Wi-Fi, which the
run fills in a copy in memory (`tools/autoflash/imprint.py`); it still
recognises the `S60-IMPRINT-` placeholders and `s60_wifi` action of earlier
builds. Like ESPHome's ready-made images, it has no OTA password, web login or
API encryption key: your device is open on your home network until you add
them, as official Tasmota is. What the S60's reports, its calibration and its
names are in [`../devices/sonoff-s60/esphome.md`](../devices/sonoff-s60/esphome.md).

The ESP32-C3's bootloader is `bootloader/bootloader.bin` from the build of the
S60's config at 1.2.0, with ESPHome 2026.9.1 and ESP-IDF 5.5.5; the classic
ESP32's is from its mover's build. A rebuilt image keeps the published
bootloader, so no device is given a new one.

## Sonoff's partition tables

Each is the standard ESP-IDF partition table a model ships with: partition
names, offsets and sizes, with no code, settings or secrets
([why they are kept](../docs/legal.md)). The S60's was read from a stock
device: every conversion on its layout starts from it, the Tasmota route's
preflight requires it byte for byte, and it is written back when a device
leaves Tasmota's layout. `update_firmware.py esp32 tables` takes the others
from public flash dumps (`tools/espressif/emulator/public_dump.py`), checked
against the hashes in `tools/espressif/layouts.py`.

## Beken devices

The Beken images are rt_ota images (`.rbl`), which the stock bootloader
applies from its download slot, sent by the stock updater, OpenBeken's own OTA
or in a UF2 through ESPHome's. OpenBeken is OpenBK7231T_App at the commit
`firmware.json` pins (its release is in the version; `NOTICES.md` names both
commits) with this project's 4 MB-table patches and driver
(`tools/beken/openbeken/`) and the device's table for it (the S61's is
`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`), built as `OpenBK7238` with
`FLASH_SIZE=4M` in `tools/beken/openbeken/build.Dockerfile`. ESPHome for the
S61 is [`../devices/sonoff-s61/esphome.yaml`](../devices/sonoff-s61/esphome.yaml)
built with ESPHome 2026.10.0 on LibreTiny 1.13.0, `image_bk7238_app.ota.rbl`
of its build. `firmware.json` pins both under `beken`, with the commits,
hashes and build images. `tools/repo/update_firmware.py openbeken` and
`esphome --device sonoff-s61` build and publish them, and `--device
sonoff-basic-1gs` the Basic Gen5's.

OpenBeken names the device `openbeken-abcdef` (its hostnames too), ESPHome
`esphome-abcdef` with the comment `Sonoff-S61`. ESPHome has no backup reader,
so the tool takes the backup through OpenBeken, the interim firmware, which
knows itself by the marker sent after it. ESPHome is adopted into Home
Assistant as a package, not a copy of its config.
