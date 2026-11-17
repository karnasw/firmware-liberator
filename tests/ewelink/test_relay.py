import unittest
from unittest import mock

from tools.ewelink import relay


class Device:
    """The cloud's socket as the app sees it, with a device behind it that
    reports each switch it is sent - or, `obeys` False, none."""

    def __init__(self, obeys=True, error=0, reports=False, knows_readings=True, meter=False):
        self.sent, self.obeys, self.error, self.on, self.reports = [], obeys, error, False, reports
        self.knows_readings, self.meter, self.answered = knows_readings, meter, 0

    def send_json(self, value):
        self.sent.append(value)
        self.answered = 0   # times what was last sent was answered

    def receive_json(self, timeout=None):
        last = self.sent[-1]
        if last.get("action") == "userOnline":
            return {"error": 0}
        if self.error:
            return {"error": self.error, "sequence": last["sequence"]}
        if not self.obeys:
            raise relay.socket.timeout
        if "uiActive" in last["params"]:   # its readings asked for: answered, then sent
            if not self.knows_readings:    # a device with no meter says nothing
                raise relay.socket.timeout
            self.answered += 1
            if self.answered == 1:
                return {"error": 0, "deviceid": "1000abcdef", "sequence": last["sequence"]}
            if self.meter and self.answered == 2:   # an S60's, a 100 W load on 230 V
                return {"userAgent": "device", "action": "update", "deviceid": "1000abcdef",
                        "params": {"voltage": 23008 if self.on else 0, "current": 43 if self.on else 0,
                                   "power": 9999 if self.on else 0}}
            raise relay.socket.timeout
        if "sledOnline" in last["params"]:
            return {"error": 0, "deviceid": "1000abcdef", "sequence": last["sequence"]}
        self.on = last["params"]["switches"][0]["switch"] == "on"
        if self.reports:   # its new state, as some devices say it
            return {"action": "update", "deviceid": "1000abcdef",
                    "params": {"switches": [{"outlet": 0, "switch": "on" if self.on else "off"}]}}
        return {"error": 0, "deviceid": "1000abcdef", "sequence": last["sequence"]}   # the S61's answer

    def close(self):
        pass


RECORD = {"deviceid": "1000abcdef", "apikey": "made-up", "devicekey": "made-up", "online": True,
          "params": {"switches": [{"outlet": 0, "switch": "off"}]}}


