# How the tool is tested

Three layers, each catching what the one before cannot:

1. **Unit tests** - fast, with no device: each piece on its own.
2. **The emulators** - a virtual device running its real Sonoff firmware
   ([emulator.md](emulator.md)): whole conversions, and every fault the
   suites below put in their way.
3. **Real devices** - what no emulator shows: real flash, real radios, real
   timings.

## Unit tests

```sh
python3 tests/run.py
```

This runs the tests side by side, in about 11 seconds on six cores, where one
after another (`python3 -m unittest discover -s tests`) they take a minute,
mostly waiting. Only what fails is shown, with what the test printed.
`python3 tests/run.py autoflash.test_output` runs one module (or a class, or
a test), `python3 tests/run.py espressif` one directory's tests, and
`--shuffle` all of them in a random order, to find a test that passes only
after another.

They cover the tool's decisions (which route, what to ask, when to try
again, what counts as done), with the device and the network stood in for,
and the parts that must be exact: the plans the device carries out, the
update protocols, image checks and pins, what is kept and what never is.
Every bug the tests or the fault suites have found has a test that fails
without its fix.

The `emulator-check` workflow runs them on Python 3.12.10, the portable Python
the start scripts install and most conversions run on, and on 3.10, the
oldest the tool runs on: a Mac's own Python may be older than 3.12, as may one
already installed where the portable one cannot be had.

A newer Python passes code the oldest cannot run, and CI's run on the oldest
says so only after a push. Before one, run them on it yourself:

```sh
python3 tools/repo/oldest_python.py
```

It tests what `HEAD` holds, in a clone of its own, in the official image of
the oldest Python with the packages CI installs, so it needs Docker. The tests
run side by side there too: about a minute in all, once the image is pulled.
The `newer python` linter finds the commonest of it at each commit.

### Where a test goes

`tests/` mirrors `tools/`: a module's tests sit where it would be, named for
it:

| Under test | Its tests |
|---|---|
| `tools/autoflash/runner.py` | `tests/autoflash/test_runner.py` |
| `tools/autoflash/routes/espressif/mover.py` | `tests/autoflash/routes/espressif/test_mover.py` |
| a package's own `__init__.py` | `test_<package>.py`, inside the package's tests |
| a script, such as `emu.sh` | its whole name with `-` and `.` as `_`: `test_emu_sh.py` |

A test across several modules, or of the whole repository, is named for what
it holds instead: in `tests/repo/`, or listed in `NOT_MIRRORED` in
`tests/repo/test_layout.py` with the reason. That test fails on a test module
that is neither mirrored nor listed.

### Side by side, in any order

`tests/run.py` runs each test in its own turn on one of many processes (two a
core, or `-j N`), in an order unittest never uses, so a test must pass
whatever ran before it in its process and beside it in the others. A test
still running after 60 seconds (`--timeout`; the longest takes about 10) is
stopped, reported with where each of its threads was, and fails the run.

2,000 runs in a row passed, each in a new random order, on Python 3.10, 3.12
and 3.14 and on 2 to 64 processes. The rules below come from what the first
runs found. The time limit is there for a hang like the one in a test
server's `shutdown()` that once waited for ever: seen only once, its cause is
not known, and the limit would now show where it was.

When you write a test, keep it safe to run this way:

- **Leave nothing behind** in a module, a class or the environment: patch for
  the test (`mock.patch...start()` with `addCleanup`), and put back what the
  code under test keeps for a process's life. A bundle's recovery image left
  in memory, or a question left unanswered for the next test's
  `output.start()`, each broke a test that ran after.
- **Keep a port** you picked as free. A server given one is started again on
  another if it cannot bind it, and a check that nobody answers keeps the
  socket bound but not listening, so a connection is refused at once and no
  other test can take the port meanwhile.
- **Share nothing across the whole computer** - its sleep inhibitors, a fixed
  port, a file outside the test's own temporary directory - unless the tests
  sharing it are in `TOGETHER`, with the reason.
