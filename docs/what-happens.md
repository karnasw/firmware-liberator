# What actually happens to your device

For anyone who wants to understand what they are about to do, without knowing
anything about embedded firmware. The tool works without it, but the one
irreversible step is easier to agree to once you know why it is irreversible.

## The problem

A Sonoff device arrives running Sonoff's own firmware, which talks to Sonoff's
servers. Replacing that firmware normally means opening the case and wiring an
adapter to the chip inside. That is fiddly, the case is often welded shut, and
on a mains-powered device a mistake is dangerous.

There is another way in. The device already knows how to replace its own
firmware, because that is how Sonoff ships updates, and it does so when its
owner's account tells it to. You are the owner.

## Getting in

When Sonoff ships an update, its servers send the device an *upgrade command*
naming the address to download the new firmware from and the fingerprint - a
SHA-256 hash - that the download must match. The device fetches that address,
checks the hash, and installs what it gets.

The command only has to come from the account that owns the device, which is
your eWeLink account, and neither the address nor the hash is tied to Sonoff.
So the tool, signed in as you, sends the device an upgrade command of its own,
naming *your computer* as the address and carrying the hash of the tool's
firmware. The device downloads that firmware and installs it exactly as it
would a genuine update. Nothing is faked or bypassed: the device makes every
check it normally makes, and has no reason to care that the address is your
computer or that the firmware is not Sonoff's.

Because the address and hash in the tool's command decide what is installed,
**no router rule is involved**, whether or not Sonoff has an update waiting for
the device.

> **A note on history.** Earlier versions asked for a router rule whenever
> Sonoff was offering the device an update, only to stop the device also
> fetching Sonoff's update in the same short window.
> [router-rules.md](router-rules.md) says why it was dropped, and has a simpler
> rule that stops a device on Sonoff's firmware installing Sonoff updates.

## Why it takes several stages

You cannot replace everything at once, because the device is running from the
memory you want to change. How many stages that takes depends on the chip.

**An Espressif device** (one with an ESP32-C3 or an ESP32, such as the S60)
has memory like a shelf with two large slots for firmware. It runs from one
and can write to the other. So:

1. Sonoff's firmware is in the first slot. The tool puts a small installer of
   its own in the second, and restarts into it.
2. The installer puts a full Tasmota into the first slot.
3. Tasmota copies itself into the second slot and restarts, so the whole first
   half of memory is free to be rebuilt.

Every stage runs from somewhere not about to be overwritten, which is why
there are more steps than seem needed. From there Tasmota
either rebuilds the memory for official Tasmota, or writes ESPHome into the
free half.

**A Beken device** (one with a BK7238, such as the S61) needs fewer. The
running firmware saves the new one in a download area, and on the next restart
the bootloader copies it over the old. Every change of firmware is one such
move.

## The step that cannot be undone

Almost every step can be interrupted and picked up again. On an Espressif
device two short writes cannot, because each replaces something the device has
only one copy of. A Beken device has neither: its bootloader and its layout
stay as Sonoff made them.

**The layout table**, a few thousand bytes at the start of the device's
memory, lists where everything lives. The bootloader reads it at every
power-on, and without it the device cannot find its firmware and does not
start. Sonoff's layout has two firmware slots and no file storage; official
Tasmota's has a small recovery firmware, one much larger area for Tasmota, and
space for files. Getting there means erasing the table and writing the new
one, with no spare for the bootloader to fall back on, so for that moment the
device has no valid table. A conversion to ESPHome keeps Sonoff's layout and
never does this.

