# The emulators: converting a virtual device

Two emulators run the whole conversion against an emulated Sonoff device, so a
change can be tried end to end without risking a real one. Each boots a real
flash dump of a stock device, so it runs Sonoff's own firmware, unmodified
apart from the changes its emulator makes. They are development tools, and say
nothing about hardware the dump did not come from, such as another regional
version's power meter.

| | Chips | How it works |
|---|---|---|
| [The Espressif emulator](#the-espressif-emulator) | the ESP32-C3 and the classic ESP32, under QEMU | [tools/espressif/emulator/](../tools/espressif/emulator/README.md) |
| [The Beken emulator](#the-beken-emulator) | the BK7238, under BekenEmulator | [tools/beken/emulator/](../tools/beken/emulator/README.md) |

This page is how to use them; how each works is in its README, beside its
code.

## The Espressif emulator

`tools/espressif/emulator/` runs the ESP32-C3 (the S60) and the classic ESP32
(the POW Elite, POWCT, TH Elite, Dual R3, NSPanel and M5, among others), with
[these changes](../tools/espressif/emulator/README.md#what-is-changed-and-why)
to Sonoff's firmware. A full run has gone from a UK S60TPG on stock 1.2.0,
through every stage, to the official Tasmota on the official layout; how far
each classic ESP32 device gets is in
[espressif-research.md](espressif-research.md#devices).

### What you need (Espressif)

- **Docker,** on Linux, macOS, or Windows through WSL 2. Everything runs in
  containers; nothing is installed on the host.
- **A Sonoff backup of a stock device** ([below](#the-backup-it-runs)).
- **The NVS partition generator** (and `cryptography`, for the cloud stub)
  in the repository's `.venv`, as the workflows pin them:

  ```sh
  .venv/bin/python -m pip install --require-hashes --no-deps -r requirements/ci.txt
  ```

#### The backup it runs

Every conversion keeps a backup of each device in
`captures/devices/<MAC>/sonoff-backup/`; `tools/espressif/flash_readout.py`
takes one over Wi-Fi without converting, leaving the device on stock. The
emulator runs a copy, so the backup is never written.

- **Which device.** `emu.sh` lists the backups by MAC address (on the device's
  label, and in your router and the eWeLink app) and asks which to run when
  there are several; `--device <MAC>` chooses beforehand, and unattended, the
  device run last is used. A dump set up the older way,
  `flash.bin` and `efuse.bin` in `captures/emulation/`, is offered too.
- **Its state between runs.** Each device's emulated flash is kept in
  `captures/emulation/state/`, so each carries on where it was when you switch
  back to it; `--fresh` puts the chosen one back to its backup. Before 3.0.0
  an instance kept its other devices' flashes in `plugs/` there and named its
  chosen device in a file called `plug`; `emu.sh` renames both in place.
- **With no backup at all.** When `captures/devices/` holds none, the
  maintainer's sealed backup is fetched from its private package and filed
  there, and a test copy that `--device` asks for is made from it
  (`copy_device.py`), given the backup's key and a login that can read the
  package. `AUTOFLASH_EMU_NO_RESTORE=1` says not to fetch it.
- **A device you do not own.** `public_dump.py` makes a test copy from a public
  dump or an update file ([below](#classic-esp32-devices)). Run again, it makes
  a copy again where an earlier version of it made that copy otherwise, as the
  firmware may not start on the old one; `--fresh` puts an emulator on the new
  copy.

**The backup is private**, holding the home Wi-Fi password and the device's
eWeLink keys in cleartext. Keep it, and everything the emulator writes, under
`captures/`, which Git ignores. A conversion in the emulator keeps its backup
of the emulated device in the emulator's own copy of the repository, never
beside your real ones.

**The emulated device is a clone of the real one**, with the same eWeLink
device id, keys and MAC. Online together, they fight over one device in the
account, and the conversion's upgrade could reach the real device: switch the
real device off before using `--cloud`.

#### Hardware

| | The emulator alone | With Windows beside it ("Windows from Linux") |
|---|---|---|
| Processor | any 64-bit one Docker runs on | x86-64 with hardware virtualisation (AMD-V/SVM or Intel VT-x) switched on in the firmware, so Linux has `/dev/kvm` |
| Memory | 256 MB for an ESP32-C3, 350 MB for a classic ESP32: the emulator's cap | 3.3 GB at most: Windows 2 GB, QEMU and the disk tools 1 GB, an ESP32-C3's emulator 256 MB. An 8 GB laptop with a desktop open manages it |
| Processors | 2, the emulator's cap | 2 more for Windows (caps, shared with everything else, not set aside) |
| Disk | about 0.5 GB: the image, and the device's state under `captures/emulation/state/` | about 30 GB free while Windows installs; then about 21 GB: the ISO 8.2 GB, the layers 10.8, 0.9 and 0.4 GB, the images 0.8 GB |
| Downloads | the emulator's image, or the QEMU fork's source where the image has to be built, once | Microsoft's ISO (8.2 GB), Windows Terminal (12 MB) and virtio-win's guest tools (32 MB), once; Python (11 MB) and OpenVPN (6 MB) each time a layer that gets them is set up |

Measured on a 6-core laptop processor from 2020 with 8 GB, Windows peaked at
2.1 GB and the layers took 7, 2 and 1 minutes to set up. What an emulator
itself uses under its cap is [with running several](#several-at-once). The
caps [can be changed](#windows-from-linux), and a computer short of memory is
warned before Windows starts.

#### How long it takes

For a computer like that one; a faster one is nearer the low end.

**Once, the first time:**

| Step | Takes |
|---|---|
| Downloading the emulator's image, or building it (QEMU from source) where it is not published | under a minute; 2-3 minutes to build |
| Building the Windows VM's image | about a minute |
| Downloading Microsoft's ISO, 8.2 GB | depends on the connection: about 20 minutes at 50 Mbit/s, 2-3 at 500 Mbit/s |
| Installing Windows (layer `installed`) | 5-10 minutes |
| Setting up the `rig` and `tool` layers | 2-4 minutes together |

**Each run:**

| Step | Takes |
|---|---|
| The emulator starting, until the device is on its Wi-Fi | under 10 seconds |
| Windows from Linux: starting, until the link says Connected | 30-60 seconds |
| A conversion | about 7 minutes on a real device, through all 15 stages; about 5 for an S60 in the emulator (4m 35s for an unattended run from stock) |
| Windows shutting down at the end | about 10 seconds |

**After changing what Windows is set up with**, the layers whose scripts
changed are set up again before the run: about a minute for `tool`, 2-4 for
`rig` (and `tool` above it), or the full install again for `installed`, from
the kept ISO.

`windows-test.sh`, and `emu.sh` while you are in it, keep the computer from
sleeping, as an install or a conversion cut off half way has to start again.
They hold a lock (`systemd-inhibit`) and change no setting, so the lock goes
however they exit. A flat battery cannot be held off, so on a laptop running
on its battery `windows-test.sh` tells you to put it on mains power.

### Using it (Espressif)

On Linux or macOS (for Windows, see [On Windows](#on-windows)):

```sh
tools/espressif/emulator/emu.sh            # device offline: nothing reaches the internet
tools/espressif/emulator/emu.sh --cloud    # device and tool online - asks you to confirm the real device is off
tools/espressif/emulator/emu.sh --cloud --yes  # ... without asking: the real device is off already (for unattended tests)
tools/espressif/emulator/emu.sh --stub-cloud  # a local eWeLink stub serves this instance alone (its own cloud; see docs/cloud-stub.md)
tools/espressif/emulator/emu.sh --device 58:cf:79:01:02:03  # run that backed-up device, without asking which
tools/espressif/emulator/emu.sh --fresh    # put the device back to its Sonoff backup, and forget the previous run and the eWeLink account
tools/espressif/emulator/emu.sh --pcap     # also record the device's Wi-Fi traffic
tools/espressif/emulator/emu.sh --rebuild  # rebuild the image first
tools/espressif/emulator/emu.sh --detach   # start in the background; join: docker exec -it -u ubuntu autoflash-emu tmux attach -t autoflash
tools/espressif/emulator/emu.sh --no-firewall  # without the "computer's" firewall, which is on by default
tools/espressif/emulator/emu.sh --windows  # Windows is the computer (see "On Windows"; started by emu.ps1 and windows-test.sh)
tools/espressif/emulator/emu.sh --instance 2  # a second emulator beside the first (see "Several at once")
tools/espressif/emulator/emu.sh --no-api   # readouts kept unsent, not sent to the API beside it
```

A conversion needs a cloud, as the upgrade to the bridge firmware is sent
through eWeLink. With `--cloud`, the real one, `emu.sh` waits for you to type
`unplugged` (`--yes` takes it as done). `--stub-cloud` is a local stand-in for
this instance alone, so nothing reaches the real eWeLink and any number of
instances convert at once
([cloud-stub.md](cloud-stub.md#the-espressif-emulators-stub)). A device's flash
is set up for one or the other: switching needs `--fresh`, or another instance.

The terminal becomes a tmux session. Window **conversion** is your computer on
the home Wi-Fi: run the tool there exactly as for a real device,

```sh
python -m tools.autoflash run    # or just: run  (options pass through: run --verbose)
```

and window **device** follows the device's serial output, with the dump's
secrets masked (`device status` says how it is); Ctrl-b n and Ctrl-b p switch
windows. When the tool asks you to act on the device or your Wi-Fi:

| On a real device | In the emulator |
|---|---|
| Switch it off and on | `device restart` (or `device off`, `device on`) |
| Restart your router, so the device's Wi-Fi goes | `device router off [SECS]`, `device router on` |
| Power-cycle it quickly, N times | `device cycle N` (7 in a row is Tasmota's settings reset) |
| Your desktop's Wi-Fi menu | `wifi`, `wifi join [NAME]` (the device's own network without NAME), `wifi home` |
| Your home Wi-Fi's name and password | `PICSimLabWifi`, empty password |
| Your computer's password, when sudo asks | `autoflash` |
| The device's web page | http://localhost:8080 |

In the emulator (`AUTOFLASH_EMULATOR_URL`) the tool's final link points at
http://localhost:8080, and its warning that sudo may ask for a password gives
the emulator's (`AUTOFLASH_EMULATOR_SUDO_PASSWORD`); on a real computer it
never knows one.

**Readouts go to the API beside it.** A readout made in the emulator is sent
to the project's API in its own code
([`website/api/local.mjs`](../website/api/local.mjs)), in a container next to
the emulator (`autoflash-emu-api`, `autoflash-emu-N-api` for instance N) that
goes when it stops, so you see both sides: what the tool says, and what the API
was asked and kept, in the state's `api/` (`captures/emulation/state/api/`).
`AUTOFLASH_API_ENOUGH` is its `ENOUGH`, the products it has enough readouts of.
`--no-api` keeps readouts on the computer, unsent. Nothing a run in either
emulator sends reaches the project's own API.

#### Stopping

Leaving the session (exiting every window, or Ctrl-b d) stops the emulator and
keeps the device's flash. One started with `--detach` runs on: join it with
`docker exec -it -u ubuntu autoflash-emu tmux attach -t autoflash` (not
`docker attach`, which shows the session but takes no typing), where Ctrl-b d
leaves it running, and end it with `docker stop autoflash-emu`
(`autoflash-emu-N` for an instance).

#### Several at once

`--instance N` (1-99; `--instance=N` on `unattended-test.sh`) runs another
emulator beside the first, with its own container (`autoflash-emu-N`), state
(`captures/emulation/state-N/`) and browser port (8080+N). A new instance has
no device yet, so name one with `--device MAC` when there is more than one
backup.

- **The instances meet in eWeLink, and nowhere else.** Each is its own small
  network, but every clone has the same device id, the cloud keeps one
  connection per id, and the upgrade at C2 is sent through it, so it can reach
  another instance's device. With the real cloud, run the stages up to C2 one
  instance at a time; after C2 nothing uses the cloud.
- **Run no more at once than your computer has cores**, or runs fail that
  would otherwise pass. Sampled with `docker stats` every 2 seconds through
  whole conversions, an S60's emulator, its computer included, took about 0.6
  of a CPU and about 115 MB, peaking near 150 MB; a classic ESP32's runs two
  cores and peaked at 250 MB.
- **A busy computer's emulated device can answer ARP late**: one sweep of
  the network then misses it. A readout looks for the device by its hardware
  address again until `readout.LOOK_FOR` is up, before asking for its
  address. To try that, delay
  ARP alone to the device's container, on its link's host end (`eth0@ifN`
  inside it is host link N): `tc qdisc add dev VETH root handle 1: prio`,
  `tc qdisc add dev VETH parent 1:3 handle 30: netem delay 3000ms`, and
  `tc filter add dev VETH parent 1:0 protocol arp prio 1 u32 match u32 0 0
  flowid 1:3`. One sweep's 2.5 s then always misses it.
- **Each emulator waits for room before it starts**: 250 MB of free memory
  for an ESP32-C3, 350 MB for a classic ESP32 or the S61 (each about 100 MB
  over the most one was measured to use with its computer;
  `AUTOFLASH_EMULATOR_MIN_FREE_MB` asks for another amount), and a load under
  twice the computer's cores. After ten minutes (`AUTOFLASH_EMULATOR_ROOM_WAIT`,
  in seconds) it starts anyway and says so, and a failure then may be the
  computer's. Where the computer cannot say what is free, as on macOS, it does
  not wait.
- **Each instance's computer has its own copy of the checkout**, holding only
  what git tracks or would: never `captures/`, `.env`, `.venv` or agents'
  worktrees.
- **Its device's state stays between runs.** `tools/repo/prune_captures.py`
  lists what of it, and of the suites' logs and build caches, has gone unused
  for two weeks, and `--delete` removes it, keeping any backup.
- **All share one Docker network**, `autoflash-emu`, with any Windows
  container. `emu.sh` makes it the first time and keeps it
  ([why it has its own](../tools/espressif/emulator/README.md#how-it-fits-together));
  `AUTOFLASH_EMU_NETWORK` names another to use instead.

#### Unattended, for testing

One command starts the emulator, converts the device unattended, prints the
screen the run left, and stops the emulator.
[testing.md](testing.md) says how that tests every route, and every fault in
its way.

```sh
tools/espressif/emulator/unattended-test.sh --fresh    # from stock, as a new user would
tools/espressif/emulator/unattended-test.sh            # from where the last run left the device
tools/espressif/emulator/unattended-test.sh --keep     # leave the emulator running afterwards
tools/espressif/emulator/unattended-test.sh --stub-cloud   # against the local cloud stub: no account, no .env
tools/espressif/emulator/unattended-test.sh --no-lan-control   # as with LAN Control off: no discovery answers, no port 8081
tools/espressif/emulator/unattended-test.sh --save=NAME    # keep the device as the run left it, with its captures
tools/espressif/emulator/unattended-test.sh --from=NAME    # start from a device kept with --save
tools/espressif/emulator/unattended-test.sh -- --new   # anything after -- goes to the run itself
```

`--pcap`, `--rebuild`, `--no-firewall`, `--device=MAC` and `--instance=N` are
`emu.sh`'s; the script's header lists the rest. Without `--stub-cloud` it needs
a `.env` with the eWeLink account the dump's device is in, and starts the
emulator with `--cloud --yes`, without asking, so **the real device must
already be off**. Once the device answers on the emulated Wi-Fi, it runs
`tools/repo/unattended_run.py` in the container's copy of the repository, as
your user, with the emulator's sudo password, answering everything as
[unattended.md](unattended.md) describes.

- **`.env` is handed to that run alone** (`--env-file`, from the container's
  `/tmp`, removed afterwards). Neither the container's nor Windows' copy of
  the checkout holds it, so a run you start yourself asks for the eWeLink
  account and everything else, as a real one does.
- **What `.env` holds of your real network and devices is left out**: the Wi-Fi
  details, and a device's Tasmota or ESPHome password, web login or API
  encryption key. To test with some, set `AUTOFLASH_TASMOTA_PASSWORD`,
  `AUTOFLASH_ESPHOME_OTA_PASSWORD`, `AUTOFLASH_ESPHOME_WEB_USER`,
  `AUTOFLASH_ESPHOME_WEB_PASSWORD` or `AUTOFLASH_ESPHOME_API_KEY` where the
  script runs.
- **On a terminal the run is shown live**; elsewhere, as in a CI log, the
  final screen is printed at the end, colours and all. The recording stays in
  `captures/emulation/state/work/captures/unattended/<time>/`, beside the run's
  own `run.log`.
- **The exit code is the run's**: 0 when the device was converted.

#### Public devices

A device from your Sonoff backup, or kept with `--save`, holds Sonoff's
settings and a copy of the backup. A **public device** holds nothing of
Sonoff's or of your device's, so it can go where your backup must not. Past D5
a device runs none of Sonoff's code (the installer and the interim Tasmota have
taken its app slots, and D5 its bootloader), so public devices test the
firmware a conversion ends on ([updating-firmware.md](updating-firmware.md)).

`tools/espressif/emulator/sanitise_device.py` makes one from a device kept past
D5 on a test copy's made-up MAC (`copy_device.py`), rebuilding the flash from
blank with only what it shows to be public:

- **From the flash:** the bootloader, partition table and app images, each byte
  for byte a published file in `artifacts/`; the startup firmware, the published
  one with the emulator's open network written in; the boot selection; and the
  interim Tasmota's settings, naming only that network. Not Sonoff's settings
  (its eWeLink keys, the Wi-Fi it was set up with, its calibration), the copy
  the repartition keeps, or any leftover.
- **From the eFuses:** the MAC, and what every chip of the kind shares (its
  revision and package, which the bootloader checks, and the flash's type).
  Nothing measured on the one chip: not its calibration, nor its factory ID.
- **From the computer's files:** the run's state and its steps' reports, with
  the account and eWeLink device made placeholders; not the Sonoff backup, the
  logs or the firmware bundle.

It then searches everything it wrote for any 256-byte block of your dump,
every identifying value of Sonoff's settings, the real MAC in any form, and the
eWeLink account and devices on your computer, and writes nothing if it finds
one. `--explain` says which setting a value came from, never the value.

`tools/espressif/emulator/public_devices.py` makes the two the final-firmware
test starts from (after E3 on the way to Tasmota, after D5 on the way to
ESPHome) from the fault suite's devices saved after D1, and runs that test:

```sh
tools/espressif/emulator/fault_suite.py --device=12:34:56:ab:cd:ef --stub-cloud prep   # once, with your backup
python -I tools/espressif/emulator/public_devices.py make --device 12:34:56:ab:cd:ef \
    --stock captures/devices/<your device's MAC>/sonoff-backup
python -I tools/espressif/emulator/public_devices.py test                              # no backup, no cloud
```

They need making again only when an interim firmware, or what a run keeps of
itself, changes. Then:

- `public_devices.py publish --stock <backup>` shows them public once more,
  uploads them as one tarball named by its hash to the repository's
  [`emulator-public-plugs`](https://github.com/adamsthws/firmware-liberator/releases/tag/emulator-public-plugs)
  pre-release, and prints the pin for `PUBLISHED` in `public_devices.py`. The
  release keeps the name it was published with, as does the tarball pinned now,
  `public-plugs-<hash>.tar.xz`; a new one is named `public-devices-<hash>.tar.xz`.
- `public_devices.py fetch` puts the pinned ones in `captures/`, checked: how a
  checkout with no backup, such as the `emulator-check` workflow's GitHub
  Actions runner, tests from them.

`unattended-test.sh --from=public-...` runs one offline, with no cloud, real or
stub, and no `.env`. A device saved from that run is public too, and
`emu.sh --device-dir=DIR` starts one with no backup at all. A public device
cannot go back to Sonoff, having nothing of Sonoff's to go back to.

#### Classic ESP32 devices

A classic ESP32 device runs with the same commands as an S60. `emu.sh` reads
the chip from the bootloader's header in the device's flash (at `0x0` on
an ESP32-C3, `0x1000` on a classic ESP32) and picks the QEMU for it
([which, and what else differs](../tools/espressif/emulator/README.md#how-qemu-is-run-containerbindevice-supervisor)).

The flash is the dump as it is, 4 MB on every device so far. Some stock
firmware stops at start-up without its device's external RAM (PSRAM), which
the kind's `psram_mb` in [kinds.py](../tools/autoflash/kinds.py) gives as
QEMU's `-m` (4 MB on the NSPanel, 2 on the MINIR4). The kind is found by the
device's eWeLink product model: a test copy's own (`device.json`), the one an
earlier run recorded, or `AUTOFLASH_EMU_STUB_PRODUCT_MODEL`.

For a device you do not own, make a test copy from a public dump, or from
eWeLink's update file on another device's flash, and run it by the made-up MAC
printed:

```sh
.venv/bin/python tools/espressif/emulator/public_dump.py sonoff-pow-elite
tools/espressif/emulator/unattended-test.sh --stub-cloud --fresh --device=12:34:56:e3:2d:01
```

[adding-a-device.md](adding-a-device.md) takes a new device through this,
including one that does not boot;
[the emulator's README](../tools/espressif/emulator/README.md#what-a-classic-esp32-needed)
has what running classic ESP32 firmware at all needed.

A device whose status in [`devices/status.json`](../devices/status.json) is
`readout-wanted` or `not-supported` is turned down, in the emulator as on a
real one, unless `AUTOFLASH_NOT_READY_YET=1` is set: `unattended-test.sh`
passes it on, and in a run you start with `emu.sh` you set it in the conversion
window (`AUTOFLASH_NOT_READY_YET=1 run`). Where each such device stops is in
[devices/](../devices/README.md).

### On Windows

To test the tool's Windows code paths (`arp -a`, `netsh` firewall rules behind
the elevation prompt, the Windows firewall itself), Windows can be the computer
on the emulated home Wi-Fi. A wire carries Ethernet frames between the emulator
and a TAP adapter on Windows, so Windows sees the device's MAC, mDNS answers
and connections exactly as on a real network. There are two ways:

- **Windows from Linux**, with nothing to install by hand and a clean Windows
  every run: `tools/espressif/emulator/windows-test.sh`.
- **On a Windows computer**, the emulator in WSL beside it:
  `tools\espressif\emulator\emu.ps1`.

Both list what is missing and get it all after one question and, where
administrator rights are needed, one sudo or User Account Control prompt.
Joining the device's own Wi-Fi from Windows is not emulated either way.

#### Windows from Linux

```sh
tools/espressif/emulator/windows-test.sh --cloud
```

Beside the emulated device, **Windows Server 2025, Server Core** runs in a
container of its own (`tools/espressif/emulator/windows/`): QEMU with KVM, one
layer of virtualisation, nothing nested. It needs Linux with `/dev/kvm` and
Docker; on a Mac, use a [Windows computer or VM](#on-a-windows-computer). If
`/dev/kvm` is missing on a PC, its processor's virtualisation is usually off
in the firmware set-up: "SVM Mode" on AMD, "Intel Virtualization Technology"
(VT-x) on Intel.

Both containers are capped as [Hardware](#hardware) lists, so they cannot take
over the computer: Windows' 2 GB and 2 CPUs change with
`AUTOFLASH_EMU_WINDOWS_RAM=3G` and `AUTOFLASH_EMU_WINDOWS_CPUS=4` (its
container gets at most 1 GB more, for QEMU, without swap), the emulator's with
`AUTOFLASH_EMU_MEMORY=512m` and `AUTOFLASH_EMU_CPUS`.

```sh
tools/espressif/emulator/windows-test.sh --cloud                    # a conversion, in OpenConsole, as on Windows 11
tools/espressif/emulator/windows-test.sh --cloud --classic-console  # ... in the classic console, as on Windows 10
tools/espressif/emulator/windows-test.sh --cloud --first-run        # ... on a Windows that has never seen the tool
tools/espressif/emulator/windows-test.sh --cloud --fresh            # ... with the device back to stock and no earlier run
tools/espressif/emulator/windows-test.sh --rebuild-layer=rig        # set Windows' rig layer (and tool) up again first
tools/espressif/emulator/windows-test.sh --stub-cloud               # the local cloud stub instead of eWeLink: a device a Linux run converted against it
```

`--fresh`, `--pcap`, `--no-firewall`, `--rebuild` and `--device=<MAC>` are
`emu.sh`'s. Which device to run is asked before Windows starts.

The first run fetches Microsoft's **Windows Server 2025 evaluation ISO** (8.2
GB) from Microsoft's Evaluation Center link, with no sign-up, checks its pinned
SHA-256 and keeps it. Windows is set up from it in three **layers**, each a
disk in `~/.cache/autoflash-emu/windows` holding only its changes to the one
below ([how long each takes](#how-long-it-takes)):

| Layer | What it adds | Set up by |
|---|---|---|
| `installed` | Windows Server Core, as Setup leaves it: the user `autoflash`, who logs on by itself, and a hook that runs `start.ps1` at logon | the ISO, `unattend.xml`, `firstlogon.ps1` |
| `rig` | what only the tests need: Remote Desktop, SSH, the clipboard over VNC (virtio-win's guest tools, pinned), no Windows Update or SConfig, and for the link OpenVPN's TAP driver, the "Autoflash emulator" adapter and a Python of its own, from the same `windows-bootstrap.ps1` a Windows computer uses | `rig.ps1` |
| `tool` | what the tool's own first run leaves: its portable Python - set up with progress bars on, as in a person's window, and failed if any shows | `tool.ps1` |

```
 ~/.cache/autoflash-emu/windows/
   server2025-eval.iso ─ Setup ─▶ installed.qcow2      Windows as Setup leaves it
                                    └─ rig.qcow2        + what the tests need
                                        └─ tool.qcow2   + the tool's Python
                                            └─ (a run)  thrown away when Windows shuts down
   captures/      the Windows conversion's captures, kept between runs
   windows-terminal.zip, *.built-from   OpenConsole's release; what each layer was set up from
```

Every run starts from a throwaway layer on `tool`, with your checkout in
`C:\Autoflash\repo`, in about a minute. `--first-run` starts it on `rig`
instead, so `start.cmd` gets its Python itself, as on a new computer. When a
layer's scripts change, the next run sets it and every layer above it up again
first; `--rebuild-layer=installed|rig|tool` does that anyway. A layer is
replaced only once its set-up has finished, so a failed one leaves the layers
as they were. The evaluation lasts 180 days from installation; after that,
`--rebuild-layer=installed` installs it again from the kept ISO.

Windows logs on by itself and opens the link and a PowerShell window for the
conversion, in **OpenConsole** (Windows Terminal's own console, as on Windows
11) or with `--classic-console` in the classic one, as on Windows 10. Only your
computer can reach it:

- **In your browser**, at **http://localhost:6080**, with nothing to install,
  also while Windows installs. Copy and paste through the clipboard box in
  noVNC's side panel, as browsers keep pages from reading the clipboard; what
  you paste stays Windows' clipboard while the page is open.
- **Over Remote Desktop**, `localhost:3389`, user `autoflash`, password
  `Windows-emulator`, with its own clipboard (`xfreerdp /clipboard`).
- **With a VNC viewer** on `localhost:5900`, copying and pasting through
  SPICE's agent in Windows.
- **Over SSH** ([below](#ssh-to-drive-a-run-from-a-terminal)).

The converted device's page is at its own address, **http://192.168.60.50**,
as the conversion's last link gives it: while the run lasts, the emulator
routes your computer's connections, and only those, to the device's network
(and still nothing to the internet without `--cloud`). The route needs no sudo,
and is left out if your computer has a network of its own at 192.168.60.0/24;
http://localhost:8080 works either way.

The terminal you started from is the emulator's, for `device restart` and the
serial log; leaving it shuts Windows down. The conversion's captures are kept
in `~/.cache/autoflash-emu/windows/captures`, so an unfinished one can be
resumed.

##### SSH, to drive a run from a terminal

Windows runs OpenSSH's server (from the `rig` layer), with PowerShell as its
shell, on `127.0.0.1:2222` (`AUTOFLASH_EMU_WINDOWS_SSH_PORT` changes it), for
`autoflash` with a key `windows-test.sh` makes once in
`~/.cache/autoflash-emu/windows/ssh`. `tools/espressif/emulator/windows-ssh.sh`
wraps it:

```sh
tools/espressif/emulator/windows-ssh.sh --wait          # until Windows has logged on and lets the key in
tools/espressif/emulator/windows-ssh.sh                 # PowerShell, in C:\Users\autoflash
tools/espressif/emulator/windows-ssh.sh 'Get-ChildItem C:\Autoflash\repo\captures'  # any PowerShell command
tools/espressif/emulator/windows-ssh.sh log -f          # follow the conversion's latest run.log
tools/espressif/emulator/windows-ssh.sh get C:/Autoflash/repo/captures ./win-captures
tools/espressif/emulator/windows-ssh.sh put tools/autoflash/cli.py C:/Autoflash/repo/tools/
```

The key is let in at logon (`start.ps1`), about a minute after a run starts.
SSH is a session of its own beside the run's console, so read what the
conversion shows there through its `run.log`. A conversion can also run over
SSH (`windows-ssh.sh`, then `cd C:\Autoflash\repo; .\start.cmd`),
in the console SSH gives it, which is neither OpenConsole nor the classic one.

##### What Server Core leaves untested

Server Core has no desktop, which makes it quick, but leaves three things of
a person's Windows untested:

- **The User Account Control prompt.** Server Core has neither the prompt nor
  its service, and an administrator's programs are always elevated, so the
  firewall rule is added without asking. `tests/autoflash/test_windows_uac.py`
  answers the prompt every way instead, and a Windows 11 computer shows the
  real one.
- **Windows Security's notifications.**
- **Windows Terminal's window itself.** Terminal cannot start on Server Core,
  which lacks the XAML it draws with. OpenConsole, from the same release
  (pinned, 12 MB, fetched once into `~/.cache/autoflash-emu/windows`), is the
  same code for what the tool's output depends on (colours, the escape
  sequences that redraw a line or take a block back off the screen, wrapping),
  without Terminal's tabs or clickable links.

Everything the tool does on the network - finding the device, the firewall
rule, serving the firmware - is as on any Windows.

##### Starting over

| To get | Do |
|---|---|
| A clean Windows | nothing: every run starts from a throwaway layer, so no firewall rule, setting or file reaches the next |
| The device back to stock, and no earlier conversion to resume | `--fresh` (the device's flash, the emulator's run, and the Windows run's captures; the eWeLink email is not offered either) |
| A Windows that has never seen the tool | `--first-run` |
| The test rig, or the tool's Python, set up again | `--rebuild-layer=rig` or `--rebuild-layer=tool` |
| Windows installed again | `--rebuild-layer=installed`, from the kept ISO |
| None of it on the computer | `docker rmi autoflash-win autoflash-emu`, and delete `~/.cache/autoflash-emu` |

Everything under `~/.cache/autoflash-emu` belongs to you, not root, though the
containers write it.

#### On a Windows computer

From the repository in PowerShell:

```powershell
tools\espressif\emulator\emu.ps1 -Cloud     # opens the emulator and the link, each in a window
.\start.cmd                       # once the link says Connected - as a Windows user would
```

`emu.ps1` first runs `tools\espressif\emulator\windows-bootstrap.ps1
-LocalEmulator`, which installs what is missing:

- WSL 2 (one restart: run it again after, and it carries on without asking),
  and Ubuntu in it with Docker and the NVS generator;
- Python, if there is no 3.10 or newer (python.org's, for your account);
- OpenVPN's TAP-Windows6 driver (pinned installers, checked by SHA-256);
- the "Autoflash emulator" adapter: 192.168.60.10/24 with no gateway, so
  Windows' internet stays where it is, and the lowest interface metric, so the
  tool's mDNS goes out on it. With no emulator running it shows as disconnected
  and does nothing.

`windows-bootstrap.ps1 -ListPython` shows every Python it finds and which it
would use. `emu.ps1` takes `emu.sh`'s options as `-Cloud`, `-Fresh`, `-Pcap`,
`-Rebuild`, `-NoFirewall` and `-Device <MAC>`; `-Distro` picks the WSL
distribution (by default Ubuntu-24.04).

The emulator window is the usual tmux session. To wire a Windows computer to
an emulator running elsewhere, run
`windows-bootstrap.ps1` without `-LocalEmulator`, then
`windows_link.py --host ADDRESS`.

From a Windows checkout the emulator keeps its state (the device's flash, the
serial log, the in-container run's captures) in
`~/.local/share/autoflash-emu/state` inside WSL
(`\\wsl$\<distribution>\home\<you>\.local\share\autoflash-emu\state` from
Windows), not in `captures/emulation/state/`: WSL reaches the Windows drive
through 9p, where one flash write can take 100 ms, and the bridge then trips
its interrupt watchdog while writing Tasmota. The Windows tool's own captures
stay in `captures/`.

WSL stops its VM, and Docker with it, soon after its last window closes, so
keep one open with `emu.sh --detach`.

```
 Windows: autoflash/cli.py ── "Autoflash emulator" TAP 192.168.60.10
                                   │  windows_link.py ⇄ 127.0.0.1:7860 (length-prefixed frames)
 container autoflash-emu ──────────┼───────────────────────────────────────
   windows-link ⇄ tap1 ─┐          │
                        br0 192.168.60.1 (dnsmasq, ufw) ── tap0 ── QEMU (device .50)
```

### Known gaps (Espressif)

- **Open networks only.** The fork's access points have no WPA2, so the home
  network has no password. Every setup network the conversion makes is open
  from the installer 4.4.3 on, so all can be joined (`wifi join NAME`, and
  `wifi home` to go back); an earlier installer's, with WPA2, cannot be.
- **A power meter, but no buttons or screen.** The meter chip (the S60's and
  the POW Elite's CSE7766, the Dual R3's and the POWCT's CSE7761) measures a
  100 W load on 230 V while the relay is on and none while it is off, so the
  end-of-run check can say the meter confirms the switch, and Tasmota's
  calibration from the chip can be tried
  ([how](../tools/espressif/emulator/README.md#the-devices-board-boardpy)).
  A conversion passing says the firmware installs and starts, and reads a
  meter wired as the kind's template says - not that the template is right for
  the device.
- **No Supla GUI-Generic.** It is written and started, but stops at once
  (`assert failed: __esp_system_init_fn_init_flash`, after `qio_mode: Failed
  to set QIE bit`) and restarts over and over - as it does flashed with the
  builder's own bootloader and layout, so it is the emulated flash chip it
  does not take, not the route. The run to Supla can be tried up to S1, whose
  writes can be checked in the kept flash; S2 then stops, seeing Supla nowhere.
- **One device, one region.** Another model or region needs a dump of its own.
  A test copy made from an update file runs on another device's flash, so it
  tests the firmware, not the device's own layout or factory data.
- **No chip revision.** QEMU has no revision register, so a classic ESP32 says
  revision 0.0 and the installer's pin levels are empty
  ([espressif-research.md](espressif-research.md#what-a-backup-logs)).

## The Beken emulator

`tools/beken/emulator/` runs the conversion against an emulated Sonoff Beken
device: [BekenEmulator](https://github.com/openshwprojects/BekenEmulator) with
this project's patches, as a container image. It boots the public S61s dump,
or a whole flash you give it, and runs Sonoff's firmware onto its Wi-Fi and
eWeLink as a real device would.

### What you need (Beken)

- **Docker.** Each command first pulls the emulator's image, or builds it where
  it is not published.
- **No firmware of your own.** None is in the image or this repository.
  `beken-emu.sh dump`, which the tools run first, fetches the public S61s dump
  from upstream's repository into `captures/emulation/beken/firmwares/`, and
  `beken-emu.sh test` fetches upstream's other devices' dumps there too (about
  280 MB). That directory is mounted at the emulator's `firmwares/`, and the
  checkout at `/work`, as your own user; give a tool there a dump of your own
  by its path under `captures/`.

### Using it (Beken)

The tools in `tools/beken` run in its container:

```sh
tools/beken/emulator/beken-emu.sh python tools/beken/emulator/seed_join.py
tools/beken/emulator/beken-emu.sh python SSID=MyNet SECS=60 tools/beken/emulator/seed_join.py   # its settings
tools/beken/emulator/beken-emu.sh run captures/your-dump.bin --wifi-peer auto
tools/beken/emulator/beken-emu.sh test -k "Sonoff 4MB"    # upstream's self-tests, under the patches
tools/beken/emulator/beken-emu.sh device                     # the S61s dump as a device, on a home Wi-Fi of its own
tools/beken/emulator/beken-emu.sh computer tools/autoflash/cli.py run   # the tool, on that Wi-Fi
tools/beken/emulator/beken-emu.sh power cycle              # the device switched off and on
tools/beken/emulator/beken-emu.sh router on NEW-PASSWORD    # the home Wi-Fi's password changed
tools/beken/emulator/beken-emu.sh --fresh device            # the device as new, on Sonoff's firmware again
tools/beken/emulator/beken-emu.sh ui                       # OpenBeken's page, at http://127.0.0.1:8200/
tools/beken/emulator/beken-emu.sh --rebuild device           # rebuild the image first
tools/beken/emulator/beken-emu.sh --instance 2 device        # a second device beside the first
```

`beken-emu.sh` with no argument lists the rest. Its options are spelt as
`emu.sh`'s: `--fresh`, `--rebuild`, and `--instance N` (1-99) for another device
with its own container, network, state and web page port (8200+N, clear of the
Espressif emulators' 8080+N); give `computer`, `power` and `ui` the same
`--instance N` to reach it.

**The device.** `beken-emu.sh device` runs the dump as a device provisioned on a
home Wi-Fi, on a Docker network of its own (`autoflash-beken-emu`), where
eWeLink is a local stub and nothing reaches the internet
([how](../tools/beken/emulator/README.md#how-it-fits-together);
[the stub](cloud-stub.md#the-beken-emulators-stub)). The command returns once
the device is in its stub's account, a minute or two from a new device, and the
device runs on until `docker stop autoflash-beken-emu` (`autoflash-beken-emu-N`
for instance N). It boots from its flash, kept between runs, so it carries on
across firmware as a real device does. Like the Espressif emulator, it
[waits for room](#several-at-once) before it starts.

**The computer.** `beken-emu.sh computer` is a computer on that Wi-Fi, where
the tool finds the device as on a home network and its eWeLink is the stub. It
works in its own copy of this checkout (the device's `work/`), with captures of
its own; an unattended run takes its eWeLink account, a made-up one the stub
accepts, from `work/captures/`. Its readouts go to the API's own code, as the
[Espressif emulator's](#using-it-espressif) do: in a container beside the device
(`autoflash-beken-emu-api`, `autoflash-beken-emu-N-api`), started with the
first computer and gone with the device, so a second readout finds the first's;
what it was asked and kept is in the state's `api/`. `beken-emu.sh --no-api
computer` keeps them unsent.

**Restarts.** Sonoff's firmware, having fetched, staged and CRC-checked a new
image, restarts a second later and the bootloader installs it, as on a real
device (the firmware's own code says so, [beken-research.md](beken-research.md)).
A device that does not come back is switched off and on with
`beken-emu.sh power cycle`, as the tool then asks; an attended run offers to do
it itself, as the device's power switch is passed to the computer
(`AUTOFLASH_EMULATOR_POWER`).

**Your Wi-Fi, and the device's own network.** `beken-emu.sh router off`
switches the home Wi-Fi's access point off, and `router on` back on;
`router on PASSWORD` gives it a new password, as when you change your
router's, so a device with the old details can no longer join. With the
access point off, the device's frames to it go unanswered, and its firmware
gives it up as a real one does, and looks for it again. A device on
ESPHome that cannot join raises a network of its own, and the computer sees
and joins it as from your desktop's Wi-Fi menu: in `beken-emu.sh computer`'s
shell, `wifi` says what it can see, `wifi join` joins the device's network,
and `wifi home` goes back - which, unlike a real computer, it does not do by
itself when that network goes
([how](../tools/beken/emulator/README.md#the-devices-own-network)). An
attended run can do each as the run asks (`"router"` and `"wifi"` answers,
in `tools/espressif/emulator/attended_suite.py`).

**The device's web page goes through a proxy**: `beken-emu.sh ui` serves
OpenBeken's or ESPHome's, telling which by the name the device gave its router.
Opened straight, a browser's parallel requests and the page's once-a-second
polling fill the slow device's few connections in seconds, and it stops
answering though its firmware still runs
([how the proxy prevents that](../tools/beken/emulator/README.md#the-web-pages-proxy-ui_proxypy)).

**Where its files are.** In `captures/emulation/beken/`, laid out as the
Espressif emulator's `captures/emulation/`: each device's `state/` (`state-N/`)
with its flash, logs and the stub's CA, the fault suite's `saved/` devices and
`suite/` results, the fetched dumps and official files in `firmwares/`, and in
`rebuilt/` the whole flash the emulator check rebuilds for a device with no
public dump. Older checkouts' folders
named after the S61 (`captures/emulation/s61/`, `s61-suite/`,
`beken-firmwares/`) are moved there by `beken-emu.sh` and the fault suite the
first time they run, unless a container is using one.

**Another model.** `AUTOFLASH_BEKEN_EMU_FLASH` names another whole flash under
this checkout for a new device to start from, such as one `rebuild_dump.py`
made for another model
([adding a Beken device](adding-a-device.md#what-the-emulator-does-for-you-beken)).
The emulator check does so for the Sonoff Basic Gen5
(`tools/repo/emulator_check.py sonoff-basic-1gs`), and names the device in
`AUTOFLASH_BEKEN_EMU_FLASH_DEVICE` (its key), which the runs' emulator stats
are counted for ([testing](testing.md#emulator-stats)). The emulated board stays
the S61's whatever the flash; the check holds a device with no meter only to
its relay switching, not to a meter seeing it.

### Known gaps (Beken)

- **No Bluetooth.** The BLE controller's registers are modelled, but there is no
  radio: nothing is sent, and no phone can pair with the device over BLE.
- **Slow.** The real firmware runs far slower than on a chip, hence the web
  page's proxy.
- **A power meter, but no button.** The S61's BL0937 measures a 100 W load on
  230 V while the relay pin is driven high and none while it is low, so the
  end-of-run check can say the meter confirms the switch
  ([how](../tools/beken/emulator/README.md#the-devices-board-boardpy)). OpenBeken
  and ESPHome read it a little high, 244-250 V and 103-105 W, as the firmware's
  clock falls behind in the emulator; Sonoff's firmware reports 230 V and
  100 W to the stub. A conversion passing says the firmware installs, starts
  and reads a meter on the pins its config names - not that those are the
  device's.
- **A Linux computer only.** `beken-emu.sh computer` is a Linux container; the
  Espressif emulator's [Windows](#on-windows) set-up has no Beken counterpart yet.
