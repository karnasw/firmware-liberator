"""tools/beken/ef40_dump.py, the command that lists a dump's KV40 store."""
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.beken.test_ef40 import kv40_store
from tools.beken import ef40, ef40_dump


class Ef40DumpTests(unittest.TestCase):
    """tools/beken/ef40_dump.py reads a store as ef40 does, and lists it."""

    def test_the_dump_tool_reads_the_same_store(self):
        store = kv40_store([("wifi-ssid", b"HomeNet"), ("wifi-pass", b"secret"), ("wifi-ssid", b"NewNet")])
        store[8] ^= 0x01                 # the first wifi-ssid's CRC fails: stale
        garbage = bytearray(ef40.encode_node("k", "v"))
        garbage[12] = 0                  # a node with no name, before them all
        store = garbage + store
        nodes = ef40_dump.parse(bytes(store))
        self.assertEqual({k: v for k, (v, ok, _n) in nodes.items() if ok},
                         ef40.parse_store(store, region=(0, len(store))))
        self.assertEqual(nodes["wifi-pass"][1:], (True, 1))
        self.assertEqual(nodes["wifi-ssid"][0], b"NewNet")

    def test_the_dump_tool_lists_valid_keys_unless_asked_for_all(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        flash = bytearray(b"\xff" * 0x3FD000)
        flash[0x3E0000:0x3E0000 + 0x1000] = kv40_store([("wifi-ssid", b"HomeNet"), ("elecCalibCoef", bytes(12)),
                                                        ("blob", bytes(range(40)))])
        flash[0x3E0000 + 8] ^= 0x01      # wifi-ssid stale
        flash[0x3F6000:0x3F6000 + 0x100] = kv40_store([("sys-key", b"on")], size=0x100)
        (tmp / "flash.bin").write_bytes(flash)

        def run(*args):
            with mock.patch.object(sys, "argv", ["ef40_dump.py", str(tmp / "flash.bin"), *args]), \
                 contextlib.redirect_stdout(io.StringIO()) as log:
                ef40_dump.main()
            return log.getvalue()

        log = run()
        self.assertNotIn("wifi-ssid", log)
        self.assertIn("elecCalibCoef", log)
        self.assertIn("hex  " + bytes(12).hex(), log)                    # a short value, whole
        self.assertIn("hex  %s... (40 bytes)" % bytes(range(16)).hex(), log)   # a long one, cut
        self.assertIn("str  'on'", log)
        self.assertIn("app  (0x3e0000-0x3ebfff) : 3 distinct keys", log)
        log = run("--all")
        self.assertIn("wifi-ssid", log)
        self.assertIn("[CRC MISMATCH/stale]", log)

    def test_the_dump_tool_flags_a_stale_node(self):
        store = kv40_store([("wifi-pass", b"secret")])
        store[8] ^= 0x01
        value, ok, _n = ef40_dump.parse(bytes(store))["wifi-pass"]
        self.assertEqual(value, b"secret")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
