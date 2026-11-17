"""From Sonoff's firmware to Tasmota through the installer
(tools/autoflash/routes/espressif/installer.py): the bridge, the stock
backup, and Tasmota given its Wi-Fi."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.autoflash.test_records import efuse_with_mac
from tools.autoflash import detect, device, errors, firewall, hostsys, join, names, output, records, prompts
from tools.autoflash import safety_locks, setup_network, state
from tools.autoflash.routes.espressif import installer
from tools.autoflash.runs import ROOT
from tools.autoflash.state import DONE, RunState
from tools.espressif import dump_stock_flash


class BridgeOnHomeNetworkTests(unittest.TestCase):
    """Bridge v4 joins the home network; D1 uploads there instead of asking."""

    def find(self, addresses, pages, timeout=10.0):
        from types import SimpleNamespace
        from unittest import mock

        polls = iter(addresses)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: next(polls, None)))
        clock = iter(range(0, 10_000, 2))
        with mock.patch.object(setup_network, "page", side_effect=lambda host, **_: pages.get(host)), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"):
            return installer.bridge_on_home_network(ctx, timeout=timeout)

    def test_the_bridge_is_found_where_the_device_was(self):
        bridge = "<title>Autoflash bridge</title>"
        self.assertEqual(self.find([None, "192.0.2.51"], {"192.0.2.51": bridge}), "192.0.2.51")

    def test_stock_firmware_at_that_address_is_not_the_bridge(self):
        # Before the bridge starts, the device answers there with no web page.
        self.assertIsNone(self.find(["192.0.2.51"] * 20, {}))

    def test_no_device_on_the_network_falls_back_to_joining(self):
        self.assertIsNone(self.find([None] * 20, {}))

    def test_tasmota_there_is_not_the_bridge_unless_asked_for(self):
        tasmota = "<title>Tasmota</title> Tasmota 15.6.0 by Theo Arends"
        with mock.patch.object(installer, "tasmota_at_home", return_value="192.0.2.51"):
            self.assertIsNone(self.find(["192.0.2.51"] * 20, {"192.0.2.51": tasmota}))


class BackOnSonoffTests(unittest.TestCase):
    """A power cut before the installer has done its work puts the device back on
    Sonoff's firmware, by design: the installer is sent again, not waited for."""

    def reach(self, running, sends=(None,), found=(None, "192.0.2.51"), waits=(None,)):
        from types import SimpleNamespace
        from unittest import mock

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("device_ip", "192.0.2.51")
        state.set_input("stock_version", "1.2.0")  # as eWeLink gave it at A2
        ctx = self.ctx = SimpleNamespace(state=state, finder=SimpleNamespace(poll=lambda: "192.0.2.51"))
        found = iter(found)
        outcomes = iter(sends)

        def send(_):
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return {"seconds": 20}

        with mock.patch.object(setup_network, "page", return_value=None), \
             mock.patch.object(installer, "bridge_on_home_network", side_effect=lambda *a, **k: next(found, None)), \
             mock.patch.object(detect, "identify",
                               **({"side_effect": running} if isinstance(running, list) else {"return_value": running})), \
             mock.patch.object(installer, "step_bridge", side_effect=send) as bridge, \
             mock.patch.object(installer, "wait_for_bridge", side_effect=list(waits)) as wait, \
             mock.patch.object(prompts, "find_ssid", return_value=None), \
             mock.patch.object(hostsys, "copy_to_clipboard", return_value=False), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "tell"), \
             mock.patch.object(output, "say"):
            try:
                address = installer.reach_bridge(ctx)
            except errors.Abort as exc:
                address = exc
        return address, bridge.call_count, wait.call_count, state

    def test_the_installer_is_sent_again_and_found(self):
        address, sent, waited, state = self.reach("sonoff")
        self.assertEqual((address, sent, waited), ("192.0.2.51", 1, 0))
        self.assertEqual(state.status("C2"), "done")
        self.assertEqual(self.ctx.bridge_sends, 1)   # counted for the run, against BRIDGE_SENDS

    def test_sent_and_not_back_on_sonoff_it_is_waited_for_not_sent_again(self):
        address, sent, waited, _ = self.reach(["sonoff"] + [None] * 10, sends=(None,) * 5, found=(None,) * 10)
        self.assertEqual((sent, waited), (1, 1))

    def test_a_send_cut_off_is_tried_again_three_times_in_all(self):
        failed = errors.Abort("the installer was not fully delivered")
        # Two cut off, the third gets there.
        address, sent, _, _ = self.reach("sonoff", sends=(failed, failed, None),
                                         found=(None, "192.0.2.51"))
        self.assertEqual((address, sent), ("192.0.2.51", 3))
        # Three cut off: it stops, with the reason, rather than go on for ever.
        address, sent, _, _ = self.reach("sonoff", sends=(failed,) * 5)
        self.assertIs(address, failed)
        self.assertEqual(sent, installer.BRIDGE_SENDS)

    def advice(self, said, reported=(), started=False, downloaded_from=None, listen_ip="192.0.2.10"):
        from types import SimpleNamespace
        from unittest import mock
        state = SimpleNamespace(input=lambda key: downloaded_from if key == installer.DOWNLOADED_FROM else None)
        ctx = SimpleNamespace(state=state, listen_ip=lambda: listen_ip)
        with mock.patch.object(output, "tool_said", side_effect=lambda text: any(text in s for s in said)), \
             mock.patch.object(firewall, "firewall_hint", return_value=["FIREWALL"]):
            return " ".join(installer.bridge_failure_advice(ctx, list(reported), started))

    def test_a_send_that_never_reached_ewelink_does_not_blame_the_firewall(self):
        # Seen against the real cloud: eWeLink's servers dropping connections
        # (SSL EOF), and the run said the firewall had kept the device out - of
        # a download it was never asked to make.
        import tools.espressif.serve_tasmota_ota as sender
        error = "ERROR: [SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol"
        advice = self.advice([error, sender.NOT_SENT], reported=[error, sender.NOT_SENT])
        self.assertNotIn("FIREWALL", advice)
        self.assertIn("connection to eWeLink failed", advice)
        self.assertIn("run this again", advice)

    def test_a_send_refused_before_the_command_says_only_that_nothing_was_sent(self):
        import tools.espressif.serve_tasmota_ota as sender
        refused = "ERROR: Device reports 1.3.0, expected 1.2.0; refusing"
        advice = self.advice([refused, sender.NOT_SENT], reported=[refused, sender.NOT_SENT])
        self.assertEqual(advice, "Nothing was sent to the device, so it keeps its Sonoff firmware.")

    def test_a_command_sent_and_never_fetched_is_the_firewall(self):
        self.assertEqual(self.advice(["Upgrade command sent with owner-controlled private-LAN URLs."]), "FIREWALL")

    def test_a_device_that_downloaded_from_here_earlier_does_not_blame_the_firewall(self):
        # Seen in the fault suite: half the file served, then three sends the
        # device never asked for, each put down to the firewall.
        sent = ["Upgrade command sent with owner-controlled private-LAN URLs."]
        advice = self.advice(sent, downloaded_from="192.0.2.10")
        self.assertNotIn("FIREWALL", advice)
        self.assertIn("a firewall is not the cause", advice)
        self.assertIn("switch the device off and on", advice)

    def test_a_computer_on_a_new_address_since_the_download_may_still_be_the_firewall(self):
        sent = ["Upgrade command sent with owner-controlled private-LAN URLs."]
        self.assertEqual(self.advice(sent, downloaded_from="192.0.2.10", listen_ip="192.0.2.11"), "FIREWALL")

    def test_a_device_busy_with_an_update_cut_off_is_to_be_switched_off_and_on(self):
        # The Beken S61 answers so (error 410); the S60 leaves eWeLink instead.
        advice = self.advice(["Cloud: upgrade command rejected (error 410)"])
        self.assertIn("still busy with an update", advice)
        self.assertIn("switch the device off and on", advice)

    def test_what_a_switch_off_and_on_would_fix(self):
        from types import SimpleNamespace
        from unittest import mock
        for said, downloaded_from, expected in ((["rejected (error 410)"], None, True),
                                                (["Device is offline in eWeLink; refusing"], None, True),
                                                (["Upgrade command sent"], "192.0.2.10", True),
                                                (["Upgrade command sent"], None, False)):
            state = SimpleNamespace(input=lambda key, d=downloaded_from: d if key == installer.DOWNLOADED_FROM else None)
            ctx = SimpleNamespace(state=state, listen_ip=lambda: "192.0.2.10")
            with mock.patch.object(output, "tool_said", side_effect=lambda text, s=said: any(text in x for x in s)):
                self.assertIs(installer.needs_power_cycle(ctx), expected, said)

    def test_offline_and_cut_off_sends_keep_their_own_advice(self):
        self.assertIn("eWeLink does not see the device", self.advice(["Device is offline in eWeLink; refusing"]))
        self.assertIn("stopped part way", self.advice(["Served /user1.bin"], started=True))

    def test_a_failed_send_is_followed_by_the_next_though_the_device_is_quiet(self):
        # Seen in the fault suite: a send refused (the device offline in eWeLink),
        # then the device not answering here for a moment - and the run asked the
        # person to join an installer network that was never raised.
        failed = errors.Abort("eWeLink does not see the device")
        address, sent, waited, _ = self.reach(["sonoff"] + [None] * 10, sends=(failed, None),
                                              found=(None, "192.0.2.51"))
        self.assertEqual((address, sent, waited), ("192.0.2.51", 2, 0))

    def test_back_on_sonoff_after_every_send_stops_asking_after_three(self):
        # Sent, but the device went back each time (its power keeps going): after
        # three it falls through to the usual wait, which says what is wrong.
        address, sent, waited, _ = self.reach("sonoff", sends=(None,) * 5, found=(None,) * 10)
        self.assertEqual((sent, waited), (3, 1))

    def test_back_on_sonoff_while_waiting_sends_it_again(self):
        # A power cut during the backup, once the run was waiting for the
        # installer's own network: the installer is gone, so it is sent again.
        address, sent, waited, state = self.reach([None, "sonoff"], waits=(installer.BACK_ON_SONOFF,),
                                                  found=(None, "192.0.2.51"))
        self.assertEqual((address, sent, waited), ("192.0.2.51", 1, 1))
        self.assertEqual(state.status("C2"), "done")

    def test_a_run_started_on_the_installer_says_to_start_again(self):
        # It has none of eWeLink's details, so cannot send the installer again.
        from unittest import mock
        with mock.patch.object(state.RunState, "input",
                               side_effect=lambda self, key: {"device_ip": "192.0.2.51"}.get(key),
                               autospec=True):
            address, sent, _, _ = self.reach("sonoff")
        self.assertIsInstance(address, errors.Abort)
        self.assertIn("--new", str(address))
        self.assertEqual(sent, 0)

    def test_anything_else_is_waited_for_as_before(self):
        address, sent, waited, _ = self.reach(None)
        self.assertEqual((sent, waited), (0, 1))


