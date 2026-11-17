"""The check an imprintable recovery Safeboot passes before it is published
(tools/espressif/safeboot/validate_imprintable_safeboot.py): it passes the
image as published, and refuses one it should not."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.espressif.safeboot import validate_imprintable_safeboot
from tools.autoflash.imprint import imprint, repair_image
from tools.espressif.safeboot.safeboot_migration import RECOVERY_MARKER


ROOT = Path(__file__).resolve().parents[3]


PUBLISHED = (ROOT / "artifacts/autoflash-recovery-safeboot-imprintable.bin").read_bytes()


def with_marker_replaced(image, replacement):
    """The image with the recovery marker's bytes overwritten, NUL-padded, and
    its checksum and hash made good, as a build would have them."""
    assert len(replacement) <= len(RECOVERY_MARKER)
    return repair_image(image.replace(RECOVERY_MARKER, replacement.ljust(len(RECOVERY_MARKER), b"\0"), 1))


class ImprintableSafebootTests(unittest.TestCase):
    """validate_imprintable_safeboot.py: publishable, and writable into."""

    def validate(self, image):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "safeboot.bin"
            path.write_bytes(image)
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(sys, "argv", ["validate_imprintable_safeboot.py", str(path)]), \
                 contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = validate_imprintable_safeboot.main()
        failed = [line.strip()[len("FAIL  "):] for line in out.getvalue().splitlines()
                  if line.strip().startswith("FAIL")]
        return code, failed, out.getvalue() + err.getvalue()

    def test_the_published_image_is_publishable(self):
        code, failed, said = self.validate(PUBLISHED)
        self.assertEqual((code, failed), (0, []), said)
        self.assertIn("\nPUBLISHABLE", said)

    def test_an_image_with_credentials_written_in_is_not(self):
        code, failed, said = self.validate(imprint(PUBLISHED, "Home-2G", "a-password"))
        self.assertEqual(code, 1)
        self.assertTrue(any(check.startswith("network name placeholder") for check in failed), failed)
        self.assertIn("NOT PUBLISHABLE", said)

    def test_an_image_holding_a_build_path_is_not(self):
        code, failed, _ = self.validate(with_marker_replaced(PUBLISHED, b"/home/builder/tasmota/src/x.c"))
        self.assertEqual(code, 1)
        self.assertEqual(len(failed), 1, failed)
        self.assertTrue(failed[0].startswith("no build-machine paths baked in"), failed)

    def test_an_image_already_holding_what_it_is_tested_with_is_not(self):
        # Else reading back what was written could find it where it already was.
        code, failed, _ = self.validate(with_marker_replaced(PUBLISHED, b"S" * 32))
        self.assertEqual(code, 1)
        self.assertEqual(failed, ["no credential survives into the shipped image"])

    def test_a_corrupted_image_is_not(self):
        changed = bytearray(PUBLISHED)
        changed[len(changed) // 2] ^= 0xFF
        code, failed, _ = self.validate(bytes(changed))
        self.assertEqual(code, 1)
        self.assertTrue({"checksum valid", "appended SHA-256 valid"} & set(failed), failed)

    def test_an_image_too_big_for_the_partition_is_not(self):
        code, failed, _ = self.validate(PUBLISHED + bytes(validate_imprintable_safeboot.SAFEBOOT_PARTITION_BYTES))
        self.assertEqual(code, 1)
        self.assertTrue(any(check.startswith("fits the safeboot partition") for check in failed), failed)

    def test_a_missing_image_is_said_so(self):
        with mock.patch.object(sys, "argv", ["validate_imprintable_safeboot.py", "/nonexistent/safeboot.bin"]), \
             contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(validate_imprintable_safeboot.main(), 2)
        self.assertIn("FAIL", err.getvalue())


if __name__ == "__main__":
    unittest.main()
