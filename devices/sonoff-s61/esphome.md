# ESPHome for the Sonoff S61

[`esphome.yaml`](esphome.yaml) is the ESPHome the conversion installs on an S61
when you choose ESPHome, built on [LibreTiny](https://docs.libretiny.eu/) for its
BK7238. You do not need to build it: the tool installs the pre-built image in
[`../../artifacts/`](../../artifacts/README.md). It is the S60's config
([`devices/sonoff-s60/esphome.yaml`](../../devices/sonoff-s60/esphome.yaml)) with the
S61's wiring and storage. Nothing is written into the image: it reads your
Wi-Fi from the stock store at boot ([how it differs](#how-it-differs-from-the-s60s)).

## What it reports

The same entities, actions, calibration handling and energy totals as
[the S60's](../sonoff-s60/esphome.md#what-it-reports), so the two devices look alike in
Home Assistant. The `Calibration` sensor says `stock store`, `from backup` (given
through `autoflash_calibration`, nine numbers) or `missing`.

## Adopted into Home Assistant

Home Assistant's ESPHome builder can adopt a converted device. It uses this config,
at the release the device's firmware came from, and your device keeps that config until
you choose a newer release: [how it works, how to take a fix and how to customise
it](../../docs/home-assistant-adoption.md).

## Changing it

Edit `esphome.yaml`, then build and publish it with
`python3 tools/repo/update_firmware.py esphome --device sonoff-s61`
([docs/updating-firmware.md](../../docs/updating-firmware.md)). Keep the names of
the `WiFi Signal` sensor, the `Relay` switch, the four energy totals and the
`autoflash_wifi` and `autoflash_calibration` actions: the tool and Home Assistant use
each, as [the S60's page](../sonoff-s60/esphome.md#changing-it) says. This image has
no `AUTOFLASH-IMPRINT-` placeholders.

ESPHome 2026.10.0 built the current image, `artifacts/esphome-sonoff-s61-2026.10.0-3.rbl`,
with `tools/repo/update_firmware.py esphome 2026.10.0 --device sonoff-s61`.

To build it by hand, use ESPHome's own Docker image with the config alone in a
directory, as for the S60. The first build fetches LibreTiny, its Beken SDK
and the toolchain.

```sh
mkdir build && cp devices/sonoff-s61/esphome.yaml build/ && cd build
docker run --rm -u "$(id -u):$(id -g)" -e HOME=/config -v "$PWD":/config \
    ghcr.io/esphome/esphome:2026.10.0 compile esphome.yaml
```

The outputs, in `.esphome/build/esphome/.pioenvs/esphome/`:

- `image_bk7238_app.ota.rbl` is the **rt_ota update** (about 500 KB) for the
  conversion, which the stock bootloader applies from the download slot.
  Wrapped with [`tools/beken/fota_wrap.py`](../../tools/beken/fota_wrap.py) it
  is what a stock device downloads; given to OpenBeken's `ota_http` it
  converts a device on OpenBeken.
- `firmware.uf2` is LibreTiny's flasher and OTA image, which ESPHome's own OTA
  (and Home Assistant's ESPHome builder) sends to a device already on ESPHome.
- `board.json` is the layout the build used. Check that it says `download:
  0x286000+0x15A000` and `kvs: 0x3EE000+0x8000`.

## How it differs from the S60's

- **Wi-Fi comes from the stock store, not from the image.** The S60's image
  has two placeholders the tool fills with the device's Wi-Fi. The S61's image
  is an rt_ota update (gzip, AES with RT-Thread's public key), which cannot be
  patched in place, and does not need to be: the device's Wi-Fi is still in the
  stock EasyFlash store at `0x3e2000`, which no conversion touches. At boot
  (priority 1000, before Wi-Fi) the config reads `wifi-ssid` and `wifi-pass`
  from it with `lt_flash_read`, as the tool's OpenBeken driver does, and sets
  them as the station. After Wi-Fi is up (priority -100) they are saved the way
  the S60's are, under both keys ESPHome looks in.
- **The factory meter calibration** is read from the same store at the same
  time (`elecCalibCoef`: magic, flag, then `a, b, c` for current, voltage and
  power), and applied as the stock firmware applies it: the BL0937's pulse
  rate reckoned with a 1.1 V reference where the datasheet (and ESPHome) say
  1.218 V, then `a*x^2/1e7 + b*x/1e6 + c/1e6` on the reading in A, V or W
  ([the derivation](research.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping)).
  It is one set of nine numbers, where the S60 has two sets, for below and
  above 300 W.
- **The wiring** is the OpenBeken template's (S61STPF / `WBBK_01P_V1.3`): relay
  P26, button P9 (active low, pull-up), relay LED P0 and Wi-Fi LED P1 (both lit
  low), BL0937 CF P8, CF1 P6, SEL P24, with the 1 mΩ shunt and 2040:1 divider.
  `hlw8012` reads the meter. It alternates CF1 between current and voltage
  (`change_mode_every: 1`, every 2 s), so each refreshes every 4 s.
- **SEL is `inverted`.** `hlw8012` drives SEL high for the current, as an
  HLW8012 takes it, but a BL0937 measures the voltage with SEL high, as
  ESPHome's `hlw8012` page says for this model. Before 3.0.0 SEL was not
  inverted and each reading was the other's: about 20 A whatever the load, and
  a voltage that followed it, 5 V at 100 W. The emulated meter showed it
  ([the emulator's board](../../tools/beken/emulator/README.md#the-devices-board-boardpy)).
- **The flash layout.** LibreTiny's generic BK7238 board has the SDK's 2 MB
  layout. The config overrides it through `platformio_options` (PlatformIO's
  `board_flash.*` and `board_build.*`) to the S61's stock bootloader table:

  | Part | Where |
  |---|---|
  | app | `0x11000+0x275000` (`0x250000` logical, so `bkrbl_size_app`) |
  | download | `0x286000+0x15A000`: the slot the stock bootloader applies an update from, so ESPHome's own updates, which LibreTiny writes there, go through it too |
  | radio calibration | `0x3e0000` |
  | ESPHome's preferences (FlashDB) | the empty 32 KB at `0x3ee000` |
  | LittleFS (unused) | the sector at `0x3e1000`, where the interim firmware left its settings |

  The stock EasyFlash stores at `0x3e2000` and `0x3f6000` are left alone, so
  the device can always go back to stock.
- **No backup reader.** The tool backs an S60 up from its stock firmware, but
  the S61's stock firmware has no way to read its store out, and ESPHome reads
  no flash. So the tool installs OpenBeken first, as the interim firmware,
  takes the backup through its flash reads (`/api/flash`), and has it fetch
  this image. Nothing in the image reads the device out, so nothing on the
  network can after the conversion.

Nothing in the S61 route needs a change to LibreTiny or ESPHome: ESPHome
2026.10.0 pins LibreTiny 1.13.0, which has the BK7238.

## Reverting to stock, and to OpenBeken

A device on ESPHome takes updates through ESPHome's OTA as UF2 files, and
LibreTiny writes a UF2's `download` part to the download slot. So a UF2 whose
download part is the stock app RBL, sent through the ordinary ESPHome OTA, is
a reversion: the stock bootloader applies it on the reboot that follows. The
RBL is rebuilt from a dump with
[`tools/beken/stock_app.py`](../../tools/beken/stock_app.py)
([how](../../tools/beken/openbeken/README.md#reverting-to-stock)), and
[`tools/beken/uf2.py`](../../tools/beken/uf2.py) writes the UF2
(`uf2.write(rbl, name="sonoff-stock", version="1.0.2")`), byte for byte what
ltchiptool, LibreTiny's tool, writes for the S61's board:

```sh
ltchiptool uf2 write -o stock_revert.uf2 -b board.json -L 1.13.0 \
    -F "sonoff-stock:1.0.2" --legacy "stock_app_1.0.2.rbl=device:download"
```

The same with OpenBeken's 4 MB-table `.rbl` moves the device to OpenBeken, and
OpenBeken's ordinary `ota_http <url of the ESPHome .rbl>` brings it back,
because its 4 MB-table build downloads into the same slot.

The stock EasyFlash store is never touched, so whichever firmware comes up
reads its Wi-Fi, calibration and wiring from it on first boot (OpenBeken
through its driver, ESPHome through its boot lambdas), and nothing is typed or
copied across. Each firmware's other settings (names, thresholds, the relay's
last state) are its own and start afresh: ESPHome's live in the 32 KB at
`0x3ee000` and OpenBeken's in the sector at `0x3e1000`, and neither touches
the other's.

## What the emulator has shown

Each of these works in the BekenEmulator fork, on a 4 MB flash shaped like a
real S61 (stock firmware, provisioned with a test network), with no bypass of
any check the device makes. None has been tried on a real S61 yet.

1. **The conversion**, the way the tool drives it. The device, on the local
   eWeLink stub, is pushed an `upgrade` command; it fetches the FOTA-wrapped
   `.rbl` in ranged requests and stages it verbatim at `0x286000`.
2. **The apply and first boot.** Booted from that flash, the stock bootloader
   logs `upgrade(1.0.2->26.10.29-bk7238)`, decompresses (`_STREAM_END`, the
   raw size matching the build), erases and writes `app`, then `go
   os_addr(0x10000)`. LibreTiny 1.13.0 starts, and ESPHome logs `Calibration:
   stock store` and `Wi-Fi from the stock store: 'MyTestNet' (11-char
   password)`, joins (four-way handshake, DHCP), announces itself over mDNS as
   `esphome-xxxxxx` and asks for NTP. Through the emulator's port forwards it
   answers the REST the tool reads (`/switch/Relay` `ON`,
   `/text_sensor/Calibration` `stock store`, `/sensor/WiFi Signal`) and the
   native API, which lists every entity and both actions, reports the relay
   restored on, and answers `autoflash_wifi` with the stock network's name and
   password.
3. **Reversion.** The stock UF2 above is sent through ESPHome's OTA, which on
   LibreTiny listens on port 8892, not 3232 (`esphome upload` and `espota2`
   know). The log reads `OTA successful`, `Rebooting safely`, then the stock
   bootloader's `upgrade(26.10.29-bk7238->1.0.2)`, erase and write. The flash
   afterwards holds the stock app byte for byte over its length, and the
   EasyFlash store with the same keys, Wi-Fi and calibration; loaded afresh it
   boots the stock firmware (SDK 3.0.70, `EasyFlash V4.1.99 is initialize
   success`).
4. **To OpenBeken and back.** A UF2 carrying OpenBeken's 4 MB-table `.rbl`
   goes through ESPHome's OTA (`OTA successful`,
   `upgrade(26.10.29-bk7238->s61v6)`). The fresh boot imports the stock Wi-Fi,
   calibration and pin template and joins in 8 s. Then `ota_http` of the
   ESPHome `.rbl`, served from the host (`init OTA, startaddr 0x286000`,
   `upgrade(s61v6->26.10.29-bk7238)`), and the fresh boot of that is ESPHome
   again: `Calibration: stock store`, joined, the API answering
   `autoflash_wifi` with the stock network.

5. **Its own network, and a Wi-Fi that goes.** Refused by the home network (a
   changed password, or none there), ESPHome raises its own open network,
   `esphome-xxxxxx` at 192.168.4.1, about 40 seconds after it starts, and goes
   on trying the home one; given details there it joins with them. Its router
   off, it notices within seconds and looks for it; off for a minute and a half
   or more, it raises its own network meanwhile, and rejoins the home one once
   the router is back. Then, in some runs, it releases the address it goes on
   using (`DHCPACK`, a second `DHCPACK` of the same address, then
   `DHCPRELEASE`, in the emulated router's log), so a router may give it to
   another device. Two bugs upstream, LibreTiny's and ESPHome's, make it
   ([traced](../../docs/beken-research.md#esphome-releasing-the-address-it-keeps));
   whether a real device does the same is not known
   ([open question](../../docs/beken-research.md#questions-still-open)).

As with OpenBeken, the emulator does not run the applied app in the same
process after a firmware-initiated reboot; the flash it saved is what counts.
It needed one change, in the fork: LibreTiny keeps its FreeRTOS tick on
hardware Timer 0 (the 26 MHz group), where OpenBeken and the stock firmware
use Timer 3, and the emulator raised only Timer 3, so every delay waited for
ever. It now models the 26 MHz group too on the BK7238, each enabled channel
firing at its own period
([why the period matters](../../docs/beken-research.md#the-esphome-route-libretiny)).

The tool's own route runs end to end in the emulator
([its fault suite](../../docs/testing.md#the-beken-fault-suite)).
