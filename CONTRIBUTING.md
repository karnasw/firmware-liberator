# Contributing

Thank you for helping. A report of how a conversion went is as useful as
code: what worked, what did not, and on which device and computer.

## Reporting

- **How a conversion went** - clean, stopped part way, or only the read-only
  checks: [Conversions In The Wild](https://github.com/adamsthws/firmware-liberator/discussions/1).
- **A conversion that went wrong**: an [issue](https://github.com/adamsthws/firmware-liberator/issues/new/choose),
  with the run's log attached. The log is safe to share - your account,
  network and device are already written in it as `<email>`, `<wifi-name>` and
  so on - but its folder is named after the device's MAC address: attach the
  file, do not paste its path. [docs/troubleshooting.md](docs/troubleshooting.md)
  covers what has gone wrong before.
- **A device the list does not have yet** - if you got the tool working on a
  model or a stock firmware version that is not in the compatibility list, please
  contribute it back so the list keeps growing. Say what the device was and
  what the conversion took - the addresses, config and wiring a new model
  needed, and anything that differed from a listed one. A report alone is
  welcome; a pull request that adds the support is more welcome still, and the
  notes behind it are what this project most wants to receive.
- **A security problem**: privately, never as an issue - see [SECURITY.md](SECURITY.md).

## Setting up

The tool needs nothing but Python 3.10 or newer. Working on it needs a few
tools more - the test helpers and the linters - which go in a virtual
environment in the checkout, never into the system's Python:

```sh
tools/repo/setup-dev                       # creates .venv, installs both files below
python3 tools/repo/leak_scan.py install
```

`tools/repo/setup-dev` is the one-command form of:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --require-hashes --no-deps -r requirements/ci.txt -r requirements/lint.txt
```

[uv](https://docs.astral.sh/uv/) works as well and faster, from the same
files: `uv venv` and `uv pip install --require-hashes -r ...`.

The leak-scan line points Git at the hooks in `.githooks/`: before each commit
they scan what is staged for secrets and real identifiers, and lint it; before
each push they scan every commit it sends. A linked worktree reuses this
checkout's `.venv`, so run `tools/repo/setup-dev` once in the main checkout - and
in the worktree as well when its branch changes `requirements/`, or it lints
with the main checkout's versions.

Optional, for what they check: Docker, for the ESPHome configs' check and
the emulator; PowerShell 7.4.6 or newer (`pwsh`), with
`Install-Module PSScriptAnalyzer`, for the PowerShell scripts. A commit on a
computer without them is not stopped - the hook says what it skipped, and
the workflows run them all.

## Testing

```sh
python3 tests/run.py          # the unit tests, about 11 s
python3 tools/repo/lint.py all     # every linter on every file
python3 tools/repo/oldest_python.py  # before a push: the unit tests on the oldest Python (Docker)
```

Every fix comes with a test that fails without it. The tests run side by
side and in any order, so a new test must leave nothing behind, keep hold of
any port it picked, and touch nothing shared across the whole computer:
[docs/testing.md](docs/testing.md#side-by-side-in-any-order). Coverage and
mutation testing show where the tests are thin - a guide, not a target:
[docs/testing.md](docs/testing.md#how-much-they-test). The emulators - a
virtual device of each chip family, running Sonoff's own firmware - test whole
conversions: [docs/emulator.md](docs/emulator.md). Never try a change on a
device you cannot afford to lose.

## Keeping private things out

The repository is public, and the tool handles people's Wi-Fi passwords,
eWeLink accounts and devices' identities.

- `.env` (passwords for unattended runs) and `captures/` (each device's
  backup and runs) are never committed; Git ignores both.
- In examples, tests and docs, use the made-up MAC `12:34:56:ab:cd:ef` and
  addresses that are nobody's - never your device's, network's or account's. A
  test's address can be a real device where it runs. Tests, the archive and
  internal defaults use `192.0.2.x`, reserved for examples. What people read -
  prompts, help text, docs, screenshots - uses `192.168.99.x`, which looks like
  a home network, with `192.168.99.50` the device. An address a program really
  uses stays, such as the Beken emulator's built-in network;
  `tests/repo/test_example_addresses.py` holds every file to this and lists them.
- If the leak scan stops a commit, take the finding out. A new made-up
  example belongs in `tools/repo/leak-scan-allow`; nothing real is ever let through
  with `--no-verify`.
  [docs/technical-details.md](docs/technical-details.md#keeping-secrets-out-of-git)
- No third party's proprietary firmware - Sonoff's stock images, a device's
  dump, anything else this project has no right to publish - is ever committed,
  put on a release or built into a published image. Git ignores `*.bin` for this
  reason; the exceptions listed in `.gitignore` are open-source firmware this
  project ships. Where the tooling needs such an image, as the emulators'
  tests do, it is fetched at run time from wherever its owner or another
  project publishes it, never copied into this repository.

## Commits and pull requests

- **One cohesive change per commit**, its message saying what changed and
  why. While a branch is unpushed, fold a fix to that branch's work into the
  commit it belongs to rather than adding one on top.
- **A pull request** is squashed into one commit when merged, once the
  required checks pass: the unit tests on Python 3.12 and 3.10, the
  emulator, the lint and the leak scan. A pull request from a fork runs them
  all - none needs a secret - once a maintainer has let a first-time
  contributor's workflows run. The form a new pull request opens with lists
  what you need to check.
- **Written with an AI assistant?** That's fine, and you don't need to say
  so: as with the Linux kernel, whoever submits a change answers for it -
  its correctness, its tests and its licence - however it was written.
- **A change people will notice** gets a line in `CHANGELOG.md`. The
  maintainer sets the version and releases.

## Code

- The tool uses Python's standard library only, on Python 3.10 or newer.
  Open text files with `encoding=`.
- `artifacts/` - its firmware, `firmware.json` and `SHA256SUMS` - changes only through
  `tools/repo/update_firmware.py` - most often run by the weekly firmware-update
  workflow: [docs/updating-firmware.md](docs/updating-firmware.md).
- A script in `tools/` that is run from the command line has a shebang and is
  executable (`git update-index --chmod=+x`); a module only imported has
  neither (`tests/repo/test_executables.py` holds this).
- Where things go: `tools/autoflash/` is the conversion tool (`python3 -m
  tools.autoflash` runs it) and `tools/repo/` the repository's own plumbing; `tools/espressif/` and `tools/beken/`
  hold each chip family's tooling, its emulator in `emulator/`; `tools/ewelink/`
  what talks to eWeLink whatever the chip, and in `stub_cloud/` the stand-in
  for it both emulators use; `devices/<device>/` what is one device's
  alone (its sheet, its ESPHome configs - at the paths converted devices give
  Home Assistant, so a move means a new image - and modules); `tests/` mirrors `tools/`
  (`tests/autoflash/`, `tests/repo/`, `tests/ewelink/`, `tests/espressif/`,
  `tests/beken/`; `tests/run.py <directory>` runs one of them); `docs/` prose only;
  and `archive/` the work that got the project here and is no longer used,
  kept for the reasoning, linted like the rest.
- The chip families behave alike. A feature or fix for one family's route
  (Espressif, Beken, and any family added later) is made for every family it
  applies to, in the same way: the same sources tried in the same order, the
  same questions asked, the same things checked, logged and reported, with
  parallel code named and laid out alike. Where a family cannot do the same,
  say why in a comment beside the difference.
- Docs, comments and commit messages match the existing ones: plain,
  compact sentences saying what something does and why.

## Recording what you find

What you learn while working - how a device's firmware behaves, why something
failed, an answer to an open question - goes into the repository's docs in the
same change that comes of it, not later, and not only into a commit message or
a conversation. Put each finding where someone would look for it: a device's
own page, the chip family's research notes
([docs/espressif-research.md](docs/espressif-research.md),
[docs/beken-research.md](docs/beken-research.md)), the emulator's README, or the
page about the part of the tool it concerns.

- **Correct what it makes stale, then and there.** When research or a test
  shows a page was wrong or a guess was, change that page in the same change:
  never leave a known-wrong line for later.
- **Say how sure it is.** Mark what was seen (where, and how) apart from what is
  inferred, so the next person knows what to check.
- **Close open questions.** An answer found moves out of "Questions still open"
  into the section it belongs to; a new question found goes in.
- **Write the method down too**, where it would help someone repeat it: how a
  firmware was read, how a run was watched.

## Writing docs

Write for the person reading the page, not from the side of whoever built the
tool. That person may not know how the project works, or why it was made this
way.

- **Say "you" and "your device"**, and tell readers what they do and what happens
  to them. Call the software "the tool" or "this project", or name the program,
  such as "the stub". Avoid "we", "our" and "ours".
- **Say "device", not "plug"**, in docs, code and what the tool prints: the
  project began with one smart plug and now converts switches, relays and
  panels too. "Plug" stays only where it is someone else's word: "smart plug"
  as a product's description, a product's or firmware's own name, a name
  already published (the `emulator-public-plugs` release), and the names
  earlier releases gave things the tool still reads (`captures/plugs/`).
  `tests/repo/test_device_wording.py` holds every file to this.
- **Use words the reader already has.** On pages for device owners, say "firmware"
  rather than "image", and leave out terms such as "pushed" or "pinned" where
  only the result matters. Pages for people working on the code can use them.
- **Say where.** "From this project" or "on your computer", not "from here".
- **Write full sentences.** A reader should not have to read one twice to find
  out what it is about.
- **Keep to any computer.** The tool, its tests and its emulators run on many
  kinds of computer, so describe what was found as it applies to any of them
  ("a firewall that turns the emulators' network away", "a 6-core computer"),
  never the machine it was found on: no computer, virtual machine or user
  names, home folders or a contributor's own setup.

For example:

| Instead of | Write |
|---|---|
| so one you add changes nothing of ours | The project's config does not use substitutions, so you cannot change its settings with them |
| The tool then leaves the device to the builder | From then on you update the device from the builder |
| Your config then fetches nothing from here | The builder then downloads nothing from this project |
| ESPHome takes it for a new item and refuses it | ESPHome treats it as a new item and shows an error |

Released sections of `CHANGELOG.md` are history and stay as they were.
`tests/repo/test_docs_voice.py` fails on "we", "our", "ours", "us" and "from
here" in the docs.
[docs/home-assistant-adoption.md](docs/home-assistant-adoption.md) is a good page
to copy the style from.

### What goes in `docs/`, and what in `.github/`

`docs/` is published on the website, for the people who use the tool and
those who work on its code. A page there says what its reader does and sees,
and why, and links to the code for how.

What only the maintainer needs goes in a page in `.github/`, which the website
leaves out: how the project's servers and API work (proof of work, rate
limits, storage, retention), the accounts and tokens behind them, and how to
look after what they hold. Such a page says at its start that it is for the
maintainer, and links to the `docs/` page its reader would otherwise look
for. [.github/readouts.md](.github/readouts.md) is the maintainer's side of
[docs/readouts.md](docs/readouts.md).

### Specifics live in the code

A doc that copies a detail from the code goes stale the day the code
changes, and nothing tells you. So each specific - a limit, a timeout, a
count, a version, a permission, a list of names, which file does what - is
written once, in the code, with a comment beside it saying why. A page says
what happens and why, and names where to look for the rest.

- **Name the constant or file, not its value.** "`TOKENS` in
  `tools/repo/cloudflare_settings.py` lists each token and what it may do",
  not the list itself; "at most `READOUT_LIMIT` a minute", not "two a
  minute"; "the Workers", not "the three Workers".
- **Have the tool say it.** Where someone needs the details to act, the code
  prints them from what it holds - in `--help`, or in the message that says
  what is left to do - rather than a page repeating them.
  `tools/repo/cloudflare_tokens.py` run without its token says how to make
  one, from `ROTATE_PERMISSIONS`.
- **A page that must give a value** - a port to open in a firewall, say - has
  a test holding it to the code's.
- **Comments sit beside what they explain**, so a change to one is made with
  the other in view.

`tests/repo/test_layout.py` fails when a page names, in backticks, a path
under the project's own folders that is not there, or when a maintainer's or
contributor's page names an `UPPER_CASE` constant no code holds: the most
common way a page goes stale is a rename.

## Elsewhere

A bug found in another project - Tasmota, ESPHome, ESP-IDF, OpenBeken, the
emulator's QEMU - is reported there, and every pull request or issue opened
because of work on this project gets an entry in
[docs/upstream-contributions.md](docs/upstream-contributions.md) in the same
change, in that document's style: a row in the table of the topic it belongs
to (or a new topic, with a short introduction), giving the project, a link
and one line on what it fixes, and a note on how the fix was proven. Open the
pull request, then write the entry, so the link is real.

AI assistants working here: [AGENTS.md](AGENTS.md).
