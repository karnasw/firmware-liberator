"""A Beken device's backup as tools/beken/backup.py takes it: the MAC from the
factory TLV, and a first backup never overwritten."""
import contextlib
import hashlib
import io
import json
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.beken import backup


MAC = "12:34:56:ab:cd:ef"   # made up, like every identifier here


def factory_tlv(entries, pad=0):
    """The factory TLV at 0x3e0000: 'TLV\\0', total length, then tag/marker/len/data entries."""
    body = b""
    for tag, marker, data in entries:
        body += bytes([tag]) + marker + struct.pack("<I", len(data)) + data
    body = b"\x00" * pad + body
    return b"TLV\x00" + struct.pack("<I", len(body)) + body


class BackupTests(unittest.TestCase):
    MAC_BYTES = bytes.fromhex(MAC.replace(":", ""))

    def test_the_mac_is_read_from_the_factory_tlv(self):
        region = factory_tlv([(0x01, b"\x11\x11\x11", b"S61"), (0x02, b"\x11\x11\x11", self.MAC_BYTES)])
        self.assertEqual(backup.factory_mac(region + b"\xff" * 64), MAC)

    def test_padding_entries_are_stepped_over(self):
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)], pad=16)
        self.assertEqual(backup.factory_mac(region), MAC)

    def test_tag_2_of_another_section_is_not_the_mac(self):
        other = factory_tlv([(0x02, b"\x22\x22\x22", bytes(6))])
        with self.assertRaises(ValueError):
            backup.factory_mac(other)
        both = factory_tlv([(0x02, b"\x22\x22\x22", bytes(6)), (0x02, b"\x11\x11\x11", self.MAC_BYTES)])
        self.assertEqual(backup.factory_mac(both), MAC)

    def test_a_region_without_the_tlv_is_refused(self):
        with self.assertRaises(ValueError):
            backup.factory_mac(b"\xff" * 64)
        with self.assertRaises(ValueError):
            backup.factory_mac(factory_tlv([(0x02, b"\x11\x11\x11", bytes(4))]))   # not 6 bytes

    def quick(self):
        for name in ("GAP_S", "RETRY_S"):
            patch = mock.patch.object(backup, name, 0)
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def response(data):
        r = mock.MagicMock()
        r.__enter__.return_value.read.return_value = data
        return r

    def test_the_region_is_read_in_paced_chunks(self):
        self.quick()
        flash = bytes(range(256)) * 96      # 24 KiB: a whole chunk and a half
        urls = []

        def urlopen(url, timeout):
            urls.append(url)
            a, n = (int(x, 16) for x in url.rsplit("/", 1)[1].split("-"))
            return self.response(flash[a - 0x3E0000:a - 0x3E0000 + n])

        with mock.patch.object(backup.urllib.request, "urlopen", urlopen):
            got = backup.read_flash("http://device/", 0x3E0000, len(flash))
        self.assertEqual(got, flash)
        self.assertEqual(urls, ["http://device/api/flash/3e0000-4000", "http://device/api/flash/3e4000-2000"])

    def test_a_failed_or_short_read_is_tried_again(self):
        self.quick()
        answers = [OSError("timed out"), self.response(b"\x01" * 10), self.response(b"\x01" * 0x4000)]
        with mock.patch.object(backup.urllib.request, "urlopen", side_effect=answers) as urlopen:
            self.assertEqual(backup.read_flash("http://device", 0x3E0000, 0x4000), b"\x01" * 0x4000)
        self.assertEqual(urlopen.call_count, 3)

    def test_a_read_that_keeps_failing_names_where(self):
        self.quick()
        with mock.patch.object(backup.urllib.request, "urlopen", side_effect=OSError("down")) as urlopen:
            with self.assertRaises(RuntimeError) as caught:
                backup.read_flash("http://device", 0x3E0000, 0x8000)
        self.assertIn("0x3e0000", str(caught.exception))
        self.assertEqual(urlopen.call_count, 4)      # four tries, then given up: no later chunk asked for

    def run_main(self, tmp, region, stock_rbl=None):
        argv = ["backup.py", "--device", "http://device", "--captures", str(tmp)]
        if stock_rbl:
            argv += ["--stock-rbl", str(stock_rbl)]
        with mock.patch.object(backup, "read_flash", return_value=region) as read, \
             mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as log:
            code = backup.main()
        return code, read, log.getvalue()

    def test_the_backup_is_filed_under_the_devices_mac_with_its_manifest(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)]).ljust(0x20000, b"\xff")
        rbl = tmp / "stock_app_1.0.2.rbl"
        rbl.write_bytes(b"RBL\x00" + bytes(200))
        backup = tmp / "devices" / "12-34-56-ab-cd-ef" / "sonoff-backup"
        backup.mkdir(parents=True)       # the device's directory may be there already, with no backup in it
        code, read, _log = self.run_main(tmp, region, rbl)
        self.assertEqual(code, 0)
        read.assert_called_once_with("http://device", 0x3E0000, 0x20000)
        self.assertEqual((backup / "config-0x3e0000.bin").read_bytes(), region)
        self.assertEqual((backup / "stock-app-1.0.2.rbl").read_bytes(), rbl.read_bytes())
        manifest = json.loads((backup / "backup.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mac"], MAC)
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["flash_offset"], "0x3e0000")
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["length"], 0x20000)
        self.assertEqual(manifest["regions"]["config-0x3e0000.bin"]["sha256"], hashlib.sha256(region).hexdigest())
        self.assertEqual(manifest["regions"]["stock-app-1.0.2.rbl"]["sha256"],
                         hashlib.sha256(rbl.read_bytes()).hexdigest())

    def test_a_backup_once_made_is_never_overwritten(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        region = factory_tlv([(0x02, b"\x11\x11\x11", self.MAC_BYTES)]).ljust(0x20000, b"\xff")
        backup = tmp / "devices" / "12-34-56-ab-cd-ef" / "sonoff-backup"
        backup.mkdir(parents=True)
        (backup / "config-0x3e0000.bin").write_bytes(b"the first backup")
        code, _read, log = self.run_main(tmp, region)
        self.assertEqual(code, 0)
        self.assertEqual((backup / "config-0x3e0000.bin").read_bytes(), b"the first backup")
        self.assertFalse((backup / "backup.json").exists())
        self.assertIn("never overwritten", log)


if __name__ == "__main__":
    unittest.main()
