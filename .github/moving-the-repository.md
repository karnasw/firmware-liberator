# Moving the repository

For the repository's owner: what to change when the repository moves to an
organisation or another account, so that nothing is left naming the old one.

1. **Transfer it** with GitHub's transfer, which takes the issues,
   discussions and releases with it. GitHub then redirects every old address
   - pages, `git`, release downloads - for as long as no repository takes the
   old name. So keep the old account, and never create a `firmware-liberator`
   under it again: published firmware and one-line installs depend on that
   redirect.
2. **Change the address, in one commit.** It is written once, as `REPOSITORY`
   in `tools/repo/github_settings.py`. Then change every copy that
   `git grep -n "<old owner>/firmware-liberator"` finds outside `archive/`
   and outside released sections of `CHANGELOG.md`. Three kinds matter most:
   - Each device's `esphome.yaml`, whose `package_import_url` is where Home
     Assistant fetches the config from. Firmware already published keeps the
     old address, through the redirect.
   - The one-line installs: `liberate.sh`, `liberate.ps1` and
     `website/public/_redirects`.
   - The emulator images (`image.sh` in each emulator's folder). Packages on
     ghcr.io stay with the account that published them and are not
     redirected. The image workflows publish under the new owner by
     themselves, so run each once.

   `tests/repo/test_repository_address.py` fails on GitHub until `REPOSITORY`
   names where the repository is, and it and `tests/repo/test_site.py` fail
   until the copies that code reads agree with `REPOSITORY`.
3. **Leave these as they are:** `EARLIER_ESPHOME_NAMESPACES` in
   `tools/autoflash/kinds.py`, which only names the old firmware; the
   sponsorship links (`.github/FUNDING.yml`, `tools/autoflash/report.py`),
   which are a person's, not the repository's; the authors in the emulators'
   patches; and the private backup's package (`OWNER` in
   `tools/repo/private_backup.py`). Move that package only by uploading it
   again under the new owner, private
   ([private-firmware-backup.md](private-firmware-backup.md)).
4. **After the push**, run `python3 tools/repo/github_settings.py`. It says
   whether every setting came across, the `website` environment's secrets
   included; `... apply` sets what did not, and `... secret NAME` a secret.
5. **Check that a release still verifies** (`gh release verify`).