class BridgeScreenTests(unittest.TestCase):
    """What reaching the bridge shows goes once it is reached: its network
    joined, the instructions to join it are done with (output.until_done)."""

    def screen(self, waits, resend=None):
        import io
        import os
        from types import SimpleNamespace
        from tools.repo import unattended_run

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ctx = SimpleNamespace(state=RunState.open(Path(directory.name)))
        terminal = Terminal()
        with mock.patch("sys.stdout", terminal), mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))), \
             mock.patch.object(installer, "bridge_answers", return_value=False), \
             mock.patch.object(installer, "bridge_on_home_network", return_value=None), \
             mock.patch.object(installer, "resend_if_back_on_sonoff", side_effect=resend or (lambda ctx: None)), \
             mock.patch.object(prompts, "find_ssid", return_value="autoflash-recovery-ABCD"), \
             mock.patch.object(installer, "wait_for_bridge",
                               side_effect=waits if callable(waits) else list(waits)):
            output.start(Path(directory.name) / "run.log", verbose=False)
            output.stage_begin(7, "Backing up Sonoff firmware")
            try:
                installer.reach_bridge(ctx)
                output.stage_end("stopped")   # stopped all the same: what is left shows
            except errors.Abort:
                output.stage_end("stopped")
            finally:
                output.finish()
        screen = unattended_run.Screen(100, 40)
        screen.feed(terminal.getvalue().replace("\n", "\r\n"))
        return screen.render(colour=False), (Path(directory.name) / "run.log").read_text(encoding="utf-8")

    def advice_screen(self, found):
        import io
        import os
        from types import SimpleNamespace
        from tools.repo import unattended_run

        class Terminal(io.StringIO):
            def isatty(self):
                return True
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        ctx = SimpleNamespace(state=RunState.open(Path(directory.name)))
        terminal = Terminal()
        with mock.patch("sys.stdout", terminal), mock.patch.object(output, "colour_ok", return_value=True), \
             mock.patch("shutil.get_terminal_size", return_value=os.terminal_size((100, 40))), \
             mock.patch.object(installer, "_wait_for_tasmota", return_value=found), \
             mock.patch.object(installer.join, "configure_device_wifi",
                               side_effect=lambda ctx, network, why: output.tell("-- Join the device's setup network")):
            output.start(Path(directory.name) / "run.log", verbose=False)
            output.stage_begin(8, "Installing interim firmware")
            installer.finish_tasmota_setup(ctx, route="home network", wait=900.0,
                                           advice=("Tasmota has not appeared on your network", ["Switch it off and on."]))
            output.stage_end("stopped")   # stopped all the same: what is left shows
            output.finish()
        screen = unattended_run.Screen(100, 40)
        screen.feed(terminal.getvalue().replace("\n", "\r\n"))
        return screen.render(colour=False)

    def test_the_advice_goes_once_tasmotas_network_appears(self):
        # It has done its job: the person switched it off and on, as advised.
        screen = self.advice_screen(("network", "tasmota-ABCDEF-1234"))
        self.assertNotIn("Tasmota has not appeared", screen)
        self.assertIn("-- Join the device's setup network", screen)   # what is to do now

    def test_it_stays_while_neither_tasmota_nor_its_network_appears(self):
        self.assertIn("Tasmota has not appeared", self.advice_screen(None))

    def test_the_instructions_and_the_waits_go_once_the_bridge_is_reached(self):
        screen, log = self.screen([installer.BACK_ON_SONOFF, installer.BACK_ON_SONOFF, setup_network.SETUP_HOST])
        self.assertNotIn("Join the installer's Wi-Fi network", screen)
        self.assertNotIn("While waiting", screen)
        self.assertIn("Join the installer's Wi-Fi network", log)        # the log keeps them
        self.assertEqual(log.count("went back to its Sonoff firmware."), 1)   # said once, then
        self.assertIn("went back to Sonoff's own firmware again", log)  # only noted

    def test_back_on_sonoff_the_instructions_go_until_the_bridge_is_back(self):
        # The bridge's network gone with it, joining it is no longer to do;
        # given again once the installer has made it again. Kept, they stood
        # above the installer sent again (the fault suite's C3fb:outage-5).
        import sys
        from tools.repo import unattended_run

        def joins_shown():
            screen = unattended_run.Screen(100, 40)
            screen.feed(sys.stdout.getvalue().replace("\n", "\r\n"))
            return screen.render(colour=False).count("Join the installer's Wi-Fi network")
        sending, waiting = [], []

        def resend(ctx):
            sending.append(joins_shown())   # as the installer is sent again
            return None

        def wait(ctx, network, **kwargs):
            waiting.append(joins_shown())
            return [installer.BACK_ON_SONOFF, setup_network.SETUP_HOST][len(waiting) - 1]
        self.screen(wait, resend=resend)
        self.assertEqual(sending, [0, 0])   # not while it is sent again
        self.assertEqual(waiting, [1, 1])   # asked again once back, never twice over

    def test_they_stay_when_the_wait_stops_the_stage(self):
        screen, _ = self.screen([errors.Abort("the bridge never appeared")])
        self.assertIn("Join the installer's Wi-Fi network", screen)


