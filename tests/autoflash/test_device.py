"""A device on Tasmota, or its startup firmware, as tools/autoflash/device.py
asks it things: requests held by a busy device, uploads refused, its web
password, its flash read, its settings backed up, and its restarts seen."""
import base64
import http.server
import json
import re
import select
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
from unittest import mock

from tests.espressif.test_sonoff_identity import sonoff_nvs
from tools.autoflash import device, plans
from tools.autoflash.device import Device, DeviceError


class BusyTasmota(http.server.ThreadingHTTPServer):
    """Answers as a Tasmota device does - /slow only after `delay` seconds, as
    while Berry runs, or once `release` is set - and notes whether the client
    closed a request first."""
    daemon_threads = True

    def __init__(self, delay: float = 0.0):
        super().__init__(("127.0.0.1", 0), BusyTasmotaHandler)
        self.delay = delay
        self.slow_page = False
        self.closed_unanswered: list[bool] = []
        self.answered = threading.Event()
        self.release = threading.Event()


class BusyTasmotaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, status: int, body: bytes = b"ok", headers: dict[str, str] | None = None):
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: BusyTasmota = self.server
        if self.path.startswith("/slow") or (server.slow_page and self.path == "/"):
            server.release.wait(server.delay)
            # A client that gave up and closed has sent its FIN by now.
            readable, _, _ = select.select([self.connection], [], [], 0)
            closed = bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
            server.closed_unanswered.append(closed)
            self.reply(200, b"late")
            server.answered.set()
        elif self.path.startswith("/missing"):
            self.reply(404, b"not found")
        elif self.path.startswith("/bad"):
            self.reply(400, b"bad request")
        elif self.path.startswith("/moved"):
            # /moved301 and the like: that status; /moved, 302.
            self.reply(int(self.path[len("/moved"):] or 302), b"", {"Location": "/cm?cmnd=Status"})
        elif self.path.startswith("/headers"):
            self.reply(200, json.dumps(dict(self.headers.items())).encode())
        elif self.path.startswith("/cm"):
            self.reply(200, b'{"Status":{}}')
        else:
            self.reply(200, b"<title>Tasmota</title>")

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        self.reply(303, b"", {"Location": "/headers"})   # as a form's answer comes


# Long enough that a device told to answer late answers only once released: a
# test that checks what happens before the answer does not race it.
NOT_YET = 60


def held_ends(within: float = 10) -> bool:
    """Whether every request held ends, its worker done, within `within` seconds."""
    deadline = time.monotonic() + within
    while device.held_requests() and time.monotonic() < deadline:
        time.sleep(0.05)
    return device.held_requests() == 0


