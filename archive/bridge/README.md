# One-shot OTA bridge

This is a native ESP-IDF application, built for the ESP32-C3 and the classic
ESP32, not a factory image. It is
designed to be wrapped with `tools/espressif/build_vendor_ota.py` and written by a plug's
stock updater - the Sonoff S60's, so far - to its inactive application slot.
The application is the same for every plug; the wrapper names the plug's
model, so each kind of plug has a wrapped file of its own.

Bridge v3 first confirms itself so ESP-IDF rollback cannot later discard the
recovery slot, then selects the peer application for the next boot. If Wi-Fi or
the HTTP server fails, a power cycle returns to that peer. Immediately before
an upload erases the peer, the bridge selects itself instead. It selects the
new image only after `esp_ota_end` validates it.

The remaining irreducible risk is failure before `app_main` executes. The stock
1.2.0 bootloader was built without ESP-IDF application rollback, so no software
running in an app partition can eliminate that risk.

Bridge v5 added two things. It reads the Wi-Fi the stock firmware saved and
joins that network, so the run finds it where the plug was. And it takes
Tasmota's settings template, fills in the Wi-Fi it joined with, and saves it
where Tasmota reads its settings, so Tasmota joins by itself. The password
leaves the plug only inside the backup the run reads through `GET /flash`.

Bridge 4.4.2, the one the run installs, joins the stock network alone and
raises its own network only if it has had no address there for 45 seconds, at
start-up or later. v5 ran both at once on one radio, and on a plug the joined
network dropped out for seconds at a time while its own was up; tried on the
same plug, 4.4.1 (then called "v6") stayed on for ten minutes without one.
Where stock saved no Wi-Fi it raises its own network at once, as v3 did.

From 4.4.1 on, a bridge is named by one version: `BRIDGE_VERSION` in
`main/main.c` and `PROJECT_VER` in `CMakeLists.txt` (a test keeps them equal)
are the version its file is wrapped with and the one `/status` reports.
Change both for the next bridge, 4.4.7. The stock updater is told 9.9.9
instead: the sender wraps the app again as that, above any Sonoff release.

Bridge 4.4.3 starts even when Sonoff's slot is no longer bootable - an upload
of Tasmota into it (D1) cut off part way, by a power cut or the computer
stopping. 4.4.2 refused to start then, so the plug came up with no network
at all, every time, and only a serial cable could recover it. With no
fallback left there is nothing to keep: 4.4.3 says so in its log and on
`/status` (`fallback: none`), stays the one that starts, and takes the
upload again. And once on its own network, 4.4.3 tries the home network again
every three minutes, for 20 seconds, while nobody is joined to its own: a
router back after a restart is found again, where 4.4.2 kept to its own
network for good. Never both at once, as 4.4.1 found on one radio.

