# ESPHome for the Sonoff S60

[`esphome.yaml`](esphome.yaml) is the ESPHome the conversion installs when you
choose ESPHome. You do not need to build it: the tool installs it pre-built,
from [`artifacts/`](../../artifacts/README.md), and writes your Wi-Fi into it in
memory only.

Home Assistant adopts a converted device from this config (`dashboard_import`),
so it is one self-contained file. It includes no files of its own, only system
headers, and no partition file: an app image does not hold the partition table,
and the device keeps Sonoff's.

## What it reports

It switches the relay, from its button and over the network, and reports
voltage, current and power, and apparent and reactive power and the power
factor, which Home Assistant leaves off until you turn them on. The relay is on
after a first start, and otherwise as it was before a power cut. Over the
`Overload threshold` (3500 W unless you change it) for five seconds, it switches
the relay off, and the `Overload` sensor shows it. `Countdown` toggles the relay
after the seconds you set.

Apparent power, reactive power and the power factor are worked out each second
from the same meter readings. Reactive power is 0 while power is within 1.5%
of voltage times current, and it and the power factor change only in a second
steady with the last, because a switching load's power follows its current a
second or three behind.

Voltage, current and power are corrected as Sonoff's own firmware corrects
them, and the diagnostic `Calibration` sensor says how:

- `stock NVS, factor 1.02` (or `1.00`): the device's factory calibration, read
  at start-up from what Sonoff's firmware left in its memory, and the factor its
  board calls for on voltage and power, read from three of its pins.
- `from backup, factor 1.02` (or `1.00`): the calibration had gone from Sonoff's
  memory, as on a device converted through Tasmota, whose new layout clears it,
  so the tool restored it from the device's Sonoff backup.
- `missing, factor 1.02` (or `1.00`): no factory calibration is left, and the
  tool had no backup to restore it from, so only the board's factor applies.
  That costs little: on the devices examined so far, the factory calibration
  changes nothing below 300 W.

One of those pins, GPIO19, is also the USB port's D+, so at start-up the config
detaches the USB port from it, as Sonoff's firmware does. The log over USB stops
there; the log through the API is unaffected.

`Energy` is what the meter has counted since the device started, in Wh,
calibrated the same way. `Daily energy`, `Monthly energy`, `Yearly energy` and
`Total energy` are counted from the same pulses, so they agree with it, and are
kept through restarts and updates, including from an earlier build. The first
three start again at midnight, on the first of the month and on 1 January.

## Adopted into Home Assistant

Home Assistant's ESPHome builder can adopt a converted device. It uses this
config at the release the device's firmware came from, and your device keeps
that config until you choose a newer release:
[how it works, how to take a fix and how to customise it](../../docs/home-assistant-adoption.md).

## Changing it

Edit `esphome.yaml`, then build and publish it with
`python3 tools/repo/update_firmware.py esphome`, as for a new ESPHome release
([docs/updating-firmware.md](../../docs/updating-firmware.md)). The command
raises the config's `project` `version`, builds it in ESPHome's own Docker
image, checks the image can still be imprinted, and publishes it in
`artifacts/` with its lines in `SHA256SUMS` and `firmware.json`.
ESPHome 2026.10.0 built the current image.

The command keeps the published bootloader unless you pass `--new-bootloader`.
A new one is written to every device on its next route through D5 or L1, and a
power cut in that second leaves a device that has to be opened. A change to the
config's app alone leaves the bootloader as it is.

The tool finds these by name, so keep each as it is:

- **The `WiFi Signal` sensor.** The tool reads it before it changes the firmware
  of a device on ESPHome. If a device's ESPHome has no such sensor (an early
  build, or one from a config without it), the tool reads the signal from the
  configuration dump of the native API instead, unless the API has an
  encryption key.
- **The `Relay` switch, and the `Voltage` and `Power` sensors.** At the end of a
  run the tool switches the relay over and back, and reads the meter each time.
- **The four energy totals.** Each start finds what each had saved by its name,
  and Home Assistant keeps its history by it.
- **The `autoflash_calibration` action.** The tool gives the device's factory
  calibration through it.
- **The `autoflash_wifi` action** (`s60_wifi` on the earliest builds). It gives
  the tool the device's Wi-Fi name and password when the device leaves ESPHome:
  the next firmware needs them written in, and ESPHome gives them no other way.
  Anything that can use the API can call it, which is anything on your home
  network while the API has no encryption key, just as it could send the device
  new firmware while OTA has no password. Giving the API a key closes it; the
  tool then takes the details from the device's Sonoff backup, if your computer
  has it, or asks you for them.
- **The two `AUTOFLASH-IMPRINT-` placeholders** (`S60-IMPRINT-` on the earliest
  builds). The tool finds and fills them, and refuses an image in which either
  appears more than once.

If you add an update password later (`password:` under `ota:`), the tool asks
for it when it takes the device off ESPHome, or you can give it with
`--esphome-ota-password` or as `AUTOFLASH_ESPHOME_OTA_PASSWORD`. If you give the
API an encryption key (`encryption:` under `api:`), the tool asks for that, or
takes it from `--esphome-api-key` or `AUTOFLASH_ESPHOME_API_KEY`. It then reads
the device through the encrypted API, and sends its update encrypted where the
device takes that, as it must where `encryption:` is under `ota:`.

Keep the last `on_boot` lambda too. ESPHome saves Wi-Fi details under a key
made from the config and its version when a build has a network of its own at
start-up, as this image has once the tool writes the Wi-Fi in, and under one
fixed key when it has none. The lambda saves the Wi-Fi in use under the fixed
key as well. Without it, a build of this config with no Wi-Fi in it, such as
Home Assistant's ESPHome builder makes when it adopts the device, would find
nothing saved, and raise its own network rather than join yours.

Like every ESPHome the conversion installs, whatever the device, it is named
`esphome` and ESPHome's MAC suffix (`esphome-abcdef`), as is its own network;
its friendly name joins the two with a space (`esphome abcdef`). Its comment,
`Sonoff-S60`, says what device it is, and the tool identifies the device by
it.

[`archive/esphome-prototype/FINDINGS.md`](../../archive/esphome-prototype/FINDINGS.md)
records how this route was worked out and tested.
