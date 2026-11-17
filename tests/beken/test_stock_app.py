"""The stock app lifted out of a Beken dump (tools/beken/stock_app.py): checked
against its RBL header, and as its command writes it."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.beken.test_rbl import rbl_header
from tools.beken import rbl, stock_app


def striped(logical: bytes) -> bytes:
    """A raw dump: a CRC after every 32 bytes, as the flash holds it."""
    out = bytearray()
    for i in range(0, len(logical), 32):
        out += logical[i:i + 32].ljust(32, b"\xff") + b"\0\0"
    return bytes(out)


class StockAppTests(unittest.TestCase):
    def dump(self, app=b"stock app" * 1000, version="1.0.2", hash_offset=0):
        header = rbl.Header(algo=0x402, timestamp=7, name="app", version=version, sn=b"sn", body_crc32=0,
                            raw_hash=(rbl.fnv1a32(app) + hash_offset) & 0xFFFFFFFF, size_raw=len(app),
                            size_package=0).pack()
        logical = bytearray(b"\xff" * (stock_app.APP_RBL_LOGICAL + rbl.HEADER + 64))
        logical[stock_app.APP_LOGICAL:stock_app.APP_LOGICAL + len(app)] = app
        logical[stock_app.APP_RBL_LOGICAL:stock_app.APP_RBL_LOGICAL + rbl.HEADER] = header
        return striped(bytes(logical))

    def test_the_app_is_lifted_out_and_checked_against_its_header(self):
        app = b"stock app" * 1000
        found, header = stock_app.extract(self.dump(app))
        self.assertEqual(found, app)
        self.assertEqual((header.version, header.timestamp, header.sn), ("1.0.2", 7, b"sn".ljust(24, b"\0")))
        packed, version = stock_app.stock_rbl(self.dump(app))
        self.assertEqual(version, "1.0.2")
        self.assertEqual(rbl.unpack(packed), app)
        self.assertEqual((rbl.header(packed).timestamp, rbl.header(packed).sn), (7, header.sn))

    def test_a_dump_whose_app_does_not_match_is_refused(self):
        with self.assertRaisesRegex(stock_app.DumpError, "raw hash"):
            stock_app.extract(self.dump(hash_offset=1))
        with self.assertRaisesRegex(stock_app.DumpError, "no app RBL header"):
            stock_app.extract(striped(b"\xff" * (stock_app.APP_RBL_LOGICAL + 200)))

    def test_unstripe_drops_the_crc_after_every_32_bytes(self):
        self.assertEqual(stock_app.unstripe(striped(bytes(range(64)))), bytes(range(64)))


class StockAppCommandTests(unittest.TestCase):
    def test_unstripe_drops_each_blocks_crc(self):
        physical = b"".join(bytes([i]) * 32 + b"\xcc\xcc" for i in range(3)) + b"\x07" * 20
        self.assertEqual(stock_app.unstripe(physical), b"".join(bytes([i]) * 32 for i in range(3)))

    def test_fnv1a32_known_values(self):
        self.assertEqual(stock_app.fnv1a32(b""), 0x811C9DC5)
        self.assertEqual(stock_app.fnv1a32(b"a"), 0xE40C292C)
        self.assertEqual(stock_app.fnv1a32(b"foobar"), 0xBF9CF968)

    def dump(self, app, raw_hash=None, name=b"app"):
        logical = bytearray(b"\xff" * 0x260000)
        logical[0x10000:0x10000 + len(app)] = app
        header = rbl_header(name=name, version=b"1.0.2", size_raw=len(app),
                            raw_hash=stock_app.fnv1a32(app) if raw_hash is None else raw_hash)
        logical[0x25FFA0:0x25FFA0 + 96] = header
        return b"".join(bytes(logical[i:i + 32]) + b"\xcc\xcc" for i in range(0, len(logical), 32))

    def extract(self, dump):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / "dump.bin").write_bytes(dump)
        out = tmp / "app.bin"
        with mock.patch.object(sys, "argv", ["stock_app.py", str(tmp / "dump.bin"), "-o", str(out)]), \
             contextlib.redirect_stdout(io.StringIO()) as log, contextlib.redirect_stderr(io.StringIO()) as err:
            code = stock_app.main()
        return code, out, log.getvalue() + err.getvalue()

    def test_the_app_is_extracted_and_verified(self):
        app = bytes(range(256)) * 10 + b"end"
        code, out, log = self.extract(self.dump(app))
        self.assertEqual(code, 0)
        self.assertEqual(out.read_bytes(), app)
        self.assertIn("stock app 1.0.2: %d bytes" % len(app), log)

    def test_an_app_that_does_not_match_its_header_is_not_written(self):
        app = bytes(range(256)) * 10
        for dump, what in ((self.dump(app, raw_hash=0x12345678), "raw hash"),
                           (self.dump(app, name=b"bootloader"), "no app RBL header")):
            code, out, log = self.extract(dump)
            self.assertEqual(code, 1, what)
            self.assertFalse(out.exists(), what)
            self.assertIn(what, log)


if __name__ == "__main__":
    unittest.main()
