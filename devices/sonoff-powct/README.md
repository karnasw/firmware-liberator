# Sonoff POW Ring

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-powct.json)](README.md)

## Testing

**In the emulator:** it converts, from a public dump of a stock one, to Tasmota
and to ESPHome and back to Sonoff's firmware. The emulator has none of its
hardware but its CSE7761 meter, which ESPHome reads, so the rest of its wiring
below is what its sources say, not what was seen.

**Worth checking:** whether the relay, display, button and LEDs work, and what
the meter reads against a load you know.

## The device

Sonoff's clamp-on energy meter with a small display, the **POWCT**.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSW-E32POWCT-POWCT-ESP32D0WDV3` |
| Flash | 4 MB |
| Stock model | `SN-ESP32D0-POWCT-01` |
| Relay | GPIO21 |
| Status LED | GPIO13, lit when high: on while the relay is on ([how this is known](#the-status-leds-polarity)) |
| Wi-Fi LED | GPIO15, inverted |
| Power meter | CSE7761: RX GPIO25, TX GPIO26, 38400 baud, even parity |
| Display | TM1621: CS GPIO17, data GPIO5, read GPIO23, write GPIO18 |
| Button | GPIO0, active low |

Pins from [ESPHome's page](https://devices.esphome.io/devices/sonoff-powct/),
but for the status LED's polarity. Blakadder has no template for it; the Tasmota
template in [`kinds.py`](../../tools/autoflash/kinds.py) is the one Tasmota's
CSE7761 driver gives (`xnrg_19_cse7761.ino`). Its meter's RX is CSE7761 Rx 2,
which tells Tasmota that the meter is a POWCT's, with a current clamp, and not
a Dual R3's.

### The status LED's polarity

The sources disagree on GPIO13. ESPHome's page has it lit when low, as the
template in Tasmota's TM1621 driver does (`Led_i`, 320). Tasmota's CSE7761
driver has it lit when high (`Led`, 288): Tasmota's maintainer changed it from
320 in the commit that fixed the POWCT's calibration (arendst/Tasmota
`23de275dbe`, July 2024). Sonoff's own firmware (1.1.1) settles it for high,
found without the device:

- **It drives GPIO13 as the relay.** Its relay function (at `0x400db1c8`)
  sets GPIO21 and then GPIO13 to the same level, the relay's state, through
  ESP-IDF's `gpio_set_level`; the only other code that drives GPIO13 is its
  start-up and its factory pin test. In the emulator, switching the relay
  through eWeLink (the cloud stub) sets GPIO13's output bit with GPIO21's:
  `GPIO_OUT` is `0x04262020` with the relay on and `0x04060020` with it off,
  GPIO13 a plain GPIO output (its matrix selection `0x100`). A relay LED is lit
  while the relay is on, so high is lit.
- **It inverts the LED that is lit when low.** The network LED, GPIO15, is
  driven through LEDC, and its driver (`0x40135788`, duty at `0x401357f8`)
  inverts the brightness it is given into the duty cycle, so full brightness
  is the pin held low. No such inversion is applied to GPIO13.

eWeLink's network indicator setting (`sledOnline`) is GPIO15's, and leaves
GPIO13 as it is: the emulator showed no change to it either way.

This project's [`esphome.yaml`](esphome.yaml) drives GPIO13 the same way, lit
when high, from its build 2026.10.0-3; the builds before it, never published,
had it inverted.

### The meter's clamp

On ESPHome 2026.10, the meter reads the clamp as a Dual R3's shunt, 3.2 times
too high. The config divides by π, as ESPHome's page says, which leaves the
reading about 2% high. ESPHome 2026.11.0 reads it as a clamp
(`current_input: current_transformer`), and the build with that release sets
it and drops the division:
[`test_firmware_pins.py`](../../tests/repo/test_firmware_pins.py) fails until
it does.

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: Sonoff's, the same table as the S60's
  ([`artifacts/sonoff-s60-partition-table.bin`](../../artifacts/README.md)).
- **Calibration**: not carried over.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: as the S60 is ([`docs/what-happens.md`](../../docs/what-happens.md)),
  with the firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