class HeldRequestTests(unittest.TestCase):
    def setUp(self):
        # The requests held are the whole process's: each test counts its own.
        patcher = mock.patch.object(device, "_held", [])
        patcher.start()
        self.addCleanup(patcher.stop)

    def start(self, delay: float = 0.0) -> Device:
        server = BusyTasmota(delay)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        self.addCleanup(server.release.set)  # no request left waiting
        self.server = server
        return Device(f"127.0.0.1:{server.server_address[1]}")

    def test_a_request_the_device_has_not_answered_is_held_open_not_closed(self):
        # Closing it is what leaves a busy device's web server stuck for good.
        client = self.start(delay=NOT_YET)
        with self.assertRaises(TimeoutError):
            client._get("/slow", {}, timeout=0.3)
        self.assertEqual(device.held_requests(), 1)
        self.assertTrue(device._held[0][1].daemon)   # never keeps the tool from ending
        self.server.release.set()
        self.assertTrue(self.server.answered.wait(10))
        self.assertTrue(held_ends(), "the answered request was still held")
        self.assertEqual(self.server.closed_unanswered, [False])

    def test_a_slow_answer_within_the_time_given_is_waited_for(self):
        client = self.start(delay=0.5)
        self.assertEqual(client._get("/slow", {}, timeout=10), "late")

    def test_a_device_with_requests_held_is_not_sent_more_than_a_few(self):
        client = self.start(delay=NOT_YET)
        for _ in range(device.MOST_HELD + 2):
            with self.assertRaises(TimeoutError):
                client._get("/slow", {}, timeout=0.2)
        self.assertEqual(device.held_requests(client.host), device.MOST_HELD)
        self.server.release.set()
        self.assertTrue(held_ends(), "the answered requests were still held")
        self.assertEqual(self.server.closed_unanswered, [False] * device.MOST_HELD)
        self.assertEqual(client._get("/slow", {}, timeout=5), "late")  # answered: sent again

    def test_a_held_request_is_kept_alive_and_ends_when_the_device_restarts(self):
        # The commit restarts the device, cutting off the reply to the request that
        # started it: that hold must end, or it counts against the device for ever.
        import struct
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        accepted = []
        threading.Thread(target=lambda: accepted.append(listener.accept()[0]), daemon=True).start()
        client = Device(f"127.0.0.1:{listener.getsockname()[1]}")
        with mock.patch.object(device, "_keep_alive", wraps=device._keep_alive) as kept:
            with self.assertRaises(TimeoutError):
                client._get("/cm", {"cmnd": "Br autoflash_busy(600000)"}, timeout=0.3)
        kept.assert_called_once()
        self.assertTrue(kept.call_args.args[0].getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE))
        self.assertEqual(device.held_requests(client.host), 1)
        # A restarted device refuses the old connection: here, reset at once.
        connection = accepted[0]
        connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        connection.close()
        self.assertTrue(held_ends(), "the cut-off request was still held")

    def test_the_web_page_check_still_says_no_at_once(self):
        client = self.start(delay=NOT_YET)
        self.server.slow_page = True
        self.assertIs(client.answers(timeout=0.3), False)
        self.assertFalse(self.server.answered.is_set())  # no, before the device answered
        self.server.release.set()
        self.assertTrue(self.server.answered.wait(10))

    def test_errors_reach_the_caller_as_before(self):
        client = self.start()
        with self.assertRaises(urllib.error.HTTPError) as caught:
            client._get("/missing", {}, timeout=5)
        self.assertEqual(caught.exception.code, 404)
        self.assertEqual(caught.exception.read(), b"not found")
        with self.assertRaises(urllib.error.HTTPError) as caught:
            client._get("/bad", {}, timeout=5)
        self.assertEqual(caught.exception.code, 400)
        self.assertTrue(client.answers(timeout=5))  # any answer is an answer
        self.assertEqual(client.command("Status"), {"Status": {}})

    def test_a_redirect_is_followed(self):
        client = self.start()
        for status in ("", "301", "302", "303", "307", "308"):
            self.assertEqual(client._get(f"/moved{status}", {}, timeout=5), '{"Status":{}}', status)

    def test_a_form_redirected_is_fetched_with_its_headers_but_its_body_type(self):
        client = Device(self.start().host, password="s3cret-71")
        sent = json.loads(client._request("POST", "/u2", {}, 5, body=b"x",
                                          headers={"Content-Type": "multipart/form-data; boundary=b"}))
        self.assertNotIn("Content-Type", sent)
        self.assertEqual(sent["Referer"], f"http://{client.host}/")
        self.assertTrue(sent["Authorization"].startswith("Basic "))

    def test_a_device_is_asked_on_port_80_unless_its_address_says_otherwise(self):
        for host, port in (("192.0.2.50", 80), ("192.0.2.50:8080", 8080)):
            with mock.patch.object(device.http.client, "HTTPConnection") as made:
                made.return_value.connect.side_effect = ConnectionRefusedError("refused")
                with self.assertRaises(urllib.error.URLError):
                    device._exchange(host, "GET", "/", {}, None, 1.0)
            made.assert_called_once_with("192.0.2.50", port, timeout=1.0)

    def test_nobody_there_is_a_url_error_and_nothing_is_held(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            with self.assertRaises(urllib.error.URLError):
                Device(f"127.0.0.1:{port}")._get("/", {}, timeout=2)
        self.assertEqual(device.held_requests(), 0)

    def test_a_web_password_is_still_reported(self):
        class Refusing(BusyTasmotaHandler):
            def do_GET(self):
                self.reply(401, b"")
        server = BusyTasmota()
        server.RequestHandlerClass = Refusing
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (server.shutdown(), server.server_close()))
        with self.assertRaisesRegex(DeviceError, "web UI has a password"):
            Device(f"127.0.0.1:{server.server_address[1]}")._get("/", {}, timeout=5)
        # Its commands refused without Tasmota's own words: a password all the same.
        with self.assertRaisesRegex(DeviceError, "web UI has a password"):
            Device(f"127.0.0.1:{server.server_address[1]}")._get("/cm", {}, timeout=5)


