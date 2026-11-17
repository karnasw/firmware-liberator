# Sonoff S61

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-s61.json)](README.md)

## Testing

**In the emulator:** it converts in every direction, faults and all, from the
public dump of a real one ([the fault suite](../../docs/testing.md#the-beken-fault-suite)).
The emulator has its meter, measuring a load while the relay pin is on, but no
relay, button or LEDs.

**Worth checking:** whether it came back on your Wi-Fi, and whether the relay,
button, LEDs and meter work.

## The device

Sonoff's Beken smart plug, sold as the **S61S** (iPlug Gen2, Matter over
Wi-Fi); the European one is model **S61STPF**. The firmware calls itself `S61`,
its board id is `FWSW-01-S61-BK`, and the module is the `WBBK_01P_V1.3`. This
folder holds or links everything specific to the S61; the tooling it shares
with other Beken devices is in [`tools/beken/`](../../tools/beken/).

| | |
|---|---|
| Chip | BK7238 (ARM968E-S) |
| Firmware name | `FWSW-01-S61-BK7238` |
| Flash | 4 MB |
| Stock model | `SN-BK7238-S61-01` |
| Relay | P26 |
| Button | P9, active low, pull-up |
| Relay LED, Wi-Fi LED | P0, P1, both lit low |
| Power meter | BL0937: CF P8, CF1 P6, SEL P24; 1 mΩ shunt, 2040:1 divider |

Pins from [OpenBeken's template for this board](research.md#the-s61s-wiring-proven-openbeken-template),
which its author checked on hardware, and which agrees with the stock driver.

## Files

- **OpenBeken**: [`drv_autoflash_sonoff_s61.c`](drv_autoflash_sonoff_s61.c),
  the S61's table for the tool's OpenBeken driver
  ([`tools/beken/openbeken/drv_autoflash.c`](../../tools/beken/openbeken/drv_autoflash.c)),
  which reads the stock Wi-Fi and meter calibration on first boot and applies
  the wiring. [`tools/beken/openbeken/`](../../tools/beken/openbeken/README.md)
  covers the driver, its build with the 4 MB patches offered upstream, and
  reverting to stock. The tool installs
  `artifacts/openbeken-sonoff-s61-<version>.rbl`, built and pinned by
  `tools/repo/update_firmware.py`
  ([docs/updating-firmware.md](../../docs/updating-firmware.md)).
- **ESPHome** (LibreTiny): [`esphome.yaml`](esphome.yaml), the path converted
  devices name as their `package_import_url`. [`esphome.md`](esphome.md) says
  what it reports, how to change it, how it differs from the S60's, how it
  goes back to stock and what the emulator validated. The tool installs
  `artifacts/esphome-sonoff-s61-<version>.rbl`, built the same way.
- **Flash layout**: the stock bootloader's table, which both firmwares are built
  for: app `0x11000`, download slot `0x286000` (where it applies an rt_ota
  update), radio calibration `0x3e0000`. A conversion never writes the stock
  firmware's EasyFlash V4 stores, at `0x3e2000` (Wi-Fi, `elecCalibCoef`) and
  `0x3f6000`.
- **Calibration**: the factory values (`elecCalibCoef`) in the stock store, read
  on first boot by OpenBeken's driver and by ESPHome
  ([how they apply](research.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping)).
- **Back to Sonoff**: Sonoff's firmware as the device's backup keeps it, else
  the update file eWeLink offers the device, else its public copy
  ([`public_copies.py`](../../tools/autoflash/routes/public_copies.py)); this
  repository holds no Sonoff firmware.
- **How it is converted**: by the tool's Beken route,
  [`tools/autoflash/routes/beken/`](../../tools/autoflash/routes/beken/).
  Sonoff's firmware takes the first firmware as an update pushed through
  eWeLink; OpenBeken and ESPHome each take the next firmware as an update of
  their own; Sonoff's firmware comes back as **Back to Sonoff** says. The backup is
  taken through OpenBeken's flash reads after the first conversion, so a run to
  ESPHome installs OpenBeken first, as the interim firmware, to take it.
  [The run, step by step](../../docs/what-happens.md#a-beken-device) gives each
  step and its ID. A run back to Sonoff's firmware ends with K3: eWeLink shows
  the device online, and it answers there.
- **Research**: [`research.md`](research.md), the S61's own: its meter's
  calibration, its wiring, and what OpenBeken did on it in the emulator. What
  was found out about its firmware, which the other 4 MB Beken devices share,
  is in [`docs/beken-research.md`](../../docs/beken-research.md).