class WaitForBridgeTests(unittest.TestCase):
    """While waiting for the bridge's own network, a late join of the home one
    must still be noticed: the bridge then never raises its own."""

    def wait(self, addresses, pages, timeout=60.0, gone_back=None, answering=True, told=None):
        from types import SimpleNamespace
        from unittest import mock

        polls = iter(addresses)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: next(polls, None)),
                              state=SimpleNamespace(input=lambda key: "192.0.2.51"))
        clock = iter(range(5_000, 105_000, 2))   # a computer up a while
        with mock.patch.object(setup_network, "page", side_effect=lambda host, **_: pages.get(host)), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(hostsys, "ping_ok", return_value=answering), \
             mock.patch.object(output, "tell", side_effect=lambda line: (told if told is not None else []).append(line)), \
             mock.patch.object(output, "say"):
            return installer.wait_for_bridge(ctx, "autoflash-recovery-CDEF", timeout=timeout,
                                                 gone_back=gone_back)

    def test_a_late_join_of_the_home_network_is_taken(self):
        bridge = "<title>Autoflash bridge</title>"
        self.assertEqual(
            self.wait([None, None, "192.0.2.51"], {"192.0.2.51": bridge}), "192.0.2.51"
        )

    def test_joining_the_bridge_network_still_works(self):
        bridge = "<title>Autoflash bridge</title>"
        self.assertIsNone(self.wait([None] * 50, {setup_network.SETUP_HOST: bridge}))

    def test_a_device_gone_back_to_sonoffs_firmware_ends_the_wait(self):
        self.assertEqual(self.wait([None] * 100, {}, timeout=600.0, gone_back=lambda: True),
                         installer.BACK_ON_SONOFF)

    def test_nothing_at_the_devices_address_is_told_to_switch_it_off_and_on(self):
        # Sonoff's firmware, its Wi-Fi gone while it started, stays off it.
        told = []
        with self.assertRaises(errors.Abort):
            self.wait([None] * 500, {}, timeout=400.0, answering=False, told=told)
        self.assertIn("switch it off and on", " ".join(told))
        self.assertEqual(sum("No answer from the device" in line for line in told), 1)   # once, not each look
        told = []
        with self.assertRaises(errors.Abort):
            self.wait([None] * 500, {}, timeout=400.0, answering=True, told=told)
        self.assertNotIn("switch it off and on", " ".join(told))  # something is there

    def test_neither_in_time_stops_with_a_reason(self):
        with self.assertRaisesRegex(errors.Abort, "did not appear on your network"):
            self.wait([None] * 50, {}, timeout=20.0)


class TasmotaAtHomeTests(unittest.TestCase):
    """Tasmota found on the home network, and the name it goes by kept - one
    it keeps, not the run's for it part way through."""

    def found(self, hostname):
        from types import SimpleNamespace
        inputs = {}
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"),
                              state=SimpleNamespace(set_input=inputs.__setitem__, input=inputs.get),
                              set_device_ip=lambda ip: inputs.__setitem__("device_ip", ip))
        client = mock.Mock()
        client.command.return_value = {"StatusNET": {"Hostname": hostname}}
        with mock.patch.object(setup_network, "page", return_value="<title>Tasmota</title>"), \
             mock.patch.object(device, "Device", return_value=client), \
             mock.patch.object(output, "say"):
            self.assertEqual(installer.tasmota_at_home(ctx), "192.0.2.51")
        return inputs.get("device_name")

    def test_tasmotas_own_name_is_kept(self):
        self.assertEqual(self.found("tasmota-ABCDEF-1234"), "tasmota-ABCDEF-1234")

    def test_the_runs_name_for_it_part_way_is_not(self):
        self.assertIsNone(self.found("autoflash-recovery-CDEF"))


