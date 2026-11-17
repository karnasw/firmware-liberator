# The tool's OpenBeken driver

The tool's OpenBeken driver, [`drv_autoflash.c`](drv_autoflash.c), runs in the
OpenBeken the tool installs on a Beken device. On the first boot after a
conversion it reads the device's **stock** Wi-Fi credentials from the untouched
EasyFlash store and applies them through OpenBeken's own config API, so the
device rejoins your network with no typing and no second device. It also
carries the meter's factory calibration over, applies the device's wiring,
answers the tool's questions, marks the interim firmware and takes the device
back to Sonoff's firmware.

It sets the Wi-Fi through `CFG_SetWiFiSSID()` and `CFG_SetWiFiPass()` rather
than writing the config blob, so it keeps working across OpenBeken releases
that change the `mainConfig_t` layout or checksum. It only reads the stock
store, so it never stands in the way of reverting to stock. The findings it
rests on are in
[beken-research.md](../../../docs/beken-research.md#carrying-wi-fi-across-the-conversion-the-openbeken-config).

The driver serves any eWeLink device on a Beken chip that keeps its stock
rt_ota bootloader, and names no device itself. What is specific to one device
it reads from that device's table, an `autoflash_device_t` named
`autoflash_device`, whose fields [`drv_autoflash.h`](drv_autoflash.h) lists:
its kind and stock model, where its stock store, download slot and OpenBeken's
settings sit, its wiring, the relay's channel, and how its meter's calibration
reads. A build carries one table, for the device it is built for. An image
carrying several, which would tell the device by its stock store's model at
run time, waits for a device that needs it. The tables:

- [`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`](../../../devices/sonoff-s61/drv_autoflash_sonoff_s61.c):
  the S61's, with its BL0937 meter's calibration
  ([how it reads](../../../devices/sonoff-s61/research.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping)).
- [`devices/sonoff-basic-1gs/drv_autoflash_sonoff_basic_1gs.c`](../../../devices/sonoff-basic-1gs/drv_autoflash_sonoff_basic_1gs.c):
  the Basic Gen5's, with no meter.
- [`drv_autoflash_readout.c`](drv_autoflash_readout.c): the readout build's,
  for a device the tool does not convert yet. It has no pins at all, so
  nothing is driven on hardware nobody has mapped
  ([readouts.md](../../../docs/readouts.md)).

## Integrating into OpenBeken

Three edits against `openshwprojects/OpenBK7231T_App`, shown here for the S61:

1. Copy `drv_autoflash.c`, `drv_autoflash.h` and the device's table
   (`drv_autoflash_sonoff_s61.c`) to `src/driver/`.
2. Add the two `.c` files to the build list in `platforms/obk_main.mk`,
   beside the other drivers:

   ```
   OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash.c
   OBKM_SRC  += $(OBK_SRCS)driver/drv_autoflash_sonoff_s61.c
   ```

3. Call it once from `Main_Init()` in `src/user_main.c`, right after
   `CFG_InitAndLoad();`, so the imported SSID is in place before the Wi-Fi
   connects:

   ```c
   CFG_InitAndLoad();

   extern void Autoflash_Init(void);
   Autoflash_Init();
   ```

`Autoflash_Init()` registers the driver's five commands, runs the import (a
no-op once a Wi-Fi SSID is set) and looks for the interim marker, so it is
safe on every boot.

## What the driver does on first boot

The driver imports only while OpenBeken has no Wi-Fi SSID yet, so your later
changes are never touched. It reads the stock EasyFlash store and:

1. imports **`wifi-ssid` and `wifi-pass`** through `CFG_SetWiFiSSID()` and
   `CFG_SetWiFiPass()`;
2. carries the **factory meter calibration** over, on a device whose table has
   a meter. On the S61, `elecCalibCoef`'s trim for each quantity, `b/1e6`,
   times the stock driver's datasheet constants gives OpenBeken's `cal` for
   current, voltage and power exactly, and `PowerMax` becomes the device's
   3840 W;
