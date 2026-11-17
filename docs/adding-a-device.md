# Adding a device

How to add a device to the tool, and prove it in the emulator before it goes near
a real one. What every device shares comes first. Each chip family then has a
part of its own, laid out alike: what you need, the steps, what the emulator does
for you, what a run tells you, and what to do when it does not boot.

## For every device

- **Nothing of Sonoff's is committed.** No Sonoff firmware or dump goes into the
  repository: the tooling fetches what it needs at run time, by the name and
  hash it pins.
- **Pins are never guessed.** Take them from a source that has checked them on
  a device, and say on the device's page where they came from.
- **Run the tests, and add the ones the device needs.**
  `tests/autoflash/test_kinds.py` covers telling it apart from its neighbours.
  Updates need no wiring in: the weekly update builds every device's ESPHome
  with one release, and moves each chip's Tasmota with the ESP32-C3's
  (`update_firmware.py`'s `esphome_command`). A test fails if it cannot tell how
  to build a device's firmware, or if its chip has no Tasmota that follows.
- **Give it a run in CI.** A device the tool converts has its own run of the
  `emulator-check` workflow, from its public dump: each final firmware, then
  each device taken to the other
  ([emulator_check.py](../tools/repo/emulator_check.py)). A Beken device with
  no public dump has its run from a flash rebuilt from the S61's dump and its
  official file, once that file is pinned ([its steps](#the-steps-beken)). One with
  nothing to run from is named in that file's `NOTHING_TO_RUN`, or a test
  fails.
- **Be plain about what was tested.** The emulator has no relays, buttons, LEDs
  or screens, and of meters only the ones it emulates. A conversion passing
  there says the firmware installs and starts, not that the pins are right.
- **Run no more emulators at once than the computer has cores**: more gives
  false failures.
- **If it will not convert yet**, keep the work: its folder stays in
  `devices/`, with its status `readout-wanted` or `not-supported`, and
  [devices/README.md](../devices/README.md) says what is holding it up. The
  tool then turns it down.

### Its status

Each device has one status, in [`devices/status.json`](../devices/status.json)
and nowhere else. [devices/README.md](../devices/README.md) says what each means
and what the tool does with it. The warning before a first write is the same
for every device wanting testers (`tools/autoflash/testers.py`).

After changing the file, run `python3 tools/repo/device_status.py`. It writes
each device's badge file under `devices/status/`, with the words and colour of
its status. The badges read those files from GitHub, so they show what is on
`main` once it is pushed, not your local edits.

A device's README says its status only through its badge, at the top of its
`## Status` section, and the main README lists the device in its table with the
same badge. `tests/repo/test_device_status.py` fails if any of these disagree
with the file, or if a README says a status in words.

**When a report comes in** of a real one converted, change its line in
`devices/status.json` to `supported` and run `tools/repo/device_status.py`. That
changes what the tool does and every badge.

### Its pages

Write `devices/<key>/README.md` and `esphome.md`, laid out as the other devices'
of its chip family are: `tests/repo/test_device_pages.py` holds the sections,
table rows and bullets they share. The README starts with its status, and its
table has a `| Firmware name |` row after its chip, which is the kind's
`wrapper_model` (`tests/repo/test_firmware_names.py` holds the two alike).

### Writing down what you found out

So that the next person need not find it out again, what is true of one device
goes in a `research.md` in its folder, listed in
[device-research.md](device-research.md); a test fails until it is. What holds
for its whole chip family goes in [espressif-research.md](espressif-research.md)
or [beken-research.md](beken-research.md).

## An Espressif device

### What you need (Espressif)

- **A whole-flash dump of the device on stock firmware, if you can find one.**
  [FlashDumps](https://github.com/openshwprojects/FlashDumps) has several, and
  an owner's [readout](readouts.md) is one. A dump has the device's real
  partition table, settings and factory data, so what you test is what the
  device has.
- **Failing that, eWeLink's public update file for it.** It runs on another
  device's flash (a *donor*), which tests the firmware but not the device's own
  layout. Some firmware will not get far this way: see
  [When it does not boot](#when-it-does-not-boot-espressif).
- **Its wiring**, as a Tasmota template and an ESPHome config. Blakadder's
  template list and ESPHome's device list are the usual sources.
- **A device that is not locked.** Secure boot or flash encryption rules a
  device out: a dump that looks encrypted, or eWeLink's newer FOTA container for
  ESP firmware, is the sign. Matter versions of a device are usually locked.

### The steps (Espressif)

1. **Make a test copy.** Add the device to `DUMPS` or `UPDATES` in
   [public_dump.py](../tools/espressif/emulator/public_dump.py), with the file's
   hash and an unused number, then make it:

   ```sh
   .venv/bin/python tools/espressif/emulator/public_dump.py sonoff-example
   ```

   This puts it under `captures/devices/`, with a made-up identity in place of
   the device's. After a change to how it makes copies, run it again: a copy
   that differs from what it makes now is made again. For an update file, give it the model its firmware calls
   itself (`strings` finds it, in the shape `SN-...`, `E32-...` or
   `ESP32D0-...`), and its eWeLink kind (`uiid`) where that is not the donor's.
   The donor's user settings (`user_usual`) are another firmware's: where the
   device's own firmware reads a setting that changes how it runs, give it
   `settings`, as the Dual R3 has its work mode.

2. **Add the kind** to [kinds.py](../tools/autoflash/kinds.py): its chip,
   eWeLink product model, stock model, the firmware name its update wrapper
   names, its Tasmota template, its partition layout, whether it has a relay
   and a power meter (`has_relay`, `has_meter`), and what its sensors measure
   (`sensors`, `()` for none): every kind gives these four. Pin Sonoff's update
   file for it, as FlashDumps publishes it, in `COPIES` in
   [public_copies.py](../tools/autoflash/routes/public_copies.py), by name and
   SHA-256: a device going back to Sonoff's firmware with no backup and nothing
   from eWeLink takes it. Set `product_model_decides` if another
   device shares its stock model but is wired differently, so that it is never
   mistaken for that one. Give it a [status](#its-status): `not-supported`
   until its readout runs in the emulator, then `readout-wanted` until step 6
   passes.

3. **Boot it in the emulator** with the cloud stub, and watch for it to come
   online (step 6 runs this too, and shows what it logged):

   ```sh
   tools/espressif/emulator/emu.sh --stub-cloud --fresh --device 12:34:56:e3:2d:NN
   ```

4. **Add its partition layout**, if it is new, to
   [layouts.py](../tools/espressif/layouts.py). A dump's table is in its flash
   at `0x8000`. The tool refuses a real device whose table it does not know
   before it overwrites anything, so an unknown layout stops safely. To leave
   Tasmota, the mover goes at `0x20000`. If your device's `ota_0` is elsewhere,
   as the TH Elite's is, give the layout a `temporary_sha256`: its temporary
   table is built at run time, with `ota_0` at `0x20000`, and
   `tests/espressif/test_layouts.py` holds it to that hash. Its `ota_1` has to
   be where the device's own table has it.

5. **Add its firmware.** Write `devices/<key>/esphome.yaml` and
   [its pages](#its-pages), then build and pin the firmware. `artifacts/`
   changes only this way:

   ```sh
   .venv/bin/python tools/repo/update_firmware.py esphome --device sonoff-example
   ```

6. **Convert it both ways**, each from a fresh copy, and then back to Sonoff's
   firmware from each:

   ```sh
   tools/espressif/emulator/unattended-test.sh --stub-cloud --fresh \
       --device=12:34:56:e3:2d:NN --save=example-tas -- --firmware tasmota
   tools/espressif/emulator/unattended-test.sh --stub-cloud --fresh \
       --device=12:34:56:e3:2d:NN --save=example-esp -- --firmware esphome
   tools/espressif/emulator/unattended-test.sh --stub-cloud \
       --device=12:34:56:e3:2d:NN --from=example-tas -- --firmware sonoff
   ```

7. **Finish as [for every device](#for-every-device)**: the tests, what was
   tested, and what you found out.

### What the emulator does for you (Espressif)

No device needs a patch of its own for these: each is found in the firmware,
not listed by address. The scripts are in `tools/espressif/emulator/`, except
`make_ca.py`, which is in `tools/ewelink/stub_cloud/`.

| What | Why | Where |
|---|---|---|
| Bluetooth start-up skipped | QEMU has no Bluetooth; the functions calling `esp_bt_controller_init` return at once | `patch_stock.py` |
| DPORT stall skipped (classic ESP32, ESP-IDF 4) | a workaround for a silicon bug QEMU does not have; the other core can be too slow to answer | `patch_stock.py` |
| eWeLink's CA replaced with the stub's | the stub has to be trusted. RSA where the firmware's is RSA, or its stack overflows checking an EC signature | `patch_stock.py`, `make_ca.py` |
| Wi-Fi and cloud settings rewritten | points the device at the emulator's network and stub | `rewrite_nvs.py` |
| Chip revision (ESP32-C3) | taken from the app's header, or its bootloader refuses to start it | `public_dump.py` |

Set `psram_mb` on the kind if its stock firmware stops at start-up without its
external RAM.

### What a run tells you (Espressif)

Every backup logs what it found in the run's `run.log`, and on the screen too
when the partition table is not one the tool knows: the chip and its revision,
whether secure boot and flash encryption are on, the flash size, the partition
table, and each app's version, ESP-IDF and the firmware name its updater checks.
The installer adds what a backup cannot hold, such as the exact chip revision,
the flash chip's id and the input pins' levels
([what it logs](espressif-research.md#what-a-backup-logs)). None of it is
secret, so a report of a device the tool stopped on is enough to start adding
it.

#### Learning the firmware name

A device's stock updater refuses a file wrapped for another firmware name, and
the names vary too much to guess: `FWSW-E32S60-S60-ESP32C3FN4`,
`FWSWES-E32DR3-DUALR3-ESP32D0WDV3`, `FWPNL-E32NSP-NSPANEL-ESP32D0WDV3`. A wrong
name does no harm: the device reports a failed update and stays on Sonoff's
firmware.

Before anything is written, each run tries to learn the name, and logs it in
`run.log` with the chip it implies. It does so on a device the tool then turns
down too, so a report of one already has its name.

1. **The file eWeLink offers.** eWeLink offers nothing to a device on the latest
   firmware, so the tool also asks with the device's own id and version `0.0.1`,
   which gets it the latest. It reads only the first bytes of that `user1.bin`,
   and never logs the link or the id. Nothing changes on the device: only
   eWeLink is asked. The id must be the device's own, as a made-up one gets an
   unrelated file.
2. **The public file list.** If that gives no name, the tool looks in the
   filenames of the
   [public update files](https://github.com/openshwprojects/FlashDumps/tree/4d3556605da2ce101382a01b6fb7d84e68c3df2a/IoT/zOTAz/eWeLink),
   matching eWeLink's product model, then the model the stock firmware names. It
   gives no name unless exactly one fits.

The S60's name read from eWeLink matched its `wrapper_model`.
[What eWeLink answers to each way of asking](espressif-research.md#learning-the-firmware-name)
is in the research notes.

For a kind the tool knows, a name that differs from its `wrapper_model` is shown
on the screen too, and the conversion goes on as before. A run that cannot learn
the name goes on without it. A run on the cloud stub learns nothing, as the stub
offers no file and the run does not look further.

To use the name in your own code, call `firmware_name.learn()` in
[firmware_name.py](../tools/ewelink/firmware_name.py). It returns the name, its
chip and where it came from.

### When it does not boot (Espressif)

Read `captures/emulation/state/logs/uart.log` (`state-N` with `--instance=N`).

| You see | It means | Do |
|---|---|---|
| `abort()` straight after `Disabling RNG`, no other message | external RAM missing | set `psram_mb` |
| `This chip is revision 0 but ...` | eFuses too old | make the copy again: `public_dump.py` sets it |
| `A stack overflow in task colink task` | stub CA is a heavier type than the firmware's | see the CA row above |
| `Interrupt wdt timeout` at start-up | a core waiting for the other | look for a new cross-core wait; do not stretch the watchdog |
| `factory mode`, `factory pid not set` | factory data only a real unit has: on Sonoff's ais SDK (`aisFactoryLoadInfo` in the firmware), a record of its own | on the ais SDK, `ais_sdk=True` in `public_dump.py` writes one; else a whole dump is needed |
| Both CPUs idle after `factory?`, no Wi-Fi | it is waiting for something the donor lacks | a whole dump is needed |
| `ESP_ERR_NOT_FOUND` from `espInit` | a partition or setting the donor lacks: on the BASICR4, its partition table, built to be at 0xC000 | `table_at` in `public_dump.py`; else a whole dump is needed |
| The upgrade answered `"error": 407` | the firmware refuses updates: on the BASICR4, it takes its thermistor, read as 0 V, to be overheating | the kind's `adc_readings` |
| `GET /dispatch/device` over and over, never `/api/ws` | the firmware's JSON parser turned the stub's answer down | the stub answers compact JSON, as eWeLink's must be: check what changed |

## A Beken device

### What you need (Beken)

- **eWeLink's official update file for it**, from
  [FlashDumps](https://github.com/openshwprojects/FlashDumps), which publishes
  it. A run uses the file eWeLink offers the device first, as on the Espressif
  route, and this copy, pinned by hash in `COPIES`
  ([public_copies.py](../tools/autoflash/routes/public_copies.py)), where eWeLink
  offers none that passes the same checks. A whole-flash dump of the device, where there is one, has its factory data
  too; without one, the file's app runs on the S61's bootloader and settings.
- **A pin map you can trust**, such as an OpenBeken template its author checked
  on hardware. A device stays out without one
  ([the candidates](#the-candidates-and-why-only-one-was-added)).
- **A BK7238 on the 4 MB table**, the S61's (`beken-4m` in
  [layouts.py](../tools/beken/layouts.py)). The 2 MB devices, the door sensor
  among them, are another firmware family, on hold
  ([why](beken-research.md#2-mb-devices-on-hold)).

### The steps (Beken)

1. **Make a test copy.** Build a whole flash from the S61's public dump and the
   device's official file, and run
   [stock_points.py](../tools/beken/stock_points.py) on the file: it names each
   point the emulator needs that it cannot find
   ([below](#what-the-emulator-does-for-you-beken)).

   ```sh
   python3 tools/beken/rebuild_dump.py S61-DUMP.bin OFFICIAL.bin captures/device.bin
   ```

2. **Add the kind** to [kinds.py](../tools/autoflash/kinds.py): its chip,
   eWeLink product model, stock model, the firmware name its update wrapper
   names, its layout (`beken-4m`), whether it has a relay and a power meter
   (`has_relay`, `has_meter`) and what its sensors measure (`sensors`). Give
   it the [status](#its-status) `testers-wanted`, as no real one has been
   converted.

3. **Boot it in the emulator**, from that flash, and watch for it to come online
   on the stub:

   ```sh
   AUTOFLASH_BEKEN_EMU_FLASH=captures/device.bin tools/beken/emulator/beken-emu.sh --fresh device
   ```

4. **Pin its official update file** in `COPIES` in
   [public_copies.py](../tools/autoflash/routes/public_copies.py), by name and SHA-256.
   A run checks the file's partition table before it writes anything
   ([below](#what-the-tool-checks)), and sends the file when the device goes back
   to Sonoff's firmware.

5. **Add its firmware.** Write its table for the tool's OpenBeken driver,
   `devices/<key>/drv_autoflash_<key>.c`
   ([the driver](../tools/beken/openbeken/README.md)), its `esphome.yaml` and
   [its pages](#its-pages). Then build and pin both firmwares:

   ```sh
   .venv/bin/python tools/repo/update_firmware.py openbeken --device sonoff-example
   .venv/bin/python tools/repo/update_firmware.py esphome --device sonoff-example
   ```

6. **Convert it** to each firmware and back, with the tool run on the emulated
   device's Wi-Fi
   ([the Beken emulator](testing.md#the-beken-emulator)):

   ```sh
   tools/beken/emulator/beken-emu.sh computer tools/autoflash/cli.py run --unattended --env-file captures/tool.env --firmware esphome
   ```

7. **Finish as [for every device](#for-every-device)**. A conversion passing in
   the emulator proves the firmware and the tool's path, not the device's
   hardware or its pin map, as it runs on the S61's bootloader and settings.
   Its run in CI makes the same flash for itself, from the two files fetched
   by name and hash, and keeps it in `captures/emulation/beken/rebuilt/`:

   ```sh
   python -I tools/repo/emulator_check.py sonoff-example
   ```

The [Sonoff Basic Gen5](../devices/sonoff-basic-1gs/README.md) was the first
device added this way.

#### What the tool checks

A device added from its official file, with no dump, is offered for these
reasons, and with these questions left open:

- **The file's partition table is checked first.** Each app holds the SDK's
  table, as entries of five words: owner, description, start, length, options.
  `layouts.py` reads it by the description strings, and a run refuses a file
  whose table is not listed there, at B1, again at B2, and when Sonoff's
  firmware is sent back. The one known is `beken-4m`: `Application`
  `0x11000+0x250000`, `ota` `0x286000+0x15a000`, `RF Firmware` `0x3e0000`,
  `NET info` `0x3e1000`, `NVS` `0x3e2000+0xb000`.
- **Before a conversion, that table is the only word on the flash.** The stock
  register message gives only `romVersion`, `chipid` (its MAC) and `model`; the
  account record's `params` add the Wi-Fi and the relay's settings; the
  firmware name ends in `BK7238`, where an Espressif one can end in `FN4`. The
  app reads the flash's ID for itself (`[Flash]id:` on the serial output) but
  tells no one, and this route has no LAN API.
- **Published specs disagree with the table.** CNX Software's review and others
  give the Basic Gen5, the BASIC-1GSP and the T6 "2 MB flash (SiP)", the chip's
  usual figure, while their official files hold only the 4 MB table. The file
  is what the device runs, so the tool goes by it, but nobody has read the
  flash of one of these
  ([the evidence](../devices/sonoff-basic-1gs/research.md#its-flash-size)).
- **After the conversion the tool can tell** ([below](#what-a-run-tells-you-beken)).
- **You are told.** The go-ahead at B2 says no real one has been proven yet and
  asks for the run's result, as on every device wanting testers; `PROCEED` goes
  on as on any other.

#### The candidates, and why only one was added

All are BK7238 on the 4 MB table, and each file's stock firmware comes online on
the stub.

| Device | Firmware name, stock model | Pins | Added |
|---|---|---|---|
| Basic Gen5 `BASIC-1GS` | `FWSW-01-BASICR5-BK7238`, `SN-BK7238-BASICR5-01` | [one person's template](https://www.elektroda.com/rtvforum/topic4167932.html), in OpenBeken's device list | yes |
| DIN rail `BASIC-1GSP` | `FWSW-01-BASIC1GSP-BK7238`, `SN-BK7238-BASIC1GSP-01` | none published. It has a meter, and forum questions describe a latching relay driven by pulses, which a wrong map could harm | no |
| `S41STPB` (S41s, US) | `FWSW-01-S41-BK7238`, `SN-BK7238-S41-01` | none published. Built from the S61's source tree (`BK7238_S61_S41`), which does not make its pins the S61's | no |
| T6 `T6-1C/2C/3C-120M` (TX Gen2) | `FWSW-06-SWITCHES-BK7238`, `SN-BK7238-T6C3-01` | [one two-pad unit's](https://github.com/saltech1/esphome-sonoff-t6-uart): relays P6 and P26, LED P9. No third relay is known, and its touch pads talk over a UART protocol (P1), which OpenBeken has no driver for | no |

### What the emulator does for you (Beken)

The emulator's help for Sonoff's Beken firmware (the integrity gate, the cloud
dial, the trust-anchor swap) goes where
[stock_points.py](../tools/beken/stock_points.py) finds it in the firmware
itself, so another version or model needs nothing moved
([one code path](beken-research.md#one-code-path-for-every-4-mb-ewelink-build)).

It needs a whole flash to run from. The app of a BK7238 official file runs on
another device's bootloader and settings
([rebuild_dump.py](../tools/beken/README.md#rebuilding-a-dump)), but only the
device's own whole flash has its factory data. On a rebuilt flash the
bootloader, radio data and settings are the S61's. The stock firmware's model
comes from its sealed factory data, so it says it is an S61
(`SN-BK7238-S61-01`), and the stub gives the model the app itself holds instead
(`cloud_stub.py`). Whether a real one reports that model is not known: if not,
B1 stops with nothing written and the model in the log.

### What a run tells you (Beken)

Stock Beken firmware says nothing of its flash, so each run logs what it learns
in `run.log`: at B1, the official file's firmware name, version and whole
partition table, and the version the device reports; at B4, whether OpenBeken
found the device's own factory record at `0x3e0000`, with the device's own MAC.
On a smaller flash that record would not be there, so finding it is logged as
the flash matching the table
([what it logs](beken-research.md#what-a-backup-logs)). The run learns the
firmware name from the public file list
([as above](#learning-the-firmware-name)).

### When it does not boot (Beken)

`stock_points.py` names each point it cannot find in a firmware. The door
sensor's firmware is another stack: its points are not found, and the tools
refuse it by name ([rebuild_dump.py](../tools/beken/README.md#rebuilding-a-dump)).
