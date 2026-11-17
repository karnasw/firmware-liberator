"""stub_server.py, the local stand-in for eWeLink's cloud both emulators use."""

import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from tools.ewelink.stub_cloud import stub_server

STUB = Path(__file__).resolve().parents[3] / "tools" / "ewelink" / "stub_cloud"


class RunIsolatedTests(unittest.TestCase):
    """Run as the emulator runs it: in isolated mode (-I), where only its own
    path puts the repository on sys.path."""

    def test_it_imports_the_repository(self):
        done = subprocess.run([sys.executable, "-I", str(STUB / "stub_server.py"), "--help"],
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("usage:", done.stdout)


class FirmwareOfferTests(unittest.TestCase):
    """Asked as query_ota.latest_offer asks, with a version older than any,
    the stub offers the device its own firmware, and serves it to its id."""

    def setUp(self):
        from tools.ewelink.stub_cloud.firmware import Update
        self.stub = stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef", "devicekey": "k"},
                                     Update("1.2.0", b"the file"))

    def query(self, version, deviceid="1000abcdef"):
        body = json.dumps({"deviceInfoList": [{"deviceid": deviceid, "model": "m", "version": version}]})
        return self.stub.ota_query(body.encode())["data"]["otaInfoList"]

    def test_offered_only_to_a_device_behind_it(self):
        offer, = self.query("0.0.1")
        self.assertEqual((offer["version"], offer["binList"][0]["name"]), ("1.2.0", "user1.bin"))
        self.assertEqual(offer["binList"][0]["downloadUrl"], "https://192.0.2.1/ota/user1.bin")
        self.assertEqual(self.query("1.2.0"), [])  # the conversion's own question: nothing on offer
        self.assertEqual(self.query("0.0.1", "1000fedcba"), [])
        self.assertEqual(stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef"}).ota_query(b"{}"),
                         {"error": 0, "data": {"otaInfoList": []}})

    def test_a_register_says_whether_it_is_the_accounts_own_device(self):
        handler = stub_server.Handler.__new__(stub_server.Handler)
        handler.stub = stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef", "apikey": "a",
                                                                "devicekey": "a"})
        for deviceid, key, said in (("1000abcdef", "a", ("yes", "yes")), ("1000fedcba", "b", ("NO", "NO"))):
            with mock.patch.dict(stub_server._devices, {}, clear=True), \
                    mock.patch.object(stub_server, "log") as logged:
                handler._device_message(mock.Mock(peer="test"), {"action": "register", "deviceid": deviceid,
                                                                 "apikey": key}, None)
            line = next(call.args for call in logged.call_args_list if "register:" in call.args[1])
            self.assertEqual((line[2], line[4]), said)
            self.assertNotIn(deviceid, " ".join(map(str, line)))

    def test_the_file_goes_to_the_devices_own_id(self):
        self.assertEqual(self.stub.ota_file("deviceid=1000abcdef&ts=1&sign=0"), b"the file")
        self.assertIsNone(self.stub.ota_file("deviceid=1000fedcba&ts=1&sign=0"))
        self.assertIsNone(self.stub.ota_file(""))


class DispatchTests(unittest.TestCase):
    def test_both_fields_hold_the_address_by_default(self):
        answer = stub_server.Stub("192.0.2.1", 443, 10).dispatch_response()
        self.assertEqual((answer["IP"], answer["domain"], answer["port"]), ("192.0.2.1", "192.0.2.1", 443))

    def test_a_domain_given_is_a_name_and_the_address_stays_numeric(self):
        # The MINIR4 connects by `domain` and checks the certificate against it.
        answer = stub_server.Stub("192.0.2.1", 443, 10, iot_domain="iot.autoflash-stub.test").dispatch_response()
        self.assertEqual((answer["IP"], answer["domain"]), ("192.0.2.1", "iot.autoflash-stub.test"))

    def test_what_goes_on_the_wire_is_compact_json_as_ewelinks_is(self):
        # The BASICR4's parser turns down {"error": 0}: a space after the colon.
        self.assertEqual(stub_server.wire({"error": 0, "IP": "192.0.2.1"}), b'{"error":0,"IP":"192.0.2.1"}')