class TasmotaWifiPresetTests(unittest.TestCase):
    """D1 gives Tasmota the Wi-Fi through the bridge when it safely can."""

    def setUp(self):
        from types import SimpleNamespace
        from tools.autoflash import tasmota_settings as ts
        self.ts = ts
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.inputs = {}
        self.ctx = SimpleNamespace(state=SimpleNamespace(set_input=self.inputs.__setitem__,
                                                         input=self.inputs.get))

    # The bridge's answers (archive/bridge/main/main.c), joined to the home
    # network and on its own.
    JOINED = "Tasmota settings saved with the home network.\n"
    ALONE = "Tasmota settings saved without Wi-Fi details: the bridge has not joined a network.\n"

    def run_preset(self, template, send_error=None, address="192.0.2.51", answer=JOINED):
        from unittest import mock
        sent = []

        def send(data, **kwargs):
            if send_error:
                raise setup_network.SetupError(send_error)
            sent.append(data)
            return answer

        with mock.patch.object(self.ts, "load_template",
                               return_value=(template, "" if template else "none captured")), \
             mock.patch.object(setup_network, "send_tasmota_settings", side_effect=send), \
             mock.patch.object(output, "say") as said:
            given = installer.give_tasmota_the_wifi(self.ctx, address)
        self.said = " ".join(call.args[0] for call in said.call_args_list)
        return given, sent

    def test_with_a_template_the_bridge_gets_it(self):
        self.assertEqual(self.run_preset(b"T" * 4096), (True, [b"T" * 4096]))

    def test_given_the_wifi_tasmota_is_no_longer_named_alone(self):
        # An earlier try saved it without the Wi-Fi; this one with it: Tasmota
        # joins by itself, and raises no network of the installer's name.
        self.inputs["tasmota_named_alone"] = True
        self.assertTrue(self.run_preset(b"T" * 4096)[0])
        self.assertEqual((self.inputs["tasmota_wifi_preset"], self.inputs["tasmota_named_alone"]), (True, False))
        self.assertTrue(self.inputs.get("tasmota_wifi_preset"))  # outlasts a cut-off upload

    def test_the_tasmota_is_named_as_every_firmware_part_way_through(self):
        # Until F4's reset gives it Tasmota's own name; and its setup network,
        # should it raise one, by the same.
        template = self.ts.template_path(Path(__file__).resolve().parents[4]).read_bytes()
        self.inputs["device_mac"] = "12:34:56:ab:cd:ef"
        given, sent = self.run_preset(template)
        self.assertTrue(given)
        self.assertEqual(self.ts.text(sent[0], self.ts.SET_HOSTNAME), "autoflash-recovery-CDEF")
        self.assertEqual(self.ts.problems(sent[0]), [])  # the bridge checks the checksum first

    def test_on_the_installers_network_tasmota_is_named_without_the_wifi(self):
        # 4.4.4 has joined no network there, so it saves the settings with no
        # Wi-Fi: Tasmota makes its own network named as the installer's was,
        # not tasmota-ABCDEF-1234 (seen in the fault suite, C3 with the home
        # Wi-Fi gone).
        template = self.ts.template_path(Path(__file__).resolve().parents[4]).read_bytes()
        self.inputs["device_mac"] = "12:34:56:ab:cd:ef"
        given, sent = self.run_preset(template, address=setup_network.SETUP_HOST, answer=self.ALONE)
        self.assertFalse(given)
        self.assertEqual(self.ts.text(sent[0], self.ts.SET_HOSTNAME), "autoflash-recovery-CDEF")
        self.assertTrue(self.inputs.get("tasmota_named_alone"))
        self.assertIsNone(self.inputs.get("tasmota_wifi_preset"))
        self.assertIn("make its own network by that name", self.said)

    def test_an_installer_before_4_4_4_leaves_tasmota_its_own_name_quietly(self):
        self.inputs["device_mac"] = "12:34:56:ab:cd:ef"
        template = self.ts.template_path(Path(__file__).resolve().parents[4]).read_bytes()
        given, _ = self.run_preset(template, send_error="The bridge has not joined a network",
                                   address=setup_network.SETUP_HOST)
        self.assertFalse(given)
        self.assertIsNone(self.inputs.get("tasmota_named_alone"))
        self.assertEqual(self.said, "")  # the Wi-Fi is given on its network, as it always was

    def test_without_the_mac_nothing_is_sent_on_the_installers_network(self):
        self.assertEqual(self.run_preset(b"T" * 4096, address=setup_network.SETUP_HOST), (False, []))

    def test_named_alone_tasmotas_network_is_looked_for_once_the_installer_has_gone(self):
        from types import SimpleNamespace
        from unittest import mock
        self.inputs.update(device_mac="12:34:56:ab:cd:ef", tasmota_named_alone=True)
        ctx = SimpleNamespace(state=self.ctx.state)
        order = []
        with mock.patch.object(installer, "installer_gone",
                               side_effect=lambda **k: order.append("gone")), \
             mock.patch.object(installer, "tasmota_or_its_network",
                               side_effect=lambda ctx, **k: order.append(k["networks"])
                               or ("network", "autoflash-recovery-CDEF")), \
             mock.patch.object(join, "configure_device_wifi", return_value="192.0.2.52") as by_hand, \
             mock.patch.object(output, "say"):
            result = installer.finish_tasmota_setup(ctx, route="bridge network")
        self.assertEqual(order[0], "gone")
        self.assertEqual(order[1][0], "autoflash-recovery-CDEF")
        self.assertEqual(order[1][1:], names.setup_networks(ctx))  # and Tasmota's own, after a reset
        by_hand.assert_called_once_with(ctx, "autoflash-recovery-CDEF", join.NOT_GIVEN)
        self.assertEqual(result["device_ip"], "192.0.2.52")
        self.assertIsNone(result["device_name"])  # the run's name for it is not its own

    def test_the_interim_name_is_not_looked_for_otherwise(self):
        from types import SimpleNamespace
        from unittest import mock
        self.inputs["device_mac"] = "12:34:56:ab:cd:ef"
        ctx = SimpleNamespace(state=self.ctx.state)
        with mock.patch.object(installer, "installer_gone") as waited, \
             mock.patch.object(installer, "tasmota_or_its_network",
                               return_value=("network", "tasmota-ABCDEF-1234")) as looked, \
             mock.patch.object(join, "configure_device_wifi", return_value="192.0.2.52"), \
             mock.patch.object(output, "say"):
            installer.finish_tasmota_setup(ctx, route="bridge network")
        waited.assert_not_called()
        self.assertNotIn("autoflash-recovery-CDEF", looked.call_args.kwargs["networks"])

    def test_tasmota_on_the_installers_network_when_run_again_is_set_up(self):
        # Cut off after the upload: the network the installer had is Tasmota's
        # now, by the same name - not waited on as the installer's for ever.
        from types import SimpleNamespace
        from unittest import mock
        self.inputs.update(device_mac="12:34:56:ab:cd:ef", tasmota_named_alone=True)
        ctx = SimpleNamespace(state=self.ctx.state)
        # On its setup network Tasmota's page waits on a Wi-Fi scan, longer
        # than the look allows: only its status answers at once.
        pages = {(setup_network.SETUP_HOST, "/cm?cmnd=Status%202"):
                 '{"StatusFWR":{"Version":"15.6.0.1(f5b34a2-bluetooth)"}}'}
        with mock.patch.object(prompts, "find_ssid",
                               side_effect=lambda names, **k: "autoflash-recovery-CDEF"
                               if "autoflash-recovery-CDEF" in names else None), \
             mock.patch.object(setup_network, "page",
                               side_effect=lambda host, path="/", **k: pages.get((host, path))), \
             mock.patch.object(installer, "upload_tasmota") as uploaded, \
             mock.patch.object(join, "configure_device_wifi", return_value="192.0.2.52") as by_hand, \
             mock.patch.object(output, "say"):
            result = installer.step_berry_tasmota(ctx)
        uploaded.assert_not_called()
        by_hand.assert_called_once_with(ctx, "autoflash-recovery-CDEF", join.NOT_GIVEN)
        self.assertEqual((result["device_ip"], result["resumed"]), ("192.0.2.52", True))

    def test_tasmota_already_at_home_when_run_again_is_carried_on_with(self):
        # Seen in the fault suite (D1, a weak signal): the upload took, but its
        # answer was lost; Tasmota, given the Wi-Fi, joined by itself, and the
        # try again waited a quarter of an hour for the installer, gone.
        from types import SimpleNamespace
        from unittest import mock
        self.inputs.update(device_mac="12:34:56:ab:cd:ef", tasmota_wifi_preset=True)
        ctx = SimpleNamespace(state=self.ctx.state, finder=SimpleNamespace(poll=lambda: "192.0.2.51"),
                              set_device_ip=lambda ip: self.inputs.__setitem__("device_ip", ip))
        pages = {"192.0.2.51": "<title>Tasmota</title> Tasmota 15.6.0 by Theo Arends"}
        with mock.patch.object(prompts, "find_ssid", return_value=None), \
             mock.patch.object(setup_network, "page", side_effect=lambda host, *a, **k: pages.get(host)), \
             mock.patch.object(device, "Device", side_effect=OSError), \
             mock.patch.object(installer, "upload_tasmota") as uploaded, \
             mock.patch.object(installer, "ask_to_join_bridge") as asked, \
             mock.patch.object(output, "say"):
            result = installer.step_berry_tasmota(ctx)
        uploaded.assert_not_called()
        asked.assert_not_called()
        self.assertEqual((result["device_ip"], result["wifi"]), ("192.0.2.51", "preset"))

    def test_tasmota_on_its_own_network_says_why_for_the_report(self):
        from types import SimpleNamespace
        ctx = SimpleNamespace(state=self.ctx.state)
        self.assertEqual(installer.tasmota_why(ctx), join.NOT_GIVEN)   # never given the Wi-Fi
        self.inputs["tasmota_wifi_preset"] = True
        self.assertEqual(installer.tasmota_why(ctx), join.NOT_JOINED)   # given it, and did not join

    def test_the_bridges_own_network_asked_for_is_recorded_as_the_installer_not_joining(self):
        from types import SimpleNamespace
        from unittest import mock
        ctx = SimpleNamespace(state=self.ctx.state)
        with mock.patch.object(installer, "resend_if_back_on_sonoff", return_value=None), \
             mock.patch.object(prompts, "find_ssid", return_value="autoflash-recovery-CDEF"), \
             mock.patch.object(installer, "ask_to_join_bridge"), \
             mock.patch.object(installer, "wait_for_bridge", return_value=setup_network.SETUP_HOST), \
             mock.patch.object(output, "mark"):
            self.assertEqual(installer.join_bridge(ctx), setup_network.SETUP_HOST)
        self.assertEqual(self.inputs[join.OWN_NETWORK], [join.NOT_JOINED])

    def test_the_installer_is_waited_for_until_it_has_restarted(self):
        from unittest import mock
        answers = iter([True, True, False])
        clock = iter(range(5_000, 6_000, 2))
        with mock.patch.object(installer, "bridge_answers", side_effect=lambda host: next(answers)) as asked, \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "progress"):
            installer.installer_gone(timeout=60.0)
        self.assertEqual(asked.call_count, 3)

    def through_the_bridges_network(self, *, preset, at_home):
        """D1 sent through the bridge's own network: what follows."""
        from types import SimpleNamespace
        from unittest import mock
        self.inputs.update(tasmota_wifi_preset=preset)
        ctx = SimpleNamespace(state=self.ctx.state)
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(prompts, "find_ssid", return_value=None), \
             mock.patch.object(names, "bridge_networks", return_value=("autoflash-recovery-",)), \
             mock.patch.object(names, "setup_networks", return_value=("tasmota-",)), \
             mock.patch.object(installer, "reach_bridge", return_value=setup_network.SETUP_HOST), \
             mock.patch.object(installer, "upload_tasmota") as self.uploaded, \
             mock.patch.object(installer, "give_tasmota_the_wifi", return_value=False) as self.given, \
             mock.patch.object(installer, "tasmota_at_home",
                               return_value="192.0.2.51" if at_home else None), \
             mock.patch.object(join, "configure_device_wifi", return_value="192.0.2.52") as by_hand, \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "progress"), \
             mock.patch.object(output, "say"):
            result = installer.step_berry_tasmota(ctx)
        return result, by_hand.called, [call.args[0] for call in shown.call_args_list]

    def test_the_installers_network_still_listed_is_not_taken_for_tasmotas(self):
        # Seen in the fault suite (D1, the home Wi-Fi gone for a while): sent
        # through the installer's own network, Tasmota joined the home Wi-Fi
        # with the details it was given, while the installer's network - named
        # as the run names Tasmota - was still listed for a moment.
        from types import SimpleNamespace
        from unittest import mock
        self.inputs["device_mac"] = "12:34:56:ab:cd:ef"
        ctx = SimpleNamespace(state=self.ctx.state)
        clock = iter(range(0, 10_000, 5))
        with mock.patch.object(prompts, "scan_wifi_ssids",
                               return_value=["Home", "autoflash-recovery-CDEF"]), \
             mock.patch.object(installer, "tasmota_at_home", side_effect=[None, "192.0.2.51"]), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(output, "progress"):
            self.assertEqual(installer.tasmota_or_its_network(ctx, timeout=60.0), ("home", "192.0.2.51"))

    def test_tasmota_given_the_wifi_before_a_cut_off_joins_by_itself(self):
        # Seen in the fault suite: the bridge saved Tasmota's settings, the
        # upload was cut off, and the next went through the bridge's own
        # network - Tasmota then joined the home Wi-Fi with them, and the run
        # waited on a setup network it never raised.
        result, by_hand, shown = self.through_the_bridges_network(preset=True, at_home=True)
        self.assertEqual(result["device_ip"], "192.0.2.51")
        self.assertFalse(by_hand)
        self.assertIn("Back to your usual Wi-Fi", shown)
        self.given.assert_not_called()   # the bridge has them already
        self.uploaded.assert_called_once()

    def test_tasmota_found_at_home_is_not_asked_for_its_setup_network(self):
        # Without the record - settings saved by an earlier run - it is seen anyway.
        result, by_hand, _ = self.through_the_bridges_network(preset=False, at_home=True)
        self.assertEqual(result["device_ip"], "192.0.2.51")
        self.assertFalse(by_hand)

    def test_tasmota_on_neither_is_set_up_through_its_network(self):
        result, by_hand, _ = self.through_the_bridges_network(preset=False, at_home=False)
        self.assertTrue(by_hand)
        self.assertEqual(result["device_ip"], "192.0.2.52")
        self.given.assert_called_once()
        self.uploaded.assert_called_once()

    def test_without_a_template_tasmota_is_set_up_by_hand(self):
        self.assertEqual(self.run_preset(None), (False, []))

    def test_a_bridge_that_refuses_means_by_hand(self):
        self.assertEqual(self.run_preset(b"T" * 4096, send_error="older than v5")[0], False)

    def test_a_template_is_not_used_with_other_berry_firmware(self):
        template, reason = self.ts.load_template(self.dir.name, "0" * 64)
        self.assertIsNone(template)
        self.assertIn("Berry Tasmota", reason)

    def test_the_bridge_page_is_not_mistaken_for_tasmota(self):
        from types import SimpleNamespace
        from unittest import mock
        pages = iter(["<title>Autoflash bridge</title>", None, "<title>Tasmota</title> tasmota"])
        state = SimpleNamespace(set_input=lambda *a: None, input=lambda *a: None)
        ctx = SimpleNamespace(finder=SimpleNamespace(poll=lambda: "192.0.2.51"),
                              set_device_ip=lambda ip: None, state=state)
        clock = iter(range(0, 10_000, 3))
        with mock.patch.object(setup_network, "page", side_effect=lambda *a, **k: next(pages)), \
             mock.patch.object(time, "monotonic", side_effect=lambda: next(clock)), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(device, "Device", side_effect=OSError), \
             mock.patch.object(output, "say"):
            self.assertEqual(installer.tasmota_on_home_network(ctx), "192.0.2.51")