3. applies the **device's wiring** from its table, and sets `SetStartValue
   <relay channel> -1`, so the relay restores its last state after a power cut
   (what the tool sets on Tasmota and ESPHome too);
4. **names the device** as the conversion does on every firmware:
   `openbeken-abcdef`, from the last three bytes of its MAC. That is both its
   short name (its mDNS hostname and its name in Home Assistant) and its full
   one (its DHCP hostname).

## The commands

- **`AutoflashImportWiFi`** runs the first-boot import by hand.
- **`AutoflashRevertToStock <url>`** is [below](#reverting-to-stock), and
  **`AutoflashInterim`** in [the interim firmware](#the-interim-firmware).
- **`AutoflashKind`** tells the tool what device an OpenBeken is on, since
  every firmware it installs is named alike and you may rename it.
  `AutoflashKind sonoff-s61` answers OK on an S61 and an error for any other
  kind; an OpenBeken without the driver does not know the command. It changes
  nothing. The tool asks it only of an OpenBeken on a chip one of its devices
  has (BK7238).
- **`AutoflashCalibration`** says where the meter's calibration came from, for
  the run's report. `AutoflashCalibration stock` answers OK when OpenBeken's
  current, voltage and power calibration is what the first boot takes from the
  stock store, and `AutoflashCalibration missing` when the store holds none,
  or none valid. Both give an error otherwise (it was not applied, or you have
  calibrated the device since) and on a device with no meter. It works the
  calibration out from the store each time, so it holds after a restart, and
  changes nothing. The tool asks it once OpenBeken has started (K1). The
  report then says `Meter: Retained` for `stock` and `Not retained` for either
  of the others, as it does from ESPHome's Calibration sensor; a driver older
  than the command leaves it `Not checked`.

The driver's log lines begin `AutoflashImport:`, or `AutoflashRevert:` for
reverting. Logs from before the driver was split from the S61's table use
older names ([the S61's research](../../../devices/sonoff-s61/research.md#openbeken-on-the-s61-what-the-emulator-showed)).

## The interim firmware

A run to ESPHome from Sonoff's firmware installs this same OpenBeken first, to
back the device up through it, then has it fetch ESPHome. The tool marks it as
the interim firmware in the same update: B2 sends the image, then `0xff` up to
the next 4 KB boundary (counted from the image's start), then one 4 KB marker
sector holding `AFINTERM`, a format version, the image's size, the size and
FNV-1a hash of the app it installs (its RBL header's `size_raw` and
`raw_hash`) and a CRC-32 ([`tools/beken/interim_marker.py`](../interim_marker.py)
has the layout). Sonoff's updater writes the whole file into the download
slot, and the stock bootloader, having applied the image, erases only the
slot's first sector (the image's header), so the marker is still there on the
first boot. A run to OpenBeken sends the image alone.

On every start, after the first-boot import and before Wi-Fi starts, the
driver looks at each 4 KB boundary of the slot past the first for a valid
marker: its magic, version and CRC, at the boundary after the image size it
gives. That is 345 reads of 28 bytes; with no marker nothing changes and the
app is not read. A marker found is checked against the app running: the FNV-1a
hash of the app partition's first `size_raw` bytes, read raw from `0x11000`
with the CRC stripes skipped (32 bytes, then 2 of CRC). That is the hash the
bootloader checked the app against once it wrote it.

- **For that app**, it names the device `autoflash-recovery-CDEF` (the MAC's
  last two bytes in upper case, as the S60's interim firmwares are named),
  short and full name alike, saves the config, and only then erases the
  marker, so a power cut in between leaves it to be found again. The device
  asks for its first address under that name.
- **For any other app** (an update that did not install, a marker sent with
  another image, or one left by anything but the install it came with) the
  marker is erased and changes nothing. The bootloader installs an image
  before any app starts, so a marker whose app is not running never will be.

That name is the record of it being the interim firmware. The driver keeps no
flag in OpenBeken's config: upstream may give its flags and unused bytes a
meaning, and a config from a later OpenBeken could then read as interim. A
name set through the setters survives any change of the config's layout. A
device you have renamed is your own, and no longer the interim firmware.

`AutoflashInterim` answers OK on the interim firmware and an error on any
other; an OpenBeken without the driver, or with one older than the command,
does not know it.

- `AutoflashInterim off` makes it the device's OpenBeken for good
  (`openbeken-abcdef`), for a run that finds a device left on the interim
  firmware and is to end on OpenBeken. The tool then restarts it, and its DHCP
  and mDNS names follow.
- `AutoflashInterim on` makes it the interim firmware, for the run that sent
  it as one when the marker did not arrive (its update cut off in its last few
  kilobytes, after the whole image). The tool does not restart it, so it keeps
  the name it started with until the next firmware replaces it.

Once the interim firmware has fetched the next firmware, ESPHome or Sonoff's
own, it leaves nothing of itself. OpenBeken's HTTP OTA calls
`HAL_OTA_BeforeReboot` just before the restart that applies the download (the
hook patch [below](#building-bk7238-4-mb-table)). If this is the interim
firmware and the slot holds a whole rt_ota app image (its header's CRC-32 and
its body's both good), the driver erases OpenBeken's settings (`0x3e1000`),
its flash variables (OpenBeken's EasyFlash at `0x1e5000`-`0x1ed000`: boot
counts, kept channel values, energy totals) and any marker past the image.

- A download cut off leaves it the interim firmware, to be sent the image
  again.
- A power cut after the erase still applies the image, which is whole in the
  slot.
- A later move back to OpenBeken starts afresh, named by its first boot's
  import.
- Any other OpenBeken - your own, updating itself - is left as it is.
- OpenBeken's LittleFS, which would sit at the top of the download slot, is
  never made on the interim firmware, so there is none to erase.

## Building (BK7238, 4 MB table)

OpenBeken's BK7238 build hard-wires the SDK's 2 MB partition table. The
devices the tool converts are on Sonoff's 4 MB table, which is the SDK's 4 MB
one. Built with that, OpenBeken's OTA slot becomes the stock download
partition (`0x286000`) that the stock bootloader applies from, so OpenBeken
updates and reversion to stock both work through the ordinary `ota_http`. Its
config moves to the dedicated, empty `0x3e1000`, and it reads the device's
real radio calibration at `0x3e0000`.

Two patches, kept here and offered upstream as
[OpenBK7231T_App#2290](https://github.com/openshwprojects/OpenBK7231T_App/pull/2290)
and [beken_freertos_sdk#13](https://github.com/NonPIayerCharacter/beken_freertos_sdk/pull/13),
add a `FLASH_SIZE=4M` build switch. A third gives the driver its hook before
an OTA's restart:

- [`openbk7231t_app-bk7238-4m-flash.patch`](openbk7231t_app-bk7238-4m-flash.patch)
  (against `openshwprojects/OpenBK7231T_App`):
  - `make FLASH_SIZE=4M` builds with the 4 MB table.
  - `hal_ota.h` takes the OTA slot from the partition table the firmware was
    built with instead of a hard-coded `0x132000`. The REST and web-UI OTA
    path used the constant: on the first 4 MB build without this, the download
    still went to `0x132000`.
  - `DEFAULT_FLASH_LEN`, LittleFS's bounds and the raw flash-write cap, all
    2 MB constants too, follow the table: `GET /api/flash/...` can then read
    the config region for the backup, and LittleFS no longer sits inside the
    app partition of a 4 MB build.
  - A change of flash size between builds rebuilds the app's objects, which
    the SDK build would otherwise reuse from the previous size.
- [`beken_freertos_sdk-4m-pack-profile.patch`](beken_freertos_sdk-4m-pack-profile.patch)
  (against the SDK it clones, `NonPIayerCharacter/beken_freertos_sdk`):
  - The pack step names its outputs by flash size instead of always `_2M`.
  - The 4 MB QIO profile's app size matches the table: 2516 K, where the
    2890 K it had ran into the OTA slot.
  - A 4 MB QIO is packed with a 4 MB bootloader: the shipped
    `bootloader_bk7238_uart1_v1.0.15.bin` with the 4 MB partition table
    written into its tail, which is byte for byte the S61's factory
    bootloader. The pull request ships that file; the patch here has
    `make_bootloader_4m.py` generate it on the first pack.
- [`openbk7231t_app-ota-before-reboot-hook.patch`](openbk7231t_app-ota-before-reboot-hook.patch)
  (against `openshwprojects/OpenBK7231T_App`, alone or after the first) adds
  `HAL_OTA_BeforeReboot(ok)`: weak and empty unless a driver defines it, and
  called in the Beken HTTP OTA path once the download is written and the
  config saved, just before `bk_reboot()`. `ok` is set when no error was seen
  while it downloaded. OpenBeken's HTTP client ends a download the same way
  whether it finished or failed, and can take a connection closed early for
  the end, so a hook that acts on it checks the image too; this driver's does
  ([the interim firmware](#the-interim-firmware)). It is offered upstream as
  [OpenBeken #2293](https://github.com/openshwprojects/OpenBK7231T_App/pull/2293).

The Beken SDK bundles its own `arm-none-eabi-gcc`, which needs
**GLIBC ≥ 2.34**: build in Ubuntu 24.04 or 22.04, as OpenBeken's own Docker
image is too old and fails with `GLIBC_2.34 not found`. The SDK's pack step
calls `python`, not `python3`, so `python-is-python3` is required. The S61's
build:

```sh
git clone https://github.com/openshwprojects/OpenBK7231T_App
cd OpenBK7231T_App
cp <this repo>/tools/beken/openbeken/*.patch .
git apply openbk7231t_app-bk7238-4m-flash.patch
git apply openbk7231t_app-ota-before-reboot-hook.patch
# ...the three driver edits from the section above...
docker run --rm -v "$(pwd)":/app ubuntu:24.04 bash -c '
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq \
      cmake srecord make git python3 python-is-python3 wget xxd build-essential
  cd /app && git config --global --add safe.directory "*"
  make APP_NAME=OpenBK7238 prebuild_OpenBK7238      # clones the SDK
  git -C sdk/beken_freertos_sdk apply /app/beken_freertos_sdk-4m-pack-profile.patch
  make APP_VERSION=s61 APP_NAME=OpenBK7238 FLASH_SIZE=4M OpenBK7238'
```

The build by hand leaves `output/s61/OpenBK7238_4M_s61.rbl`, the `rt_ota`
image for the **OTA conversion**: wrap it in the CoolKit FOTA container with
[`fota_wrap.py`](../fota_wrap.py) to push it to a stock device.
`OpenBK7238_4M_QIO_s61.bin` beside it is a whole 4 MB flash, bootloader
included, for the emulator or a UART flash.

`tools/repo/update_firmware.py openbeken` does all of this for each device
(the clone, the patches, the driver and table, and the build in
[`build.Dockerfile`](build.Dockerfile)) and publishes the image as
`artifacts/openbeken-<device>-<version>.rbl`, with the OpenBeken and SDK
commits pinned in `artifacts/firmware.json`
([docs/updating-firmware.md](../../../docs/updating-firmware.md#3-openbeken)).
The current builds are 1.18.320-2 for the S61, and 1.18.320-1 for the Basic
Gen5 and the readout build.

What the builds were seen to do in the emulator, on the S61's dump, is in
[the S61's research](../../../devices/sonoff-s61/research.md#openbeken-on-the-s61-what-the-emulator-showed).

## Updating

The tool updates a device on an earlier release of this OpenBeken
(`--firmware openbeken`; `tools/autoflash/routes/update.py`). Such a device
answers `AutoflashKind` for its kind and gives its release in its build string
(`make APP_VERSION=`); the tool has it fetch the published image by `ota_http`
into the download slot, as any OpenBeken update.

Its settings stay: the hook erases them only on the interim firmware, and the
first-boot import, finding a Wi-Fi SSID set, imports nothing again. So what a
later driver does on first boot reaches only a device converted with it. An
upstream build is for the 2 MB table and has no driver, so it is not an update
for these devices.

## Reverting to stock

On a converted device the stock `rt_ota` bootloader is still in place, and it
applies any valid RBL it finds at `0x286000`, downgrades included. With the
4 MB-table build that is OpenBeken's own OTA slot, so reversion is an ordinary
OTA download of a **stock app RBL**:

```
AutoflashRevertToStock http://<tool>/stock_app_1.0.2.rbl
```

The command first checks that the OTA slot really is `0x286000`, and refuses
otherwise: a 2 MB-table build would write inside the app partition, where the
stock bootloader never looks. The stock EasyFlash store is never touched, so
the device comes back with its own Wi-Fi, calibration and identity.

- **The S61's is rebuilt from a dump**, whose app partition holds only the
  decompressed image. [`stock_app.py`](../stock_app.py) extracts the app and
  verifies it against the stored FNV-1a hash, and [`rbl.py`](../rbl.py) packs
  it as the SDK's own packer does (`-c gzip -s aes`, RT-Thread's public key):
  the same body byte for byte, with the header's version, serial and time as
  the dump's own header has them. `stock_app.stock_rbl(dump)` does both, with
  nothing but Python. The tool makes the image this way, from the public S61
  dump, when an S61 goes back.
- **A device with no public dump**, such as the Basic Gen5, goes back on
  eWeLink's official file instead (`tools/autoflash/routes/beken/images.py`).

The SDK's packer gives the same file:

```sh
python3 ../stock_app.py dump.bin -o stock_app_1.0.2.bin
sdk/beken_freertos_sdk/tools/rtt_ota/rt_ota_packaging_tool_cli \
    -f stock_app_1.0.2.bin -o stock_app_1.0.2.rbl -p app -c gzip -s aes \
    -k 0123456789ABCDEF0123456789ABCDEF -i 0123456789ABCDEF -v 1.0.2
```

## Backing up the device

[`backup.py`](../backup.py) copies the device's own region,
`0x3e0000–0x400000` (the factory TLV with MAC and radio calibration, the
EasyFlash app store with Wi-Fi credentials and `elecCalibCoef`, the system
ENV), out of a converted device over OpenBeken's `GET /api/flash/<addr>-<len>`.
It files it as an S60's backup is filed, never overwriting one:

```
captures/devices/<mac-with-dashes>/sonoff-backup/
    config-0x3e0000.bin    the 128 KiB region, as read
    stock-app-1.0.2.rbl    the stock app, repackaged (with --stock-rbl)
    backup.json            offsets, hashes, when, how to restore
```

```sh
python3 ../backup.py --device http://<device> --stock-rbl stock_app_1.0.2.rbl
```

The copy was byte-exact against the S61's dump in the emulator.