- **Reach nothing beyond this computer**: stub what the code under test asks
  of a device, eWeLink or anything else on the network. A made-up private
  address can be a real device where the tests run, so any connection beyond
  loopback fails the test that made it, naming the address, even where the
  code catches the error (`tests/no_network.py`, switched on in each test
  package's `__init__.py`).

To soak them again after a change to how tests share anything:

```sh
for i in $(seq 200); do python3 tests/run.py --shuffle -j 24 > soak.txt 2>&1 || { grep -m1 seed soak.txt; break; }; done
```

The loop stops at the first failure and prints its seed; `--seed N` with the
same `-j` runs that order again.

### How much they test

Neither measure below is a gate or a target: a test written to raise one
tends to test the stand-ins, not the tool. They point to a decision no test
takes both ways, or a check whose reason no test asks about, for you to judge
what is worth a test there; what only the emulator or a device can show is
tested there.

**Coverage** says what the tests run. The `emulator-check` workflow writes it
into its summary by file; on your computer, with what each file misses:

```sh
.venv/bin/python -m coverage run --branch --source=tools -m unittest discover -s tests
.venv/bin/python -m coverage report --sort=cover -m
```

`--branch` counts each way an `if` can go; without it, a line run once counts
however its condition went.

**Mutation testing** says whether the tests would notice a mistake. A tool
([mutmut](https://mutmut.readthedocs.io/)) changes the code one small way at
a time - `>` to `>=`, `or` to `and`, an error's message to `None` - and runs
the tests on each change; one they all still pass is a mistake nobody would
see. It takes minutes and asks for judgement, so run it by hand, never in a
hook, on a module that decides something costly once its tests are written.
It runs in a copy of the checkout with its own virtual environment (mutmut
needs pytest, which runs unittest's tests as they are):

```sh
work=$(mktemp -d)
git archive HEAD | tar -x -C "$work"
python3 -m venv "$work/.venv"
"$work/.venv/bin/pip" install mutmut==3.8.0 "pytest<9"   # 3.8.0 reads a pytest 8 attribute
cat > "$work/pyproject.toml" <<EOF
[tool.mutmut]
source_paths = ["tools"]
only_mutate = ["tools/autoflash/plans.py"]
pytest_add_cli_args_test_selection = ["tests/autoflash/test_plans.py", "tests/autoflash/routes/espressif/test_mover.py",
                                      "tests/autoflash/routes/espressif/test_tasmota.py"]
also_copy = [$(git ls-files | cut -d/ -f1 | sort -u | grep -vx -e tools -e tests | sed 's/.*/"&"/' | paste -sd, -)]
EOF
cd "$work" && .venv/bin/mutmut run && .venv/bin/mutmut results
```

`only_mutate` is the module, and the tests every file that uses it (`grep -l`
its name in `tests/`): leave one out and mutmut reports the code only that
file reaches as "no tests". `mutmut results` lists each change that survived,
and `mutmut show NAME` what it changed.

mutmut names a mutant by its file's path (`tools.autoflash.plans...`) and
matches it to what the tests import. So a module the tests import by bare
name (the Beken tools in `tools/beken` and its emulator's in
`tools/beken/emulator`, which `tests/beken/test_wifi_migrate.py` and
`tests/beken/emulator/` put on `sys.path`) is mutated from a copy at
`src/<name>.py` in the work copy, the test's path changed to `src`.

Some changes can never be caught, as they change nothing: in `Plan.check`,
`length <= 0` and `length <= 1` never differ, every length being whole
sectors. Say so in the commit that adds the tests, so the next run's reader
need not work it out again.

`plans.py` shows what the two measures tell apart: with 92% of its lines
covered, 144 of 634 changes survived, 31 in `Plan.check`, whose refusals were
tested only as some `PlanError`, and its limits only past them. With each
refusal's reason and each limit now tested from both sides, 117 survive,
mostly in the plans it builds, which the emulator runs whole.

## Linters

```sh
tools/repo/setup-dev               # once: creates .venv, installs the linters (and test helpers)
python3 tools/repo/lint.py all
```

The linters find what is likely wrong in the code before the tests run it:
the pre-commit hook on the files a commit changes, and the `lint` workflow on
every file.

| Linter | On | Looks for |
|---|---|---|
| compile | Python | what parses yet will not compile, which no linter sees: more than 20 blocks nested in one function, say (CPython's limit before 3.13); CI also runs it alone on Python 3.10 (`lint.py compile`) |
| newer python | Python | what came in a Python newer than the oldest the tool runs on and is easy to use out of habit - `TestCase.enterContext`, `tomllib`, `typing.Self`, `except*` - which a newer Python runs without a word; the list is in `tools/repo/newer_python.py` |
| [ruff](https://docs.astral.sh/ruff/) | Python (`.ruff.toml`) | undefined or unused names, likely bugs, a text file opened without `encoding=`, syntax newer than Python 3.10 |
| [shellcheck](https://www.shellcheck.net/) | shell scripts | unquoted expansions, mistakes in tests and traps, what `sh` does not have |
| [actionlint](https://github.com/rhysd/actionlint) | the workflows | wrong keys, expressions and types; shellcheck on each `run:` script |
| [zizmor](https://docs.zizmor.sh/) | the workflows, `dependabot.yml` | what an attacker could use: write access a job does not need, a value expanded into a script, a token left in the checkout; with `GH_TOKEN` set (the workflow sets it), a pinned commit that is not the action's own, or a version with an advisory |
| [PSScriptAnalyzer](https://learn.microsoft.com/powershell/utility-modules/psscriptanalyzer/overview) | PowerShell (`PSScriptAnalyzerSettings.psd1`) | syntax Windows PowerShell 5.1 does not have, which every Windows runs `tools/windows-start.ps1` with; likely mistakes |
| `esphome config` | the ESPHome configs `firmware.json` builds, and the examples in [home-assistant-adoption.md](home-assistant-adoption.md) | what ESPHome would refuse to build, checked by the ESPHome image the next build uses (Docker) |

`compile` and `newer python` need only Python; ruff, shellcheck, actionlint and
zizmor come from `requirements/lint.txt`, pinned with their hashes.
The other two are installed apart ([setting up](../CONTRIBUTING.md#setting-up)):
PSScriptAnalyzer with
`pwsh -Command Install-Module PSScriptAnalyzer -Scope CurrentUser`, and
Docker for `esphome config`, which pulls its image the first time. Without
one, the hook skips it and the workflow runs it, but `lint.py all` fails.
`git commit --no-verify` skips them all. `archive/` is linted like the rest.

## The emulators

Each chip family's emulator runs a conversion with nobody at the keyboard
([unattended.md](unattended.md)) on an emulated device of its own, set up as
[emulator.md](emulator.md) says.

### The Espressif emulator

```sh
tools/espressif/emulator/unattended-test.sh --fresh --device=MAC -- --firmware esphome
```

`--instance=N` (1-99) runs several side by side, their eWeLink stages (up to
C2) taking turns on the real cloud ([below](#running-it-espressif)). Every direction is
tested from a device the tool converted, kept with what your computer
recorded about it (`--save`) and started from again (`--from`):

```sh
tools/espressif/emulator/unattended-test.sh --fresh --device=MAC --save=on-tasmota -- --firmware tasmota
tools/espressif/emulator/unattended-test.sh --from=on-tasmota --device=MAC -- --firmware esphome
```

### The Beken emulator

```sh
tools/beken/emulator/beken-emu.sh --fresh device
tools/beken/emulator/beken-emu.sh computer tools/autoflash/cli.py run --unattended --env-file captures/tool.env --firmware esphome
```

The tool runs in the device's own copy of this checkout (`work/`), so the env
file is that copy's `captures/tool.env`, a made-up eWeLink account for the
local stub ([more](emulator.md#using-it-beken); the fault suite writes one there).
`--instance N`, given to both commands, runs several side by side with
nothing shared. A device keeps its flash from run to run until `--fresh`, and
the fault suite's `prep` keeps devices on each firmware to start from
([below](#the-beken-fault-suite)).

## The fault suites

Each emulator has a fault suite, which puts every fault in the way of every
step, in every direction. The two work alike, and are laid out alike.

A scenario converts a saved device and brings its fault while the step under
test is under way, as the run's own events show (MISTIMED if not). The tool is
then run again as a person would, switching the device off and on or joining
its own network where the run says to, until the device is converted or four
runs have failed. A fault that never happened is NOT-FIRED, never a pass.

A run is followed by its events, not its words: `events.jsonl` beside its
`run.log`, a line as each step begins and ends, at points inside a step a
fault is timed from, and as the person is asked to join a network, go back to
their own Wi-Fi, or switch the device off and on (`EVENTS` in
`tools/autoflash/output.py` lists them). Rewording a message cannot move a
fault or leave the stand-in person waiting. A run of an earlier release,
which writes none, is followed by its words.

Both suites run a batch the same way:

- `list` writes every scenario's id, for `batch` or `share`; `list quick` a
  routine run's, with outages only of the shortest and longest length
  (`QUICK_OUTAGES`). The other lengths have found bugs these did not, so run
  them all before a release:

  ```sh
  tools/espressif/emulator/fault_suite.py list quick > quick.txt
  tools/beken/emulator/fault_suite.py list quick > beken-quick.txt
  ```

- `batch` runs what has not passed yet, longest first so as not to end on a
  long one started last, by how long each last took (from the suite's
  results in `captures/emulation/*/`, and those kept from other machines);
  one never run counts as a typical one plus its fault's length.
- `share` splits a list between machines by the same times, so that they end
  together. After the list it takes each machine's number of emulators, and
  `--times-from` for each other machine's suite folders copied here (each
  machine keeps its own). It writes `all.txt.1`, `all.txt.2`, ..., one per
  machine to `batch`, with about how long each takes:

  ```sh
  tools/espressif/emulator/fault_suite.py share all.txt 4 4 6 --times-from second/ --times-from third/
  tools/beken/emulator/fault_suite.py share beken.txt 2 2 2
  ```

- Before a machine that ran a batch is wiped, copy its results back to
  `captures/emulation/kept/NAME/` (`scenario_times.KEPT`), one folder a
  machine, laid out as its `captures/emulation/` is. `batch` and `share` take
  their times from it too, and `found` what it found:

  ```sh
  mkdir -p captures/emulation/kept
  rsync -a --prune-empty-dirs --exclude=/kept/ --include='*/' --include=results.jsonl --exclude='*' \
      HOST:REPO/captures/emulation/ captures/emulation/kept/NAME/
  ```

- `found` says, for each fault and length, how many scenarios have run it
  here and in what is kept, and which of them ever failed with the fault
  landed (`tools/repo/fault_yield.py`): a length that never finds what the
  others do not can go, as the 60 s outage did.

- The next scenario waits, up to half an hour, while this computer's load is
  over 1.5 a core (`tools/repo/cpu_load.py`), and says so: unit tests run
  beside a batch once took a 6-core computer's load to 28, and fresh devices
  never reached eWeLink. Run the unit tests elsewhere or between batches
  anyway.
- A result's notes say how short of memory the computer was, and a failure
  while it was starved, when connections can fail with nothing wrong, is
  marked "memory-starved".

### The Espressif fault suite

`tools/espressif/emulator/fault_suite.py` has 1,559 scenarios on an S60.

#### What it covers (Espressif)

**Directions.** The six conversions between Sonoff's firmware, Tasmota and
ESPHome (`sonoff>tasmota`, `sonoff>esphome`, `tasmota>esphome`,
`tasmota>sonoff`, `esphome>tasmota`, `esphome>sonoff`); `update>tasmota`,
official Tasmota updated from the release before; `update>esphome`, this
project's ESPHome updated from as an earlier release of the tool installed
it; and `nobackup>sonoff`, Tasmota to Sonoff on a computer with no backup,
which puts Sonoff's firmware together with the identity the device kept and
is faulted where it differs, at R1 and R2.

**Steps.** Each step that writes to or waits on the device, K1 (the Sonoff
identity kept at the top of the flash) among them, held to the tool's own by
a unit test.

**Faults at one moment of a step**, named as in scenario ids:

| Fault | What happens |
|---|---|
| `outage` | the network gone (the router off) for 5, 15, 30, 120 or 180 s |
| `weak` | a weak network: one packet in ten lost for a minute |
| `deviceoff` | the device's power off for 1, 5, 30 or 180 s |
| `crash` | the tool and what it serves killed outright, as in a crash, and started again 1 or 180 s later; also 60 s at C2 and where the device raises a network of its own |
| `sleep` | the computer asleep for 10 minutes; also for 2 at the riskiest steps, at C2, L1 and X1, and where the device raises a network of its own |
| `pcoff` | the computer off for 30 s or 2 minutes, then its run started again |
| `ctrlc-once` | Ctrl-C once, which must not stop the run (FAIL-STOPPED) |
| `cancel` | Ctrl-C twice, then carrying on (`continue`), starting again from the beginning (`new`, the tool's `--new`), or choosing the other firmware (`switch`) |
| `hangup` | the terminal window closed: the run must stop (FAIL-NOT-STOPPED), and the next must say why the last one stopped (FAIL-NO-NOTE) |
| `corrupt` | a packet changed on its way, its checksum made good, so only the tool's and the device's own checks can tell; not at R2 (which only sees that Sonoff's firmware takes a connection) or U2 (which asks ESPHome its version and waits), as neither sends anything to change |
| `diskfull` | the disk filling during the backup (C3): the run must never go on without all of it (FAIL-BAD-BACKUP) |
| `fallback` | the device falls back to its own setup network and the person joins it unasked: the run must find it there and carry on |
| `token-login` | at C2, the eWeLink account signed in to again beside the run: each login ends the one before |

A device that loses the home network raises its own at C3 and D1 (the
installer, after 45 s) and at L1, M1, X1 and I2 (the mover and ESPHome,
after 1.5 to 2 minutes).

The tool's own handling is the same at every step, so it is tried only at
C2, L1 and X1, the riskiest steps and where each firmware is first written
from: `second-run`, a second run started while the first works, must be
refused (FAIL-NOT-REFUSED) without disturbing the first (FAIL-DISTURBED);
`ctrlc-apart`, Ctrl-C twice, apart, must not stop the run (FAIL-STOPPED); and
`ctrlc-countdown`, Ctrl-C at the run's question, must stop it at once
(FAIL-NOT-STOPPED).

**When.** At 40% of the step's usual time; at the riskiest steps (D4, E3, F2,
F4, R1, U5 and X2) a 1 s power cut, a 30 s outage and a crash also at 10%
and 90%, and a power cut or an outage twice running (`double`).

**Faults that take two things at once** (`composite_scenarios()`):

- Tasmota sent through the installer's own network, the home Wi-Fi gone from
  the backup, then the run cut off just after the upload (`ownnet-cut`), or
  the computer back on the home network during the upload
  (`ownnet-dropupload`) or as Tasmota is given the Wi-Fi (`ownnet-dropsetup`).
- The installer's answer to an upload that took being lost
  (`lostanswer-once`).
- The device switched off and on 7 times, which erases a Tasmota's settings,
  at D4, F4 and once converted (G1) (`seven-times`), and when the run advises
  it for a Tasmota that cannot join the home Wi-Fi (`seven-advised`).
- F4's Berry driver refused, so that F4 falls back to two restarts
  (`nohook-run`); a power cut or crash at 10, 50 and 90% of F4's reset (step
  `F4rs`); and that fallback cut off by a second's power cut as the device
  restarts to save the template it was sent, which can leave it without one
  (`nohook-cut`, step `F4tp`).

**A device on a temporary table given its own** (step `D5ot`, where the
layout needs one, such as the TH Elite's): a power cut or crash at 10, 50 and
90% of D5 writing its own table back.

**A Berry phase riding out the network by itself** (`blips-3`, `lossy-phase`,
at E2, G1, I1, L1 and R1): two 3 s outages between 10% and 90% of the phase,
further apart than its 20 s of retrying (which count only the time spent
failing), or one packet in ten lost throughout.
The step must not be tried again: a phase started over from its first block,
a pass anywhere else, is FAIL-RETRIED here. Every scenario also keeps how
long each phase kept the device busy (`device_busy`), and `report` lists the
longest, unjudged: a real S60TPG once stopped
answering after 70-90 s busy, though an S60TPF in the test reports was busy
for two minutes without harm.

**Converted devices left as a user's device once was**, then run to each of
the three firmwares:

- **On Tasmota's own safeboot** (`lostapp-factory`). An ESPHome factory image
  (bootloader, partition table and app, for a serial flasher) is uploaded
  through Tasmota's page, as a person who once asked about it had done.
  Tasmota switches to its safeboot for a file that big, the safeboot writes
  it over Tasmota, and the device starts the safeboot from then on. The tool
  must put Tasmota back (FAIL-NOT-PUT-BACK). In `lostapp-switched` the device
  is only switched to its safeboot, Tasmota unharmed, and must be started
  again, never written over (FAIL-REPLACED).
- **Without its template** (`notemplate-generic`). The saved Tasmota is given
  the generic template and an MQTT host, as a person might, so it switches
  nothing. The tool must recognise it (FAIL-NOT-UNTEMPLATED), never
  send it the hub's steps, whose first is an ESPHome update no Tasmota takes
  (FAIL-X1-ON-TASMOTA), and, staying on Tasmota, give it the template alone,
  keeping the MQTT host (FAIL-SETTINGS-LOST).
- **In a boot loop** (`bootloop-fallback`, to Tasmota only). Tasmota started a
  sixth time in a row within 10 s of the last start goes over to its fallback
  module (Generic, unless the device's template is set there) and switches
  nothing. The suite sets that module as Tasmota would, as the emulator loses
  Tasmota's count of starts at every restart; the device must end on its
  template (BAD-TEMPLATE). Restarting it for real would cost its settings
  too: Tasmota counts each start ending before 8 s as a quick power cycle, and
  at the seventh erases everything, its Wi-Fi included.
- **In ESPHome's safe mode** (`safemode-ten`). The saved ESPHome is switched
  off as soon as it answers until it starts in safe mode, as it does after
  ten starts in a row each stopped within a minute (a count kept in flash, so
  the emulator keeps it too). Safe mode, like the mover, has only Wi-Fi
  and the update service, for five minutes. The tool must recognise it and
  wait (FAIL-TAKEN-FOR-MOVER), never take it for a device left part way: on a
  run staying on ESPHome, that mistake sent it the startup firmware.

**Runs left by an earlier release** (`legacy`, on an S60 only, the one device
those releases knew), run from another tree with `unattended-test.sh
--code=DIR`. Such a release keeps its records in `captures/plugs/`, which
your checkout's tool renames to `captures/devices/` as it carries the run on;
the suite reads either.

- `legacy-v1.2.0`: release 1.2.0's tagged files, with only
  `tools/espressif/emulator/legacy/v1.2.0.patch` on top to point them at the
  cloud stub, convert a device fresh from Sonoff, killed at 40% of each step,
  and your checkout's tool runs over the same captures, as after an update.
  At step `start`, a device 1.2.0 converted to Tasmota is taken to ESPHome or
  back to Sonoff (OLD-RUN-FAILED if 1.2.0 did not convert it).
- `legacy-v1.2.0+G1off`: 1.2.0 killed at F4, then the device switched off part
  way through the next run's G1, which writes over 1.2.0's startup firmware
  without checking it and cannot rebuild it, so the run after must carry on
  after the sectors already written.
- `legacy-v2.0.2`: the same for the last release before the firmware the tool
  installs changed, whose runs, their bundle pinned to the earlier firmware,
  your checkout must finish with its own
  ([updating-firmware.md](updating-firmware.md)): killed at each step, to
  Tasmota and to ESPHome, and each kind of device it converted taken on from
  there.
- A run in which that release had already installed its own Tasmota (from F2)
  or ESPHome (from I1) finishes and is updated to your checkout's release in
  the same run: FAIL-NOT-LATEST if the device ends on another (its ESPHome
  release asked of its API).
- `legacy-v1.2.0+U5off`, `legacy-v2.0.2+U5off`: each release killed at F4,
  then the device switched off part way through the update's upload.

**Readouts** (`sonoff>readout`, `tasmota>readout`), timed from the points in
`readout`'s events, as it takes no step of the tool's:

- A device fresh from Sonoff's firmware read out unattended, found as for a
  person who does not know its address; also with the device switched off for
  a second or the network gone for 30 s, at 40% of the read (12 s, as C3
  reads the flash through the same bridge) and of putting Sonoff's firmware
  back. That takes about 10 s in the emulator, the person switching the device
  off and on when asked, 6 s in, so the fault lands about 7 s in, as Sonoff's
  firmware starts.
- The computer crashing mid-read, also with the tool then run, the bridge
  still running, before the device is switched off and on: to Sonoff's
  firmware, to a readout, or to Sonoff's firmware without this computer's
  records of the device's runs, as on another computer. None may be taken for
  an unfinished conversion (FAIL-TAKEN-FOR-CONVERSION).
- After each, the readout is run again as a person would, and **passes** when
  a run kept `readout.tar`, unsent (the suite gives it no API, `--no-api`), holding every file
  [readouts.md](readouts.md#what-is-in-one) lists for an Espressif device,
  the whole flash matching its own hash check (FAIL-READOUT), and the device
  ends answering as Sonoff's firmware (FAIL-NOT-SONOFF, or BRICK if its serial
  output ends as a device that cannot start does).
- A readout of a device on Tasmota, given its address, must keep its first
  stage alone - the firmware eWeLink offers, its manifest naming Tasmota and
  its memory not read (FAIL-READOUT) - end with no failure (FAIL-EXIT), send
  the device nothing (FAIL-SENT), and leave its Tasmota and template as they
  were (FAIL-DISTURBED).

**A readout of every device** (`sonoff>readout-all`, on the stub only):
`readout --all` unattended on a device fresh from Sonoff's firmware, among
made-up devices the stub lists beside it (`AUTOFLASH_EMU_STUB_OTHERS`), which,
not emulated, get only the first stage. Readouts go to the API Worker's own
code ([as `unattended-test.sh` sends them](unattended.md#in-the-emulator)) in
its own container beside the emulated computer, so nothing of the suite's
computer, such as its Node or firewall, comes into it. The accounts:

| Account | Beside the device | Tests |
|---|---|---|
| `mixed` | two other devices | the plain case; also the device switched off for a second, or the computer crashing, at 40% of the device's read, which comes last, stopping the run there; it is run again, as a person would, until one finishes |
| `alike` | three alike in product, firmware model and version, and one not | the API keeps one of the three from one sender |
| `enough` | two others, with the device's product and one other's in the API's `ENOUGH` | those are thanked and not kept |
| `many` | seven | enough first stages to reach the API's limit, lowered to one readout a minute however slowly they come (NOT-FIRED if no run was answered 429) |

Each run is judged by what the API answered and kept, never by its rules,
from its `requests.log` and store, copied beside the scenario's log
(`...-api-N/`) before the next run starts it afresh. It **passes** when, in
every run:

- each device read out, and in a finished run every device in the account, was
  asked `/wanted` once (FAIL-NOT-ASKED);
- every one answered wanted was sent until answered 201, a 429 sent again -
  all but the device a stopped run stopped on (FAIL-NOT-SENT);
- none answered not wanted was sent (FAIL-SENT-UNWANTED);
- each readout answered 201 is in the API's store (FAIL-NOT-STORED), the
  device's holding every file of a whole readout (FAIL-READOUT);
- no readout was kept unsent (FAIL-KEPT-UNSENT);

and the last run finished (FAIL-NOT-FINISHED), and the device answers as
Sonoff's firmware at the end (FAIL-NOT-SONOFF).

**Another device.** A test copy of another device, from its public dump
([adding a device](adding-a-device.md)), runs the suite too, the device taken
from the copy's record and its results in a folder of their own
(`suite-stub-sonoff-th-elite`, say). Where the layout needs a temporary
table, Tasmota to ESPHome goes through the hub, as the tool takes it, with
`D5ot` added and the device's own step lengths. On any device but the S60,
the runs of earlier releases and `update>esphome` are left out. The TH Elite
has 1,621 scenarios.

#### Running it (Espressif)

```sh
tools/espressif/emulator/fault_suite.py --device=MAC prep           # the starting devices (eWeLink)
tools/espressif/emulator/fault_suite.py list > all.txt            # every scenario: cloud, stub or any
AUTOFLASH_EMU_ANY_WORKERS=3 \
    tools/espressif/emulator/fault_suite.py --device=MAC batch all.txt   # what has not passed yet
tools/espressif/emulator/fault_suite.py report
```

`batch` refuses a converted starting device `prep` made with other firmware
than your checkout installs: run `prep` again after a firmware change.

`prep-update` makes the devices the update scenarios (`update>tasmota`,
`update>esphome`) start from, which no run can, as the tool installs only the
current release. For Tasmota it puts the previous release that `firmware.json`
pins, fetched from Tasmota's releases, in the app slot of the device `prep`
saved on Tasmota. For ESPHome it converts a device with the tool as an
earlier release of this project shipped it.

**On another MAC.** To see names made from the MAC follow it, run the suite
on a test copy of a backed-up device with another MAC, given to `--device`,
with saved starting devices of its own (the MAC's last four digits follow the
name). A copy runs against the stub only: the real eWeLink drops a copy as
soon as it registers with a MAC eWeLink's record lacks, so it is never seen
online.

```sh
python -I tools/espressif/emulator/copy_device.py --from MAC --to 12:34:56:ab:cd:ef
python -I tools/espressif/emulator/copy_device.py --remove 12:34:56:ab:cd:ef
```

**On the stub.** `--stub-cloud` on `prep` and `batch` gives each emulator its
own local eWeLink stub instead of the shared real cloud: no account, no
turns, every scenario in parallel
([its workers and folders](cloud-stub.md#running-it)).

```sh
AUTOFLASH_EMU_STUB_WORKERS=3 tools/espressif/emulator/fault_suite.py --device=MAC --stub-cloud prep
AUTOFLASH_EMU_STUB_WORKERS=3 tools/espressif/emulator/fault_suite.py --device=MAC --stub-cloud batch all.txt
```

A stub device's Sonoff firmware, on it and in its backup, trusts the stub's
CA as it was when `prep` made it. `batch` refuses one that does not trust the
CA the stub serves it now (`captures/emulation/stub-cloud*/`, made again only
if gone), as it would turn the stub down, never come online and fail its
checks: run `prep` again, and `prep-update` for an `-earlier` device.

**On the real cloud.** `list` marks each scenario `cloud`, `stub` or `any`. A
real-cloud batch runs only the 75 `cloud` ones, which test eWeLink's own
behaviour: the send through it at C2 at every length of each fault, sending
it again at C3fb, a power cut at C3 and D1 that leaves the device back on
Sonoff's firmware, and a run of an old release killed at C2. The other 180
that need a cloud (`stub`) are readouts, whose send through eWeLink is C2's,
or have their fault after the device has left Sonoff's firmware, where the
stub serves as well. The batch says how many it left out, for
`--stub-cloud batch` to run.

The cloud scenarios run on six emulators (`AUTOFLASH_EMU_CLOUD_WORKERS`; one
per core on a computer with fewer), with the rest's `AUTOFLASH_EMU_ANY_WORKERS`
on top. Eight took as long as six over the 75, their turns at eWeLink coming
one at a time. Use fewer where memory is mostly taken: there, four had their
memory swapped out, and their connections to eWeLink timed out with nothing
wrong.

*One device at a time.* Every emulated device is the same device to eWeLink
([why](cloud-stub.md#why-a-stub)), and each login ends the one before, so:

- A device's way to the internet is shut, its connections reset at both ends
  so eWeLink lets go at once, and opened only for its turn.
- A turn starts when the run is about to need the cloud (the eWeLink steps,
  or the installer sent again), and the run waits for it before logging in
  (`AUTOFLASH_EMULATOR_CLOUD_TURN`, set by the suite); without that wait, a
  run took the device whose turn it was for its own and sent it the
  installer.
- The turn lasts until the device has downloaded the installer and started
  it, typically 30-60 s, as Sonoff's firmware gives up a download when it
  loses eWeLink part way. It also ends with the run, or once the device has
  fetched nothing for a minute since its command went out, as eWeLink does
  not pass a lost command on again; a later run asks again.
- Turns go by how long the run can wait: finding the device in eWeLink,
  then a device back on Sonoff's firmware part way, then the rest, and last a
  C2 scenario's device, whose turn starts before its run. Runs of an old
  release (`legacy`) know no such wait, and hold a turn from the start of
  their first run.
- Between two devices' turns there is a 5 s pause
  (`AUTOFLASH_EMU_CLOUD_COOL_DOWN`) while eWeLink lets go of the last one.
- A run signs in only once its device has held a connection to the internet
  for 15 s, waiting a minute at most, as eWeLink can still count the last
  turn's connection as the device and pass a command down it to no one.
- A fresh device not back within that minute, with nothing yet sent to it, is
  switched off and on once, as a person would, and waited for again
  (`fresh_device_restarted`): such devices had every dial to eWeLink time
  out, and their runs stopped at A1 to ask before the fault came.

The suite can feel out how many the cloud allows, one more while the cloud
scenarios pass (`AUTOFLASH_EMU_CLOUD_MOST`) or one fewer at each trouble
(`AUTOFLASH_EMU_CLOUD_BACK_OFF`), and lengthen the pause when eWeLink is the
trouble (`AUTOFLASH_EMU_CLOUD_PAUSE_UP`).

*One real-cloud run a day.* To eWeLink, a real-cloud batch is one device
reconnecting every minute or two for hours. One run of the 75 is fine, but
after several hundred real-cloud scenarios in a day eWeLink drops the
device's sessions seconds after they open, more and more often, and the
device stays away about five minutes: runs find it offline and stop at
"Discovering the device" (NOT-FIRED), and `connections.log` shows sessions
closing within 15 s with nothing sent. Stop the batch for an hour; run again,
it carries on with what has not passed.

**How many at once.** At most one emulator per core
([why](emulator.md#several-at-once)). A batch started with
`AUTOFLASH_EMU_MOST_WORKERS` above its workers can be scaled while it runs:
only the number written in `workers` in its results directory take a
scenario, so raising it starts more, and lowering it stops the last ones once
their scenario ends.

**Several machines.** Scenarios that need no eWeLink (every step after D1)
run on any machine with Docker, given the repository, the saved devices
(`captures/emulation/saved/`), its part of the list (`share`, above) and
`AUTOFLASH_EMU_NO_CLOUD=1`. Only one machine may let its devices reach
eWeLink.

#### Reading its results (Espressif)

Everything is kept in `captures/emulation/suite/`: the results
(`results.jsonl`), a log per scenario, what each failure leaves (its run
records and the device's serial output, in `<scenario>-evidence`), and every
run's screen recording in `<scenario>-screens/run-N.raw`, all the run drew
where the log has only its last screen (`cat` it in a terminal, or feed it to
`unattended_run.Screen`). A scenario run again keeps its earlier attempts,
numbered in order: `<scenario>.log.1` and `<scenario>-evidence.1` are its
first.

On the real cloud, `connections.log` has every device's connections opening
and closing, with times, looked at every 2 s (`AUTOFLASH_EMU_CONNECTION_LOG`,
0 for none), showing whether two devices were on eWeLink at once, or a
session dropped with no other device about.

A scenario **passes** when the device ends up converted and checked
(Tasmota read back against the official files, ESPHome answering and
confirmed, Sonoff's firmware back on the network) and none of these, which
any conversion can have, applies (others are given with their scenarios,
above):

| Verdict | Means |
|---|---|
| MISTIMED | the fault did not land while its step was under way |
| NOT-FIRED | the fault never happened, so converting proves nothing |
| NO-INTERNET | it never happened because this computer could not reach eWeLink (its name not found, its network down): a batch runs it again, not blaming the cloud |
| NO-TRAFFIC | a packet was to be changed, and the step sent none that could be: a batch does not run it again |
| FAIL | the device was not converted after four runs |
| BRICK | not converted, and its serial output ends as a device that cannot start does |
| NEEDS-JOIN | not converted, the run asking for the device's own network to be joined: right only where the device really is on it, so each is looked at |
| FAIL-UNEXPECTED | a run stopped with "something unexpected went wrong", or whose own outcome classes its stop a crash: an error the tool did not foresee and so a bug whatever the fault, even if the next run converts the device |
| FAIL-CHECKS | the converting run's report says its end-of-run checks failed (below) |
| BAD-TEMPLATE | a device converted to Tasmota lacks its template, so switches nothing |
| BAD-NAME | the device went by a name it never should ([made from its MAC](device-names.md)), at home or as a network of its own, or did not end under its converted name; every name it had is kept with the result |
| FAIL-SCREEN | a run of your checkout's tool left on screen what a stage should have taken away (below) |
| FAIL-ASKED-AGAIN | a run asked for the go-ahead twice, as a step that retried itself once did |
| FAIL-BACKUP-LINE | a run from a converted device reported no Sonoff backup though its converting run took one, or reported one on a computer without |
| FAIL-IDENTITY | a run back to Sonoff with no backup did not report the identity as the device's own |
| LEFTOVER | converted, but the run left a firewall rule, a helper or the tool itself running |
| HARNESS-ERROR | the suite itself failed: its error is kept with the result |

**FAIL-CHECKS.** `Checks: False` in the report means the relay did not switch
or never answered, as on every run back to Sonoff's firmware when it asked
eWeLink to switch the relay before the device had got there; the result keeps
the check's own `Relay` line. On Tasmota and ESPHome the check needs only the
firmware, so `Checks: Not run` fails there too. Back on Sonoff's firmware the
relay is switched, and the meter read, through eWeLink by the device id the
device's record keeps, so `Not run`, for a device with none kept, is no
failure. A run with no report, of a device already on the firmware asked for,
has nothing to judge (`tools/repo/fault_verdicts.py`). Each is read from the
run's own outcome, its line in the run folder's `outcomes.jsonl`, as the run
judged its checks; from the report's words in its log only for a run that
kept none there, an earlier release's.

**FAIL-SCREEN.** Each run is recorded on a terminal of its own, and the
screen it ended on is read back. A stage that passed should leave one line,
its outcome, with only what it kept under it; a stage that stopped should
leave what it showed above its line, as that says why. A run fails on what
[unattended.md](unattended.md#the-recording) lists (an outcome shown twice,
an "Underway" line left), and on two blank rows in a row; the result's notes
name each. The unit tests find most of these first, without the emulator:

| Test | Checks |
|---|---|
| `tests/autoflash/test_route_screens.py` | every route through the run's own loop, to its report and the links under it, at the shortest window it goes on in and at a wide one |
| `tests/autoflash/test_end_screens.py` | every way a run ends (a conversion finished, failed or crashed, in each chip family; a readout that read devices, none, or failed), at 60 to 160 columns, checked as the attended runs check it; each at 100 columns kept in `tests/autoflash/end_screens/`, so a change shows as a difference there |
| `tests/autoflash/test_prompts.py` | every kind of question, asked on a real terminal and answered as a person would, wrongly first, and with keys pressed by accident before it and while it is asked; the same on Windows' console through a stand-in for `msvcrt` (`FakeConsole`), its keys with no character as the console gives them |
| `tests/repo/test_screen_text.py` | each section of text keeps to half the shortest window |
| `tests/repo/test_report_answers.py` | the longest report, read back with the suites' own patterns |
| `tests/autoflash/test_cli.py` | the run-again menu names the stage its line showed |

#### What it cannot show (Espressif)

- **Timings.** The emulated device is slower than a real one, and its flash
  never wears or fails. It cannot measure the one real moment of risk, a
  power cut while the bootloader or partition table is written (well under a
  second).
- **Radio.** Its Wi-Fi is a network cable under a Wi-Fi driver: a weak signal
  is lost packets, not a device at the edge of range.

Every network a device raises of its own (Tasmota's, ESPHome's, the mover's
and the installer's) can be joined from the emulated computer, as from a
person's laptop or phone ([emulator.md](emulator.md)). As on a real computer,
its list of networks keeps one the device put out for a minute after it has
gone or been renamed (the emulator's `nmcli`; a number of seconds in
`/run/autoflash-emu/scan-cache` in the container changes it, 0 for only what
is on air now), so a run sees what a person would: the installer's network
still listed as Tasmota starts, or `ESP_` and the access-point MAC for a
moment as Tasmota names its own.

#### What whole runs have shown (Espressif)

The current code passes the whole suite, as it was before its 60 s outages went: the 1,541 scenarios on the stub, and
the 75 cloud ones against the real eWeLink, where two NOT-FIRED, eWeLink
having dropped the device's sessions, passed when run again after an hour's
pause ([one real-cloud run a day](#running-it-espressif)). Run across several
computers, with the Beken suite and the attended runs beside it, that took
about ten hours.

How long a phase keeps the device busy is measured in sample runs, not in a
whole run. In runs of the drop, packet-loss and weak-signal scenarios, made
once the firmware-writing steps checked each block on the device instead of
sending it back, the longest was 78 seconds, on a weak signal for a minute of
it. That is close to the 70-90 s after which a real S60TPG once stopped
answering ([above](#what-it-covers-espressif)), which is why `report` lists each
scenario's longest.

#### Still open (Espressif)

- **An update command ignored.** Now and then the stock device, back on
  eWeLink after a send cut off part way and a restart, ignores the next
  update command (the run says "the device never connected"), and the run
  after goes through; once, two in a row did. Every such scenario passed when
  run again.
- **A download of the installer that stalls.** Twice, after a short outage
  in the first send to ESPHome, every later download of the installer stalled
  part way. Both passed when run again; it is unexplained.
- **More than one emulator per core** slows the emulated device enough to
  fail with nothing wrong, once losing the tool until it restarted. It
  happened once at one per core too, on a virtual machine whose host was
  busy: after a fault at E2, the device's own requests to the tool stalled at
  G1 for four runs, while it still answered. Run again, it passed.
- **A Tasmota that stopped.** Once, the interim Tasmota, testing the Wi-Fi
  details sent on its setup network, said "Connect failed with AP timeout"
  with the router on, then printed and answered nothing for a quarter of an
  hour, not even restarting when its setup network's three minutes were up.
  It passed when run again, and is unexplained.

#### What it has found (Espressif)

Each fault the suite has found is fixed, with a unit test that fails without
the fix, and its scenario was run again; the [changelog](../CHANGELOG.md)
lists them. They were of a few kinds: a power cut or a stopped run leaving
the flash half written, or the next run unable to carry on; an upload that
stalled and was waited on for good; a device taken for something it was not,
as it restarted or was too busy to answer; the network gone just before a
phase began, or as a firmware first started; trouble reported as something
else ("something unexpected went wrong", or the firewall blamed); and a
stopped run leaving a firewall rule or a safety lock behind.

What they showed about the firmware, which any change must still allow for:

- **The installer must start whatever Sonoff left.** A power cut during D1,
  or a run stopped there, can leave Sonoff's slot half written and unbootable;
  an installer that would not start then left the device on no network, for a
  serial cable to recover. The installer starts regardless, and the run finds
  the device on it.
- **ESPHome sent as an update confirms itself only after a minute running.**
  A power cut before then takes the device back to the firmware before.
- **Tasmota keeps an upload the network cut off.** The next upload hangs
  silently until the device restarts, so the tool restarts it first, also
  before an upload sent again within a step.
- **Just restarted, or busy, Tasmota is not yet itself.** For a moment it
  puts out a network that looks like its setup network (as `ESP_` and its
  access-point MAC, [device-names.md](device-names.md)), cannot yet say which
  slot it runs from, and may not say its template in time. The tool gives a
  device a minute after a restart, or after it is switched on, before
  judging it.
- **Official Tasmota cut off just after F4's reset** is on its own network
  with no Wi-Fi, and is set up through that network.
- **Tasmota keeps Wi-Fi details from a cut-off try.** Sent through the
  installer's own network, it joined the home Wi-Fi by itself with what an
  earlier try had left, and raised no setup network.
- **Sonoff's firmware, written back, tries its network only as it starts.** If
  the network is gone then, it stays off the Wi-Fi until switched off and on,
  which the run asks for when nothing has answered for two minutes.
- **A device whose installer send was stopped part way stays away from
  eWeLink** for a while; the run waits for it, then asks for the device to be
  switched off and on.

### The Beken fault suite

`tools/beken/emulator/fault_suite.py` does the same for the Beken devices, on
emulated ones started from the public S61s dump
(`beken-emu.sh --instance N device`,
[its README](../tools/beken/emulator/README.md)), each on a home Wi-Fi of its
own with the local eWeLink stub, so nothing reaches the internet and every
scenario can run side by side. It has 414 scenarios.

#### What it covers (Beken)

**Directions.** Every direction between Sonoff's firmware, OpenBeken and
ESPHome; from the interim firmware, where a run to ESPHome left a device, to
each (`interim>...`); and an earlier release's OpenBeken and ESPHome updated
(`outdated>...`).

**Faults.** At 40% of each step that writes to or waits on the device: the
device switched off for 1 or 30 s (`deviceoff`), the network gone - the
router off - for 5, 15, 30, 120 or 180 s, as in the Espressif suite
(`outage`), the tool killed and run again after 1 or 180 s (`crash`), and
Ctrl-C (`ctrlc`). Where ESPHome raises its own network when it cannot join
(K2), the router alone off for 150 s (`fallback`): the run sends ESPHome the
Wi-Fi details through that network, a stand-in person joining it as the run
says to and going back after, as the Espressif suite's does. At the steps that write, also a power
cut and a kill at 10% and at 90%, a power cut and another as the tool sends
the step again (`double`), and the router giving the device a new address
once the step is done (`readdress`).

**The bootloader's write.** Each route also has the device switched off 10,
50 and 90% of the way through its bootloader writing the new firmware in
(step `apply`), timed off the device's own output: the moment a real device
is likeliest to be left unable to start.

**An image the bootloader refuses** (`badimage`). Each step that sends an
image the stock bootloader installs - Sonoff's firmware's (B2), and
OpenBeken's fetch of ESPHome (J2, O1) - is given one spoilt once
(`--simulate-bad-image STEP`, one byte of its body changed): it is fetched
whole, but the device starts the firmware it had again, and the tool must
see that and send the image again by itself. At J2 and O1, on OpenBeken, it
is also spoilt once more than the run sends it again (`badimage-restart`),
and the run must then restart the device, as a person switching it off and
on would, and send it afresh.

**The interim firmware.** A device converted through it passes only if, by
the names it asked its router's DHCP for, it was `autoflash-recovery-CDEF`
there from its first request and never OpenBeken's own name, and if, once
ESPHome is on it, the interim's settings are gone from its flash.

**Readouts** (`sonoff>readout`, `openbeken>readout`): the Espressif suite's,
for a Beken device, found by its hardware address as every device is.

- A device fresh from Sonoff's firmware read out unattended; also with the
  device switched off for a second or this computer off its Wi-Fi for 30 s,
  at 40% of the read (about 400 s: the whole 4 MB in 256 paced chunks, where
  B4 reads 8 in 12 s) and at 10% of putting Sonoff's firmware back (100 s, as
  O2 and K3 take: the fault lands while the readout OpenBeken fetches it, the
  part only the readout has); and with the tool killed mid-read.
- After each, as a person would: the device is switched off and on where the
  run says to, Sonoff's firmware put back with the tool
  (`run --firmware sonoff`) where the device was left on OpenBeken, and
  otherwise the readout run again. It passes when a run kept `readout.tar`,
  unsent (the suite gives it no API, `--no-api`), holding every file [readouts.md](readouts.md#what-is-in-one) lists
  for a Beken device and the whole 4 MB flash (FAIL-READOUT), and the device,
  switched off and on, starts Sonoff's firmware and is back in eWeLink
  (FAIL-NOT-SONOFF).
- A readout of a device on OpenBeken must keep its first stage alone, as the
  Espressif suite's of a device on Tasmota (FAIL-READOUT, FAIL-EXIT), serving
  the device nothing (FAIL-SENT), which must still answer on OpenBeken
  (FAIL-DISTURBED).

#### Running it (Beken)

```sh
tools/beken/emulator/fault_suite.py prep      # the starting devices: on the interim firmware, OpenBeken, ESPHome
tools/beken/emulator/fault_suite.py prep-update   # and on an earlier release's OpenBeken and ESPHome
tools/beken/emulator/fault_suite.py list > beken.txt
AUTOFLASH_BEKEN_EMU_WORKERS=2 tools/beken/emulator/fault_suite.py batch beken.txt   # what has not passed yet
tools/beken/emulator/fault_suite.py report
```

`prep-update` makes the update scenarios' devices through the earlier
release's own suite. `list openbeken` gives the six scenarios a new OpenBeken
build must pass ([updating-firmware.md](updating-firmware.md#5-test)).
`check` runs each final firmware with no faults, installed from Sonoff's
firmware and then each taken to the other, and holds each way to the
end-of-run checks too, on emulators 1 and 2 unless `--instances` names two
others. It is what `tools/repo/emulator_check.py DEVICE` runs for each Beken
device: the S61 from its public dump, the Basic Gen5 from a flash rebuilt
from that dump and the Basic Gen5's official file, each with its logs in
`captures/emulation/device-check/DEVICE/`. To check both on one computer at
once, give one of them `--instances 3,4`.

Each emulated device takes a core, so two workers suit a laptop. Sonoff's
firmware restarts by itself once it has its update, in the emulator as on a
device, so the tool asks for a switch off and on only after a fault.

#### Reading its results (Beken)

Results are in `captures/emulation/beken/suite/`
(`AUTOFLASH_BEKEN_EMU_SUITE_DIR` names another folder). A scenario passes
when the run's record has every step of its route done (FAIL otherwise, as
for the interim firmware, above) and none of these applies:

| Verdict | Means |
|---|---|
| FAIL-CHECKS | the report's end-of-run checks did not pass, as on the Espressif suite. On OpenBeken and ESPHome, whose relay pin drives the emulated meter's load, the check of a device with a meter (the S61) must also say the meter confirms the switch, and one of a device without (the Basic Gen5) only that the relay switched, as its kind in the run's record says; back on Sonoff's firmware the relay is switched through the stub's eWeLink |
| FAIL-SCREEN | as on the Espressif suite, each run recorded through `tools/repo/unattended_run.py` |
| FAIL-NOT-LATEST | a device converted to OpenBeken or ESPHome, updated from an earlier release's or not, does not run your checkout's release of it |
| FAIL-UNEXPECTED | as on the Espressif suite: a run stopped by an error the tool did not see coming, even if the next run converts the device - by its words, or by the run's own outcome classing its stop a crash (`tools/repo/fault_verdicts.py`, both suites') |
| MISTIMED, NOT-FIRED | as on the Espressif suite |
| NOT-READY | the emulated device could not be started for the scenario |

#### What it cannot show (Beken)

- **Timings.** The emulator runs the real firmware far slower than a chip, so
  a step's length, and the moment of risk as the bootloader writes the new
  firmware in, are its own, not a real device's.
- **Radio.** The device's Wi-Fi is a TAP device bridged onto a Docker
  network: a fault takes the computer off it, with no signal to weaken.
- **The wiring.** Only the meter is wired, with no relay or button, so a pass
  says the firmware reads a meter on the pins its config names, not that
  those are the device's ([emulator.md](emulator.md#known-gaps-beken)).

#### What whole runs have shown (Beken)

The current code passes every scenario, every direction end to end in the
emulator, faults and all, from the public dump of a real S61: a whole run of
the 291 it had, and the longer outages and K2's fallback added since.

#### Still open (Beken)

- **A real device.** No real Beken device has been converted, so whether one
  answers as the emulator does (OpenBeken's `/api/flash` returning its raw
  flash, and Sonoff's checks of a rebuilt image) is not known
  ([Beken research](beken-research.md#questions-still-open)).

#### What it has found (Beken)

Each fault found is fixed, and its case is in the suite. What they showed
about the firmware and the emulator:

- **ESPHome holds an update cut off part way for up to 105 s,** its update
  service taking nothing else meanwhile, so a run started again in that time
  times out. The tool waits that out once, then sends the update again
  (`outdated>esphome:U1@10:crash-1`).
- **An OpenBeken just restarted cannot be told restarting again by its
  uptime.** Restarted by the stock bootloader refusing ESPHome, it has been up
  for less than it takes to start again, so the tool also takes it going
  quiet once asked, then answering, as a restart
  (`sonoff>esphome:J2:badimage-restart`). In the emulator, uptime cannot be
  set against the time since the device was asked either: the emulated
  device's time runs faster than the wall's, so its uptime outgrows the time
  the tool counts, which a real device's does not (`step_esphome_check` in
  `tools/autoflash/routes/beken/arrived.py`). This is apart from the
  firmware's own clock falling slightly behind the emulated device's time
  ([the emulator's README](../tools/beken/emulator/README.md)).

### Attended runs

The fault suites run the tool unattended, never seeing its questions. The
attended runs answer each as a scenario says, often wrongly first where a
wrong answer can be given, and judge the screen at every answer and at the
end.

```sh
tools/espressif/emulator/attended_suite.py list
tools/espressif/emulator/attended_suite.py run [NAME ...] [--workers 3] [--first-instance 61] --device MAC
```

A scenario is a file in `tools/espressif/emulator/attended/` (an S60's) or
`tools/beken/emulator/attended/` (an S61's), saying where the device starts,
what is done to it first (a Tasmota web password, an ESPHome with a web login
and encrypted updates, sudo remembering no password), the run's options, the
answers, and how the run should end, with a second run after one stopped
part way (`again`), as you would start it again. An answer can also switch
the device off (`"power": "off"`), as if unplugged part way, switch the home
Wi-Fi's router (`"router": "off"`, or on an S61's `"on NEW-PASSWORD"`, its
password changed), take the computer onto the device's own network and back
as the run asks (`"wifi": "join"`, `"home"`; `"after": SECONDS` waits that
long once its text shows, as a person waits for a network to come up), or
signal the
run (`"signal": "TERM"`, or `"HUP"` for its window closed) as it waits at a
question; a scenario can be a readout (`"command": "readout"`), and have the
API beside the emulator hold enough of the device's product already
(`"api_enough": true`), so its readout is not needed. A run is made to fail
as a refused image fails it, for its failed ending to be seen, with the
run's option `--simulate-bad-image STEP[:N]`: the image step STEP sends,
spoilt the first N times - an S61's at B2, J2 or O1, refused by its
bootloader; an S60's at D1, the Berry Tasmota, refused by the installer.
Each runs on an emulator of its own, `--workers` at once; the S61's run after
the S60's, one at a time. Their instances are numbered from `--first-instance`
(61), one per worker, and reused, each scenario starting its device afresh: a
run keeps clear of the instances it was not given.

A run passes when it ends as the scenario says with every answer used;
otherwise it is:

| Verdict | Means |
|---|---|
| FAIL-RESULT | it ended with another exit code, or its report another Result (as the run's log has it: `result.txt`) |
| FAIL-UNANSWERED | it stopped at a question no answer covers: new, or reworded |
| FAIL-UNUSED | an answer was never typed: its question is no longer asked |
| FAIL-ANSWER | an answer that switches the device or the router, or moves the computer's Wi-Fi, failed to: the network not on the air to join, say |
| FAIL-QUESTION | something was wrong as a question was answered: no "(q to quit)" on it, a row the terminal wrapped itself, or a question shown twice (an earlier try left on the screen) |
| FAIL-SCREEN | the screen it ended on: as the fault suites' FAIL-SCREEN, and how it ended (`unattended_run.end_problems`) - the report's columns lined up and balanced, the links to support the project or the way to open an issue under it, the report ID and what was put on the clipboard as a failed run says them and nowhere else, no question left unanswered above what came after it - a row the terminal wrapped itself, or a line the scenario's `expect.shows` wants not there, or one its `expect.never` wants nowhere (at any answer or at the end: a key pressed by accident, echoed) |
| HARNESS | it could not be run - the device not prepared, say: its `harness.log` says why |

Results and recordings go to `captures/emulation/attended/<time>/`.
`tests/espressif/emulator/test_attended_suite.py` checks every answer still
matches a question the tool asks, so a reworded question fails in the unit
tests first.

### Emulator stats

Every suite - fault, attended and the final-firmware check, of every chip
family - sends what each scenario's run came to to the admin view
(`website/admin/emulator.js`): the device, the scenario, its fault and the
fault's length or argument, its verdict, whether it is a first try or a
re-run, how many runs of the tool it took and how long, why each run that
stopped did, as the run itself classed it in its report (the
`outcomes.jsonl` each run adds a line to in its folder,
`tools/repo/fault_verdicts.py` `run_outcomes`), the
version of the tool, the commit it ran from and the emulator image's tag, the
firmware the checkout installs on the device, whether the computer was short
of memory, and, on a run the suite could not judge, its own error by
exception class alone. As a batch begins it sends how many
scenarios its suite has for the device and how many it runs. No log, report or
recording leaves the machine.

The view takes each verdict as an outcome: a pass, a failure, or
inconclusive - a run the suite could not judge, such as a fault that never
landed or a device that did not start (`INCONCLUSIVE` in
`website/api/emulator.js`). Pass rates are of first tries the suite could
judge, as a batch runs only what has not passed yet and its re-runs would
read as a worse rate. The view shows runs a day; pass rates and new failures
(a scenario that passed on the commit before) by commit; each device, version by
version of the tool, with the share of its suite's scenarios passed on that
version - each version on its own, as a change may break what passed on the
one before - and the firmware it ran; failures by step and fault; what each
fault found at each length (a failure, or a pass that needed a run again after
one stopped: what `fault_suite.py found` says of one machine's results, for
every machine's); why runs stopped, by step - a bug a pass hides shows there;
first tries by emulator image; how many tries a conversion took; the test
rig's own trouble (the inconclusive runs) apart from the
tool's; flaky scenarios; real-cloud failures by the hour into a batch; and the
latest runs that did not pass, each of which you mark with what it came from
and the commit or issue that fixed it or follows it - "tool" counts as a bug
found, once a commit or issue. Days before today are counted each night;
`?live=1` has the view load itself again every minute. The public
[figures](https://firmwareliberator.com/stats) show each device's share of
scenarios passed on the newest version and on each before it, from checkouts
with no changes alone: how far a device in testing has got.

`tools/repo/emulator_stats.py` keeps each run beside the suite's results and
sends it as soon as it finishes, beside the suite, so the view fills as runs
end and a machine thrown away part way through a batch has sent what it ran.
The batch sends whatever is left as it ends; a run that could not be sent
goes with the next, or with `emulator_stats.py send DIR`. Sending needs the
suites' service token, in `~/.config/firmware-liberator/emulator.env` - one
file a machine, which every checkout and worktree on it reads.
`tools/repo/cloudflare_settings.py apply` writes it, sets it as the
repository's secrets for the final-firmware check on `main` (a pull request's
check sends nothing), and copies it to each machine `EMULATOR_STATS_HOSTS` in
`tools/repo/.env.infra` names (`website/README.md`, The admin view);
`emulator_stats.py push HOST...` copies it to another, and
`emulator_stats.py check` says whether a machine's runs are taken. Without
it nothing is sent: the first batch says so as it begins, so that you copy it
there before the machine is thrown away, and each batch again as it ends. The view takes at most
`DAILY_RUNS` runs a day, so a token that leaked could add only so many.
`AUTOFLASH_EMU_NO_STATS=1` keeps and sends none, as every unit test has it.
A suite of a new chip family calls the same functions (`began`, `fault_run`,
`attended_run`, `check_run`, `send`); `tests/repo/test_emulator_stats.py`
checks that every `tools/*/emulator/fault_suite.py`, and each family's
final-firmware check, does.

## Real devices

The S60 is the only device tested for real, and the only one
[Supported](../devices/README.md); every other is tested in the emulator alone,
and no real Beken device has been converted
([still open](#still-open-beken)). A real device under test is powered through a
second smart plug, so its power can be cut on purpose. Tasmota to ESPHome and
back has run on a real S60TPG: every write read back, the final check of the
device on Tasmota matching all four regions, and the relay and power meter
checked through each firmware.

## Related

[Fixes sent upstream](upstream-contributions.md): the bugs this testing found
in other projects (Tasmota, arduino-esp32, ESP-IDF and others), and the fixes
sent.
