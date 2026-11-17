# Technical details

The [README](../README.md) says how to convert a device, and
[what-happens.md](what-happens.md) explains in plain English what happens to
it. This page is the reference behind both; you do not need it to convert a
device.

## The firmware bundle

The first run prepares the bundle of firmware files an Espressif device is
converted with. It downloads the official Tasmota release files that
[`firmware.json`](../artifacts/firmware.json) pins, from their GitHub release
page, checks each against a pinned SHA-256, and writes the bundle into
`captures/`, which git ignores. It asks nothing: your Wi-Fi details are added
later in the run. Each chip has its own bundle. To rebuild the ESP32-C3's on
its own:

```sh
python3 tools/espressif/safeboot/prepare_imprinted_bundle.py --without-wifi
```

A Beken device needs no bundle: each firmware for it is one published file
([the artifact notes](../artifacts/README.md)).

### What is in it

Partway through the conversion the device needs two Tasmota images that the
ordinary release download lacks:

- **A Tasmota with Berry**, small enough for one of Sonoff's slots, which runs
  while Sonoff's partition map is still in place. For the ESP32-C3 it is
  [`artifacts/tasmota32c3-bluetooth.bin`](../artifacts/tasmota32c3-bluetooth.bin),
  pinned, with its upstream source revision in
  [the artifact notes](../artifacts/README.md). For the classic ESP32 it is
  the release's own `tasmota32-display.bin`: the Bluetooth build keeps so much
  memory for Bluetooth that Berry runs out while holding a bootloader to
  write.
- **The small temporary startup firmware**,
  [`artifacts/autoflash-recovery-safeboot-imprintable.bin`](../artifacts/autoflash-recovery-safeboot-imprintable.bin)
  (`-esp32-imprintable.bin` for the classic ESP32). It holds placeholders,
  not a real network name or password, so the published copy is safe to
  share. It also saves the Wi-Fi where Tasmota reads it, so the official
  Tasmota installed after it joins your network by itself.

Never flash the placeholder image directly. Each run writes your Wi-Fi
details into an in-memory copy, repairs both ESP image integrity fields, reads
the details back from where the firmware will read them, and proves that the
copy differs from the published image in nothing else.

The bundle pins that copy by SHA-256 without storing it, and holds the Berry
Tasmota, the official Tasmota release files and the partition table taken from
them, each pinned by hash so that nothing can be swapped underneath you. It
needs no flash dump: the tool reads and checks the device's partition table
and settings live before anything is written. The published images only save
compiling Tasmota locally: every image and live flash readback still has to
match its pinned provenance.

## Where the one-line start keeps the tool

The README's one line runs `liberate.sh` (macOS, Linux) or `liberate.ps1`
(Windows), taken from the latest release. They unpack the release's
`autoflash.tar.gz` or `.zip` into `~/Firmware-Liberator`
(`%USERPROFILE%\Firmware-Liberator` on Windows) and start the tool there, as
`start.sh` and `start.cmd` do. Arguments pass through:
`sh -c "$(curl -fsSL ...)" sh run --verbose`.

