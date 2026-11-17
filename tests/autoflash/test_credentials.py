"""The answers an unattended run is given in advance (tools/autoflash/credentials.py)."""
import os
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import credentials, output


class CredentialsFileTests(unittest.TestCase):
    def setUp(self):
        self.addCleanup(credentials._values.clear)
        self.addCleanup(credentials.renamed.clear)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [*credentials.KEYS, *credentials.RENAMED]:
            os.environ.pop(key, None)

    def load(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(textwrap.dedent(text), encoding="utf-8")
            return credentials.load(path)

    def test_shell_style_lines_are_read(self):
        given = self.load("""
            # the account
            export EWELINK_EMAIL = me@example.com
            EWELINK_PASSWORD="pass word#1"
            AUTOFLASH_SUDO_PASSWORD=
        """)
        self.assertEqual(given, ["EWELINK_EMAIL", "EWELINK_PASSWORD"])
        self.assertEqual(credentials.get("EWELINK_PASSWORD"), "pass word#1")
        self.assertIsNone(credentials.get("AUTOFLASH_SUDO_PASSWORD"))  # empty is not given

    def test_the_wifi_for_a_device_on_esphome_can_be_given(self):
        given = self.load("AUTOFLASH_WIFI_SSID=Home-2G\nAUTOFLASH_WIFI_PASSWORD=a pass\n")
        self.assertEqual(given, ["AUTOFLASH_WIFI_PASSWORD", "AUTOFLASH_WIFI_SSID"])
        self.assertEqual(credentials.get("AUTOFLASH_WIFI_PASSWORD"), "a pass")

    def test_a_file_read_again_holds_only_what_it_says_now(self):
        self.load("EWELINK_EMAIL=me@example.com\nEWELINK_PASSWORD=old-pass\n")
        self.assertEqual(self.load("EWELINK_EMAIL=me@example.com\n"), ["EWELINK_EMAIL"])
        self.assertIsNone(credentials.get("EWELINK_PASSWORD"))

    def test_the_default_file_is_the_one_at_the_repositorys_root(self):
        self.assertEqual(credentials.ENV_FILE, Path(__file__).resolve().parents[2] / ".env")

    def test_the_environment_wins_over_the_file(self):
        self.load("EWELINK_EMAIL=file@example.com\n")
        os.environ["EWELINK_EMAIL"] = "shell@example.com"
        self.assertEqual(credentials.get("EWELINK_EMAIL"), "shell@example.com")

    def test_a_misspelt_name_is_refused_rather_than_ignored(self):
        with self.assertRaisesRegex(ValueError, "EWELINK_PASSWRD"):
            self.load("EWELINK_PASSWRD=x\n")
        with self.assertRaisesRegex(ValueError, "line 1"):
            self.load("just a password\n")

    def test_no_default_file_is_fine_but_a_named_one_must_exist(self):
        with mock.patch.object(credentials, "ENV_FILE", Path("/nonexistent/.env")):
            self.assertEqual(credentials.load(), [])
        with self.assertRaisesRegex(ValueError, "does not exist"):
            credentials.load(Path("/nonexistent/.env"))

    def test_the_passwords_are_kept_out_of_the_log(self):
        self.addCleanup(output._private.clear)
        self.load("EWELINK_PASSWORD=hunter2-secret\nAUTOFLASH_SUDO_PASSWORD=sudo-secret\n")
        self.assertEqual(output.scrub("x hunter2-secret sudo-secret"),
                         "x <ewelink-password> <sudo-password>")

    def test_a_dotenv_with_the_names_from_before_the_prefix_still_works(self):
        # Written for a checkout from before AUTOFLASH_: read, and noted for renaming.
        self.addCleanup(output._private.clear)
        given = self.load("EWELINK_EMAIL=me@example.com\nS60_SUDO_PASSWORD=sudo-old-secret\n"
                          "S60_WIFI_SSID=Home-2G\nS60_WIFI_PASSWORD=wifi-old-secret\n")
        self.assertEqual(given, ["AUTOFLASH_SUDO_PASSWORD", "AUTOFLASH_WIFI_PASSWORD",
                                 "AUTOFLASH_WIFI_SSID", "EWELINK_EMAIL"])
        self.assertEqual(credentials.get("AUTOFLASH_SUDO_PASSWORD"), "sudo-old-secret")
        self.assertEqual(credentials.renamed, ["S60_SUDO_PASSWORD", "S60_WIFI_PASSWORD", "S60_WIFI_SSID"])
        self.assertEqual(output.scrub("sudo-old-secret wifi-old-secret"),
                         "<sudo-password> <wifi-password>")  # as private as under the new names

    def test_a_new_name_wins_over_its_old_one_and_the_environment_is_read_too(self):
        self.load("S60_SUDO_PASSWORD=old\nAUTOFLASH_SUDO_PASSWORD=new\n")
        self.assertEqual(credentials.get("AUTOFLASH_SUDO_PASSWORD"), "new")
        os.environ["S60_WIFI_SSID"] = "From-Env"  # exported in the shell under the old name
        self.load("")
        self.assertEqual(credentials.get("AUTOFLASH_WIFI_SSID"), "From-Env")
        self.assertEqual(credentials.renamed, ["S60_WIFI_SSID"])


if __name__ == "__main__":
    unittest.main()
