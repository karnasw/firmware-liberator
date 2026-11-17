# Unattended runs

For testing: a whole conversion with nobody at the keyboard, its screen kept
for review, colours and all.

## Give the answers in advance

Copy `.env.example` to `.env`, which git ignores, and fill it in:

```sh
cp .env.example .env
chmod 600 .env
```

| Name | What |
|---|---|
| `EWELINK_EMAIL` | the eWeLink account the device is in: its email, or the phone number it was made with, with its country code (`+44 7700 900123`) |
| `EWELINK_PASSWORD` | its password |
| `AUTOFLASH_SUDO_PASSWORD` | Linux and macOS: your computer's password, only if sudo asks for one (for the temporary firewall rule) |
| `AUTOFLASH_WIFI_SSID`, `AUTOFLASH_WIFI_PASSWORD` | only for a device on an ESPHome that cannot say its Wi-Fi password (one of this project's first builds, or one whose API encryption key is not given as `AUTOFLASH_ESPHOME_API_KEY`) and no Sonoff backup of it on your computer: its network, and the password (none for an open network) |
| `AUTOFLASH_TASMOTA_PASSWORD` | a device on Tasmota with a web password: that password. Without it, the run stops when it finds the device |
| `AUTOFLASH_ESPHOME_OTA_PASSWORD` | a device on ESPHome with an update password (`password:` under `ota:` in its YAML config): that password. Without it, the run stops before the go-ahead to leave ESPHome |
| `AUTOFLASH_ESPHOME_WEB_USER`, `AUTOFLASH_ESPHOME_WEB_PASSWORD` | a device on ESPHome with a web login (`username:` and `password:` under `web_server:` `auth:` in its YAML config): both. Without them, the run stops when it finds the device |
| `AUTOFLASH_ESPHOME_API_KEY` | a device on ESPHome with an API encryption key (`key:` under `api:` `encryption:` in its YAML config): that key, with which the run reads the device's Wi-Fi details off it and sends its update encrypted, needing no update password. Without it, a device that takes only encrypted updates (`encryption:` under `ota:`) stops the run before the go-ahead |

`.env.example` explains the file's other names, such as `AUTOFLASH_NO_REPORT`
([what-the-tool-sends.md](what-the-tool-sends.md#turning-it-off)). A name set
in the environment wins over the file, `--env-file PATH` reads another file,
and a misspelt name is refused, not ignored. The names from before the
`AUTOFLASH_` prefix (`S60_SUDO_PASSWORD`, `S60_WIFI_SSID`, `S60_WIFI_PASSWORD`)
are still read, and the run asks you to rename them. The values stay in the
tool's memory: the helper tools it starts do not inherit them, and the
passwords are kept out of the log.

An ordinary run reads the file too, saving typing the account details; a
password eWeLink refuses is asked for instead. In the emulator only
`unattended-test.sh` hands `.env` to its run
([emulator.md](emulator.md#unattended-for-testing)).

## Run it

```sh
python3 tools/repo/unattended_run.py                       # recorded, as below
python3 tools/repo/unattended_run.py -- --device-ip 192.168.99.50 --new
sh start.sh run --unattended                          # the same, unrecorded
```

Anything after `--` goes to the run itself. `--timeout MINUTES` stops a run
still going after that long, as Ctrl-C would. `--command readout`
makes a [readout](readouts.md) instead, kept and never sent.

With `--unattended`:

- The "Before you start" list and window-size note are not waited on.
- The run does not ask which firmware to install: give `--firmware`
  (`tasmota` or `esphome` for an Espressif device, `openbeken` or `esphome`
  for a Beken one, `sonoff` to go back to Sonoff's firmware), or it stops
  there.
- PROCEED, before the point of no return, is taken as typed.
- An unfinished run is carried on, as the menu's first option would (`--new`
  starts afresh), redoing, as any run does, an interrupted step the device
  does not show finished.
- A yes/no question takes its cautious default: a weak Wi-Fi signal, for
  one, stops the run.
- Any other question stops the run, saying which: two devices answering,
  say, or Wi-Fi details that cannot be read off the device.

Each answer it gives shows on screen, marked `(unattended)`.

## The recording

`unattended_run.py` runs the conversion on a pseudo-terminal of its own, so
it draws exactly what a person would see, shown live too when started from a
terminal. At the end, the final screen is rebuilt from the recording,
printed when the output is not a terminal (a CI log, say), and kept in
`captures/unattended/<time>/`, or where `--save DIR` says:

| File | What |
|---|---|
| `screen.ansi` | the final screen, with its colours: `cat` or `less -R` it |
| `screen.txt` | the same without colours, to read or compare anywhere |
| `screen.raw` | every byte as written: `cat` replays it in a terminal |

The run's terminal is the size of yours, or 100x40 where there is none;
`--size 120x50` sets it. The exit code is the run's: 0 for
finished, 130 for stopped at a question or by Ctrl-C, 143 for stopped by
`--timeout`, and anything else for failed. The full log is the run's
`run.log`, under `captures/devices/<MAC>/runs/` as usual.

A line starting `SCREEN:` after the exit code says a stage left on screen
what it should have taken away: a failed try still shown after a retry, what a
passed stage showed while under way, or a stage left at "Underway" when the
next began; or that the run ended otherwise than it is drawn to - its report
out of line, neither the links nor the way to open an issue under it, a
report ID or the clipboard said by a run that did not fail, a question left
unanswered above what came after it. The fault suites fail such a run
(FAIL-SCREEN in [testing.md](testing.md)).

What the run puts on the clipboard is kept beside the recording, as
`clipboard.txt`: a stand-in for the clipboard tool, first on the run's
`PATH`, takes it, and this computer's clipboard is left alone.

## As a person sits through it

`--attended` runs without `--unattended`, asking every question as it would
a person, and `--answers FILE` answers them from a JSON list of what
to type when the screen shows what:

```json
[{"when": "Press any key", "type": "x"},
 {"when": "Type PROCEED", "type": "PROCEED\r"},
 {"when": "Wi-Fi password for", "type": "secret\r", "times": 2}]
```

`when` is a regular expression, matched against the question waiting for an
answer: the screen's last row, where its prompt ends it (with the field under
it, for a password), not the notes above or the question answered before.
`type` is typed once, or `times` times; `\r` is Enter. When several match,
the first with times left is typed. `"anywhere": true` types it when `when`
shows anywhere on the screen, such as a Ctrl-C (`"\u0003"`) while a stage
shows - again only once the run shows it anew - and `"after": SECONDS` once
it has shown that long. In place of `type`, `"power": "off"` (or `"on"`) switches the
emulated device, as if unplugged; `"router": "off"` (or `"on"`, and in the
Beken emulator `"on NEW-PASSWORD"`) its home Wi-Fi's router; `"wifi":
"join"` (or `"home"`) moves the computer onto the device's own network, or
back, with the emulator's `wifi`; and `"signal": "TERM"` (or `"HUP"`, its
window closed) is sent to the run.

The recording shows each question as asked and what it left once answered,
and `answers.json` beside it says where in `screen.raw` each answer was
typed. In the emulator, `unattended-test.sh --answers=FILE` does the same.

## In the emulator

`tools/espressif/emulator/unattended-test.sh --fresh` starts the emulator,
converts its device this way, prints the final screen and stops it
([Unattended, for testing](emulator.md#unattended-for-testing)).

A readout made in the emulator is sent to the API Worker's own code
([`website/api/local.mjs`](../website/api/local.mjs)), run in its own
container on the emulator's network from a pinned Node image, started afresh
for each run, with `ENOUGH` from `AUTOFLASH_API_ENOUGH`; what it was asked and
kept is under `api/` in the instance's state directory. `--no-api` keeps it
unsent instead. With `AUTOFLASH_EMU_STUB_OTHERS` listing other
devices in the account ([cloud-stub.md](cloud-stub.md)), it also tests a
readout of every device.

## Windows

The recorder runs on Linux and macOS only. On Windows,
`start.cmd run --unattended` runs unrecorded, and the firewall rule's UAC
window still needs a click.