Bridge 4.4.4 goes by `autoflash-recovery-XXXX` (the last two bytes of the
plug's Wi-Fi MAC) on the home network and as its own network, as every
firmware a plug runs part way through a conversion does, and its page is
titled "Autoflash bridge". 4.4.3 joined as `s60-ota-bridge`, raised
`Sonoff-Setup-XXXX` and was titled "S60 OTA Bridge"; the run still knows a
plug left on it by those names. On its own network, with no proven Wi-Fi,
4.4.4 still saves the Tasmota settings it is sent - without Wi-Fi details -
where 4.4.3 refused them (409): Tasmota then starts named
`autoflash-recovery-XXXX` and makes its own network by that name.

Bridge 4.4.5 adds the read-only `GET /info`, for the run's log: what a backup
cannot hold, as plain `name: value` lines after the bridge's name. It writes
nothing, sets no pin, and holds no Wi-Fi details, MAC or other identity.

| Line | What it says | Where from |
|---|---|---|
| `chip`, `chip revision`, `cores`, `embedded flash` | the running chip, the revision as `major.minor` | `esp_chip_info`, which on a classic ESP32 also reads the register that tells revision 2 from 3 |
| `flash id`, `flash chip size` | the JEDEC id in hex, and the size in bytes from that id | `esp_flash_read_id`, `esp_flash_get_physical_size` |
| `psram in the package`, `psram on its own chip`, or `psram` | classic ESP32: the package's, and `not looked for`; ESP32-C3: none | the fuses, through `esp_chip_info` |
| `running slot`, `fallback slot` | label, address and size; `none` when this slot starts again | the partition table and otadata |
| `gpio levels` | `pin=level` for each pin whose input is on | `GPIO_IN_REG`, `GPIO_IN1_REG` |
| `gpio input off` | the pins not read | `FUN_IE` in `IO_MUX` |

The bridge cannot find PSRAM on a chip of its own: finding it means driving pins
16 and 17 on a classic ESP32, which a board without PSRAM may use for something
else, so this build does not set PSRAM up. A pin whose input is off reads 0 whatever
is on it, and turning its input on would change how the board is set up, so it
is named and not read. The flash chip's pins are left out altogether: 6 to 11 on a
classic ESP32, 12 to 17 on an ESP32-C3.

Bridge 4.4.6 adds `POST /restart`: the plug starts again into the firmware
chosen for its next start - Sonoff's, which the bridge chooses as it starts -
just as switching it off and on would, so a run need not ask anyone to. It is
refused (409) while an upload is under way, and where the next start would be
the bridge again, an upload cut off part way having left no Sonoff firmware to
start.

4.4.6 also says where the plug starts next as it is now, read from otadata on
each request: an upload pins the bridge before it erases Sonoff's slot, so one
cut off leaves no Sonoff firmware to start from that moment, not from the next
start. Before, its page went on saying "The untouched stock slot is selected for
the next power cycle", and `/status` gave no `fallback: none`, until the plug
was switched off and on.

4.4.6 also starts on a device whose partition table is at 0xC000 (the
BASICR4's firmware and bootloader are built so), where an earlier bridge found no
partitions at all and stopped on an assertion, over and over, with Sonoff's
bootloader starting it each time. Where 0x8000 holds no table and 0xC000 one
whose MD5 record matches, it registers that table's entries itself
(`esp_partition_register_external`) before anything looks for a partition:
nothing is written. It then only reads - `/flash`, `/info`, `/restart` - and
refuses `/update` and `/tasmota-settings` (409), as Tasmota and ESPHome look
for the table at 0x8000. `/info` says where the table is. ESP-IDF's own MD5
check is off (`sdkconfig.defaults`), as with it a blank 0x8000 fails every
partition lookup; the bridge checks the 0xC000 table's MD5 itself.

From 4.4.2 every build has the read-only `GET /flash` (the whole chip, with
its size in `X-Flash-Size`), `GET /flash-sha256` (the SHA-256 of the whole
chip, worked out on the plug) and `GET /efuse` (the eFuse read registers), so
the run keeps a backup of the Sonoff firmware, checked byte for byte against
the plug, before Tasmota is written.

`sdkconfig.defaults` holds the settings v3 was built with; with them a rebuild
matches the published v3 except for the build time, date and ELF hash. Keep
`CONFIG_ESP_WIFI_NVS_ENABLED=n`: the bridge never initialises NVS, so with it
on `esp_wifi_init()` fails and the bridge aborts at start-up (the plug then
falls back to stock by itself). Before rebuilding, delete the generated
`sdkconfig.autoflash-bridge*` files, or old settings are reused - but never
`sdkconfig.defaults`: a `sdkconfig.*` wildcard removes it too, the build
then silently turns Wi-Fi NVS back on, and the bridge downloads in full but
never starts. Check a rebuild against the published bridge: built from the
same source it differs only in the build time and date, the ELF hash and the
image checksum.

Build and publish each chip's bridge with `tools/repo/update_firmware.py`, which
builds it in Docker from a clean copy of this folder, checks it, and writes the
file, `artifacts/firmware.json`, `artifacts/SHA256SUMS` and `.gitignore`, removing
the file it replaces:

```sh
python3 tools/repo/update_firmware.py esp32c3 bridge   # wrapped for the S60's updater
python3 tools/repo/update_firmware.py esp32 bridge
```

The ESP32-C3's is published wrapped, with the version from the image and the
S60's model; the run wraps the same app again for any other kind of plug. Check
it with `python3 tools/espressif/analyze_vendor_ota.py <file>`.

When it cannot join the stock network, or stock saved none, it creates
`autoflash-recovery-XXXX` (the last four hex digits of the plug's Wi-Fi MAC),
an open network. Browse to `http://192.168.4.1/` to upload a native ESP32-C3
application image. The browser sends the selected file as a raw request body;
JavaScript must be enabled.

The same bridge images a stock plug without opening it or converting it:
`tools/espressif/flash_readout.py` installs it, reads and checks the dump through
`GET /flash` and `GET /efuse`, sends nothing to `/update`, and walks you through
putting the plug back to stock. The bridge's own write is the startup otadata
selection that returns the plug to stock on the next power cycle. The stock updater
writes it over the inactive slot, so whatever was there (often the previous Sonoff
version) is lost; the running stock version, bootloader, partition table and NVS are
kept.

Do not deploy this bridge until its build artifact and wrapped OTA file pass
the offline validators and a compatible final Tasmota image has been prepared.
