"""arrival: a device looked for until it is back, and back on Sonoff's firmware."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.routes.beken.test_beken import a_context
from tools.autoflash import detect, errors, kinds, records
from tools.autoflash.routes import arrival

MAC = "12:34:56:ab:cd:ef"


class UntilBackTests(unittest.TestCase):
    def test_how_long_a_device_took_to_answer_once_back_on_is_kept_the_longest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            looks = iter([arrival.NotBack(), "found", arrival.NotBack(), "found"])

            def look(_restarted):
                found = next(looks)
                if isinstance(found, Exception):
                    raise found
                return found
            # Said back on at 100 s, found at 141; then at 200, found at 210.
            clock = iter([100.0, 141.0, 200.0, 210.0])
            with mock.patch.object(arrival.prompts, "section"), mock.patch.object(arrival.prompts, "ask_choice", return_value=0), \
                    mock.patch.object(arrival.output, "transient_begin"), mock.patch.object(arrival.output, "transient_end"), \
                    mock.patch.object(arrival.time, "monotonic", side_effect=lambda: next(clock)):
                self.assertEqual(arrival.until_back(ctx, look, "OpenBeken", "advice"), "found")
                self.assertEqual(arrival.until_back(ctx, look, "OpenBeken", "advice"), "found")
            self.assertEqual(ctx.state.input("back_after_power_cycle"), 41)
            self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 2})

    def test_a_not_back_that_says_why_is_asked_and_stopped_with_its_own_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")

            def look(_restarted):
                raise arrival.NotBack("its own reason", ("its own line",))
            with mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    self.assertRaisesRegex(errors.Abort, "^its own reason$"):
                arrival.until_back(ctx, look, "OpenBeken", "advice")
            with mock.patch.object(arrival.prompts, "section") as shown, \
                    mock.patch.object(arrival.prompts, "ask_choice", side_effect=arrival.prompts.QuitRun(130)), \
                    mock.patch.object(arrival.output, "transient_begin"), self.assertRaises(arrival.prompts.QuitRun):
                arrival.until_back(ctx, look, "OpenBeken", "advice")
            self.assertEqual(shown.call_args.args[1], ["its own line"])


class PowerCycleTests(unittest.TestCase):
    def test_asked_and_counted_by_whom_the_emulators_switch_used_where_there_is_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = a_context(tmp, firmware="openbeken")
            switch = Path(tmp) / "power"
            with mock.patch.object(arrival.prompts, "section"), \
                    mock.patch.object(arrival.prompts, "ask_choice", side_effect=[0, 1]) as asked, \
                    mock.patch.object(arrival.output, "transient_begin"), mock.patch.object(arrival.output, "transient_end"), \
                    mock.patch.object(arrival.output, "say"), mock.patch.object(arrival.time, "sleep"), \
                    mock.patch.dict(arrival.os.environ, {arrival.EMULATED_POWER: str(switch)}):
                arrival.power_cycle(ctx, "Title", ("a line",), "send it again")
                arrival.power_cycle(ctx, "Title", ("a line",), "send it again")
            self.assertEqual(asked.call_args.args[1], ["Send it again - the device is back on",
                                                       "Power-cycle the emulated device, then send it again"])
            self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 1, "by the tool": 1})
            self.assertEqual(switch.read_text(encoding="utf-8"), "on\n")


class OnlineInEwelinkTests(unittest.TestCase):
    def test_waits_for_the_device_online_and_gives_up_at_once_without_ewelink(self):
        answers = iter([[{"deviceid": "1001234abc", "online": False}], [{"deviceid": "1001234abc", "online": True}]])
        ctx = SimpleNamespace(account_devices=lambda: next(answers))
        with mock.patch.object(arrival.time, "sleep"), mock.patch.object(arrival.output, "progress"), \
                mock.patch.object(arrival.output, "progress_clear"):
            self.assertTrue(arrival.online_in_ewelink(ctx, "1001234abc"))
            self.assertFalse(arrival.online_in_ewelink(SimpleNamespace(account_devices=lambda: None), "1001234abc"))


class BackOnSonoffTests(unittest.TestCase):
    """R2 and K3 alike: on the Wi-Fi, and in eWeLink."""

    def setUp(self):
        for name in ("say", "tell", "progress", "progress_clear"):
            patcher = mock.patch.object(arrival.output, name)
            patcher.start()
            self.addCleanup(patcher.stop)

    def back(self, ctx, tmp, *, running=None, wait=50, devices=None, answers=True, **kwargs):
        ctx.account_devices = (lambda: devices) if not callable(devices) else devices
        ctx.ewelink = mock.sentinel.account
        with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(detect, "identify", return_value=running), \
                mock.patch.object(arrival.ewelink_relay, "answers", return_value=answers), \
                mock.patch.object(arrival.time, "sleep"), \
                mock.patch.object(arrival.time, "monotonic", side_effect=iter(range(50_000, 60_000, 5))):
            if kwargs.get("ewelink_id", True):
                records.remember_device(MAC, ewelink_id="1001234abc")
            return arrival.back_on_sonoff(ctx, wait=wait, leaving=kwargs.get("leaving", ()))

    def espressif(self, tmp):
        ctx = a_context(tmp, device_ip="192.0.2.50")
        ctx.state.set_input("device_kind", kinds.SONOFF_S60.key)
        return ctx

    def test_on_the_wifi_but_never_in_ewelink_the_person_hears_of_the_internet(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.espressif(tmp)
            offline = [{"deviceid": "1001234abc", "online": False}]
            with mock.patch.object(arrival.prompts, "UNATTENDED", True), \
                    self.assertRaisesRegex(errors.Abort, "on your Wi-Fi but not in eWeLink.*internet"):
                self.back(ctx, tmp, running="sonoff", devices=offline)
            with mock.patch.object(arrival.prompts, "section") as shown, \
                    mock.patch.object(arrival.prompts, "ask_choice", side_effect=arrival.prompts.QuitRun(130)), \
                    mock.patch.object(arrival.output, "transient_begin"), self.assertRaises(arrival.prompts.QuitRun):
                self.back(ctx, tmp, running="sonoff", devices=offline)
            self.assertEqual(shown.call_args.args[1], list(arrival.ON_WIFI_ONLY_LINES))

    def test_back_in_ewelink_it_says_when_it_was_on_the_wifi_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.espressif(tmp)
            result = self.back(ctx, tmp, running="sonoff", devices=[{"deviceid": "1001234abc", "online": True}])
            self.assertEqual(result["confirmed_by"], "eWeLink")
            self.assertIn("on_wifi_after", result)

    def test_with_ewelink_out_of_reach_the_wifi_says(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctx = self.espressif(tmp)
            self.assertEqual(self.back(ctx, tmp, running="sonoff", devices=None)["confirmed_by"], "your Wi-Fi")

    def test_every_family_answering_nothing_known_is_on_the_wifi_by_its_hardware_address(self):
        # Sonoff's firmware opens eWeLink's LAN port only with LAN Control on.
        for kind in (kinds.SONOFF_S60, kinds.SONOFF_S61):
            with self.subTest(kind.key), tempfile.TemporaryDirectory() as tmp:
                ctx = a_context(tmp, device_ip="192.0.2.50")
                ctx.state.set_input("device_kind", kind.key)
                ctx.finder = SimpleNamespace(poll=lambda: "192.0.2.50")
                self.assertEqual(self.back(ctx, tmp, devices=None)["confirmed_by"], "your Wi-Fi")
                self.assertTrue(arrival.on_wifi(ctx, None))
                # Or by eWeLink's LAN port, with LAN Control on.
                self.assertTrue(arrival.on_wifi(ctx, "sonoff"))
                # Still the firmware it was to leave: not Sonoff's on the Wi-Fi.
                self.assertFalse(arrival.on_wifi(ctx, "openbeken"))
                ctx.finder = SimpleNamespace(poll=lambda: None)
                self.assertFalse(arrival.on_wifi(ctx, None), "not on the Wi-Fi at all")


class SonoffAnswersTests(unittest.TestCase):
    """A wait that must not take another firmware starting for Sonoff's: its
    LAN port, or the device at its hardware address answering through eWeLink."""

    def answers(self, tmp, *, running=None, seen=MAC, online=True, relayed=True):
        ctx = a_context(tmp, device_ip="192.0.2.50")
        ctx.state.set_input("device_kind", kinds.SONOFF_S60.key)
        ctx.account_devices = lambda: [{"deviceid": "1001234abc", "online": online}]
        ctx.ewelink = mock.sentinel.account
        with mock.patch.object(records, "DEVICES", Path(tmp) / "devices"), \
                mock.patch.object(detect, "identify", return_value=running), \
                mock.patch.object(arrival.hostsys, "mac_of", return_value=seen), \
                mock.patch.object(arrival.ewelink_relay, "answers", return_value=relayed) as self.relay:
            records.remember_device(MAC, ewelink_id="1001234abc")
            return arrival.sonoff_answers(ctx, "192.0.2.50")

    def test_its_lan_port_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.answers(tmp, running="sonoff", relayed=False))
            self.relay.assert_not_called()

    def test_with_no_lan_control_the_device_answering_through_ewelink_says_so(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertTrue(self.answers(tmp))
            self.relay.assert_called_once_with(account=mock.sentinel.account, device_id="1001234abc")

    def test_nothing_less_will_do(self):
        for case in ({"running": "installer"}, {"seen": None}, {"seen": "12:34:56:ab:cd:00"},
                     {"online": False}, {"relayed": False}, {"relayed": None}):
            with self.subTest(case), tempfile.TemporaryDirectory() as tmp:
                self.assertFalse(self.answers(tmp, **case))


if __name__ == "__main__":
    unittest.main()