class BerryFireTests(unittest.TestCase):
    """A phase that restarts the device cuts its own reply off: that is no reply,
    not a crash (the E3 traceback on macOS)."""

    def test_a_reply_cut_off_is_no_reply(self):
        import http.client
        from tools.autoflash.device import Device

        device = Device("10.0.0.63")
        for error in (http.client.IncompleteRead(b""), ConnectionResetError(54, "reset")):
            with mock.patch.object(device, "berry", side_effect=error):
                self.assertIs(device.berry_fire('autoflash_commit("x")'), False)

    def test_other_errors_still_surface(self):
        from tools.autoflash.device import Device, DeviceError

        device = Device("10.0.0.63")
        with mock.patch.object(device, "berry", side_effect=DeviceError("password")):
            with self.assertRaises(DeviceError):
                device.berry_fire("x")


class RebootDetectionTests(unittest.TestCase):
    """A restart soon after another must still be seen - the D4 hang."""

    class Rebooting:
        """Answers like a device that reboots once, after `polls_until_reboot`."""

        def __init__(self, uptime_before, uptime_after, reboots=True):
            from tools.autoflash.device import Device

            outer = self

            class Fake(Device):
                def reachable(self, timeout=3.0):
                    return True

                def started_at(self):
                    return outer.marker()

                def uptime_seconds(self):
                    return outer.uptime()

            self.device = Fake("10.0.0.5")
            self.polls = 0
            self.reboots = reboots
            self.uptime_before = uptime_before
            self.uptime_after = uptime_after

        def rebooted(self):
            self.polls += 1
            return self.reboots and self.polls > 4

        def marker(self):
            return "2026-10-06T13:26:18#3" if self.rebooted() else "2026-10-06T13:25:45#2"

        def uptime(self):
            return self.uptime_after if self.polls > 4 and self.reboots else self.uptime_before + self.polls

    def wait(self, client, **kwargs):
        import tools.autoflash.device as device_module
        with mock.patch.object(device_module.time, "sleep"):
            return client.device.wait_for_reboot(timeout=0.5, settle=0, **kwargs)

    def test_a_restart_with_higher_uptime_than_before_is_seen(self):
        # D1 had just restarted the device: uptime 15s before the upload, and by
        # the time it was back on Wi-Fi after the next restart, 40s.
        client = self.Rebooting(uptime_before=15, uptime_after=40)
        self.wait(client, started_at="2026-10-06T13:25:45#2", before=15)

    def test_no_restart_is_not_mistaken_for_one_just_because_uptime_is_low(self):
        from tools.autoflash.device import DeviceError
        client = self.Rebooting(uptime_before=15, uptime_after=0, reboots=False)
        with self.assertRaises(DeviceError):
            self.wait(client, started_at="2026-10-06T13:25:45#2")

    class Clock:
        """Time that passes only as the code under test sleeps."""

        def __init__(self):
            self.now = 1000.0

        def monotonic(self):
            return self.now

        def sleep(self, seconds):
            self.now += seconds

    def wait_on_clock(self, device, clock, **kwargs):
        import tools.autoflash.device as device_module
        with mock.patch.object(device_module, "time", clock):
            return device.wait_for_reboot(timeout=60, settle=0, **kwargs)

    def test_a_restart_answered_late_on_a_device_with_no_clock_is_seen(self):
        # F4's Reset 4 at 10s of uptime, just as Tasmota counted the boot: with
        # no clock both boots read "#1", and the request the restart cut off
        # held the first answer back until the new boot was 14s up.
        from tools.autoflash.device import Device
        clock = self.Clock()
        start, restart = clock.now, clock.now + 3.2

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return not restart <= clock.now < restart + 12

            def started_at(self):
                return "#1"

            def uptime_seconds(self):
                if clock.now < restart:
                    return 10 + int(clock.now - start)
                return int(clock.now - restart)

        self.wait_on_clock(Fake("10.0.0.5"), clock, started_at="#1", before=10)

    def test_a_device_with_no_clock_that_does_not_restart_is_still_waited_for(self):
        from tools.autoflash.device import Device, DeviceError
        clock = self.Clock()
        start = clock.now

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return True

            def started_at(self):
                return "#1"

            def uptime_seconds(self):
                return 10 + int(clock.now - start)

        with self.assertRaises(DeviceError):
            self.wait_on_clock(Fake("10.0.0.5"), clock, started_at="#1", before=10)

    def answering(self, clock, *, offline=(), boot=None, uptime):
        """A device answering on `clock`: not at the seconds in `offline`, its
        boot `boot` (a function of the seconds waited, or None for no answer),
        its uptime `uptime` of them."""
        from tools.autoflash.device import Device
        start = clock.now

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return clock.now - start not in offline

            def started_at(self):
                return boot(clock.now - start) if boot else None

            def uptime_seconds(self):
                return uptime(clock.now - start)

        return Fake("10.0.0.5")

    def settled(self, client, clock, **kwargs):
        import tools.autoflash.device as device_module
        with mock.patch.object(device_module, "time", clock):
            return client.wait_for_reboot(timeout=60, settle=4, **kwargs)

    def test_each_way_a_restart_is_seen_settles_and_says_how_long_it_waited(self):
        # Seen at the first look, 3 s in, then 4 s to settle.
        clock = self.Clock()
        another_boot = self.answering(clock, boot=lambda waited: "#2", uptime=lambda waited: 100)
        self.assertEqual(self.settled(another_boot, clock, started_at="#1"), 7.0)
        clock = self.Clock()
        younger_than_the_wait = self.answering(clock, boot=lambda waited: "#1", uptime=lambda waited: 0)
        self.assertEqual(self.settled(younger_than_the_wait, clock, started_at="#1"), 7.0)
        clock = self.Clock()
        clock_went_back = self.answering(clock, uptime=lambda waited: 5)
        self.assertEqual(self.settled(clock_went_back, clock, before=10), 7.0)

    def test_a_device_up_a_moment_before_the_wait_is_not_taken_for_restarted(self):
        from tools.autoflash.device import DeviceError
        clock = self.Clock()
        same_boot = self.answering(clock, boot=lambda waited: "#1", uptime=lambda waited: 1 + int(waited))
        with self.assertRaises(DeviceError):
            self.settled(same_boot, clock, started_at="#1")

    def test_with_nothing_to_go_on_only_going_away_is_a_restart(self):
        from tools.autoflash.device import DeviceError
        clock = self.Clock()
        never_away = self.answering(clock, uptime=lambda waited: 600)
        with self.assertRaises(DeviceError):
            self.settled(never_away, clock)
        clock = self.Clock()
        away_once = self.answering(clock, offline={3.0}, uptime=lambda waited: 600)
        self.assertEqual(self.settled(away_once, clock), 10.0)

    def test_an_uptime_question_the_restart_cuts_off_is_asked_again(self):
        import urllib.error
        from tools.autoflash.device import Device
        clock = self.Clock()
        answers = iter([urllib.error.URLError("connection reset"), 3])

        class Fake(Device):
            def reachable(self, timeout=3.0):
                return True

            def uptime_seconds(self):
                answer = next(answers)
                if isinstance(answer, Exception):
                    raise answer
                return answer

        self.wait_on_clock(Fake("10.0.0.5"), clock, before=10)


