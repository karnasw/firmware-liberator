# Beken tooling

The tooling for Beken (BK72xx) devices, worked out on the Sonoff S61. This page
says what each script is for and how to run it; what was found with them is in
[docs/beken-research.md](../../docs/beken-research.md). The tool's own Beken
route (`tools/autoflash/routes/beken/`) imports the format modules
([below](#conversion-and-config-migration)). The rest drive the
**BekenEmulator** with this project's patches, or analyse a firmware image
statically. The pairing experiments (AirKiss replay, BLE provisioning, the
button and pairing drives) are in
[archive/s61-research/](../../archive/s61-research/): no conversion needs them.

How to use the emulator is in
[docs/emulator.md](../../docs/emulator.md#the-beken-emulator), and how it works
in [emulator/](emulator/README.md). Its `beken-emu.sh python` runs a tool in
its container, with this checkout at `/work`:

```sh
tools/beken/emulator/beken-emu.sh python tools/beken/emulator/integrity_gate.py
```

In the container the emulator is at `/opt/beken-emulator`, the tools' default
`BK_EMU_ROOT`, and the fetched dump is their default `AUTOFLASH_BEKEN_EMU_DUMP`.
The image's Python has `capstone`, so the static tools run there too. Only
`decomp.py` needs `angr`, which goes into a virtual environment of your own,
never the system's Python.

The examples below run from `tools/beken`. The static tools are given as
`python3 TOOL.py`, on your computer with `capstone` or in the container with
`emulator/beken-emu.sh python` in place of `python3`. The emulator drivers run
only in the container, so theirs are written out. A driver's settings (its
`Env` below) go after `python`, before the script, as `NAME=VALUE`, and
nothing else of your environment reaches the container, so a variable set in
your shell for something else changes no driver:

```sh
emulator/beken-emu.sh python DISPATCH= SECS=90 emulator/cloud_dial.py
```

## Static analysis

These work on the **logical image**: the dump with its CRC stripes removed, so
that a CPU address equals a file offset. Make it once with `unstripe.py`, then
point the other tools at it.

- **`unstripe.py`** un-stripes a raw Beken dump to a logical image and decodes
  its RBL update headers. It uses the fork's CRC routine.
  `python3 unstripe.py <raw-dump> logical.bin`
- **`disasm.py`** disassembles a window with capstone, in Thumb unless told
  otherwise.
  `python3 disasm.py logical.bin 0x4203c 24`
- **`xref.py`** finds references to an address: BL/BLX callers over the code
  region, and 32-bit data words holding `addr` or `addr|1`.
  `python3 xref.py logical.bin 0x3eba0 0x21e50`
- **`strxref.py`** locates code by the log or format string it uses. It finds
  the string and its literal-pool references, and disassembles the `ldr` that
  loads each.
  `python3 strxref.py logical.bin "pairingMode by" "softap:ssid"`
- **`decomp.py`** decompiles functions to pseudo-C with angr. It is best-effort,
  a reading aid beside `disasm.py`, and needs no Java or Ghidra.
  `python3 decomp.py logical.bin 0x4203c`
- **`layouts.py`** reads the partition table a BK7238 app holds and says
  whether it is one the tool knows. A run refuses a device whose official file
  holds any other table, before anything is written.
  `python3 layouts.py official.bin`
- **`stock_points.py`** finds the places in a BK7238 eWeLink app where the
  emulator has to help it, by what the code there refers to. The emulator
  drivers use nothing else, so one code path runs every build. A point it
  cannot find, or could find in two places, is an error that names the point.
  It uses the standard library only.
  `python3 stock_points.py dump.bin official.bin app.bin`

  | Point | From what it is found |
  |-------|-----------------------|
  | `x509_crt_parse` | the function holding both `-----BEGIN/END CERTIFICATE-----` |
  | `ef_get_env_blob` | of those logging `ENV isn't initialize OK.`, the one keeping a fourth argument |
  | `after_memcmp`, `gate_passed` | the 32-byte compare after `%s%s%s%d%s%s`, whose result is tested at once |
  | `appfw_task`, `appfw_loop` | the function given to the task `ais_appfw_task`; the target of its first branch back |
  | `link_task`, `link_create`, `ais_cloud_start` | the task `ais_link_task`: its function, the call creating it, the function holding that call |
  | `start_cloud` | the one function calling `ais_cloud_start` |
  | `ctx_pointer`, `cloud_flag` | the first word in RAM each of those two reads through |
  | `lan_publish` | called straight before `start_cloud`, starting by reading a flag beside `cloud_flag` |
  | `dispatch_url`, `url_offset` | the plain `http://` URL in the function that checks `cloud_flag`; its field in the structure that function fills |

## Driving the emulator

The conversion path's drivers are in [emulator/](emulator/); the pairing
experiments are in [archive/s61-research/](../../archive/s61-research/) and run
from there, in the container as the drivers do: their examples' `python3` is
`../../tools/beken/emulator/beken-emu.sh python` there. The addresses quoted
are those of the S61's firmware 1.0.2; the current drivers find theirs with
`stock_points.py`. Each driver works only
inside the emulator, on a dump (the public one, or one of your own device):
none is ever written to or run on a real device.

### The conversion path

- **`integrity_gate.py`** lets the stock firmware start its app layer. Under
  emulation the `app` task otherwise hangs in a factory integrity self-check:
  `bk_flash_get_uid` is hashed and compared with a stored certificate, which
  the emulator's synthetic flash UID cannot match
  ([the gate](../../docs/beken-research.md#the-flash-uid-integrity-gate)).
  The driver forces that identity `memcmp` to "equal", the outcome it has on
  real hardware. The firmware then starts the app framework by itself:
  `key_add` registers the button (pins 0 and 2), the provisioned check runs
  and `ais_appfw_task` is created.
  `emulator/beken-emu.sh python emulator/integrity_gate.py`

- **`seed_join.py`** puts the emulated device on a network the way a
  provisioned one joins. It hooks the firmware's own `ef_get_env_blob`
  (EasyFlash V4) to return the tool's SSID and passphrase for `wifi-ssid` and
  `wifi-pass`, and forces nothing else. The fork's peer is a WPA2-PSK access
  point with the same credentials, and the device runs the real sequence:
  station mode, association, the WPA2 four-way handshake, then **IPv4 DHCP,
  which gives it 192.168.1.100**. It clears the integrity gate too: with the
  gate in place, seeded credentials leave the device idle. Env
  `SSID`/`PASS`/`CH`/`SECS`.
  `emulator/beken-emu.sh python emulator/seed_join.py`

- **`cloud_dial.py`** makes the emulated device dial the **eWeLink (CoolKit)
  cloud**. Left alone after the join, the device does **Matter** (`_matterc`):
  the eWeLink cloud link is created only when pairing succeeds. That trigger
  is one firmware call, the start-cloud handler (`0x2c204`), whose only callers
  are in the pairing state machine. The driver seeds the Wi-Fi credentials
  (join and DHCP), then calls that handler. The AIS cloud context is populated
  at the factory (UUID, model, MAC and dispatch URL at `*0x4044ac`, the pointer
  `stock_points.py` finds as `ctx_pointer`), so `ais_link_task` opens a TCP
  connection and sends `GET /dispatch/device`, retried with backoff. The
  dispatch URL's factory placeholder is `http://8.8.8.8:80`. **By default the
  driver repoints it to a local stub on the emulator's AP gateway
  (`http://192.168.1.1:80`), so the dial stays on your computer.** `DISPATCH`
  steers it elsewhere, and `DISPATCH=` keeps the placeholder. **Your own
  account only:** never point it at the real eWeLink cloud with anyone's
  account or device but your own. A dump of a device bound to an account would
  add only a faithful server and device secret. Env
  `DISPATCH`/`SSID`/`PASS`/`SECS`/`FORCE_AT`.
  `emulator/beken-emu.sh python emulator/cloud_dial.py`

- **`cloud_online.py`** completes the cloud cycle: the device reaches
  **ONLINE**, then sends heartbeats. It runs the CoolKit stub
  (`tools/ewelink/stub_cloud/stub_server.py`, with `--plain` dispatch) on
  loopback, serving dispatch in plaintext and the IoT channel over TLS, and
  bridges the emulator's netstack relay to it, so nothing leaves your
  computer. It seeds the Wi-Fi credentials
  and fires the pairing trigger as `cloud_dial.py` does, then drives dispatch,
  TLS `/api/ws`, `register` and the `{error:0, config}` answer that is ONLINE.
  It handles two things the S60 does not need:
  - The S61's dispatch reply must be **compact** JSON with a **`domain`** key
    and a **string** `port`.
  - The IoT `wss://` certificate is verified. The driver clears that with a
    **trust-anchor swap**, the S60's `--trust-ca` technique, applied at
    `mbedtls_x509_crt_parse` (`0x125e4c`) because the S61 assembles its
    CoolKit CA in RAM rather than keeping it in flash. It generates its own
    EC P-256 CA and leaf with openssl and serves the leaf. No verification is
    weakened: only the trusted root is swapped for the driver's own, and the
    firmware's verify path is untouched.

  Env `SSID`/`PASS`/`SECS`/`FORCE_AT`/`HB`. Its set-up is a `CloudSession`,
  which `cloud_push.py` builds on.
  `emulator/beken-emu.sh python emulator/cloud_online.py`

- **`cloud_push.py`** runs a whole S61 conversion the way the tool would drive
  one. The device comes online against the stub (`cloud_online.CloudSession`)
  and is pushed an eWeLink `upgrade` for an rt_ota `.rbl`, wrapped by
  `fota_wrap.py` and served from a placeholder public address that the relay
  sends to your computer. The device downloads it in ranges and stages it at
  `0x286000`, checked byte for byte; with `--apply`, the stock bootloader
  applies it on the next boot and the new firmware starts. The Wi-Fi is first
  written into a copy of the dump's EasyFlash store, so the new firmware finds
  it as on a provisioned device.
  `emulator/beken-emu.sh python emulator/cloud_push.py image.rbl out.bin --apply`
  (env `SECS`, `APPLY_SECS`)

- **`stage_apply.py`** is the apply half of a conversion, or of a reversion,
  in one command. It stages an rt_ota `.rbl` (OpenBeken's, the ESPHome
  build's, or the repackaged stock app's) in the download slot of a copy of a
  dump, seeds test Wi-Fi credentials in its EasyFlash store, and boots the
  stock bootloader over it with the emulator modelling the flash writes. The
  flash as the run leaves it is saved, so that
  `beken-emu.sh run OUT --wifi --wifi-peer ap ...` can show the installed
  firmware start afresh and join.
  `emulator/beken-emu.sh python emulator/stage_apply.py dump.bin image.rbl out.bin`
  (env `SECS`, `SSID`, `PASS`, `SEED=0`)

### The pairing experiments (archived)

- **`long_press.py`** drives the button long-press end to end through the
  firmware's own key stack. It needs two interventions: the integrity bypass,
  and raising Timer 5's interrupt status (`timer3_5_ctl` bit 9) each slow tick.
  The key driver's periodic runs on the `bk_timer`, hardware Timer 5, and the
  emulator raises only Timer 3, the RTOS tick, so the key-driver timer thread
  (`0x3eba0`) otherwise never wakes. With both, the thread wakes natively, its
  scan (`0x421e0`) runs, and a press-and-hold of pins 0/2 trips the real
  long-press action `0x42250`, which drives the relay and LED through
  `bk_gpio_output`. The raise stays in this tool: raising Timer 5 every tick in
  the emulator's `_pacing` broke 3 other BK7238 self-tests, and a clean fix in
  the fork needs each timer's period modelled.
  `python3 long_press.py`

- **`pair_drive.py`** drives the firmware into pairing mode and watches the
  emulated radio follow. With the gate cleared, the app framework's state
  machine (`ais_appfw_task`) cycles, waiting on `rt_event_recv` (`0x134da8`,
  event `[0x4040e0]`), but nothing sets a pairing method, so the device idles.
  The tool calls the firmware's own pairing-method dispatcher
  `0x21eb4` from inside that task's context. Method 0 (coexist) brings up
  **soft-AP**: the MAC leaves monitor mode and transmits beacons, and the
  virtual peer sees the device as an access point. Method 1 enters **station**
  mode, then probes and associates. `airkiss` instead drives the sniffer start
  `0x223b0`, and the RX-FIQ handler object goes live, but there is no decoder
  behind it in this build ([AirKiss](#airkiss-end-to-end-procedure)). It
  shares the integrity bypass and the Timer 5 raise with `long_press.py`.
  `python3 pair_drive.py 0` (soft-AP) `| 1` (station) `| airkiss`. Env
  `SSID`/`PASS`, `SECS`, `FIRE_AT`.

- **`ble_provision.py`** synthesises the eWeLink **BLE provisioning session**
  without a BLE radio. It feeds the provisioning messages (DEVICE_INFO,
  AUTH_TOKEN, HIDDEN_AP_CONNECT/Wi-Fi, SERVER_INFO) through the firmware's
  command dispatcher `0x72940`, so the firmware's own consumer task runs the
  handlers in context. Driving the handlers directly corrupts the shared
  state; the dispatcher does not. Data fields are injected in plaintext past
  the AES, whose key is the device apikey at RAM `0x40ce90`. The handlers run
  and the device joins the tool's access point (WPA2 and DHCP), the same join
  `seed_join.py` reaches. This exercises the real eWeLink provisioning path,
  but does not by itself dial the cloud: that needs the pairing trigger
  (`cloud_dial.py`). Env `SEQ`/`SSID`/`PASS`/`SECS`.
  `python3 ble_provision.py`

- **`button_sim.py`** is an older button simulation, from before the button
  was seen to register by itself; it defaults to the wrong pin, 3. It force-runs the block init and `key_add`, then presses
  by driving the GPIO low and latching the capture status in
  `state.pwmn_stat`, which reaches the armed long-press timer.
  `python3 button_sim.py [pin]`

- **`airkiss_capture.sh`** puts a Wi-Fi adapter that can capture in monitor
  mode (an Intel AX200 can) into it on the router's channel, and captures a
  phone's eWeLink AirKiss/smartconfig broadcast to a pcap. It needs root and
  takes over the Wi-Fi adapter while capturing.
  `sudo ./airkiss_capture.sh <channel> [iface] [out.pcap]`

- **`airkiss_replay.py`** replays captured 802.11 frames into the emulated
  S61's Wi-Fi MAC, in monitor mode, for the firmware's AirKiss decoder to
  consume; an AP-role peer with the known SSID and password then lets the
  device associate. Its pcap and radiotap parsing (DLT 127/105) uses the
  standard library only.
  - `python3 airkiss_replay.py capture.pcap --ssid MyNet --pass MyPass`
  - `python3 airkiss_replay.py --selftest` (a pipeline check on synthetic frames)

### AirKiss end-to-end procedure

Frame delivery into the MAC works (`sent == rx`, 100 %, on the self-test and
on a real capture of 7446 frames) once injection waits for the RX-FIQ handler
object (`S61_RX_OBJ_PTR`, default `0x403c10`); the tool sets
`monitor_rx_deliver` on the fork's MAC. **This build has no decoder to reach,
though:** the only monitor-frame callback the firmware ever registers at
`0x403b20` is the stub `0x12c483` (`bx lr`), so more than 11 000 replayed
frames decode nothing. eWeLink compiled the EZ decoder out, and real pairing is
soft-AP and BLE
([the research](../../docs/beken-research.md#airkiss-the-decoder-is-a-stub)).
The tooling is kept for a firmware that does decode:

1. Pick an SSID and password to provision to, of a real or throwaway network.
2. `sudo ./airkiss_capture.sh <router-channel> wlp1s0 airkiss.pcap`
3. On the phone: eWeLink app → add the device → Wi-Fi/quick (AirKiss) pairing →
   enter that SSID and password → start. Let it run for 30 to 60 s, then
   Ctrl-C the capture.
4. `python3 airkiss_replay.py airkiss.pcap --ssid <SSID> --pass <PASS>`
5. Watch whether the firmware leaves monitor mode, probes and associates, and
   gets a DHCP lease.

A capture carries the credentials of whatever network it was taken on, in its
frame lengths, so a `.pcap` or `.pcapng` is **never committed**: the leak scan
refuses one by its name. The regression check uses synthetic frames instead
(`airkiss_replay.py --selftest`), not a captured file.

## Conversion and config migration

The tool's Beken route, `tools/autoflash/routes/beken/`, is built on the
modules listed first below (`backup.factory_mac`, for one, names the backup).
The findings behind carrying the Wi-Fi credentials into OpenBeken (the config
partition map, OpenBeken's `mainConfig_t` format, the signed-char `Tiny_CRC8`,
the `0x1e1000` config offset) are in
[the Beken research](../../docs/beken-research.md#carrying-wi-fi-across-the-conversion-the-openbeken-config).

**What a conversion uses:**

- **`fota_wrap.py`** wraps any rt_ota `.rbl` in the CoolKit FOTA container the
  stock updater downloads (magic `0x1beef11e`), unwraps an official file, and
  can serve one.
- **`rbl.py`** and **`aes.py`** read and write rt_ota RBL images as the SDK's
  packer makes them: gzip, PKCS#7 and AES-256-CBC under RT-Thread's public
  default key, and the 96-byte header with its CRC-32s and FNV-1a hash.
  `rbl.pack()` gives the same body, byte for byte, as
  `rt_ota_packaging_tool_cli -c gzip -s aes`, and `rbl.unpack()` checks and
  unpacks one. The AES is written out in `aes.py` because the tool runs on
  Python's standard library, which has none.
- **`stock_app.py`** extracts the stock app from a dump, verified against the
  FNV-1a hash in its RBL header (`extract()`), and packs it again as the RBL
  the stock bootloader applies for a revert to stock (`stock_rbl()`).
  `python3 stock_app.py dump.bin -o stock_app_1.0.2.bin`
- **`uf2.py`** writes the UF2 that an ESPHome built on LibreTiny takes as an
  update, with an RBL in its `download` part for the stock bootloader to
  apply: byte for byte what
  `ltchiptool uf2 write --legacy IMAGE=device:download` writes for the S61's
  board. It is how a device on ESPHome goes to OpenBeken or back to stock.
- **`interim_marker.py`** makes the one flash sector, sent after the OpenBeken
  image in the same update, that tells OpenBeken it is the interim firmware
  on a run to ESPHome
  ([how the driver uses it](openbeken/README.md#the-interim-firmware)).
- **`backup.py`** copies a converted device's own flash region
  (`0x3e0000-0x400000`) out over OpenBeken's REST API into
  `captures/devices/<mac>/sonoff-backup/`, never overwriting a backup
  ([what it holds](openbeken/README.md#backing-up-the-device)).
  `python3 backup.py --device http://<device> --stock-rbl stock_app_1.0.2.rbl`
- **`scrub.py`** takes your Wi-Fi name and password and the device's eWeLink
  identity out of a copy of a whole flash before a readout leaves your
  computer ([docs/readouts.md](../../docs/readouts.md)).
- **[`openbeken/drv_autoflash.c`](openbeken/README.md)**, with each device's
  table for it, is **the mechanism the tool ships**: an OpenBeken driver that,
  on first boot, reads the stock EF40 credentials with `beken_hal_flash_read`
  and sets them through OpenBeken's own `CFG_SetWiFiSSID()` and
  `CFG_SetWiFiPass()`, so it survives changes to OpenBeken's config format. It
  also carries the meter calibration and wiring over, and only reads the stock
  store, so a conversion stays reversible.
- **[`devices/sonoff-s61/esphome.md`](../../devices/sonoff-s61/esphome.md)**
  covers the **ESPHome route**: how the LibreTiny config reads the stock Wi-Fi
  and meter calibration at boot, its 4 MB flash layout, the build, reversion
  through ESPHome's own OTA, and what the emulator has shown.

**For reading a store, and the offline route:**

- **`ef40_dump.py`** lists the stock EasyFlash V4 (EF40) key-value stores in a
  flash dump, checking each node's CRC. It shows `wifi-ssid`/`wifi-pass`,
  `elecCalibCoef` and the rest. `python3 ef40_dump.py <flash.bin>`
- **`ef40.py`** is the EF40 reader and writer library. `parse_store()` reads
  KV40 nodes; `provision()` writes synthetic `wifi-ssid`/`wifi-pass` nodes,
  for building a provisioned test dump, since the public dump holds no Wi-Fi
  to join.
- **`obk_config.py`** builds an OpenBeken `mainConfig_t` blob carrying given
  Wi-Fi credentials, with the right ident, version and offsets, a `sizeof` of
  3584, and the **signed-char** `Tiny_CRC8` the shipped binary uses.
  `build_config(ssid, pass)`.
- **`wifi_migrate.py`** is the offline chain: it reads `wifi-ssid`/`wifi-pass`
  from a stock dump's EF40, builds an OpenBeken config and writes it at the
  config partition of an OpenBeken image. It breaks whenever OpenBeken changes
  its config format: prefer the driver above.
  `python3 wifi_migrate.py --stock stock.bin --obk OpenBK7238_QIO.bin --out converted.bin`

## Rebuilding a dump

**`rebuild_dump.py`** makes a BK7238 device's whole stock flash again with no
serial cable, so that a device with no public dump can be run in the emulator.
It is not part of the tool's run. Everything it reads or writes is private, and
the result is Sonoff's firmware, so it all stays under `captures/`. The
emulator check (`tools/repo/emulator_check.py`) uses it for a device with no
public dump, the public S61 dump standing in for a readout.

1. Convert the device to OpenBeken with the stock updater, which overwrites the
   start of the stock app.
2. Read the whole flash with `GET /api/flash/<start>-<len>` (`backup.read_flash`).
3. Get the model's official OTA file: a FOTA container around an RBL of LZMA
   under the public AES key (`fota_wrap.unwrap`, `rbl.unpack`).
4. `python3 tools/beken/rebuild_dump.py READOUT.bin OFFICIAL.bin OUT.bin [--compare DUMP.bin]`

**What is shown.** It was tried on the S61 in the emulator, with the public
dump as the answer. Reading over REST gave the device's flash byte for byte,
and each region of the result compares so:

| Region | Against the public dump |
|--------|-------------------------|
| bootloader and partition table, radio calibration, stale sectors in the app partition, user config | the same |
| OpenBeken's config sector and other writes (`INTERIM_WRITES`) | the same once they are erased |
| app | that of the official file, byte for byte as the stock bootloader writes it. The file's version need not be the device's: the public file is 1.1.0, the dump 1.0.2 |
| RBL header, download slot | that of the official file, as the bootloader leaves them. They come from the file, so match only at the same version |
| EasyFlash stores | differ: the device's own history since the dump (Wi-Fi details, `ota&state`, the eWeLink keys, a Matter id) |

The rebuilt flash boots in the emulator to the point the public dump reaches:
through the stock bootloader, onto Wi-Fi and ONLINE on the local stub, under
the usual patches. With 1.1.0 from the public file it reports `romVersion`
1.1.0, and `cloud_push.py --apply` then converts it to OpenBeken as it does
the dump.

Whether the file is the version the device ran shows in the output: OpenBeken
is shorter than the stock app, so the rest of the old app is still in the
flash to compare with. The emulator's help goes where `stock_points.py` finds
it, so another version or model needs nothing moved. Each BK7238 official file
of the S61's generation, on the S61's bootloader and settings, comes ONLINE on
the stub: 1.0.2 from the public dump, and S61 1.1.0, BASIC-1GS 1.0.4,
BASIC-1GSP 1.0.5, S41 1.0.6 and T6 1.0.3 rebuilt. The door sensor's firmware
is another stack: its points are not found, and the tools refuse it by name.

**What is not proven.** None of it has run on a real device, including that
the REST read returns the raw striped flash there as it does in the emulator.
The flash's unique ID is not read at all: the integrity gate, which checks it,
stays patched. Only this project's OpenBeken build was read, soon after its
first start: `INTERIM_WRITES` assumes those places were blank before it, as
they are on the S61, and another model's may not be.

**A device on another table needs more.** The official apps carry the SDK's
table, though not the bootloader's FAL one. The S41's, the T6's and the Basic
Gen5's (`BASIC-1GS`, `BASIC-1GSP`) are the S61's 4 MB one. The 2 MB door
sensor's puts `NET info` at `0x1e1000`, where it holds data, and OpenBeken's
2 MB build writes its config there, so a conversion would lose that sector
before it could be read. `rebuild()` refuses a table it has no
`INTERIM_WRITES` for unless they are given.

## Adding a device

How to add a 4 MB BK7238 device, with or without a whole dump, what the tool
checks of one added from its official update file, and the candidates looked at:
[docs/adding-a-device.md](../../docs/adding-a-device.md#a-beken-device).

## Status

Everything on this page has been shown in the emulator, on the S61's public
dump. None of it has yet run on a real device: a conversion of a real S61 is
what remains ([the S61's page](../../devices/sonoff-s61/README.md)).

- **Works:** the stock firmware starts its app layer, joins a network, dials
  the eWeLink cloud and comes ONLINE on a local stub
  ([the cloud](../../docs/beken-research.md#the-cloud)). The device converts
  over the cloud as the S60 does: an `upgrade` command names a `downloadUrl`
  the sender controls, and the stock bootloader applies the `rt_ota` RBL in
  the CoolKit FOTA download. Nothing in that chain needs a signature or a
  secret key: `rt_ota_custom_verify` checks a link address, the body is
  encrypted only under RT-Thread's public default AES key, and integrity is
  CRC-32 and FNV-1a. OpenBeken's own `OpenBK7238_<ver>.rbl`, staged with no
  bypass, passes every check and boots
  ([the update path](../../docs/beken-research.md#the-update-path)); the
  emulator performs the whole apply
  ([how](emulator/README.md#the-emulator-performs-the-whole-ota-apply)). A dump
  is rebuilt from a converted device ([above](#rebuilding-a-dump)).
- **Reached, but not needed by a conversion:** the button's long press and
  pairing mode, with the archived tools.
- **Does not work:** AirKiss, whose decoder is a stub in this build
  ([above](#airkiss-end-to-end-procedure)).
