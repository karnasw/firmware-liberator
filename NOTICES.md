# Third-party software and corresponding source

How each file is built and published is in
[`artifacts/README.md`](artifacts/README.md) and
[`docs/updating-firmware.md`](docs/updating-firmware.md).

## Tasmota-derived firmware

Tasmota is distributed under the GNU General Public License v3.0 only.

`artifacts/tasmota32c3-bluetooth.bin` is an unmodified Tasmota build, from
`firmware/unofficial/tasmota32c3-bluetooth.bin` in Tasmota's official
`install` repository, a moving branch that no longer serves these bytes. It embeds the source identifier `f5b34a2`: its
corresponding source is upstream commit
[`f5b34a26be51be2469737d2c43a62acb237c264f`](https://github.com/arendst/Tasmota/tree/f5b34a26be51be2469737d2c43a62acb237c264f),
build environment `tasmota32c3-bluetooth` in
[`platformio_tasmota_cenv_sample.ini`](https://github.com/arendst/Tasmota/blob/f5b34a26be51be2469737d2c43a62acb237c264f/platformio_tasmota_cenv_sample.ini).

`artifacts/autoflash-recovery-safeboot-imprintable.bin` (ESP32-C3) and
`artifacts/autoflash-recovery-safeboot-esp32-imprintable.bin` (classic ESP32)
are a custom Safeboot built from upstream Tasmota commit
[`db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892`](https://github.com/arendst/Tasmota/tree/db3ae7e0276fdc38b7aeb241a2a8e33d8ffd6892).
This project's changes and build tooling are, in `tools/espressif/safeboot/`,
`repartition/recovery-safeboot/volatile-wifi-defaults.patch`,
`repartition/recovery-safeboot/persist-settings.patch`,
`configure_imprintable_safeboot.py` and `build_recovery_safeboot.sh`, and
`tools/repo/update_firmware.py` for the classic ESP32's.

The older custom `s60-tasmota-15.6.0-*.bin` builds, with their configuration,
patch, documentation and helper, are kept together under `archive/`.

## ESPHome firmware

ESPHome's firmware code is distributed under the GNU General Public License
v3.0. The images for Espressif chips, and the bridge, include Espressif's
ESP-IDF, under the [Apache License 2.0](https://www.apache.org/licenses/LICENSE-2.0):
its bundled components keep their own notices and licences, listed in the
ESP-IDF source for the version each build names.

`artifacts/esphome-sonoff-s60-imprintable-2026.10.0-2.bin` is built with ESPHome
2026.10.0 and ESP-IDF 5.5.6 from this project's config
[`devices/sonoff-s60/esphome.yaml`](devices/sonoff-s60/esphome.yaml), with `esphome compile`.
Its corresponding source is the
[ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0)
and that config.

`artifacts/esphome-sonoff-pow-elite-imprintable-2026.10.0-2.bin`,
`artifacts/esphome-sonoff-powct-imprintable-2026.10.0-3.bin`,
`artifacts/esphome-sonoff-th-elite-imprintable-2026.10.0-2.bin` and
`artifacts/esphome-sonoff-dualr3-imprintable-2026.10.0-2.bin` are built the
same way, from the same release, each from its device's `esphome.yaml`.

`artifacts/autoflash-mover-esp32c3-1.2.0.bin`,
`artifacts/autoflash-mover-esp32-1.1.0.bin` and the latter's bootloader,
`artifacts/esphome-esp32-bootloader-1.1.0.bin`, are built the same way with
ESPHome 2026.10.0 and ESP-IDF 5.5.6 from
[`devices/esp32c3/mover.yaml`](devices/esp32c3/mover.yaml) and
[`devices/esp32/mover.yaml`](devices/esp32/mover.yaml), each with
[`devices/mover_routes.h`](devices/mover_routes.h).
`artifacts/esphome-sonoff-s60-bootloader-1.2.0.bin` is the bootloader from the
S60's config at 1.2.0 in this repository's history, built with ESPHome
2026.9.1 and ESP-IDF 5.5.5: its corresponding source is the
[ESPHome 2026.9.1 release](https://github.com/esphome/esphome/tree/2026.9.1)
and that config.

`artifacts/esphome-sonoff-s61-2026.10.0-3.rbl` is built with ESPHome 2026.10.0 and
LibreTiny 1.13.0 from [`devices/sonoff-s61/esphome.yaml`](devices/sonoff-s61/esphome.yaml),
the rt_ota image LibreTiny's build makes. Its corresponding source is the
[ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0),
[LibreTiny 1.13.0](https://github.com/libretiny-eu/libretiny/tree/v1.13.0)
(MIT), the Beken SDK LibreTiny builds on, and that config.

`artifacts/esphome-sonoff-basic-1gs-2026.10.0-2.rbl` is built with ESPHome 2026.10.0 and
LibreTiny 1.13.0 from [`devices/sonoff-basic-1gs/esphome.yaml`](devices/sonoff-basic-1gs/esphome.yaml),
as the S61's is. Its corresponding source is the
[ESPHome 2026.10.0 release](https://github.com/esphome/esphome/tree/2026.10.0),
[LibreTiny 1.13.0](https://github.com/libretiny-eu/libretiny/tree/v1.13.0)
(MIT), the Beken SDK LibreTiny builds on, and that config.

## OpenBeken firmware

`artifacts/openbeken-sonoff-s61-1.18.320-2.rbl` is OpenBeken
([OpenBK7231T_App](https://github.com/openshwprojects/OpenBK7231T_App)) at commit
[`ce1abad8dc931720167a7628840adae30b92afaf`](https://github.com/openshwprojects/OpenBK7231T_App/tree/ce1abad8dc931720167a7628840adae30b92afaf)
with its Beken SDK
([beken_freertos_sdk](https://github.com/NonPIayerCharacter/beken_freertos_sdk)) at commit
[`f5a64d4cda03bac6d885f4f30c27bbd3435c5e3a`](https://github.com/NonPIayerCharacter/beken_freertos_sdk/tree/f5a64d4cda03bac6d885f4f30c27bbd3435c5e3a),
built for the BK7238 with a 4 MB flash. This project's changes are the
patches in [`tools/beken/openbeken/`](tools/beken/openbeken/) (the 4 MB
table, offered upstream as OpenBK7231T_App#2290 and beken_freertos_sdk#13,
and a hook before an OTA's restart, offered as OpenBK7231T_App#2293) and,
built in, the driver
[`tools/beken/openbeken/drv_autoflash.c`](tools/beken/openbeken/drv_autoflash.c)
with the S61's table for it,
[`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`](devices/sonoff-s61/drv_autoflash_sonoff_s61.c).
`firmware.json` records the commits and each file's hash.

`artifacts/openbeken-sonoff-basic-1gs-1.18.320-1.rbl` is the same with the
Basic Gen5's table,
[`devices/sonoff-basic-1gs/drv_autoflash_sonoff_basic_1gs.c`](devices/sonoff-basic-1gs/drv_autoflash_sonoff_basic_1gs.c),
and `artifacts/openbeken-readout-1.18.320-1.rbl` with a table that sets no
pins, [`tools/beken/openbeken/drv_autoflash_readout.c`](tools/beken/openbeken/drv_autoflash_readout.c).

## OTA bridge

`artifacts/autoflash-bridge-esp32c3-4.4.6.ota` and
`artifacts/autoflash-bridge-esp32-4.4.6.bin` are this project's own firmware,
under its [`LICENSE`](LICENSE), built from `archive/bridge/` with ESP-IDF
through PlatformIO, as `archive/bridge/platformio.ini` pins it, and wrapped
for the stock updater by `tools/espressif/build_vendor_ota.py`.

## Data files

`artifacts/tasmota-settings-template-15.6.0.0.bin` and
`artifacts/tasmota-settings-template-esp32-15.6.0.0.bin` are Tasmota 15.6.0's
default settings for the ESP32-C3 and the classic ESP32, with no network
details in them, captured from a running Tasmota.

`artifacts/sonoff-s60-partition-table.bin`,
`artifacts/sonoff-nspanel-partition-table.bin` and
`artifacts/sonoff-th-elite-partition-table.bin` are the standard ESP-IDF
partition tables those Sonoff models ship with: partition names, offsets and
sizes, with no code or settings ([why they are kept](docs/legal.md)). The
S60's was read from a stock device; the others come from public dumps in
[openshwprojects/FlashDumps](https://github.com/openshwprojects/FlashDumps/tree/4d3556605da2ce101382a01b6fb7d84e68c3df2a/IoT/ESP32).

`website/api/held-words.json`, the words for which the project's API holds a
report back for review, is drawn from Shutterstock's
[List of Dirty, Naughty, Obscene, and Otherwise Bad Words](https://github.com/LDNOOBW/List-of-Dirty-Naughty-Obscene-and-Otherwise-Bad-Words/tree/5faf2ba42d7b1c0977169ec3611df25a3c08eb13),
licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/): its
entries in the Latin alphabet, of three letters or more, lower-cased, taken by
`tools/repo/held_words.py`, and otherwise unchanged.

## Website stylesheet

`website/public/assets/github-markdown-dark.css` and
`website/public/assets/github-markdown-light.css` are the dark and light
stylesheets of [github-markdown-css](https://github.com/sindresorhus/github-markdown-css)
5.9.0, unmodified, with which firmwareliberator.com shows the README as GitHub
does. It is distributed under the MIT License:

> Copyright (c) Sindre Sorhus <sindresorhus@gmail.com> (https://sindresorhus.com)
>
> Permission is hereby granted, free of charge, to any person obtaining a copy
> of this software and associated documentation files (the "Software"), to
> deal in the Software without restriction, including without limitation the
> rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
> sell copies of the Software, and to permit persons to whom the Software is
> furnished to do so, subject to the following conditions:
>
> The above copyright notice and this permission notice shall be included in
> all copies or substantial portions of the Software.
>
> THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
> IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
> FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
> AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
> LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING
> FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS
> IN THE SOFTWARE.

## Availability of source

The Tasmota, ESPHome, LibreTiny and OpenBeken sources above are named by
upstream commit or release. If any of them stops being available upstream, ask
in an issue and the project will provide the corresponding source for any
binary it has published.

## Trademarks

Sonoff and eWeLink are trademarks of their respective owners. The GitHub
mark on the web page's GitHub button (the `mark-github` icon of GitHub's
Octicons, MIT) links to the repository, as GitHub's logo guidelines allow.
This project is independent, and not an official Sonoff, eWeLink, Tasmota,
ESPHome or OpenBeken installer.

`docs/PublicSponsor.png` is GitHub's Public Sponsor achievement badge, a copy
of GitHub's own image, shown in the README to point to GitHub Sponsors. GitHub
and GitHub Sponsors are trademarks of GitHub, Inc.