class RelayTests(unittest.TestCase):
    def check(self, device, record=RECORD, ask=relay.check_relay, sockets=None, **more):
        """Asked of `device`, or of `sockets` one connection at a time."""
        if ask is relay.check_relay:
            more.setdefault("say", lambda *a: None)
        connect = {"side_effect": sockets} if sockets else {"return_value": device}
        account = relay.Account(lambda: ("eu", {"at": "t", "user": {"apikey": "a"}}))
        with mock.patch.object(relay, "get_devices", return_value=[record]), \
                mock.patch.object(relay, "READING_SETTLE", 0), \
                mock.patch.object(relay, "api_json", return_value={"domain": "192.0.2.1", "port": 443}), \
                mock.patch.object(relay.WebSocket, "connect", **connect):
            return ask(account=account, device_id="1000abcdef", **more)

    def test_a_device_that_answers_is_there_and_nothing_on_it_changes(self):
        device = Device()
        self.assertIs(self.check(device, ask=relay.answers), True)
        self.assertEqual([m["params"] for m in device.sent if m.get("action") == "update"], [{"uiActive": 60}])
        self.assertFalse(device.on)

    def test_a_device_with_no_meter_is_asked_to_keep_its_leds_setting(self):
        device = Device(knows_readings=False)
        record = {**RECORD, "params": {**RECORD["params"], "sledOnline": "off"}}
        self.assertIs(self.check(device, record, ask=relay.answers), True)
        self.assertEqual([m["params"] for m in device.sent if m.get("action") == "update"],
                         [{"uiActive": 60}, {"sledOnline": "off"}])
        self.assertFalse(device.on)

    def test_a_dualr3_driving_a_motor_says_so(self):
        self.assertTrue(relay.motor_mode({"workMode": 2, "motorTurn": 0}))
        self.assertFalse(relay.motor_mode({"workMode": 1, "switches": [{"outlet": 0, "switch": "off"}]}))
        self.assertFalse(relay.motor_mode({"switch": "on"}))

    def test_with_no_led_setting_reported_nothing_more_is_sent(self):
        device = Device(knows_readings=False)
        self.assertIs(self.check(device, ask=relay.answers), False)
        self.assertEqual([m["params"] for m in device.sent if m.get("action") == "update"], [{"uiActive": 60}])

    def test_a_device_refusing_the_question_is_there(self):
        # The DualR3's Sonoff firmware answers uiActive with error 400: its own
        # refusal, which eWeLink passes on, and only a device there sends.
        device = Device(error=400)
        self.assertIs(self.check(device, ask=relay.answers), True)
        self.assertEqual([m["params"] for m in device.sent if m.get("action") == "update"], [{"uiActive": 60}])

    def test_a_device_online_still_from_before_does_not_answer(self):
        # eWeLink keeps a device that restarted online until its heartbeats are
        # missed: what is sent to it meanwhile goes to no one.
        self.assertIs(self.check(Device(obeys=False), ask=relay.answers), False)
        self.assertIs(self.check(Device(error=504), ask=relay.answers), False)

    def test_whether_it_answers_is_not_known_without_ewelink(self):
        account = relay.Account(mock.Mock(side_effect=relay.EwelinkError("no route")))
        self.assertIsNone(relay.answers(account=account, device_id="1000abcdef"))

    def test_a_sign_in_eWeLink_ended_is_made_again_once(self):
        # A helper tool signing in to the same account ends the run's sign-in.
        sign_in = mock.Mock(side_effect=[("eu", {"at": "old", "user": {"apikey": "a"}}),
                                         ("eu", {"at": "new", "user": {"apikey": "a"}})])
        account = relay.Account(sign_in)

        def devices(_region, token):
            if token == "old":
                raise relay.SessionEnded("eWeLink ended the sign-in")
            return [RECORD]
        with mock.patch.object(relay, "get_devices", devices), \
                mock.patch.object(relay, "api_json", return_value={"domain": "192.0.2.1", "port": 443}), \
                mock.patch.object(relay.WebSocket, "connect", return_value=Device()):
            self.assertIs(relay.answers(account=account, device_id="1000abcdef"), True)
            self.assertIs(relay.answers(account=account, device_id="1000abcdef"), True)
        self.assertEqual(sign_in.call_count, 2)

    def test_a_websocket_refusing_the_token_is_a_sign_in_ended(self):
        class Refusing(Device):
            def receive_json(self, timeout=None):
                return {"error": 401}
        with mock.patch.object(relay, "api_json", return_value={"domain": "192.0.2.1", "port": 443}), \
                mock.patch.object(relay.WebSocket, "connect", return_value=Refusing()), \
                self.assertRaises(relay.SessionEnded):
            relay.app_socket("eu", {"at": "old", "user": {"apikey": "a"}})

    def test_the_relay_is_switched_over_and_back(self):
        device = Device()
        self.assertEqual(self.check(device)[:2], (True, True))
        switched = [m["params"] for m in device.sent if "switches" in m.get("params", {})]
        self.assertEqual(switched, [{"switches": [{"switch": "on", "outlet": 0}]},
                                    {"switches": [{"switch": "off", "outlet": 0}]}])
        self.assertFalse(device.on)   # as it was

    def test_a_report_of_the_new_state_confirms_a_switch_too(self):
        self.assertEqual(self.check(Device(reports=True))[:2], (True, True))

    def test_a_query_from_the_device_passed_on_meanwhile_is_not_its_report(self):
        # Seen in the emulator: as Sonoff's firmware came back it asked the cloud
        # for its settings, a query whose params are a list of their names.
        class Asking(Device):
            def receive_json(self, timeout=None):
                if self.sent[-1].get("action") == "update" and not getattr(self, "asked", False):
                    self.asked = True
                    return {"action": "query", "deviceid": "1000abcdef", "params": ["TZ", "timers"]}
                return super().receive_json(timeout)
        self.assertEqual(self.check(Asking(reports=True))[:2], (True, True))

    def test_a_switch_the_device_never_reports_is_not_confirmed(self):
        with mock.patch.object(relay, "SWITCH_WAIT", 0.2), mock.patch.object(relay, "RESTORE_WAIT", 0.5), \
                mock.patch.object(relay.time, "sleep"):
            self.assertEqual(self.check(Device(obeys=False))[:2], (False, False))

    def test_a_device_that_drops_off_as_it_switches_is_switched_back_once_it_returns(self):
        # Seen in the emulator: the device confirmed the first switch, closed its
        # connection, and the switch back reached no one; the relay was left over.
        class Dropping(Device):
            def receive_json(self, timeout=None):
                if self.sent[-1].get("action") == "update" and len(self.sent) > 2:
                    raise relay.socket.timeout       # the switch back, sent as it went
                return super().receive_json(timeout)
        first, again = Dropping(), Device()
        again.on = True
        with mock.patch.object(relay.time, "sleep"), mock.patch.object(relay, "SWITCH_WAIT", 0.2):
            self.assertEqual(self.check(first, sockets=[first, again])[:2], (True, True))
        self.assertFalse(again.on)   # as it was

    def test_a_device_that_never_returns_is_given_up_on(self):
        with mock.patch.object(relay, "SWITCH_WAIT", 0.2), mock.patch.object(relay, "RESTORE_WAIT", 0.5), \
                mock.patch.object(relay.time, "sleep"):
            self.assertEqual(self.check(Device(obeys=False))[:2], (False, False))

    def test_a_switch_turned_down_is_not_confirmed(self):
        with mock.patch.object(relay, "RESTORE_WAIT", 0.5), mock.patch.object(relay.time, "sleep"):
            self.assertEqual(self.check(Device(error=504))[:2], (False, False))

    def test_an_offline_device_is_not_tried(self):
        device = Device()
        self.assertIsNone(self.check(device, {**RECORD, "online": False}))
        self.assertEqual(device.sent, [])

    def test_the_meter_is_read_after_each_switch_as_the_app_asks(self):
        device = Device(meter=True)
        checked = self.check(device)
        self.assertEqual(checked, relay.RelayCheck(True, True, {"Voltage": 230.08, "Power": 99.99},
                                                   {"Voltage": 0.0, "Power": 0.0}))
        asked = [m["params"] for m in device.sent if m.get("action") == "update"]
        self.assertEqual(asked, [{"switches": [{"switch": "on", "outlet": 0}]}, {"uiActive": 60},
                                 {"switches": [{"switch": "off", "outlet": 0}]}, {"uiActive": 60}])

    def test_a_device_on_already_is_read_on_after_its_switch_back(self):
        device = Device(meter=True)
        device.on = True
        record = {**RECORD, "params": {"switches": [{"outlet": 0, "switch": "on"}]}}
        checked = self.check(device, record)
        self.assertEqual((checked.on["Power"], checked.off["Power"]), (99.99, 0.0))
        self.assertTrue(device.on)   # as it was

    def test_a_device_sending_no_readings_has_none(self):
        with mock.patch.object(relay, "READING_WAIT", 0.2):
            self.assertEqual(self.check(Device()), relay.RelayCheck(True, True, None, None))

    def test_a_device_refusing_to_send_readings_is_not_waited_for(self):
        # The DualR3's Sonoff firmware refuses uiActive (error 400).
        class Refusing(Device):
            def receive_json(self, timeout=None):
                last = self.sent[-1]
                if "uiActive" in last.get("params", {}):
                    return {"error": 400, "sequence": last["sequence"]}
                return super().receive_json(timeout)
        started = relay.time.monotonic()
        self.assertEqual(self.check(Refusing()), relay.RelayCheck(True, True, None, None))
        self.assertLess(relay.time.monotonic() - started, relay.READING_WAIT)

    def test_readings_are_sent_in_hundredths(self):
        # The S61's Sonoff firmware with its relay off, seen in the emulator.
        self.assertEqual(relay.reading({"voltage": 23022, "current": 0, "power": 0, "dayKwh": 0}),
                         {"Voltage": 230.22, "Power": 0.0})
        self.assertIsNone(relay.reading({"switches": [{"outlet": 0, "switch": "on"}]}))
        self.assertIsNone(relay.reading({"voltage": {"min": 1, "max": 2}}))

    def test_a_single_switch_is_read_and_sent_as_one(self):
        self.assertIs(relay.relay_state({"switch": "on"}), True)
        self.assertEqual(relay.switch_params({"switch": "on"}, False), {"switch": "off"})
        self.assertIsNone(relay.relay_state({"fwVersion": "1.3.0"}))


if __name__ == "__main__":
    unittest.main()