class StockBackupTests(unittest.TestCase):
    """D1 keeps the device's whole flash through the bridge before writing Tasmota."""

    REPORT = {"size": 8, "sha256": "0" * 64, "partition_table_md5_valid": True,
              "partitions": [], "otadata_selects": "ota_0"}
    MAC = "58:cf:79:01:02:03"

    def setUp(self):
        from types import SimpleNamespace
        self.records = records
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patch = mock.patch.object(records, "DEVICES", Path(self.dir.name))
        patch.start()
        self.addCleanup(patch.stop)
        self.inputs = {"stock_version": "1.2.0", "device_mac": self.MAC}
        self.captures = {}
        state = SimpleNamespace(input=lambda key, default=None: self.inputs.get(key, default),
                                set_input=self.inputs.__setitem__, capture=self.captures.__setitem__)
        self.ctx = SimpleNamespace(state=state, args=SimpleNamespace(no_backup=False))
        unread = mock.patch.object(installer, "require_known_layout")  # its own tests: StockLayoutTests
        self.layout_checked = unread.start()
        self.addCleanup(unread.stop)
        self.dump = (b"F" * 8, efuse_with_mac(self.MAC), dict(self.REPORT))
        self.reached = "192.0.2.51"  # where reach_bridge finds the installer again

    def back_up(self, reads, answers=(1,)):
        """Run the backup with each read in turn returning or raising `reads`,
        and the failure menu answered with `answers` (0 again, 1 without)."""
        from tools.autoflash.prompts import QuitRun
        outcomes, replies = iter(reads), iter(answers)
        asked = []
        self.read_from = []

        def read(host):
            self.read_from.append(host)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        def ask(question, options, help_lines=()):
            asked.append(options)
            reply = next(replies)
            if reply == "quit":
                raise QuitRun(130)
            return reply

        with mock.patch.object(dump_stock_flash, "read_dump", side_effect=read), \
             mock.patch.object(installer, "reach_bridge", return_value=self.reached), \
             mock.patch.object(prompts, "ask_choice", side_effect=ask), \
             mock.patch.object(prompts, "section"), \
             mock.patch.object(output, "say"):
            installer.back_up_stock(self.ctx, "192.0.2.51")
        return asked

    def test_the_backup_is_kept_under_the_devices_mac(self):
        self.assertEqual(self.back_up([self.dump]), [])
        saved = self.records.backup_dir(self.MAC)
        self.assertEqual((saved / "flash.bin").read_bytes(), b"F" * 8)
        self.assertIn("write_flash 0x0 flash.bin", (saved / "README.txt").read_text())
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])
        self.assertEqual(self.records.read_device(self.MAC)["stock_version"], "1.2.0")

    def test_one_failed_read_is_retried(self):
        self.back_up([OSError("timed out"), self.dump])
        self.assertIn("stock_backup", self.inputs)

    def test_a_retry_reads_where_the_installer_is_now(self):
        # Seen in the fault suite: the home Wi-Fi gone mid-backup, the
        # installer raised its own network, and this computer joined it -
        # both reads went to its address on the home network, and timed out.
        self.reached = "192.168.4.1"
        self.back_up([OSError("timed out"), self.dump])
        self.assertEqual(self.read_from, ["192.0.2.51", "192.168.4.1"])
        self.assertIn("stock_backup", self.inputs)

    def test_a_bridge_that_cannot_read_flash_means_no_backup(self):
        import urllib.error
        missing = urllib.error.HTTPError("http://x/flash", 404, "Not Found", {}, None)
        self.assertEqual(self.back_up([missing]), [])
        self.assertNotIn("stock_backup", self.inputs)

    def failing(self):
        """Every read of a round of BACKUP_ROUNDS failing."""
        return [OSError(str(n)) for n in range(2 * installer.BACKUP_ROUNDS)]

    def test_a_failed_round_is_read_again_before_anyone_is_asked(self):
        # The reads only read: another round is what the person would choose.
        self.assertEqual(self.back_up([OSError("a"), OSError("b"), self.dump]), [])
        self.assertIn("stock_backup", self.inputs)
        self.assertEqual(self.captures["C3.attempts"], 3)   # its retries in the report, as B4's are

    def test_after_every_round_fails_the_person_may_carry_on_without(self):
        (options,) = self.back_up(self.failing())
        self.assertEqual(options, ["Try again", "Carry on without a backup"])
        self.assertNotIn("stock_backup", self.inputs)

    def test_or_try_again(self):
        self.back_up(self.failing() + [self.dump], answers=(0,))
        self.assertIn("stock_backup", self.inputs)

    def test_an_unattended_run_carries_on_without(self):
        from tools.autoflash import prompts
        with mock.patch.object(prompts, "UNATTENDED", True), \
             mock.patch.object(prompts, "answered_unattended"):
            self.assertEqual(self.back_up(self.failing(), answers=()), [])
        self.assertNotIn("stock_backup", self.inputs)

    def test_a_simulated_failure_fails_until_the_question_only(self):
        with mock.patch.object(installer, "SIMULATED_BACKUP_FAILURES", 2 * installer.BACKUP_ROUNDS):
            asked = self.back_up([self.dump], answers=(0,))
        self.assertEqual(len(asked), 1)
        self.assertIn("stock_backup", self.inputs)

    def stage(self, done=()):
        """Run the backup stage, C3, with the bridge reached at 192.0.2.51."""
        self.ctx.state.status = lambda step: DONE if step in done else "pending"
        with mock.patch.object(installer, "reach_bridge", return_value="192.0.2.51") as reach, \
             mock.patch.object(dump_stock_flash, "read_dump", return_value=self.dump) as read, \
             mock.patch.object(output, "say"):
            result = installer.step_backup(self.ctx)
        return result, reach, read

    def test_the_backup_stage_reads_through_the_bridge(self):
        result, reach, read = self.stage()
        read.assert_called_once_with("192.0.2.51")
        self.assertEqual(result, {"outcome": "saved", "stage_outcome": "complete"})
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])
        self.layout_checked.assert_called_once_with(self.ctx)   # before D1 writes over Sonoff's firmware

    def test_carrying_on_without_a_backup_skips_the_stage(self):
        self.ctx.state.status = lambda step: "pending"
        with mock.patch.object(installer, "reach_bridge", return_value="192.0.2.51"), \
             mock.patch.object(dump_stock_flash, "read_dump", side_effect=OSError("timed out")), \
             mock.patch.object(prompts, "ask_choice", return_value=1), \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            result = installer.step_backup(self.ctx)
        self.assertEqual(result, {"outcome": "failed", "stage_outcome": "skipped"})
        self.assertEqual(self.inputs["backup_outcome"], "failed")

    def test_a_device_backed_up_before_needs_no_bridge(self):
        self.records.save_backup(b"original", efuse_with_mac(self.MAC), {}, "1.2.0")
        result, reach, read = self.stage()
        reach.assert_not_called()
        read.assert_not_called()
        self.assertEqual(result, {"outcome": "existing", "stage_outcome": "already done"})
        self.assertIn("sonoff-backup", self.inputs["stock_backup"])
        self.layout_checked.assert_called_once_with(self.ctx)   # its layout checked all the same

    def test_a_resumed_run_does_not_read_again(self):
        self.inputs["stock_backup"] = "captures/devices/x/sonoff-backup"
        _, reach, read = self.stage()
        reach.assert_not_called()
        read.assert_not_called()

    def test_no_backup_when_asked_for_none(self):
        self.ctx.args.no_backup = True
        result, reach, read = self.stage()
        read.assert_not_called()
        self.assertEqual(result["outcome"], "not wanted")
        self.assertNotIn("stock_backup", self.inputs)

    def test_a_resumed_run_keeps_how_its_backup_went(self):
        self.inputs["stock_backup"] = "captures/devices/x/sonoff-backup"
        for earlier, outcome in (("saved", "saved"), (None, "existing")):
            self.inputs["backup_outcome"] = earlier
            self.assertEqual(self.stage()[0]["outcome"], outcome, earlier)

    def test_a_run_past_the_installer_does_not_look_for_it(self):
        # A run from before this stage existed, resumed after D1.
        result, reach, _ = self.stage(done=("D1",))
        reach.assert_not_called()
        self.assertEqual(result, {"outcome": "not taken", "stage_outcome": "skipped"})

    def test_or_quit_before_tasmota_is_written(self):
        from tools.autoflash.prompts import QuitRun
        with self.assertRaises(QuitRun):
            self.back_up(self.failing(), answers=("quit",))
        self.assertNotIn("stock_backup", self.inputs)