class SendToDeviceTests(unittest.TestCase):
    def test_a_command_reaches_the_device_online_and_no_other(self):
        conn = mock.Mock()
        with mock.patch.dict(stub_server._devices, {"1000abcdef": conn}, clear=True):
            self.assertTrue(stub_server.send_to_device("1000abcdef", {"action": "upgrade"}))
            self.assertFalse(stub_server.send_to_device("1000fedcba", {"action": "upgrade"}))
        self.assertEqual(json.loads(conn.send.call_args.args[0]), {"action": "upgrade"})


class OnlineTests(unittest.TestCase):
    """The app watching a device is told when it comes and goes, as at eWeLink."""

    def test_a_device_registering_is_said_to_be_online_to_its_watchers(self):
        # Seen: a device back after a power cut was never said to be online, so the
        # sender did not send again the command it had lost, and waited 300 s.
        app = mock.Mock()
        handler = stub_server.Handler.__new__(stub_server.Handler)
        handler.stub = stub_server.Stub("192.0.2.1", 443, 10)
        with mock.patch.dict(stub_server._devices, {}, clear=True), \
                mock.patch.dict(stub_server._subscribers, {"1000abcdef": {app}}, clear=True), \
                mock.patch.object(stub_server, "log"):
            handler._device_message(mock.Mock(peer="test"), {"action": "register", "deviceid": "1000abcdef",
                                                             "apikey": "made-up"}, None)
        told = [json.loads(call.args[0]) for call in app.send.call_args_list]
        self.assertIn({"action": "sysmsg", "deviceid": "1000abcdef", "params": {"online": True}}, told)

    def test_going_offline_is_said_too(self):
        app = mock.Mock()
        with mock.patch.dict(stub_server._subscribers, {"1000abcdef": {app}}, clear=True):
            stub_server._tell_watchers("1000abcdef", False)
        self.assertEqual(json.loads(app.send.call_args.args[0])["params"], {"online": False})


class OtherDevicesTests(unittest.TestCase):
    """The account's other devices, none emulated, listed beside the device."""

    OTHER = {"deviceid": "1000000102", "name": "Hall panel", "productModel": "NSPanel",
             "model": "E32-MSW-NX", "uiid": 133, "fwVersion": "1.5.0"}

    def test_listed_after_the_device_offline_with_their_own_record(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef", "devicekey": "k"},
                                others=[self.OTHER])
        mine, other = [entry["itemData"] for entry in stub.device_thing()["data"]["thingList"]]
        self.assertEqual(mine["deviceid"], "1000abcdef")
        self.assertEqual((other["deviceid"], other["name"], other["productModel"], other["online"]),
                         ("1000000102", "Hall panel", "NSPanel", False))
        self.assertEqual((other["extra"], other["params"]), ({"uiid": 133, "model": "E32-MSW-NX"},
                                                             {"fwVersion": "1.5.0"}))

    def test_listed_before_the_device_registers_too(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10, {"staMac": "12:34:56:ab:cd:ef"}, others=[self.OTHER])
        self.assertEqual([entry["itemData"]["deviceid"] for entry in stub.device_thing()["data"]["thingList"]],
                         ["1000000102"])

    def test_offered_no_firmware(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef", "devicekey": "k"},
                                update=mock.Mock(version="9.9.9", data=b"x"), others=[self.OTHER])
        asked = json.dumps({"deviceInfoList": [{"deviceid": "1000000102", "model": "E32-MSW-NX",
                                                "version": "0.0.1"}]}).encode()
        self.assertEqual(stub.ota_query(asked)["data"]["otaInfoList"], [])

    def test_read_from_the_state_directory_where_emu_sh_puts_them(self):
        emu = (Path(__file__).resolve().parents[3] / "tools/espressif/emulator/emu.sh").read_text(encoding="utf-8")
        self.assertIn('cp "$AUTOFLASH_EMU_STUB_OTHERS" "$STATE/stub-others.json"', emu)
        source = Path(stub_server.__file__).read_text(encoding="utf-8")
        self.assertIn('default=Path("/state/stub-others.json")', source)


