"""The check a recovery Safeboot passes before it is used, its Wi-Fi written in
(tools/espressif/safeboot/validate_recovery_safeboot.py): it passes the image
as published, and refuses one it should not; and the header that Wi-Fi is
rendered into.
"""

import contextlib
import io
import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.espressif.safeboot.test_validate_imprintable_safeboot import PUBLISHED, with_marker_replaced
from tools.espressif.safeboot import validate_recovery_safeboot
from tools.espressif.safeboot.configure_recovery_safeboot import render_header
from tools.autoflash.imprint import imprint
from tools.espressif.safeboot.validate_recovery_safeboot import header_value


def header(ssid, password):
    """The private header configure_recovery_safeboot.py writes for that Wi-Fi."""
    return render_header(ssid.decode(), password.decode())


class RecoverySafebootTests(unittest.TestCase):
    """validate_recovery_safeboot.py: a private build holds the Wi-Fi it was
    built with, and its report never shows it."""

    SSID, PASSWORD = b"Home-2G", b"a-password"
    BUILT = imprint(PUBLISHED, SSID.decode(), PASSWORD.decode())

    def test_a_build_holding_its_wifi_passes_and_its_report_holds_none_of_it(self):
        report = validate_recovery_safeboot.validate(self.BUILT, header(self.SSID, self.PASSWORD))
        self.assertEqual((report["ssid_bytes"], report["password_present"]), (len(self.SSID), True))
        self.assertEqual(report["kind"], validate_recovery_safeboot.RECOVERY_KIND)
        self.assertEqual(report["headroom"], validate_recovery_safeboot.SAFEBOOT_SIZE - len(self.BUILT))
        written = json.dumps(report)
        self.assertNotIn(self.SSID.decode(), written)
        self.assertNotIn(self.PASSWORD.decode(), written)

    def test_an_open_network_needs_no_password(self):
        # configure_recovery_safeboot.py takes an empty password, as STA_PASS1 "".
        built = imprint(PUBLISHED, self.SSID.decode(), "")
        self.assertFalse(validate_recovery_safeboot.validate(built, header(self.SSID, b""))["password_present"])

    def test_a_build_without_what_it_should_hold_is_refused(self):
        for case, image, given, said in (
                ("another network", self.BUILT, header(b"Other-5G", self.PASSWORD), "SSID was not linked"),
                ("another password", self.BUILT, header(self.SSID, b"other-password"), "password was not linked"),
                ("no recovery marker", with_marker_replaced(self.BUILT, b"x"), header(self.SSID, self.PASSWORD),
                 "recovery marker is absent"),
                ("a header without the network", self.BUILT, b'#define STA_PASS1 "\\x61"\n', "no encoded STA_SSID1"),
                ("an empty network name", self.BUILT, b'#define STA_SSID1 ""\n#define STA_PASS1 ""\n',
                 "empty STA_SSID1"),
                ("not an image", b"\0" * 1024, header(self.SSID, self.PASSWORD), "")):
            with self.assertRaisesRegex(ValueError, said, msg=case):
                validate_recovery_safeboot.validate(image, given)

    def test_the_official_safeboot_is_refused(self):
        from tools.espressif.safeboot.safeboot_migration import sha256_bytes
        pinned = {"safeboot": {"sha256": sha256_bytes(self.BUILT)}}
        with mock.patch.object(validate_recovery_safeboot, "PINNED_ARTIFACTS", pinned), \
             self.assertRaisesRegex(ValueError, "equals official Safeboot"):
            validate_recovery_safeboot.validate(self.BUILT, header(self.SSID, self.PASSWORD))

    def test_the_report_is_written_for_its_owner_only(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.bin").write_bytes(self.BUILT)
            (root / "private.h").write_bytes(header(self.SSID, self.PASSWORD))
            output = root / "private" / "report.json"
            argv = ["validate_recovery_safeboot.py", "--image", str(root / "image.bin"),
                    "--private-header", str(root / "private.h"), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as said:
                self.assertEqual(validate_recovery_safeboot.main(), 0)
            self.assertEqual(json.loads(output.read_text(encoding="utf-8"))["ssid_bytes"], len(self.SSID))
            self.assertEqual([p.name for p in output.parent.iterdir()], ["report.json"])  # no temporary left
            if sys.platform != "win32":
                self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(output.parent.stat().st_mode), 0o700)
        for secret in (self.SSID, self.PASSWORD):
            self.assertNotIn(secret.decode(), said.getvalue())

    def test_a_refused_build_writes_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "image.bin").write_bytes(self.BUILT)
            (root / "private.h").write_bytes(header(b"Other-5G", self.PASSWORD))
            output = root / "report.json"
            argv = ["validate_recovery_safeboot.py", "--image", str(root / "image.bin"),
                    "--private-header", str(root / "private.h"), "--output", str(output)]
            with mock.patch.object(sys, "argv", argv), contextlib.redirect_stderr(io.StringIO()), \
                 self.assertRaises(SystemExit):
                validate_recovery_safeboot.main()
            self.assertFalse(output.exists())


class RecoveryHeaderTests(unittest.TestCase):
    def test_private_recovery_header_round_trip_without_plaintext(self):
        ssid = "Home test café"
        password = 'safe-quote-"-test'
        header = render_header(ssid, password)
        self.assertEqual(header_value(header, "STA_SSID1"), ssid.encode("utf-8"))
        self.assertEqual(header_value(header, "STA_PASS1"), password.encode("utf-8"))
        self.assertNotIn(ssid.encode("utf-8"), header)
        self.assertNotIn(password.encode("utf-8"), header)


if __name__ == "__main__":
    unittest.main()
