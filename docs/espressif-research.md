# Espressif research

What is known about the ESP32 and ESP32-C3 devices the tool converts or
might, and how it was found, so you need not repeat the work.
[device-research.md](device-research.md) lists the rest, by chip family and by
device; [Beken research](beken-research.md) is laid out the same way.

"Emulator" below means the device ran under QEMU against the stub cloud
([emulator.md](emulator.md)); nothing here ran on a real device unless it says
so. Having no relays, meters, buttons or screens, the emulator cannot prove a
device's wiring. The legal footing for studying the firmware is
in [legal.md](legal.md#studying-and-emulating-the-firmware).

## Devices

| Device | Chip | Layout | Firmware name | Source of the firmware | In the emulator |
|---|---|---|---|---|---|
| S60TPG | ESP32-C3 | S60 style | `FWSW-E32S60-S60-ESP32C3FN4` | whole dump | converts both ways |
| POW Elite (POWR316D) | ESP32 | S60 style | `FWSW-E32POWP-POWPRO-ESP32D0WDV3` | whole dump | converts both ways, and back |
| POWCT | ESP32 | S60 style | `FWSW-E32POWCT-POWCT-ESP32D0WDV3` | whole dump | converts both ways |
| TH Elite (THR320D) | ESP32 | TH style | `FWSW-E32THR3-THR3-ESP32D0WDV3` | whole dump | converts both ways |
| Dual R3 | ESP32 | TH style | `FWSWES-E32DR3-DUALR3-ESP32D0WDV3` | update file, on the TH Elite's flash | converts both ways |
| NSPanel | ESP32 + PSRAM | S60 style, no `reserve` | `FWPNL-E32NSP-NSPANEL-ESP32D0WDV3` | whole dump | boots; rejects a one-record file at the header ([why](../devices/sonoff-nspanel/research.md#the-nspanel-wants-three-records)) |
| BASICR4 | ESP32-C3 | table at 0xC000; the rest unknown | `FWSW-E32R4-BASICR4-ESP32C3` | update file, on the S60's flash with its table moved | starts and is read out ([below](#sonoffs-ais-sdk)); not converted ([why](#converting-a-table-at-0xc000)) |
| MINIR4 | ESP32 + PSRAM | S60 style | `FWSW-E32R4-MINIR4-ESP32D0WDR2V3` | update file | starts with the ais SDK's records and is read out ([below](#sonoffs-ais-sdk)); a conversion not tried yet |
| M5 1C, 2C, 3C (not Matter) | ESP32 | unknown | `FWSWES-E32T5-T5-ESP32D0WDV3` | update file | goes quiet, never starts its Wi-Fi |

The stock model a firmware gives itself is `E32-MSW-NX` on the NSPanel,
`E32-2SW-P0` on the Dual R3 and `ESP32D0-MSW-T5` on the M5. The POW Elite
shares its stock model with the POW Elite 20A (POWR320D), which is wired
differently, and the TH Elite shares its own with the THR316D, so a device
kind in the tool can let the eWeLink product model decide which it is.

### Locked devices

Reported locked, with secure boot and flash encryption, are the
[MINIR4M](https://www.elektroda.com/news/news4160452.html), the M5's Matter
versions ([M5-3C-80W](https://www.elektroda.com/rtvforum/topic4125389.html),
whose public dump is encrypted and so unusable) and the MINI-D.

Matter is not itself the lock (the S61 carries a full Matter stack, and its
updater checks integrity only): Espressif's secure boot on an ESP32 Matter
build is, and any of these shows it before anything is written:

- the partition labels `esp_secure_cert`, `nvs_matter` or `fctry`;
- `secure boot v2` or `flash encryption is enabled` in a serial boot log;
- `Download mode is disabled` with GPIO0 held low;
- a dump with no readable partition table.

Each backup logs the fuses too ([below](#what-a-backup-logs)).

## Flash and layouts

| Partition | S60 style | TH style | Locked Matter |
|---|---|---|---|
| `esp_secure_cert` | | | `0xf000`, `0x2000` |
| `nvs_matter`, `nvs_matter_bkp` | | | `0x11000`, `0x17000` |
| `nvs` | `0x9000`, `0x10000` | `0x9000`, `0x40000` | `0x1d000`, `0xf000` |
| `reserve` | `0x19000`, `0x4000` | | |
| `otadata` | `0x1d000`, `0x2000` | `0x49000`, `0x2000` | `0x2c000`, `0x2000` |
| `phy_init` | `0x1f000`, `0x1000` | `0x4b000`, `0x1000` | `0x2e000`, `0x1000` |
| `ota_0` | `0x20000`, `0x1f0000` | `0x60000`, `0x1a9000` | `0x30000`, `0x1e0000` |
| `ota_1` | `0x210000`, `0x1f0000` | `0x210000`, `0x1a9000` | `0x210000`, `0x1e0000` |
| `fctry` | | | `0x3f0000`, `0x6000` |

All three are 4 MB, with `ota_1` at `0x210000`. They are a pattern to guess
from, not to assume: the Dual R3, first guessed S60 style, showed TH style in a
real one's boot log. So after its backup a device's table is compared, by hash,
with the one its kind expects, and a run that finds another stops before
anything of Sonoff's is written over.

These layouts were seen on real units:
[the Dual R3's boot log](https://community.home-assistant.io/t/help-with-flashing-sonoff-dual-r3-please/297547?page=2)
(stock by its labels and IDF 4.1, though the poster does not say it was never
flashed),
[the MINIR4's layout](https://github.com/smeso/esp32-ptm216b) and
[the MINI-D's boot log](https://community.home-assistant.io/t/can-you-flash-a-sonoff-mini-d/932312).
No stock table was found for the BASICR4, the non-Matter M5 or the Dual R3
Lite.

### The Dual R3's work mode

The Dual R3 drives two relays or a curtain motor, which its firmware (1.6.1)
keeps as `deviceMode` in its user settings (the JSON in NVS namespace
`user_usual`, key `content`) and reports it to eWeLink as `workMode`: 1 is
relays (it then loads each relay's saved state), 2 is a motor, and 3 is a third
mode not looked into. Finding no `deviceMode` at start-up, or one outside 1 to
3, it writes 2 and runs as a motor, refusing every switch from eWeLink with
error 400. A `workMode` update through eWeLink changes the mode. This was read
in the firmware's start-up and `workMode` handler and seen in the emulator;
[SonoffLAN](https://github.com/AlexxIT/SonoffLAN) also treats workMode 2 on a
Dual R3 (uiid 126) as a cover. The firmware resets all its user settings,
`deviceMode` 2 among them, when it registers with an account other than the
one its `user_unusual` settings name (`finalUserApikey`).

The emulator's Dual R3 runs on the TH Elite's settings, which have no
`deviceMode`, so [public_dump.py](../tools/espressif/emulator/public_dump.py)
gives it `{"init":"true","deviceMode":1}` (the firmware adds its other
defaults) and makes the test copy's account the stub's, keeping the mode.
That a Dual R3 comes in relay mode is assumed: its factory's setting is not
known.

### Leaving Tasmota on a TH-style layout

The 715 KB mover does not fit in the 512 KB between `ota_0` at `0x60000` and
Tasmota's app at `0xE0000`: the flash is 4 MB by the boot log, but a 256 KB
`nvs` pushes `ota_0` up. So these devices leave Tasmota through a temporary
table: `nvs` `0x9000`, `0x14000`; `otadata` `0x1D000`;
`phy` `0x1F000`; `ota_0` `0x20000`, `0x1F0000`; `ota_1` `0x210000`,
`0x1A9000`. The mover goes at `0x20000`, in Tasmota's Safeboot area, and the
interim Tasmota in `ota_1`; the hub's Tasmota then writes the real table last.

In the emulator the TH Elite and the Dual R3 go from Tasmota to Sonoff and to
ESPHome, and the TH Elite survived a power cut at each stage of its way to
Sonoff; a cut inside the table plan was set up by hand, not timed, and one in
the middle of a write while leaving was not reached.

### One bootloader

Every device ends with ESPHome's bootloader, however it got Tasmota: stage D5
writes it, from the interim Tasmota, and leaving Tasmota never does. So a
device going from Tasmota to ESPHome takes the direct route only if it already
has that bootloader, known by its hash, and otherwise goes through the interim
Tasmota. Tasmota's own bootloader starts the mover, as a POW Elite's boot log
in the emulator showed. Leaving Tasmota, Berry has about 28 KB free on the full
build; a whole write held a classic ESP32's 26 KB bootloader twice over and
stopped there. As every conversion has already put that bootloader in place,
the tool compares its hash first.

### Converting a table at 0xC000

The BASICR4's bootloader and app read its partition table at 0xC000
([how this was found](#sonoffs-ais-sdk)). Where a program looks is built into
it (`CONFIG_PARTITION_TABLE_OFFSET`), not passed on by the bootloader, and
everything the tool installs is built for 0x8000: the interim Tasmota, the
mover, the safeboot, ESPHome and its bootloader, and the bridge, which reads
0xC000 only when 0x8000 is empty. Tasmota's Partition Wizard
(`partition_core.be`) and the tool's plans (`plans.TABLE_ADDRESS`) write the
table at 0x8000. This was read in the code and Tasmota's source, not tried.

Tasmota put in a slot would start under Sonoff's bootloader, find no table or
NVS at 0x8000, and restart for ever, so bridge 4.4.6 refuses an update on such
a device. The likeliest way, inferred and not tried, needs no change upstream:
the bridge first writes a copy of the 0xC000 table's entries at 0x8000 and
reads it back. Sonoff's bootloader and app never read that
sector, so a power cut there costs nothing. The interim Tasmota then sees the
same partitions, and D5 puts ESPHome's bootloader in place as on any device,
after which the device is as any other converted one. On the way from Sonoff,
the stale copy at 0xC000 goes before the Tasmota layout's `nvs`
(0x9000-0xDFFF) takes that range; on the way back, the backup restores both.

This needs a bridge built with dangerous flash writes allowed (its sdkconfig
aborts on them), a BASICR4 layout in `layouts.py`, and first a readout of a real
one: if its bootloader runs past 0x8000 (a guessed reason for 0xC000,
[below](#sonoffs-ais-sdk)), a table there would overwrite it, and the conversion
must refuse. Custom Tasmota or ESPHome builds for 0xC000 would also work, but
not as the official images, and the Partition Wizard would write a table nothing
reads.

## A device's identity and stores

A device's eWeLink identity is a few entries of Sonoff's NVS, 300 to 550
bytes: `frame_params/content` and `data_backups/content` on every Espressif
device, `productId/productId` on the S60, and `channel_params/content` and
`PID/content` on the POWCT. The POW Elite's cached eWeLink server certificates
(`wss_ca1` to `wss_ca4`, about 5.9 KB) are not identity, nor are the Wi-Fi
(`nvs.net80211`, and `frame_params`' `ssid` and `passwd`), histories, logs and
radio calibration. The S60's
stores, key by key, are in [cloud-stub.md](cloud-stub.md#its-identity-in-nvs).

On a real S60, `frame_params.deviceid` is the account record's `deviceid`;
`frame_params.apikey`, `encrypt_key` (where there is one) and
`data_backups.apikey` are its `devicekey`; and `model` and `uiid` are
`extra.model` and `extra.uiid`. The record lacks `sha256` (64 hex digits),
`init`, the dispatch server and port, and the Wi-Fi. So
`sonoff_identity.from_account` rebuilds `frame_params` and `data_backups` from
the record, with the account region's dispatch server (`eu-dispd.coolkit.cc`),
`encrypt_key` and `is_encrypt` on a classic ESP32 as their dumps have them,
and no `sha256`.

In the emulator the S60's firmware 1.2.0 registers and reports its state the
same with `sha256` as it was, without it, and as 64 zeros, and with
`frame_params` cut to `init`, `uiid`, `deviceid`, `apikey` and `model` and no
`productId`, `data_backups` or user settings: `sha256` is sent neither in
dispatch nor in `register`. Whether the real eWeLink accepts a device rebuilt
this way has not been tried.

The ESPHome route keeps Sonoff's layout and NVS, identity included; the Tasmota
route shrinks the NVS to 20 KB, losing it. So the tool keeps the identity in a
sector of its own at `0x3FF000` ([its
format](technical-details.md#an-espressif-device-on-tasmota)), which in the
emulator fared so:

| What | S60 | POW Elite |
|---|---|---|
| Stock to Tasmota: the sector holds the identity, the table its entry, Tasmota boots, its filesystem (316 KB) takes a file | yes | yes |
| A Tasmota OTA update through its safeboot (`Upgrade 1`) | kept | |
| `Reset 1`, `Reset 2`, `Reset 5`, `Reset 6` | kept | |
| The filesystem formatted (first two blocks erased; Tasmota made it again) | kept | |
| The filesystem filled up | kept | |
| Tasmota's factory image written from `0x0` | kept; the table's entry gone, found by its magic | |
| Partition Wizard (v16.0.0, loaded by hand) | offers "Resize FS to max" by 4 KB; that drops the entry, and filling the filesystem then wrote over the sector | |
| The filesystem grown over the sector (Wizard as in #25241, deletion confirmed): 200 rewrites of a 200-byte file | kept | |
| The same, an 8 KB file rewritten | written over at the 10th rewrite (about 80 KB); after another restart, kept for 12 | |
| Back to Sonoff, no backup, from the kept identity: online with its own id and key | yes, after the factory image too | yes |
| Back to Sonoff, no backup, identity from the account record alone | yes | yes |
| Tasmota to ESPHome: the identity in ESPHome's settings, the sector kept | yes | |
| Stock to ESPHome: Sonoff's settings kept, the sector written too | yes | |
| From either ESPHome back to Sonoff, no backup: online with its own id and key | yes, from the sector | |
| TH Elite stock to Tasmota (sector kept; `0x3FF000` is in no partition of its layout) | yes | |

The Partition Wizard counts the `sonoff` area as free, looking for unallocated
flash past the last *filesystem* partition, not the last partition.
Its "Resize FS to max" first removes every non-app partition after the
filesystem (`remove_partition_after_last_fs`, in `resize_max_flash_size_k`),
whatever the flash size; its other resize, needing the filesystem last, is not
offered. A fix that asks first is proposed upstream,
[arendst/Tasmota#25241](https://github.com/arendst/Tasmota/pull/25241)
([what it changes](upstream-contributions.md#smaller-fixes-found-along-the-way)).

Once the filesystem covers the sector, the sector goes well before the
filesystem is full: LittleFS has no table of free blocks, and after each mount
starts allocating at a block picked from its metadata's checksums, moving
forward through every free block and wrapping round, so all 80 blocks are used
in turn whatever is stored. Small files live inside the directory's own blocks
and move nothing, which is why the 200 rewrites above left the sector alone;
one larger file, such as an extension, a script or a web page, can reach it.
Until then the tool still finds the sector by its header.

The last 4 KB of `app0`, at `0x3AF000` just in front of the filesystem, was
tried and is not used. On Tasmota's layout it would be better: the filesystem
keeps its 320 KB, the Wizard's only offer there ("Resize FS") refuses every
size ("last `app` partition has no firmware"), and after a factory image only
an update of 0x2D0000 bytes reaches it, as Tasmota's page upload stops 4 KB
short of `app0`'s end. But on Sonoff's layout, which every device passes
through, `0x3AF000` is inside the interim Tasmota the hub runs from `ota_1` on
an ESP32-C3 (`tasmota32c3-bluetooth.bin` ends at `0x3C92C0`; a classic
ESP32's at `0x377230`). On the S60, writing the identity there damaged the
running image, and every way back writes that image over it again, so going
back fell back on the eWeLink account.

The sector at the end of `ota_1` survives, as updates erase only as far as
their image: Arduino's `Update` by the size it began with, ESP-IDF's
`esp_ota_begin` the whole partition only for size 0 or unknown, and ESPHome
2026.10.0 up to the next 64 KB past the image. The settings do not: the
startup firmware wipes NVS when it finds no Tasmota settings there
(`persist-settings.patch`), as on the way from the mover or from ESPHome to the
interim Tasmota, so the identity L1 writes into the settings, or ESPHome kept,
is gone by the time R1 runs. R1 reads the identity from the sector, and the
tool keeps the sector on the way to ESPHome too.

## Sonoff's ais SDK

The BASICR4's firmware (1.1.0, ESP-IDF 4.4.2) and the MINIR4's are on a newer
Sonoff SDK than the S60's, with `ais_*` components (`ais_factory`, `ais_ota`,
`ais_colink`) where the S60's are `esf_*`. As read from the BASICR4's update
file (its RISC-V code disassembled), it differs in what an emulated device
needs:

- **The partition table is at 0xC000**
  ([what that means for a conversion](#converting-a-table-at-0xc000)): its
  `load_partitions` maps sector 0 and adds 0xC000 where the S60's adds 0x8000.
  On the S60's flash it found no table, `nvs_flash_init` returned
  `ESP_ERR_NOT_FOUND` and `espInit` aborted. The MINIR4's real table is at
  0x8000 (its `nvs` starts at 0x9000);
  [`flashmap.py`](../tools/espressif/flashmap.py) finds a table at either.
- **Its identity and Wi-Fi are records of its own**, not `frame_params`
  ([`ais_records.py`](../tools/espressif/ais_records.py) reads both). Each is a
  blob `info`: the magic `0x55AA` and a version, then NUL-padded text at fixed
  places. A readout's [`scrub.py`](../tools/espressif/scrub.py) takes the
  Wi-Fi and identity out, and blanks a record of an unknown version whole after
  its header.

  The **factory record** (namespace `factory`, 178 bytes, version 1 on both)
  holds the eWeLink identity the device registers with. It is written at the
  factory by `AT+LICENSE_WRITE`, from a license whose fields hash to its
  `sha256_check`; without one the firmware stays in `factory mode`.
  `aisFactoryLoadInfo` checks only the magic and version.

  | Offset | Field | Size |
  |---|---|---|
  | 0 | magic `0x55AA`, version `1` | 2 + 2 |
  | 4 | chip MAC, `%02X` six times | 16 |
  | 20 | `device_model` (must be the firmware's own, `SN-ESP32C3-BASICR4-01`) | 32 |
  | 52 | `deviceid` | 16 |
  | 68 | `factory_apikey` | 40 |
  | 108, 126 | `sta_mac`, `sap_mac` | 18 each |
  | 144 | `uiid` (138 on both, by SonoffLAN's list) | u16 |
  | 146 | SHA-256 of `factory_apikey`, `sta_mac`, `sap_mac`, `device_model`, the uiid in decimal, `deviceid` and the chip MAC, run together | 32 |

  The **pair record** (namespace `pair`) holds the Wi-Fi the device was paired
  with, which makes it paired, and where it finds eWeLink. Two versions are
  known:

  | Field | Version 1: 142 bytes (the BASICR4's 1.1.0) | Version 2: 176 bytes (the MINIR4's 1.2.0) |
  |---|---|---|
  | magic, version | 0, 2 + 2 | 0, 2 + 2 |
  | dispatch port | 4, u16 | 4, u16 |
  | dispatch host | 6, 32 | 6, 65 |
  | SSID | 38, 33 | 71, 33 |
  | password | 71, 65 | 104, 65 |
  | not read | 136, 6 | 169, 7 |

  Version 1's places were read from the BASICR4's code (`aisPairLoadInfo`, and
  the cloud connection and Wi-Fi join that read the record). Version 2's come
  from an emulated MINIR4 given a version 1 record holding a made-up password,
  which it wrote again as version 2 (`pair info data ver too old` ...
  `pair save info data ok` on its serial port): the host got 65 bytes, the
  SSID was seen at 71, and the password at 104, NUL-padded, also logged as
  `pair info: ssid[...] pwd[...]`. The password's 65 bytes are as in version 1;
  only one over 64 characters could show whether the field runs on into the
  7 bytes after it, which were 0. The scrub also removes every copy of the
  password in ESP-IDF's own `nvs.net80211`.
- **It boots in normal mode only when nobody answers** the `factory?` it sends
  three times on its serial port with `factory!`, as a production jig would.
- **Its settings' namespaces** share only `phy` with the S60's: `factory`,
  `pair`, `pair_bkp`, `pair_run_param`, `ewelink_device`, `outlets`, `scene`
  and `slisaon`. The donor's `frame_params` and the rest mean nothing to it.
- **Its dispatch** is `GET https://<host>:<port>/dispatch/device`, with no body,
  through `esp_http_client` with the pinned CA. Its handler copies only a reply
  under 128 bytes and not chunked, then needs `error` 0, and `IP`
  and `port` as strings (with no `port` it leaves the port out), and makes
  `wss://<IP>[:<port>]/api/ws`. A reply it cannot parse leaves that URL empty,
  and its connection loop silently dispatches again every 2 seconds, as it did
  for JSON with spaces: **its parser takes only compact JSON** (`"error":0`,
  not `"error": 0`).
- **It turns down an EC CA** where it pins eWeLink's RSA one (`unknown_ca`).
- **The MINIR4 puts `-v1` into its dispatch host** (`-v1%s`), and **connects by
  dispatch's `domain`**, checking the certificate against it, which ESP-IDF
  4.4's mbedTLS cannot do for an IP address
  ([how the stub answers](cloud-stub.md#where-other-devices-firmware-differs)).
  Both were found in its 1.2.0's serial log, showing each step of its cloud
  connection (`http request: ...`, `Get WsUri OK!`,
  `WEBSOCKET_EVENT_DISCONNECTED`); the BASICR4's 1.1.0 says nothing past
  `normal mode`.
- **No secure boot or flash encryption** code is in its app or the S60's:
  neither has the `secure_boot` or `flash_encrypt` strings such a build holds.
  Its updater (`ais_ota`) accepts the wrapped bridge (`"error": 0`).
- **It turns down updates while overheating.** A `TEMPERATURE_CHECK` task reads
  ADC1 channel 3 (GPIO3) through `esp_adc_cal`, in millivolts: at start-up 3
  readings 300 ms apart, over 12 rounds, and later 10 readings a second apart.
  When all are under 580 mV it posts `TEMP_EVENT` 0, whose handler sets a flag
  (`0x3fca0649` in 1.1.0) and an LED pattern (13); while the flag is set,
  `upgrade` gets `"error": 407`. Low meaning hot points to an NTC divider
  to ground; no published pin map names GPIO3. Neither the Tasmota template
  nor the ESPHome config the tool installs reads it, so a converted BASICR4
  would lose this protection. The emulated ADC
  read 0 V, and with no eFuse calibration 0 mV, so the emulator gives the
  device room temperature (`adc_readings` in
  [`kinds.py`](../tools/autoflash/kinds.py)) and every emulated ESP32-C3 the
  calibration real ones carry
  ([how](../tools/espressif/emulator/README.md#the-fork-patchespatch)).
- **Flash size, and other Sonoff ESP32-C3 builds.** By their image headers the
  BASICR4's, S60's and MINIR4's apps are all for 4 MB of flash (the BASICR4's
  at 80 MHz, the S60's at 40 MHz), so the table did not move for a larger
  flash. The other published Sonoff ESP32-C3 update files, the MINI-2GS's,
  MINI-DIM's and MINIR4M's, are Matter OTA images (magic `0x1BEEF11E`, with
  Sonoff's 100-byte wrapper inside and the app 178 or 330 bytes in) built with
  ESP-IDF 5, whose `load_partitions` differs, so their table's place is not
  known. 0xC000 is ESP-IDF's place for a table when the bootloader needs more
  than 32 KB (secure boot, flash encryption) and the default in Espressif's
  Matter SDK; that the BASICR4 was built from Sonoff's Matter-era project for
  that reason is a guess.

On the classic ESP32, only the NSPanel's firmware reads its ADC: it alone links
`esp_adc_cal`, for its room thermistor (GPIO38, ADC1 channel 2), and only
reports the reading. The other classic firmwares' strings show neither
`esp_adc_cal` nor a temperature check, which cannot rule out a raw read, but
the POW Elite, POWCT, TH Elite and Dual R3 convert both ways in the emulator as
it is. These firmwares are built on ESP-IDF v4.1.1, by their
app descriptions or source paths. The emulator's classic ADC reads 0 and its
eFuses carry no calibration
([why](../tools/espressif/emulator/README.md#the-fork-patchespatch)). With
neither a measured reference voltage (block 0) nor two points (block 3),
`esp_adc_cal` uses the reference the firmware passes, so a raw 0 reads as the
attenuation's offset, 75 to 142 mV on ADC1, not 0 mV. The emulated NSPanel
then sends eWeLink `"temperature": -2`, with no other effect seen
([the NSPanel's research](../devices/sonoff-nspanel/research.md#its-room-temperature)).
A Beken device's ADC is in
[Beken research](beken-research.md#the-factory-record-at-0x3e0000-mac-and-radio-calibration).

## No restart command

Sonoff's firmware has no command that only restarts the device, over the
eWeLink cloud or the local network: a device on it that stops answering must
be switched off and on by hand. Every other firmware a conversion leaves
a device on part way can be restarted by the tool
([`restart.py`](../tools/autoflash/restart.py)).

This was read from the strings and app image segments of a copy of the S60's
stock 1.2.0, not disassembled:

- **The cloud.** The WebSocket's `action` values, listed together at file
  offset `0x39a84`, are the whole set: `register`, `update`, `upgrade`, `date`,
  `notify`, `addNotify`, `deleteNotify`, `testNotify`, `resetDispatch` and
  `redirect`. Only `upgrade` restarts the device, after writing new firmware;
  `resetDispatch` and `redirect` reconnect a connected one to the cloud.
- **The local network.** eWeLink's LAN control, `/zeroconf/` on port 8081,
  announced as `_ewelink._tcp`, takes the cloud's `params` (`switch`, `pulse`,
  `startup` and so on). Its routes go through a framework, not string
  compares, so strings cannot list them all; none is a restart.
- **Nowhere else.** No `reboot` or `restart` is a command key or value in the
  image: the only such strings are ESP-IDF's crash, watchdog and update logs.
  A factory reset (the button, or `AT+RESET=` on the serial port) wipes the
  pairing and settings.

The `params` key `reset` (`0x26514`) was not traced to its handler; listed
with the energy keys (`startups`, `totalKwh`, `getEventLog`), it is most likely
the energy counters' reset. Settling it, or listing every LAN route, takes
disassembly of the handlers, a few hours each, and neither can turn up a
restart command, as there is no such token. The S61's firmware is
[the same](beken-research.md#no-restart-command).

## The update path

A stock device takes its conversion as an update: an app inside Sonoff's
100-byte wrapper, which must name the device's own firmware
([below](#learning-the-firmware-name)).

The emulator cannot run Sonoff's official update file, as every Sonoff app it
runs has Bluetooth start-up skipped and the stub's CA swapped in, so the stub
offers the device's own app, changed the same way, as eWeLink's latest
([the update offer](cloud-stub.md#the-stub-server)); fetching, unwrapping and
checking it is the same code as against eWeLink.

### Learning the firmware name

In the emulator the POW Elite read 100 bytes of a file whose wrapper named
another firmware, reported `ota: fail` and stayed on Sonoff's firmware, so a
wrong name costs nothing, but names vary too much to guess. On eWeLink's
real server:

| Asked | Answer | Tried on |
|---|---|---|
| `ota/query` with the device's current version | no offer | S60 |
| `ota/query` with its own id and version `0.0.1` | its latest firmware | S60, S61 |
| its own id, with another model | its own firmware: the model is ignored | S60 |
| a made-up id, with any model | an unrelated 3.5.0: never use one | |
| an empty id | error 400 | |
| GET on the offered URL as given | empty 400 | S60 |
| GET with `?deviceid=<its id>&ts=1&sign=0` | the file; the signature is not checked, and a `Range` is answered | S60, S61 |

`tools/ewelink/firmware_name.py` uses this at stage 5, reading only the file's
first bytes. Where eWeLink offers nothing, it falls back on the names in public
update files' filenames
([FlashDumps](https://github.com/openshwprojects/FlashDumps) `IoT/zOTAz`), and
gives a name only when exactly one fits: that finds all 13 kinds, none
wrongly. A file for a run of gangs, such as the M5's `M5-1C-3C`, counts as each
gang's (`M5-2C-86`), unless a file names the device itself, as `M5-3C-80W`'s
does.

A wrapper starts `03 01`, byte 1 is its record count, and its 76-byte records
start at `0x18`. An NSPanel file has 3 names, an SPM 2. A Beken file has the
same wrapper inside its
[FOTA container](beken-research.md#the-fota-download-container).

## What a backup logs

`tools/espressif/device_facts.py` logs the chip and its revision, secure boot,
flash encryption, the flash size, the partition table, and each app's version,
ESP-IDF and firmware name, on screen too when the table is not a known one. A
classic ESP32's eFuse file cannot tell revision 2 from 3, so from the file alone
it logs "at least".

The run also asks the installer's `GET /info` once, logging the exact chip
revision in place of "at least", then `Installer says` lines with the chip and
its cores, whether its flash is embedded, the flash chip's JEDEC id and real
size, the running and fallback slots, PSRAM, and input pin levels; an installer
before 4.4.5, or one that does not answer, costs the run only these lines. Two
have limits:

- **PSRAM.** The fuses say whether PSRAM is in the chip's package. A separate
  PSRAM chip is not looked for, as that would drive pins a board without one
  may use for something else, so "no" does not mean the device has none. An
  ESP32-C3 cannot have any.
- **Pin levels** are read from the chip's input registers, changing nothing.
  Pins whose input is off (`gpio input off`) read 0 whatever is on them, so
  they are named, not read; the flash chip's pins (6 to 11 on a classic ESP32,
  12 to 17 on an ESP32-C3) are left out. A level is a moment's snapshot in the
  installer, with the board's own pull-ups and whatever the stock firmware left
  behind, not a pin map.

In the emulator the answer comes back whole, but QEMU has no pads or revision
register: every pin's input is off, leaving `gpio levels` empty, and a classic
ESP32 says revision 0.0. A real chip's answer has not been seen.

## Tools and methods

- **The emulator**: [emulator.md](emulator.md).
- **Scripts**: those in [`tools/espressif/`](../tools/espressif/README.md),
  each described there, and in `tools/ewelink/`: `firmware_name.py`
  ([above](#learning-the-firmware-name)), the stub's `stub_cloud/firmware.py`
  ([above](#the-update-path)) and `firmware_findings.py` (what Sonoff's
  firmware says of its hardware, a detector each).
- **Reading an update file's code**, as the BASICR4's was: strip the 100-byte
  wrapper, split the ESP image into its segments (load address and bytes), make
  each an ELF with `riscv32-esp-elf-objcopy -I binary -O
  elf32-littleriscv -B riscv --rename-section .data=.text,code,alloc,load,contents`,
  then disassemble it with `riscv32-esp-elf-objdump -d --adjust-vma=<its address>`.
  The DROM segment (`0x3c...`) holds the strings, naming each `lui`/`addi`
  pair's address. An aborting `ESP_ERROR_CHECK` names its file, line and
  function; the line, in `a2` of its call to `_esp_error_check_failed`, finds
  the call. The table's place is the constant `load_partitions` adds after
  `spi_flash_mmap(0, 0x1000, ...)` (`lui a5,0x8` or `0xc`), and the app
  description (the first segment's start) gives the ESP-IDF version.
  PlatformIO's toolchain (`~/.platformio/packages/toolchain-riscv32-esp`) has
  both tools.
- **Watching a running emulated device** with gdb, not restarting it
  ([the emulator's README](../tools/espressif/emulator/README.md#debugging)).

## Questions still open

- What the M5 waits for after `factory?`. Neither input pin levels nor
  eWeLink's model number for it (160 to 162) moved it, and the only whole M5
  dump published is the encrypted Matter one ([above](#locked-devices)), with
  nothing to compare.
- The BASICR4's whole partition table, and whether its bootloader runs past
  0x8000 ([why](#converting-a-table-at-0xc000)).
- Whether the real eWeLink accepts a device whose identity was
  [rebuilt from its account record](#a-devices-identity-and-stores).
- How a Dual R3 out of the box is set to
  [relay mode](#the-dual-r3s-work-mode), and what its work mode 3 is.
- What GPIO3, the BASICR4's [temperature input](#sonoffs-ais-sdk), is wired to.
- What a real chip's installer answers to `GET /info`
  ([what a backup logs](#what-a-backup-logs)).