class SpoiltTasmotaTests(unittest.TestCase):
    """D1 - --simulate-bad-image D1[:N]: Tasmota sent to the installer spoilt,
    so the installer refuses it as it would one damaged on the way."""

    IMAGE = ROOT / "artifacts" / "tasmota32c3-bluetooth.bin"

    def test_spoilt_as_many_times_as_asked_then_sent_whole(self):
        from types import SimpleNamespace
        from tools.autoflash import imprint
        from tools.autoflash.routes.espressif import images
        sent = []
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(images, "SPOIL_STEP", "D1"), mock.patch.object(images, "SPOIL_TIMES", 2), \
                mock.patch.object(setup_network, "upload_to_bridge",
                                  side_effect=lambda payload, **_: sent.append(payload)), \
                mock.patch.object(output, "say"):
            ctx = SimpleNamespace(state=RunState.open(Path(directory)))
            for _ in range(3):
                installer.upload_tasmota(ctx, self.IMAGE)
            self.assertEqual(ctx.state.data["captures"]["D1.spoilt"], 2)
        whole = self.IMAGE.read_bytes()
        self.assertEqual([payload == whole for payload in sent], [False, False, True])
        # Whole, but failing the image's own checks, which the installer makes.
        self.assertEqual(len(sent[0]), len(whole))
        checked = imprint.parse_esp_image(sent[0])
        self.assertFalse(checked["checksum_valid"] or checked["sha256_valid"])

    def test_unasked_it_is_sent_as_it_is(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(setup_network, "upload_to_bridge") as uploaded, mock.patch.object(output, "say"):
            installer.upload_tasmota(SimpleNamespace(state=RunState.open(Path(directory))), self.IMAGE)
        uploaded.assert_called_once_with(self.IMAGE.read_bytes(), host=setup_network.SETUP_HOST)


class StockLayoutTests(unittest.TestCase):
    """C3 - a device laid out as no plan expects stops while it can still go back."""

    def run_gate(self, table, backup=None):
        from types import SimpleNamespace
        from unittest import mock
        from tools.autoflash.routes.espressif import installer
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda name: None, set_input=lambda name, value: None))
        with mock.patch.object(installer, "reach_bridge", return_value="192.0.2.51"), \
                mock.patch.object(installer, "bridge_info", return_value={}), \
                mock.patch.object(installer.dump_stock_flash, "read_table", return_value=table):
            installer.require_known_layout(ctx)

    def test_sonoffs_usual_table_goes_on(self):
        from tools.espressif.safeboot.safeboot_migration import SOURCE_TABLE_FILE
        self.run_gate(SOURCE_TABLE_FILE.read_bytes())

    def test_any_other_stops_before_anything_is_written(self):
        from tools.autoflash.errors import Abort
        with self.assertRaisesRegex(Abort, "switch it off and on"):
            self.run_gate(b"\xff" * 0x1000)


