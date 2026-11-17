# Sonoff S60

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-s60.json)](README.md)

## Testing

**For real:** Tasmota to ESPHome and back has been run on a real S60TPG, with
every write read back, and the relay and power meter checked through each
firmware ([real devices](../../docs/testing.md#real-devices)).

**In the emulator:** it converts in every direction, faults and all, from a dump
of a real one ([the fault suite](../../docs/testing.md#the-espressif-fault-suite)).
The emulator has no button or LED, but it has the CSE7766 meter, measuring a
100 W load while the relay is on: Tasmota, ESPHome and Sonoff's firmware 1.2.0
each read it (Sonoff's reports 230.08 V, 0.43 A and 99.99 W).

**Worth checking:** whether the relay, button, LED and meter work on yours.

## The device

Sonoff's ESP32-C3 smart plug: the **S60TPG** (UK, Type G) and the **S60TPF**
(Europe, Type E/F), the same board behind different mains pins. This folder holds or
links everything specific to the S60; the tooling it shares with other Espressif
devices is in [`tools/espressif/`](../../tools/espressif/).

| | |
|---|---|
| Chip | ESP32-C3 |
| Firmware name | `FWSW-E32S60-S60-ESP32C3FN4` |
| Flash | 4 MB |
| Stock model | `SN-ESP32C3-S60-01` |
| Relay | GPIO4 |
| Status LED | GPIO5, not inverted |
| Power meter | CSE7766 on UART, RX GPIO7, 4800 baud, even parity |
| Button | GPIO9, active low, pull-up |
| Board variant | read from GPIO6, GPIO10 and GPIO19 with pull-ups at start-up; it chooses the meter's correction factor (1.02 or 1.00) |

Pins checked on a real S60TPG: the relay and the meter, through each firmware
([real devices](../../docs/testing.md#real-devices)). The board variant's pins
were read from Sonoff's firmware 1.2.0
([`calibration.py`](../../tools/autoflash/calibration.py)).

## Files

- **Tasmota**: the official release, unchanged, given this template -
  `{"NAME":"Sonoff S60TPG","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,1,1,1,1],"FLAG":0,"BASE":1}` -
  and a settings record that the bridge fills your Wi-Fi into. The record is
  [`artifacts/tasmota-settings-template-*.bin`](../../artifacts/README.md),
  captured from a device on the pinned release by
  [`tools/espressif/capture_tasmota_template.py`](../../tools/espressif/capture_tasmota_template.py)
  whenever a release changes its layout
  ([docs/updating-firmware.md](../../docs/updating-firmware.md#1-tasmota)).
  [docs/device-names.md](../../docs/device-names.md) lists its names on the
  network.
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports and how to change it. Home Assistant adopts a converted device
  from this file's path, its `package_import_url`, so moving the file means
  building a new image, as in 2.3.0. The mover that changes a device's memory
  layout is shared by every ESP32-C3 device:
  [`../esp32c3/mover.yaml`](../esp32c3/mover.yaml).
- **Flash layout**: Sonoff's, which ESPHome keeps and Tasmota replaces with its
  own ([`artifacts/sonoff-s60-partition-table.bin`](../../artifacts/README.md)).
  The recovery Safeboot and the repartitioning route are in
  [`tools/espressif/safeboot/`](../../tools/espressif/safeboot/).
- **Calibration**: the factory values in the stock NVS and the board's factor,
  read by [`tools/autoflash/calibration.py`](../../tools/autoflash/calibration.py).
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: [`docs/what-happens.md`](../../docs/what-happens.md),
  [`docs/technical-details.md`](../../docs/technical-details.md).
