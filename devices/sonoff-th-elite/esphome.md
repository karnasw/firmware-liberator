# ESPHome for the Sonoff TH Elite 20A

[`esphome.yaml`](esphome.yaml) is the ESPHome the conversion installs when you
choose ESPHome. You do not need to build it: the tool installs it pre-built,
from [`artifacts/`](../../artifacts/README.md), and writes your Wi-Fi into it in
memory only.

## What it reports

It shows on its display, and reports, the temperature and humidity of a Sonoff
THS01 on its socket. It switches the main relay from its button and over the
network, and the dry contact over the network. The main relay latches: each of
its two pins is pulsed for half a second, interlocked with the other. Its state
cannot be read back, so what is shown is what it was last told.

The main relay is off after a first start, and otherwise as last told before a
power cut; the dry contact is off at every start.

## Adopted into Home Assistant

Home Assistant's ESPHome builder can adopt a converted device from this config:
[how it works](../../docs/home-assistant-adoption.md).

## Changing it

Edit `esphome.yaml`, then build and publish it with
`python3 tools/repo/update_firmware.py esphome --device sonoff-th-elite`. Keep the
`WiFi Signal` sensor's and `Relay` switch's names, the `autoflash_wifi` action
and the two `AUTOFLASH-IMPRINT-` placeholders: the tool uses each
([how](../sonoff-s60/esphome.md#changing-it)).
