"""The FOTA container tools/beken/fota_wrap.py wraps an image in for the stock
updater, its server, and the eWeLink command that sends a device to it."""
import contextlib
import hashlib
import http.client
import io
import socket
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path

from tests.beken.test_rbl import rbl_header
from tools.beken import fota_wrap as fota


class FotaWrapTests(unittest.TestCase):
    RBL = rbl_header(version=b"1.18.319") + bytes(range(256)) * 4

    def test_the_version_comes_from_the_rbl_header(self):
        self.assertEqual(fota.rbl_version(self.RBL), "1.18.319")
        self.assertEqual(fota.rbl_version(self.RBL[:96]), "1.18.319")   # a header alone is enough

    def test_only_an_rbl_is_wrapped(self):
        with self.assertRaises(ValueError):
            fota.rbl_version(b"ELF\x00" + bytes(200))
        with self.assertRaises(ValueError):
            fota.rbl_version(b"RBL\x00" + bytes(50))     # too short for a header
        with self.assertRaises(ValueError):
            fota.wrap(bytes(200))

    def test_the_announced_version_is_one_the_device_takes(self):
        # The stock updater fetches only a version newer than its own 1.0.2,
        # compared as text: OpenBeken's own 1.0.0 was refused, nothing fetched.
        self.assertGreater(fota.ANNOUNCED_VERSION, "1.0.2")
        self.assertLessEqual(len(fota.ANNOUNCED_VERSION), fota.VERSION_MAX)

    def test_the_container_the_firmware_decodes(self):
        c = fota.wrap(self.RBL)
        self.assertEqual(len(c), 116 + len(self.RBL))
        self.assertEqual(struct.unpack_from("<I", c, 0)[0], 0x1BEEF11E)
        self.assertEqual(struct.unpack_from("<H", c, 12)[0], 0)
        trailer, desc = c[16:40], c[40:116]
        self.assertEqual(trailer[1], 1)                                   # one descriptor
        self.assertEqual(trailer[2:20], b"1.18.319".ljust(18, b"\x00"))
        self.assertEqual(struct.unpack_from(">I", trailer, 20)[0], zlib.crc32(trailer[:20]))
        self.assertEqual(desc[:18], b"FWSW-01-S61-BK7238")
        self.assertEqual(desc[0x20:0x30], b"1.18.319".ljust(16, b"\x00"))
        self.assertEqual(struct.unpack_from(">I", desc, 0x30)[0], 116 - 16)   # payload at 116, base 16
        self.assertEqual(struct.unpack_from(">I", desc, 0x34)[0], len(self.RBL))
        # Without it the device stages the payload but reports the update failed,
        # and never restarts to install it.
        self.assertEqual(struct.unpack_from(">I", desc, 0x38)[0], zlib.crc32(self.RBL))
        self.assertEqual(struct.unpack_from(">I", desc, 0x3C)[0], zlib.crc32(desc[:0x3C]))
        self.assertEqual(c[116:], self.RBL)

    def test_a_given_name_and_version_are_used(self):
        c = fota.wrap(self.RBL, name="OTHER", version="26.10.29-bk7238")
        self.assertEqual(c[40:45], b"OTHER")
        self.assertEqual(c[45:58], bytes(13))
        self.assertEqual(c[18:33], b"26.10.29-bk7238")
        self.assertEqual(c[40 + 0x20:40 + 0x2F], b"26.10.29-bk7238")
        self.assertEqual(c[40 + 0x2F], 0)

    def test_a_version_that_does_not_fit_is_refused(self):
        c = fota.wrap(self.RBL, version="v" * 15)
        self.assertEqual(len(c), 116 + len(self.RBL))
        for version in ("v" * 16, "", "1.0.2é"):
            with self.assertRaises(ValueError, msg=repr(version)):
                fota.wrap(self.RBL, version=version)

    def test_the_server_answers_each_range_as_the_device_fetches(self):
        container = fota.wrap(self.RBL)
        srv = fota.server(container, "127.0.0.1", 0)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
        self.addCleanup(conn.close)
        with contextlib.redirect_stderr(io.StringIO()):
            got = []
            for rng, want in (("bytes=0-115", (0, 115)), ("bytes=116-215", (116, 215)),
                              ("bytes=1000-", (1000, len(container) - 1)),
                              ("bytes=0-99999", (0, len(container) - 1)), (None, (0, len(container) - 1))):
                headers = {"Range": rng} if rng else {}
                conn.request("GET", "/user1.bin", headers=headers)
                r = conn.getresponse()
                body = r.read()
                got.append((r.status, r.getheader("Content-Range"), r.will_close, body))
                self.assertEqual(body, container[want[0]:want[1] + 1], rng)
        for status, content_range, will_close, body in got:
            self.assertEqual(status, 206)
            self.assertFalse(will_close)       # keep-alive: the device reads every range on one connection
            self.assertEqual(content_range, "bytes %d-%d/%d" % (
                int(content_range.split()[1].split("-")[0]),
                int(content_range.split()[1].split("-")[0]) + len(body) - 1, len(container)))

    def test_the_response_head_is_what_the_devices_parser_reads(self):
        container = fota.wrap(self.RBL)
        srv = fota.server(container, "127.0.0.1", 0)
        self.addCleanup(srv.server_close)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        with contextlib.redirect_stderr(io.StringIO()), socket.create_connection(srv.server_address, timeout=10) as s:
            s.sendall(b"GET /user1.bin HTTP/1.1\r\nHost: device\r\nRange: bytes=16-39\r\n\r\n")
            got = b""
            while b"\r\n\r\n" not in got or len(got) < got.index(b"\r\n\r\n") + 4 + 24:
                got += s.recv(4096)
        head, body = got.split(b"\r\n\r\n", 1)
        self.assertEqual(body, container[16:40])
        lines = head.split(b"\r\n")
        self.assertEqual(lines[0], b"HTTP/1.1 206 Partial Content")
        for line in (b"Content-Type: application/octet-stream", b"Content-Range: bytes 16-39/%d" % len(container),
                     b"Content-Length: 24", b"Connection: keep-alive"):
            self.assertIn(line, lines)

    def test_each_range_asked_for_is_logged_and_counted(self):
        container = fota.wrap(self.RBL)
        for quiet, lines in ((False, 2), (True, 0)):
            srv = fota.server(container, "127.0.0.1", 0, quiet=quiet)
            self.addCleanup(srv.server_close)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            self.addCleanup(srv.shutdown)
            conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=10)
            self.addCleanup(conn.close)
            log = io.StringIO()
            with contextlib.redirect_stderr(log):
                for rng in ("bytes=0-15", "bytes=16-39"):
                    conn.request("GET", "/user1.bin", headers={"Range": rng})
                    conn.getresponse().read()
            self.assertEqual(srv.requests, 2)
            self.assertEqual(log.getvalue().count("[ota] GET bytes="), lines, log.getvalue())

    def test_the_command_writes_the_container_beside_the_input(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        rbl = tmp / "OpenBK7238_1.18.319.rbl"
        rbl.write_bytes(self.RBL)
        with contextlib.redirect_stdout(io.StringIO()) as log:
            self.assertEqual(fota.main([str(rbl)]), 0)
        container = (tmp / "OpenBK7238_1.18.319.fota").read_bytes()
        self.assertEqual(container, fota.wrap(self.RBL))
        self.assertIn(hashlib.sha256(container).hexdigest(), log.getvalue())
        with contextlib.redirect_stdout(io.StringIO()):
            fota.main([str(rbl), "-o", str(tmp / "out.fota"), "--version", "2.0.0", "--name", "OTHER"])
        self.assertEqual((tmp / "out.fota").read_bytes(), fota.wrap(self.RBL, version="2.0.0", name="OTHER"))

    def test_the_command_refuses_what_is_not_an_rbl(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        (tmp / "x.bin").write_bytes(bytes(300))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            fota.main([str(tmp / "x.bin")])
        self.assertFalse((tmp / "x.fota").exists())


class UpgradeCommandTests(unittest.TestCase):
    def test_the_command_names_the_device_the_container_and_its_digest(self):
        register = {"action": "register", "deviceid": "1000abcdef", "apikey": "made-up-key",
                    "model": "SN-BK7238-S61-01"}
        command = fota.upgrade_command(register, "http://8.8.8.8:8080/user1.bin", "ab" * 32, "9.9.9")
        self.assertEqual((command["action"], command["deviceid"], command["apikey"]),
                         ("upgrade", "1000abcdef", "made-up-key"))
        self.assertEqual(command["params"]["model"], "SN-BK7238-S61-01")
        self.assertEqual(command["params"]["binList"],
                         [{"downloadUrl": "http://8.8.8.8:8080/user1.bin", "digest": "ab" * 32, "name": "user1.bin"}])


if __name__ == "__main__":
    unittest.main()