**The bootloader**, the small program that starts the firmware, is replaced
once on every Espressif device ([step 9](#an-espressif-device) says why).

Each of those writes takes under a second. If the power fails during one, the
device can no longer be reached over Wi-Fi and has to be opened to repair.
Everything around them makes them brief and certain: the new firmware is in
place and verified before the table is touched, the replacement table is
checked byte for byte in memory first, and the device refuses the whole
operation unless it is running from the far end of memory, well away from
everything being changed.

## How your Wi-Fi carries across

Your device keeps the Wi-Fi details it was given in the eWeLink app, so a
conversion does not ask you for them
([how they are handled](technical-details.md#how-your-wi-fi-details-are-handled)).
On an Espressif device each firmware hands them to the next, as the steps
below say, down to official Tasmota; if any of that fails, the tool asks you to
join the device's own setup network instead. On a Beken device they stay in
the store Sonoff's firmware keeps them in, which no conversion writes to, and
each new firmware reads them from there.

Each new firmware has to join your Wi-Fi again, which a weak signal makes
less certain. So the tool reads the signal as early as the device can report
it, and again later ([on a converted device](#the-wi-fi-signal)). Below
-75 dBm it suggests moving the device nearer your router and asks, once in a
run, whether to carry on; the run's report gives the lowest reading.

## The run, step by step

This is what `python3 -m tools.autoflash run` does, in order, for a device still
on Sonoff's firmware ([a device already converted](#changing-firmware-later)
differs). The first three steps are the same on every device; then
[an Espressif device](#an-espressif-device) and the shorter run of
[a Beken device](#a-beken-device) differ.

Each step has a short ID, which the tool shows as it runs and keeps in the
run's record. Side by side, numbered as in the lists below:

| | An Espressif device | A Beken device |
|---|---|---|
| 1. What has to be in place | `A0` | `A0` |
| 2. Find the device | `A1` | `A1` |
| 3. Which firmware | `A3` | `A3` |
| 4. | Download the firmware: `A5` | Ask eWeLink what it runs: `B1` |
| 5. | Ask the device what it runs: `A2` | Send the firmware, the point of no return: `B2` |
| 6. | The installer, the point of no return: `C2` | OpenBeken started: `K1`; on the way to ESPHome, the interim firmware: `J1` |
| 7. | Back up the Sonoff firmware: `C3` | Back up the device's own data: `B4` |
| 8. | A first Tasmota: `D1`, `D4` | ESPHome fetched: `J2` |
| 9. | The bootloader `D5`, the Sonoff identity `K1`, the new layout `E1`, `E2`, `E3`; for ESPHome, ESPHome written: `I1` | ESPHome started: `K2` |
| 10. | Official Tasmota: `F2`, `F4`, `G1`; for ESPHome, ESPHome started: `I2` | |
| At the end | Every byte checked: `H1` | |

An ID names a step within its family, so the same ID need not be the same
step in the other family, as `K1` shows.

### The start, on every device

1. **List what has to be in place first** - the eWeLink app and account, the
   device added in it - and wait for a key press.
2. **Find the device on your network.** The tool signs in to your eWeLink
   account, asking once for the whole run for your email (or phone number, for
   an account made with one) and password. It looks on your network for the
   devices in that account it can convert, by the hardware address your
   account gives each, so no other device can be mistaken for yours. Sonoff's
   firmware need answer nothing more, so the app's LAN Control setting does
   not matter. The tool carries on by itself if it finds one, and asks which
   if it finds several. A device on your network while eWeLink still shows it
   offline (just switched on) gets up to a minute to reach eWeLink before the
   tool asks anything. If yours is not found, the tool says why: offline in
   eWeLink, or on a different network.
3. **Ask which firmware you would like**: Tasmota or ESPHome for an Espressif
   device, OpenBeken or ESPHome for a Beken one. The choices depend on which
   device eWeLink says yours is (`--firmware` answers in advance).

### An Espressif device

Steps 4-8, and the start of step 9, are the same whichever firmware you
choose.

4. **Download the firmware it needs**, on the first run only, and check every
   file against a known fingerprint.
5. **Ask eWeLink what firmware the device has** and where it downloads
   updates from. The tool stops here, before anything is written, unless the
   device is a kind it converts. It also reads the Wi-Fi signal the device
   last reported to eWeLink: a weak device is best moved now, while nothing
   has been written.
6. **Replace the Sonoff firmware** with the tool's small installer. *This is
   the point of no return,* and the one place the tool asks you to confirm.
   After a short warning, which also says that the Sonoff firmware is backed
   up first or already was, you type PROCEED. That covers the whole
   conversion, including the backup in step 7 and the rewrite in step 9, and
   nothing is sent to the device before it. If your computer's firewall is on,
   the tool first adds a temporary rule for the device.
7. **Back up the Sonoff firmware.** The installer, which has joined your Wi-Fi
   by itself, hands over a copy of the device's whole memory, checked byte for
   byte and kept under its MAC address in `captures/devices/`
   ([Your Sonoff backup](technical-details.md#your-sonoff-backup)). A device
   backed up before is not read again; `--no-backup` skips the step.
8. **Install a first Tasmota through the installer**, and give it your Wi-Fi
   details so it joins too. If the installer could not join, or Tasmota does
   not appear, the tool asks you to join the device's own network, names it,
   does the rest itself, and tells you when to switch back.
9. **Update the bootloader** to ESPHome's, which starts Tasmota, ESPHome and
   Sonoff's firmware alike and rolls back an update that fails to start. There
   is only one, so it is fetched and checked whole first, then written in
   under a second and read back; if it does not read back right, the old one
   is put back.

   Then **keep the device's Sonoff identity**: its eWeLink id and key, model
   and kind - a few hundred bytes of Sonoff's settings, never your Wi-Fi. It
   goes in the last 4 KB of the device's memory, taken from its backup on your
   computer, or from the device's own settings if there is none. With it the
   device can go back to Sonoff's firmware later even if your computer no
   longer has the backup ([Changing firmware later](#changing-firmware-later)).

   Then, for Tasmota, **rebuild the device's memory layout** to match
   official Tasmota's, except that the filesystem ends 4 KB early, leaving
   that last 4 KB to the identity. First the tool copies the device's
   settings, and the note that says which half to start, beyond the rebuild's
   reach and points the device at the copy, so a power cut leaves it starting
   the same Tasmota, settings and all. The tool then puts the settings back
   and tries again by itself, up to three times in all, after a power cut, a
   read-back that differed, or the device changing since its checks. Only
   the moments the layout table itself is written - a few milliseconds, twice
   each time - would leave a device that has to be opened.
10. **Install official Tasmota**, which joins your Wi-Fi by itself, apply the
    device's template and settings, and check the result. Only if it does not
    appear on your network are you asked to join its setup network. On an S60
    the tool then **calibrates the meter** as Sonoff's firmware did: by the
    factor the device's board calls for, read from three of its pins, and by
    its own factory calibration, from its Sonoff backup.

The tool then checks the finished device against the official Tasmota release
([At the end](#at-the-end)) and removes the temporary firewall rule, saying
first why the system will ask for administrator rights again.

For **ESPHome**, step 9 starts the same way, with the bootloader and the
identity. ESPHome keeps Sonoff's settings, identity and all, but they are lost
when you later move the device to another firmware; the copy in the last 4 KB
stays. Then:

9. **Install ESPHome** into the half of memory Tasmota is not running from:
   this project's ESPHome for your device, with your Wi-Fi written in, in your
   computer's memory only. Every block is read back and compared byte for
   byte. Only then does the device clear the note that says which half to
   start, and restart into ESPHome; until then it still starts Tasmota. The
   memory layout is not changed.
10. **Check ESPHome is running** and has joined your Wi-Fi. If it cannot, it
    puts up its own network, named like `esphome-abcdef`, where its page asks
    for your Wi-Fi. On an S60 the tool then gives ESPHome the device's factory
    calibration from its Sonoff backup, which ESPHome uses where the device's
    own memory has lost it (on a device converted through Tasmota first).

The tool then switches the relay off and on through ESPHome to check it, and
gives you the device's ESPHome page.

Either way, the tool then asks what next: convert another device or read one
out, looking for it afresh with your eWeLink sign-in kept, or `q` to end. A
readout asks the same.

### A Beken device

Everything up to the new firmware goes through eWeLink
([the route](../tools/autoflash/routes/beken/)). Steps 4-7 are the same
whichever firmware you choose.

4. **Ask eWeLink what the device runs**: its model and firmware must be those
   of a kind the tool converts. The partition table in Sonoff's official
   update file is checked too, and the run stops before anything is written
   unless it is the one the tool's firmware is built for. That file is the
   one eWeLink offers your device, or, if eWeLink offers none that passes the
   checks, a public copy the tool knows by its hash.
5. **Send the new firmware through eWeLink**, wrapped as Sonoff's updates are,
   from your computer, after a temporary firewall rule if your firewall is on.
   *This is the point of no return,* and the one place the tool asks you to
   confirm: you type PROCEED, and nothing reaches the device before that.
   Sonoff's firmware saves the download, and on the next restart the
   bootloader installs it in place of Sonoff's. A run to ESPHome sends
   OpenBeken first, as the *interim firmware*, with a marker by which it knows
   itself as that.
6. **Check OpenBeken has started** and joined your Wi-Fi by itself. On its
   first start the tool's driver in it reads your Wi-Fi and the meter's factory
   calibration from Sonoff's own store, and applies the device's wiring
   ([the driver](../tools/beken/openbeken/README.md)). The tool asks the
   driver whether the calibration is the one in Sonoff's store, and the report
   says which.
7. **Back up the device's own data** through OpenBeken: its factory record,
   Sonoff's stores and the system store (`0x3e0000`-`0x400000`), checked
   against the MAC in the factory record and kept under it in
   `captures/devices/` ([what it holds](technical-details.md#a-beken-devices)).
   Sonoff's firmware goes beside it, since step 5 wrote over the device's own:
   from the update file eWeLink offers your device, or else the public copy
   of it the tool knows by its hash. A device backed up before is not read again;
   `--no-backup` skips the step.

A run to OpenBeken ends there. For **ESPHome**:

8. **Have the interim firmware fetch ESPHome**, which it saves in the same
   download area. Just before the restart that installs ESPHome, it erases its
   own settings, so it leaves nothing of itself.
9. **Check ESPHome is running** and has joined your Wi-Fi, which it reads, with
   the calibration, from Sonoff's store every time it starts. If it cannot
   join, it puts up its own network, as on an Espressif device, and the tool
   gives it your Wi-Fi through that.

A device that has not come back on its new firmware, whichever it is, is most
often one that needs switching off and on. The tool asks you to, then looks
for it again, and the report says whether you had to.

## At the end

On a device with a relay, every run that converts it, on either chip family,
ends by switching the relay to the other state and back (the first relay, on
a device with several), through the firmware now on it: Tasmota, OpenBeken or
ESPHome, or eWeLink for Sonoff's own. On a device with a power meter, the meter
is read after each switch. Whatever is plugged in is switched for a few
seconds, and the device ends as it started.

On Tasmota, ESPHome or OpenBeken the tool then asks the device which release it
runs, and checks it is the one this version installs, as this project built it.
On a device with sensors, such as a TH Elite with a probe plugged in, it reads
them and checks each reading is one the sensor can give. A device with no
probe plugged in fails nothing. The report's Checks line says whether every
check that ran passed. The screen shows the relay's answers, and the others
only if they fail; the run's log has them all.

A Beken device's bootloader checks each new firmware's checksum before it
installs it, and the tool reads nothing back. On an Espressif device converted
to Tasmota, the tool also reads back four parts of the device's memory,
calculates a fingerprint of each, and compares it with the file that part came
from:

- the bootloader
- the memory layout table
- the recovery firmware
- Tasmota itself

If all four match, the device's Tasmota is byte-for-byte identical to that of
a device flashed over USB with the official release - not equivalent,
identical - with ESPHome's bootloader starting it. The one difference is in
the layout table, which has the area for the Sonoff identity after a
filesystem 4 KB shorter.

From then on it is an ordinary Tasmota device, updated through its own web
page, with normal integrations and nothing touching Sonoff's servers.
Tasmota's updates, its `Reset` commands and its filesystem leave the identity
alone. Two things do not:

- **Tasmota's Partition Wizard** offers to grow the filesystem by those 4 KB
  ("Resize FS to max"), which removes the identity's area from the table. The
  identity is still there at first, but the filesystem spreads its writes over
  all of its space, however empty it is: in a test, saving files of about 80 KB
  in all wrote over it. Leave that button alone.
- **Writing Tasmota's factory image over the device**, with the web installer
  say, puts Tasmota's own table back. The tool still finds the identity, but
  the filesystem can now write over it in the same way.

Either way, your backup or your eWeLink account can still take the device
back to Sonoff's firmware. (Tasmota does not run on a Beken device, so none
of this applies there.)

## Changing firmware later

A device this tool converted can move to any other firmware the tool installs
on it, or back to Sonoff's. The tool finds it on the network by how it
answers, then runs steps 1 and 3 above, with no eWeLink in between: what has
to be in place, then what the device runs and the others on offer. Every route
has the same checks as a first conversion.

### How each move is made

On a **Beken device**, every move is the one a first conversion makes: the
running firmware takes the next as an update of its own (OpenBeken fetches it
from your computer, ESPHome takes an ESPHome update), and the bootloader
installs it on the next restart. A device left on the interim firmware is
recognised when you run the tool again, and can go on to any of the three.

On an **Espressif device**, most routes pass through the same place the first
conversion does: Sonoff's memory layout, with a full Tasmota running from its
second half. From there the steps above finish the job for Tasmota or ESPHome,
and going back to Sonoff writes the device's own firmware and settings into
the first half, checks them, and switches to them.

- **From ESPHome**, the tool sends the startup firmware as an ordinary
  ESPHome update, with your Wi-Fi written in. This project's ESPHome gives its
  Wi-Fi name and password through its native API, encrypted with the API's key
  where it has one. Otherwise the tool takes them from the device's Sonoff
  backup on your computer, when that is shown to be the device's network
  still: the firmware bundle on your computer was set up for exactly those
  details, or ESPHome says it is on that network. Failing that it asks you for
  them, or, where the API is encrypted, for its key, so that ESPHome gives
  them. The startup firmware joins your Wi-Fi and saves it for Tasmota, and
  the tool sends it the full Tasmota.
- **From Tasmota**, the device is on Tasmota's own layout, where Tasmota sits
  across both halves of Sonoff's. A small ESPHome, the *mover*, is written in
  front of it, the area Sonoff's layout keeps settings in is cleared, and
  Sonoff's layout table is written last - the one moment a power cut would
  leave a device that has to be opened. The device restarts into the mover,
  under whichever bootloader it has. To ESPHome, on a device that already has
  ESPHome's bootloader, the mover takes ESPHome itself as an update. On the
  way to any other firmware, or on a device with another bootloader, it takes
  the startup firmware as on the way from ESPHome, and the full Tasmota then
  installs ESPHome's bootloader, so that every device the tool converts ends
  with the same one.
- **A device whose layout puts its first app slot too near Tasmota** (the TH
  Elite and Dual R3) has no room for the mover there. Its mover goes where
  it goes on Sonoff's usual layout, under a temporary layout table with the
  first app slot moved there. The second slot is in the same place in both, so the full Tasmota
  runs from that and writes the device's own layout table, last, as before.
- **A device left part way** - running the mover, the startup firmware or the
  full Tasmota on Sonoff's layout - is recognised when you run the tool again,
  and can go on to any of the three from where it is.

### Going back to Sonoff's firmware

On an Espressif device, going back writes the device's own backup from your
computer when it is there. Without it, the tool puts the same together:
Sonoff's official firmware for your device, downloaded from eWeLink as the
device itself would (or else from the FlashDumps project's public copy),
Sonoff's layout, and settings holding the device's identity and its current
Wi-Fi. The
identity is the one the device keeps, or else rebuilt from your eWeLink
account, which knows the device's id, key, model and kind while the device is
still in it. The run says which it used.

On a Beken device, going back sends Sonoff's firmware the same way: the copy
kept beside the device's backup, else the file eWeLink offers the device,
else the public copy of it. Its own data stays on the device.

On either, a public copy is Sonoff's update file as the FlashDumps project
publishes it, pinned by its hash, and checked as the file eWeLink offers is.

On either, the tool signs in to your eWeLink account for this if the run has
not yet.

On an Espressif device, Sonoff's firmware is offered either way. Once you
choose it, and before anything is written, the tool finds where the identity
will come from. On Tasmota it reads the one the device keeps, or else signs in to your eWeLink
account; with neither, it stops there. From ESPHome the kept identity can be
read only part way through, so the tool checks your account and otherwise
counts on the one every device this tool converted keeps. If there is none
after all, or Sonoff's firmware cannot be downloaded, the tool puts the device
back on ESPHome or Tasmota, whichever it came from, and says so at the end.

Sonoff's firmware then starts with its own settings: names, timers and energy
history start afresh, and the meter's factory calibration comes back only
with the backup.

On either, the tool then waits for the device to answer on your Wi-Fi and to
show online in eWeLink again, answering there. One that is on your Wi-Fi but
never reaches eWeLink is most likely kept off the internet, by a firewall or a
guest-network or parental-control rule, and the tool says so.

On a Beken device there is nothing to find or put together: no conversion
writes to the store Sonoff's firmware keeps its identity, Wi-Fi and meter
calibration in, so Sonoff's firmware reads them from there as before. The
tool then waits for the device to be back in your eWeLink account.

### Keeping a firmware, or updating it

**Tasmota, kept.** On Tasmota, which runs on Espressif devices only, the tool also offers to keep it
(`--firmware tasmota` chooses that in advance). That changes only the meter,
which the tool calibrates as step 10 does if it is not calibrated already (on
a device converted before the tool did that, say). A meter calibrated by hand,
against another meter, is left as it is.

**Tasmota, updated.** On an official Tasmota older than the release this
tool installs, choosing Tasmota updates it. Tasmota restarts into its
safeboot, which takes the new Tasmota from your computer as in the
conversion's last steps, and the tool checks the meter as above. Tasmota's
settings and the device's template stay. Only Tasmota's own release builds are
offered this; a build of your own is left as it is. The Firmware Upgrade on
Tasmota's page, from Tasmota's site, keeps working too.

**ESPHome, updated.** On an earlier release of this project's ESPHome, the
tool offers the release it installs (`--firmware esphome` chooses it in
advance), sent as Home Assistant would send an update, with your Wi-Fi read
from the device and written in. ESPHome
writes it into the half of its memory it is not running from and starts it,
keeping its settings. On an Espressif device, a power cut before the new
release has run for a minute takes the device back to the release it had, so
the tool waits that minute out. The device reports its release, and when it was built,
through its native API.

A device adopted into an ESPHome dashboard, with an encryption key or
without, is not offered this: its build is not one this project published, and
the tool's firmware would take away the dashboard's changes and any key. You
update it from the dashboard
([how, and how to change a device adopted before 2.3.0](home-assistant-adoption.md)).
To install this project's firmware over it anyway, give `--replace-own-build`,
which tells you what you lose and asks you to confirm. It replaces only this
project's ESPHome for that kind of device, never another project's.

**OpenBeken, updated.** The same goes for an earlier release of this
project's OpenBeken (`--firmware openbeken`): it fetches the tool's release
from your computer, as it fetches any update, and restarts into it, keeping
its settings. The tool tells the device's release
from its build, and that it is this project's OpenBeken for that kind of
device from its driver. Update it this way rather than from the OpenBeken
project, whose own builds have neither the driver nor the device's memory
layout.

### Passwords and encryption keys

If your device's firmware has a password - a Tasmota web password, or an
ESPHome update password or web login, set in Home Assistant, say, on either
chip family - the tool asks for it: a web password or login when it finds the device, ESPHome's
update password before the go-ahead to leave ESPHome. If you have removed the
password on the device meanwhile, choose to try again without one. Each try is
checked on the device before anything is written. A password you type is held
in memory for the run only, and sent only to a device that asked for one.

An ESPHome with an API encryption key (`key:` under `api: encryption:` in its
YAML config) encrypts its native API, and from ESPHome 2026.9 takes its
updates encrypted too - and only those, with `encryption:` under `ota:`. With
the key, the tool reads ESPHome's Wi-Fi details again and sends its update
encrypted, needing no update password. It asks for the key as for a password:
before the go-ahead, for an ESPHome that takes only encrypted updates (or
instead of the update password, for one that takes both), and before it would
ask you for the Wi-Fi details.

`--web-password`, `--esphome-ota-password`, `--esphome-web-user` with
`--esphome-web-password`, and `--esphome-api-key` give them in advance, as do
the variables [Unattended runs](unattended.md#give-the-answers-in-advance)
lists.

### The Wi-Fi signal

Whatever the device runs, the tool reads its Wi-Fi signal before anything is
written. Tasmota and OpenBeken report it when asked. ESPHome reports it from
its Wi-Fi signal sensor, which this project's ESPHome has, or else from what
it prints of its configuration when asked through its native API (encrypted,
with its key, where it has one). On an Espressif device whose ESPHome gives
neither, the tool reads the signal once the full Tasmota is running, as the
startup firmware cannot report it either.

## Safety, and why you can believe the checks

- **Nothing is trusted.** Every firmware file is checked against a known
  fingerprint before use. An Espressif device converted to Tasmota is read
  back and compared with the official release; a Beken device's bootloader
  checks each firmware's checksum before installing it.
- **The risky step needs you to type PROCEED**, not a y/n, below a warning
  that spells out the risk.
- **The device checks too.** Every stage that writes refuses to run unless the
  device is in exactly the state it expects, and says so.
- **Nothing private is published.** Each run keeps its records in `captures/`,
  which git ignores.
