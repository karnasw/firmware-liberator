# Fixes sent upstream

Building this converter pushed ESP32 and Beken firmware, HTTP clients and OTA
code harder than most projects: firmware downloaded in pieces, over Wi-Fi that
may drop at any moment, by a device that has to survive it. Where a bug lay in
another project, it was chased upstream and fixed there. This page lists those
fixes.

<br>

## Contents

- [HTTP clients that waited for ever](#http-clients-that-waited-for-ever) -
  Tasmota, arduino-esp32, ESPEasy, LibreTiny
- [Firmware updates that never gave up](#firmware-updates-that-never-gave-up) -
  arduino-esp32, ESP-IDF, Tasmota
- [A build failure, followed to its root](#a-build-failure-followed-to-its-root) -
  LibreTiny, SCons
- [The emulator's Wi-Fi](#the-emulators-wi-fi) - QEMU (lcgamboa's fork)
- [The emulator's classic ESP32](#the-emulators-classic-esp32) - QEMU (lcgamboa's fork, Espressif)
- [OpenBeken on a 4 MB BK7238](#openbeken-on-a-4-mb-bk7238) - OpenBeken,
  beken_freertos_sdk
- [A Beken device on ESPHome giving up an address it still uses](#a-beken-device-on-esphome-giving-up-an-address-it-still-uses) -
  LibreTiny, ESPHome
- [A security fix, reported privately](#a-security-fix-reported-privately) -
  Tasmota
- [Smaller fixes found along the way](#smaller-fixes-found-along-the-way) -
  Tasmota, ESPHome, OpenBeken
- [The POWCT's status LED](#the-powcts-status-led) - Tasmota, ESPHome
- [Other people's reports, reproduced on the emulator](#other-peoples-reports-reproduced-on-the-emulator) -
  Tasmota

<br>

## HTTP clients that waited for ever

The conversion downloads firmware with Tasmota's `webclient`, and a network
drop at the wrong moment left a device answering nothing until it was switched
off and on. Tasmota's HTTP client waited for the rest of a body for as long as
the connection looked open, and a server that goes quiet without closing it
looks open for ever. The same loop came from arduino-esp32's `HTTPClient` and
was copied into ESPEasy and LibreTiny. Each now gives up once no data has
arrived for the client's timeout, as it already did for the headers.

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| Tasmota | [#25139](https://github.com/arendst/Tasmota/pull/25139) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25139?label=) | `HttpClientLight` waited for ever for the rest of a body |
| Tasmota | [#25141](https://github.com/arendst/Tasmota/pull/25141) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25141?label=) | The same in `AsyncHttpClientLight`, in its sync and async paths, and it never noticed a server closing the connection |
| Tasmota | [#25142](https://github.com/arendst/Tasmota/pull/25142) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25142?label=) | Every chunked response through `AsyncHttpClientLight` failed after 5 s |
| arduino-esp32 | [#12990](https://github.com/espressif/arduino-esp32/pull/12990) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/arduino-esp32/12990?label=) | `HTTPClient` waited for ever for the rest of a body, and polled without pause between TLS packets |
| ESPEasy | [#5649](https://github.com/letscontrolit/ESPEasy/pull/5649) | ![state](https://img.shields.io/github/pulls/detail/state/letscontrolit/ESPEasy/5649?label=) | Its copy of `HttpClientLight` had the same unbounded wait |
| LibreTiny | [#420](https://github.com/libretiny-eu/libretiny/pull/420) | ![state](https://img.shields.io/github/pulls/detail/state/libretiny-eu/libretiny/420?label=) | Its `HTTPClient` had the same unbounded wait |
| Tasmota | [#25180](https://github.com/arendst/Tasmota/pull/25180) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25180?label=) | A read timeout over 65.5 s wrapped round: `webclient.set_timeouts(80000)` gave up after 14.5 s |

**Related:** Berry's `webclient` gained the `connected()` method it had but
never exposed, in Tasmota's maintainer's
[#25145](https://github.com/arendst/Tasmota/pull/25145).

<br>

## Firmware updates that never gave up

The usual ways ESP32 firmware downloads an update stalled too, and testing them
found two more bugs:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| arduino-esp32 | [#12991](https://github.com/espressif/arduino-esp32/pull/12991) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/arduino-esp32/12991?label=) | A stalled `HTTPUpdate` blocked for about 40 minutes, not the 30 s its code intended |
| Tasmota | [#25177](https://github.com/arendst/Tasmota/pull/25177) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25177?label=) | The same in Tasmota's own OTA, attempted 4 times: about 2 h 40 min |
| ESP-IDF | [#19246](https://github.com/espressif/esp-idf/pull/19246) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/esp-idf/19246?label=) | `esp_https_ota()` retried a stalled download for ever |
| ESP-IDF | [#19243](https://github.com/espressif/esp-idf/pull/19243) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/esp-idf/19243?label=) | `esp_https_ota()` crashed when the staging and final partitions were the same |
| arduino-esp32 | [#13015](https://github.com/espressif/arduino-esp32/pull/13015) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/arduino-esp32/13015?label=) | A write that gave up left the connection open, so a page sent to a client that had stopped answering held the device 10 s per piece |

**Related:** the endless retry had been reported before, in ESP-IDF issue
[#6364](https://github.com/espressif/esp-idf/issues/6364), which was closed
without it being fixed.

<br>

## A build failure, followed to its root

Testing the LibreTiny fix on its own toolchain found two build failures, the
second rooted in SCons; LibreTiny got a workaround and SCons the fix:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| LibreTiny | [#421](https://github.com/libretiny-eu/libretiny/pull/421) | ![state](https://img.shields.io/github/pulls/detail/state/libretiny-eu/libretiny/421?label=) | Any sketch using `HTTPClient` failed to link on Beken and Realtek chips |
| LibreTiny | [#422](https://github.com/libretiny-eu/libretiny/pull/422) | ![state](https://img.shields.io/github/pulls/detail/state/libretiny-eu/libretiny/422?label=) | Every build failed with PlatformIO 6.2.0 |
| SCons | [#4924](https://github.com/SCons/scons/pull/4924) | ![state](https://img.shields.io/github/pulls/detail/state/SCons/scons/4924?label=) | Build scripts were compiled with SCons's own `from __future__ import annotations`, the root cause of LibreTiny's build failure |

<br>

## The emulator's Wi-Fi

The [emulator](emulator.md) runs on [lcgamboa's QEMU fork](https://github.com/lcgamboa/qemu),
the one QEMU that emulates the ESP32-C3's Wi-Fi. Each fix came from something
going wrong on the emulator that would not on a device:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| QEMU (lcgamboa) | [#13](https://github.com/lcgamboa/qemu/pull/13) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/13?label=) | A received frame was written into a buffer the guest's driver had taken back, corrupting its heap - a crash when the guest stopped its Wi-Fi |
| QEMU (lcgamboa) | [#14](https://github.com/lcgamboa/qemu/pull/14) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/14?label=) | On a tap or bridged network, the guest's frames carried the emulated access point's address, not its own: the host could not tell it apart, nor give it an address by its MAC |
| QEMU (lcgamboa) | [#15](https://github.com/lcgamboa/qemu/pull/15) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/15?label=) | The host could not join a network the guest raised itself - Tasmota's or ESPHome's setup network - nor could the guest stay a station while it did |
| QEMU (lcgamboa) | [#16](https://github.com/lcgamboa/qemu/pull/16) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/16?label=) | The signal strength reached the firmware 96 too high: Tasmota reported its Wi-Fi at +71 to +78 dBm |
| QEMU (lcgamboa) | [#17](https://github.com/lcgamboa/qemu/pull/17) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/17?label=) | Run on its own, every input pin read low, so Tasmota saw its button held and reset itself; the levels can now be set |
| QEMU (lcgamboa) | [#18](https://github.com/lcgamboa/qemu/pull/18) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/18?label=) | `-monitor` bound its socket and never answered |
| QEMU (lcgamboa) | [#19](https://github.com/lcgamboa/qemu/pull/19) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/19?label=) | A guest's Wi-Fi could not be taken away, so reconnect and fallback code could not be tested: a property now switches the emulated access points off |

The emulator carries #18 and #19 as patches `0001` and `0002` until its pin
moves past them.

<br>

## The emulator's classic ESP32

Devices with a classic ESP32 run on the same fork, whose cache emulation comes
from Espressif's QEMU. Each flash operation retranslated a whole 4 MB cache
window, so Tasmota took about 10 s to boot:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| QEMU (lcgamboa) | [#20](https://github.com/lcgamboa/qemu/pull/20) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/20?label=) | After a flash operation, only the cache pages whose bytes changed are refreshed: Tasmota boots in about 7 s, and the reads take 14-18 s |
| QEMU (Espressif) | [#211](https://github.com/espressif/qemu/pull/211) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/qemu/211?label=) | The same change where the code came from; ESP-IDF's flash tests 10-35% faster |

The emulator carries #20 as its patch `0003` until its pin moves past the
merge. Its own `0004`, faster again, gives up QEMU's fault for reading flash
with the cache off, so it is not offered upstream
([more](../tools/espressif/emulator/README.md#what-is-changed-and-why)).

On a classic ESP32 Tasmota also found no power meter: the UART it answers on
held each answer 18 ms, where Tasmota waits 6. The emulator carries #21 as its
patch `0006`:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| QEMU (lcgamboa) | [#21](https://github.com/lcgamboa/qemu/pull/21) | ![state](https://img.shields.io/github/pulls/detail/state/lcgamboa/qemu/21?label=) | A UART on REF_TICK, as Arduino-ESP32 runs it, held received bytes 64 times its RX timeout |
| QEMU (Espressif) | [#212](https://github.com/espressif/qemu/pull/212) | ![state](https://img.shields.io/github/pulls/detail/state/espressif/qemu/212?label=) | The same, and a UART on REF_TICK running 40 times too fast, which hid it |

<br>

## OpenBeken on a 4 MB BK7238

The Sonoff S61 is a Beken BK7238 with 4 MB of flash. OpenBeken's BK7238 build
assumed the SDK's 2 MB partition table, which on 4 MB puts its OTA slot and
config inside the stock app partition, and the SDK could not pack a 4 MB image.
The fixes make the build follow the chip; the 4 MB bootloader it now ships is,
byte for byte, the S61's factory one.

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| beken_freertos_sdk (NonPIayerCharacter) | [#13](https://github.com/NonPIayerCharacter/beken_freertos_sdk/pull/13) | ![state](https://img.shields.io/github/pulls/detail/state/NonPIayerCharacter/beken_freertos_sdk/13?label=) | No 4 MB BK7238 image could be packed |
| OpenBeken (OpenBK7231T_App) | [#2290](https://github.com/openshwprojects/OpenBK7231T_App/pull/2290) | ![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/OpenBK7231T_App/2290?label=) | No way to build for a 4 MB flash: the OTA slot, flash bounds and LittleFS were fixed to 2 MB |

Proven in the BekenEmulator on a dump of a real S61
([docs/beken-research.md](beken-research.md)).

**Related:** the BekenEmulator had no licence;
[issue #2](https://github.com/openshwprojects/BekenEmulator/issues/2) ![state](https://img.shields.io/github/issues/detail/state/openshwprojects/BekenEmulator/2?label=)
asked for one, and it is GPL-3.0 since 7 November 2026. It was rewritten in C++
the same day, so this project's fixes to it will be ported once that settles.

<br>

## A Beken device on ESPHome giving up an address it still uses

After a router outage long enough for it to raise its own network, the S61 on
ESPHome sometimes rejoined and then released its DHCP lease, leaving the router
free to give the address away. Two bugs together cause it:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| LibreTiny | [#446](https://github.com/libretiny-eu/libretiny/pull/446) | ![state](https://img.shields.io/github/pulls/detail/state/libretiny-eu/libretiny/446?label=) | On Beken chips a scan that found no networks never sent its scan-done event, so a caller waited out its own timeout - ESPHome up to 31 s a scan while the router was off |
| ESPHome | [#22797](https://github.com/esphome/esphome/pull/22797) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/esphome/22797?label=) | With a portal up, the skipped adapter restart left the last error set, so a connection made during the cooldown was reported, then dropped at once |

Proven over 48 router outages in the BekenEmulator: with both fixes the
address was never released
([the trace](beken-research.md#esphome-releasing-the-address-it-keeps)).

<br>

## A security fix, reported privately

A weakness in Tasmota's web server was reported to its maintainers privately on
2 November 2026, with a fix offered. Tasmota fixed it on 4 November, in
[39146bb](https://github.com/arendst/Tasmota/commit/39146bb7b789b2394f22ba2dfb9b17ddd09b9f0e),
and the fix reaches users in its next release.

<br>

## Smaller fixes found along the way

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| Tasmota | [#25163](https://github.com/arendst/Tasmota/pull/25163) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25163?label=) | Berry's `webclient` logged a connection timeout's error code as its time: "Connection timeout after -1 ms" |
| Tasmota | [#25169](https://github.com/arendst/Tasmota/pull/25169) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25169?label=) | Its setup network went out under the chip's default name (`ESP_` and its MAC) for its first tenth of a second, which phones and computers then list beside the real one |
| Tasmota | [#25241](https://github.com/arendst/Tasmota/pull/25241) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25241?label=) | The Partition Wizard deleted any data partition after the file system, losing the Sonoff identity kept there; now it asks first |
| Tasmota | [Tasmota-Extensions #7](https://github.com/tasmota/Tasmota-Extensions/pull/7) | ![state](https://img.shields.io/github/pulls/detail/state/tasmota/Tasmota-Extensions/7?label=) | #25241's fix, ported to the Partition Wizard in the extension catalog that devices install it from |
| ESPHome | [#21672](https://github.com/esphome/esphome/pull/21672) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/esphome/21672?label=) | An `api` action with an array variable that also responds did not compile |
| ESPHome | [#22163](https://github.com/esphome/esphome/pull/22163) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/esphome/22163?label=) | A friendly name always joined the MAC suffix with a space; now the separator can be chosen |
| ESPHome | [esphome.io #8011](https://github.com/esphome/esphome.io/pull/8011) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/esphome.io/8011?label=) | The documentation for #22163's option, and what `name_add_mac_suffix` does to the friendly name |
| ESPHome | [#22224](https://github.com/esphome/esphome/pull/22224) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/esphome/22224?label=) | The CSE7766 logged expected noise at DEBUG on nearly every frame of a resistive load; now at VERBOSE |
| OpenBeken | [#2293](https://github.com/openshwprojects/OpenBK7231T_App/pull/2293) | ![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/OpenBK7231T_App/2293?label=) | No way for a driver to act before an HTTP OTA restarts the device; the S61's interim firmware uses the new hook to erase its settings |
| OpenBeken | [#2309](https://github.com/openshwprojects/OpenBK7231T_App/pull/2309) | ![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/OpenBK7231T_App/2309?label=) | On `beken_freertos_sdk` chips a busy serial log lost a byte in every 127, and `logtype direct` cut lines at 126 characters |
| OpenBeken | [#2310](https://github.com/openshwprojects/OpenBK7231T_App/pull/2310) | ![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/OpenBK7231T_App/2310?label=) | A request body arriving after its headers was never read: `POST /api/cmnd` ran nothing. The tool sends such a command again meanwhile (`openbeken.command`) |
| OpenBeken | [#2311](https://github.com/openshwprojects/OpenBK7231T_App/pull/2311) | ![state](https://img.shields.io/github/pulls/detail/state/openshwprojects/OpenBK7231T_App/2311?label=) | `ota_http` answered "OK" to a download it did not start, and a second download could reset the first. The tool restarts OpenBeken where this bites (`onward.serve_to_device`) |

<br>

## The POWCT's status LED

Sonoff's POWCT firmware drives its status LED (GPIO13) high with the relay;
one of Tasmota's two templates and ESPHome's page had it inverted
([devices/sonoff-powct](../devices/sonoff-powct/README.md#the-status-leds-polarity)):

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| Tasmota | [#25243](https://github.com/arendst/Tasmota/pull/25243) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25243?label=) | The POWCT template in the TM1621 driver's comment had the LED inverted; the CSE7761 driver's copy was fixed in 2024 |
| ESPHome | [devices.esphome.io #1976](https://github.com/esphome/devices.esphome.io/pull/1976) | ![state](https://img.shields.io/github/pulls/detail/state/esphome/devices.esphome.io/1976?label=) | ESPHome's page for the POWCT had the LED inverted |

<br>

## Other people's reports, reproduced on the emulator

Tasmota's open reports were searched for problems the emulator could reproduce
that the owner of a converted device could meet:

| Project | Pull request | Status | What it fixes |
|---|---|---|---|
| Tasmota | [#25183](https://github.com/arendst/Tasmota/pull/25183) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25183?label=) | On ESP32, `WifiConfig 2` gave a setup network with no captive portal, which took no Wi-Fi details |
| Tasmota | [#25184](https://github.com/arendst/Tasmota/pull/25184) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25184?label=) | A password with a `;` saved from a settings page was cut there and the rest run as a command |
| Tasmota | [#25185](https://github.com/arendst/Tasmota/pull/25185) | ![state](https://img.shields.io/github/pulls/detail/state/arendst/Tasmota/25185?label=) | Matter could not encode a string longer than 255 bytes |

<br>

## How each fix was proven

Every fix was tested before and after, on the same cases, with the results in
its pull request. Most testing ran on this project's [emulator](emulator.md):
unmodified firmware on a real S60's flash, behind an emulated router that can
restart, fed by a test server that sends bodies whole, chunked, slowly or
stopping part way, several devices side by side. The Tasmota and arduino-esp32
fixes ran again on a real S60, power-cycled by a second smart plug: every hang
the emulator showed, the device showed too, and every fix behaved alike.

<br>

## Related

[How the tool is tested](testing.md): the unit tests, the emulator and the
fault suites that found these bugs, and what has been run on real devices.
