# Sonoff M5 2-gang

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-m5-2c.json)](README.md)

## Where it stops

Its stock firmware starts in the emulator, but goes quiet and never starts its
Wi-Fi. It runs there on another device's flash, as no usable dump of one is
published: the public one is of a locked Matter version.

## The device

Sonoff's SwitchMan wall switch with 2 gangs, the **M5**. **Not the Matter
version**, whose part number ends in W: it has secure boot and flash encryption
on, and cannot be converted; the tool does not offer it.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSWES-E32T5-T5-ESP32D0WDV3` |
| Flash | not known |
| Stock model | not known: eWeLink's product model alone says which device it is |
| Relays | GPIO23, GPIO19 |
| Status LED | GPIO5, inverted |
| Buttons | GPIO4, GPIO15 |

Pins from [ESPHome's page](https://devices.esphome.io/devices/sonoff-m5/).
Blakadder's template for this firmware's pins was not used, so the Tasmota
template in [`kinds.py`](../../tools/autoflash/kinds.py) was worked out from
that table.

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: expected to be the S60's; a device laid out otherwise stops
  the run before anything is written.
- **Calibration**: none, as it has no meter.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: not yet ([where it stops](#where-it-stops)). The route
  is the S60's ([`docs/what-happens.md`](../../docs/what-happens.md)), with the
  firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
