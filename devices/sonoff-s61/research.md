# Sonoff S61: research

What is known of the S61's own hardware, and what OpenBeken was seen to do on
it in the emulator. Its firmware is the one the Beken research was done on:
[beken-research.md](../../docs/beken-research.md).

## Energy-meter calibration: the stock formula, and the OpenBeken mapping

The meter is a **BL0937**, whose stock driver is registered as `bl0937b`. Its
factory calibration is the EF40 key **`elecCalibCoef`** (80 B): a `u32` magic,
a flag byte (`1`), padding, then **three 24-byte records in the order current,
voltage, power**, each beginning with three `int32` **`a, b, c`** (the rest is
unused). The setter at `0x31160` writes that shape. The getter at `0x311ac`
checks the magic and flag and hands the 72-byte payload to the driver's init
(`0x301c0`), which copies each record's `a, b, c` into the driver's
calibration object (`0x404bbc`, mode 1).

The driver's measurement function (`0x41a84`; `0x41918` is its uncalibrated
twin) works in doubles. From the pulse period it forms the frequency `f` (Hz)
and the **HLW8012 datasheet's nominal value**, with these board constants:

| quantity | nominal from `f` | constants |
|---|---|---|
| current | `I = f · 1.1 · 1000 / 94638` | 94638 (datasheet), 1 mΩ shunt |
| voltage | `V = f · 1.1 · 2040 / 15397` | 15397 (datasheet), 2040:1 divider |
| power   | `P = f · 1.21 · 2040 · 1000 / 1721506` | 1721506 (datasheet), 1.21 = 1.1² |

The 1.1 plays the part of V_ref; the BL0937's nominal is 1.218 V, which is why
the factory trims below come out at about 0.88 to 0.95. The driver then
applies the factory trim:

```
x_cal = a·x²/1e7 + b·x/1e6 + c/1e6
```

