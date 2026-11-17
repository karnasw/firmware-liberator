# Beken research

What is known about the eWeLink firmware on Sonoff's BK7238 devices, and how
each thing is known, so you need not repeat the work.
[device-research.md](device-research.md) lists the rest, by chip family and by
device; [Espressif research](espressif-research.md) is laid out the same way.

Most of it was worked out on the S61's firmware 1.0.2 in the Beken emulator
([tools/beken/emulator/](../tools/beken/emulator/README.md)), and holds for
every 4 MB eWeLink build ([one code path](#one-code-path-for-every-4-mb-ewelink-build)).
Addresses are the S61 1.0.2's; the tools find each one they need in the
firmware itself ([from what](../tools/beken/README.md#static-analysis)). A
claim was tested in the emulator unless the text says otherwise. No real Beken
device has been converted yet ([Questions still open](#questions-still-open)).

Per-device identifiers stay out of this repository: examples use the made-up
MAC `12:34:56:ab:cd:ef`, and a device's own UUID, MAC and keys are read from
its dump when a tool runs and stay in your private `captures/`. The legal
footing for studying the firmware is in
[legal.md](legal.md#studying-and-emulating-the-firmware).

## Devices

### One code path for every 4 MB eWeLink build

The emulator helps the stock firmware at a few places (the integrity gate,
the credential read, the cloud dial, the trust anchor), and
[`tools/beken/stock_points.py`](../tools/beken/stock_points.py) finds each in
the firmware itself. On every file below, each point gave one answer and the
firmware reached ONLINE on the stub. Apart from the S61's own dump, the files
ran on the S61's bootloader and settings, so this shows each firmware comes up,
not how a real device's hardware behaves.

| Device | Firmware name | Version |
|---|---|---|
| S61 | `FWSW-01-S61-BK7238` | 1.0.2 (dump), 1.1.0 |
| Basic Gen5 (BASIC-1GS) | `FWSW-01-BASICR5-BK7238` | 1.0.4 |
| BASIC-1GSP | `FWSW-01-BASIC1GSP-BK7238` | 1.0.5 |
| S41 | `FWSW-01-S41-BK7238` | 1.0.6 |
| T6-3C | `FWSW-06-SWITCHES-BK7238` | 1.0.3 |

### Adding a device from a pin map

A firmware that comes up is not enough: OpenBeken and ESPHome need the
device's pins, which are never guessed. The Basic Gen5 was added from a published map,
[one post](https://www.elektroda.com/rtvforum/topic4167932.html) and OpenBeken's
list; [its page](../devices/sonoff-basic-1gs/README.md) says what has been run.
Three devices stay out for want of one:

- **BASIC-1GSP**: no published pin map. It has a meter, and forum titles mention
  a pulse-driven latching relay, which a wrong map could harm.
- **S41**: no published pin map.
- **T6**: one unit's pins only, and its touch panel speaks a UART protocol that
  OpenBeken has no driver for
  ([saltech1's ESPHome work](https://github.com/saltech1/esphome-sonoff-t6-uart)).

The Basic Gen5, S41 and T6 firmware lacks the `uiActive` string that the S61's and
the BASIC-1GSP's have. A check of such a device back on Sonoff's firmware re-sends
`sledOnline` instead, which has been shown only on the stub.

### 2 MB devices: on hold

The public door sensor is the one 2 MB device found, and it is a different
firmware family, not a smaller S61: colink rather than `ais_*`, no Matter, no
`EF40` store, and plain RBL updates with no FOTA container. Its `NET info` at
`0x1e1000` holds data, the MAC among it, where OpenBeken's 2 MB build writes
its config, so a conversion would destroy it unread. These devices need a route
of their own.

## Flash and layouts

<a id="their-flash"></a>

### Partition tables

Each app holds the SDK's partition table as entries of owner, description, start,
length and options, so the table can be read from an update file by its
description strings ([`tools/beken/layouts.py`](../tools/beken/layouts.py)).

| Partition | 4 MB (every device [above](#one-code-path-for-every-4-mb-ewelink-build)) | 2 MB (door sensor) |
|---|---|---|
| Bootloader | `0x0`, `0xf000` | the same |
| Application | `0x11000`, `0x250000` | `0x11000`, `0x121000` |
| OTA | `0x286000`, `0x15a000` | `0x132000`, `0xae000` |
| RF Firmware | `0x3e0000`, `0x1000` | `0x1e0000`, `0x1000` |
| NET info | `0x3e1000`, `0x1000` | `0x1e1000`, `0x1000` |

The 4 MB table has further entries, the same in all five files: NVS `0x3e2000`,
`0xb000`; KEY `0x3ed000`, `0x1000`; matter info backup `0x3ee000`, `0x8000`;
matter info `0x3f6000`, `0x8000`; matter factory info `0x3fe000`, `0x2000`. The OTA
entry's description is lowercase `ota`.

The files' strings name their stock models: `SN-BK7238-BASICR5-01`,
`-BASIC1GSP-01`, `-S41-01`, `-T6C3-01`. A register message takes its model
from factory data, not those strings; only the S61's has been seen.

The bootloader holds an RT-Thread FAL table that agrees, and the update files'
apps, at 1.35 to 1.41 MB, would not fit a 2 MB table's application partition.
That these devices have 4 MB of flash is inferred from the files, not from a
read-out. What is known of the Basic Gen5's own flash, and the one read that
settles it, is in [its research](../devices/sonoff-basic-1gs/research.md).

### The S61's flash, as dumped

Code regions are stored CRC-striped: 32 data bytes, then a 2-byte CRC,
repeating. Remove the stripes (`unstripe.py`) before searching code for strings
or RBL headers. The config regions are not striped: the firmware reads them as
raw physical bytes through the flash controller.

| Partition  | Offset     | Size      | Notes |
|------------|------------|-----------|-------|
| bootloader | `0x000000` | `0x010000`| RT-Thread + FAL + `rt_ota` (`V0.2.8-beken`) |
| app        | `0x010000` | `0x250000`| the running firmware |
| download   | `0x286000` | `0x15A000`| staged OTA slot (erased in this dump: no update staged) |
| factory TLV| `0x3e0000` | (0x1e0 used)| manufacturing store: MAC and radio calibration |
| EF40 app   | `0x3e2000` | `0x9000`  | EasyFlash V4 KV store: device config and credentials (active write sector `0x3e3000`) |
| EF40 sys   | `0x3f6000` | `0x8000`  | EasyFlash V4 CHIP/ENV store |

The bootloader and the app are given as the bootloader's FAL table gives them,
with the stripes removed. With its stripes the app's `0x250000` takes `0x275000`
of flash, from `0x11000` up to the download slot, which is how the SDK's table
(above) and LibreTiny's ([below](#the-esphome-route-libretiny)) give it.

The `app` partition's own RBL header sits at `0x285f9a`, just before the
download partition. Overwriting it only makes the bootloader log `App verify
failed! Need to recovery factory firmware.` and boot the existing app, so leave
it alone. Both places were found by logging the bootloader's reads through the
flash controller (`REG_FLASH`, `0x803000`) during boot.

An update copies `download` to `app` and touches none of the three config
regions, so everything in
[A device's identity and stores](#a-devices-identity-and-stores) survives a
conversion: carrying the Wi-Fi across and going back to Sonoff's firmware both
rest on that.

### Sonoff's layout is the SDK's 4 MB table

The Beken SDK's 4 MB table (`BkDriverFlash.c`, `bk7231_partitions_4M`) is
Sonoff's layout to the byte: `APPLICATION 0x11000`, `OTA
0x286000` (the stock download slot), `RF_FIRMWARE 0x3e0000` (the factory record
with the radio calibration), `NET_PARAM 0x3e1000` (empty on a stock device),
`MATTER_FLASH 0x3e2000` (the EF40 app store) and `USR_CONFIG 0x3ee000`.

The S61's factory bootloader is the SDK's too, byte for byte: the shipped
`bootloader_bk7238_uart1_v1.0.15.bin` with the 4 MB FAL table in its tail. The
table is three 64-byte `01PE` entries (`bootloader 0x0+0x10000`,
`app 0x10000+0x250000`, `download 0x286000+0x15A000`), each a name, a flash
device, an offset, a length and a CRC-32 over its first 60 bytes. What that
means for an open firmware is in
[OpenBeken on the 4 MB table](#openbeken-on-the-4-mb-table).

## A device's identity and stores

<a id="0x3e0000--factory-tlv-store-mac--radio-calibration"></a>

### The factory record at `0x3e0000`: MAC and radio calibration

A nested TLV: `"TLV\0"`, a total length as `u32` (480 on the public S61s dump),
then sections. Each section is a header `tag=0x00 | marker(3) | payload_len u32`,
then entries `tag(1) | marker(3) | len u32 | value`. There are four sections,
named here as Beken's SDK names them (`bk7011_cal_pub.h`,
[beken_freertos_sdk](https://github.com/openshwprojects/beken_freertos_sdk)):

- **`0x111111` TXID (90 B): the device and its manufacture.** ID (`0x01`), the
  Wi-Fi/base MAC (`0x02`, 6 B), THERMAL (`0x03`), CHANNEL (`0x04`), XTAL (`0x05`),
  ADC (`0x06`, 4 B: the SAR ADC's calibration, `{u16 low, u16 high}`) and LPFCAP
  (`0x07`, 8 B). On the S61s dump the ADC values are the SDK's own defaults
  (`0x472`, `0x8E9`), so its factory stored none of its own.
- **`0x222222` TXPWR_TAB (162 B): TX power.** An enable flag, the 802.11b and
  802.11g power tables (`0x02`, `0x03`, 14 B each: one value a channel), DIF_GN20,
  the BLE power table (`0x07`, 40 B) and a calibration status. Its last 34 bytes
  are not read here.
- **`0x333333` CALI_MAIN_TX (132 B)** and **`0x444444` CALI_MAIN_RX (64 B)**: the
  radio's TX and RX DC, gain and phase calibration.

This is the radio's calibration, distinct from the energy meter's in the
[app store](#the-app-store-at-0x3e2000-settings-and-credentials). No firmware
the tool installs could make it again, and no step of a conversion writes it:

- The interim and readout OpenBeken write their settings at `0x3e1000` and their
  variables below `0x3e0000` (`rebuild_dump.INTERIM_WRITES`).
- ESPHome (LibreTiny) points the SDK at it (`board_flash.calibration`
  `0x3E0000+0x1000`) and puts nothing of its own there.

Tests hold both, and the device's backup keeps the sector (`config-0x3e0000.bin`,
`records.BEKEN_REGION`).

Both open firmwares, built for the 4 MB table, read the radio calibration at
`0x3e0000`; an upstream 2 MB build would look at `0x1e0000`, find none and take
defaults. That the SDK's closed calibration library reads this sector is inferred
from its partition table and prototypes, not seen in its code.

Nothing uses the ADC values. Neither open firmware applies them (OpenBeken's
`hal_adc_bk7231.c` and LibreTiny's `wiring_analog.c` return the raw sample),
and neither device acts on an ADC reading: the S61 meters with a BL0937
counting pulses, the Basic Gen5 has no meter, and neither OpenBeken's drivers
here nor the ESPHome configs give a pin an ADC role. Both stock firmwares (the
S61's 1.0.2, the Basic Gen5's 1.0.4) hold only the SDK's own ADC code, its
internal temperature sensor's thread (`temp_detect`, `tadc_thread_entry`),
which compensates the radio and crystal for temperature, and its `adc` console
command: no overheat watch like the BASICR4's (no `TEMP_EVENT` or
`TEMPERATURE_CHECK`). That is read from their strings, not their code.

### The app store at `0x3e2000`: settings and credentials

An EasyFlash V4 store: its sector magic is `"EF40"` and its nodes are `KV40`
records. [`tools/beken/ef40_dump.py <flash.bin>`](../tools/beken/ef40_dump.py)
lists every node of a dump, checking each CRC. These are the keys on the public
dump, a device that was paired, then reset, and never joined a network (values of
per-device secrets are left out):

| Key | Size | What it is | Of use to an open firmware? |
|-----|------|-----------|----------------------|
| `elecCalibCoef` | 80 B | the factory energy-meter calibration (binary) | yes: accurate volts, amps and watts from the first start |
| `elecCalibFlag` | 1 B | the meter is calibrated (`01`) | yes |
| `outlet&conf` | JSON | power-on state, pulse, offBrightness | yes (the relay at start-up) |
| `sled&conf` | JSON | the status LED's behaviour (`sledOnline`, relayLight) | yes |
| `elec&app` / `elec&appex` | JSON | over-power, over-voltage and over-current protection | yes (the limits) |
| `elec_region`, `elec_dst_hour` | 1/20 B | the meter's region and daylight saving | maybe |
| `wifi-ssid` / `wifi-pass` | ≤32/≤64 B | the Wi-Fi credentials, in plain text (a provisioned device only; absent on this dump) | yes: they carry the Wi-Fi across |
| `ssid` / `pass` | 34/66 B | the same, obfuscated in an encoding not yet decoded | unknown |
| `apiKey`, `app-apikey`, `domain`, `secureCertSha256`, `factory`, `user` | - | the eWeLink cloud identity and certificates, each device's own | no (the cloud is dropped) |
| `timerInfo`, `timer&revision`, `outlet&protect`, `lan&info`, `ota&*`, `recordInfo`, `fabric&list`, `RecoverFlag`, `fc_rssi`, `port` | - | timers and other eWeLink state | mostly no |

The `factory` value is the device's own factory credentials, encrypted (base64
of ciphertext), and the stock firmware does not start with it changed in any
way, as booting copies of an S61 in the emulator, each with one setting taken
out, showed ([`tools/beken/scrub.py`](../tools/beken/scrub.py) keeps it so).

A node's format, checked against real nodes:

```
"KV40"(4) | len(4) | crc32(4) | name_len(1)+0xff*3 | value_len(4) | name | value | 0x00 0x00 + 0xff*6
  len = 20 + name_len + value_len + 8
  crc = zlib.crc32( bytes from the name_len field through the end of value )   # standard crc32
```

As the region is raw, nodes can be written in place: valid `wifi-ssid` and
`wifi-pass` nodes appended to the write sector (`0x3e3000`) parse with the
right CRC, and the firmware finds them as on a provisioned device
(`ef40.provision`, which `stage_apply.py` and `cloud_push.py` use). In the
emulator the firmware reads them only once it is past
[the integrity gate](#the-flash-uid-integrity-gate).

### The system store at `0x3f6000`

A second EasyFlash V4 store, the CHIP/ENV one: boot and reboot counters
(`chip-counters.*`), `chip-factory.uniqueId`, and Matter globals (`g/*`). The
public dump has no `f/1/n` fabric or NOC in it.

### Wi-Fi credentials

A provisioned device keeps its Wi-Fi credentials in the clear, as the nodes
`wifi-ssid` and `wifi-pass`, so reading them takes only the store parsed out of
a flash dump: no decoder, no pairing and no emulator.

- **The setter, `0x488dc`** (`put ssid to wifi-ssid`, `put password to
  wifi-pass`), stores both verbatim: it copies its input with `memcpy`, takes its
  length with a plain `strlen` (`0x12214`), and calls config-set `0x5adb4` with the
  raw buffer as the value.
- **The reader, `0x48124`**, calls config-get `0x5ad84(ctx, "wifi-ssid", out, …)`
  (through `0x12f438`), which returns `0xd8` when the key is absent, as on an
  unprovisioned device.

The S60 keeps its credentials in the clear too, as JSON in NVS
(`"apikey":"…"`, `"deviceid":"…"`, `"passwd":"…"`, `"ssid":"…"`), so a
conversion carries them the same way on both, with a parser for each
container.

The one derived value is the apikey, the device's identity, unrelated to the
Wi-Fi. A UUID-format apikey appears in RAM at boot with no copy in flash: the
writer at `0x427b8` formats it character by character from a buffer of the
device's MAC and serial (cleared by the memset at `0x45ec`).

## The cloud

### Protocol and trust

- **Registration** is a `GET /dispatch/device`, whose answer names the server,
  then a TLS WebSocket (`wss://`) at `/api/ws`. The dispatch seen in the
  emulator is plain HTTP, to the factory's placeholder address
  (`http://8.8.8.8:80`); a paired device's has not been seen.
- **Trust** is mbedTLS checking against a CA the firmware carries, not a pinned
  server certificate (its "pinned session" strings are Matter's CASE sessions),
  so the S60's stub technique applies: a local stub issues its own CA, and the
  firmware is made to trust it in place of CoolKit's root. Where the S60's
  `patch_stock --trust-ca` swaps a small EC P-256 trust anchor in the flash
  image, the S61 keeps no CA in its flash and puts one together in RAM, so the
  swap is made where the CA is installed ([the handshake](#the-handshake-to-online)).

### The provisioned state

On this firmware a provisioned device is one in a cryptographic trust
relationship with an outside authority, not one with a flag set and credentials
stored. The firmware carries a full Matter commissioning stack: `NOC` (node operational
certificates), `fabric` (110 references), `PASE` (the SPAKE2+ passcode handshake),
`CASE` (certificate-authenticated sessions), device `attestation` and the setup
passcode. Beside it is the eWeLink cloud binding (`apikey`, `deviceid`,
`device_secret`). A provisioned device holds a valid fabric (a NOC and ICAC chain,
with operational keys established over CASE) and a cloud binding issued by
eWeLink.

The public dump carries only a cloud identity (`apiKey`, `app-apikey`,
`secureCertSha256`), with no Wi-Fi credentials or Matter fabric. The check at
`0x48cc4`, an event-triggered callback whose pointer is loaded near `0x54250`,
decides between provisioned and idle (the decision is at `0x48cc6`; it reads
`commission_status` and `recover_flag`, the status in `[sp+0xc]` as 0, 1 or
2).

- **An unprovisioned device**, with no credentials and no fabric, stays idle,
  waiting for a pairing trigger.
- **A provisioned device** reads `wifi-ssid` and `wifi-pass` through
  `ef_get_env_blob` (`0x12f438`) about 3.1 s into boot, and the station connect
  follows. In the emulator that needs the `app` task to be past
  [the integrity gate](#the-flash-uid-integrity-gate), which otherwise hangs it
  before the read.

**What does not work: forging the bound state.** A fabric and a cloud binding
are signed or issued with the commissioner's and the cloud's private keys, so
forged values fail the firmware's certificate and signature checks. A dump the
real cloud would accept comes only from pairing a real device with the eWeLink
app. The emulator needs none: the cloud dial uses none of the bound state
([below](#the-cloud-dial)), and a local stub accepts whatever identity it is
given.

### The cloud machinery

The eWeLink cloud is a set of AIS tasks that `ais_appfw` creates on demand:

| Task | Entry | Role |
|------|-------|------|
| `ais_appfw_task` | `0x319a8` | the app framework's state machine (it comes up even unbound, reads `wifi-ssid`, then waits) |
| `ais_link_task` | `0x35aa8` | the cloud link: dispatch (`/dispatch/device`), then the IoT WebSocket (`/api/ws`) |
| `ais_ws_task` | created near `0x385a4` | the WebSocket pump |
| `ais_httpd_task` | - | the LAN control server (`:8081`), only once online |

`ais_link_task` is a state machine driven by a context: it reads the global
context `*0x4044ac`, with its state byte at `ctx+0x290`. `ais_cloud_start`
(`0x35744`) creates it with the RTOS thread-create `0x1349f0` (the name and entry
literals are at `0x35820` and `0x35824`, giving the entry `0x35aa9`). That is a
Thumb veneer to the ARM function `0x14070`, which allocates a stack and a
`0x5c`-byte control block and copies the task's name into it; 17 call sites
start tasks through it, the key-driver timer thread's
([below](#the-button)) among them. `ais_cloud_start` has one call site, the
start-cloud handler at `0x2c204`:

```
0x2c204  if (*0x403e31 != 0 && precond_0x24fd8() == 0)   // precond returns ctx[0x31]
             ais_cloud_start(0x35744)
                 ctx = *0x4044ac;  if (ctx && ctx[0xaeb] == 0)  create ais_link_task
```

The start-cloud handler has exactly three callers, `0x48ffe`, `0x49b76` and
`0x49ebc`, all inside the eWeLink pairing state machine (`0x48a84` to `0x49cxx`,
the `PAIR_EVT_*` and `ewelink pair ok` code), so on a real device pairing
succeeding starts the dial. Pairing does not build the context: it is filled at
boot (on the heap, at `0x42c870` for one) with everything the link task needs:

| Offset | Field | Value in the public dump |
|--------|-------|--------------------------|
| `+0x0b` | device UUID | the device's apikey UUID (left out here) |
| `+0x30` | model | `SN-BK7238-S61-01` |
| `+0x51` | MAC | the device's MAC (left out here) |
| `+0x5f` | dispatch URL | `http://8.8.8.8:80`, a factory placeholder |
| `+0x31` | precondition byte | `0` (the gate passes) |

No eWeLink binding key is read at boot, and the dial needs none. So all the
dial takes from pairing is its trigger, the call of the start-cloud handler,
and it goes wherever `ctx+0x5f` points.

### Joining Wi-Fi by seeding the credentials

Seeding the stored credentials puts the emulated device on a network for real,
in the end state a real provisioning reaches, without the BLE exchange an
emulator with no radio cannot do. [`seed_join.py`](../tools/beken/emulator/seed_join.py)
hooks the firmware's own `ef_get_env_blob` (`0x12f438`) to return a chosen network
name and passphrase for `wifi-ssid` and `wifi-pass`, and attaches the fork's peer
as a WPA2-PSK access point with the same credentials. The device then runs its
genuine sequence: station mode, authenticate, associate, the WPA2 four-way
handshake, and IPv4 DHCP, which gives it `192.168.1.100`. Matter's
`BekenWiFiDriver` connect path runs the same way.

Left to itself after the join, the device does Matter work: it advertises
`_matterc._udp` (Matter commissionable) over mDNS and reads the Matter fabric
and attribute store (`g/fidx`, `g/fs/c`, `g/a/...`). It does not dial the
eWeLink cloud, whose link is created only once the device is eWeLink-paired.

### The cloud dial

Calling the start-cloud handler (`0x2c204`) once Wi-Fi is up stands in for
pairing succeeding, and makes the device dial the eWeLink cloud for real.
[`cloud_dial.py`](../tools/beken/emulator/cloud_dial.py) seeds the credentials,
waits for the join and the DHCP lease, then calls the handler. The device opens
a TCP connection to its dispatch server and sends CoolKit's
`GET /dispatch/device`, retrying with a backoff: the genuine first step of
registration.

The tool overwrites `ctx+0x5f` to point the dial at a local stub on the
emulator's own access point (`192.168.1.1:80`), so it stays on your computer;
the factory placeholder (`8.8.8.8:80`) is used only with `DISPATCH=` empty.
Never steer the dial at the real eWeLink cloud with any account or device but
your own.

With the default stub, `python3 cloud_dial.py` shows:

```
device IP = 192.168.1.100
dial-chain: {'ais_cloud_start': 1, 'link_create': 1, 'ais_link_task': 1}
TCP 192.168.1.100:62636 <-> 192.168.1.1:80 sends: HTTP GET /dispatch/device HTTP/1.1
  (retried t=34.6, 35.7, 37.2, 39.3, 41.9, 45.0, 48.6 — backoff)
```

Only the BLE GATT link is missing from this: on a real device it carries the pairing
that calls the handler ([BLE](#ble) says how far the pairing itself can be driven).

### The handshake to ONLINE

Past the dial, the handshake completes against a local stub and the device
reaches ONLINE. [`cloud_online.py`](../tools/beken/emulator/cloud_online.py)
drives it against the CoolKit stub
([`tools/ewelink/stub_cloud/stub_server.py`](../tools/ewelink/stub_cloud/stub_server.py),
with its `--plain` dispatch mode). Nothing leaves your computer: the network
relay (`attach_wifi_peer(net_out=True)`) sends the factory dispatch address to
loopback, with plaintext dispatch on one port and the TLS IoT channel on another.
The stub's side is in [cloud-stub.md](cloud-stub.md#the-beken-emulators-stub).

**The dispatch reply.** The cJSON parser in the dispatch handler (`0x34ca0`;
parse `0x264d6`, getters `0x2669c` and `0x265e2`) is strict in three ways the
S60's is not, each making the device silently ask for dispatch again:

- the reply must be compact: whitespace between tokens is rejected;
- it reads a `domain` key, the host it connects to, not only `IP`;
- `port` is a string (`"443"`), formatted with `wss://%.*s:%.*s`.

Given `{"error":0,"reason":"","IP":...,"domain":...,"port":"443"}`, the device
accepts dispatch and opens the IoT WebSocket.

**The trust anchor.** The IoT channel is `wss://` and its certificate is
verified (authmode REQUIRED), so a stub's self-signed certificate is refused
with `bad_certificate`. As on the S60, the trust anchor is swapped and the
verification left alone. The anchor built in is `CN=CoolKit Root CA ECC,
O=CoolKit, C=CN`, an EC CA of 510 bytes as DER. The S61 keeps no CA in its
image: it puts the CA together in RAM when it sets TLS up (with snprintf, around
`0x3c0b6`) and installs it through `mbedtls_x509_crt_parse` (`0x125e4c`; call
sites `0x3c0ca` and `0x3c5b8`, both reached only for the IoT client).
`cloud_online.py` swaps at that one point: when the firmware loads the CoolKit
CA into a trust store, it gives its own EC P-256 CA instead, recognising the
CoolKit one by the CN in the decoded DER so that nothing else is touched. Every
check in the verify path then runs unmodified, against the stub's certificate
signed by that CA.

**The stub's certificate** needs two things for the firmware's mbedTLS to accept
it: a `dNSName` SAN for the address dialled (an `iPAddress` SAN alone is not
matched against the name the firmware verifies, and it sends the SNI `8.8.8.8`),
and a serverAuth EKU.

`tools/beken/emulator/beken-emu.sh python tools/beken/emulator/cloud_online.py`
shows the whole sequence:

```
dispatch -> {"error":0,...,"domain":"8.8.8.8","port":"443"}   (accepted)
CA trust-swaps = 1   (CoolKit root CA -> the stub's own CA)
websocket open                                 (TLS handshake against the stub's CA)
ws <- {"action":"register","version":8,"romVersion":"1.0.2",...}
ws -> {"error":0,...,"config":{"hb":1,"hbInterval":10}}        => ONLINE
ws <- {"action":"query",...,"params":["TZ"]}   (heartbeat / timezone loop, reconnects)
```

What a stub cannot stand in for is a bound session the real cloud would
accept, which needs eWeLink's device secret from a paired device's dump.

## No restart command

As on the S60 ([Espressif research](espressif-research.md#no-restart-command)),
Sonoff's firmware has no command that only restarts the device, over the cloud or
on the local network. This is read from the strings of the public S61 dump's app,
with its stripes removed; no code was read for it.

- **The cloud.** The actions of the eWeLink link module (`ais/src/link.c`) are
  `register`, `upgrade`, `subDevice`, `resetDispatch`, `redirect`, `addNotify` and
  `notify`. Only `upgrade` restarts the device, with `bk_reboot(1)` a second after
  the update is downloaded ([the FOTA container](#the-fota-download-container)).
- **The local network.** The `/zeroconf/` strings are a live server, not a client
  or dead code: eWeLink's LAN control on port 8081 (`http://%s:8081`,
  `/zeroconf/*`, announced as `eWeLink_%s` on `_ewelink`), with which the eWeLink
  app or an iHost switches the device once it is online. It has no update route
  and no restart route.
  In the emulator, the public S61 dump on Sonoff's firmware, online on the
  stub, has port 8081 open, so the tool's `detect.identify` takes it for
  Sonoff's firmware as it does an S60's. No answer to eWeLink's discovery came
  back from it there; whether that is the firmware or the emulated network is
  not known. The tool needs neither: every device is found by the hardware
  address its eWeLink record gives.
- `bk_reboot(%d)` is only the log line inside the restart function itself.

## The update path

A device is converted to OpenBeken or ESPHome through the vendor's own update,
as the S60 is. The update path holds no signature and no secret key, so a
third-party, unsigned image is accepted: images are packed with AES-256 under
RT-Thread's public default key, which the bootloader has compiled in; the
integrity checks are a standard CRC-32 and an FNV-1a hash; and the RBL's
`crypt` field is a flag that need only be non-zero
([the bootloader's checks](#what-the-bootloader-checks)).

The whole chain: push `upgrade` with a `downloadUrl` you control; the device
downloads the image in its FOTA container and stages it, verbatim, in the
download partition; on the restart the stock bootloader decompresses and
applies it. [`fota_wrap.py`](../tools/beken/fota_wrap.py) builds the container
for any `.rbl`.

### The `upgrade` command names the server

The cloud pushes
`{"action":"upgrade","params":{"model","version","binList":[{"downloadUrl",
"digest","name"}]}}` over the IoT WebSocket, and the device's OTA task
(`ais_qota_task`, user agent `FOTA/1.0`) downloads from whatever `downloadUrl` the
command carries, over HTTP with ranged requests (`bytes=%lu-%lu`), apart from
the WebSocket session, as the S60 does. There is no update-server setting: the
command gives the address, which `tools/ewelink/probe_ota_command.py` relies on
for the S60.

In the emulator, with the device ONLINE on the local stub, the stub sends the
`upgrade` frame down the socket it holds and the device issues
`GET <downloadUrl>` as `FOTA/1.0`; served a 404, it flashes nothing, which makes
a safe probe. So whoever runs the stub both starts the update and names the
server.

### The FOTA download container

`ais_qota_task` expects not a bare RBL but CoolKit's FOTA container, worked
out from the device's read offsets and its parsing code:

- The HTTP answer must be `206 Partial Content` (the client checks for 206) and
  keep-alive (it reads several sections on one connection).
- **`[0:16]`, the header:** the magic `0x1beef11e` (little-endian) at `+0`, and a
  `u16` at `+12` that sizes a region before the trailer. Set it to `0`, so that
  the trailer sits at `+16` and descriptor offsets count from `16`.
- **`[16:40]`, the trailer:** the descriptor count at byte `+1` (1 to 3), a
  version string at `+2` that must compare greater than the running version
  (`"1.0.2"`) as text, and a CRC-32 over the trailer's first 20 bytes, big-endian,
  at `+0x14`.
- **`[40:40+76·n]`, the descriptors,** 76 bytes each: data in `[0:0x3c]` and a
  big-endian CRC-32 of it at `+0x3c`. The data holds an 18-byte image-type name at
  `+0x00`, a version at `+0x20`, a big-endian `u32` file offset at `+0x30` (the
  image's place in the file is this plus the base `16`), a big-endian `u32` size
  at `+0x34`, and a big-endian CRC-32 of the payload at `+0x38`.
- **The payloads** follow, each at the offset its descriptor gives.

The name chooses the handler: `"FWSW-01-S61-BK7238"` (an `strncmp` at
`0x2c45d`) for the one that stages to the download partition. A wrong or empty
name falls through to a handler that buffers in RAM and never writes flash, so
the download completes and nothing is staged.

The CRCs are standard CRC-32 (zlib's polynomial), stored big-endian because the
firmware swaps the field's bytes before comparing.

**Nothing transforms the bytes.** The download loop issues its ranged `GET`s,
copies each chunk into a RAM buffer and calls the image type's write callback.
The stock type's handlers are a table at `0x165604`: the name match `0x2c45d`,
the init `0x2c69d`, the write `0x2c629` and, fifth, the validate `0x2c405`. The
write goes to `0x3ee60`, which checks the offset against the download partition
(entry 2 of the SDK's partition table, looked up by `0x213f0`), writes the chunk
through the `flash` device's write (`0x19e64`, shaped like the SDK's
`ddev_write`), then reads it back
and compares, writing once more on a mismatch. The app has no FAL code of its
own: FAL's strings are only in the bootloader. This was read from the
disassembly of the public S61s dump. The validate handler is
`movs r0, #0; bx lr`: it always succeeds. The only checks are of
integrity: the FOTA magic, the trailer's and descriptors' CRC-32s, the version
comparison, and the `digest`, a SHA-256 of the file from the `upgrade` command,
which whoever pushes the command computes over whatever they serve. The payload
is written to the download partition as it arrives, byte for byte: an 8 KB test
RBL and OpenBeken's whole 507 KB `.rbl` both staged with matching bytes.

**The payload's CRC decides the restart.** It is checked only once the whole
payload is in (`0x397ce`), by when the image is already in the download partition.
With it right, the task writes `ota&state` 4 and posts the OTA event (id 1), whose
handler (`0x2c738`) waits 1 s and calls `bk_reboot(1)`, so the bootloader installs
the update straight away. With it wrong, the updater reports
`{"ota":"fail","ota_fail_reason":1}` and returns without restarting, and the
device converts only when it is next switched on.

### The RBL format

The bootloader applies `rt_ota` RBL containers.
[`tools/beken/rbl.py`](../tools/beken/rbl.py) reproduces the dump's real
`bootloader` and `app` headers exactly, and the body of every image the SDK's
packer made: the stock app's, OpenBeken's and LibreTiny's. The header is 96 bytes:

| Offset | Field | Notes |
|--------|-------|-------|
| `0x00` | magic | `"RBL\0"` |
| `0x04` | algo | `u32`: a `crypt` nibble (checked as a flag: it must be non-zero) and a compression nibble (the app's is `0x402`) |
| `0x08` | timestamp | |
| `0x0c` | name[16] | `"app"`, `"bootloader"` |
| `0x1c` | version[24] | `"1.0.2"`, say |
| `0x34` | sn[24] | |
| `0x4c` | body CRC32 | standard CRC-32 of the packaged (compressed) body |
| `0x50` | raw hash | FNV-1a-32 of the raw (decompressed) body |
| `0x54` | size_raw | the decompressed size |
| `0x58` | size_package | the packaged size (less than size_raw when compressed) |
| `0x5c` | header CRC32 | standard CRC-32 over bytes `[0:0x5c]` |

Both CRC fields are the standard CRC-32 (reflected, polynomial `0xedb88320`, as in
zlib and PNG; the bootloader's table generator at `0x9674` builds exactly that
from a 14-tap array), so `zlib.crc32` gives them. The raw hash is FNV-1a-32
(offset basis `0x811c9dc5`, prime `0x01000193`) over the decompressed image,
matching the stock app's stored value (`0x0d89f7b1`) exactly. None is a
proprietary on-chip hash: all can be computed when the image is built.

### What the bootloader checks

Whether a device takes a third-party image is the bootloader's decision; the
FOTA container is only how the image arrives. So the quickest test stages an
RBL straight into a copy of a dump, header at `0x286000` and body after it, and
boots that ([`stage_apply.py`](../tools/beken/emulator/stage_apply.py)).

On boot the bootloader runs `ota_main`, `rt_ota_check_upgrade`,
`rt_ota_upgrade` and `copy_fw_from_dl_part`. (It lives in the bootloader
partition, `0x0` to `0x10000`, at offsets equal to its run-time addresses. Its
message strings are loaded PC-relative with `adr`, so a search for data words
referring to them finds nothing; the `__func__` pointers are ordinary
literals.) Its three gates, in order:

1. **The `crypt` nibble.** If it is zero, `ota_main` logs `none encryption Not
   allow!` (`ota_main:125`) and refuses the update. The stock app declares
   `0x402` and OpenBeken's release RBLs `0x102`; both pass. The only key in the
   path is RT-Thread's public default AES key (`0123…`), which the SDK's own
   packer uses.
2. **`rt_ota_custom_verify`, a check of the link address.** It takes a CRC16 of
   the vector table and checks that an address field in the image lies in the
   `app` partition's range, `[0x10000, 0x10000+size)`: the firmware must be linked
   to run at `0x10000`; one that is not gets `The application must link to
   the flash 64KB location!`. A real OpenBeken or ESPHome build passes; only a
   made-up test body fails. Neither this nor anything else in the path is a
   signature.
3. **The checksums.** `copy_fw_from_dl_part` reads the staged body from the
   download partition (`0x7654` and `0x7218`: a bounds check, then the flash
   device's read method), applies the declared decryption and decompression, and
   verifies the body CRC-32 over the packaged body and the FNV-1a raw hash over
   the decompressed image, ending in `part_fw_hash_verify`.

Compression is chosen by the algorithm's compression nibble. The SDK's
decompressor is an LZMA-family range coder, and a genuine OpenBeken release RBL
(gzip, nibble `1`, where the stock app uses `4`) decompresses on the stock
bootloader. The hardware security block at `0x0080b400` fires once at boot and
has nothing to do with the update.

With all three satisfied, a test image with a made-up body (gate 2 forced, as
it guards no secret; the emulator can force gate 1's branch too) is applied in
full:

```
OTA firmware(app) upgrade(1.0.2->9.9.9) startup
The partition 'app' is erasing  →  erase success
OTA Write: [app] [====================================] 100%
```

### A real OpenBeken image converts

OpenBeken already ships BK7238 images in this `rt_ota` format, the
`OpenBK7238_<ver>.rbl` assets of the
[OpenBK7231T_App releases](https://github.com/openshwprojects/OpenBK7231T_App/releases). Release `1.18.319`'s is a well-formed RBL:
magic `RBL\0`, name `app`, `algo = 0x102` (crypt flag `2`, compression nibble
`1`), a body CRC-32 that `zlib.crc32` gives, an FNV-1a raw hash, `size_raw` 750544
and `size_package` 506976.

Staged in the download partition of a copy of the dump and booted on the stock
bootloader with nothing forced, it passes every gate:

```
OTA firmware(app) upgrade(1.0.2->1.18.319) startup
total_len(750544) >= size_raw(750544), _STREAM_END     ← decompresses (compress=1 supported)
The partition 'app' erase success
OTA Write: [====================================] 100%  ← full image written
```

The bootloader then starts the OpenBeken it has just installed; the emulator
performs the whole apply itself
([how](../tools/beken/emulator/README.md#the-emulator-performs-the-whole-ota-apply)).
Sent the cloud's way instead, in its FOTA container after a pushed `upgrade`,
the same image is staged byte for byte and applied on the restart
([`cloud_push.py`](../tools/beken/emulator/cloud_push.py)). None of it has been
done on a real device, where the step cannot be undone by running it again.

### OpenBeken on the 4 MB table

OpenBeken's BK7238 build hard-wires `CFG_FLASH_SELECTION_TYPE =
FLASH_SELECTION_TYPE_2M` (`sys_config_bk7238.h:374`), so an upstream OpenBeken
uses the SDK's 2 MB table even on the S61's 4 MB chip. Its OTA slot would be at
`0x132000` and its config (`NET_PARAM`) at `0x1e1000`: both inside the stock app
partition, and the slot one that the stock `rt_ota` bootloader never looks at.

Built with `FLASH_SELECTION_TYPE_4M` instead, it has
[Sonoff's own layout](#sonoffs-layout-is-the-sdks-4-mb-table): its OTA download
lands in the slot the stock bootloader applies from, so both an OpenBeken
update and a return to stock go through the ordinary `ota_http`; its config
goes in a sector stock leaves empty; and it reads the device's real radio
calibration. Two places did not follow the partition table and
now do: `hal_ota.h` hard-coded the REST `/api/ota` address (`0x132000`), and
`rest_interface.c` bounded `GET /api/flash` at 2 MB. In the emulator the 4 MB
build writes its config at `0x3e1000` and `0x1e1000` stays blank.

The build is a `FLASH_SIZE=4M` switch, in two patches kept in
[`tools/beken/openbeken/`](../tools/beken/openbeken/) and offered upstream as
OpenBK7231T_App#2290 and beken_freertos_sdk#13
([upstream-contributions.md](upstream-contributions.md#openbeken-on-a-4-mb-bk7238);
the recipe is in
[the driver's notes](../tools/beken/openbeken/README.md#building-bk7238-4-mb-table)).

Packing a whole 4 MB image needs a 4 MB bootloader, which the SDK's packager
makes from the one it ships (`make_bootloader_4m.py`): the S61's factory
bootloader, so a 4 MB QIO from the build is a converted device's whole flash,
and the emulator boots it as one. In that run OpenBeken's `ota_http`
downloaded to `0x286000` and its own bootloader applied it
(`upgrade(4M.1220->s61v6)`, `_STREAM_END`, the slot erased). The emulator then
stayed at the bootloader's banner, but the flash it had written verifies (the app
matches the RBL header's FNV-1a) and starts s61v6 when loaded afresh: that was the
emulator's state after a watchdog restart OpenBeken asked for, not the image.

### OpenBeken's serial log, and its web server's reads

Two things in OpenBeken's BK7238 build (the pinned commit, `app_commit` in
`artifacts/firmware.json`), found in the emulator and read in its source:

- **Its serial log loses a byte when busy.** `logging.c` undefines
  `PLATFORM_BEKEN` on a `PLATFORM_BEKEN_NEW` build, so the BK7238 sends its log
  in 127-byte chunks (`getData` into `seriallogbuf[128]`) through the SDK's
  `bk_printf`, which prints at most 126 (`vsnprintf(string, sizeof(string) - 1,
  ...)` into 128 bytes). The 127th byte of every full chunk is lost - a chunk
  is full when 127 or more are waiting, so a busy log loses one about every
  127 bytes, a quiet one none - and with `logtype direct` a line over 126
  characters loses its end. The words a command was given are whole (read
  back, a setting is as sent), and so is the log over HTTP (`/lograw`), from
  the same ring: read that where a character matters. Seen as
  `ota_http .../irmware.rbl` in an emulated device's serial log, its fetch
  asking for `firmware.rbl` all the same.
- **Its web server takes the first read for the whole request.**
  `new_tcp_server.c` stops reading once a `recv` returns less than its buffer
  holds, so a body that arrives a moment after the headers is never read: the
  command runs empty and answers "No command entered", having done nothing.
  The tool sends such a command again (`openbeken.command`, `EMPTY_TRIES`).
- **`ota_http` answers "OK" to a fetch it does not start.** A fetch already
  under way - its server gone quiet, waiting out its read - is turned down
  only in the log ("http in progress, not starting another"), so a run
  carried on after one cut off asked for its firmware and got nothing until it
  restarted the device. And the check misses a fetch still connecting: it
  tests `request->state == 1`, set only once data comes, so a second
  `ota_http` then resets the request the first fetch's thread is using (its
  connect fails, "getaddrinfo error") and starts a second thread on it. The
  tool restarts OpenBeken before a fetch where the last run was cut off in
  one, and does so too where `ota_http` answers an error
  (`onward.serve_to_device`).

Each has a fix offered upstream: [#2309](https://github.com/openshwprojects/OpenBK7231T_App/pull/2309),
[#2310](https://github.com/openshwprojects/OpenBK7231T_App/pull/2310) and
[#2311](https://github.com/openshwprojects/OpenBK7231T_App/pull/2311), each tested in the emulator on OpenBeken's
`main` with and without it.

### ESPHome releasing the address it keeps

In the fault suite's long router outages an emulated S61 on ESPHome
(2026.10.0, LibreTiny 1.13.0) sometimes released the address it went on
using: `DHCPACK`, a second `DHCPACK` of the same address, then `DHCPRELEASE`,
in the router's log. Traced with a build of the same config with LibreTiny's
Wi-Fi debugging on and the SDK's own output, the SDK's calls logged as they
came (`DEVICE_TRACE`), and the serial log timed beside the router's, router
outages of two minutes on eight emulated devices:

1. **An empty scan never ends** (LibreTiny, Beken only). With the router off,
   ESPHome's scans find nothing; `scanHandler` takes room for no entries as an
   allocation failure ("scan->ap alloc failed") and never sends
   `ARDUINO_EVENT_WIFI_SCAN_DONE`, so ESPHome waits out its scan timeout (up
   to 31 s, "Scan timeout") each time: 22 to 28 times in six outages.
2. **The SDK joins by itself meanwhile.** ESPHome's `WiFi.disconnect()` after
   each "Probe Request Unsuccessful" does nothing to a station not yet
   associated ("ignore connection loss event"), so the SDK's supplicant goes
   on scanning and joins the router once it is back, DHCP and all, while
   ESPHome is still waiting.
3. **A stale error drops that connection** (ESPHome). With its own network up,
   ESPHome's `RESTARTING_ADAPTER` phase skips `restart_adapter()` and only
   disconnects, leaving `error_from_callback_` set from the last failure.
   After the cooldown `check_connecting_finished()` reports the SDK's
   connection ("Connected", then "Disabling AP"), and `is_connected_()`, reading
   the stale flag, calls it lost at once ("Connection lost; reconnecting").
4. **Reconnecting releases the lease.** Each attempt begins by tearing down a
   live session (`WiFi.disconnect()`, then the SDK's `sta_ip_down`,
   `dhcp_stop`, `dhcp_release_and_stop`). The release waits for the router's
   ARP answer, and goes out once the device has joined again and been given
   the same address - so the router frees an address still in use.

Taking ESPHome's own network down (`bk_wlan_stop(BK_SOFT_AP)`) comes just
before the drop in step 3, but plays no part. With LibreTiny's empty scans
reported done, steps 1 and 2's long waits went (no scan timeouts in 48
outages) but step 3 still dropped a connection five times; with ESPHome's
flag cleared as `restart_adapter()` clears it, none of the 48 did, and no
address was released. Both fixes are offered upstream
([upstream-contributions.md](upstream-contributions.md#a-beken-device-on-esphome-giving-up-an-address-it-still-uses)). Separately, LibreTiny
drops the SDK's `RW_EVT_STA_DEAUTH` and `RW_EVT_STA_ACTIVE_DISCONNECTED`
(`WiFiEvents.cpp`), so a joined station's deauthentication reaches ESPHome as
nothing.

### Carrying Wi-Fi across the conversion: the OpenBeken config

The new firmware should rejoin your network with nothing typed, as on the
S60. The credentials are in the stock app store, which the update does not
touch, and there are two ways to give them to OpenBeken.

**What the tool ships: OpenBeken reads them itself.** The driver
([`tools/beken/openbeken/drv_autoflash.c`](../tools/beken/openbeken/drv_autoflash.c),
with the S61's table for it,
[`devices/sonoff-s61/drv_autoflash_sonoff_s61.c`](../devices/sonoff-s61/drv_autoflash_sonoff_s61.c);
[what it does](../tools/beken/openbeken/README.md#what-the-driver-does-on-first-boot))
reads the stock EF40 store with `beken_hal_flash_read` on first boot, finds the
newest `wifi-ssid` and `wifi-pass` whose CRC is good, and calls OpenBeken's own
`CFG_SetWiFiSSID()` and `CFG_SetWiFiPass()`. The setters make it immune to
changes of `mainConfig_t`'s layout and checksum between OpenBeken releases, and
as it only reads the stock store, going back stays open.

**The fallback, kept as an emulator aid: a config written beforehand.**
[`wifi_migrate.py`](../tools/beken/wifi_migrate.py) reads the credentials from the
stock store, builds a `mainConfig_t` holding them and writes it into an OpenBeken
image. It depends on the format, which is why it does not ship: it broke on
exactly the signed CRC and the `sizeof` below. The format, checked byte for byte
against a real OpenBK7238 1.18.319 config:

- **Layout** (`src/new_pins.h`): `ident0/1/2 = 'C','F','G'` at offsets 0 to 2, a
  `crc` byte at offset 3, `version` (an `int`, 5) at offset 4, then flags and
  counters, `wifi_ssid[64]` at `0x14` and `wifi_pass[64]` at `0x54`.
- **Size:** `sizeof(mainConfig_t)` on the BK7238 build is 3584. OpenBeken writes
  exactly 3584 bytes in one `HAL_Configuration_SaveConfigMemory` burst.
- **Checksum:** `Tiny_CRC8` (`src/tiny_crc8.c`) over bytes `[4 : 3584]`, kept in
  the `crc` byte. The shipped binary compiles `char crc` as signed, so `crc >>= 1`
  is an arithmetic shift that extends the sign. A plain unsigned CRC-8/MAXIM gives
  the wrong value (`0xac` where the real one is `0x80`), and OpenBeken rejects
  every config made with it. In Python:
  `crc = ((crc>>1)|0x80) if (crc&0x80) else (crc>>1)`, then `if bit: crc ^= 0x8C`.
- **Place:** the start of `BK_PARTITION_NET_PARAM`, physical `0x1e1000` on the
  upstream BK7238 2 MB build (BK7231N's SDK uses `0x1D1000`).

In the emulator, a config built so and written at `0x1e1000` of an OpenBK7238
image padded to 2 MB boots to `Correct config has been loaded`, `Using SSID
[...]`, the four-way handshake and a DHCP lease.

When trying this in the emulator:

- Pad the QIO image to the real flash size, or the `NET_PARAM` write is out of
  range.
- Set `emu.flash_writethrough = True`, so that saving and reading the config
  work.
- The UART, the `CFG:` verdict included, prints to stdout, not to the `log` or
  `uart_sink` callbacks.
- Config writes are ANDed into the flash with no clean erase, so a later save
  corrupts the blob. For a clean reference, hook the flash-program path and keep
  the first burst.

### Reversion to stock, and the backup

The stock bootloader applies any valid RBL at `0x286000`, a downgrade
included. With the stock app's RBL staged on a converted device it logged `OTA
firmware(app) upgrade(s61v2->1.0.2)`, erased and wrote `app`, and the stock
firmware started (`EasyFlash V4.1.99 is initialize success`) with its store
untouched: the same 33 keys, the same Wi-Fi credentials, the same `elecCalibCoef`.

**The stock app's RBL is rebuilt from the dump.** The app partition holds the
decompressed image (the OTA body is gone), so the app with its stripes removed,
whose FNV-1a matches the stored raw hash `0x0d89f7b1`, is packed again as the
SDK's own packer would, under the same public key as every other RBL's, and
the bootloader accepts it ([`stock_app.py`](../tools/beken/stock_app.py), and
the packer's command in
[the driver's notes](../tools/beken/openbeken/README.md#reverting-to-stock)).

**OpenBeken fetches it with `AutoflashRevertToStock <url>`** (once
`S61RevertToStock`). It checks that the OTA slot is `0x286000` in both the
SDK's table and `START_ADR_OF_BK_PARTITION_OTA`, the constant the REST OTA path
uses, and refuses on a 2 MB-table build, where the download would land at
`0x132000`; then OpenBeken's normal OTA download fetches it. Against an HTTP
server on the host: `init OTA, startaddr 0x286000`, a restart,
`upgrade(s61v6->1.0.2)`, and the stock firmware up with its store untouched.

**The backup** is the device's own region, `0x3e0000` to `0x400000` (the
factory record, the app store and the system store), which
[`backup.py`](../tools/beken/backup.py) copies out over
`GET /api/flash/<addr>-<len>`, whose `DEFAULT_FLASH_LEN` this project's build
raises to 4 MB ([how it is filed](../tools/beken/openbeken/README.md#backing-up-the-device)).
In the emulator it matches the dump byte for byte. With the stock app, the same
for every device of a model, and this region, a device can always be put back
to stock.

### The ESPHome route (LibreTiny)

The second open firmware is ESPHome on LibreTiny, whose 1.13.0 (the version
ESPHome 2026.10.0 pins) has the BK7238. The config,
[`devices/sonoff-s61/esphome.yaml`](../devices/sonoff-s61/esphome.yaml), is the
S60's with the S61's wiring and storage, and needs no change to LibreTiny or
ESPHome; the build, the reversion and the validation are in
[devices/sonoff-s61/esphome.md](../devices/sonoff-s61/esphome.md). The route
rests on these:

- **The image is the same `rt_ota` format.** LibreTiny's build makes
  `image_bk7238_app.ota.rbl` (name `app`, gzip, AES with RT-Thread's public key),
  which the stock bootloader applies from the download slot like any other.
  Staged at `0x286000` by a pushed `upgrade`, the bootloader logs
  `upgrade(1.0.2->26.10.29-bk7238)` and `_STREAM_END` with the raw size the build
  reported, erases and writes `app`, and jumps.
- **LibreTiny's partition table is the board's `flash` section,** which it wraps
  over the SDK's (`BkDriverFlash.c`), and PlatformIO's `board_flash.*` overrides
  reach both the firmware and ltchiptool (the build writes the merged board to
  `board.json`). So the config carries the S61's layout in `platformio_options`:
  app `0x11000+0x275000` with `bkrbl_size_app 0x250000`; download `0x286000`, so
  that ESPHome's own OTA lands where the stock bootloader looks; calibration
  `0x3e0000`; FlashDB preferences at `0x3ee000`, the empty 32 KB that OpenBeken's
  4 MB table calls `USR_CONFIG`; LittleFS at the empty `0x3e1000`. Nothing of the
  stock stores is touched.
- **Wi-Fi and calibration come from the stock store at boot,** read with
  `lt_flash_read` in a boot lambda (the OpenBeken driver's scan, in C++), so
  nothing is written into the packaged image, which could not be patched. The calibration is applied with
  [the stock formula](../devices/sonoff-s61/research.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping).
  ESPHome's `hlw8012` reckons the pulse rate with the BL0937 datasheet's 1.218 V
  where the stock driver uses 1.1 V, so the readings are scaled by that ratio
  (squared for power) before the trim.
- **Reversion is ESPHome's own OTA.** LibreTiny writes a UF2's `download` part to
  the download slot, so a UF2 carrying the stock app's RBL (ltchiptool builds one;
  [`uf2.py`](../tools/beken/uf2.py) here) brings the stock bootloader's apply on
  the next restart. One carrying OpenBeken's 4 MB-table `.rbl` moves the device to
  OpenBeken, whose `ota_http` of the ESPHome `.rbl` moves it back. Both ran in
  the emulator: each first boot takes Wi-Fi, calibration and wiring from the
  stock store, and the two keep their settings in different sectors (`0x3ee000`
  ESPHome, `0x3e1000` OpenBeken), so neither disturbs the other.
- **The emulator needed its 26 MHz timers.** LibreTiny keeps its FreeRTOS tick on
  hardware Timer 0 (the 26 MHz group, for a fine `micros()`;
  `cores/beken-72xx/base/fixups/clock_rtos.c`), where OpenBeken and the stock
  firmware use Timer 3. With only Timer 3's status raised, LibreTiny's timer
  ISR acknowledged a channel with no handler while `xTaskIncrementTick` never
  ran: every log line said `0.000`, the first `delay()` never returned, and the
  Wi-Fi start stalled at `Waiting for init_thread`. The fork now models the
  26 MHz group too, each enabled channel firing at its programmed period, at the
  slow tick's resolution. The period matters: OpenBeken's BK7238 build feeds its
  interrupt watchdog from a 10 s channel of that group (two key writes with a
  busy delay between), and raising every enabled channel on every tick ran that
  feed back to back and starved OpenBeken just after `Main_Init_Before_Delay`.

## What a backup logs

The backup is taken at B4, through OpenBeken, once the first conversion has put it
on ([how it is read and filed](#reversion-to-stock-and-the-backup)). Stock
firmware says nothing of its flash, so what a run learns of a device goes in its
log (`tools/autoflash/routes/beken/`):

- **At B1**: its official update file's firmware
  name and version, the version the device reports, and the file's whole partition
  table, with the flash size that table is for.
- **At B4**: whether the device's own factory record was read at `0x3e0000`. On a
  smaller flash it would not be there, so finding it is the first sign that the
  flash matches the table.

## The stock firmware in the emulator

To bring Sonoff's firmware online the emulator supplies five things, each
explained below or above: the flash chip's Unique ID ([Boot](#boot)), a pass of
[the integrity gate](#the-flash-uid-integrity-gate), the Wi-Fi credentials
([seeding](#joining-wi-fi-by-seeding-the-credentials)),
[the pairing-success call](#the-cloud-dial) and
[the trust anchor](#the-handshake-to-online). `cloud_online.StockHooks` puts them
in, with a raise of Timer 5 ([the button](#the-button)). The rest of this part
is how the firmware starts and pairs, and how far each pairing route goes with
no radio.

### Boot

Right after `ais_evt_task` starts, the Beken SDK reads the flash chip's Unique
ID (command `0x4B`, 4 dummy bytes, 8 data bytes) over the SPI master at
`0x802700`, apart from the memory-mapped flash the emulator serves. Upstream's
emulator does not model that controller: its STATUS register never says
"done", the read spins, and the task watchdog resets the chip, over and over.

The fork models it in `src/spi_flash.py` (`SpiFlashMaster`), which serves the
SPI STATUS and DATA registers and answers `0x4B` with a made-up id, wired into
the two MMIO hooks of `src/emulator.py` in about three lines. It acts only
while a transfer is configured (`CONFIG != 0`), leaving a firmware that never
drives the SPI master unaffected. With it the firmware boots with no restarts,
and upstream's regression tests pass 6 of 6 (the SPI-LED controllers,
OpenBK7238's Wi-Fi, DHCP and HTTP, the S61 dump).

### What the firmware does at boot

The eWeLink app boots to `ate_mode_state[0]` (normal, not ATE), runs its
`ais` event loop, drives its status LED through the PWM block (`0x802B00`,
about 290,000 writes a run), brings up its BLE stack (`BLE_STACK_OK`) and
initialises EasyFlash V4.1.99, then settles: every AIS task created and blocked on its event queue,
timers and interrupts (SYS, IRQ, FIQ, SVC) firing, about 58,000 passes of the idle
loop, and no Wi-Fi frame sent. The central event loop is a message pump at
`0x23d6c`, blocked on a queue receive (`0x134f7c`) from the queue whose handle is
`*(0x403c0c)+4 = 0x403c10` (created about 0.45 s into boot by `0x17700` and stored
at `0x16604`). It dispatches event messages 1 to 9 and otherwise waits.

The public dump's device is unprovisioned
([the provisioned state](#the-provisioned-state)), so it waits for a pairing
trigger: the button, or a command from the app over BLE or the cloud. Two
things in its output look like activity and are not. The RX filter `0x7fffffde` and the
UART line "monitor mode … as in EZ pairing" are the MAC's default filter, printed
during `rwnxl_init`, not an AirKiss sniff: the monitor callback `0x403b20` is NULL
at boot ([AirKiss](#airkiss-the-decoder-is-a-stub)). And BLE does not advertise at
boot: no advertising payload is staged.

### The flash-UID integrity gate

Left alone, the emulated app layer never starts: `ais_appfw_task`, the
button's registration and the provisioned check all wait on the `app` thread,
which hangs in a check of the device's identity.

Boot creates every RTOS task through `ais_evt_task`. The `app` task then runs the
AIS app-start (`0x24430`, then `0x29b54`), which reaches a poll loop at `0x29c38`.
There it builds `SHA256(hex(flash_uid) + identity_fields)` (the hash is `0x25454`,
the string comes from `0x19b70`) and compares it with `memcmp` (`0x42788`) against
a 32-byte value stored at `ctx+0x84`. On a mismatch it branches to `0x29c44`, an
endless loop of 1-second delays: a deliberate hang. A stack unwind finds the `app`
task parked there (`0x24430`, `0x29b54`, `0x29c4a`, the delay `0x1349b4`).

The 16-byte identity is the flash chip's Unique ID: `0x2856c` calls `0x21408`,
which opens the `flash` device and reads `bk_flash_get_uid` (SPI command `0x4B`).
The emulator answers with its made-up id (ASCII `S61EMU\0\1`, read as
`533631454d550001ffff…`), so the hash it computes (`e3f30c7e…`) never matches the
value stored from the real id (`f009c2a1…`). A real device passes, its own id
matching its own factory record. The real id is a 128-bit hardware value the
dump does not hold (the config's `secureCertSha256` and
`chip-factory.uniqueId*` do not match what the check wants), so the emulator
passes only if given the real device's id, or with the result forced.

[`integrity_gate.py`](../tools/beken/emulator/integrity_gate.py) forces it: the
`memcmp` result at `0x29c3c` reads "equal", as on real hardware. Within about
3.5 s, with nothing else called, `key_add` registers the button on GPIO pins 0
and 2, the provisioned check (`0x48cc4`) runs, and `ais_appfw_task` is
created.

### The button

On a real device you hold the button for 5 s to enter pairing. In the emulator
the firmware's own code times the hold and fires the long-press action, which
turns out to drive the relay; the path from a long press to pairing has not
been found ([Entering pairing](#entering-pairing)).

**The hardware.** The button is a capture interrupt on GPIO 0 to 5, in the PWM and
timer block at `0x802B00` (three groups of `0x40`, two channels each). It is not
in the GPIO `INTEN` mask at `0x802884`, so driving pins 10 and 11 there does
nothing. The chain:

```
pin edge
  -> PWM-capture interrupt (status in bits 30/31 of the group reg)
  -> ICU IRQ line 9
  -> ISR 0x1bfd8        reads group status, finds the channel,
                        dispatches callback(r0=pin, r1=level) from the
                        per-pin table at 0x4038f4
  -> key handler 0x4203c
  -> debounce           reads the pin via 0x3eb30, counter threshold > 2
  -> on a held press: 0x42078 arms a 5-unit timer (0x1348fc, r1=5 = "hold 5 s")
                      and posts a key event
```

**Registration.** The block's init, `0x1bc18`, registers the ISR
(`intc_register(9, 0x11, 0x1bfd8)`); it is generic and runs at every boot.
`key_add` (`0x41f64`) wires the button up, and runs only past the integrity gate:

- it writes the pin to `0x404c25`, fills the key-id table `0x404bb4` with the pin
  number and sets the driver flag `0x404c23 = 1`;
- it registers the callback through `0x3f090` and `0x1c14c`, into the per-pin
  table `0x4038f4`;
- it enables the capture interrupt through `0x3f0d0`, `0x12c214` and `0x1c2f0`,
  which writes the pin's field in the `0x802B00` group register;
- it creates the key-driver timer thread through `0x3ec6c` (thread-create
  `0x1349f0`, entry `0x3eba0`, startup `0x217ec`, period `0x64`).

**The key-driver timer thread** (entry `0x3eba0`) times the hold. It blocks on the
RTOS wait `0x134618` (timeout −1, for ever), which goes to `0x151a8` (`svc #0`,
waiting on its TCB's signal, `[tcb+0x54]`). On each wake it walks its list of
software timers once, counting each down and calling those that expire (`blx` at
`0x3ec18`), and runs the periodic key scan `0x421e0`, which reads the button's pin
(`[0x404c25]`) and counts the hold. The armed 5-unit timer sits in that list.

The thread is woken by `rt_thread_resume` (`0x15274`, Thumb veneer `0x134818`),
called by the giver `0x3ec30`, which the thread's startup `0x217ec` registers on
the `bk_timer` hardware-timer device (`0x19ed0` device-control,
`bk_timer_initialize`). `bk_timer` is Timer 5: interrupt-status bit 9 of
`timer3_5_ctl`, with its period `0x7d00` at `0x802a44`. The emulator's `_pacing`
raises only Timer 3 (`timer3_5_ctl` bit 7, ICU bit 8), the RTOS tick of the
OpenBeken and Tuya firmwares, so the giver never runs and the thread is never
signalled (`wait`=1, `nodeproc`=0 for ever). With Timer 5's status bit raised,
the thread wakes, the scan runs hundreds of times and the hold is timed
(`archive/s61-research/long_press.py`). The tools raise bit 9 themselves:
raising every enabled channel in `_pacing` (as the BK7252 path does) breaks 3
other BK7238 self-tests that drive Timers 4 and 5 at their own rates.

**A press.** The emulator models `0x802B00` as PWM output only: it keeps the
capture and PWM interrupt-status bits 30 and 31 in `state.pwmn_stat` (overlaid
on reads, write-1-to-clear, driving ICU bit 9) and sets them from PWM activity,
never from a pin's edge, so a press must set the channel's bit itself.
`archive/s61-research/button_sim.py` does that through the
[force-call harness](#tools-and-methods):

1. run the block init `0x1bc18`;
2. run `key_add` (`0x41f64`) with the descriptor
   `bytes([0,0,PIN,PIN,PIN,PIN,0,0,0,0,0,0])` at the scratch address `0x403d80`;
3. press: drive the GPIO low and set the channel's capture bit
   (`state.pwmn_stat |= 1 << (2*GROUP + CH)`, with `GROUP, CH = PIN//2, PIN&1`).

To hold the level low the pin must stay readable as an input: `0x3eb30`
decodes the pin's mode (`0x1a198`) from `gpio_cfg` bits 2 and 3, and the
capture config reads the non-input path, so the tool forces
`gpio_cfg[PIN] = (… | 0x0c) & ~0x1`. One capture edge debounces only to 1, so
the press repeats about 6 edges, as a held button does, to pass the `> 2`
threshold. The firmware's own code does the rest:

```
ISR(0x1bfd8) -> handler(0x4203c, r0=pin) -> press_path -> 5s-longpress-timer armed @0x42078 (timer_start r1=5)
```

That script predates the integrity-gate finding and defaults to pin 3; past
the gate the firmware registers the button on pins 0 and 2 itself, and
`long_press.py` holds those.

**The long-press action** (`0x42250`) posts through `0x3eb6c` and `0x12c180` to
`bk_gpio_output`: it drives the relay or an LED. It is not the pairing trigger.

### Entering pairing

Once the gate is passed, the pairing state machine is alive. `ais_appfw_task`
(entry `0x319a8`) loops at `0x319c4`, waiting about 30 times a second on
`rt_event_recv` (`0x134da8`, mask 7) for the event object `[0x4040e0]`. Its state
is the struct `0x404fb4` (state byte `0x404fb8`; functions `0x48a88` to `0x496b8`,
the loop near `0x49b40`), and it is driven by
`PAIR_EVT_START_{COEXIST,EWELINK,BCM}` (name table at `0x49c18`).

Pairing runs from an appfw state handler to the config builder `0x74870`, the
method runner `0x748cc` and the method dispatcher `0x21eb4(r0=config)`, which
reads `config[0]`:

| Method | Handler | What it starts |
|---|---|---|
| 0, coexist | `0x21e50` (`airkiss_start`) | the soft-AP (`0x21a80`) and the AirKiss sniffer, through the RW task |
| 1, station | `0x21d18` | station mode (the Wi-Fi mode is set through `0x20e00`) |
| 2 | `0x21d90` | not traced |

The runner `0x74870` hard-codes `config[0]=1`; the coexist method comes from
another runner, or from the dispatcher's RAM-resident caller `0x3f60c`.

**What does not start it.** Posting a bare event to the appfw (`rt_event_send`
`0x134674` to `[0x4040e0]`) wakes it and runs `0x748cc` again, but with no
method or parameters set the runner does nothing. The real triggers, which set
the method first, are two:

- **The `pairingMode` command**, registered by name through `0x27568`
  (`register("pairingMode", 0x492ae)`; its log string `"pairingMode by %d"` is at
  `0x49548`). The handler `0x492ae` fetches its value with `get_value` (`0x12200`)
  and then reads a session object given as its first argument: the JSON pointer at
  `session+0x8c`, its length at `+0x90`, a flag at `+0x88`. The command is a JSON
  array (cJSON `0x264d6`, `0x265da`, `0x26712`), with each item's fields decoded
  from base64 or hex (`0x12f8b8`) and run through crypto and list operations
  (`0x5b078`, `0x4842e`, `0x48372`, `0x48400`). It needs a genuine session from
  the cloud WebSocket or BLE, so it cannot simply be called, and rebuilding
  eWeLink's encoded command would be a project of its own that nothing needs:
  [the cloud dial](#the-cloud-dial) takes one call.
- **The button's app-layer path.** The long press posts a key event that the app's
  key task turns into pairing entry. Where that sets the method and posts
  `PAIR_EVT_START` has not been traced.

**Calling the dispatcher directly** does drive the radio. Called in the appfw
task's own context (a hook on the loop at `0x319c4` with the force-call harness,
so that the RTOS and RW-task messaging run in a real task),
`archive/s61-research/pair_drive.py` shows:

- **Method 0** builds the soft-AP, the MAC leaves monitor mode (`RX filter … back
  to normal reception`) and it sends beacons. The fork's virtual peer reports
  `device acts as an access point (it sends beacons)`, takes the station role and
  finds the device's BSS. The network's name is the firmware's default, `JIEWU`
  (in the image at logical `0x137ed8`), and it is protected by a cipher, so the
  peer, which joins only open networks, does not finish associating.
- **Method 1** puts the MAC in station mode, probing and associating; the peer
  takes the access-point role and registers the device as a station.

Pairing starts soft-AP, BLE and AirKiss commissioning; finishing a Wi-Fi join
from there still takes the app and the cloud, or a Matter commissioner.

### Pairing and provisioning routes

Each route below gives a device its Wi-Fi credentials once pairing has been
entered. None can be completed in an emulator with no radio, which is why
the emulated device joins by
[seeding the credentials](#joining-wi-fi-by-seeding-the-credentials).

#### The soft-AP: beacons only

The firmware has `Soft_AP_start` and `bk_wlan_ap_init`, and the soft-AP is driven
by command: the device enters COEXIST unconditionally at start
(`bk_wlan_set_coexist_at_init`) and changes mode on a `pairingMode` command. Its
status printer is at `0x130c00` (`softap:ssid=%s,channel=%d`), and `0x21a80`
refers to the `Soft_AP_start` symbol at `0x137c80`. Once an access point is up,
the emulator's peer takes the station role (the OpenBK7238 access-point self-test
passes).

It is not an access point you join to hand over credentials, as in eWeLink's
older AP pairing:

- **It carries no provisioning server.** The only TCP or WebSocket server in the
  image is the LAN control endpoint on port 8081 (`http://%s:8081`,
  `ws://%s:8081`, advertised over mDNS as `_ewelink._tcp`), the one the S60 opens
  once online ([cloud-stub.md](cloud-stub.md)). It is gated on the station and
  online path, never on the soft-AP's, and the image has no endpoint to POST
  credentials to. Credentials reach this device over BLE (`EwelinkBleServer`, for
  eWeLink and Matter), feeding one sink: `recv ssid/password/timeout`, store
  `wifi-ssid` and `wifi-pass`, `switch wifi`.
- **In the emulator it only beacons.** The coexist handler `0x21e50` runs in two
  parts. The first brings the soft-AP up (`0x21a80`, building the config through
  `0x219e8` and `0x21920`, ending at `0x24248`); that completes, and the MAC
  beacons. The second, from `0x21e80`, calls the IP and DHCP-server setup
  (`0x21ea4`, then `0xfdf0c`; the netif IP config `0xfe4ac`, and a path gated on
  the role whose failure logs `warning: dhcpd no open`). Between them are two
  RivieraWaves kernel sends, `0xee350` and `0xee35c` (messages `0x64` and `0x65`,
  through `0xee048` to `0xedfb4`), and a trace of the calls ends there:

  ```
  disp → coexist_21e50 → softap_21a80 (→219e8→21920→24248) → rw350 → rw35c → ⊥
  ```

  `0xee35c` never returns to the handler. On an unprovisioned, emulated device
  `0xedfb4` returns -1, its destination KE task at `*0x417564` not being
  registered, and `0xee35c` is a blocking request that retries after a delay
  (`0x13499c`), which the synchronous force-call harness cannot carry through.
  Watching `0xfe4ac`, `0xfdf70`, `0xfdfaa`, the `dhcpd no open` site and the two
  builders of the port-8081 server while method 0 runs confirms it: none fires.
  That a real device gives the soft-AP an address and a DHCP server is likely,
  the code being there, but has not been seen.

So the soft-AP only shows the device is there while it pairs. To drive a
device over the LAN on port 8081 rather than through the cloud, iHost is the
reference: its "eWeLink Smart Home" add-on drives eWeLink devices locally, with
a working client for the mDNS discovery, the request format and the AES scheme
keyed on the device's secret. That channel only controls: the stock LAN
endpoint has no firmware route (no `/zeroconf/ota_*`), so a conversion still
goes through the cloud's `upgrade`.

#### AirKiss: the decoder is a stub

AirKiss would suit a relay, as the phone never associates with the device: it
broadcasts, and the device listens. But this build has nothing behind the
listening.

`0x403b20` holds the callback for frames received in monitor mode. The RX
consumers `0xaaf40` and `0xb37a8` do `bl 0x2220c` (which reads `*0x403b20`), then
`blx r3`, once a frame. Only two pieces of code write it: the register helper
`0x221fc` (`set_monitor_cb(cb){ if(cb) *0x403b20=cb; }`) and the clear `0x221bc`
(`*0x403b20=0`). The helper has exactly two callers, both in the AirKiss start
handler (`0x12c666` and `0x12c6a4`), and the only non-zero value either stores is
the literal `0x12c483`, which is `bx lr` (the bytes at `0x12c482` are the same in
RAM as in flash). With `*0x403b20 = 0x12c483` set and more than 11,000 frames
replayed in monitor mode, the callback is called for every frame (`0xaaf40` to
`0x12c482`), with no fault and nothing decoded.

eWeLink kept the scaffolding (the start `0x223b0` and stop `0x221bc` with
their guards, the log `monitor (ie.airkiss) is not finish yet` at `0x137c90`,
the register and clear helpers) and compiled the decoder out, in keeping with
its move to soft-AP and BLE. The coexist path changes nothing: its handler
`0x12c640` registers the same stub, and in the emulator its send (`0xee35c`,
message `0x65`) fails anyway.

Delivering frames to the callback does work, 100% of them
([Monitor-mode RX injection](#monitor-mode-rx-injection)), and the capture and
replay tools (`archive/s61-research/airkiss_capture.sh`, `airkiss_replay.py`;
[how to use them](../tools/beken/README.md#airkiss-end-to-end-procedure)) stand
for any firmware that does decode. Never commit a real capture: AirKiss carries
the Wi-Fi credentials in the lengths of its frames.

#### BLE

BLE is how credentials really arrive, through the eWeLink BLE provisioning
protocol (the device also offers Matter BLE commissioning), and the protocol is
understood from end to end.

A BLE write goes to a dispatcher (`0x72940`), which looks the message type up in a
table at `0x145430` and posts `{index, payload, len}` to a queue (`0x134b6c`, the
queue `*0x402dd0`). A consumer task then calls `table[index].handler(payload,
len)`. There are six commands, named here by their `[BLE CMD: …]` log strings:

| Type | Handler | Command | Purpose |
|------|---------|---------|---------|
| `0x02` | `0x725e4` | AUTH_TOKEN | the app proves it knows the device's apikey |
| `0x04` | `0x727bc` | REQ_SCAN / REQ_AUTH | scan for access points; sets the step byte `0x40cf14=2` |
| `0x07` | `0x722a4` | REQ_CONNECT | connect to a scanned access point (by BSSID) |
| `0x09` | `0x72258` | DEVICE_INFO_REQUEST | the device returns its info; step `=5` |
| `0x0b` | `0x718a0` | SERVER_INFO_REQUEST | the cloud's dispatch server |
| `0x0d` | `0x712e0` | HIDDEN_AP_CONNECT | the Wi-Fi credentials (`recv_ssid`) |

- **The encryption.** Each message that carries data is decoded from base64, then
  decrypted with AES-128-CBC and PKCS#7 padding. The AES key and AUTH_TOKEN's
  "cache" are one 16-byte secret at RAM `0x40ce90`, derived from `factory_apikey`
  at boot (the apikey UUID is in the same page; both are a real device's
  identifiers and are not given here). So the secret can be read out of the
  emulator, and nothing has to be forged. AUTH_TOKEN (`0x725e4`) compares the
  decrypted token with that cache (`memcmp` `0x13456c`, called at `0x726d0`; its
  error is `auth_token cmp err, recv[%s] cache[%s]`).
- **The Wi-Fi message, type `0x0d`.** `recv_ssid(r0=packed_buf, r1=tag)` takes
  `[ssid_len:1][ssid][pad:1][timeout:1][encflag:1][password…]`. With `encflag=1`
  the password is copied into the connect config; the plain `encflag=0` path
  copies only the name, a quirk of the firmware. `recv_ssid` packs a config
  (`method=1` for station, the name at `+1`, the password at `+0x22`, the BSSID at
  `+0xcf`) and sends it through the method dispatcher `0x21eb4`. Called in the
  appfw's context with the password decoder bypassed, it makes the device
  associate (WPA2) and take the DHCP lease `192.168.1.100`.
- **What driving it reaches.** `archive/s61-research/ble_provision.py` sends
  the messages through the dispatcher: the handlers run and Wi-Fi connects, but
  the device is not paired. The handlers share a session state machine (session
  flag `0x40cfe3`, step byte `0x40cf14`) and a field accumulator, and pairing's
  completion is posted by the BLE connection's own lifecycle, which an emulator
  with no radio never produces: after the whole sequence the device is on
  Wi-Fi, still sends the `0xFFF6` eWeLink pairing advertisement, and dials
  nothing. That completion is the one call [the cloud dial](#the-cloud-dial)
  makes.

What the emulator's BLE can and cannot do:

- **The controller model answers the host.** The boot's gapm commands
  (`GAPM_RESET` op 1, `GAPM_SET_DEV_CONFIG` op 3) complete with `gapm_cmp_evt`,
  so advertising could complete if asked for.
- **Nothing advertises at boot** (no `[BLE_ADV]` payload is staged). Advertising
  starts from the pairing trigger: Matter's CHIP `BLEManagerImpl` and its
  commissioning window (`COMMISSION_OPEN_WINDOW : ECM/BCM`, near `0x49exx`) and
  the starter of eWeLink and Matter pairing (`ewelink_ble_pairing=%d,
  matter_ble_commission=%d`, near `0x48abc`) live in the pairing state machine.
- **A real phone cannot be bridged in.** `hci_send_2_controller` (`0xc1da4`)
  fires once at boot, for a vendor command (`r3=0x848`). The GAP flow goes
  through RivieraWaves KE messages in shared memory, not across a serialised HCI
  boundary, and the radio is the proprietary RW link layer behind the
  `0x900000` block and its exchange memory, so bridging a real dongle would mean
  intercepting KE messages and rewriting the controller's host-facing side.

### Monitor-mode RX injection

The fork can deliver received frames while the MAC is in monitor mode, which
AirKiss needs: the smartconfig path sets the wide-open filter `0x7fffffde` but
never drives the normal RX-enable handshake, and the scan state machine stays
out of `HW_ACTIVE`. In this firmware the frames reach a stub, so this matters
for other BK7238 images.

It is in `src/wifimac.py`, off unless asked for: the flag `monitor_rx_deliver`
(env `BK_MONITOR_RX=1`, or `mac.monitor_rx_deliver = True`). Off, the emulator
behaves as upstream's does, and the OpenBK7231T station tests pass 3 of 3. On,
`inject()` and `_rx_flush()` deliver frames if the MAC is `HW_ACTIVE`, or in
`monitor` with the flag set.

Every frame arrives (`sent == rx`, on a synthetic self-test and on a real
capture of 7446 frames, every descriptor recycled) given two things the replay
tool sees to:

- **Wait for the RX-FIQ handler object.** The firmware sets the monitor filter
  very early (about 0.15 s) but allocates the handler object `*0x403c10` about
  0.4 s later (allocator `0x17700`, stored at `0x16604`). A frame injected in
  between drives the RX FIQ against a NULL object and hangs it in a bogus `memcpy`
  (`0x427a8`, its length read from the unused vector-table word `0xdeadbeef`), so
  descriptors are never recycled. Waiting for `*0x403c10 != 0`
  (`S61_RX_OBJ_PTR`) gives the order real hardware has, where no phone broadcasts
  that early.
- **Inject from the emulator's worker thread,** through a `tick` hook: from the
  main thread, access to `rx_queue` races.

The RX FIQ's path, for reference: vector `0x1c`, the FreeRTOS context-save stub
`0x108c0`, the C dispatcher `0x10950`, RAM-resident UMAC glue, the ARM veneer
`0x3f1520`, then `0x16618`, which does `if (rtos_is_in_interrupt()) { obj =
*0x403c10; process(obj); }` (`rtos_is_in_interrupt` is the RAM pair `0x3f0fb4` and
`0x3f0f9c`), and the processors `0x17a20` and `0x1744c`. MAC RX interrupts are
FIQs (lines 16 to 22), and the firmware drives RX from `TIMER_RX_TRIGGER`
(bit 17).

### The connect path

When the device connects, it goes from `BekenWiFiDriver`'s connect, `0x74bfc`
("stop monitor, then connect to ssid", gated by `if arg3!=0`), to `0x74b90` and
`0x223f0` (`bk_wlan_sta_init`'s config, and the start of scan and connect). The
config is a `network_InitTypeDef_st` of about 158 bytes (`wifi_mode@0`,
`wifi_ssid@1`, `wifi_key@~33`, `dhcp@161`), and `sta_init` is `0x21bf8`. Connect
first stops monitor mode through `0x21dbc`, `0x20d14`, `sub_ee3c9`, `sub_ee049`
and `sub_edfb5`.

`sub_edfb5` is the RivieraWaves RW-task message send. It builds a message and
either handles it at once (when `in_rw_task()`, which is `sub_134bed(0x417564)`,
and a flag bit is clear) or posts it to the RW task and waits on a semaphore for
the result. Stop-monitor is also queued with `wpa_hostapd_queue_command`
(`sub_db0f9`) to the wpa_supplicant command queue (handle `*0x417474`, queue
`0x421cf8`, command 116), which `wpas_thread` works through. These complete
only in the firmware's real task context and the MAC's own order of events, so
a call forced from another context never gets its answer.
`IsWiFiStationProvisioned` is `0x7388e` (it reads `wifi-ssid` through `0x5ad84`
and `0x12f438`), reached from CHIP's `DriveStationState`, `0x73b28`.

## Tools and methods

The tools, and how each is run, are in
[`tools/beken/`](../tools/beken/README.md): the static analysis on the logical
image (`unstripe.py`, `disasm.py`, `xref.py`, `strxref.py`, `decomp.py`), the
emulator drivers (`cloud_push.py --apply` runs a whole conversion,
`stage_apply.py` the apply alone, a reversion's too) and the formats
(`stock_app.py`, `rbl.py`, `uf2.py`, `backup.py`). Those of the pairing
experiments are in `archive/s61-research/`. The emulator's tools take their
paths from the env `BK_EMU_ROOT` and `AUTOFLASH_BEKEN_EMU_DUMP`. The methods
behind the findings:

- **Running a dump:** `src/main.py <dump> --with-boot -chip BK7238 --only-uart
  --wifi-peer auto` (peer roles: `auto`, or `ap --wifi-ssid <x> --wifi-pass <y>`;
  `--net-out` lets it reach the real cloud).
- **FLASHDBG** (`FLASHDBG=1`, built into `emulator.py`) logs flash operations on
  the config regions (read opcode `op=5`). It found the live config reads at
  `0x3e0000` and `0x3f6000`.
- **A RAM dump, searched** (`emu.mu.mem_read(0x400000, 1<<20)`), found the decoded
  apikey UUID and the hard-coded URL template `http://%s-apid.coolkit.%s/...`.
- **Watchpoints** (`UC_HOOK_MEM_WRITE`, `UC_HOOK_MEM_READ`, on one buffer) find
  the PC and registers that make or use any buffer: they found the apikey
  formatter `0x427b8` and the memset `0x45ec`.
- **Logging the bootloader's flash reads** (through `REG_FLASH`, `0x803000`) found
  the download partition and the app's RBL header.
- **The force-call harness** runs a chosen function part way through a boot. Save
  the registers, set the arguments, set `LR = 0x5` (to trap at `0x4`) and an odd
  PC (`addr | 1`, for Thumb), and bump `emu._exc_epoch` so that the chunked run
  loop reads the PC and the Thumb bit again; a hook at `0x4` restores the
  registers on return. Set CPSR to `(cpsr & ~0x3f) | 0x1f | 0x20`. It reaches
  firmware code correctly, but cannot supply the RTOS or RW-task context that
  exists only inside the owning task: calls forced into the RW-task message path
  or the key-driver timer thread park or fault.
- **Static analysis** works on the logical image, where a CPU address equals a
  file offset; `decomp.py` gives angr's pseudo-C with no Java or Ghidra.

The BK7238's UART transmit register is `0x0080210c`: a test image can write to it
to show that it has booted.

### Rebuilding a whole dump without a serial cable

[`tools/beken/rebuild_dump.py`](../tools/beken/rebuild_dump.py) makes a device's
whole stock flash again from OpenBeken's `GET /api/flash` and the official
file; its README gives
[the steps and what was shown](../tools/beken/README.md#rebuilding-a-dump). On
the S61 in the emulator, rebuilt from 1.1.0, everything but the EasyFlash stores
(which hold the device's own later history) matches the stock bootloader's own
apply of 1.1.0, and the result booted to ONLINE and converted to OpenBeken
again. It cannot know the flash's unique id, or the device's exact old version
if the file is newer, though about 680 KB of the old app survives a conversion
and shows whether the versions match. The bootloader fills a short last block
with `00 00 10 00 80 80 02 29`, then zeros.

## Questions still open

- **Whether a real device answers as the emulator does:** `/api/flash` returning
  raw flash on a Beken device, and stock's checks of a rebuilt image. No real
  Beken device has been converted.
- **Where the button's long press becomes pairing**: the app code that sets the
  pairing method and posts `PAIR_EVT_START` to the appfw (names at `0x49c18`).
  With it, pairing would start from the firmware itself, not a forced call of
  the dispatcher.
- **Where each received credential is written.** BLE's `recv_ssid` and the
  `pairingMode` command (`0x492ae`) are the decoders left, AirKiss's being a stub;
  the write of `wifi-ssid` and `wifi-pass` has not been traced from either.
- **Whether a real Beken device on ESPHome releases its lease as it rejoins
  after a long router outage.** The emulator does, now and then, and why is
  known ([below](#esphome-releasing-the-address-it-keeps)); the code at fault
  is LibreTiny's and ESPHome's, not the emulator's, but two steps come from the
  Beken SDK - its supplicant joining by itself after LibreTiny's `disconnect()`,
  and the release waiting for an ARP answer until after the next lease - and
  only a device can say whether they happen on hardware: its router off for two
  minutes, then on, and the router's log read for a `DHCPRELEASE` just after
  its `DHCPACK`.
- **Whether the pairing soft-AP serves DHCP on a real device,** and its
  passphrase, without which (or an open build) the emulator's peer cannot
  complete an association.
- **Timers 3 to 5 at their own periods.** Modelling each enabled channel's
  period, as the fork does for Timers 0 to 2, would raise Timer 5 (`bk_timer`)
  at its real rate without breaking the other BK7238 self-tests (Zengge's among
  them), so the tools need not raise it by hand.
- **Passing the integrity gate with a real id.** `spi_flash.py` already takes
  `uid=` (its comment that nothing needs the real value is wrong for the S61), but
  nothing reads a real device's flash id, so the gate is forced at `0x29c3c`.
- **The encoding of the `ssid` and `pass` keys** in the app store.
- **Whether a paired device's dispatch is plain HTTP,** as the factory
  placeholder's is.