class BootIdentityTests(unittest.TestCase):
    """started_at: Tasmota counts a boot once it has been up 10 s, so a count
    read before then is waited out - it went up by one in the same boot, and
    an upload cut off was taken for one the device took and booted."""

    def boots(self, *uptimes):
        """What started_at says of a device answering Status 1 with each uptime in
        turn, its boot counted from 10 s on; and how long it waited."""
        answers = iter(uptimes)
        slept = []

        def command(cmnd, timeout=None):
            uptime = next(answers)
            return {"StatusPRM": {"Uptime": f"0T00:00:{uptime:02d}", "StartupUTC": "",
                                  "BootCount": 3 if uptime >= 10 else 2}}

        client = Device("192.0.2.50")
        with mock.patch.object(client, "command", side_effect=command), \
             mock.patch.object(device.time, "sleep", side_effect=slept.append):
            return client.started_at(), slept

    def test_a_boot_not_counted_yet_is_waited_for(self):
        self.assertEqual(self.boots(9, 11), ("#3", [2]))

    def test_a_counted_boot_is_read_at_once(self):
        self.assertEqual(self.boots(40), ("#3", []))

    def test_uptime_as_tasmota_writes_it(self):
        self.assertEqual(device.uptime_of("1T02:03:04"), 93784)
        self.assertIsNone(device.uptime_of(None))


