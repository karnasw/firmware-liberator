# The maintainer's sealed Sonoff backup

This page is for the project's maintainer. Nobody converting a device needs it,
and nothing here is part of a conversion.

The emulator ([emulator.md](../docs/emulator.md)) boots a real device's Sonoff
backup: Sonoff's firmware, with that device's eWeLink keys and the Wi-Fi it
was set up on. The project has exactly one, and can never make another, as
every device it had is converted.
[tools/repo/private_backup.py](../tools/repo/private_backup.py) keeps a copy
of it off the maintainer's computer without it ever being public: sealed on
the maintainer's side, then kept in a **private** package on GitHub's
container registry. It is not in the repository - not its history, a clone,
a fork or a release - so [legal.md](../docs/legal.md)'s "no Sonoff firmware
is in the repository" stays true.

The script holds the sealed file's format, the package's name and each
command (`private_backup.py --help`). This page holds what a person has to
know and do.

## What is sealed

The backup directory as a conversion filed it,
`captures/devices/<MAC>/sonoff-backup/`, and the device's record beside it
(`device.json`), which the emulator's cloud stub needs. They are compressed,
then encrypted with a key only the maintainer has. Sealing proves the result
opens to the backup before it writes it, and opening checks every file
against the manifest sealed with it. Only the sealed file ever leaves your
computer: `upload` refuses anything else. Nothing writes over a file or
directory that is there already, and `unseal` leaves each file read-only, as
the backup is kept.

The firmware in `artifacts/` is not compressed, and does not need to be: Git
already stores every file compressed, and the tool and its tests use those
files byte for byte as they are.

## The key

`private_backup.py key` makes a random key once, writes it into `.env` as
`AUTOFLASH_BACKUP_KEY` and never shows it. Without it nobody can open the
sealed backup, so keep a copy **off your computer**, in a password manager: a
`.env` lost with the computer takes the original backup and the only key with
it. A new key is never written over an old one, which would leave the sealed
copy unopenable.

A `.env` that holds the key still loads for a conversion, which keeps the
value out of the log, and the emulator never hands it to a run.

## Where it is kept

In a private package owned by the maintainer's account, which is not linked
to the repository. A package made public cannot be made private again, so
never change its settings.

Uploading and fetching need Docker and the GitHub CLI's login, which needs
the package scopes once:

```sh
gh auth refresh -h github.com -s write:packages,read:packages
```

## Sealing and uploading

```sh
B=captures/devices/<MAC>/sonoff-backup
python3 tools/repo/private_backup.py seal "$B" sonoff-backup.sealed     # the sealed copy
python3 tools/repo/private_backup.py verify sonoff-backup.sealed "$B"   # opens to exactly it? writes nothing
python3 tools/repo/private_backup.py upload sonoff-backup.sealed        # into the package, then checks it is private
python3 tools/repo/private_backup.py check-private                      # at any time
```

`check-private` calls the package private only if three things agree: the
registry refuses it to a request made as nobody, having first handed over a
public package so that the check is shown to tell the two apart; GitHub's own
setting for it says private; and the package is there under its tag.
`upload` runs it straight after uploading.

## Restoring

When the emulator finds no Sonoff backup in `captures/devices/`, it runs
`restore` itself: the sealed backup is fetched, opened and filed where a
conversion files one, with the device's record beside it. A copy already
there is used as it is, and one that differs from the sealed backup is never
written over: the emulator stops and says so. A test copy asked for with
`--device` is then made from it, so every emulator run can start from an
empty `captures/`. On the maintainer's computer that makes `captures/` a
working copy, safe to delete
([technical-details.md](../docs/technical-details.md#what-captures-holds)).

By hand:

```sh
python3 tools/repo/private_backup.py restore                            # filed where the emulator looks
python3 tools/repo/private_backup.py fetch sonoff-backup.sealed         # the sealed file alone
python3 tools/repo/private_backup.py unseal sonoff-backup.sealed OUT    # the backup, into a new directory OUT
```

### If your computer is lost

1. On any computer, clone the repository, install Docker, and log the GitHub
   CLI in as the maintainer, with `read:packages`.
2. Put the key from the password manager in `.env` as `AUTOFLASH_BACKUP_KEY`.
3. Run the emulator, which restores the backup on its first run, or run
   `restore`.
4. Check that its `flash.bin` has SHA-256 `4b6e3393…9f9bcc40`, as every copy
   has.

## On GitHub's side, one day

Nothing opens the backup on GitHub's side today. It is needed only to make
the public starting devices again
([emulator.md](../docs/emulator.md#public-devices)) and for the whole fault
suite, which run on the maintainer's machines. A workflow that opens it (on
runners of the maintainer's own, say) needs:

- the package to give the repository access (its settings, "Manage Actions
  access": read), and the workflow `permissions: packages: read`, so that
  `fetch` reaches it with the workflow's `GITHUB_TOKEN`;
- the `AUTOFLASH_BACKUP_KEY` secret, which `private_backup.py secret` sets
  from `.env` (GitHub never shows a secret back, so that copy is not one to
  recover the key from);
- to run only on a schedule or by hand (`workflow_dispatch`), from `main`,
  and never on `pull_request_target`, which would run a contributor's code
  with the repository's secrets and could open the backup. A pull request
  from a fork never gets the repository's secrets: once the package gives
  the repository access, a workflow that such a request adds could fetch the
  sealed file, but never the key to open it. Keep "require approval" for
  outside contributors' workflows on (the repository's Actions settings).
  Forks get no packages at all.

The workflow unseals into the runner's own `captures/` and nowhere else, and
publishes nothing made from it but what `sanitise_device.py` has shown
public.