class AccountDeviceTests(unittest.TestCase):
    """A device whose identity the stub is not given - the S61's, sealed in its
    store - is listed as it registers, as eWeLink lists it."""

    def stub(self, device=None):
        return stub_server.Stub("192.0.2.1", 443, 10, device)

    def test_nothing_listed_before_the_device_registers(self):
        stub = self.stub({"staMac": "12:34:56:ab:cd:ef", "model": "SN-BK7238-S61-01"})
        self.assertEqual(stub.device_thing()["error"], 404)

    def test_the_registered_identity_completes_the_record(self):
        stub = self.stub({"staMac": "12:34:56:ab:cd:ef", "model": "SN-BK7238-S61-01"})
        stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up-apikey",
                    "romVersion": "1.0.2"})
        item = stub.device_thing()["data"]["thingList"][0]["itemData"]
        self.assertEqual((item["deviceid"], item["apikey"]), ("1000abcdef", "made-up-apikey"))
        self.assertEqual(item["extra"]["model"], "SN-BK7238-S61-01")
        self.assertEqual(item["params"]["staMac"], "12:34:56:ab:cd:ef")
        self.assertEqual(item["params"]["fwVersion"], "1.0.2")
        self.assertEqual(stub.account_login()["data"]["user"]["apikey"], "made-up-apikey")

    def test_a_device_converted_since_is_listed_with_what_it_last_reported(self):
        # As eWeLink keeps its record: a readout of a device on Tasmota, which
        # never speaks to the cloud again, found no firmware version on a fresh
        # stub, and so no firmware offered (the fault suite's none-first-stage).
        import tempfile
        with tempfile.TemporaryDirectory() as folder:
            kept = Path(folder, "stub-reported.json")
            device = {"deviceid": "1000abcdef", "devicekey": "k"}
            stub_server.Stub("192.0.2.1", 443, 10, device, reported_file=kept).heard(
                {"action": "update", "deviceid": "1000abcdef", "params": {"fwVersion": "1.2.0"}})
            again = stub_server.Stub("192.0.2.1", 443, 10, device, reported_file=kept)
            self.assertEqual(again.device_thing()["data"]["thingList"][0]["itemData"]["params"]["fwVersion"], "1.2.0")
            kept.write_text("not json", encoding="utf-8")   # spoilt: none kept, nothing assumed
            self.assertEqual(stub_server.Stub("192.0.2.1", 443, 10, device, reported_file=kept).reported, {})

    def test_the_relay_is_off_until_the_device_reports_it(self):
        # The POW Elite's and POWCT's firmware reports its relay only when it changes.
        stub = self.stub({"deviceid": "1000abcdef", "devicekey": "k"})
        params = lambda: stub.device_thing()["data"]["thingList"][0]["itemData"]["params"]
        self.assertEqual(params()["switches"], [{"switch": "off", "outlet": 0}])
        stub.heard({"action": "update", "deviceid": "1000abcdef",
                    "params": {"switches": [{"switch": "on", "outlet": 0}, {"switch": "off", "outlet": 1}]}})
        self.assertEqual(params()["switches"], [{"switch": "on", "outlet": 0}, {"switch": "off", "outlet": 1}])

    def test_a_single_switch_reported_leaves_no_list(self):
        # The TH Elite's Sonoff 1.0.2 reports one switch, and answers a list with 400.
        stub = self.stub({"deviceid": "1000abcdef", "devicekey": "k"})
        stub.heard({"action": "update", "deviceid": "1000abcdef", "params": {"switch": "on", "startup": "off"}})
        params = stub.device_thing()["data"]["thingList"][0]["itemData"]["params"]
        self.assertEqual(params["switch"], "on")
        self.assertNotIn("switches", params)

    def test_a_given_identity_is_kept(self):
        stub = self.stub({"deviceid": "1000fedcba", "devicekey": "k", "apikey": "a"})
        stub.heard({"action": "register", "deviceid": "1000abcdef", "apikey": "other"})
        item = stub.device_thing()["data"]["thingList"][0]["itemData"]
        self.assertEqual(item["deviceid"], "1000fedcba")