class HardwareNoteTests(unittest.TestCase):
    """C3 - the chip's revision and the flash chip, kept for the run's report."""

    def noted(self, info, backup=None):
        from types import SimpleNamespace
        from tools.autoflash.routes.espressif import installer
        kept = {}
        ctx = SimpleNamespace(state=SimpleNamespace(set_input=kept.__setitem__))
        installer.note_hardware(ctx, info, backup)
        return kept["hardware"]

    def test_as_the_installer_says_them(self):
        self.assertEqual(self.noted({"chip revision": "0.4", "flash id": "C84016", "flash chip size": "4194304"}),
                         {"revision": "0.4", "flash_id": "c84016", "flash_mb": 4})

    def test_the_size_from_the_backup_where_the_installer_says_none_and_nothing_made_up(self):
        import tempfile
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "flash.bin").write_bytes(b"\xff" * 0x800000)
            backup = SimpleNamespace(directory=Path(tmp))
            self.assertEqual(self.noted({"chip revision": "three", "flash id": "not readable"}, backup),
                             {"revision": None, "flash_id": None, "flash_mb": 8})
        self.assertEqual(self.noted({}), {"revision": None, "flash_id": None, "flash_mb": None})


class BridgeInfoTests(unittest.TestCase):
    """C3 - what only the chip can say goes in the log, and costs a run nothing."""

    INFO = b"bridge 4.4.5\nchip: esp32\nchip revision: 3.1\nflash id: c84016\n"

    def setUp(self):
        from types import SimpleNamespace
        from unittest import mock
        from tools.autoflash.routes.espressif import installer
        self.installer = installer
        self.ctx = SimpleNamespace(state=SimpleNamespace(input=lambda name: None, set_input=lambda name, value: None))
        self.logged = []
        for patch in (mock.patch.object(installer.output, "log_only", self.logged.append),
                      mock.patch.object(installer.output, "progress_clear"),
                      mock.patch.object(installer.dump_stock_flash, "read_table",
                                        return_value=self.table()),
                      mock.patch.object(installer, "reach_bridge", return_value="192.0.2.51")):
            patch.start()
            self.addCleanup(patch.stop)

    @staticmethod
    def table():
        from tools.espressif.safeboot.safeboot_migration import SOURCE_TABLE_FILE
        return SOURCE_TABLE_FILE.read_bytes()

    def read(self, *, answers=True, home=None, body=INFO):
        from unittest import mock
        with mock.patch.object(self.installer, "bridge_answers", return_value=answers), \
                mock.patch.object(self.installer, "bridge_on_home_network", return_value=home) as looked, \
                mock.patch.object(self.installer.dump_stock_flash, "read_info", return_value=body) as read:
            return self.installer.bridge_info(self.ctx), looked, read

    def test_read_once_on_the_installers_own_network(self):
        info, looked, read = self.read()
        self.assertEqual(info["flash id"], "c84016")
        read.assert_called_once_with(self.installer.setup_network.SETUP_HOST)
        looked.assert_not_called()

    def test_or_on_the_home_network_without_asking_anyone(self):
        info, looked, read = self.read(answers=False, home="192.0.2.51")
        self.assertEqual(info["chip"], "esp32")
        read.assert_called_once_with("192.0.2.51")
        self.assertLessEqual(looked.call_args.kwargs["timeout"], 15)

    def test_an_older_bridge_or_no_bridge_gives_nothing(self):
        self.assertEqual(self.read(body=None)[0], {})
        info, _, read = self.read(answers=False, home=None)
        self.assertEqual(info, {})
        read.assert_not_called()

    def test_a_network_error_gives_nothing(self):
        from unittest import mock
        with mock.patch.object(self.installer, "bridge_answers", side_effect=OSError("gone")):
            self.assertEqual(self.installer.bridge_info(self.ctx), {})

    def test_with_no_backup_its_lines_are_still_logged(self):
        from unittest import mock
        with mock.patch.object(self.installer, "bridge_info",
                               return_value=self.installer.device_facts.parse_info(self.INFO)):
            self.installer.require_known_layout(self.ctx)
        self.assertIn("    Chip revision: 3.1", self.logged)
        self.assertIn("    Installer says flash id: c84016", self.logged)

    def test_with_an_older_bridge_the_run_goes_on_and_logs_nothing_of_it(self):
        from unittest import mock
        with mock.patch.object(self.installer, "bridge_info", return_value={}):
            self.installer.require_known_layout(self.ctx)
        self.assertEqual(self.logged, [])

    def test_the_backups_facts_carry_the_exact_revision(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        from tools.espressif import flashmap
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "flash.bin").write_bytes(b"\xff" * 0x400000)
            (Path(folder) / "efuse.bin").write_bytes(bytes(flashmap.ESP32_EFUSE_SIZE))
            with mock.patch.object(self.installer.kinds, "of_run"):
                self.installer.log_device_facts(self.ctx, Path(folder), shown=False,
                                                info=self.installer.device_facts.parse_info(self.INFO))
        self.assertIn("    Chip revision: 3.1", self.logged)
        self.assertFalse([line for line in self.logged if "at least" in line], self.logged)


class DeliveryPortTests(unittest.TestCase):
    """The bridge is served on 8088 unless something else has it."""

    def run_bridge(self, busy, sends=1, codes=(0,)):
        from types import SimpleNamespace
        from unittest import mock
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / safety_locks.DIR).mkdir(parents=True)
            (root / safety_locks.DIR / "RECOVERY_LOCK").write_text("locked")
            state = RunState.open(root)
            state.set_input("device_ip", "192.0.2.96")
            state.set_input("stock_version", "1.2.0")
            ctx = SimpleNamespace(state=state, bridge=Path("bridge.ota"), dir=root,
                                  listen_ip=lambda: "192.0.2.141", email=lambda: "me@example.com",
                                  ewelink_env=lambda: {}, helper_env=lambda: {}, ewelink_region=lambda: "eu",
                                  account_device=SimpleNamespace(device_id="1001234abc"))
            opened = []
            # installer's own ROOT: the lock it lifts is this one, never the checkout's.
            with mock.patch.object(installer, "ROOT", root), \
                 mock.patch.object(hostsys, "port_is_free", lambda ip, port: port not in busy), \
                 mock.patch.object(firewall, "ensure_firewall",
                                   lambda c: opened.append(firewall.firewall_ports(c))), \
                 mock.patch.object(prompts, "warning_box"), \
                 mock.patch.object(prompts, "require_word", return_value=True) as asked, \
                 mock.patch.object(hostsys, "run_tool", side_effect=list(codes) * sends) as run_tool, \
                 mock.patch.object(output, "say"):
                try:
                    for _ in range(sends):
                        self.result = installer.step_bridge(ctx)
                    error = None
                except errors.Abort as exc:
                    error = str(exc)
            self.asked = asked.call_count
            command = self.command = run_tool.call_args.args[0] if run_tool.called else None
            port = command[command.index("--listen-port") + 1] if command else None
            if command:
                # Lifted here, under this root: its record says so.
                self.assertTrue((root / "captures" / ".safety-locks" / "RECOVERY_LOCK.guard").exists())
            return port, opened, state.input("delivery_port"), error

    def test_not_delivered_for_want_of_a_switch_off_and_on_it_is_asked_for_and_sent_again(self):
        # As the Beken route's B2 asks (arrival.power_cycle), counted for the report.
        from unittest import mock
        with mock.patch.object(installer, "evidence_bridge_installed", return_value=(False, "")), \
                mock.patch.object(installer, "needs_power_cycle", return_value=True), \
                mock.patch.object(installer, "bridge_failure_advice", return_value=["switch it off and on"]), \
                mock.patch.object(output, "tool_problems", return_value=[]), \
                mock.patch.object(installer.arrival, "power_cycle") as asked, \
                mock.patch.object(installer.arrival, "online_in_ewelink", return_value=True):
            self.run_bridge(busy=set(), codes=(1, 0))
        asked.assert_called_once()
        self.assertIn("seconds", self.result)
        with mock.patch.object(installer, "evidence_bridge_installed", return_value=(False, "")), \
                mock.patch.object(installer, "needs_power_cycle", return_value=True), \
                mock.patch.object(prompts, "UNATTENDED", True), mock.patch.object(prompts, "section"), \
                mock.patch.object(installer.arrival, "power_cycle") as asked:
            self.assertEqual(self.run_bridge(busy=set(), codes=(1,))[3], "the installer was not delivered")
        asked.assert_not_called()   # unattended: it stops, saying to

    def test_sent_again_in_the_same_run_the_go_ahead_is_not_asked_again(self):
        # The device back on Sonoff's firmware after a power cut, the installer is
        # sent again by itself (resend_if_back_on_sonoff): asked again, an
        # unattended run's PROCEED was typed twice (the suite's FAIL-ASKED-AGAIN).
        self.run_bridge(busy=set(), sends=2)
        self.assertEqual(self.asked, 1)

    def test_the_sender_adds_its_events_to_the_runs(self):
        # Its sending again, for the fault suites to follow (output.EVENTS).
        from unittest import mock
        events = Path("/run-folder") / output.EVENTS
        with mock.patch.object(output, "events_path", return_value=events):
            self.run_bridge(busy=set())
        self.assertEqual(self.command[self.command.index("--events") + 1], str(events))
        with mock.patch.object(output, "events_path", return_value=None):
            self.run_bridge(busy=set())
        self.assertNotIn("--events", self.command)

    def test_the_sender_says_each_line_as_it_comes(self):
        # Seen: its lines reached the run's log all at once as it ended, each
        # stamped then, so when eWeLink said the device was back could not be told.
        self.run_bridge(busy=set())
        self.assertEqual(self.command[1], "-u")

    def test_8088_is_used_when_free(self):
        port, opened, recorded, _ = self.run_bridge(busy=set())
        self.assertEqual((port, recorded), ("8088", "8088"))
        self.assertTrue(0 <= self.result["seconds"] < 60, self.result)   # how long it took, for the report
        self.assertEqual(opened, [[8088, 8089]])

    def test_the_sender_is_given_the_accounts_device_id_not_a_discovery_report(self):
        self.run_bridge(busy=set())
        self.assertEqual(self.command[self.command.index("--device-id") + 1], "1001234abc")
        self.assertNotIn("--mdns-capture", self.command)

    def test_a_busy_8088_moves_to_8090_and_the_firewall_follows(self):
        # 8089 is the phase port, so it is never offered for the bridge.
        port, opened, recorded, _ = self.run_bridge(busy={8088})
        self.assertEqual((port, recorded), ("8090", "8090"))
        self.assertEqual(opened, [[8089, 8090]])

    def test_every_port_busy_stops_before_anything_is_sent(self):
        port, opened, _, error = self.run_bridge(busy={8088, *range(8090, 8100)})
        self.assertIsNone(port)
        self.assertEqual(opened, [])
        self.assertIn("all in use", error)