This is the same CoolKit quadratic mvoss96 found in the S60's
`calib_coefficient0` (adamsthws/firmware-liberator PR #7). A current below
3 mA zeroes both current and power. On the dump `a = c = 0` for all three and
`b = 954547 / 879893 / 930747` (I/V/P): a purely linear trim.

**OpenBeken's BL0937 driver measures the same `f`.** It counts falling edges
of CF and CF1 each second (`drv_bl0937.c`) and scales `real = f · cal`
(`drv_pwrCal.c`, `PWR_CAL_MULTIPLY`). So for a linear trim the mapping is
exact:

```
cal_I = (1.1·1000/94638)        · b_I/1e6   = 0.011623 · 0.954547 = 0.011095
cal_V = (1.1·2040/15397)        · b_V/1e6   = 0.145743 · 0.879893 = 0.128238
cal_P = (1.21·2040·1000/1721506)· b_P/1e6   = 1.433861 · 0.930747 = 1.334562
```

OpenBeken's generic defaults are 0.0118577 / 0.1325301 / 1.5: the same
magnitudes, an independent check that `f` means the same thing in both. The
tool's driver applies these with `CFG_SetPowerMeasurementCalibrationFloat` on
first boot, and sets `PowerMax` to the S61's 3840 W rating. A non-zero `a` or
`c` has no OpenBeken equivalent; the driver then uses `b` alone and logs a
warning. Not yet done: a check against a real metered load.

**In the emulator** the board gives the BL0937's pulses at the rates this
calibration makes of a 100 W load on 230 V: CF 75 Hz, and CF1 1794 Hz for the
voltage and 39 Hz for the current
([the board](../../tools/beken/emulator/README.md#the-devices-board-boardpy)).
Each firmware reads them back:

| Firmware | Relay on | Relay off |
|---|---|---|
| OpenBeken | 248-250 V, 103-105 W | 0 W |
| ESPHome | 244 V, 103 W | 0 W |
| Sonoff's | 230.22 V, 0.43 A, 100.00 W | 230.22 V and nothing else |

OpenBeken and ESPHome read a little high, from the firmware's clock in the
emulator; Sonoff's firmware times each period by the chip's clock. Which way
SEL selects was settled from three sources, not yet on hardware:
OpenBeken's template (`SEL`, not `SEL_n`: voltage on high), Tasmota's BL0937
driver (voltage on high unless the pin is `NRG_SEL_i`) and ESPHome's `hlw8012`
page ("When SEL=0 current is measured, when SEL=1 voltage is measured" for the
BL0937).

## How Sonoff's firmware reads the meter

Read from the public dump's app (destriped with `tools/beken/unstripe.py`,
disassembled with `disasm.py`) and checked by running it in the emulator. The
driver, `bl0937b` (its ops at `0x1657e4`: open `0x41f64`, the measurement
`0x41918` and `0x41a84`), counts no GPIO interrupts:

- **Open** sets SEL (P24) as an output, low, and puts two channels of the
  chip's PWM block (pwm_new, `0x802B00`) into capture: channel 0 (P6, CF1) and
  channel 2 (P8, CF). Each goes through `0x3f090(channel, 0, handler)`, whose
  mode 0 is pwm_new's `PWM_CAP_POS_MODE` (4): rising edges, on the 26 MHz
  clock. The handler, `0x4203c(channel, count)`, is the PWM interrupt's
  callback (`PWM_CAP_FUNC`), given the count the channel latched for one
  period.
- **SEL is switched for each sample.** For CF1 the handler reads SEL back.
  With SEL low (the current) it drops the first two periods after a switch,
  and with SEL high (the voltage) the first five. It keeps the next in a
  five-entry ring for that quantity, and switches SEL. CF's periods go in a
  ring of their own.
- **The measurement** turns each ring's period into microseconds (count + 1,
  over 26) and a frequency, then applies the formula above (the doubles 1.1,
  1000, 94638, 2040, 15397, 1.21 and 1721506 sit in its literal pool), and
  zeroes current and power below 3 mA.
- **To the cloud** it reports `voltage` (0.01 V), `current` (0.01 A) and
  `power` (0.01 W) with its other parameters, and again while the app asks for
  readings (`uiActive`).

The emulator's PWM block captures nothing by itself, so the firmware read and
reported 0 V, 0 A and 0 W until the board gave its capture channels their
counts
([the board](../../tools/beken/emulator/README.md#the-devices-board-boardpy)).

## The S61's wiring (proven OpenBeken template)

The OpenBeken device database carries an entry for exactly this board,
**S61STPF / `WBBK_01P_V1.3`**, verified on hardware by its author
(elektroda.com topic 4157852; `OpenBekenIOT/webapp` `devices.json`):

| pin | role | channel |
|---|---|---|
| P26 | `Rel` (relay) | 1 |
| P9 | `Btn_Tgl_All` (button) | 1 |
| P0 | `LED_n` (relay/power LED, active-low) | 1 |
| P1 | `WifiLED_n` (Wi-Fi LED, active-low) | 2 |
| P8 | `BL0937CF` (power pulses) | – |
| P6 | `BL0937CF1` (V/I pulses) | – |
| P24 | `BL0937SEL` | – |

It agrees with the stock driver. The 6-byte parameter block its init passes to
`bl0937b::open()` (`[0x13548c]+0x24` = `26 00 00 00 02 18`) ends in `0x18`,
P24 for SEL, and its bytes 2-4 are the PWM channels that capture CF1 (0, on
P6, given twice, for the current and the voltage) and CF (2, on P8)
([above](#how-sonoffs-firmware-reads-the-meter)). What the first byte, `0x26`,
means was not worked out; reading it as `8<<2 | 6`, CF and CF1 packed, did not
hold.

The template's `loglevel 0` worked around UART logging disturbing pulse
counting on BK7238, which was fixed upstream in 1.18.235 (PR #1930); the
release this project builds on has the fix. The tool's driver applies this
template on first boot, with `SetStartValue 1 -1` so the relay restores its
last state after a power cut, as the tool sets on the S60. The ESPHome config
([`devices/sonoff-s61/esphome.yaml`](esphome.yaml), on LibreTiny) uses the
same pins (`hlw8012: model: BL0937, sel_pin: P24, cf_pin: P8, cf1_pin: P6`);
see [the ESPHome route](../../docs/beken-research.md#the-esphome-route-libretiny).

## OpenBeken on the S61: what the emulator showed

The S61's table for [the tool's OpenBeken driver](../../tools/beken/openbeken/README.md),
[`drv_autoflash_sonoff_s61.c`](drv_autoflash_sonoff_s61.c), holds where its
stock store (`0x3e0000`-`0x3ec000`), download slot (`0x286000`) and
OpenBeken's settings (`0x3e1000`) sit, the wiring and the meter calibration
above.

These runs were made in the BekenEmulator when the driver was still the S61's
own import module, `drv_sonoff_s61_import.c`. Its names were the older ones:
`S61ImportWiFi`, `S61RevertToStock` and the log prefixes `S61Import:` and
`S61Revert:` for today's `AutoflashImportWiFi`, `AutoflashRevertToStock`,
`AutoflashImport:` and `AutoflashRevert:`, and it named the device `S61`, now
`openbeken-abcdef`.

**The import.** On a 4 MB flash shaped like a converted device (the OpenBeken
app, and the stock EF40 at `0x3e0000` provisioned with a test SSID), the
first boot read the stock credentials and joined, with nothing typed:

```
Info:CFG:S61Import: imported stock Wi-Fi SSID 'MyTestNet' (11-char pass)
Info:MAIN:Using SSID [MyTestNet]
[WIFI] ...completed the four-way handshake; the link is encrypted
[NET]  DHCP: device ... got 192.168.1.100
```

**The whole conversion.** The stock `rt_ota` bootloader applies the 4 MB-table
build's `.rbl` staged in the stock download partition (`0x286000`):
`OTA firmware(app) upgrade(1.0.2->s61import)` → `partition 'app' erase
success`. OpenBeken boots, imports from the untouched EF40 and joins:

```
[I/OTA] OTA firmware(app) upgrade(1.0.2->s61v6) startup.
S61Import: imported stock Wi-Fi SSID 'MyTestNet' (11-char pass)
S61Import: stock BL0937 factory trim b=954547/879893/930747 (I/V/P) -> OpenBeken cal I=0.011095 V=0.128238 P=1.334562, PowerMax 3840 W
S61Import: applied the S61 pin template (Rel P26, Btn P9, LED P0, WifiLED P1, BL0937 CF P8 CF1 P6 SEL P24); relay restores its last state
Info:MAIN:Started BL0937.
```

The config OpenBeken then saved at `0x3e1000` decodes to: P26 `IOR_Relay`
ch 1, P9 `IOR_Button_ToggleAll` ch 1, P0 `IOR_LED_n` ch 1, P1
`IOR_LED_WIFI_n` ch 2, P8 `IOR_BL0937_CF`, P6 `IOR_BL0937_CF1`, P24
`IOR_BL0937_SEL`; `startChannelValues[1] = -1`; cal `V=0.128238 I=0.011095
P=1.334562 PowerMax=3840`.

**Reverting** works both ways it was tried. Staged by hand on a converted
device, the bootloader logs `upgrade(s61v2->1.0.2)`, rewrites `app`, and the
stock firmware boots with the EasyFlash store intact. End to end through
OpenBeken, with the stock RBL served from the host and `S61RevertToStock
<url>` sent over HTTP, the log reads `init OTA, startaddr 0x286000` →
`Rebooting in 1 seconds` → `OTA firmware(app) upgrade(s61v6->1.0.2) startup`
→ `The partition 'app' erase success` → `EasyFlash V4.1.99 is initialize
success`, with the same 33 EF40 keys still there afterwards.

**The revert command checks two addresses**, the table's slot and the
downloader's. On a 2 MB-table build it refuses (`OTA slot is 0x132000, not the
stock download slot 0x286000 - refusing`). On the first 4 MB-table build,
without the `hal_ota.h` patch, the downloader still wrote to `0x132000` and
the stock bootloader rebooted straight back into OpenBeken.

**The two upstream patches** were checked on a clean clone. The default 2 MB
build is unchanged: its config is at `0x1e1000`, and a REST read beyond 2 MB
is refused. The `FLASH_SIZE=4M` QIO boots in the emulator on a 4 MB flash,
saves its config at `0x3e1000`, answers `GET /api/flash/3e0000-1000`, and
`ota_http` logs `init OTA, startaddr 0x286000`. Its own 4 MB bootloader
applies the download there on reboot: `upgrade(4M.1220->s61v6)` →
`_STREAM_END` → slot erased.

After that OpenBeken-initiated watchdog reboot the emulator stays at the
bootloader banner. That is the emulator's state, not the image's: the flash it
saved verifies against its RBL header's FNV-1a hash and, loaded afresh, boots
s61v6 and joins, and the same RBL applied from a bootloader-only boot of the
same flash comes up first time.