class SignInTests(unittest.TestCase):
    """Each sign-in ends the one before, as at eWeLink."""

    def setUp(self):
        self.stub = stub_server.Stub("192.0.2.1", 443, 10, {"deviceid": "1000abcdef", "devicekey": "k"})
        self.handler = stub_server.Handler.__new__(stub_server.Handler)
        self.handler.stub = self.stub
        self.sent = []
        patcher = mock.patch.object(stub_server.Handler, "_send_http",
                                    lambda _self, sock, status, body, ctype: self.sent.append(json.loads(body)))
        patcher.start()
        self.addCleanup(patcher.stop)

    def asked_with(self, token):
        self.handler._serve_signed_in(None, {"authorization": f"Bearer {token}"}, self.stub.device_thing, "peer", "list")
        return self.sent[-1]["error"]

    def test_a_later_sign_in_ends_the_earlier_token(self):
        first = self.stub.account_login()["data"]["at"]
        self.assertEqual(self.asked_with(first), 0)
        second = self.stub.account_login()["data"]["at"]
        self.assertNotEqual(first, second)
        self.assertEqual(self.asked_with(first), 401)
        self.assertEqual(self.asked_with(second), 0)

    def test_nothing_is_answered_before_a_sign_in(self):
        self.handler._serve_signed_in(None, {}, self.stub.device_thing, "peer", "list")
        self.assertEqual(self.sent[-1]["error"], 401)

    def test_an_ended_token_cannot_open_the_app_channel(self):
        first = self.stub.account_login()["data"]["at"]
        conn = mock.Mock(peer="peer")
        self.handler._app_message(conn, {"action": "userOnline", "at": first})
        self.assertEqual(json.loads(conn.send.call_args.args[0])["error"], 0)
        self.stub.account_login()
        self.handler._app_message(conn, {"action": "userOnline", "at": first})
        self.assertEqual(json.loads(conn.send.call_args.args[0])["error"], 401)


class TimeZoneTests(unittest.TestCase):
    def test_a_time_zone_query_is_answered_with_one(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        reply = stub.ws_reply({"action": "query", "deviceid": "1000abcdef", "params": ["TZ"]})
        tz = reply["params"]["TZ"]
        self.assertEqual((tz["dst"], tz["offset"], tz["nextOffset"]), (0, 0, 0))
        self.assertRegex(tz["utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertGreater(tz["nextDst"], tz["utc"])

    def test_another_query_is_answered_plainly(self):
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        self.assertNotIn("params", stub.ws_reply({"action": "query", "params": ["switch"]}))


class SilentDeviceTests(unittest.TestCase):
    """A device switched off sends no close: missing its heartbeats, it goes
    offline, as at eWeLink."""

    def test_a_silent_device_is_dropped(self):
        import socket
        import threading
        stub = stub_server.Stub("192.0.2.1", 443, 10)
        stub.silence_limit = lambda: 0.5
        ours, theirs = socket.socketpair()
        handler = stub_server.Handler.__new__(stub_server.Handler)
        handler.stub = stub
        register = json.dumps({"action": "register", "deviceid": "1000abcdef", "apikey": "made-up"}).encode()
        theirs.sendall(bytes([0x81, len(register)]) + register)
        with mock.patch.dict(stub_server._devices, {}, clear=True), mock.patch.object(stub_server, "log"):
            serving = threading.Thread(target=handler._serve_ws, args=(ours, {}, "test"))
            serving.start()
            serving.join(10)
            self.assertFalse(serving.is_alive())
            self.assertFalse(stub_server._online("1000abcdef"))
        ours.close()
        theirs.close()


if __name__ == "__main__":
    unittest.main()
