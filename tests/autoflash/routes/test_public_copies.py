"""public_copies: Sonoff's update file for each kind, pinned, for either family."""

import hashlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import kinds
from tools.autoflash.routes import public_copies
from tools.ewelink import firmware_name


class PublicCopiesTests(unittest.TestCase):
    def test_every_kind_has_one_naming_its_firmware(self):
        for kind in kinds.EVERY:
            with self.subTest(kind=kind.key):
                name, digest = public_copies.COPIES[kind.key]
                found = firmware_name.PUBLIC_FILE.fullmatch(name)
                self.assertIsNotNone(found)
                self.assertIn(kind.wrapper_model, found.group("names").split("+"))
                self.assertRegex(digest, r"^[0-9a-f]{64}$")

    def test_fetched_once_kept_and_checked_by_its_hash(self):
        data = b"Sonoff's update file"
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(public_copies, "KEPT", Path(tmp) / "kept"), \
                mock.patch.dict(public_copies.COPIES, {"sonoff-s60": ("a.bin", hashlib.sha256(data).hexdigest())}), \
                mock.patch.object(public_copies.output, "say"), \
                mock.patch.object(public_copies.hostsys, "download", return_value=data) as fetched:
            self.assertEqual(public_copies.fetch(kinds.SONOFF_S60), data)
            self.assertEqual(public_copies.fetch(kinds.SONOFF_S60), data)   # kept: not fetched again
            fetched.assert_called_once()
            self.assertTrue(fetched.call_args.args[0].startswith(public_copies.AT))
            self.assertIn(firmware_name.PUBLIC_COMMIT, public_copies.AT)
            # Not the file pinned, fetched or kept: refused.
            (Path(tmp) / "kept" / "a.bin").write_bytes(b"something else")
            fetched.return_value = b"something else"
            with self.assertRaisesRegex(public_copies.Unavailable, "hash differs"):
                public_copies.fetch(kinds.SONOFF_S60)

    def test_unreachable_or_unknown_says_why(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(public_copies, "KEPT", Path(tmp)), \
                mock.patch.object(public_copies.output, "say"), \
                mock.patch.object(public_copies.hostsys, "download", side_effect=OSError("no network")):
            with self.assertRaisesRegex(public_copies.Unavailable, "could not be fetched: no network"):
                public_copies.fetch(kinds.SONOFF_S60)
            with mock.patch.dict(public_copies.COPIES, clear=True), \
                    self.assertRaisesRegex(public_copies.Unavailable, "no public copy"):
                public_copies.fetch(kinds.SONOFF_S60)


if __name__ == "__main__":
    unittest.main()