Run again, it updates the folder in place to any newer release, replacing or
removing only the files listed in `.installed-release`: not `captures/`
([below](#what-captures-holds)), `.python/` or a file of your own. Offline,
it starts the version already there. `AUTOFLASH_HOME` chooses
another folder; a git clone in the folder is started as it is, and a folder
holding something else is left alone.

## How your Wi-Fi details are handled

A conversion does not ask for your network name and password, because the
device already has them.

On a Beken device they stay in the store Sonoff's firmware keeps them in:
each new firmware reads them from there, and the tool handles them only inside
the backup.

On an Espressif device they are carried from one firmware to the next. The
installer reads the Wi-Fi the Sonoff firmware saved, joins that network, and
gives the first Tasmota settings with the details in. The temporary startup
firmware that comes later has to join by itself, so the tool reads the
details from the Tasmota then running, which is connected with them (proof
that they work), checks the name against the network it reports being on,
and writes them into the startup firmware. That in turn saves them where
official Tasmota reads them, so the finished device joins by itself.

Only if the details cannot be read or given automatically does the tool ask
for them, once, saying why. With `--wifi` they are asked for
instead, typed twice, since Tasmota never reports its password for checking.

Apart from the Sonoff backup ([below](#your-sonoff-backup)), your Wi-Fi
password is never written to disk, only held in memory for the run. The
temporary startup firmware is built from it in memory and handed to the
migration server through a pipe, never as a file. The device loses the
password at the end of the run, when the official startup firmware replaces
the temporary one.

No other secret is kept outside the backup either
([the LAN key](#how-your-ewelink-account-is-handled) included): the update
links, the device's settings memory and staged firmware are deleted or
reduced to hashes once checked, however the run ends.
`python3 -m tools.autoflash cleanup` deletes any such file left on disk, as
after the computer lost power mid-run, and leaves the backups alone.

### What crosses the network

Not written to disk is not the same as never exposed. On an Espressif device
the password crosses your local network up to three times during a run, in
plain HTTP:

- inside the Sonoff backup, as the installer sends the device's flash to the
  tool (not with `--no-backup`). While it runs, the installer answers anyone
  on your network, but it also takes any firmware it is sent, which could read
  the same memory, so reading it adds no new way in;
- when the tool reads it in the device's configuration backup (skipped if you
  typed the details instead): the same `/dl` download as Configuration →
  Backup Configuration in Tasmota's web UI. The file is only lightly
  obfuscated, each byte XORed with a fixed key by a published method that
  tools such as Tasmota's `decode-config.py` reverse, and the password inside
  is plain text;
- when the temporary startup firmware is uploaded, with the password written
  into it as plain bytes.

On a Beken device it crosses once, inside the backup (not with `--no-backup`,
nor when this computer has the device's backup already): the tool reads the
last 128 KiB of flash through OpenBeken's `GET /api/flash`, in plain HTTP,
and Sonoff's store in it holds the password as plain bytes, as OpenBeken's
Autoflash driver reads it. Tasmota's own web password, if one is set,
also travels unencrypted, from the tool just as from a browser logging in.

That risk is accepted on purpose. Tasmota's web UI has no HTTPS, so every
settings backup taken from a browser exposes the password the same way, and
the password only gets someone onto the network that anyone able to watch
this traffic is on already. Keeping it on the device throughout was
considered (Berry could write it into the startup firmware on the device, or
leave it in flash for that firmware to read), but is not worth it to close
two brief exposures. If traffic on your local network is a concern, separate
it, for example with an IoT VLAN.

## Your Sonoff backup

Before a conversion writes over Sonoff's firmware it keeps a copy, under the
device's MAC address, with the device's other records:

```text
captures/devices/58-cf-79-01-02-03/
    device.json            the device's eWeLink name and id, model, Sonoff version
    sonoff-backup/         the backup
    runs/20261013-031500/  each conversion of it: its state and its log
```

A run starts in `captures/devices/unidentified/runs/` and moves under the MAC
as soon as the device is found, before anything is written to it.

The backup is the original: written once, read-only, never replaced. A device
converted again keeps its first backup, and the warning before PROCEED says so
rather than reading the device again. `python3 -m tools.autoflash list` shows
every backed-up device.

**It holds secrets in plain text:** your Wi-Fi password, as the Sonoff
firmware saved it, and the device's eWeLink keys. Only your user can read the
directory and files, `cleanup` keeps them, and they never belong in an issue
or a discussion. Delete them if you do not want them, or convert with
`--no-backup` to keep none.

### An Espressif device's

The backup step ("Backing up Sonoff firmware", `C3`) comes straight after the
installer is sent, before anything is written over the Sonoff firmware. The
PROCEED typed at the step before, until which nothing reaches the device,
covers it, and its warning says the backup will be taken (or that the device
has one already). It reads the device's whole 4 MB flash
through the installer (the bridge's read-only `GET /flash` and `GET /efuse`,
from bridge 4.4.2), in under a minute. The copy is checked byte for byte
against the SHA-256 the installer works out on the device itself
(`GET /flash-sha256`), and a backup whose hash differs is refused and read
again. It is filed under the MAC in the chip's own eFuses, even if the network
reported another.

The backup holds:

- `flash.bin` - the whole chip: bootloader, partition table, the Sonoff
  firmware, its settings and its eWeLink pairing;
- `efuse.bin` - the chip's eFuse registers, which only the emulator needs;
- `report.json` - what the backup holds, checked when it was taken;
- `README.txt` - how to use it.

Like every stage, the backup shows as one line on screen; what it found (the
partitions, the Sonoff version, where it was kept) is in the run's log, and on
screen with `--verbose`. After two failed reads the run says the conversion
can go on without a backup, though that is not recommended, and offers to try
again, carry on without one, or quit. Quitting leaves the device on the
installer, to carry on from there next time or go back to Sonoff with a power
cycle. Carrying on, as an unattended run does, marks the stage "Skipped", in
green: the conversion is not affected. The test report says whether the
backup completed, failed or was not taken, never where it is or which device
it came from.

It is for two things:

- **Going back to Sonoff.** Going back over the air writes this backup back
  when it is on the computer; without it, the tool fetches Sonoff's firmware
  and uses the identity the device keeps or your eWeLink account
  ([The finished memory layout](#the-finished-memory-layout)). Writing
  `flash.bin` back over USB-serial
  (`esptool.py --chip esp32c3 write_flash 0x0 flash.bin` for an ESP32-C3,
  `--chip esp32` for a classic ESP32) does the same, but means opening the
  device. One of the two app slots holds the installer
  rather than an older Sonoff version; the device boots the other.
- **The emulator.** `tools/espressif/emulator/emu.sh` offers every backed-up
  device by its MAC and runs a copy of the one you choose
  ([emulator.md](emulator.md)).

### A Beken device's

Sonoff's Beken firmware has no way to read a device out, so the backup is
taken after the first conversion, through OpenBeken (step `B4`). It holds:

- `config-0x3e0000.bin` - the last 128 KiB of the device's flash, as read:
  the factory record (MAC, radio calibration), the store Sonoff's firmware
  keeps its Wi-Fi, meter calibration and eWeLink identity in, and the system
  store, none of which a conversion changes;
- `stock-app-<version>.rbl` - Sonoff's firmware, the same for every device of
  the kind (from the update file eWeLink offers the device, else its public
  copy), as the image the bootloader installs to go back - going back sends
  this first;
- `backup.json` - what the backup is: offsets, hashes and when;
- `README.txt` - how to use it.

Going back to Sonoff's firmware needs only the tool: the device fetches
`stock-app-<version>.rbl`, its bootloader installs it, and Sonoff's firmware
finds its own data still in place.

## What captures/ holds

`captures/` is ignored by Git: it is what the tool and its tests keep on your
computer. **Your Sonoff backups are the one thing in it that cannot be made
again**: keep a copy of `captures/devices/` somewhere safe.

| In `captures/` | What it is | Made again by |
|---|---|---|
| `devices/<MAC>/sonoff-backup/`, `device.json` | your device's Sonoff firmware and settings, and its record | nothing: **back it up** |
| `devices/<MAC>/runs/`, `unattended/` | each run's state and log, and recordings of unattended runs | the next run |
| `reports/` | reports of runs that could not be sent yet ([what the tool sends](what-the-tool-sends.md)) | nothing: a later run sends them |
| `safeboot-recovery-migration/`, `safeboot-recovery-migration-esp32/` | the firmware bundle, for each chip | the next run (A5) |
| `settings.json`, `.autoflash-current`, `.safety-locks/` | the eWeLink email offered next time, and the region it signs in to, the run in progress, the locks' state | the next run |
| `esphome-build/`, `firmware-build/`, `openbeken-build/` | build caches, for `update_firmware.py` | its next build |
| `emulation/state*/`, `emulation/beken/state*/` | each emulator's device and computer | the emulator, from the backup |
| `emulation/saved/` | devices kept part way (`--save`), the fault suite's and the public ones | `fault_suite.py prep`, `public_devices.py make` |
| `emulation/suite*/`, `emulation/public-devices/` | the fault suite's results and logs, the public devices' | their next run; what they found is in [testing.md](testing.md) |
| `emulation/legacy/`, `emulation/cache/`, `emulation/stub-cloud/` | earlier releases' trees, the QEMU fork's source, the cloud stub's own CA | the suite, `emu.sh` (a new CA means the saved stub devices are made again) |

`python3 tools/repo/prune_captures.py` lists what can be made again and has
not been used for a while, and removes it with `--delete`. It never removes a
backup.

Releases before 3.0.0 kept each device's records in `captures/plugs/<MAC>/`,
with `plug.json` for today's `device.json`. The first time a newer release
starts, it renames that folder to `captures/devices/` in place, in one step,
copying or rewriting nothing. It will not while both folders exist, or while a
run of the earlier release is still converting a device, and says which
([what to do](troubleshooting.md#records-from-an-earlier-release)). Older
still, runs were kept straight under `captures/`; those that know their
device's MAC are moved under it the next time the tool starts, and the rest
stay where they are and still work.

The maintainer's own backup is sealed in a private package, which the emulator
restores when `captures/` lacks it.

## How your eWeLink account is handled

A run from Sonoff's firmware uses your eWeLink account for two things: to
find the device, by matching the devices it converts in the account against
what answers on your network, and to send the device the upgrade command that
installs the tool's first firmware. Both go straight to eWeLink's own servers
over HTTPS, the upgrade command over an encrypted WebSocket, and nothing about
the account passes through anyone else.

Your eWeLink password is asked once per run and held in memory only: never
written to disk, the run state or the log, and never put on a command line.
The delivery tool, `serve_tasmota_ota.py`, receives it in the
`EWELINK_PASSWORD` environment variable and clears its copy once signed in.

The run signs in once, because each sign-in ends the one before, and uses the
access token eWeLink hands back for everything it asks, holding it in memory
for the run alone and out of the log. The helper tools it starts
(`query_ota.py`, `serve_tasmota_ota.py`) receive the token in the
`EWELINK_SESSION` environment variable, and sign in themselves only if
eWeLink no longer takes it.

The account's email (or phone number, for an account made with one) is kept
in `captures/settings.json` and the run's state file, so that the next run
can offer it first. The run's log writes it as `<email>` (or `<phone>`), and
replaces the device's id, name and hardware address likewise.

The device list eWeLink returns, asked or not, includes each device's LAN key
(`devicekey`): not the device id or an account login, but the secret its LAN
Control mode encrypts local traffic with. The run does not use it, never logs
or records it, and holds it in memory only until its processes exit. It stops
mattering once the device no longer runs Sonoff's firmware.

The account and password can instead be given in advance in a `.env` file
([unattended.md](unattended.md#give-the-answers-in-advance)), which
`AGENTS.md` tells AI coding tools not to read.

A throwaway eWeLink account is fine, and limits what a mistake could expose:
once the device is converted, the account is no longer needed.

## Locks on the destructive steps

Besides PROCEED, the destructive steps are locked by default: the underlying
tools run them on their own only once the `RECOVERY_LOCK` and
`REPARTITION_LOCK` files in `tools/espressif/` are deliberately renamed. A run
lifts each lock itself for the step that needs it, and puts it back.

## The finished memory layout

### An Espressif device on Tasmota

The device is laid out again: official Tasmota on the official partition
layout, as flashing it over USB would give, with one area more at the end:

| Area | What's in it | Where | Size |
|---|---|---|---|
| `nvs` | Settings | `0x9000` | 20 KiB |
| `otadata` | Which firmware to start | `0xE000` | 8 KiB |
| `safeboot` | Recovery firmware for future updates | `0x10000` | 832 KiB |
| `app0` | Tasmota itself | `0xE0000` | 2,880 KiB |
| `spiffs` | File storage | `0x3B0000` | 316 KiB |
| `sonoff` | The device's Sonoff identity (data, subtype `0x40`) | `0x3FF000` | 4 KiB |

That gives firmware 2,880 KiB instead of the Sonoff layout's 1,984 KiB, and
a filesystem the device never had. Tasmota's tables for
a 4 MB ESP32-C3 and a 4 MB classic ESP32 are byte for byte the same.

The table is built at run time from the one in Tasmota's factory image: the
filesystem's entry 4 KiB shorter, the `sonoff` entry added, the checksum made
again, and the result checked against a reviewed hash
(`safeboot_migration.target_table`). A new Tasmota release whose factory table
differs stops the firmware update until the repartition is reviewed again. A
device converted earlier, on Tasmota's own table, keeps working as it is: it
has no `sonoff` area, and going back to Sonoff's firmware without its backup
uses your eWeLink account instead.

The `sonoff` area is one self-describing sector: the magic `SonoffID`, a
version, a length and a CRC-32, then the identity as JSON. The identity is
Sonoff's NVS entries `frame_params`, `data_backups` and, where the device has
them, `productId`, `channel_params` and `PID`, 300 to 550 bytes, with
`frame_params`' Wi-Fi name and password taken out. The tool finds it by that
header, not by the table, so a table that lost the entry (Tasmota's factory
image written over the device) still yields it. Nothing Tasmota does writes
it: its updates, `Reset 1`, `2`, `5` and `6`, and a filesystem formatted or
full were each seen to leave it as it was. The Partition Wizard does not (see
[What actually happens](what-happens.md#at-the-end)).

Leaving Tasmota, the tool writes the identity into the settings the next
layout keeps, as an NVS it builds (`tools/espressif/nvs_image.py`, the same
bytes ESP-IDF's `nvs_partition_gen` makes). On Sonoff's layout the sector is
the last of `ota_1`, past any firmware written there, and is where going back
to Sonoff finds the identity once the startup firmware has wiped the settings
on its way to the interim Tasmota. Its checksum shows whether it is whole,
and only this device's identity is ever written there.

### An Espressif device on ESPHome

ESPHome keeps Sonoff's partition layout and its settings, the device's
identity among them, and the tool writes the `sonoff` sector at the end of
`ota_1` too
([Espressif research](espressif-research.md#a-devices-identity-and-stores)).

### A Beken device

OpenBeken and ESPHome are both built for the stock bootloader's own 4 MB
table, so a conversion writes only the app and the areas Sonoff's firmware
leaves empty. The stock bootloader stays, and applies every later update from
the download slot:

| Area | What's in it | Where | Size |
|---|---|---|---|
| Bootloader | Sonoff's `rt_ota` bootloader, kept | `0x0` | 60 KiB |
| Application | OpenBeken or ESPHome | `0x11000` | 2,368 KiB |
| OTA | The download slot an update is staged in | `0x286000` | 1,384 KiB |
| RF Firmware | The factory record: the MAC and radio calibration, kept | `0x3e0000` | 4 KiB |
| NET info | OpenBeken's settings, or ESPHome's LittleFS | `0x3e1000` | 4 KiB |
| NVS | Sonoff's store: Wi-Fi, meter calibration, eWeLink identity, kept | `0x3e2000` | 44 KiB |
| matter info backup | ESPHome's preferences (FlashDB) | `0x3ee000` | 32 KiB |
| matter info | Sonoff's system store, kept | `0x3f6000` | 32 KiB |

The device keeps its identity, Wi-Fi and calibration where Sonoff's firmware
keeps them, for each first boot to read and for going back to find as they
were, so a Beken device needs no `sonoff` area. The two open firmwares keep
their settings in different areas, so neither disturbs the other.
Where each area came from: [Beken research](beken-research.md#flash-and-layouts)
and [the ESPHome route](beken-research.md#the-esphome-route-libretiny).

## Stock firmware versions

The tool converts every Sonoff firmware known for the S60: 1.1.1 and 1.2.0.
Each other device's page under [devices/](../devices/README.md) names the
version it was tried with.
[Blocking Sonoff updates](router-rules.md#blocking-sonoff-updates) is the
insurance against a later version closing the way in.

## For developers

Start with [CONTRIBUTING.md](../CONTRIBUTING.md); what follows is what it
leaves to this page.

### The code

The tool, the package `tools/autoflash/`, sits on top of smaller,
single-purpose scripts in `tools/`, each of which runs on its own:

- **A run.** `cli.py` takes the command line and chooses the run;
  `context.py` holds a run's context; `runner.py` runs its steps, with their
  retries and the evidence of one cut off; `report.py` writes the report.
- **Under them.** `runs.py` is a run's record on disk and `records.py` each
  device's records, its backup among them; `account.py` the device in its
  eWeLink account (the one part of a run that reaches eWeLink); `join.py` the
  device onto the Wi-Fi and found there; `names.py` what its networks are
  called; `wifi.py` where its Wi-Fi details come from; `unlock.py` a converted
  device's own passwords and keys; `firewall.py` this computer's ports and
  firewall rules; `errors.py` what stops a run; and `handover.py` the go-ahead
  before a converted device's first write and an update sent to any ESPHome.
- **The steps** are the routes, a package per chip family.
  In both, `evidence.py` is what a step cut off left behind and `images.py`
  the firmware a device is given (in `routes/beken/`, Sonoff's included).
  `routes/espressif/` has `installer.py`, `hub.py`, `tasmota.py`, `mover.py`
  and `sonoff.py` by where the device is on its way, `source.py` for where a
  run left one part way, `bundle.py` for the firmware bundle, and
  `sonoff_firmware.py` for Sonoff's firmware where the device has no backup.
  `routes/beken/` has `stock.py` for the push from Sonoff's firmware through
  eWeLink, `onward.py` for each converted firmware taking the next, and
  `arrived.py` for the checks and the backup.
- **What the families share.** `routes/source.py` finds the device and what
  it runs when a run starts, whatever its family. `routes/__init__.py` holds
  the start every route shares (A0, then the firmware chosen, A3; from
  Sonoff's firmware, the device found in eWeLink between them, A1, which says
  its kind) and settles which route a run takes, by the device's kind. A
  family package gives it the same handful of names (its `FIRMWARES`,
  `SOURCES` and `steps_for`, and what the runner needs to know of each step),
  so another chip family is another package, not a change to the runner.

The modules call each other's functions by module name
(`installer.reach_bridge`), never imported bare, so a test patches a function
on the one module that defines it.

`archive/` keeps, though no longer needed, the research tools, the bridge
firmware source and the custom Tasmota build that got the early devices
converted. Their research (the update mechanism, the partition analysis, the
reviewed migration plan) is in
**[archive/docs/reference/](../archive/docs/reference/README.md)**:
[manual-procedure.md](../archive/docs/reference/manual-procedure.md) to do a
conversion by hand, [ota-findings.md](../archive/docs/reference/ota-findings.md)
for how the firmware format was decoded,
[automatic-wifi.md](../archive/docs/reference/automatic-wifi.md) for how the
network switches and Wi-Fi typing were removed and what was ruled out, and
[lessons-learned.md](../archive/docs/reference/lessons-learned.md) for the
pitfalls met along the way.

### Testing a change

`python3 tests/run.py` checks the tool against itself, touching no device
([testing.md](testing.md)). To try a change without converting a real device,
the emulators run whole conversions in Docker against a virtual device that
boots real stock firmware; on a Linux computer with KVM the Espressif one also
runs the tool on Windows beside the device ([emulator.md](emulator.md)).
`run --unattended` runs a conversion with nobody at the keyboard, and
`tools/espressif/emulator/unattended-test.sh` does that against the emulated
device in one command ([unattended.md](unattended.md)).

### Keeping secrets out of Git

`tools/repo/leak_scan.py` stops a commit or a push that would put a secret or a
real identifier into the repository. Set it up once in a clone:

```sh
python3 tools/repo/leak_scan.py install
```

That points Git at the hooks in `.githooks/`. The scan checks what is staged
and the message before each commit, and every commit a push would send before
it goes, so a commit made with `--no-verify` is still caught on its way out.
It finds two kinds of thing, which the script's own notes list in full:

- **Your own, by value**, which only your computer knows: every value in
  `.env`, and in `tools/repo/.env.infra` and the emulator suites' credentials
  (`~/.config/firmware-liberator/emulator.env`) if you keep the project's
  services (its tokens, keys and private addresses), the eWeLink account, and what `captures/devices/` holds of each
  device: its MAC in any form, its eWeLink id and name, and the Sonoff
  settings in its backup that say which device, account or network it is. Add
  anything else of yours (a router's name, a public address) as a line of
  `tools/repo/leak-scan.local`, which Git ignores. A secret with no safe
  shape, such as a device's `device_secret`, `app-apikey` or
  `secureCertSha256`, which look like the repository's own hashes, goes there
  too.
- **Anyone's, by shape**: a MAC address or UUID that could be a real device's,
  a private key or a service's token, disassembly or decompiler output (commit
  the tool, not its dump), and a packet capture, whose frames carry a
  network's Wi-Fi credentials.

The scan says where each finding is and what kind, never the value. Take the
finding out; a made-up example meant to be there, such as an example MAC,
belongs in `tools/repo/leak-scan-allow`. The repository's `leak-scan` workflow
runs the shape checks on each push to `main` and each pull request, and
GitHub's own secret scanning and push protection, on for the repository,
refuse pushes with known kinds of token. In examples, use
`12:34:56:ab:cd:ef`, and addresses that are nobody's.

### The repository's settings

What GitHub is set to do for the repository is written down, with why, in
`tools/repo/github_settings.py`, and set from there: branch protection and the
checks a pull request must pass, squashed merges, secret scanning and the
other security features, immutable releases (GitHub signs an attestation of
each, which `gh release verify` checks), and the `website` environment, from
which only `main` deploys [the website](../website/README.md). As an admin,
`python3 tools/repo/github_settings.py` says what differs from that, and
`... apply` sets it.

### Building the startup firmware yourself

The local-build route for the temporary startup firmware is still there:

```sh
python3 tools/espressif/safeboot/configure_recovery_safeboot.py        # asks for your Wi-Fi, privately
tools/espressif/safeboot/build_recovery_safeboot.sh /tmp/tasmota-src   # builds the image
python3 tools/espressif/safeboot/prepare_recovery_safeboot_migration.py
python3 tools/espressif/safeboot/safeboot_migration_status.py          # says whether the bundle is good
```

It needs a full flash dump of a device for `prepare_safeboot_migration.py`.
The procedure, including which Tasmota commit to check out, is in
[part-2-safeboot-recovery-contingency.md](../archive/docs/reference/part-2-safeboot-recovery-contingency.md).
