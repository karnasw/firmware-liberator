# Sonoff BASICR4

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-basicr4.json)](README.md)

## Where it stops

Its firmware keeps its partition table at 0xC000, and its bootloader reads it
there. Tasmota and ESPHome, like the tool's own programs, are built to look for
it at 0x8000, so the tool does not convert it yet, and turns it down as it does
any device it does not know. Its stock firmware starts in the emulator, on a
stand-in flash with its table moved there, and is read out.

**A readout is safe, and wanted** ([`docs/readouts.md`](../../docs/readouts.md)):
the bridge (4.4.6 or later) takes the table from 0xC000 without writing anything,
only reads the device, and puts it back on Sonoff's firmware. The copy sent in has
the Wi-Fi password and eWeLink identity taken out of the records this firmware
keeps them in (its pair and factory records). It would show what nobody has seen:
the whole table, of which the stand-in guesses everything past the table itself
(`nvs` from 0xD000, the rest as the S60's).

## The device

Sonoff's ESP32-C3 inline switch, model **BASICR4**.

| | |
|---|---|
| Chip | ESP32-C3 |
| Firmware name | `FWSW-E32R4-BASICR4-ESP32C3` |
| Flash | 4 MB by the header of its official update file; nobody has read one here |
| Stock model | `SN-ESP32C3-BASICR4-01`, 1.1.0 in the public file |
| Relay | GPIO4 |
| LED | GPIO6: lit high by the Tasmota template, lit low by the ESPHome sources |
| Button | GPIO9, active low |
| Zero-cross input | GPIO5: a pulse at each mains zero crossing, read for Sonoff's Magic Switch. Left unset here |
| Thermistor | GPIO3 (ADC1 channel 3), read by its stock firmware: under 580 mV it takes itself to be overheating and refuses updates. Not seen on a board yet, and read by neither the Tasmota template nor the ESPHome config here, so a converted one would lose this protection |

Pins from [Blakadder's template](https://templates.blakadder.com/sonoff_BASICR4.html),
which agrees on the relay, button and LED pins with
[ESPHome's device page](https://devices.esphome.io/devices/sonoff-basic-r4-v1.0),
taken from [digiblur's](https://digiblur.com/wiki/devices/relays/sonoff-basic-r4/).
They disagree on whether the LED is lit high or low; this follows Blakadder's,
and the choice changes only when the LED is lit. GPIO5 is
[Tasmota's MagicSwitch input](https://github.com/arendst/Tasmota/pull/20247): the
device's zero-cross circuit drives it, so nothing here sets it as an output.
Nobody has checked the pins here, and the emulator cannot.

Blakadder's warning stands: **its circuit has no transformer, and its low-voltage
side sits at mains potential (130 V measured on 240 V mains). Never touch or
connect its pins with it on the mains.**

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py), without MagicSwitch.
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: its partition table is at 0xC000; the rest is not known. In
  the emulator, [`public_dump.py`](../../tools/espressif/emulator/public_dump.py)
  puts eWeLink's official update file on the S60's flash with its table moved
  there, the factory and pair records a paired one has, and its thermistor
  reading room temperature, so that it takes updates.
- **Calibration**: none, as it has no meter.
- **Back to Sonoff**: a readout leaves it there; the tool does not convert it yet.
- **How it is converted**: not yet. Neither Tasmota nor ESPHome needs to change.
  The likeliest way is for the bridge first to write a copy of its table at
  0x8000, which Sonoff's bootloader and app never read. The interim Tasmota then
  starts, and the tool's usual steps put ESPHome's bootloader and table in
  place, as on any device
  ([research](../../docs/espressif-research.md#converting-a-table-at-0xc000)).
  It waits on a readout of a real one, for the rest of its table and the length
  of its bootloader, which must end below 0x8000.
  [`docs/adding-a-device.md`](../../docs/adding-a-device.md) says how a device is added.
