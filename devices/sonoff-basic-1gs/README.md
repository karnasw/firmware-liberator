# Sonoff Basic Gen5

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-basic-1gs.json)](README.md)

## Testing

**In the emulator:** it runs on another device's bootloader and settings, as no
dump of a real one is published ([what was run](#what-has-been-run)).

**Worth checking:** what model it reports, whether the run's backup found the
flash matching the table, and whether the relay, both inputs and the LED work.

## The device

Sonoff's in-line Matter over Wi-Fi switch, model **BASIC-1GS**, board
`BASIC-1GS-WIFI V1.2`. It lacks the meter of its DIN rail cousin, the
BASIC-1GSP, which runs another firmware and is not converted.

| | |
|---|---|
| Chip | BK7238 |
| Firmware name | `FWSW-01-BASICR5-BK7238` |
| Flash | 4 MB by the table in its firmware; some published specs say 2 MB, and nobody has read one here ([research](research.md#its-flash-size)) |
| Stock model | `SN-BK7238-BASICR5-01`, 1.0.4 in the public file |
| Relay | P26 |
| Inputs | P6 (`Btn`: reads low when pressed), P1 (`Btn_n`: reads high when pressed), each toggling the relay |
| Wi-Fi LED | P8, lit high |

Pins from [one person's template](https://www.elektroda.com/rtvforum/topic4167932.html),
now in OpenBeken's device list. Nobody has checked them here, and the emulator
cannot. **It switches mains: never touch or connect its pins with it on the mains.**

## What has been run

Only the emulator, on a whole flash that `rebuild_dump.py` made from the public
S61 dump's bootloader and settings and this device's official update file:

- Stock 1.0.4 came online on the local eWeLink stub, with the emulator's help
  placed by `stock_points.py`.
- The tool's own unattended run took it from stock to OpenBeken: the stock
  updater took a file wrapped as `FWSW-01-BASICR5-BK7238`, the stock bootloader
  installed it, and OpenBeken joined on the Wi-Fi read from the store and took
  the backup. It then went back to stock 1.0.4 from the official file, online
  again and answering.
- The same to ESPHome, through the interim firmware, and back.
- With `--simulate-unknown-table` the run stopped at B1, before any update was
  sent.
- The emulator check (`tools/repo/emulator_check.py sonoff-basic-1gs`) passes
  all four ways: each final firmware from stock, then each taken to the other.
  With no meter, its end-of-run check asks only that the relay switched, not
  that a meter saw it. It fetches the public S61 dump and this device's
  official file, rebuilds the flash from them in
  `captures/emulation/beken/rebuilt/`, and runs from that. The
  `emulator-check` workflow runs it for every change that could affect this
  device.

This proves the firmware and the tool's path, not this device's hardware. The
bootloader, radio data and settings were the S61's. The firmware said it was an
S61 as it registered, as that comes from its factory data, so the stub's account
gave this device's model in its place. The emulator has no relay, inputs or LED.

**What the emulator cannot show:** whether a real one reports the model above
(if not, the tool stops with nothing written, and the log says what it
reported), whether its flash is 4 MB as its firmware's table says, whether the
conversion installs there, whether its stock bootloader is the S61's, and
whether the pins are right
([what the tool checks](../../docs/adding-a-device.md#what-the-tool-checks)).

A failure on a real one may need a serial cable: unlike an Espressif device, a
Beken one has no fallback by switching it off and on.

## Files

- **OpenBeken**: [`drv_autoflash_sonoff_basic_1gs.c`](drv_autoflash_sonoff_basic_1gs.c),
  this device's table for [the tool's OpenBeken driver](../../tools/beken/openbeken/README.md),
  which reads the stock Wi-Fi on first boot and applies the wiring. The tool
  installs `artifacts/openbeken-sonoff-basic-1gs-<version>.rbl`, built and pinned
  by `tools/repo/update_firmware.py`.
- **ESPHome** (LibreTiny): [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md)
  says what it reports and how to change it. The tool installs
  `artifacts/esphome-sonoff-basic-1gs-<version>.rbl`, built the same way.
- **Flash layout**: `beken-4m`, the S61's, by the table in its official file: app
  `0x11000`, download slot `0x286000`, radio data `0x3e0000`, store `0x3e2000`.
- **Calibration**: none, as it has no meter.
- **Back to Sonoff**: the tool sends eWeLink's official file, fetched by name and
  hash from [FlashDumps](https://github.com/openshwprojects/FlashDumps), which
  publishes it (`tools/autoflash/routes/beken/images.py`). This repository holds
  no Sonoff firmware.
- **How it is converted**: as the S61 is, by the same route
  ([`tools/autoflash/routes/beken/`](../../tools/autoflash/routes/beken/)), with its
  own firmware name and table.
- **Research**: [`research.md`](research.md), what is known of its flash size and
  the read that settles it.
