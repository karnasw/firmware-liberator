"""The OpenBeken HTTP client, against a stand-in for OpenBeken's own server."""
import http.server
import json
import threading
import unittest
from unittest import mock

from tools.autoflash import openbeken

FLASH = bytes(range(256)) * 128  # 32 KiB of "flash", from 0x3e0000


class FakeOpenBeken(http.server.ThreadingHTTPServer):
    """OpenBeken's REST as the S61 build answers it: /api/info, /api/cmnd,
    /api/channels, /api/flash/<hex>-<hex>."""

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.commands: list[str] = []
        self.unknown = {"NoSuchCommand"}
        self.flash_failures = 0   # the first this many flash reads come back short
        self.uptime = 42
        self.restarts_on_reboot = True
        self.kind = "sonoff-s61"   # what its import module answers AutoflashKind for; None: no module
        self.interim = False   # whether its module says it is the interim firmware (AutoflashInterim)
        # What its module answers AutoflashCalibration for: stock, missing, or
        # None for neither (its calibration changed since).
        self.calibration = "stock"
        self.short_name = "openbeken-abcdef"
        self.version = "1.0.0"   # the release its build string gives
        self.bodies_lost = 0   # the first this many commands come without their words, as OpenBeken's server can

    @property
    def host(self):
        return f"127.0.0.1:{self.server_address[1]}"


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def _send(self, status, body, kind="application/json"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/api/info":
            self._send(200, json.dumps({
                "uptime_s": self.server.uptime, "build": f"Built on Oct 30 2026 12:00:00 version {self.server.version}", "ip": "192.0.2.50",
                "mac": "12:34:56:AB:CD:EF", "flags": "0", "mqtthost": ":1883", "mqtttopic": "obk",
                "chipset": "BK7238", "webapp": "", "shortName": self.server.short_name, "startcmd": "",
                "supportsSSDP": 0, "supportsClientDeviceDB": True}).encode())
        elif self.path == "/api/channels":
            self._send(200, b'{"1":1,"2":0}')
        elif self.path.startswith("/api/flash/"):
            start, length = (int(part, 16) for part in self.path[len("/api/flash/"):].split("-"))
            data = FLASH[start - 0x3E0000:start - 0x3E0000 + length]
            if self.server.flash_failures:
                self.server.flash_failures -= 1
                data = data[:10]
            self._send(200, data, "application/octet-stream")
        else:
            self._send(404, b"<html>not here</html>", "text/html")

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
        if self.path != "/api/cmnd":
            self._send(404, b"{}")
            return
        if self.server.bodies_lost:
            # Its first read held only the headers: it ran nothing.
            self.server.bodies_lost -= 1
            self._send(400, json.dumps({"error": 400, "msg": "No command entered", "res": ""}).encode())
            return
        self.server.commands.append(body)
        name = body.split()[0]
        if name == "reboot" and self.server.restarts_on_reboot:
            self.server.uptime = 1
        if name == "AutoflashInterim" and body.split()[1:] in (["on"], ["off"]) and self.server.kind:
            self.server.interim = body.split()[1] == "on"
            self.server.short_name = "autoflash-recovery-CDEF" if self.server.interim else "openbeken-abcdef"
        if name in self.server.unknown or (name.startswith("Autoflash") and self.server.kind is None):
            self._send(200, json.dumps({"error": 501, "msg": "command not found", "res": ""}).encode())
        elif name == "AutoflashKind" and body.split()[1:] != [self.server.kind]:
            self._send(400, json.dumps({"error": 400, "msg": "bad argument", "res": ""}).encode())
        elif name == "AutoflashInterim" and not body.split()[1:] and not self.server.interim:
            self._send(400, json.dumps({"error": 400, "msg": "error", "res": ""}).encode())
        elif name == "AutoflashCalibration" and body.split()[1:] != [self.server.calibration]:
            self._send(400, json.dumps({"error": 400, "msg": "error", "res": ""}).encode())
        elif name == "AutoflashInterim" and body.split()[1:] not in ([], ["on"], ["off"]):
            self._send(400, json.dumps({"error": 400, "msg": "bad argument", "res": ""}).encode())
        else:
            self._send(200, json.dumps({"success": 200, "msg": f"{name} done", "res": ""}).encode())


class OpenBekenTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeOpenBeken()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        patch = mock.patch.object(openbeken.time, "sleep")
        self.slept = patch.start()
        self.addCleanup(patch.stop)

    def test_an_address_alone_is_asked_on_port_80(self):
        self.assertEqual(openbeken._url("192.0.2.50", "/api/info"), "http://192.0.2.50:80/api/info")
        self.assertEqual(openbeken._url("192.0.2.50:8080", "/api/info"), "http://192.0.2.50:8080/api/info")

    def test_info_says_what_it_is(self):
        found = openbeken.info(self.server.host)
        self.assertEqual((found["chipset"], found["shortName"]), ("BK7238", "openbeken-abcdef"))
        self.assertEqual(openbeken.version(found), "1.0.0")
        self.assertTrue(openbeken.is_openbeken(self.server.host))

    def test_its_kind_is_what_its_import_module_says_whatever_its_name(self):
        from tools.autoflash import kinds
        found = openbeken.info(self.server.host)
        self.assertIs(openbeken.kind(self.server.host, found), kinds.SONOFF_S61)
        self.assertIn("AutoflashKind sonoff-s61", self.server.commands)
        # Renamed by its user: the same kind.
        self.assertIs(openbeken.kind(self.server.host, {**found, "shortName": "kitchen"}), kinds.SONOFF_S61)

    def test_an_openbeken_the_tool_did_not_install_is_no_kind(self):
        self.server.kind = None   # no import module: AutoflashKind unknown
        self.assertIsNone(openbeken.kind(self.server.host, openbeken.info(self.server.host)))
        self.server.kind = "another-device"
        self.assertIsNone(openbeken.kind(self.server.host, openbeken.info(self.server.host)))

    def test_only_an_openbeken_on_a_chip_of_ours_is_asked(self):
        found = {**openbeken.info(self.server.host), "chipset": "BK7231N"}
        self.assertIsNone(openbeken.kind(self.server.host, found))
        self.assertEqual(self.server.commands, [])

    def test_the_interim_firmware_is_what_its_import_module_says(self):
        self.assertIs(openbeken.is_interim(self.server.host), False)
        self.assertEqual(self.server.commands, ["AutoflashInterim"])
        self.server.interim = True
        self.assertIs(openbeken.is_interim(self.server.host), True)
        # A module from before the command: never taken for the interim firmware.
        self.server.kind = None
        self.assertIs(openbeken.is_interim(self.server.host), False)
        # Not answering: it cannot say.
        self.assertIsNone(openbeken.is_interim("127.0.0.1:1", timeout=0.5))

    def test_its_meter_calibration_is_what_its_import_module_says(self):
        # In the words ESPHome's Calibration sensor uses, which the report reads.
        self.assertEqual(openbeken.calibration_state(self.server.host), "stock store")
        self.assertEqual(self.server.commands, ["AutoflashCalibration stock"])
        self.server.calibration = "missing"
        self.assertEqual(openbeken.calibration_state(self.server.host), "missing")
        self.server.calibration = None   # neither: not applied, or changed by its owner since
        self.assertEqual(openbeken.calibration_state(self.server.host), "not the stock store's")
        # A module from before the command, or a device not answering: it cannot say.
        self.server.kind = None
        self.assertIsNone(openbeken.calibration_state(self.server.host))
        self.assertIsNone(openbeken.calibration_state("127.0.0.1:1", timeout=0.5))

    def test_it_is_told_to_be_the_interim_firmware_or_not(self):
        openbeken.set_interim(self.server.host, False)
        openbeken.set_interim(self.server.host, True)
        self.assertEqual(self.server.commands, ["AutoflashInterim off", "AutoflashInterim on"])
        self.assertEqual(openbeken.info(self.server.host)["shortName"], "autoflash-recovery-CDEF")
        self.server.kind = None
        with self.assertRaisesRegex(openbeken.CommandError, "not a command"):
            openbeken.set_interim(self.server.host, False)

    def test_nothing_else_is_taken_for_openbeken(self):
        class Other(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_GET(self):
                body = b'{"a page": "with json but not OpenBeken"}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
        other = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Other)
        threading.Thread(target=other.serve_forever, daemon=True).start()
        self.addCleanup(other.server_close)
        self.addCleanup(other.shutdown)
        self.assertIsNone(openbeken.info(f"127.0.0.1:{other.server_address[1]}"))
        self.assertIsNone(openbeken.info("127.0.0.1:1", timeout=0.5))  # nothing listening
        for half in (b'{"chipset": "BK7238"}', b'{"build": "version 1.0.0"}'):
            with self.subTest(half=half), mock.patch.object(openbeken, "_get", return_value=half):
                self.assertIsNone(openbeken.info(self.server.host))

    def test_a_command_answers_through_the_envelope(self):
        self.assertEqual(openbeken.command(self.server.host, "SetStartValue 1 -1"), "SetStartValue done")
        self.assertEqual(self.server.commands, ["SetStartValue 1 -1"])
        with self.assertRaisesRegex(openbeken.CommandError, "NoSuchCommand.*not a command this OpenBeken knows") \
                as refused:
            openbeken.command(self.server.host, "NoSuchCommand 1")
        self.assertTrue(refused.exception.unknown)
        with self.assertRaisesRegex(openbeken.CommandError, "^AutoflashKind: bad argument$") as refused:
            openbeken.command(self.server.host, "AutoflashKind other")
        self.assertFalse(refused.exception.unknown)
        self.assertFalse(openbeken.CommandError("refused").unknown)

    def test_a_command_whose_words_did_not_reach_it_is_sent_again(self):
        self.server.bodies_lost = 1
        with mock.patch.object(openbeken.time, "sleep"):
            self.assertEqual(openbeken.ota_http(self.server.host, "http://192.0.2.2:8088/firmware.rbl"), "ota_http done")
        self.assertEqual(self.server.commands, ["ota_http http://192.0.2.2:8088/firmware.rbl"], "run once")
        self.server.bodies_lost = openbeken.EMPTY_TRIES
        with mock.patch.object(openbeken.time, "sleep"), \
                self.assertRaisesRegex(openbeken.CommandError, "No command entered") as stopped:
            openbeken.command(self.server.host, "reboot")
        self.assertTrue(stopped.exception.empty)

    def test_ota_http_sends_the_url_as_one_argument(self):
        openbeken.ota_http(self.server.host, "http://192.0.2.2:8088/esphome.rbl")
        self.assertEqual(self.server.commands, ["ota_http http://192.0.2.2:8088/esphome.rbl"])

    def test_a_restart_is_seen_by_its_uptime(self):
        self.assertTrue(openbeken.restart(self.server.host, wait=10))
        self.assertEqual(self.server.commands, ["reboot"])

    def test_one_that_does_not_restart_is_said_so(self):
        self.server.restarts_on_reboot = False
        with mock.patch.object(openbeken.time, "monotonic", side_effect=[0, 0, 5, 11]):
            self.assertFalse(openbeken.restart(self.server.host, wait=10))

    def test_a_restart_just_after_another_is_seen(self):
        # Seen in the emulator: the interim firmware, just restarted by its
        # bootloader refusing ESPHome, had been up 20 s; restarted again, it
        # had been up longer than that by the time it answered.
        answers = [{"uptime_s": 20}, None, None, {"uptime_s": 60}, {"uptime_s": 63}]
        with mock.patch.object(openbeken, "info", side_effect=answers), \
                mock.patch.object(openbeken.time, "monotonic", side_effect=[0, 0, 3, 6, 9, 200]), \
                mock.patch.object(openbeken.time, "sleep"):
            self.assertTrue(openbeken.restart(self.server.host, wait=120))

    def test_channels_are_read(self):
        self.assertEqual(openbeken.channels(self.server.host), {"1": 1, "2": 0})

    def test_flash_is_read_in_paced_chunks_each_tried_again(self):
        self.server.flash_failures = 2
        seen = []
        data = openbeken.read_flash(self.server.host, 0x3E0000, len(FLASH), progress=lambda done, total: seen.append(done))
        self.assertEqual(data, FLASH)
        self.assertEqual(seen, [0x4000, 0x8000])
        self.assertEqual(self.slept.call_count, 2 + 1)   # after each short read, then between the chunks
        self.slept.reset_mock()
        # A length that ends part way through a chunk: the last read is short,
        # and nothing is waited for after it.
        self.assertEqual(openbeken.read_flash(self.server.host, 0x3E0000, 0x4100), FLASH[:0x4100])
        self.assertEqual(self.slept.call_args_list, [mock.call(openbeken.FLASH_PAUSE)])
        with mock.patch.object(openbeken, "FLASH_TRIES", 1):
            self.server.flash_failures = 1
            with self.assertRaisesRegex(OSError, "short read"):
                openbeken.read_flash(self.server.host, 0x3E0000, 0x100)


if __name__ == "__main__":
    unittest.main()
