# For AI assistants working on this repository

What an assistant needs to know, beyond [CONTRIBUTING.md](CONTRIBUTING.md).

## Private things never reach the repository

The repository is public; the tool handles Wi-Fi passwords, eWeLink accounts
and devices' identities.

- **Never read or change `.env`.** **`captures/` is private.**
- **Examples use made-up identifiers**, e.g. MAC `12:34:56:ab:cd:ef`, address
  `192.0.2.50` (`192.168.99.50` where people read it) - never a real device's,
  network's or account's.
  [Addresses](CONTRIBUTING.md#keeping-private-things-out)
- **The leak scan** (`tools/repo/leak_scan.py`, run by `.githooks/`) stops a
  commit or push holding one: take it out, never `--no-verify`. A new
  made-up example goes in `tools/repo/leak-scan-allow`.
  [More](docs/technical-details.md#keeping-secrets-out-of-git)
- **A device's stock Sonoff backup cannot be made again**: never overwrite,
  move or delete one.
- **No third party's proprietary firmware** (Sonoff's images, a device's dump)
  is committed, released or built into a published image. Tooling that
  needs one fetches it at run time from where it is published, as
  `tools/beken/emulator/beken-emu.sh dump` does.

## Real devices and the emulator

- **Ask before doing anything to a real device**: a conversion is not undone
  by running it again.
- **Stop each emulator** (`docker stop`) when a test batch ends, and run no
  more at once than there are cores, or they fail falsely ([more](docs/emulator.md)).
- **Each emulator image's tag hashes** (`image.sh tag`)
  `tools/espressif/emulator/`'s `container/`, `patches/`, `emu.Dockerfile` and
  `UPSTREAM`, and `tools/beken/emulator/`'s `UPSTREAM`, `requirements.txt`,
  `patches/` and `beken-emu.Dockerfile`: any change, a comment included, means
  a new image everywhere. Change them only for the emulator's sake;
  `tests/repo/test_emulator_images.py` says what else goes with the next.

## Git

- **Commits are signed**, rewritten ones too.
- **One cohesive change per commit**, its message saying what and why. A
  fix, test or doc change to unpushed work goes into the commit it belongs
  to; an unrelated bug found along the way gets a commit of its own.
- **Stage only what you changed**, file by file - never `git add -A` or
  `git commit -a`: someone may be editing the same checkout.
- **Work in a worktree of your own** (`git worktree add`) and land on `main`
  with `git merge --ff-only`: other sessions commit in the main checkout.
- **Before rewriting history**, make a backup branch
  (`backup/<branch>-before-<what>-<date>`); afterwards `git diff <old tip>
  <new tip>` is empty unless a change was meant.
- **No attribution** (`Co-Authored-By`, `Generated with`, ...) in a commit,
  pull request or comment.
- **Never push or force-push unasked.** Pushing `main` with a new version in
  `tools/autoflash/__init__.py` is a release.

## GitHub

- **The repository is `adamsthws/firmware-liberator`**: always pass
  `-R adamsthws/firmware-liberator` to `gh`. Moving it:
  [.github/moving-the-repository.md](.github/moving-the-repository.md).
- **Its settings** live in `tools/repo/github_settings.py` and are set with
  it, never by hand. Renaming a job a required check names: `apply`, push,
  `apply`.
- **Workflows pin each action to a commit**, version beside it; Dependabot
  moves them a week after a release. CI stays fast: Linux runners only.
- **A pull request or issue opened in another project** is recorded in
  [docs/upstream-contributions.md](docs/upstream-contributions.md) the same
  day: project, link, one line on what it fixes.
- **Editing anything already posted** keeps what it replaces: `~~old~~`
  then the new, new parts marked **Added:**, and an **Edit:** note saying
  what changed and why.

## Code and tests

- **Python's standard library only**, 3.10 or newer. Open text files with
  `encoding=`. Packages go only into `.venv`, never the system's.
- **The chip families behave alike**: a feature or fix for one family's
  route is made for every family it applies to, the same way; a difference
  that must stay says why beside it.
  [More](CONTRIBUTING.md#code)
- **Take the idiomatic design**: the usual way of the language and the code
  around it, over a clever one.
- **Before a commit**: `python3 tests/run.py` and `python3 tools/repo/lint.py`
  on what changed; before a push, `python3 tools/repo/oldest_python.py`.
  [docs/testing.md](docs/testing.md)
- **Every fix has a test that fails without it.** A failing test or hook is
  fixed at its cause, never skipped, loosened or deleted.
- **A test of one module mirrors its path**: `tools/a/b.py`'s tests are
  `tests/a/test_b.py`, merged there rather than beside it under another
  name. A test across modules, or of the whole repository, is named for
  what it holds: in `tests/repo/`, or listed in `NOT_MIRRORED` in
  `tests/repo/test_layout.py` with why. [More](docs/testing.md#where-a-test-goes)
- **Tests run side by side and in any order**: each leaves nothing behind,
  uses a port it picked, and touches nothing computer-wide.
- **Where things go**: `tools/autoflash/` the tool, `tools/repo/` the
  repository's plumbing, `tools/espressif/` and `tools/beken/` each chip
  family's tooling, `tools/ewelink/` eWeLink, `devices/<device>/` one device's
  own, `tests/` mirroring `tools/`, `docs/` prose for users and contributors,
  `.github/` the maintainer's own pages (servers, API, accounts; not on the
  website), `website/` the site built from the rest, `archive/` retired work
  (still linted). A device's ESPHome config path is its `package_import_url`:
  moving one means a new image.

## Firmware and releases

- **`artifacts/`** changes only through `tools/repo/update_firmware.py`.
  [docs/updating-firmware.md](docs/updating-firmware.md)
- **A release**: its `CHANGELOG.md` section, then the version in
  `tools/autoflash/__init__.py`; the release workflow tags it once tests
  pass on `main`. A published version never changes and is never redone:
  fix forward.

## Writing

Docs, comments and commit messages: plain, compact sentences saying what
and why, for someone who was not there. Comments as sparing as the code
around them. Docs speak to their reader, not from our side:
[CONTRIBUTING.md](CONTRIBUTING.md#writing-docs).

**Specifics live in the code**: a value, limit, version, permission or list
is written once, in the code with a comment beside it; docs name the
constant or file instead of copying it, and the tool prints what someone
needs to act ([CONTRIBUTING.md](CONTRIBUTING.md#specifics-live-in-the-code)).

**Record findings as you go, unasked**: what research or a test shows goes
into the docs in the change it comes from, a page it makes stale is corrected
then, and an open question answered is closed
([CONTRIBUTING.md](CONTRIBUTING.md#recording-what-you-find)).
