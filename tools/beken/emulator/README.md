# The Beken emulator

How the Beken emulator (for the BK7238) is put together and what it changes,
for working on it. How to use it is in
[docs/emulator.md](../../../docs/emulator.md#the-beken-emulator).

## How it fits together

- **The home network** `beken-emu.sh device` makes is a Docker network of its
  own, `autoflash-beken-emu`, apart from the Espressif emulator's.
  The fork's `--net-tap` puts the device's radio on a TAP device, which
  `device-net.sh` bridges with the container's interface; the container is the
  router, `.1`: DHCP (the device at `.50`-`.59`) and DNS (every name the
  router).
- **The device's own network**, when it raises one, is carried to the
  computers on that Docker network in a VXLAN tunnel, Ethernet in UDP: a
  [station on the air](#the-devices-own-network) joins it, and a computer's
  `wifi join` takes it there.
- **eWeLink** is the local stub (`cloud_stub.py`): every connection leaving
  the network reaches it, not the internet.
- **The API** the tool's readouts go to is the project's own code
  (`website/api/local.mjs`) in a container of its own on the device's network,
  `autoflash-beken-emu-api`, started by the first `computer` and removed by a
  watcher once the device's container stops (`tools/repo/emulator-room.sh`), as
  the Espressif emulator's.
- **The device** is `device.py`. It boots from its flash, kept in
  `captures/emulation/beken/state/`, so it carries on across firmware, each
  restart the firmware asks for being a boot from the bootloader, as on a real
  device. Sonoff's firmware is helped online (`cloud_online.StockHooks`) only
  while it is the one that starts.

## The image

Each command first ensures the image labelled with the hash (`image.sh tag`)
of `UPSTREAM`, `patches/`, `requirements.txt` and `beken-emu.Dockerfile`,
pulling it from `ghcr.io/adamsthws/autoflash-beken-emu:<hash>`, which the
`beken-emulator-image` workflow publishes for every version pushed, or
building it.

It is built from upstream BekenEmulator at the commit `UPSTREAM` pins, the one
archive, never changed, on the release
[`emulator-source-beken-d9a3772`](https://github.com/adamsthws/firmware-liberator/releases/tag/emulator-source-beken-d9a3772):
a `git archive` of that commit, which `image.sh source` reproduces byte for
byte. It is downloaded once into `captures/emulation/cache/` and checked
against the SHA-256 `UPSTREAM` pins.

To move to a newer upstream commit, put its hash in `UPSTREAM`. `image.sh
source` then makes the new archive, `bekenemulator-<commit>.tar.xz`, writes its
name and SHA-256 into `UPSTREAM`, and names the release to publish it as,
`emulator-source-bekenemulator-<commit>` (draft first, with the archive
attached, then publish: releases are immutable), and rework any patch that no
longer applies. Today's release keeps its name after the family; the next is
named for the upstream project, as the
[Espressif emulator's](../../espressif/emulator/README.md#the-image) are, and
`image.sh`'s exception for today's goes with that pin.

## What it models

- **The base** is BekenEmulator (`openshwprojects/BekenEmulator`), a Python
  emulator on Unicorn (QEMU's CPU core) running the BK7238's ARM968E-S
  (ARMv5TE) directly. It models the chip's peripherals and a RivieraWaves Wi-Fi
  MAC, and can attach a virtual 802.11 peer and a user-mode IP network
  (`src/wifipeer.py`, `src/netstack.py`): enough for stock firmware to reach a
  cloud TLS handshake as a station.
- **Bluetooth:** the BLE controller's registers are modelled (`src/blecore.py`)
  and host commands complete, but with no radio nothing is transmitted and no
  peer can connect.
- **The SAR ADC** is modelled (`src/emulator.py`, `src/periph.py`): an unset
  channel reads 330 raw (about 1.06 V); `--adc CH:VALUE` or `--adc P23:1.65V`
  on `main.py` sets one. Channels 7 and up (7 is the internal
  temperature sensor) give no samples unless set. `beken-emu.sh` sets none, as
  unlike the ESP32-C3's BASICR4 (`adc_readings` in `kinds.py`), neither Beken
  firmware acts on a reading
  ([Beken research](../../../docs/beken-research.md#the-factory-record-at-0x3e0000-mac-and-radio-calibration)).
- **The power meter**, a BL0937, is on the board, outside the emulator: [`board.py`](#the-devices-board-boardpy). Nothing else on the board
  is modelled: no button, and the relay and LEDs are only pins the firmware
  drives.

`beken-emu.sh run <S61 dump>.bin --wifi-peer auto` runs a dump on its own,
which in the container is:

```sh
python /opt/beken-emulator/src/main.py <S61 dump>.bin --with-boot -chip BK7238 --only-uart --wifi-peer auto
```

Always pass `--only-uart`: without it the emulator logs every MMIO access
(300k+ lines) and runs too slowly to get past init.

### The device's own network

A device that cannot join the home network raises one of its own (ESPHome's
`ap:` fallback), where the tool sends it the details
([`set_up_through_own_network`](../../autoflash/join.py)). It runs here as on
a real network, the firmware's half and the tool's:

- **Two peers on the air** (patch 0006): `device.py` puts the home network's
  access point on the device's radio, and with `--own-tap` a station
  (`role="sta"`, MAC `STATION_MAC`) that joins whatever network the device
  raises, by its beacons, and takes it as gone 3 s after the last
  (`BEACON_LOSS_US`). ESPHome keeps its own network up while it goes on
  trying the home one, both on the one radio.
- **The station's traffic is on `tap1`**, which `device-net.sh` bridges into
  a VXLAN tunnel (`computer/own-network.sh`: its id, multicast group and
  port) over the Docker network, so it reaches the computers there and
  nothing else. Nothing in the device's container has an address on it: the
  device is that network's router, ESPHome's at 192.168.4.1.
- **What is on the air** is written to the state's `air/`: `home`, the home
  network's name while its access point is on; `network`, the device's own
  network's while the station hears it; and `station`, the station's MAC.
  The computer reads it from `/device/air`. Each `wifi join` writes the
  state's `joins`, and the station joins the device's network afresh, as a
  laptop does when the network is chosen: ESPHome raises its network again
  after trying details that did not work, and has forgotten the station by
  then, too soon after for its beacons' pause to be noticed.
- **The computer's Wi-Fi** is `computer/bin/`, first on its PATH: `nmcli`,
  answering the tool's scan from `air/` and remembering a network for a
  minute after it goes, as a real computer's list does; and `wifi`. `wifi
  join` makes the tunnel's end (`own0`) with the station's MAC, takes an
  address from the device by busybox's udhcpc (`computer/udhcpc.script`) and
  stops your own traffic on the home network, but for the tunnel's packets,
  which are yours too; `wifi home` undoes it. The computer starts as root and
  runs as your user with the right to change its own links (`NET_ADMIN`,
  `NET_RAW`), handed over by `computer.sh`.
- **The router switch** is the state's `router`, read as the device runs:
  `beken-emu.sh router off` takes the access point off the air (patch 0006's
  `set_on_air`), `router on PASSWORD` gives it a new password, every device on
  it made to join again, as a router's restart does.
- **What happened on the air** - each peer joining, a handshake refused, a
  network lost - is in the state's `net.log`.

The attended scenario `s61-to-esphome-own-network` runs the whole step: the
password changed as ESPHome starts, the details it has refused, its own
network joined and the new ones sent there. ESPHome raised its network about
40 seconds after it started: two minutes on its own clock, which runs ahead
of the wall's in the emulator while the firmware waits.

## What is changed, and why

### The fork (`patches/*.patch`)

Each patch is one commit, with its reasoning in its header, named
`NNNN-what-it-does.patch` as the Espressif emulator's are and applied in name
order when the image is built. `beken-emu.sh test` runs upstream's self-tests
under them.

- **`0001-wifi-monitor-mode-rx-delivery`** (`src/wifimac.py`): sniffed frames
  reach the RX descriptors in monitor mode even with the scan state machine
  inactive, as the AirKiss path needs; opt-in (`monitor_rx_deliver`), changing
  nothing else.
- **`0002-spi-flash-unique-id`** (`src/spi_flash.py`, new, wired in
  `src/emulator.py`): the [boot fix](../../../docs/beken-research.md#boot).
  Without it the SDK's UID read spins until the watchdog reboots the chip.
- **`0003-bk7238-ota-self-reprogramming`** (`src/emulator.py`): the flash JEDEC
  id and XIP write-through the stock bootloader needs to apply an update to its
  own flash ([below](#the-emulator-performs-the-whole-ota-apply)), plus
  flash-write speedups.
- **`0004-timer-group-fires-each-period`** (`src/emulator.py`,
  `tests/self_test.py`): Timer 0-2 of the 26 MHz group fire at each channel's
  programmed period, as LibreTiny's FreeRTOS tick needs, leaving OpenBeken's
  watchdog feed and the stock dump unchanged. Only on the BK7238: on the
  BK7231T and U the same addresses are the old PWM block's, whose settings,
  read as timer channels, raised an interrupt no handler acknowledged - 13 of
  upstream's self-tests failed under it and the run took seven times as long
  (found 13 November 2026). Every one passes now.
- **`0005-wifi-peer-bridged-to-host-tap`** (`src/tapnet.py`, new,
  `src/emulator.py`, `src/main.py`, `tests/tapnet.py`): `--net-tap IFACE`
  bridges the Wi-Fi peer to a TAP device instead of the built-in network, which
  is itself the device's router, reachable only through port forwards. The
  device is then a station on the host's network, addressed by its DHCP server
  and reachable by every protocol: how it gets [its own Docker
  network](#how-it-fits-together). The built-in network's options are refused
  beside it.
- **`0006-wifi-several-peers-at-once`** (`src/emulator.py`,
  `src/wifipeer.py`, `tests/wifi_peers.py`): `attach_wifi_peer` called again
  puts another peer on the air, each hearing every frame the device sends:
  the home network's access point and a station on the device's own network
  at once. A station takes only its own network's frames, by BSSID, and
  takes a network silent for 3 s as gone; `set_on_air(False)` takes a peer
  off the air, as a router switched off; `set_joining(False)` has a station
  hear a network without joining it, as a computer not on it, so the device
  has a client on its own network only while the computer has joined it. It
  could go upstream.
- **`0007-wifi-unheard-frames-go-unacknowledged`** (`src/wifimac.py`,
  `src/emulator.py`, `src/wifipeer.py`, `tests/wifi_peers.py`): a unicast
  frame no peer on the air has the receiver's address of completes
  unacknowledged (`RETRY_LIMIT_REACHED`), as the hardware reports it. The
  firmware counts beacons lost since its last acknowledged frame, so with
  every frame acknowledged it never saw its access point go: now it does,
  while the router is still off. It could go upstream, with one of upstream's
  self-tests changed: the firmware's timing moved a little, so in "BK7238
  Tuya GBDQA protection switch" two of its tasks' lines now print into each
  other, and its `initial BLE...` comes out cut ("data is NULLdata al
  BLE..."), though the device goes on as before, to its MQTT exchange; the
  other 162 pass.

To change the fork, extract the archive (`captures/emulation/cache/`), apply
the patches in order, commit the change, and `git format-patch` it into
`patches/`. `image.sh ensure autoflash-beken-emu --rebuild` builds the image
from the new set, under its new hash; the workflow publishes it once pushed.

### The device's board (`board.py`)

The S61's meter is a chip beside the BK7238, so `board.py`, the counterpart of
the [Espressif emulator's](../../espressif/emulator/README.md#the-devices-board-boardpy),
is that board. BekenEmulator's own BL0937 (`set_energy_meter`) toggles the
pins on its 2 ms tick, 250 Hz at most where the voltage needs 1.8 kHz, and
follows no relay, so `device.py` leaves it unset and attaches `board.py`
instead, from the checkout's copy in the container (`/work`) as `device.py`
itself is run: changing it needs no new image.

- **The wiring is the S61's** (`WIRING`, held by
  `tests/beken/emulator/test_board.py` to `devices/sonoff-s61/`'s
  `drv_autoflash_sonoff_s61.c` and `esphome.yaml`): relay P26, BL0937 CF P8,
  CF1 P6, SEL P24.
- **The load.** 100 W, resistive, on 230 V mains, measured while the relay's
  GPIO is driven high (its output driver on, bit 3 of the config word clear,
  and bit 1 set), nothing while it is off. The voltage, measured on the supply
  side, is there either way.
- **The BL0937** gives pulses: CF at a rate that is the power, CF1 at one that
  is the voltage while SEL is driven high and the current while it is low (an
  HLW8012's sense is the other way round). The rates come from the factory
  calibration in the public dump's store, as the stock firmware, OpenBeken's
  import and ESPHome's config each apply it
  ([devices/sonoff-s61/research.md](../../../devices/sonoff-s61/research.md#energy-meter-calibration-the-stock-formula-and-the-openbeken-mapping)):
  75 Hz on CF, 1794 Hz on CF1 for the voltage and 39 Hz for the current.
  OpenBeken and ESPHome count them by the GPIO interrupt, so the board drives
  those pins through the emulator's own GPIO model between its chunks of
  instructions (`_pacing`, which then raises the interrupt), and the idle loop
  is skipped only while no pulse is due, as a ten-chunk skip would lose one.
- **Sonoff's firmware times them instead**, with the chip's PWM block: CF1
  (P6) on PWM channel 0 and CF (P8) on channel 2 capture rising edges (mode 4,
  pwm_new's `PWM_CAP_POS_MODE`) on the 26 MHz clock, and the PWM interrupt
  hands the driver each period's count
  ([how it reads them](../../../devices/sonoff-s61/research.md#how-sonoffs-firmware-reads-the-meter)).
  The emulator models that block's registers but drove nothing into a
  capture, so the firmware reported 0 V, 0 A and 0 W. Where
  a channel's control word (`0x802B00` + 0x40 a group) has it capturing on the
  meter's pin, the board writes the count it latches (the clock, over the
  group's divider, in one period at the output's rate, half of one in edge
  mode) into the channel's `RD_DATA`, and raises its interrupt status
  (`pwmn_stat`) where enabled; a pulse waits for that status to be cleared, as
  for INTSTA below. The count is the period at the output's rate, not the time
  since the last pulse, so an edge a chunk late does not change it.
- **A cycle is one short low pulse**, the pin falling and rising again between
  two chunks: one interrupt, whichever edge the firmware waits for, the falling
  one (OpenBeken) or each in turn as LibreTiny's change interrupt switches
  (ESPHome, which counts a pulse when it reads the pin high in its
  interrupt). A square wave's two edges a cycle were more interrupts than the
  emulated CPU takes at 1.8 kHz: ESPHome read 160 V.
- **A pulse waits for the last to be taken**: one is given only once the
  firmware has cleared the interrupt the last latched (INTSTA), and cycles owed,
  up to a quarter of a second's worth, follow as soon as it has. A chip takes
  an edge within microseconds, but in the emulator a firmware's stretches with
  interrupts off last far longer in the device's time, and free-running at
  1.8 kHz a quarter of the voltage's edges were lost: OpenBeken read 176 V of
  230.
- **What the firmwares read**, in each way of `fault_suite.py check` (the
  end-of-run check, with the cloud stub): OpenBeken 248-250 V and 103-105 W
  with the relay on, the same voltage and 0 W off; ESPHome 244 V with 103 W on
  and 0 W off. Both read a few percent high because the firmware's clock
  falls behind the device's in the emulator: OpenBeken's uptime counted 67 s
  in 70 s of the device's time (seen by printing the device's time beside its
  log). The voltage being further out than the power is inferred to come from
  its read windows losing more, each with 1.8 kHz of interrupts. Sonoff's
  firmware, timing the periods by the chip's clock, not its own, reports the
  load as it is to the stub: 230.22 V, 0.43 A and
  100.00 W with the relay on (switched through the stub's eWeLink, and asked
  for readings as the app asks, `uiActive`), 230.22 V and nothing off.
- **Not modelled:** the chip's pulse shape and width (only the rate counts),
  or a pulse's timing within a 0.2 ms chunk.

### The web page's proxy (`ui_proxy.py`)

`beken-emu.sh ui` puts a proxy in front of the device's web page (OpenBeken's or
ESPHome's). Each request leaves a connection in TIME_WAIT on the device, lwIP
has room for only a few, and on the slow emulator's clock they clear late,
so a browser's parallel requests and the page's once-a-second polling fill
them in seconds and the device stops answering, though its firmware still
runs. The proxy lets one request at a time reach the device, at most one every
0.8 s (`MIN_GAP`), answers the page's polling from a 3-second cache
(`CACHE_TTL`), and passes every action on. ESPHome's page keeps its
live values coming on one open connection (`/events`), passed straight
through for one browser tab at a time ([more](ui_proxy.py)).

## Booting the converted firmware in the emulator

`OpenBK7238_QIO_<ver>.bin`, OpenBeken's full flash, comes up in the emulator
to its application layer (`OpenBK7238, version 1.18.319`, config load, MQTT
registration and the config SoftAP), and so, end to end, does the conversion's
result: OpenBeken's app (from the QIO, CRC-striped) spliced over the stock app
partition, with the **stock bootloader** kept:

```
RT-Thread OTA package(V0.2.8-beken-...) init success   ← the stock S61 bootloader
go os_addr(0x10000)                                     ← jumps to the app
SDK Rev: 3.0.78 / OSK Rev: F-3.0.51                     ← OpenBeken's build (stock is 3.0.70)
OpenBK7238, version 1.18.319
Main_Init_After_Delay done
softap ssid=OpenBK7238_<id> channel=1 cipher=OPEN       ← OpenBeken's config AP is up
```

The `<id>`, from the device identity in the dump, is redacted here like every
other per-device value.

### The emulator performs the whole OTA apply

The staged OpenBeken RBL in the download partition is erased into place,
decompressed, programmed and checked by `part_fw_hash_verify`, then the
bootloader jumps to it and OpenBeken boots. For that the emulator models two
pieces of flash hardware the bootloader relies on only when reprogramming its
own flash (patch `0003`):

- **The SPI flash JEDEC id (`REG_FLASH` RDID result at `0x803010`).** The
  bootloader derives the flash size from it (`2 << ((id & 0xff) - 1)`).
  Unmodelled, it read 0, so the size was 0: *every* bootloader write failed
  its `(addr + len <= size)` bounds check and was silently skipped, and the
  apply logged success having written nothing. It now returns an id whose
  capacity byte matches the image size, with a maker and type in the
  bootloader's chip table.
- **XIP write-through.** The memory-mapped flash image is loaded once at
  setup, where hardware reads it live through the controller's 32+2 CRC
  framing, so each program and erase is mirrored into the XIP map, de-striped;
  otherwise the hash-verify and the jump read stale bytes.

Both are behind an emulator flag (`flash_writethrough`, off by default), as
modelling real flash writes is correct but slows flash-writing firmware. With
it on, the apply completes and OpenBeken boots as above; the written app
de-stripes byte for byte to the OpenBeken image, its FNV equal to the RBL's
`raw hash`.