class AtNextSaveTests(unittest.TestCase):
    """The Berry driver that puts commands in as Tasmota saves before a restart."""

    def test_the_commands_go_in_a_driver_for_save_before_restart(self):
        from tools.autoflash.device import Device
        client = Device("192.0.2.1")
        sent = []
        client.command = lambda c, timeout=None: sent.append(c) or {"Br": "nil" if len(sent) == 1 else "AutoflashAtSave"}
        self.assertTrue(client.at_next_save(["Hostname x", 'Template {"NAME":"S60"}']))
        self.assertEqual(sent[1], "Br classname(autoflash_at_save)")
        self.assertTrue(sent[0].startswith("Br import global class AutoflashAtSave"))
        self.assertIn("def save_before_restart()", sent[0])
        self.assertIn("""['Hostname x','Template {"NAME":"S60"}']""", sent[0])

    def test_a_firmware_without_berry_says_no(self):
        from tools.autoflash.device import Device
        client = Device("192.0.2.1")
        client.command = lambda c, timeout=None: {"Command": "Unknown"}
        self.assertIs(client.at_next_save(["Hostname x"]), False)
        # And Berry that would not take it: its error, not the class, comes back.
        client.command = lambda c, timeout=None: {"Br": "[syntax_error] input:1: strict: ..."}
        self.assertFalse(client.at_next_save(["Hostname x"]))

    def test_a_quote_cannot_reach_the_berry(self):
        from tools.autoflash.device import Device
        with self.assertRaises(ValueError):
            Device("192.0.2.1").at_next_save(["Hostname it's"])

    def test_it_runs_once_and_takes_no_command_that_saves(self):
        # SaveData saves the settings itself (SettingsSaveAll), which calls
        # the driver again: in the emulator, again and again until Tasmota's
        # stack overflowed and it restarted unsaved, its settings lost.
        from tools.autoflash.device import Device
        client = Device("192.0.2.1")
        sent = []
        client.command = lambda c, timeout=None: sent.append(c) or {"Br": "AutoflashAtSave"}
        client.at_next_save(["Hostname x"])
        self.assertIn("if self.done return end self.done=true", sent[0])
        for command in ("SaveData 1", "Backlog Hostname x; Module 0", "Restart 1", "Reset 4"):
            with self.assertRaises(ValueError):
                client.at_next_save([command])


class FakeTasmota(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), FakeTasmotaHandler)
        self.visits: list[str] = []
        self.armed_for_settings = False
        self.restored: bytes | None = None


class FakeTasmotaHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def reply(self, body: bytes, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        server: FakeTasmota = self.server
        path = urllib.parse.urlsplit(self.path).path
        server.visits.append(path)
        if path == "/dl":
            self.reply(b"BACKUP")
        elif path == "/cs":
            server.referer = self.headers.get("Referer")
            # As a real device frames it: "<index>}1<reset>}1<lines>}1".
            self.reply(b"47}11}100:00:00.255 Autoflash recovery Wi-Fi defaults active\n"
                       b"00:00:00.316 Autoflash recovery: Wi-Fi settings saved for the next firmware}1")
        else:
            server.armed_for_settings = path == "/rs"  # Tasmota: HandleRestoreConfiguration
            self.reply(b"<title>Tasmota</title>")

    def do_POST(self):
        server: FakeTasmota = self.server
        server.visits.append(urllib.parse.urlsplit(self.path).path)
        body = self.rfile.read(int(self.headers["Content-Length"]))
        if server.armed_for_settings:
            start = body.index(b"\r\n\r\n") + 4
            server.restored = body[start : body.rindex(b"\r\n--")]
        self.reply(b"Upload successful")


class SettingsBackupTests(unittest.TestCase):
    def setUp(self):
        self.server = FakeTasmota()
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(lambda: (self.server.shutdown(), self.server.server_close()))
        self.device = Device(f"127.0.0.1:{self.server.server_address[1]}")

    def test_a_backup_is_downloaded(self):
        self.assertEqual(self.device.download_settings(), b"BACKUP")

    def test_the_console_log_is_read_with_a_referer(self):
        lines = self.device.console_log()
        self.assertEqual(lines[0], "00:00:00.255 Autoflash recovery Wi-Fi defaults active")
        self.assertEqual(lines[-1], "00:00:00.316 Autoflash recovery: Wi-Fi settings saved for the next firmware")
        self.assertTrue(self.server.referer)  # without one the page closes the connection

    def test_answering_is_checked_without_a_console_command(self):
        # A command's output would push the device's own last words out of its
        # short console log; the main page leaves nothing there.
        self.assertTrue(self.device.answers())
        self.assertFalse(Device("127.0.0.1:1").answers(timeout=0.5))

    def test_a_restore_arms_the_restore_page_first(self):
        # Without visiting /rs first, Tasmota would take the file as firmware.
        self.device.restore_settings(b"\x09\x12" + bytes(4094))
        self.assertEqual(self.server.visits, ["/rs", "/u2"])
        self.assertEqual(self.server.restored, b"\x09\x12" + bytes(4094))


# The page Tasmota 15.6.0 sent back for an image it would not take (the
# emulator, an upload too big for its slot), and the end of one it took.
REFUSED_PAGE = ("<div style='text-align:center;color:var(--c_ttl);'><h3>Sonoff S60TPG</h3><h2>Tasmota</h2></div>"
                "<div style='text-align:center;'><b>Upload <font color='#ff5661'>Failed</font></b><br><br>"
                "Not enough space</div><br><div></div><p></p><form id=but2 style=\"display:block;\" "
                "action='.' method='get'><button>Main Menu</button></form>")
TAKEN_PAGE = ("<div style='text-align:center;'><b>Upload <font color='#47c266'>Successful</font></b><br>"
              "<br>Device will restart in a few seconds</div>")


class UploadRefusalTests(unittest.TestCase):
    """A refused upload comes back as an ordinary page, which must be read: an
    image damaged on the way was once taken for a good one, and the run waited
    five minutes on a restart that never came."""

    def test_the_reason_is_read_off_the_page(self):
        self.assertEqual(device.upload_refusal(REFUSED_PAGE), "Not enough space")
        self.assertIsNone(device.upload_refusal(TAKEN_PAGE))
        self.assertIsNone(device.upload_refusal(""))

    def test_a_refused_upload_raises(self):
        from unittest import mock
        client = Device("192.0.2.1")
        with mock.patch.object(client, "_get"), \
             mock.patch.object(client, "_request", return_value=REFUSED_PAGE.encode()):
            with self.assertRaises(device.UploadRefused) as raised:
                client._upload("/up", "tasmota32c3.bin", b"\xe9" * 16, 10)
        self.assertEqual(raised.exception.reason, "Not enough space")
        self.assertEqual(str(raised.exception), "the device refused the upload: Not enough space")
        self.assertIsInstance(raised.exception, DeviceError)

    def test_a_taken_upload_returns_its_page(self):
        from unittest import mock
        client = Device("192.0.2.1")
        with mock.patch.object(client, "_get"), \
             mock.patch.object(client, "_request", return_value=TAKEN_PAGE.encode()):
            self.assertIn("Successful", client._upload("/up", "tasmota32c3.bin", b"\xe9" * 16, 10))


class VersionFromPageTests(unittest.TestCase):
    """A safeboot answers no Status: its version is read off its page, with
    the build's name, which tells Tasmota's own from this tool's."""

    def version(self, footer):
        from unittest import mock
        client = Device("192.0.2.1")
        page = f"<h3>SAFEBOOT</h3>...<div style='text-align:right;font-size:11px;'><hr/>{footer}</div>"
        with mock.patch.object(client, "_get", return_value=page):
            return client.version_from_page()

    def test_tasmotas_own(self):
        self.assertEqual(self.version("<a href='https://bit.ly/tasmota' target='_blank'>Tasmota 15.6.0 "
                                      "(release-safeboot) by Theo Arends</a>"), "15.6.0 (release-safeboot)")

    def test_one_built_here(self):
        self.assertEqual(self.version("Tasmota 15.6.0 (safeboot) by Theo Arends"), "15.6.0 (safeboot)")

    def test_no_build_name_still_says_safeboot(self):
        self.assertEqual(self.version("Tasmota 15.6.0 by Theo Arends"), "15.6.0 (safeboot)")


class StartAppTests(unittest.TestCase):
    """Tasmota's safeboot asked to start the Tasmota it holds, as its page's
    switch does - its only way: a safeboot answers no commands."""

    def test_the_switch_its_page_has(self):
        from unittest import mock
        client = Device("192.0.2.1")
        with mock.patch.object(client, "_get", return_value="false") as get:
            self.assertEqual(client.start_app(), "false")
        self.assertEqual(get.call_args.args[:2], ("/u4", {"u4": "ota", "api": ""}))


class LockedTasmota(http.server.ThreadingHTTPServer):
    """Answers as Tasmota with a web password does (Tasmota 15.6.0's
    xdrv_01_9_webserver.ino): its command interface takes the password as
    HTTP Basic or in the query, and refuses without it in its own words;
    every other page takes it as HTTP Basic only.  Notes each request, and
    the Authorization it came with."""
    daemon_threads = True
    PASSWORD = "s3cret-71"
    STATUS = {"Status": {"Module": "Sonoff S60TPG"}, "StatusNET": {"Hostname": "tasmota-ABCDEF-1234"},
              "StatusFWR": {"Version": "15.6.0(release-tasmota32)"}}

    def __init__(self):
        super().__init__(("127.0.0.1", 0), LockedTasmotaHandler)
        self.seen: list[tuple[str, str | None]] = []
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def host(self) -> str:
        return f"127.0.0.1:{self.server_address[1]}"

    def stop(self) -> None:
        self.shutdown()
        self.server_close()


class LockedTasmotaHandler(BusyTasmotaHandler):
    def do_GET(self):
        server: LockedTasmota = self.server
        authorization = self.headers.get("Authorization")
        server.seen.append((self.path, authorization))
        path, _, query = self.path.partition("?")
        params = urllib.parse.parse_qs(query)
        basic = authorization == "Basic " + base64.b64encode(f"admin:{server.PASSWORD}".encode()).decode()
        if path == "/cm":
            commands = {"Status 0": server.STATUS, "Status 2": {"StatusFWR": server.STATUS["StatusFWR"]},
                        "Template": {"NAME": "Sonoff S60TPG"}}
            if not (basic or (params.get("user") == ["admin"] and params.get("password") == [server.PASSWORD])):
                self.reply(401, b'{"WARNING":"Need user=<username>&password=<password>"}')
            elif "cmnd" not in params:
                self.reply(200, b'{"WARNING":"Enter command cmnd="}')
            else:
                self.reply(200, json.dumps(commands.get(params["cmnd"][0], {"Command": "Unknown"})).encode())
        elif basic:
            self.reply(200, b"<title>Tasmota</title>")
        else:
            self.reply(401, b"401 Unauthorized", {"WWW-Authenticate": 'Basic realm="Login Required"'})


class WebPasswordTests(unittest.TestCase):
    """A device on Tasmota with a web password: given it, every page is
    reached; without it, the device says so, in words saying what to do."""

    def setUp(self):
        self.client = LockedTasmota()
        self.addCleanup(self.client.stop)

    def test_it_says_when_it_wants_one_and_runs_nothing_to_say_it(self):
        self.assertTrue(Device(self.client.host).wants_password())
        self.assertTrue(Device(self.client.host, password="not-it-9").wants_password())
        self.assertIs(Device(self.client.host, password=LockedTasmota.PASSWORD).wants_password(), False)
        self.assertEqual({path.partition("?")[0] for path, _ in self.client.seen}, {"/cm"})
        self.assertFalse([path for path, _ in self.client.seen if "cmnd=" in path])

    def test_nobody_answering_is_neither(self):
        with socket.socket() as spare:
            spare.bind(("127.0.0.1", 0))
            port = spare.getsockname()[1]
            # Held, not listening, while asked: refused, and no other test can take it.
            self.assertIsNone(Device(f"127.0.0.1:{port}").wants_password(timeout=2))

    def test_a_command_refused_for_it_says_how_to_give_it(self):
        with self.assertRaisesRegex(device.NeedsPassword, "not given: give it with --web-password"):
            Device(self.client.host).command("Status 0")
        with self.assertRaisesRegex(device.NeedsPassword, "the one given is not it"):
            Device(self.client.host, password="not-it-9").command("Status 0")

    def test_given_it_every_page_is_reached_its_query_holding_it_only_for_commands(self):
        client = Device(self.client.host, password=LockedTasmota.PASSWORD)  # no user: Tasmota's admin
        self.assertEqual(client.command("Status 0"), LockedTasmota.STATUS)
        self.assertIn("Tasmota", client._get("/", {}, 5))
        basic = "Basic " + base64.b64encode(f"admin:{LockedTasmota.PASSWORD}".encode()).decode()
        self.assertEqual({authorization for _, authorization in self.client.seen}, {basic})
        commands = [path for path, _ in self.client.seen if path.startswith("/cm?")]
        pages = [path for path, _ in self.client.seen if not path.startswith("/cm?")]
        self.assertTrue(commands and all(f"password={LockedTasmota.PASSWORD}" in path for path in commands))
        self.assertTrue(pages and all(LockedTasmota.PASSWORD not in path for path in pages))


class FakeDevice:
    """A device's flash, read as the tool reads it through Berry."""

    def __init__(self, flash: bytes):
        self.flash = bytearray(flash)
        self.reads = []

    def read_region(self, address, length, timeout=30.0):
        self.reads.append((address, length))
        return bytes(self.flash[address:address + length])

    read_nvs = device.Device.read_nvs

    def sha_region(self, address, length, timeout=180.0):
        return plans.sha256(bytes(self.flash[address:address + length]))


class DeviceReadTests(unittest.TestCase):
    def test_flash_is_read_in_pieces_as_hex(self):
        flash = bytes(range(256)) * 64
        asked = []

        def berry_value(code, name, timeout=0):
            address, size = map(int, re.search(r"flash\.read\((\d+),(\d+)\)", code).groups())
            asked.append(size)
            return '"' + flash[address:address + size].hex() + '"'

        client = device.Device("192.0.2.50")
        with mock.patch.object(client, "berry_value", side_effect=berry_value):
            self.assertEqual(client.read_region(100, 2000), flash[100:2100])
        self.assertEqual(asked, [512, 512, 512, 464])
        with mock.patch.object(client, "berry_value", return_value="unknown"):
            with self.assertRaises(device.DeviceError):
                client.read_region(0, 4)

    def test_an_nvs_is_read_but_for_its_unused_pages(self):
        client = FakeDevice(bytes(0x9000) + sonoff_nvs())
        self.assertEqual(client.read_nvs(0x9000, 0x10000), sonoff_nvs())
        self.assertEqual(sum(1 for _, length in client.reads if length == 0x1000), 1)

    def test_each_used_page_is_read_whole_into_its_place(self):
        pages = [b"\x01" * 0x1000, b"\xff" * 0x1000, b"\x02" * 0x1000, b"\xff" * 0x1000]
        client = FakeDevice(bytes(0x9000) + b"".join(pages))
        self.assertEqual(client.read_nvs(0x9000, 0x4000), b"".join(pages))
        self.assertEqual(client.reads, [(0x9000, 4), (0x9000, 0x1000), (0xa000, 4),
                                        (0xb000, 4), (0xb000, 0x1000), (0xc000, 4)])


if __name__ == "__main__":
    unittest.main()
