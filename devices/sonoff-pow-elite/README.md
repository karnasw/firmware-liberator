# Sonoff POW Elite 16A

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-pow-elite.json)](README.md)

## Testing

**In the emulator:** it converts, from a public dump of a stock one, to Tasmota
and to ESPHome and back to Sonoff's firmware. The emulator has none of its
hardware but its CSE7766 meter, which Tasmota and ESPHome both read, so the rest
of its wiring below is what its sources say, not what was seen. Sonoff's
firmware 1.0.0 reads the meter too (230.08 V and 99.99 W with the relay on,
through eWeLink), as the emulated board holds GPIO23 low, the pin by which that
firmware knows a 316D ([below](#which-relay-sonoffs-firmware-drives)).

**Worth checking:** whether the relay, display, button and LEDs work, and what
the meter reads against a load you know.

## The device

Sonoff's DIN-rail power meter and switch with a small display, the **POWR316D**.
Its stock model, below, is also the 20 A POWR320D's, whose relay is a latching one
on other pins: the tool converts a device eWeLink calls a POWR316D, and no other.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSW-E32POWP-POWPRO-ESP32D0WDV3` |
| Flash | 4 MB |
| Stock model | `SN-ESP32D0-POWR3-01` |
| Relay | GPIO13 |
| Status LED | GPIO18, inverted |
| Wi-Fi LED | GPIO5, inverted |
| Power meter | CSE7766 on UART, RX GPIO16, 4800 baud, even parity |
| Display | TM1621: CS GPIO25, data GPIO14, read GPIO26, write GPIO27 |
| Button | GPIO0, active low |

Pins from [ESPHome's page](https://devices.esphome.io/devices/sonoff-pow-elite-16a/)
and [Blakadder's template](https://templates.blakadder.com/sonoff_POWR316D.html),
which agree.

### Which relay Sonoff's firmware drives

Sonoff's firmware is the same for both models. It tells them apart by GPIO23,
not by its settings or factory data, which say POWR316D on either. At start-up
it reads GPIO23 as an input with its pull-up on:

- **Low**: it drives the 316D's relay, GPIO13, high for on.
- **High**: it drives the 320D's latching relay, with a 35 ms pulse on GPIO2 for
  on or on GPIO4 for off, then both low.

GPIO18's LED follows the relay either way. So a 316D's board must hold GPIO23
low, and a 320D's leaves it to the pull-up.

This was seen in 1.0.0's code (the function that reads GPIO23, and the relay's
switch, which branches on what it found) and in the emulator. With every input
reading high there, as at first, the firmware pulsed GPIO2 and GPIO4 and never
raised GPIO13. The emulated load never came on, and the firmware took none of
the meter's frames that said there was no load, voltage and all: it reported
0 V and 0 W. The emulated board now holds GPIO23 low (`HELD_LOW` in
`tools/espressif/emulator/board.py`).

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: Sonoff's, the same table as the S60's
  ([`artifacts/sonoff-s60-partition-table.bin`](../../artifacts/README.md)).
- **Calibration**: not carried over. Its factory values are in the stock settings
  as the S60's are, but how its firmware applies them has not been read, so a
  converted one measures as Tasmota or ESPHome does uncalibrated.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: as the S60 is ([`docs/what-happens.md`](../../docs/what-happens.md)),
  with the firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
