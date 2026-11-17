# ESPHome for the Sonoff MINI Extreme

[`esphome.yaml`](esphome.yaml) is the ESPHome a conversion would install. The
tool does not convert this device yet ([why](README.md#where-it-stops)), so none
is published; to try it in the emulator, build it as [Changing it](#changing-it)
says.

## What it reports

It switches the relay, from its button, a wall switch and over the network.

The relay is off after a first start, and otherwise as it was before a power cut.

## Adopted into Home Assistant

Home Assistant's ESPHome builder can adopt a converted device from this config:
[how it works](../../docs/home-assistant-adoption.md).

## Changing it

Edit `esphome.yaml`, then build and publish it with
`python3 tools/repo/update_firmware.py esphome --device sonoff-minir4`. Keep the
`WiFi Signal` sensor's and `Relay` switch's names, the `autoflash_wifi` action
and the two `AUTOFLASH-IMPRINT-` placeholders: the tool uses each
([how](../sonoff-s60/esphome.md#changing-it)).