class BridgeEvidenceTests(unittest.TestCase):
    """The installer raises its own network only when the home one fails."""

    def test_bridge_on_the_home_network_counts(self):
        from unittest import mock
        with mock.patch.object(installer, "bridge_on_home_network", return_value="192.0.2.50"), \
             mock.patch.object(prompts, "find_ssid", return_value=None):
            installed, reason = installer.evidence_bridge_installed(mock.Mock())
        self.assertTrue(installed)
        self.assertIn("192.0.2.50", reason)

    def test_its_own_network_still_counts(self):
        from unittest import mock
        with mock.patch.object(installer, "bridge_on_home_network", return_value=None), \
             mock.patch.object(prompts, "find_ssid", return_value="autoflash-recovery-CDEF"):
            installed, _ = installer.evidence_bridge_installed(self.ctx())
        self.assertTrue(installed)

    def test_neither_is_not_proof_of_failure(self):
        from unittest import mock
        with mock.patch.object(installer, "bridge_on_home_network", return_value=None), \
             mock.patch.object(prompts, "find_ssid", return_value=None):
            installed, _ = installer.evidence_bridge_installed(self.ctx())
        self.assertIsNone(installed)

    def ctx(self):
        from types import SimpleNamespace
        return SimpleNamespace(state=SimpleNamespace(input=lambda key: "12:34:56:ab:cd:ef" if key == "device_mac" else None))


class JoinBridgeTests(unittest.TestCase):
    """The installer's network is open, like every setup network of this
    project's firmware; only an earlier release's has a password."""

    def shown(self, network):
        from unittest import mock
        with mock.patch.object(prompts, "section") as section, \
             mock.patch.object(hostsys, "copy_to_clipboard", return_value=False) as copied:
            installer.ask_to_join_bridge(network)
        return " ".join(section.call_args.args[1]), copied.called

    def test_open_no_password(self):
        # 4.4.4's, and 4.4.3's before it.
        for network in ("autoflash-recovery-CDEF", "Sonoff-Setup-CDEF"):
            text, copied = self.shown(network)
            self.assertIn(f"{network}      (open - no password)", text)
            self.assertNotIn("password  ", text)
            self.assertFalse(copied)

    def test_an_earlier_installers_network_has_its_password(self):
        text, copied = self.shown("S60-OTA-Bridge-A04D")
        self.assertIn("password  s60-ota-bridge", text)
        self.assertTrue(copied)


class RestartBridgeTests(unittest.TestCase):
    """POST /restart (bridge 4.4.6): the device started again into Sonoff's
    firmware, as switching it off and on would."""

    def serve(self, status):
        import http.server
        import threading
        asked = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                asked.append(self.path)
                self.send_response(status if self.path == "/restart" else 404)
                self.end_headers()
                self.wfile.write(b"Restarting into app0 at 0x00010000.\n")

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f"127.0.0.1:{server.server_address[1]}", asked

    def test_a_bridge_that_restarts_says_so(self):
        host, asked = self.serve(200)
        self.assertTrue(installer.restart_bridge(host))
        self.assertEqual(asked, ["/restart"])

    def test_one_that_refuses_or_has_no_restart_does_not(self):
        for status in (409, 404):   # an upload under way; a bridge before 4.4.6
            with self.subTest(status):
                host, _ = self.serve(status)
                self.assertFalse(installer.restart_bridge(host))

    def test_its_status_says_whether_sonoffs_firmware_is_there_to_start(self):
        for status, has in (("bridge 4.4.6\nhome network: joined 192.0.2.77\n", True),
                            ("bridge 4.4.6\nhome network: joined 192.0.2.77\nfallback: none - an upload was cut "
                             "off\n", False),
                            (None, True)):   # unreadable: a restart is tried, and refused or failing, asked
            with self.subTest(status=status), mock.patch.object(setup_network, "page", return_value=status):
                self.assertEqual(installer.has_fallback("192.0.2.77"), has)

    def test_restarted_it_is_waited_for_until_sonoff_answers(self):
        ctx = mock.sentinel.ctx
        with mock.patch.object(installer, "restart_bridge", return_value=True), \
                mock.patch.object(installer.time, "sleep"), mock.patch.object(output, "say"), \
                mock.patch.object(installer.arrival, "sonoff_answers", side_effect=[False, False, True]) as asked:
            self.assertTrue(installer.restart_into_sonoff(ctx, "192.0.2.77"))
        self.assertEqual(asked.call_count, 3)
        asked.assert_called_with(ctx, "192.0.2.77")

    def test_one_that_will_not_restart_is_not_waited_for(self):
        with mock.patch.object(installer, "restart_bridge", return_value=False), \
                mock.patch.object(installer.arrival, "sonoff_answers") as asked:
            self.assertFalse(installer.restart_into_sonoff(mock.sentinel.ctx, "192.0.2.77"))
        asked.assert_not_called()

    def test_back_on_sonoff_is_sonoffs_firmware_answering_for_certain(self):
        from types import SimpleNamespace
        # With LAN Control off it answers nothing here: eWeLink says (arrival.sonoff_answers).
        ctx = SimpleNamespace(state=mock.Mock(input=lambda key: {"device_ip": "192.0.2.50"}.get(key)))
        for answers in (True, False):
            with self.subTest(answers), \
                    mock.patch.object(installer.arrival, "sonoff_answers", return_value=answers) as asked:
                self.assertEqual(installer.back_on_sonoff(ctx), answers)
            asked.assert_called_once_with(ctx, "192.0.2.50")


if __name__ == "__main__":
    unittest.main()
