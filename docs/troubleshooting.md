# When it goes wrong

Every problem listed here actually happened while the tool was being built.

**First thing to try, always: start it again** - `start.cmd` on Windows, `sh
start.sh` on macOS or Linux, or `python3 -m tools.autoflash run` (the examples
below use that form, and the start scripts take the same options). It picks up
where it stopped, safely: every step checks the device before writing
anything.

**Where to look.** The screen shows a line for each stage; the full story is
in the run's log, `captures/devices/<MAC>/runs/<date>/run.log`, whose path the
tool prints. `--verbose` shows the same detail as it happens. The run's record
is in `autoflash-state.json` in the same directory, with the phase evidence
beside it, and `outcomes.jsonl` has a line for each run that worked in that
directory: how it ended, and where it stopped and why, in a word.
`events.jsonl` lists what each run did, step by step, for the test suites.

**A problem not listed here may be new.** Please open an
[issue](https://github.com/adamsthws/firmware-liberator/issues/new?template=bug-report.yml)
for it, so the fix helps everyone after you. When a run fails, the tool ends
with where to open one, [firmwareliberator.com/issues](https://firmwareliberator.com/issues),
and where its log is; its report, with its ID, is on your clipboard to paste in. The log is safe to share: it writes what identifies
you, your network or your device as `<email>`, `<wifi-name>`, `<device-id>`
and so on. To ask a question, start a
[discussion](https://github.com/adamsthws/firmware-liberator/discussions) of
your own.

---

## Your computer

**"the device did not fetch the ... instructions from this computer"**

The device could not reach your computer. The tool adds a firewall rule itself
when it can see the firewall (ufw or firewalld on Linux, Windows Defender
Firewall, the macOS firewall), so this usually means one it cannot see -
security software, or rules managed some other way - or that the addition was
declined. The message shows the command for the firewall it found. Nothing was
written; run again once the device can get through.

On Linux, blocked connections show in the kernel log:

```sh
journalctl -k | grep "SRC=192.168.99.50"
```

On Windows the tool's rule names Python itself, so Windows Security does not
ask about Python using networks. If it once asked and "Cancel" was chosen,
that left a rule blocking Python, and Windows applies blocking rules before
allowing ones. The tool turns such rules off while it runs and back on
afterwards, within the one permission question. Running the terminal as
administrator is not needed: the tool asks for administrator rights itself,
only for its firewall rule.

What the tool cannot change, it says, before it starts and again if the device
cannot connect:

- **Other security software** (Norton, McAfee, Bitdefender and the like)
  often has a firewall of its own, which can block the device whatever
  Windows' firewall allows. Allow Python in there, or pause it for the
  conversion.
- **A computer managed by an organisation** can have its firewall set, by
  Group Policy, to ignore rules added on the computer itself, and may not
  allow the administrator rights the rule needs. Ask whoever manages it, or
  use another computer.
- **A network or software that inspects encrypted connections** - common at
  work or school - makes eWeLink's and GitHub's certificates untrusted. The
  tool says so; use another network, or pause that software.

**The computer's address changed mid-run**

A network cable was unplugged, say, or the computer moved from wired to Wi-Fi.
The tool works out your computer's address afresh each time and says when it
has changed. If it chooses the wrong connection, name the address yourself:

```sh
python3 -m tools.autoflash run --listen-ip 192.168.99.20
```

Stay on one connection from the rebuild onwards, ideally wired.

**Joining the device's network: "waiting for this computer to join"**

The device answers at 192.168.4.1, and the tool carries on only once the
computer has actually joined. If the computer keeps dropping off the device's
network, close any setup page the system opened by itself: on Tasmota's setup
network every page view makes the device scan for networks, which briefly
takes its own network away.

If your home network also uses 192.168.4.x addresses and the computer has a
wired connection too, the two clash; unplug the cable while joined to the
device's network.

**An earlier installer's password is not on the clipboard**

Only an installer from an earlier release has a password on its network.
Copying it needs `xclip`, `xsel` or `wl-copy` on Linux (`pbcopy` and `clip`
are built into macOS and Windows). Without one, type it: `s60-ota-bridge`.

### Records from an earlier release

**"... captures/plugs (as releases before 3.0.0 named it) and ...
captures/devices both exist"**

Releases before 3.0.0 kept each device's backup and runs in `captures/plugs/`.
A newer release renames that folder to `captures/devices/` as it starts, but
not while both exist, as after running an earlier release again. Nothing has
been changed. Move each device's folder from
`captures/plugs/` into `captures/devices/`, delete the empty
`captures/plugs/`, and run again. Where a device has a folder in both, move
its contents across by hand, and never write over a `sonoff-backup/` already
there: the first backup is the original, and cannot be made again.

**"a run of an earlier release is converting a device right now"** means just
that: let that run finish, then run again. **"could not rename ..."** means
something has a file in that folder open; close it, and run again.

## The router rule (no longer used)

The tool no longer touches your router, so skip the destination-NAT rule an
older guide may describe. [router-rules.md](router-rules.md) says why it was
dropped and keeps the old instructions.

## A device the tool does not know

The tool converts the devices [the README lists](../README.md#what-you-need).
A model of one from another region is expected to work the same way: the UK
S60TPG and the European S60TPF have both been converted. The tool checks
rather than assumes, and stops at what it does not know:

- **"... is a ..., which this tool does not convert yet"** - eWeLink, or the
  device, reports a model the tool does not know. Nothing has been written;
  the device is still on its Sonoff firmware. The tool can
  [read it out](readouts.md) instead, to help add it.
- **"this device's memory is not laid out as this tool expects"** - the
  installer found a memory layout the tool does not know on an Espressif
  device. Nothing has been written over its Sonoff firmware: switch the
  device off and on, and it goes back to that.
- **The read-only check before the rewrite (step E1) stops the run** - the
  device's memory layout is not the one its kind has. The device runs Tasmota
  by then and keeps working; only the move to the official layout is refused.
  Leave it as it is and report it rather than forcing past the check.

In each case, please open an issue from the link the tool shows. A device
from another region that converts cleanly needs nothing more: its run's report
already tells the project ([what the tool sends](what-the-tool-sends.md)).

## The device can't be found

**"Your device was not found on this network"**

The tool lists each device in your eWeLink account that it converts, with what
it found:

- **"is offline in eWeLink"** - the device is switched off, or not on
  Wi-Fi. Switch it on and wait for the app to show it online.
- **"is at ... but is offline in eWeLink"** (under *"Your device is
  on this network but cannot reach the internet"* when it is the only
  problem) - usually the router keeps the device LAN-only. The conversion is
  sent through eWeLink, so allow it internet access, wait for the app to show
  it online, then choose *Look again*. You can block it again once it is
  converted.
- **"is online in eWeLink but not seen here"** -
  your computer is on a different network from the device, such as the 5 GHz
  or guest Wi-Fi, a different router, or a VPN. The message names the network
  the device is on.

**"No supported device in the eWeLink account ..."**

The device is in a different eWeLink account, or is one the tool does not
convert. The message lists what the account does have; choose *Use a different
eWeLink account*, or have the tool read your device out.

**"eWeLink is unreachable"**

The tool could not sign in. A device on Sonoff's firmware is found and
converted through eWeLink, so the run stops with nothing written: check your
computer's internet connection, then run it again.

**"did not answer within 180s"**

- Check it is actually powered on. This has caught everyone at least once.
- Its address may have changed. The tool finds the device by its hardware
  address on a new one by itself, asking only when it cannot be found at all.
  Your router's device list shows it under a name starting `tasmota-`.
- Your Wi-Fi may have gone, with a router restarting, say. Part way through,
  the device has no network of its own to fall back on: it keeps trying yours
  and rejoins within seconds of its return, and the tool waits.

**The device's own network**

The tool names the device's network whenever it asks you to join it. These
networks are open, and the device's page on them is at `http://192.168.4.1`.
[Device names](device-names.md) lists every name; in short:

- **Part way through a conversion** - on the installer, the mover, the startup
  firmware, or Tasmota not yet set up - the device and its network go by
  `autoflash-recovery-` and the last four characters of its hardware address,
  such as `autoflash-recovery-CDEF`.
- **Once converted**, by its firmware and the last six characters:
  `tasmota-abcdef`, `esphome-abcdef` or `openbeken-abcdef`.
- **A Tasmota that started from its own defaults** names its network after
  itself, such as `tasmota-ABCDEF-1234`, or sometimes `ESP_` and six
  characters one higher, such as `ESP_ABCDF0`: the same network, and the tool
  looks for both.
- **A device an earlier release left part way, or converted,** may show that
  release's name: `Sonoff-Setup-XXXX` for its installer, mover or ESPHome, or
  `S60-OTA-Bridge-XXXX` for an older installer, with the password
  `s60-ota-bridge`. The tool looks for those too, and gives the password.

## The first install

**"Device is offline in eWeLink; refusing to send" straight after a complete
download**

That means it worked: the installer does not talk to eWeLink, so the cloud
reports the device offline, and the tool will not send the update twice. It
looks for the installer on your network, then for its
`autoflash-recovery-XXXX` network, and carries on by itself when it finds
either. If your computer's Wi-Fi is off it cannot look for that network; turn
it on and run again.

**Asked to switch the device off and on before anything is installed**

The first send did not reach the device, or it turned it down, in a way only a
restart fixes: an update cut off part way earlier still holds it, or has left
it away from eWeLink. It keeps its Sonoff firmware. Switch it off and on at
the socket, choose to send it again, and the tool waits for eWeLink to show it
online first.

**"Device model is ..., not one this tool converts"**

Another Sonoff on the same account answered, and the tool stopped before
sending anything. Run again and choose your device from the list.

**"Empty reply from server" when you try `curl` yourself**

Tasmota refuses commands without a `Referer` header, as a security measure,
by closing the connection, which looks exactly like the device being switched
off. Add the header, as the tool does:

```sh
curl -H "Referer: http://192.168.99.50/" "http://192.168.99.50/cm?cmnd=Status%2011"
```

**Tasmota never appears after the installer gave it your Wi-Fi**

The installer gives Tasmota the Wi-Fi details it joined with. If they stop
working (say the router restarted onto another channel and refuses the
device), Tasmota keeps retrying them and never opens its setup network.

Switch the device off and on 7 times in a row, leaving it on for only 2-3
seconds each time, then leave it on. Tasmota counts quick restarts like that,
clears its settings on the seventh, and opens its setup network as on a first
install. The tool, still waiting, carries on with the setup network.

## Uploading firmware

**The upload finishes suspiciously fast and nothing changes**

A firmware upload to an Espressif device takes tens of seconds; one that
"succeeds" in under ten wrote nothing, because Tasmota silently commits
nothing without the file's size in the request. The tool sends it; by hand,
use the device's own web page rather than a bare `curl`.

**"the device refused the upload"**

The message includes what the device said. Usually the file is wrong: check
that `sha256sum -c SHA256SUMS` passes in `artifacts/`, and that you are
uploading the file the tool named, not the `.factory.bin`.

## When a migration stage stops

On an Espressif device going to Tasmota, the stages that check, stage and
rewrite the memory run on the device and report back. When one stops, the
tool shows the device's reason:

**"... did not pass: the device says: ..."**

Two kinds:

- **One of its checks refused** - "NVS changed since preflight", "source table
  changed", and the like. Nothing was written. Something changed on the device
  between stages; run again, and the earlier checks are done again first. If
  the same refusal comes back, report it rather than forcing past it.
- **"... returned HTTP -1 after N attempts"** - the device could not reach your
  computer: usually security software or a firewall that changed mid-run, the
  computer going to sleep, or a weak Wi-Fi signal (the tool warns below
  -75 dBm). These stages are safe to repeat, so the tool tries them again by
  itself, up to three times, before stopping.

**"the device stopped the ... without reaching this computer"** or **"the device
has sent nothing for 2 minutes"**

The device's reason could not reach your computer, so the tool reads the
device's console, shows its last lines, marked `The device's console:`, and
gives the reason from there ("its console says: ..."). Then as above.

**How long it waits**

A stage keeps the device busy, answering nothing else, for under a minute.
Kept busy much longer, a real device stops answering until it is switched off
and on (its energy meter's readings pile up), so it never spends more than 20
seconds in all retrying a request. If its Wi-Fi drops - a router
restarting, say - the stage stops, and the tool waits up to 5 minutes for the
device to answer, then runs the stage again by itself (three tries in all).
The tool checks on the device whenever it goes quiet, so a stopped device is
noticed within about 20 seconds, and no stage is waited on for more than 5
minutes in all. Waiting for you, to join a network say, has no time limit.

**"The device is not answering at all"**

A device kept busy too long, or left waiting on your computer when the tool
was stopped mid-stage, can answer nothing, not even its web page, though it
still answers ping. Part way through, the tool restarts a device
that has stopped getting anywhere, where its firmware has a way to; Sonoff's
own firmware has none
([Espressif research](espressif-research.md#no-restart-command)). Give it two
minutes; if its web page still does not answer, switch it off and on. Wherever
this can happen, that is safe: staging writes only the spare half of memory,
the rewrite downloads what it needs before writing anything, and installing
the official startup firmware carries on where it stopped. Then run the tool
again.

**"The device is on the network but not answering yet"**

A stage stopped part way (Ctrl-C, say) keeps the device busy for up to a
minute, until it gives up. Run again straight away and the tool waits for it,
then carries on.

## Slots and restarts

**"the device kept running its old firmware after 2 uploads"**

Each upload writes the *other* half of memory and restarts into it, so the
slot alternates. The tool handles one swap-back itself; this message means
uploads are not being committed at all (see "suspiciously fast" above). Switch
the device off and on, then run the same command again.

**A step says it was interrupted**

The tool marks a step before it starts writing, so an interruption is never
silent. Where the device shows the step finished, the tool moves on; otherwise
it does the step again, safely, as every step checks the device before it
writes and refuses if it is not as expected.

## After the rebuild

**The device is broadcasting its own Wi-Fi network**

Normally the temporary startup firmware saves your Wi-Fi details where
Tasmota reads them. If its own settings write fails, it saves nothing, and
Tasmota starts up asking to be configured. Join the setup network the tool
names (open, no password) and the tool sends your Wi-Fi details itself. If the
device cannot connect with them, the tool says so and leaves the device
waiting, with nothing saved; you can set the Wi-Fi up at
`http://192.168.4.1`.

If your computer cannot see Wi-Fi networks, the tool cannot spot the setup
network either: when it asks what to do about a device it cannot find, choose
*It is showing its own Wi-Fi network*.

**Safeboot is showing instead of Tasmota**

Safeboot is the small recovery firmware. Seeing it after the rebuild is normal;
after the whole run, it means the full Tasmota was never installed: run again
and let the tool redo that step.

A converted device also shows it after another firmware's whole-flash image,
such as an ESPHome `factory.bin`, was uploaded on Tasmota's *Firmware Upgrade*
page. For a file that big Tasmota switches to Safeboot, which writes it over
Tasmota, whether it then says "Upload Failed" or "Upload Successful", so the
device comes back on Safeboot every time, even switched off and on;
`SetOption78` makes no difference. Its settings are still there. Run the tool
on the computer that converted the device, or with `--device-ip` and the
device's address: it puts official Tasmota back with those settings, and goes
on to the firmware you choose, or stops there if you choose Tasmota. By hand,
upload the ordinary `tasmota32c3.bin` (`tasmota32.bin` on a classic ESP32),
not a `factory.bin`, on Safeboot's *Firmware Upgrade* page.

To move a converted device to ESPHome, choose ESPHome in the tool rather than
uploading it yourself: Tasmota's layout has a single place for firmware, and
the tool moves the device to one ESPHome can update itself in.

Safeboot has no Wi-Fi setup of its own and relies on settings already saved on
the device, which is why the conversion uses a temporary version with Wi-Fi
details built in, and swaps it for the official one at the very end.

## The worst case

**Power was lost while the memory layout was being rewritten**

If the device does not come back at all after that step, it cannot be
recovered over Wi-Fi. It would need opening, and a USB-serial adapter
connected to the chip inside. See
[reference/recovery-1.1.1.md](../archive/docs/reference/recovery-1.1.1.md),
written for the S60, and
[why this step cannot be undone](what-happens.md#the-step-that-cannot-be-undone).
This is the risk you accepted at the start, and the only failure that
rerunning the tool cannot fix.

## A converted device that has lost your Wi-Fi

After a new router or a changed Wi-Fi password, the device cannot join any
more.

- **ESPHome** opens its own network, such as `esphome-abcdef`, after a minute
  and a half without your Wi-Fi; join it, and its page asks for your Wi-Fi. It
  keeps trying your Wi-Fi meanwhile, so after a router restart it simply
  rejoins. It opens that network only once each time it starts: if it already
  has (your router was off for over a minute since, say), switch the device
  off and on, and it opens it a minute and a half later. (A fault of ESPHome
  2026.10, which builds the ESPHome the tool installs; ESPHome 2026.11 opens
  it every time.)
- **Tasmota** keeps trying your Wi-Fi and never opens a network by itself.
  Press its button 6 times quickly: it opens its setup network for 3 minutes
  (such as `tasmota-abcdef`, or `ESP_` and six characters). Join it, browse to
  `http://192.168.4.1` and choose your Wi-Fi.

## A converted device that switches nothing

Tasmota answers and stays on your Wi-Fi, but has no relay or power meter: in
Home Assistant's Tasmota integration it shows only a restart button and its
Wi-Fi. Tasmota restarted six times in a row, each within 10 seconds of
starting, took that for a boot loop and went over to its fallback module,
which knows nothing of your device. A device converted by 2.1.2 or later has
its own template as that fallback and goes on switching. For one converted
earlier, [the check below](#checking-a-finished-device-at-any-time) puts the
template back and makes it the fallback, whatever the device. Or, for an S60,
in the device's web page under **Tools → Console**:

```
Backlog Template {"NAME":"Sonoff-S60","GPIO":[1,1,1,1,224,544,1,3104,1,32,1,0,0,0,0,0,0,0,1,1,1,1],"FLAG":0,"BASE":1}; Module2 0; Module 0
```

## An ESPHome device with no page and no relay

If ESPHome is stopped within a minute of starting ten times in a row, by
switching it off and on say, it takes that for a boot loop and starts in safe
mode: on your Wi-Fi with only its update service, no page, no relay, nothing in
Home Assistant. Leave it switched on: after five minutes it restarts as
itself, with nothing lost. The tool, run meanwhile, says so and waits.

## Checking a finished device at any time

```sh
python3 -m tools.autoflash step H1 --device-ip 192.168.99.50
```

On an Espressif device on Tasmota, this repeats the check a conversion
[ends with](what-happens.md#at-the-end), comparing the bootloader, the memory
map, Safeboot and Tasmota with the known files. It also checks that Tasmota
has the device's template, as its fallback module too. It only reads, except
that a device without its template (which switches nothing) gets it back,
keeping its other settings, and one without it as the fallback gets that. It
is safe to run whenever you want reassurance.
