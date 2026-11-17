# Sonoff TH Elite 20A

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-th-elite.json)](README.md)

## Testing

**In the emulator:** it converts, from a public dump of a stock one, to Tasmota
and to ESPHome and back to Sonoff's firmware. The emulator has none of its
hardware, so its wiring below is what its sources say, not what was seen.

**Worth checking:** whether the relay, display, sensor, button and LEDs work,
and whether a run leaving Tasmota came through its temporary layout table.

## The device

Sonoff's thermostat switch with a socket for a sensor and a display, the
**THR320D**. Its stock model, below, is also the THR316D's, which is wired
otherwise: the tool converts a device eWeLink calls a THR320D, and no other.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSW-E32THR3-THR3-ESP32D0WDV3` |
| Flash | 4 MB |
| Stock model | `SN-ESP32D0-THR3-01` |
| Main relay | latching: a pulse on GPIO19 turns it on, on GPIO22 off. **Both on together overheats the device** |
| Dry contact | GPIO4 |
| LEDs | GPIO16, GPIO15, GPIO13, inverted |
| Display | TM1621: CS GPIO17, data GPIO5, read GPIO23, write GPIO18 |
| Sensor socket | data GPIO25, power GPIO27 |
| Button | GPIO0, active low |

Pins from [ESPHome's page](https://devices.esphome.io/devices/sonoff-thr320d/)
and [Blakadder's template](https://templates.blakadder.com/sonoff_THR320D.html),
which agree.

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: its own
  ([`artifacts/sonoff-th-elite-partition-table.bin`](../../artifacts/README.md)),
  with smaller app slots, so it runs a smaller Tasmota part way through. Its
  first app slot leaves no room in front of Tasmota for the small firmware that
  moves a device off it. So a TH Elite leaving Tasmota, for ESPHome or for
  Sonoff's firmware, spends a short time on a temporary layout table, and always
  goes through the full Tasmota that writes its own table back. A power cut then
  does no harm: run the tool again and it carries on.
- **Calibration**: none, as it has no meter.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: as the S60 is ([`docs/what-happens.md`](../../docs/what-happens.md)),
  with the firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
