"""tools/beken/wifi_migrate.py: the stock firmware's Wi-Fi, from its KV40 store,
written into OpenBeken's config."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parents[2] / "tools" / "beken"
if str(TOOLS) not in sys.path:
    sys.path.append(str(TOOLS))   # the tools import each other by bare name

import obk_config  # noqa: E402
import ef40  # noqa: E402
import wifi_migrate  # noqa: E402


class WifiMigrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def stock(self, creds):
        data = bytearray(b"\xff" * ef40.APP_STORE[1])
        if creds:
            ef40.provision(data, creds)
        path = self.tmp / "stock.bin"
        path.write_bytes(data)
        return path

    def test_the_credentials_are_read_without_their_terminators(self):
        path = self.stock({"wifi-ssid": b"HomeNet\x00", "wifi-pass": b"secret\x00"})
        self.assertEqual(wifi_migrate.read_stock_creds(path), (b"HomeNet", b"secret"))

    def test_an_unprovisioned_device_has_none(self):
        self.assertEqual(wifi_migrate.read_stock_creds(self.stock({})), (None, None))
        self.assertEqual(wifi_migrate.read_stock_creds(self.stock({"wifi-ssid": b"HomeNet"})),
                         (None, None))

    def test_the_config_lands_at_the_offset_and_the_rest_is_kept(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        image = bytes(range(256)) * 32      # 8 KiB of an image
        obk.write_bytes(image)
        out = self.tmp / "out.bin"
        result = wifi_migrate.migrate(stock, obk, out, 0x1000)
        blob = obk_config.build_config("HomeNet", "secret")
        self.assertEqual(result, (b"HomeNet", b"secret", blob[3]))
        got = out.read_bytes()
        self.assertEqual(got[:0x1000], image[:0x1000])
        self.assertEqual(got[0x1000:0x1000 + 3584], blob)
        self.assertEqual(got[0x1000 + 3584:], image[0x1000 + 3584:])

    def test_an_image_shorter_than_the_offset_is_padded_erased(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(100))
        out = self.tmp / "out.bin"
        wifi_migrate.migrate(stock, obk, out, 0x200)
        got = out.read_bytes()
        self.assertEqual(len(got), 0x200 + 3584)
        self.assertEqual(got[100:0x200], b"\xff" * (0x200 - 100))
        self.assertEqual(got[0x200:], obk_config.build_config("HomeNet", "secret"))

    def test_a_template_reaches_the_config(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        template = self.tmp / "template.bin"
        template.write_bytes(bytes([0xAB]) * 3584)
        out = self.tmp / "out.bin"
        wifi_migrate.migrate(stock, obk, out, 0, template)
        self.assertEqual(out.read_bytes()[:3584],
                         obk_config.build_config("HomeNet", "secret", template=bytes([0xAB]) * 3584))

    def test_nothing_is_written_for_an_unprovisioned_device(self):
        stock = self.stock({})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        out = self.tmp / "out.bin"
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertIsNone(wifi_migrate.migrate(stock, obk, out, 0))
        self.assertFalse(out.exists())
        self.assertIn("unprovisioned", err.getvalue())

    def test_the_command_line_takes_a_hex_offset(self):
        stock = self.stock({"wifi-ssid": b"HomeNet", "wifi-pass": b"secret"})
        obk = self.tmp / "obk.bin"
        obk.write_bytes(bytes(0x100))
        out = self.tmp / "out.bin"
        argv = ["wifi_migrate.py", "--stock", str(stock), "--obk", str(obk), "--out", str(out),
                "--cfg-offset", "0x800"]
        with mock.patch.object(sys, "argv", argv), contextlib.redirect_stdout(io.StringIO()) as log:
            wifi_migrate.main()
        self.assertEqual(out.read_bytes()[0x800:0x800 + 3], b"CFG")
        self.assertIn("ssid=b'HomeNet'", log.getvalue())


class ConversionChainTests(unittest.TestCase):
    """A stock dump's credentials end up in the OpenBeken config, whole."""

    def test_stock_store_to_openbeken_config(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        stock = bytearray(b"\xff" * 0x400000)
        ef40.provision(stock, {"wifi-ssid": "Café Net".encode("latin1"), "wifi-pass": b"p@ss w0rd!"})
        (tmp / "stock.bin").write_bytes(stock)
        (tmp / "obk.bin").write_bytes(b"\x00" * 0x200000)
        wifi_migrate.migrate(tmp / "stock.bin", tmp / "obk.bin", tmp / "out.bin", 0x1E1000)
        config = (tmp / "out.bin").read_bytes()[0x1E1000:0x1E1000 + 3584]
        self.assertEqual(config[:3], b"CFG")
        self.assertEqual(config[3], obk_config.tiny_crc8(config[4:]))
        ssid = "Café Net".encode("latin1")
        self.assertEqual(config[0x14:0x14 + len(ssid) + 1], ssid + b"\x00")
        self.assertEqual(config[0x54:0x54 + 11], b"p@ss w0rd!\x00")


if __name__ == "__main__":
    unittest.main()
