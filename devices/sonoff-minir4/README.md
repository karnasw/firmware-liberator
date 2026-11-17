# Sonoff MINI Extreme

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-minir4.json)](README.md)

## Where it stops

Its stock firmware starts in the emulator and comes online as a paired one does.
It runs there on another device's flash, as no dump of one is published, with
the factory and pair records a paired one has: its firmware is Sonoff's newer
"ais" SDK ([research](../../docs/espressif-research.md#sonoffs-ais-sdk)). A
conversion has not been tried on it yet.

**A readout is safe, and wanted** ([`docs/readouts.md`](../../docs/readouts.md)):
its partition table is the S60's (published from a real one by
[smeso/esp32-ptm216b](https://github.com/smeso/esp32-ptm216b)), so the bridge
runs on it as on an S60, only reads, and puts it back on Sonoff's firmware. The
copy sent in has the Wi-Fi password and eWeLink identity taken out of the records
this firmware keeps them in.

## The device

Sonoff's inline switch, the **MINIR4**. Not the MINIR4M, the Matter one, which is
locked.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSW-E32R4-MINIR4-ESP32D0WDR2V3` |
| Flash | not known |
| Stock model | `SN-ESP32D0-MINIR4-01`, 1.2.0 in the public file; eWeLink's product model says which device it is |
| Relay | GPIO26 |
| LED | GPIO19, inverted |
| Button | GPIO0, active low |
| Wall switch | GPIO27 |

Pins from [Blakadder's template](https://templates.blakadder.com/sonoff_MINIR4.html).

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: the S60's, by the table published from a real one; a device
  laid out otherwise stops the run before anything is written.
- **Calibration**: none, as it has no meter.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: not yet ([where it stops](#where-it-stops)). The route
  is the S60's ([`docs/what-happens.md`](../../docs/what-happens.md)), with the
  firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
