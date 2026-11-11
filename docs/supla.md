# Supla GUI-Generic

The tool can put [Supla](https://www.supla.org/)'s GUI-Generic on a Sonoff
S60 instead of Tasmota or ESPHome, over the air, from Sonoff's firmware or
from any firmware this tool installed.

## Build the firmware first

GUI-Generic is built for you, with the features you pick, so the tool comes
with none. At [gui-generic-builder-new.supla.io](https://gui-generic-builder-new.supla.io/):

- choose the **ESP32-C3** and the **S60** template (it sets the relay, LED,
  button and power meter pins);
- download the result: a `.zip` holding the firmware `.bin`, a bootloader,
  a partition table and `boot_app0.bin`.

Give the tool the `.zip` as it is, or the firmware `.bin` from it:

```sh
python3 -m tools.autoflash run --supla ~/Downloads/GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin.zip
```

`--firmware supla` without `--supla` asks for the file once the run knows
the plug. The bootloader, partition table and `boot_app0.bin` in the `.zip`
are for flashing over USB, and are not used: see
[The memory layout](#the-memory-layout).

## What the run does

The run is the ESPHome one up to the point the firmware goes in
([what-happens.md](what-happens.md)), with three steps of Supla's own:

1. **S0, before anything is written**: the file is read (a `.zip` in memory,
   never unpacked) and checked. It has to be one ESP32-C3 app that passes its
   own checksum and SHA-256, is GUI-Generic, and fits both of Supla's slots.
   A build that carries a device template has to have the S60's pins; one
   with none is taken, with a warning that it switches nothing until its pins
   are set in GUI-Generic. Its SHA-256 is recorded: a resumed run refuses a
   file that changed since.
2. **S1**: from the temporary Tasmota, which runs from the second half of the
   memory, GUI-Generic goes into the first half. It is more than one go can
   write in the time the plug stays responsive, so it is written in two; every
   block is read back and compared with the file. Then the end of the memory
   is cleared for Supla's settings, Supla's partition table is written, and
   the note that says which half to start is cleared, so the plug starts
   GUI-Generic and restarts. Until that last write it still starts Tasmota.
3. **S2**: GUI-Generic cannot be given your Wi-Fi before it starts, so it
   raises its own network, named `SUPLA-...`. The tool names it and waits
   while you join it and, at `http://192.168.4.1`, give GUI-Generic your Wi-Fi
   and your Supla server and account email. Once you are back on your Wi-Fi,
   the tool looks for the plug there by its hardware address.

If S2 sees Supla's own network but not the plug on your Wi-Fi, Supla is
still written and checked: its network stays up until it is set up, and your
router's list of devices gives its address once it has joined. If it sees
neither, the run stops rather than call the plug converted: switch the plug
off and on and run the same command again, which looks for it again.

The emulator cannot run GUI-Generic ([emulator.md](emulator.md#known-gaps)),
so the route has been tried there up to S1 only.

## The memory layout

The builder's own partition table puts the firmware at `0x10000`, over where
Sonoff's layout keeps the plug's settings and boot selection; it is meant
for a plug erased and flashed over USB. Over the air, the tool keeps
Sonoff's layout and makes room at its end for what GUI-Generic needs and
Sonoff's layout lacks:

| Area | Address | Size | What's in it |
|---|---|---|---|
| `nvs`, `reserve`, `otadata`, `phy_init` | `0x9000` | as Sonoff's | as Sonoff's |
| `ota_0` | `0x20000` | 1,984 KiB | GUI-Generic |
| `ota_1` | `0x210000` | 1,792 KiB (Sonoff's: 1,984) | GUI-Generic's next update |
| `spiffs` | `0x3D0000` | 128 KiB | GUI-Generic's settings (LittleFS) |
| `coredump` | `0x3F0000` | 64 KiB | a crash report, if it crashes |

`spiffs` and `coredump` are where, and as big as, the builder's own table
has them. A build has to fit `ota_1` (1,835,008 bytes), as an update
GUI-Generic takes later goes there. The bootloader stays the ESPHome one
the run puts on for every firmware, which starts GUI-Generic like any
ESP-IDF app.

Writing the partition table is the one moment a power cut leaves a plug
that has to be opened, as on the other routes: one sector, written in a few
milliseconds, after everything else has been read back.

## After

GUI-Generic updates itself from its own page (or from Supla's cloud) as
usual. The tool does not yet recognise a plug running GUI-Generic, so it
cannot take it on to another firmware or back to Sonoff's; that has to be
done from GUI-Generic's update page or over USB.
