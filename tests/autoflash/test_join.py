"""The device onto the Wi-Fi and found there (tools/autoflash/join.py)."""
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.test_names import SetupNetworkTestCase
from tools.autoflash import device, errors, hostsys, join, output, prompts, routes, setup_network
from tools.autoflash.state import DONE, RunState


class NetworkCheckTests(unittest.TestCase):
    """The device's live network must be the one the bundle's firmware will join."""

    def setUp(self):
        self.patches = [
            mock.patch.object(join, "bundle_ssid", return_value="Home-2G"),
            mock.patch.object(output, "say"),
            mock.patch.object(prompts, "section"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])

    def ctx(self, ssid, signal=-50):
        from types import SimpleNamespace
        wifi = {"SSId": ssid, "Signal": signal} if ssid else {}
        device = SimpleNamespace(command=lambda _: {"StatusSTS": {"Wifi": wifi}})
        state = SimpleNamespace(input=lambda key: None, set_input=lambda key, value: None)
        return SimpleNamespace(device=device, manifest_path=Path("unused"), state=state)

    def test_a_matching_network_passes(self):
        result = join.check_device_network(self.ctx("Home-2G"))
        self.assertEqual(result, {"network": "Home-2G", "signal_dbm": -50})

    def test_a_different_network_stops_when_not_rebuilt(self):
        with mock.patch.object(prompts, "ask_yes_no", return_value=False):
            with self.assertRaisesRegex(errors.Abort, "'Guest' but the firmware to come would join 'Home-2G'"):
                join.check_device_network(self.ctx("Guest"))

    def test_a_different_network_can_be_fixed_with_its_password(self):
        ctx = self.ctx("Guest")
        ctx.adopt_wifi = mock.Mock()
        with mock.patch.object(prompts, "ask_yes_no", return_value=True), \
             mock.patch.object(join, "ask_password", return_value="guest pass") as asked, \
             mock.patch.object(join, "bundle_ssid", side_effect=["Home-2G", "Guest"]):
            result = join.check_device_network(ctx)
        asked.assert_called_once_with("Guest")  # the name is known: only the password is asked
        ctx.adopt_wifi.assert_called_once_with("Guest", "guest pass")
        self.assertEqual(result["network"], "Guest")

    def test_names_differing_only_in_case_do_not_match(self):
        with mock.patch.object(prompts, "ask_yes_no", return_value=False):
            with self.assertRaises(errors.Abort):
                join.check_device_network(self.ctx("home-2g"))

    def test_no_reported_network_stops_rather_than_guessing(self):
        with self.assertRaisesRegex(errors.Abort, "did not say which Wi-Fi network"):
            join.check_device_network(self.ctx(None))

    def test_a_weak_signal_needs_a_deliberate_yes(self):
        with mock.patch.object(prompts, "ask_yes_no", return_value=False):
            with self.assertRaisesRegex(errors.Abort, "weak Wi-Fi signal"):
                join.check_device_network(self.ctx("Home-2G", signal=-82))
        with mock.patch.object(prompts, "ask_yes_no", return_value=True):
            self.assertEqual(
                join.check_device_network(self.ctx("Home-2G", signal=-82))["signal_dbm"], -82
            )


    def test_the_network_names_are_kept_out_of_the_log(self):
        with mock.patch.object(output, "private") as private:
            join.check_device_network(self.ctx("Home-2G"))
        self.assertEqual(private.call_args_list, [mock.call("Home-2G", "wifi-name")] * 2)

    def test_weak_is_below_minus_75_dbm(self):
        with mock.patch.object(prompts, "ask_yes_no", return_value=True) as asked, \
                mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"):
            join.warn_if_weak_signal(self.ctx("Home-2G"), -75, ["then"])
            asked.assert_not_called()
            join.warn_if_weak_signal(self.ctx("Home-2G"), -76, ["then"])
            asked.assert_called_once()

    def test_an_unattended_run_stops_at_a_weak_signal(self):
        with mock.patch.object(prompts, "UNATTENDED", True), mock.patch.object(prompts, "answered_unattended"), \
                mock.patch.object(output, "transient_begin"), mock.patch.object(output, "transient_end"):
            with self.assertRaisesRegex(errors.Abort, "weak Wi-Fi signal"):
                join.warn_if_weak_signal(self.ctx("Home-2G"), -82, ["then"])


