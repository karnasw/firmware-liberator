# The Espressif emulator

How the Espressif emulator (for the ESP32-C3 and the classic ESP32) is put
together and what it changes, for working on it. How to use it is in
[docs/emulator.md](../../../docs/emulator.md#the-espressif-emulator).

## How it fits together

```
 your browser ── localhost:8080 ─┐
                                 │  container autoflash-emu ("your computer")
 ┌───────────────────────────────┼──────────────────────────────────────────┐
 │  browser-forward ─────────────┘                                          │
 │  autoflash/cli.py, nmcli stand-in, wifi / device commands                  │
 │        │                                                                 │
 │  tap0 192.168.60.1/24 ── dnsmasq (DHCP + DNS; device fixed at .50)         │
 │        │                    └─ internet: only with --cloud (iptables)    │
 │  QEMU (lcgamboa fork) ── emulated Wi-Fi ── open AP PICSimLabWifi         │
 │        └─ flash.bin / efuse.bin in captures/emulation/state/             │
 └──────────────────────────────────────────────────────────────────────────┘
```

- **QEMU.** Espressif's QEMU has no Wi-Fi, so the emulator uses
  [lcgamboa's fork](https://github.com/lcgamboa/qemu) (branch `picsimlab-esp32`),
  which emulates both chips' Wi-Fi hardware and offers three open access
  points, wired to a tap device in the container. The image holds both of its
  builds, `qemu-system-riscv32` and `qemu-system-xtensa`.
- **The home network.** `tap0` is the home network and the container its
  router: dnsmasq hands out addresses (the device's MAC always gets
  192.168.60.50), and mDNS works as on a real LAN. Without `--cloud`, iptables
  keeps the container off the internet, so the cloned identity cannot be used
  by mistake.
- **The Docker network** every instance and any Windows container share,
  `autoflash-emu`, is a /24 on a subnet none of the computer's routes or other
  Docker networks use (198.18.60.0/24 where it is free). The tool in the
  container sweeps the network it is on at its real size, up to a /22, and
  Docker's default bridge is a /16: some 4 s more a run.
- **Wi-Fi scans.** The tool lists networks with `nmcli`, whose stand-in
  reports the fork's access points and any network the device broadcasts.
- **The firewall and sudo.** As on a typical Linux desktop, ufw blocks
  incoming connections and sudo asks for your password (`autoflash`), so the
  conversion must add its temporary rule for the device through sudo and remove
  it, and any exit path that leaves it behind shows up. ufw allows only the
  device's DHCP and DNS, and port 8080 for your browser. With no systemd in the
  container, a stand-in answers the tool's `systemctl is-active ufw` from ufw's
  own setting. The `wifi` command needs no password, as a desktop's Wi-Fi menu
  does not. `--no-firewall` leaves ufw off.
- **Power.** `device-supervisor` restarts QEMU whenever it exits, as a power
  cut looks.
- **The API** the tool's readouts go to is the project's own code
  (`website/api/local.mjs`) in a container of its own on the same Docker
  network, `autoflash-emu-api`, started before the emulator and removed by a
  watcher once the emulator's container stops (`tools/repo/emulator-room.sh`);
  `unattended-test.sh` starts one of its own each run instead.

## The image

The image is labelled with a hash (`image.sh tag`) of `emu.Dockerfile`,
`UPSTREAM`, `patches/` and `container/`, so any change to them, a comment
included, is a new image for everyone. The `emulator-image` workflow publishes
each as `ghcr.io/adamsthws/autoflash-emu:<that hash>`; `emu.sh` pulls that,
builds only where it is not published (`AUTOFLASH_EMU_NO_PULL=1`, or
`--rebuild`, always builds), and runs only an image labelled with its own
checkout's hash.

It is built from the fork's source at the commit `UPSTREAM` pins (`64aed50`),
with the subprojects its build fetches, as an archive on the release named after
the upstream project and that commit,
[`emulator-source-qemu-64aed50`](https://github.com/adamsthws/firmware-liberator/releases/tag/emulator-source-qemu-64aed50).
`image.sh` downloads it once into `captures/emulation/cache/`, checks it
against the SHA-256 in `UPSTREAM`, and builds from it with no network. QEMU is
GPL-2.0: the archive is its unmodified source, and this project's changes are
the patches in `patches/`, so a change to the emulator is a new patch, not a
new release.

Releases are immutable, so moving the pin is a new archive on a new release;
the old one stays for older checkouts. (The earlier `emulator-source`
release, deleted on 2026-10-29, cannot have its name used again.) To move it,
make the archive of the fork at the new commit, with its subprojects, as
`qemu-lcgamboa-<commit>.tar.xz`; `image.sh pin ARCHIVE COMMIT` writes the pin
into `UPSTREAM` (repo, commit, archive, SHA-256) and says the release to
publish it as. The [Beken emulator's image](../../beken/emulator/README.md#the-image)
is kept the same way.

## What is changed, and why

### The dump (`patch_stock.py`, `rewrite_nvs.py`)

Only copies are changed, never the original dump. `patch_stock.py` changes
every app slot holding Sonoff's firmware, after checking the app's checksum
and SHA-256, and seals it again. It finds each place in the firmware itself,
not at a fixed address, so the same code works on every device, and stops with
an error, rather than boot half-changed, when Bluetooth start-up (or the
pinned CA, with `--stub-cloud`) cannot be found.

- **Bluetooth start-up is skipped.** Neither QEMU emulates Bluetooth: the
  controller's clock set-up fails and its ROM code faults. The functions that
  call `esp_bt_controller_init` `return 0` at once; it is found as the one
  function holding the address of its own name, for its asserts (through an
  `l32r` from a literal pool on a classic ESP32, a `lui` and `addi` on an
  ESP32-C3). Bluetooth serves only first-time pairing in the eWeLink app,
  which a device being converted has done.
- **The DPORT stall is skipped, on a classic ESP32.** An ESP32 before revision
  3 has a bug in reading DPORT registers, so its firmware stops the other core
  around each read and waits for its answer. QEMU has no such bug, and its
  other core can be too slow to answer, tripping the interrupt watchdog at
  start-up. `esp_dport_access_stall_other_cpu_start` is the one function that
  holds the addresses of both cores' interrupt registers (`0x3FF000E4` and
  `0x3FF000E8`); it and the `_end` function after it return at once.
  Firmware on ESP-IDF 5 is left alone, as its `esp_ipc_isr` uses those
  registers to stop a core.
- **With `--stub-cloud`, the device trusts the stub.** eWeLink's CA, pinned in
  the firmware, is found by its issuer (the older RSA `coolkit` one, or the
  newer `CoolKit Root CA ECC`) and replaced, in its fixed 1040-byte slot, with
  the stub's: EC P-256, as an RSA-2048 one would not fit, or RSA-1024 where the
  firmware pins the RSA CA (`make_ca.py --rsa`, kept in
  `captures/emulation/stub-cloud-rsa/`), as some firmware cannot take an EC one
  ([why](../../../docs/cloud-stub.md#what-it-trusts)). As OpenSSL on Ubuntu
  refuses an RSA-1024 CA by default, the tool in the container runs with
  `OPENSSL_CONF` at `make_ca.py`'s `openssl.cnf` (`SECLEVEL=1`); nothing
  outside the container changes.
- **The home Wi-Fi becomes the emulator's.** Stock 1.2.0 keeps the network it
  joins in ESP-IDF's `nvs.net80211` (`sta.ssid`, `sta.pswd`) and in Sonoff's
  own `frame_params` (`ssid`, `passwd`). Both are rewritten to `PICSimLabWifi`
  with no password, so the real home Wi-Fi is not in the emulated device at
  all. The partition is rebuilt with ESP-IDF's `nvs_partition_gen`, which adds
  `sta.apinfo`, `sta.pmk` and `sta.apsw` itself. A dump taken after the device
  was reset, with no network or dispatch address in `frame_params`, would wait
  to be paired, so they are added.
- **The eFuse dump, which gives the device its MAC, needs no conversion.**
  QEMU's ESP32-C3 eFuse file is the read registers `EFUSE_RD_WR_DIS_REG` to
  `EFUSE_RD_SYS_PART2_DATA7_REG` in order, as `efuse.bin` holds them, padded to
  1024 bytes. A classic ESP32's is its four blocks' read registers, 124 bytes,
  used whole.
- **A test copy's eFuses hold its MAC and little else.** `public_dump.py`
  writes them, as a public dump has none. An ESP32-C3's also carries the wafer
  revision its app's header asks for (byte 14, the lowest it runs on), or the
  bootloader stops at `This chip is revision 0`. A classic ESP32's says
  revision 0, which its firmware accepts.

### The fork (`patches/*.patch`)

Each patch is `git format-patch` output, one change with its reasoning in its
header, named `NNNN-what-it-does.patch` as the Beken emulator's are and applied
in name order when the image is built. To change one, apply them all to the
fork's source, make the change, and regenerate them.

- **`0001-monitor-main-loop-context.patch` - QEMU's monitor answers.** The
  fork's main loop iterates its own context, but the monitor registers its
  handlers with GLib's default one, which nothing iterates, so `-monitor` never
  accepted a connection on its socket. Upstream as
  [lcgamboa/qemu #18](https://github.com/lcgamboa/qemu/pull/18).
- **`0002-access-points-off.patch` - the router can be restarted.** The fork's
  access points live inside QEMU, so blocking traffic outside it only cuts the
  device off from the computer (`iptables -I INPUT -s 192.168.60.50 -j DROP` in
  the container does that), with its Wi-Fi still up. While the
  `access-points-off` property is set (`device router off`, over QEMU's
  monitor), they send no beacons, answer nothing, and deauthenticate the device
  a few times and whenever it sends data, as a real access point does a station
  not joined. The device loses its Wi-Fi within a second or two, retries as a
  real one does ("Connect failed with AP timeout"), and rejoins within seconds
  of `device router on`. Upstream as
  [lcgamboa/qemu #19](https://github.com/lcgamboa/qemu/pull/19).
- **`0003-esp32-cache-refresh-only-changed-pages.patch` - a classic ESP32's
  flash operations are quicker.** Each time its flash cache was enabled or
  flushed, the fork discarded the translated code of the whole 4 MB window.
  Now only a page whose bytes changed is refreshed: Tasmota boots in about 7
  seconds instead of 10. Upstream as
  [lcgamboa/qemu #20](https://github.com/lcgamboa/qemu/pull/20).
- **`0004-esp32-cache-window-stays-mapped.patch` - a classic ESP32 is quick
  enough not to trip its watchdog.** Each flash operation also took the window
  out of the memory map and put it back, so a 32-byte flash read took
  milliseconds of the firmware's time instead of microseconds, and firmware
  reading flash a lot in one go outlasted its 5-second task watchdog and
  panicked, as Tasmota did reading 2,000 values in one Berry command. A window
  now stays mapped once on: Tasmota boots in about 3 seconds, and the tool
  reads its settings backup in about 6 instead of 70. It gives up QEMU's fault
  for reading flash with the cache off, so it is not offered upstream;
  [espressif/qemu #183](https://github.com/espressif/qemu/pull/183), not yet
  merged, makes the switch fast and keeps the fault.
- **`0005-esp32c3-saradc-channel-readings.patch` - a sensor on the board reads
  as it would at rest.** Only PICSimLab gives the fork's ADC a value, so every
  channel read 0, which the BASICR4's firmware took for overheating, refusing
  updates (`"error": 407`). The properties `adc1_ch0` to `adc1_ch4` hold each
  ADC1 channel's raw 12-bit sample, set by `device-supervisor` from a kind's
  `adc_readings` ([kinds.py](../../autoflash/kinds.py)). Not offered upstream
  yet.
- **ADC calibration in the eFuses.** A real ESP32-C3 leaves Espressif with its
  ADC measured in eFuse block 2: for each attenuation, an init code (the
  offset) and the reading at a known voltage. Without it `esp_adc_cal` turns
  every reading into 0 mV. `flashmap.esp32c3_efuse` gives each emulated chip a
  nominal calibration, version 1 with its fields 0: the BASICR4's raw 2700 then
  reads about 1.0 V, where a real chip would differ by a few percent. ESP-IDF
  reads block 2 so:
  `BLK_VERSION_MAJOR` (bits 128-129) 1 means calibrated; each attenuation's
  init code (10 bits from bit 148, one after another) is its field + 1000; its
  calibration reading (10 bits from bit 188) is 2000 plus or minus its field
  (bit 9 the sign), taken at 400, 550, 750 and 1370 mV for 0, 2.5, 6 and 11 dB.
  Block 2's bit *n* is in the eFuse file's word at `0x30 + 4 * (n / 32)`.
- **A classic ESP32's ADC is left as the fork has it**: every channel reads 0
  (`esp32_sens`'s `ADC_values`, set only by PICSimLab), and its eFuse file
  (`flashmap.esp32_efuse`) holds no calibration, so `esp_adc_cal` reads its
  attenuation's offset, 75 to 142 mV. Only the NSPanel's firmware reads its
  ADC, reporting -2 °C here
  ([more](../../../docs/espressif-research.md#sonoffs-ais-sdk)). A believable
  reading would take properties like 0005's on `misc.esp32.sens`; no
  conversion needs it.
- **`0006-esp32-uart-rx-timeout-ref-tick.patch` - a classic ESP32 hears a
  short answer in time.** A UART hands the firmware what it received when its
  FIFO fills or the line has been quiet for its RX timeout. The fork always
  counted the timeout's register in 8 bit times, its unit on the APB clock; on
  REF_TICK, which Arduino-ESP32 runs every UART up to 250000 baud from, ESP-IDF
  writes it 64 times larger for the same time. So Tasmota's CSE7761 port (38400
  baud 8E1, a timeout of one character: register 88) held each answer 18 ms
  instead of 0.3, and Tasmota, which waits 6 ms, found no meter
  ("C61: Rx none") though the board answered within 0.1 ms. The register now
  counts in the unit of the UART's clock. Offered as lcgamboa/qemu#21, and to
  Espressif's QEMU, where the fork's UART comes from and the same fault hides
  behind a baud rate on REF_TICK taken from 40 MHz, 40 times too fast, as
  espressif/qemu#212
  ([upstream contributions](../../../docs/upstream-contributions.md#the-emulators-classic-esp32)).

**When you next move the pin:** lcgamboa merged #18, #19 and #20 after the
pinned commit, on 7 November 2026 (`2d075b740`), so `0001`-`0003` drop out on a
commit after that. Replace `0004` with
[espressif/qemu #183](https://github.com/espressif/qemu/pull/183) (or
lcgamboa's copy of it, once there): written for Espressif's `esp32.c`, its
changes to that file go into `esp32_picsimlab.c`, and it touches the same part
of `esp32_dport.c` as #20. Then check that 0004's timings still hold, and that
ESP-IDF's panic test `test_cache_error` still ends in "Cache disabled but
cached memory region accessed".

Worth trying in the same rebuild, but not needed: `-icount shift=3,sleep=off`
for the classic ESP32 alone, which runs the firmware's clock by the
instructions it runs rather than by the wall's. A starved emulator now makes a
device's clock jump ahead and its timeouts fire on the jump: Tasmota's web
server once dropped an upload after 5 s with no data (the tool now restarts the
device and sends it again). Under `-icount` the 2,000-read Berry command no
longer tripped the watchdog (4 runs of 4), but with `sleep=off` the clock also
races ahead while the firmware idles waiting for the computer, so timeouts may
fire sooner instead. Ship it only if, on the classic ESP32:

1. the 2,000-read Berry command still finishes without the watchdog panic;
2. repeats of the TH Elite's upload at D4 all go in at once, none cut off or
   held back for minutes (one took 376 s on 8 November 2026);
3. `emulator_check` passes all four routes for the POW Elite and the TH Elite;
4. a conversion is no more than about a fifth slower.

Tried and dropped for the classic ESP32: QEMU's multi-threaded mode (MTTCG),
which was slower; stretching the firmware's watchdogs, which only hid real
hangs longer; and the two cores taking turns every millisecond instead of
every 100, which made no difference once flash operations were quick.

What else the emulator needed from the fork is
[upstream now](../../../docs/upstream-contributions.md#the-emulators-wi-fi):
real addresses on the wire, frames written only where the hardware would put
them, a signal strength with its minus sign, and the computer joining the
device's own network while the device also tries the home one, on a link of
its own with its own MAC (02:53:36:30:00:02).

### How QEMU is run (`container/bin/device-supervisor`)

- **The chip's QEMU.** `emu.sh` passes the chip it read from the flash, and
  the PSRAM the kind asks for, as `CHIP` and `PSRAM`
  ([Classic ESP32 devices](../../../docs/emulator.md#classic-esp32-devices)).
  The supervisor picks the binary, machine, eFuse device and Wi-Fi device by
  the chip, and gives the PSRAM as `-m`:

  | | ESP32-C3 | Classic ESP32 |
  |---|---|---|
  | QEMU | `qemu-system-riscv32`, machine `esp32c3-picsimlab` | `qemu-system-xtensa`, machine `esp32-picsimlab` |
  | eFuse file | 1024 bytes (a shorter one is padded) | 124 bytes, as it is |
  | Container memory | 256 MB | 350 MB (it peaked at 250 MB; see [below](#what-a-classic-esp32-needed)) |
- **`softap-status`: the device's own network is visible.** It reads QEMU's
  output and keeps the name of the network the device broadcasts, from the
  fork's `guest SoftAP` lines, for the `nmcli` stand-in and `wifi join`.
- **`gpio_in`: the button is not held down.** Run alone, the fork drives no
  input pins, so all read low. The S60's button (GPIO9) is active low, so
  Tasmota saw it held and after 40 seconds reset itself to its defaults,
  losing its Wi-Fi. `-global driver=esp32c3.gpio,property=gpio_in,value=0x3fffff`
  makes inputs idle high, as with pull-ups. On a classic ESP32, `gpio_in` and
  `gpio_in1` (the pins above 31) are set all high the same way, and
  `strap_mode` is `0x12`, the strapping pins' value for booting from flash. A
  pin a board holds low is in `board.py`'s `HELD_LOW`, which sets `gpio_in`
  again after these: the POW Elite's GPIO23, by which Sonoff's firmware tells
  a POWR316D from a POWR320D.
- **`-no-reboot`.** The fork keeps its peripherals running across a software
  reset, so firmware restarting into a different image (stock into the bridge,
  the bridge into Tasmota) crashed at start-up on what the previous one left
  behind. QEMU now exits on a reset and is started again, a clean power-on;
  nothing in the conversion depends on the reset type.
- **Through the device's board.** QEMU is started by
  [`board.py`](#the-devices-board-boardpy), which serves its serial ports and
  power meter.
- **Only ARP requests for the device reach it** (a `tc` filter on `tap0`, in
  `entrypoint.sh`). When the device does not answer, the tool pings every
  address on the network, and that burst of ARP requests stopped the fork's
  Wi-Fi receiving for good: the device kept sending but heard nothing until
  restarted. A real device ignores requests for other addresses anyway.

### The device's board (`board.py`)

A device's power meter is a chip on its board, beside the ESP32, which QEMU
lacks, so `board.py` is that board. `device-supervisor` starts QEMU through it,
from the checkout's copy in the container (`/work`), as the redactor and the
cloud stub are run, so changing it needs no new image. It gives QEMU every UART
as a socket it serves (`-chardev socket` and `-serial chardev:`), and a QMP
monitor of its own, through which it reads the chip's GPIO registers with `xp`:
the output and output-enable registers every 100 ms, the GPIO matrix's routing
every second.

- **The wiring is the kind's Tasmota template** (`kinds.py`; `emu.sh` passes
  the kind it finds by the device's product model as `DEVICE_KIND`): Relay1 and
  Relay2, and the meter's pins, CSE7766 RX or CSE7761 TX and RX. A UART hears
  the meter when its RX signal takes the meter's output pin, through the matrix
  or that pin's IOMUX function (U2RXD's is GPIO16, the POW Elite's meter), and
  the meter hears a UART when the matrix routes that UART's TX to the meter's
  input pin. So the firmware's own routing connects them: Tasmota and Sonoff's
  firmware read the S60's meter on UART1, ESPHome on UART0, all from GPIO7,
  and each finds it.
- **UART0 also reaches the console**, the serial log, wherever else it is
  routed, as its IOMUX pins keep their function on a real chip; ESPHome's
  commands to a CSE7761 on UART0 show in the log too.
- **What a UART does not take is lost**, as on a wire into a full FIFO: the
  board sends the meter's bytes only while QEMU has read nearly all it sent
  before. Queued instead, they left a firmware busy for a few seconds minutes
  behind the meter (QEMU paces a UART's reads to its baud rate once its FIFO
  has filled): Tasmota read the relay as off for minutes after it went on. And
  a UART the firmware never reads (UART2 takes GPIO16, the POW Elite's meter,
  through the IOMUX until told otherwise) stopped the board.
- **The load.** 100 W, resistive, on 230 V 50 Hz mains, measured while the
  channel's relay is on (its GPIO's output set and enabled) and nothing while
  it is off. The voltage, measured on the supply side, is
  there either way. The POWCT's clamp is not in its relay's line, so it reads
  the load whatever the relay does.
- **The CSE7766** (the S60, the POW Elite) sends a 24-byte frame about every
  55 ms by itself: voltage, current and power each as the chip's coefficient
  and a cycle, the PF pulse count, a checksum. Its coefficients are a typical
  chip's (191200, 16140, 5364000), none of them Tasmota's defaults, so Tasmota
  takes them as the chip's own and the run's calibration (`calibration.py`) can
  be tested. With no load the frame says the power cycle is out of range
  (header `0xF2`), as the chip does.
- **The CSE7761** (the Dual R3, the POWCT) answers at 38400 baud: reset, write
  enable and close, register reads and writes, each checked by its checksum,
  and its factory conversion coefficients with the checksum the firmware
  checks before taking them. Channel A follows
  Relay1 and B Relay2 on the Dual R3; a gain the firmware sets other than the
  board's calibrated one reads wrong, as on the real board. The POWCT's
  ESPHome sets a shunt's gain, 16, on its clamp's input and reads it at a
  shunt's scale, 3.2 times too much, which its config's division by pi nearly
  undoes: it reads 101.9 W of the 100.
- **Not modelled:** a UART's baud rate and frame format (a firmware reading
  the meter at the wrong speed would still get its frames), and a chip's
  timing: the board answers within about 0.1 ms, inside the 6 ms Tasmota gives
  a CSE7761, which a classic ESP32 meets with `patches/0006`.

### The browser forward (`container/bin/browser-forward`)

Tasmota serves every page except `/` only when the `Referer` names its own
address, a guard against other sites driving it. A browser at localhost:8080
names localhost, so a plain port forward got empty replies on every menu. The
forward rewrites `Host`, `Referer` and `Origin` to the device's address, sends
a redirect that names the device back through localhost, and gives each
request its own connection.

## What a classic ESP32 needed

Each fix is general, not per device:

| What you saw | Cause | Fix |
|---|---|---|
| Boot 26 s, Tasmota's settings backup about 70 s, the interrupt watchdog at start-up | QEMU threw away translated code and changed the memory map at every flash operation, done with interrupts off | `patches/0003` and `0004` |
| Interrupt watchdog in `esp_dport_access_stall_other_cpu_start` | a workaround for a silicon bug QEMU lacks, whose other core can be slow to answer | `patch_stock.py` skips it on ESP-IDF 4 |
| `A stack overflow in task colink task` during TLS | the firmware's stack is sized for eWeLink's RSA-1024 CA, not an EC one | an RSA stub CA where the firmware's is RSA |
| `abort()` straight after `Disabling RNG` | external RAM missing | `psram_mb` on the kind |
| `This chip is revision 0` on an ESP32-C3 | emulated eFuses too old | the revision the app's header asks for |
| QEMU killed part way through a download, which looks like the device restarting | its container held under 256 MB | 350 MB for a classic ESP32 |
| Tasmota finds no CSE7761 ("C61: Rx none") | a UART on REF_TICK held what it received 64 times its RX timeout | `patches/0006` |

## Debugging

- **The serial log** is `captures/emulation/state/logs/uart.log` (`state-N`
  with `--instance N`), redacted by `redact.py`, which masks the dump's secrets
  and drops panic memory dumps (RAM as words the masking cannot see into). Put
  any other log through it before reading it.
- **A QEMU of your own build**: `AUTOFLASH_EMU_QEMU=path/to/qemu-system-riscv32
  tools/espressif/emulator/emu.sh ...` runs it in place of the image's without
  a rebuild; for a classic ESP32, give a `qemu-system-xtensa`.
  `qemu-build.Dockerfile` builds the RISC-V one from a fork checkout with the
  patches applied (for the Xtensa one, add `xtensa-softmmu` to its
  `--target-list` and build `qemu-system-xtensa`). As the image's build
  environment alone, it suits building the fork incrementally while working
  on a patch.
- **QEMU's monitor** is at `/run/autoflash-emu/monitor.sock` in the container.
- **gdb** (PlatformIO's `riscv32-esp-elf-gdb`, or `xtensa-esp32-elf-gdb` for a
  classic ESP32) against QEMU's `-s` needs a pseudo-terminal (run it under
  `script -qfc`) and hardware breakpoints (`hbreak`), as the bootloader
  overwrites software breakpoints in IRAM when it loads the app. For the
  bridge's symbols, build its ELF with `pio run -e autoflash-bridge` in
  `archive/bridge`.
- **gdb on a running device, without a restart**: start QEMU's gdb server
  through its monitor, and reach it through the container, whose network is
  not the host's:

  ```sh
  docker exec autoflash-emu-N sh -c 'echo "gdbserver tcp:0.0.0.0:1234" | socat - UNIX-CONNECT:/run/autoflash-emu/monitor.sock'
  script -qfc "riscv32-esp-elf-gdb -q -batch -ex 'set architecture riscv:rv32' \
      -ex 'target remote | docker exec -i autoflash-emu-N socat - TCP:127.0.0.1:1234' -ex '...'" /dev/null
  ```

  `hbreak` with `commands ... printf ... continue` traces a call as it runs (so
  the BASICR4's dispatch was found); `print` can call the firmware's own
  functions, such as its ADC read, and memory reads show its state (the
  overheat flag). `gdbserver none` stops the server. A check run only at
  start-up is over before gdb attaches: read what it left instead.
- **Without a NIC** (`-nic none`) the fork does not map its Wi-Fi device, and
  firmware hangs in Wi-Fi initialisation: a test set-up problem, not a
  firmware one.
