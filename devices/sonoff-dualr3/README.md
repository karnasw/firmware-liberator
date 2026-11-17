# Sonoff Dual R3

## Status

[![Status](https://img.shields.io/endpoint?url=https%3A%2F%2Fraw.githubusercontent.com%2Fadamsthws%2Ffirmware-liberator%2Fmain%2Fdevices%2Fstatus%2Fsonoff-dualr3.json)](README.md)

## Testing

**In the emulator:** it converts from eWeLink's public firmware for it, put on
the TH Elite's flash, as no dump of a real one is published, with its own
settings in relay mode
([its work mode](../../docs/espressif-research.md#the-dual-r3s-work-mode)). The
emulator has none of its hardware but its CSE7761 meter, which ESPHome reads, so
the rest of its wiring below is what its sources say, not what was seen.
Its firmware (1.6.1) reports its Wi-Fi MAC to the cloud (`staMac`) as 12 hex
digits with no separators, as the emulator's stand-in for eWeLink saw: the
tool finds the device on your network by that MAC, written either way.

**Worth checking:** whether both relays, the wall switches, the button and the
LED work, and which meter chip yours has.

## The device

Sonoff's two-channel inline switch with a power meter.

| | |
|---|---|
| Chip | ESP32 |
| Firmware name | `FWSWES-E32DR3-DUALR3-ESP32D0WDV3` |
| Flash | not known |
| Stock model | not known: eWeLink's product model alone says which device it is |
| Relays | GPIO27, GPIO14 |
| LED | GPIO13, inverted |
| Power meter | CSE7761: TX GPIO25, RX GPIO26, 38400 baud, even parity; a BL0930 on units since September 2021 |
| Button | GPIO0, active low |
| Wall switches | GPIO32, GPIO33 |

Pins from [Blakadder's template](https://templates.blakadder.com/sonoff_DUALR3.html),
whose warning stands: **its GPIOs are connected to AC power. Never connect
anything to its pins with it on the mains.**

## Files

- **Tasmota**: the official release, unchanged, given the template in
  [`tools/autoflash/kinds.py`](../../tools/autoflash/kinds.py).
- **ESPHome**: [`esphome.yaml`](esphome.yaml); [`esphome.md`](esphome.md) says
  what it reports.
- **Flash layout**: expected to be the TH Elite's, the layout a
  [real one's boot log](https://community.home-assistant.io/t/help-with-flashing-sonoff-dual-r3-please/297547?page=2)
  shows. If yours is laid out differently, the run stops once the installer has
  read its partition table, before anything is written over Sonoff's firmware,
  and switching it off and on takes it back to Sonoff's. Please report that,
  with its model. Leaving Tasmota, for ESPHome or for Sonoff's firmware, it
  spends a short time on a temporary layout table, as the TH Elite does: its
  first app slot leaves no room in front of Tasmota for the firmware that moves
  it on.
- **Calibration**: not carried over.
- **Back to Sonoff**: the tool writes back the device's own backup, taken before
  anything of Sonoff's is written over; without it, Sonoff's official firmware for
  it, with the identity the device keeps or your eWeLink account holds
  ([how](../../docs/what-happens.md#changing-firmware-later)).
- **How it is converted**: as the S60 is ([`docs/what-happens.md`](../../docs/what-happens.md)),
  with the firmware for its chip. [`docs/adding-a-device.md`](../../docs/adding-a-device.md)
  says how a device is added.
