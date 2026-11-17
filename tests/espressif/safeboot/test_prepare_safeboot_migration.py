"""tools/espressif/safeboot/prepare_safeboot_migration.py: the official files
fetched for a bundle, tried again through a passing trouble (hostsys.download)."""
import unittest
from unittest import mock

from tools.espressif.safeboot import prepare_safeboot_migration as migration


class DownloadTests(unittest.TestCase):
    def test_the_official_files_are_fetched_through_the_retrying_download(self):
        with mock.patch.object(migration.hostsys, "download", return_value=b"file") as fetched:
            self.assertEqual(migration.download("https://example.invalid/file"), b"file")
        self.assertEqual(fetched.call_args.args[0].full_url, "https://example.invalid/file")
        self.assertIs(fetched.call_args.kwargs["say"], migration.output.detail)


if __name__ == "__main__":
    unittest.main()
