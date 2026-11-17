# Devices

Each device has a folder here, whatever its status. The status is set in one
place, [`status.json`](status.json), and shown by the badge at the top of the
device's page and in [the main README](../README.md)
([`tools/repo/device_status.py`](../tools/repo/device_status.py) makes the
badges).

| Status | In `status.json` | What it means | What the tool does |
|---|---|---|---|
| **Supported** | `supported` | A real one has been converted and reported | Converts it |
| **Testers wanted** | `testers-wanted` | It converts in the emulator, or was added from its official update file and a published pin map; no real one has been reported yet | Converts it, after a go-ahead that says so |
| **Readout wanted** | `readout-wanted` | It does not convert yet, but a readout of it through the bridge is proven in the emulator to leave it on its Sonoff firmware | Turns a conversion down; reads it out in full |
| **Not supported yet** | `not-supported` | Neither | Turns it down, as it does a device it does not know; its readout is only what Sonoff's own firmware and eWeLink say ([docs/readouts.md](../docs/readouts.md)) |

To hear when one becomes supported, [get an email as more devices are
supported](https://firmwareliberator.com/notify).

## Not converted yet

These devices' configs and templates are kept so that the work is not lost.
None has published firmware, and none has been converted for real.

| Device | Status | Where it stops, in the emulator | What would move it on |
|---|---|---|---|
| [BASICR4](sonoff-basicr4/README.md) | Readout wanted | It starts, on a stand-in flash with its partition table at 0xC000, and is read out | A readout of a real one, to learn its whole table and its bootloader's length; then a bridge that copies its table to 0x8000 before the conversion |
| [MINIR4](sonoff-minir4/README.md) | Readout wanted | It starts and is read out, with the factory and pair records it keeps; a conversion has not been tried yet | Trying a conversion in the emulator |
| [NSPanel](sonoff-nspanel/README.md) | Not supported yet | It boots from a whole dump, then turns the installer down after the file's first 24 bytes, as it wants three records, the app and two screen images ([why](sonoff-nspanel/research.md#the-nspanel-wants-three-records)) | Genuine screen images and a real panel to prove the display path on, or another way in |
| [M5 1-gang](sonoff-m5-1c/README.md), [2-gang](sonoff-m5-2c/README.md), [3-gang](sonoff-m5-3c/README.md) | Not supported yet | It goes quiet, and never starts its Wi-Fi | A whole dump of one that is not the locked Matter version |

To work on one in the emulator, set `AUTOFLASH_NOT_READY_YET=1`, and build its
ESPHome with `tools/repo/update_firmware.py esphome --device KEY`. A device
becomes **Readout wanted** once its readout runs in the emulator and leaves it
on its Sonoff firmware, and **Testers wanted** once it converts both ways there
and its firmware is published
([the whole procedure](../docs/adding-a-device.md)).
