# Espressif tooling

The tooling for Espressif devices, the ESP32-C3 and the classic ESP32. What it
was worked out from is in
[docs/espressif-research.md](../../docs/espressif-research.md). The tool
imports some of it during a run; the rest are scripts of their own, for
research, firmware builds or the emulator.

**The emulator**: how to use it is in
[docs/emulator.md](../../docs/emulator.md#the-espressif-emulator), and how it
works in [emulator/](emulator/README.md).

## Chips, layouts and identity

- **`chips.py`** - what differs from one Espressif chip to the next: where its
  bootloader goes, what Tasmota's files for it are called, and the firmware
  published for it.
- **`layouts.py`** - the partition tables Sonoff's firmware comes on, and where
  each keeps things.
- **`sonoff_identity.py`** - what of Sonoff's settings makes a device the one
  its eWeLink account knows, kept so that the device can go back to Sonoff's
  firmware with no backup on your computer.
- **`nvs_image.py`** - an ESP-IDF NVS partition, written as
  `nvs_partition_gen` writes one, with Python's standard library alone.
- **`ais_records.py`** - the factory and pair records in which Sonoff's newer
  "ais" SDK (the BASICR4's and MINIR4's firmware) keeps its eWeLink identity
  and Wi-Fi, where the S60's keeps `frame_params`.

## Reading a device

- **`flashmap.py`** - what a whole-flash dump holds, read from the dump itself:
  its chip, its partition table (at 0x8000, or 0xC000 where its firmware is
  built so), and the MAC from its eFuses.
- **`device_facts.py`** - what a backup says about a device, for the run's log.
- **`scrub.py`** - takes what is the owner's out of a copy of a whole flash
  before a readout leaves your computer.
- **`flash_readout.py`** - reads a stock device's whole flash over Wi-Fi, then
  puts it back to stock.
- **`dump_stock_flash.py`**, **`inspect_stock_nvs.py`** - imaging a stock
  device's flash through the bridge, and finding where its firmware keeps its
  Wi-Fi (research only).
- **`capture_tasmota_template.py`** - Tasmota's default settings from a
  converted device, as a template.

## Sonoff's update wrapper

- **`build_vendor_ota.py`**, **`analyze_vendor_ota.py`** - the 100-byte wrapper
  a stock updater takes, built around an ESP app, and checked.
- **`serve_tasmota_ota.py`** - serves a checked, wrapped app to one owned
  device.
- **`check_ota_firewall.sh`** - checks the router rules that send a device's
  update request to your computer.
- **`RECOVERY_LOCK`**, **`REPARTITION_LOCK`** - while each is there, the tools
  that update a stock device, and the server that replaces its partition table,
  refuse to run. Each says what to check before it is moved.

## Firmware built here

- **`firmware-build.Dockerfile`** - what builds the bridge and the recovery
  Safeboot, with PlatformIO (`tools/repo/update_firmware.py esp32`).
- **`safeboot/`** - the recovery Safeboot, its migration and the checks every
  phase of it shares.
