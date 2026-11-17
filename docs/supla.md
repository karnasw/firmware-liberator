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

Give the tool the `.zip` as it is. It installs all four, so the firmware
`.bin` alone is not enough:

```sh
python3 -m tools.autoflash run --supla ~/Downloads/GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin.zip
```

`--firmware supla` without `--supla` asks for the file once the run knows
the plug.

## What the run does

The run is the ESPHome one up to the point the firmware goes in
([what-happens.md](what-happens.md)), with three steps of Supla's own:

1. **S0, before anything is written**: the `.zip` is read in memory, never
   unpacked, and checked. The app has to be one ESP32-C3 app that passes its
   own checksum and SHA-256, is GUI-Generic, and fits both of Supla's slots.
   A build that carries a device template has to have the S60's pins; one
   with none is taken, with a warning that it switches nothing until its pins
   are set in GUI-Generic. The bootloader has to pass its own checksum and
   SHA-256, the partition table has to be the layout below, and
   `boot_app0.bin` has to start the first slot. Their SHA-256 is recorded: a
   resumed run refuses a file that changed since.
2. **S1**: from the temporary Tasmota, running from the second half of the
   memory, GUI-Generic goes where the builder puts it, at `0x10000`. It is
   more than one go can write in the time the plug stays responsive, so it is
   written in two; every block is read back and compared with the file. Then
   the end of the memory is cleared for Supla's settings, and the builder's
   boot selection, bootloader and partition table are written, the table
   last. The plug then starts GUI-Generic and restarts.
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

The plug ends up as flashing the `.zip` over USB would leave it: the
builder's bootloader, its partition table and its boot selection, with
GUI-Generic in the first slot. The run does not put ESPHome's bootloader on,
as it does for the other firmware: under it, on Sonoff's layout, GUI-Generic
could not mount its settings partition, so it forgot every setting at the
next restart.

| Area | Address | Size | What's in it |
|---|---|---|---|
| bootloader | `0x0` | | the builder's |
| partition table | `0x8000` | 4 KiB | the builder's |
| `nvs` | `0x9000` | 20 KiB | GUI-Generic's (cleared) |
| `otadata` | `0xE000` | 8 KiB | `boot_app0.bin`: start `app0` |
| `app0` | `0x10000` | 1,920 KiB | GUI-Generic |
| `app1` | `0x1F0000` | 1,920 KiB | GUI-Generic's next update |
| `spiffs` | `0x3D0000` | 128 KiB | GUI-Generic's settings (LittleFS) |
| `coredump` | `0x3F0000` | 64 KiB | a crash report, if it crashes |

A build has to fit a slot (1,966,080 bytes). A `.zip` whose partition table
is not this one is refused, not guessed at.

The temporary Tasmota runs from `0x210000` while all this is written, and
GUI-Generic ends before it. GUI-Generic's last 1,920 KiB go in first, from
`0x20000`, while the plug still starts Tasmota as it did. Its first 64 KiB
go in after, over Sonoff's settings and boot selection: a power cut from then
on still starts the same Tasmota, as the plug then tries each slot and finds
no other app, but Tasmota may have lost its Wi-Fi, which it shares those 64
KiB with. If so, it comes back with its own setup network, and running the
same command again sets it up and goes on.

Writing the bootloader and then the partition table are the two moments a
power cut leaves a plug that has to be opened, as writing the bootloader is
on the other routes: each a few sectors, written in a few milliseconds,
after everything else has been read back.

## After

GUI-Generic updates itself from its own page (or from Supla's cloud) as
usual. The tool does not yet recognise a plug running GUI-Generic, so it
cannot take it on to another firmware or back to Sonoff's; that has to be
done from GUI-Generic's update page or over USB.
