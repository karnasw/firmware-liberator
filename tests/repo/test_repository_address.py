"""The repository's address, written once as github_settings.REPOSITORY, is
where the repository is: every ESPHome image carries it as the address Home
Assistant adopts the device's config from, so a move must change it, and every
copy of it with it."""

import json
import os
import re
import unittest
from pathlib import Path

from tools.espressif.emulator import public_devices
from tools.repo import private_backup
from tools.repo.github_settings import REPOSITORY

ROOT = Path(__file__).resolve().parents[2]
ADOPTION = re.compile(r"github://([^/\s]+/[^/\s]+)/")


def running_in() -> str | None:
    """The repository a GitHub Actions run is in, None off GitHub or in a fork:
    a fork's runs are under its own name, the repository not moved."""
    repository = os.environ.get("GITHUB_REPOSITORY") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    try:
        with open(os.environ.get("GITHUB_EVENT_PATH", ""), encoding="utf-8") as event:
            if json.load(event).get("repository", {}).get("fork"):
                return None
    except (OSError, ValueError, AttributeError):
        pass
    return repository


class RepositoryAddressTests(unittest.TestCase):
    def test_it_is_where_the_repository_is(self):
        # GitHub keeps redirecting the old address after a move - until
        # someone takes the name - so nothing else would show it.
        found = running_in()
        if found is None:
            self.skipTest("not on GitHub Actions in the repository itself")
        self.assertEqual(found.lower(), REPOSITORY.lower(),
                         f"the repository is {found} now: .github/moving-the-repository.md "
                         "says what to change")

    def test_every_adoption_address_names_it(self):
        configs = sorted(ROOT.glob("devices/**/esphome.yaml"))
        self.assertTrue(configs)
        for path in [*configs, ROOT / "docs/home-assistant-adoption.md"]:
            text = path.read_text(encoding="utf-8")
            found = set(ADOPTION.findall(text))
            with self.subTest(path.relative_to(ROOT)):
                if path.suffix == ".yaml":
                    self.assertRegex(text, r"(?m)^  package_import_url: github://")
                self.assertEqual(found, {REPOSITORY})

    def test_every_copy_of_it_is_the_same(self):
        self.assertEqual(public_devices.REPOSITORY, REPOSITORY)
        self.assertEqual(private_backup.REPOSITORY, REPOSITORY)
        # The website's own copy: tests/repo/test_site.py.


if __name__ == "__main__":
    unittest.main()