class DeviceFinderTests(unittest.TestCase):
    """The device is found at its address, or by its hardware address if it moved."""

    def finder(self, inputs):
        from types import SimpleNamespace

        moved = []
        state = SimpleNamespace(input=lambda key: inputs.get(key))
        ctx = SimpleNamespace(state=state, set_device_ip=moved.append)
        return join.DeviceFinder(ctx), moved

    def test_the_device_at_its_usual_address_is_found_without_a_sweep(self):
        from tools.autoflash import hostsys
        finder, moved = self.finder({"device_ip": "10.0.0.5", "device_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=True), \
             mock.patch.object(hostsys, "neighbour_table", return_value={"10.0.0.5": "aa:bb:cc:dd:ee:ff"}), \
             mock.patch.object(hostsys, "find_by_mac") as sweep:
            self.assertEqual(finder.poll(), "10.0.0.5")
        sweep.assert_not_called()
        self.assertEqual(moved, [])

    def test_a_different_device_at_the_old_address_is_not_mistaken_for_the_device(self):
        from tools.autoflash import hostsys
        finder, moved = self.finder({"device_ip": "10.0.0.5", "device_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=True), \
             mock.patch.object(hostsys, "neighbour_table", return_value={"10.0.0.5": "11:22:33:44:55:66"}), \
             mock.patch.object(hostsys, "find_by_mac", return_value="10.0.0.9"):
            self.assertEqual(finder.poll(), "10.0.0.9")
        self.assertEqual(moved, ["10.0.0.9"])

    def test_sweeps_are_rate_limited(self):
        from tools.autoflash import hostsys
        finder, _ = self.finder({"device_ip": "10.0.0.5", "device_mac": "aa:bb:cc:dd:ee:ff"})
        with mock.patch.object(hostsys, "ping_ok", return_value=False), \
             mock.patch.object(hostsys, "find_by_mac", return_value=None) as sweep:
            self.assertIsNone(finder.poll())
            self.assertIsNone(finder.poll())
        self.assertEqual(sweep.call_count, 1)


class BusyDeviceTests(unittest.TestCase):
    """A device still finishing a stopped step is waited for, not misread."""

    def ctx(self, *, d1_done=True, answers=(), pings=True):
        from types import SimpleNamespace
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "10.0.0.63")
        if d1_done:
            state.mark("D1", DONE)
        device = mock.Mock(answers=mock.Mock(side_effect=list(answers)))
        return SimpleNamespace(state=state, device=device, steps=routes.STEPS), pings

    def wait(self, ctx, pings):
        clock = iter(range(50_000, 150_000, 3))  # as far from 0 as a real clock is
        with mock.patch.object(hostsys, "ping_ok", return_value=pings), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep") as self.slept, \
             mock.patch.object(output, "tell") as told, \
             mock.patch.object(output, "progress"), \
             mock.patch.object(output, "progress_clear"):
            join.wait_while_device_busy(ctx)
        return told

    def test_a_device_that_answers_is_not_waited_for(self):
        ctx, pings = self.ctx(answers=[True])
        self.wait(ctx, pings).assert_not_called()

    def test_a_busy_device_is_waited_for_until_it_answers(self):
        ctx, pings = self.ctx(answers=[False, False, False, True])
        told = self.wait(ctx, pings)
        self.assertEqual(ctx.device.answers.call_count, 4)
        self.assertIn("answering again", told.call_args.args[0])
        self.assertEqual(self.slept.call_count, 2)  # a pause between each two asks

    def test_a_device_off_the_network_is_left_to_the_steps(self):
        ctx, _ = self.ctx(answers=[False])
        self.wait(ctx, False).assert_not_called()

    def test_before_tasmota_nothing_is_checked(self):
        ctx, pings = self.ctx(d1_done=False)
        self.wait(ctx, pings)
        ctx.device.answers.assert_not_called()

    def test_with_no_address_a_finished_run_or_esphome_nothing_is_checked(self):
        for left in ("no address", routes.STEPS[-1][0], "I1"):
            with self.subTest(left=left):
                ctx, pings = self.ctx(answers=[False])
                if left == "no address":
                    ctx.state.set_input("device_ip", None)
                else:
                    ctx.state.mark(left, DONE)
                self.wait(ctx, pings)
                ctx.device.answers.assert_not_called()


class SendThroughOwnNetworkTests(unittest.TestCase):
    """The Wi-Fi details sent through a device's own network: tried three times."""

    def send(self, failures):
        with mock.patch.object(prompts, "section"), mock.patch.object(hostsys, "wifi_menu_hint", return_value="x"), \
                mock.patch.object(join, "wait_until_joined"), mock.patch.object(time, "sleep"), \
                mock.patch.object(output, "tell"), mock.patch.object(output, "event") as self.event, \
                mock.patch.object(setup_network, "send_esphome_wifi", side_effect=[
                    *(setup_network.SetupError("no answer") for _ in range(failures)), None]) as sent:
            try:
                join.send_through_own_network("ESP_ABCDF0", "Home", "pw")
            finally:
                self.sent = sent.call_count

    def test_a_third_try_that_works_is_enough(self):
        self.send(2)
        self.assertEqual(self.sent, 3)
        # Asked once, as the fault suites' stand-in person sees it (output.EVENTS).
        self.assertEqual(self.event.call_args_list, [mock.call("join", network="ESP_ABCDF0")])

    def test_a_join_made_is_an_event(self):
        with mock.patch.object(setup_network, "wait_for_port", return_value=True), \
                mock.patch.object(output, "event") as event, mock.patch.object(output, "say"):
            join.wait_until_joined("ESP_ABCDF0")
        event.assert_called_once_with("joined", network="ESP_ABCDF0")

    def test_three_tries_that_fail_stop_the_run(self):
        with self.assertRaisesRegex(errors.Abort, "could not be sent"):
            self.send(3)
        self.assertEqual(self.sent, 3)


class OwnNetworkRecordedTests(SetupNetworkTestCase):
    """Joining a device's own network is recorded for the report
    (report.own_network_joined), whichever firmware raised it."""

    def test_set_up_through_esphomes_own_network(self):
        ctx = self.ctx()
        ctx.wifi = lambda why=(): ("Home", "pw")
        with mock.patch.object(prompts, "find_ssid", return_value="esphome-abcdef"), \
                mock.patch.object(prompts, "section"), mock.patch.object(output, "tell"), \
                mock.patch.object(join, "send_through_own_network"), mock.patch.object(join, "wait_for"), \
                mock.patch.object(output, "event") as event:
            self.assertEqual(join.set_up_through_own_network(ctx, "ESPHome", bool, ("esphome-abcdef",)),
                             ("Home", "pw"))
        self.assertEqual(ctx.state.input(join.OWN_NETWORK), [join.NOT_JOINED])
        event.assert_called_once_with("home")   # told to go back (output.EVENTS)

    def test_its_network_not_seen_is_not_recorded(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "find_ssid", return_value=None), \
                mock.patch.object(join, "on_a_device_network", return_value=False):
            self.assertIsNone(join.set_up_through_own_network(ctx, "ESPHome", bool, ("esphome-abcdef",)))
        self.assertIsNone(ctx.state.input(join.OWN_NETWORK))

    def test_tasmotas_setup_network_joined(self):
        ctx = self.ctx()
        ctx.wifi = lambda why=(): None
        with mock.patch.object(prompts, "section"), mock.patch.object(hostsys, "wifi_menu_hint", return_value="x"), \
                mock.patch.object(join, "wait_until_joined"), \
                mock.patch.object(join, "locate_device", return_value="192.0.2.50"):
            self.assertEqual(join.configure_device_wifi(ctx, "tasmota-ABCDEF-1234", join.SETTINGS_LOST), "192.0.2.50")
        self.assertEqual(ctx.state.input(join.OWN_NETWORK), [join.SETTINGS_LOST])


class BackByItselfTests(SetupNetworkTestCase):
    """A device that raised its own network while its router was off, back on
    the home network by itself before this computer has joined that: nothing
    to set up, and the run carries on (a router restart while a run waits)."""

    def test_the_wait_to_be_joined_ends_once_the_device_is_back(self):
        looks = []

        def wait_for_port(timeout, tick):
            for elapsed in (5.0, 16.0, 31.0):
                tick(elapsed)
            return False
        with mock.patch.object(setup_network, "wait_for_port", side_effect=wait_for_port), \
                mock.patch.object(output, "progress"), mock.patch.object(output, "progress_clear"):
            with self.assertRaises(join.BackByItself):
                join.wait_until_joined("esphome-abcdef", back=lambda: looks.append(1) or len(looks) == 2)
        self.assertEqual(len(looks), 2, "looked at every BACK_LOOK_EVERY, not every tick")

    def test_nothing_is_set_up_and_no_join_is_recorded(self):
        ctx = self.ctx()
        ctx.wifi = lambda why=(): ("Home", "pw")
        ctx.state.set_input("device_ip", "192.0.2.50")
        ctx.finder = mock.Mock(poll=mock.Mock(return_value=None))
        checked = []

        def send(network, ssid, password, back=None):
            if back():
                raise join.BackByItself
        with mock.patch.object(prompts, "find_ssid", return_value="esphome-abcdef"), \
                mock.patch.object(output, "tell") as told, \
                mock.patch.object(join, "send_through_own_network", side_effect=send), \
                mock.patch.object(join, "wait_for") as waited:
            with self.assertRaises(join.BackByItself):
                join.set_up_through_own_network(ctx, "ESPHome", lambda host: checked.append(host) or True,
                                                ("esphome-abcdef",))
        self.assertEqual(checked, ["192.0.2.50"])
        waited.assert_not_called()
        self.assertEqual(ctx.state.input(join.OWN_NETWORK), [])
        self.assertIn("back on your Wi-Fi by itself", told.call_args.args[0])


class UserActionWaitTests(unittest.TestCase):
    """Waiting on a person has no time limit - unless nobody is coming."""

    def test_a_person_is_waited_for_as_long_as_it_takes(self):
        with mock.patch.object(prompts, "UNATTENDED", False):
            self.assertEqual(join.user_action_limit(), float("inf"))
        with mock.patch.object(prompts, "UNATTENDED", True):
            self.assertEqual(join.user_action_limit(), 900.0)


class NamedWithTheWifiTests(unittest.TestCase):
    """A Tasmota whose settings were erased (switched off and on 7 times) is
    named again as it saves the Wi-Fi given on its setup network."""

    def run_it(self, current, done=()):
        from types import SimpleNamespace
        hooked = []

        class Device:
            def __init__(self, host, timeout=None):
                self.host = host

            def command(self, c, timeout=None):
                # As Tasmota answers on its setup network: Hostname would give
                # the setting (%s-%04d for its default), Status 5 the name.
                return {"StatusNET": {"Hostname": current}} if c == "Status 5" else None

            def at_next_save(self, commands, timeout=None):
                hooked.append((self.host, commands))
                return True

        inputs = {"device_mac": "12:34:56:ab:cd:ef"}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get, is_done=lambda step: step in done))
        with mock.patch.object(device, "Device", Device), mock.patch.object(output, "say"):
            join.name_with_the_wifi(ctx)
        return hooked

    def test_part_way_through_it_is_the_runs_name_for_it_again(self):
        self.assertEqual(self.run_it("tasmota-ABCDEF-1234"),
                         [("192.168.4.1", ["Hostname autoflash-recovery-CDEF"])])

    def test_once_converted_it_is_its_converted_name_again(self):
        self.assertEqual(self.run_it("tasmota-ABCDEF-1234", done=("F4",)),
                         [("192.168.4.1", ["Hostname tasmota-abcdef"])])

    def test_a_name_it_still_has_is_left_alone(self):
        for name in ("autoflash-recovery-CDEF", "tasmota-abcdef", "kitchen-plug"):
            self.assertEqual(self.run_it(name), [], name)


class SetupNetworkGoneTests(unittest.TestCase):
    """Whether the device's setup network has gone, after the Wi-Fi was sent."""

    def gone(self, answering, visible, network="tasmota-ABCDEF-1234"):
        from types import SimpleNamespace
        clock = iter(range(5000, 6000, 2))  # as far from 0 as a real clock is
        ctx = SimpleNamespace(state=SimpleNamespace(input={"device_mac": "12:34:56:ab:cd:ef"}.get))
        with mock.patch.object(setup_network, "port_open", return_value=answering), \
             mock.patch.object(prompts, "scan_wifi_ssids", return_value=visible), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"):
            return join.setup_network_gone(ctx, network)

    def test_nothing_answering_and_not_listed_is_gone(self):
        self.assertTrue(self.gone(False, ["Home"]))

    def test_it_looks_again_only_after_a_pause(self):
        from types import SimpleNamespace
        clock = iter(range(5000, 6000, 2))  # as far from 0 as a real clock is
        ctx = SimpleNamespace(state=SimpleNamespace(input={"device_mac": "12:34:56:ab:cd:ef"}.get))
        with mock.patch.object(setup_network, "port_open", return_value=False) as asked, \
             mock.patch.object(prompts, "scan_wifi_ssids", return_value=["Home"]), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep") as slept:
            join.setup_network_gone(ctx, "tasmota-ABCDEF-1234")
        self.assertEqual(slept.call_count, asked.call_count)

    def test_still_answering_is_not(self):
        self.assertFalse(self.gone(True, ["Home"]))

    def test_still_listed_is_not(self):
        self.assertFalse(self.gone(False, ["Home", "tasmota-ABCDEF-1234"]))

    def test_a_scan_that_sees_nothing_cannot_say(self):
        self.assertFalse(self.gone(False, []))

    def test_given_as_a_hint_any_of_its_names_counts(self):
        hint = "tasmota-abcdef, tasmota-ABCDEF-NNNN, or ESP_ABCDF0"
        self.assertFalse(self.gone(False, ["ESP_ABCDF0"], network=hint))
        self.assertTrue(self.gone(False, ["Home"], network=hint))


class LocateTests(SetupNetworkTestCase):
    def test_a_device_not_found_can_be_set_up_on_its_own_network_instead(self):
        ctx = self.ctx()
        ctx.state.set_input("device_ip", "192.0.2.50")
        ctx.finder = SimpleNamespace(poll=lambda: None)
        ctx.set_device_ip = lambda ip: ctx.state.set_input("device_ip", ip)
        offered = []

        def choose(question, options, help_lines=()):
            offered.extend(options)
            return 2

        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(time, "sleep"), \
                mock.patch.object(time, "monotonic", side_effect=iter(range(0, 10_000, 100))), \
                mock.patch.object(prompts, "wait_for_host", return_value=False), \
                mock.patch.object(prompts, "ask_choice", side_effect=choose), \
                mock.patch.object(join, "configure_device_wifi", return_value="192.0.2.77") as setup:
            self.assertEqual(join.locate_device(ctx, setup=True), "192.0.2.77")
        setup.assert_called_once_with(ctx, None, join.NOT_JOINED)
        self.assertIn("tasmota-ABCDEF-NNNN, or ESP_ABCDF0", offered[2])

    def test_an_address_given_when_it_is_not_found_is_kept_and_no_setup_network_offered(self):
        moved = []
        ctx = SimpleNamespace(state=SimpleNamespace(input={"device_ip": "192.0.2.50"}.get),
                              finder=SimpleNamespace(poll=lambda: None), set_device_ip=moved.append)
        with mock.patch.object(time, "sleep") as slept, \
                mock.patch.object(time, "monotonic", side_effect=iter(range(0, 10_000, 100))), \
                mock.patch.object(output, "progress"), mock.patch.object(output, "progress_clear"), \
                mock.patch.object(prompts, "wait_or_ask", return_value="192.0.2.9") as asked:
            self.assertEqual(join.locate_device(ctx), "192.0.2.9")
        self.assertIsNone(asked.call_args.kwargs["other"])
        self.assertEqual(moved, ["192.0.2.9"])
        self.assertTrue(slept.called)  # a pause between looks

    def test_without_it_only_waiting_or_an_address_is_offered(self):
        offered = []
        with contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(prompts, "wait_for_host", return_value=False), \
                mock.patch.object(prompts, "ask_choice",
                                  side_effect=lambda q, options, help_lines=(): offered.extend(options) or 1), \
                mock.patch.object(prompts, "ask_ipv4", return_value="192.0.2.9"):
            self.assertEqual(prompts.wait_or_ask("192.0.2.50"), "192.0.2.9")
        self.assertEqual(len(offered), 2)



class OwnNetworkWatchTests(unittest.TestCase):
    """A device that cannot join raises its own network after a minute or two:
    the wait for it on the home network ends then, not when its time is out."""

    def waited(self, scans, own_network=("esphome-abcdef",), timeout=180.0):
        clock = [0.0]
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: "192.0.2.50"),
                              finder=SimpleNamespace(poll=lambda: None))
        with mock.patch.object(join.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(join.time, "sleep", side_effect=lambda s: clock.__setitem__(0, clock[0] + s)), \
                mock.patch.object(join.prompts, "find_ssid", side_effect=scans) as scanned, \
                mock.patch.object(join.output, "progress"), mock.patch.object(join.output, "progress_clear"):
            try:
                join.wait_for(ctx, lambda host: None, "ESPHome on your Wi-Fi", timeout=timeout,
                              own_network=own_network)
            except errors.Abort as exc:
                return exc, clock[0], scanned.call_count
        raise AssertionError("the wait ended with the device answering")

    def test_the_wait_ends_once_the_devices_own_network_is_up(self):
        stopped, at, scans = self.waited([None, None, "esphome-abcdef"])
        self.assertIsInstance(stopped, join.OwnNetworkUp)
        self.assertEqual(stopped.network, "esphome-abcdef")
        self.assertEqual(scans, 3, "every OWN_NETWORK_LOOK_EVERY seconds, not each look at the device")
        self.assertLess(at, 3 * join.OWN_NETWORK_LOOK_EVERY + 10)

    def test_with_none_up_the_wait_runs_its_time_out(self):
        stopped, at, _ = self.waited(lambda *a, **k: None)
        self.assertNotIsInstance(stopped, join.OwnNetworkUp)
        self.assertGreaterEqual(at, 180.0)

    def test_without_its_names_nothing_is_looked_for(self):
        stopped, _, scans = self.waited(AssertionError("scanned"), own_network=())
        self.assertNotIsInstance(stopped, join.OwnNetworkUp)
        self.assertEqual(scans, 0)


if __name__ == "__main__":
    unittest.main()
