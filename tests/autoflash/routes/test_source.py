"""What the device runs when a run starts, whatever its family
(tools/autoflash/routes/source.py): found by address or by a search of the
network, waited for if it may be restarting, and unlocked if it asks."""
import contextlib
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import detect, device, errors, esphome, hostsys, output, records, prompts, routes, runs
from tools.autoflash import unlock
from tools.autoflash.routes import source
from tools.autoflash.routes.espressif import source as espressif_source
from tools.autoflash.state import RunState


class KindLeftPartWayTests(unittest.TestCase):
    """A device left part way says nothing of its kind: the last run of it does."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        patches = (mock.patch.object(records, "CAPTURES", self.root), mock.patch.object(records, "DEVICES", self.root / "devices"),
                   mock.patch.object(output, "say"))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def run_of(self, mac, name, **inputs):
        directory = self.root / "devices" / mac.replace(":", "-") / records.RUNS / name
        directory.mkdir(parents=True)
        state = RunState.open(directory)
        for key, value in inputs.items():
            state.set_input(key, value)
        return directory

    def adopted(self, firmware, name=None, kind=None):
        from types import SimpleNamespace
        here = self.root / "here"
        here.mkdir()
        ctx = SimpleNamespace(state=RunState.open(here), dir=here, set_device_ip=lambda host: None)
        with mock.patch.object(runs, "file_device"), mock.patch.object(hostsys, "mac_of", return_value=None):
            source.adopt_device(ctx, detect.Found("192.0.2.50", firmware, "12:34:56:ab:cd:ef", name, kind))
        return ctx.state.input("device_kind")

    def test_the_mover_is_the_kind_its_last_run_was(self):
        self.run_of("12:34:56:ab:cd:ef", "1", device_kind="sonoff-th-elite")
        self.assertEqual(self.adopted("midway", "the mover"), "sonoff-th-elite")

    def test_the_installer_is_the_kind_its_last_run_was(self):
        # Found again by a run with --new, it was taken for an S60 and sent
        # the S60's interim, which a POW Elite's installer refused.
        self.run_of("12:34:56:ab:cd:ef", "1", device_kind="sonoff-pow-elite")
        self.assertEqual(self.adopted("installer", "the installer"), "sonoff-pow-elite")

    def test_tasmota_without_its_template_is_the_kind_its_last_run_was(self):
        # Taken for an S60, a TH Elite's was sent the S60's plan, which it refused.
        self.run_of("12:34:56:ab:cd:ef", "1", device_kind="sonoff-th-elite")
        self.assertEqual(self.adopted("untemplated"), "sonoff-th-elite")

    def test_another_devices_run_says_nothing_of_it(self):
        self.run_of("12:34:56:ab:cd:00", "1", device_kind="sonoff-th-elite")
        self.assertIsNone(self.adopted("midway", "the mover"))

    def test_what_it_says_itself_comes_first(self):
        self.run_of("12:34:56:ab:cd:ef", "1", device_kind="sonoff-th-elite")
        self.assertEqual(self.adopted("midway", "the hub", "sonoff-pow-elite"), "sonoff-pow-elite")


class UntemplatedByAddressTests(unittest.TestCase):
    """--device-ip at a Tasmota without the device's template: found as the
    network search finds it, on a device this computer knows."""

    def found(self, known):
        from types import SimpleNamespace
        inputs = {"device_ip": "192.0.2.137"}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get), web_password=None, esphome_login=None)
        with mock.patch.object(detect, "identify", return_value=None), \
                mock.patch.object(detect, "answers_as_tasmota", return_value=True), \
                mock.patch.object(hostsys, "mac_of", return_value="12:34:56:AB:CD:EF"), \
                mock.patch.object(source, "known_macs", return_value=known), \
                mock.patch.object(source, "adopt_device"), \
                mock.patch.object(espressif_source, "past_the_commit", side_effect=lambda ctx, running: running):
            return source.find_current_firmware(ctx)

    def test_a_known_device_is_untemplated(self):
        self.assertEqual(self.found({"12:34:56:ab:cd:ef"}), "untemplated")

    def test_any_other_tasmota_is_not_taken_for_one(self):
        self.assertEqual(self.found(set()), "sonoff")


class AdoptDeviceTests(unittest.TestCase):
    def test_the_kind_a_found_device_says_is_kept_for_the_run(self):
        from types import SimpleNamespace
        inputs = {}
        ctx = SimpleNamespace(state=SimpleNamespace(set_input=inputs.__setitem__, input=inputs.get),
                              set_device_ip=lambda ip: inputs.__setitem__("device_ip", ip))
        found = detect.Found("192.0.2.137", "tasmota", None, "tasmota-ABCDEF-1234",
                                           "sonoff-s60")
        with mock.patch.object(hostsys, "mac_of", return_value=None), \
             mock.patch.object(output, "say"):
            source.adopt_device(ctx, found)
        self.assertEqual(inputs["device_kind"], "sonoff-s60")

    def test_the_run_takes_its_address_and_files_itself_by_its_mac(self):
        from types import SimpleNamespace
        inputs = {}
        ctx = SimpleNamespace(state=SimpleNamespace(set_input=inputs.__setitem__, input=inputs.get),
                              set_device_ip=lambda ip: inputs.__setitem__("device_ip", ip))
        found = detect.Found("192.0.2.137", "tasmota", "12-34-56-AB-CD-EF", "tasmota-ABCDEF-1234", "sonoff-s60")
        with mock.patch.object(runs, "file_device") as filed, mock.patch.object(output, "say"):
            source.adopt_device(ctx, found)
        self.assertEqual((inputs["device_ip"], inputs["device_mac"]), ("192.0.2.137", "12:34:56:ab:cd:ef"))
        filed.assert_called_once_with(ctx, "12:34:56:ab:cd:ef", None)

    def adopt_esphome(self, title, comment=None):
        from types import SimpleNamespace
        inputs = {}
        ctx = SimpleNamespace(state=SimpleNamespace(set_input=inputs.__setitem__, input=inputs.get),
                              set_device_ip=lambda ip: inputs.__setitem__("device_ip", ip))
        with mock.patch.object(hostsys, "mac_of", return_value=None), \
             mock.patch.object(source.esphome, "title", return_value=(title, comment)), \
             mock.patch.object(output, "say"):
            source.adopt_device(ctx, detect.Found("192.0.2.137", "esphome"))
        return inputs.get("device_kind")

    def test_an_esphome_is_the_kind_its_name_says(self):
        # Every kind's ESPHome looks the same but for its comment (its name in
        # earlier builds): an S61's was once taken for an S60, and sent the
        # S60's startup firmware.
        self.assertEqual(self.adopt_esphome("esphome abcdef", "Sonoff-S61"), "sonoff-s61")
        self.assertEqual(self.adopt_esphome("esphome 123456", "Sonoff-S60"), "sonoff-s60")
        self.assertEqual(self.adopt_esphome("Kitchen", "Sonoff-S61"), "sonoff-s61")   # renamed
        self.assertEqual(self.adopt_esphome("Sonoff S60 123456", ""), "sonoff-s60")   # released before the comment
        self.assertIsNone(self.adopt_esphome("esphome abcdef"))
        self.assertIsNone(self.adopt_esphome(None))


class FirmwareSearchCase(unittest.TestCase):
    """find_current_firmware with the device and the network stood in for."""

    def setUp(self):
        self.private = mock.Mock()
        for patch in (mock.patch.object(output, "say"), mock.patch.object(output, "tell"),
                      mock.patch.object(prompts, "section"),
                      mock.patch.object(output, "transient_begin"),
                      mock.patch.object(output, "transient_end"),
                      mock.patch.object(output, "private", self.private),
                      mock.patch.object(prompts, "UNATTENDED", False),
                      mock.patch.object(prompts, "answered_unattended"),
                      mock.patch.object(source, "adopt_device"),
                      mock.patch.object(source, "converted_here", return_value=False),
                      mock.patch.object(records, "DEVICES", Path("/nonexistent/devices")),
                      mock.patch.object(hostsys, "local_ip_towards", return_value="192.0.2.110"),
                      mock.patch.object(hostsys, "mac_of", return_value="12:34:56:ab:cd:ef"),
                      # A question no test expects fails it, rather than waiting on a keyboard.
                      mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked")),
                      mock.patch.object(prompts, "ask_secret", side_effect=AssertionError("asked"))):
            patch.start()
            self.addCleanup(patch.stop)
        # The device's Tasmota wants this password, and none other - until it is
        # cleared on the device (None).
        self.wanted = "s3cret-71"

        def fake_device(host, user=None, password=None, timeout=10.0):
            return mock.Mock(host=host, wants_password=mock.Mock(
                side_effect=lambda *_: self.wanted is not None and password != self.wanted))
        patch = mock.patch.object(device, "Device", side_effect=fake_device)
        patch.start()
        self.addCleanup(patch.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        done = inputs.pop("done", ())
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: step in done)
        return SimpleNamespace(state=state, web_password=None, esphome_login=None)


class RestartingDeviceTests(FirmwareSearchCase):
    """A device given by address that does not answer yet: waited for if this
    computer converted it, never taken for one on Sonoff's firmware."""

    def setUp(self):
        super().setUp()
        for owner, name, value in ((detect, "answers_as_tasmota", False), (source.time, "sleep", None)):
            patch = mock.patch.object(owner, name, return_value=value)
            patch.start()
            self.addCleanup(patch.stop)

    def find(self, answers, *, known=True, mac="12:34:56:ab:cd:ef", clock=None):
        macs = {"12:34:56:ab:cd:ef"} if known else set()
        with mock.patch.object(source, "known_macs", return_value=macs), \
                mock.patch.object(hostsys, "mac_of", return_value=mac), \
                mock.patch.object(detect, "identify", side_effect=answers) as asked, \
                mock.patch.object(source.time, "monotonic", side_effect=clock or (lambda: 0.0)):
            return source.find_current_firmware(self.ctx(device_ip="192.0.2.137")), asked.call_count

    def test_a_device_it_converted_is_waited_for(self):
        self.assertEqual(self.find([None, None, "esphome"], clock=lambda: 1000.0), ("esphome", 3))
        self.assertEqual(source.time.sleep.call_args_list, [mock.call(5)] * 2)   # a pause between looks

    def test_with_no_mac_to_go_on_any_conversion_here_is_reason_to_wait(self):
        source.converted_here.return_value = True
        self.assertEqual(self.find([None, "esphome"], known=False, mac=None), ("esphome", 2))

    def test_a_device_it_never_converted_is_not(self):
        self.assertEqual(self.find([None], known=False), ("sonoff", 1))

    def test_it_stops_waiting_after_the_grace_and_goes_to_sonoffs_route(self):
        times = iter([0.0, 0.0, source.RESTART_GRACE + 1])
        self.assertEqual(self.find([None, None], clock=lambda: next(times)), ("sonoff", 2))

    def test_a_tasmota_without_its_template_answers_so_is_not_waited_for(self):
        detect.answers_as_tasmota.return_value = True
        with mock.patch.object(detect, "identify", return_value=None) as asked:
            self.assertIsNone(source.identify_once_restarted(self.ctx(), "192.0.2.137"))
        asked.assert_called_once()


class TasmotaWebPasswordTests(FirmwareSearchCase):
    """A device on Tasmota with a web password: asked for it, before anything
    needs it, where someone can type it; stopped, saying what to give, where
    nobody can.  Each try is checked on the device."""

    def test_a_device_given_by_address_is_asked_for_it_then_found(self):
        ctx = self.ctx(device_ip="192.0.2.137")
        answers = {None: "locked", "s3cret-71": "tasmota"}
        with mock.patch.object(detect, "identify",
                               side_effect=lambda host, password=None, **_: answers[password]), \
                mock.patch.object(prompts, "ask_choice", return_value=0) as menu, \
                mock.patch.object(prompts, "ask_secret", side_effect=["not-it-9", "s3cret-71"]) as asked:
            self.assertEqual(source.find_current_firmware(ctx), "tasmota")
        self.assertEqual(asked.call_count, 2)  # the wrong one refused on the device
        self.assertEqual(menu.call_args.args[1][0], "Type its web password")
        self.assertIn("I have removed it", menu.call_args.args[1][1])
        self.assertEqual(ctx.web_password, "s3cret-71")
        self.private.assert_any_call("s3cret-71", "tasmota-password")  # kept out of the log

    def test_unattended_it_stops_saying_what_to_give(self):
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(detect, "identify", return_value="locked"), \
                mock.patch.object(prompts, "ask_secret") as asked:
            with self.assertRaisesRegex(errors.Abort, "web password.*--web-password.*AUTOFLASH_TASMOTA_PASSWORD"):
                source.find_current_firmware(self.ctx(device_ip="192.0.2.137"))
        asked.assert_not_called()

    def test_asked_again_until_it_takes_one_or_the_run_is_stopped(self):
        with mock.patch.object(detect, "identify", return_value="locked"), \
                mock.patch.object(prompts, "ask_choice", side_effect=[0, 0, prompts.QuitRun("quit")]), \
                mock.patch.object(prompts, "ask_secret", return_value="not-it-9") as asked:
            with self.assertRaises(prompts.QuitRun):
                source.find_current_firmware(self.ctx(device_ip="192.0.2.137"))
        self.assertEqual(asked.call_count, 2)

    def test_trying_again_once_it_is_cleared_goes_on_without_one(self):
        ctx = self.ctx(device_ip="192.0.2.137")

        choices = []

        def try_again(*_):
            choices.append(1)
            if len(choices) == 2:
                self.wanted = None  # cleared on the device before choosing again
            return 1
        answers = {None: "locked", "s3cret-71": "tasmota"}
        with mock.patch.object(detect, "identify",
                               side_effect=lambda host, password=None, **_: answers[password]
                               if self.wanted else "tasmota"), \
                mock.patch.object(prompts, "ask_choice", side_effect=try_again), \
                mock.patch.object(prompts, "ask_secret") as asked:
            self.assertEqual(source.find_current_firmware(ctx), "tasmota")
        asked.assert_not_called()
        self.assertIsNone(ctx.web_password)
        output.tell.assert_any_call("    Tasmota there still has a web password.")  # tried too soon

    def test_found_on_the_network_it_is_offered_and_asked_for_once_chosen(self):
        locked = detect.Found("192.0.2.42", "locked", "12:34:56:ab:cd:ef")
        with mock.patch.object(detect, "converted_devices", return_value=[locked]), \
                mock.patch.object(detect, "identify", return_value="tasmota") as identify, \
                mock.patch.object(prompts, "ask_choice", side_effect=[0, 0]) as menu, \
                mock.patch.object(prompts, "ask_secret", return_value="s3cret-71"):
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        self.assertIn("web password", menu.call_args_list[0].args[1][0])  # the device, then its password
        identify.assert_called_with("192.0.2.42", password="s3cret-71", esphome_login=None)
        chosen = source.adopt_device.call_args.args[1]
        self.assertEqual((chosen.host, chosen.firmware), ("192.0.2.42", "tasmota"))

    def test_unattended_one_found_locked_is_left_out_and_named(self):
        found = [detect.Found("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef", "tasmota-ABCDEF"),
                 detect.Found("192.0.2.42", "locked", "aa:bb:cc:dd:ee:ff")]
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(detect, "converted_devices", return_value=found), \
                mock.patch.object(prompts, "ask_choice") as menu:
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        menu.assert_not_called()  # the one S60 that answered is the device, as before
        self.assertEqual(source.adopt_device.call_args.args[1].host, "192.0.2.137")
        told = " ".join(str(call.args[0]) for call in output.tell.call_args_list)
        self.assertIn("192.0.2.42", told)
        self.assertIn("--web-password", told)

    def test_unattended_a_device_this_computer_converted_found_locked_stops_the_run(self):
        # Gone on to Sonoff's route instead, the run would fail later, and less clearly.
        locked = [detect.Found("192.0.2.42", "locked", "12:34:56:ab:cd:ef")]
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "12-34-56-ab-cd-ef").mkdir()
            with mock.patch.object(prompts, "UNATTENDED", True), \
                    mock.patch.object(records, "DEVICES", Path(directory)), \
                    mock.patch.object(detect, "converted_devices", return_value=locked):
                with self.assertRaisesRegex(errors.Abort, "192.0.2.42 has a web password.*--web-password"):
                    source.find_current_firmware(self.ctx())
            # One it does not know is only named: the device may be on Sonoff's firmware still.
            with mock.patch.object(prompts, "UNATTENDED", True), \
                    mock.patch.object(records, "DEVICES", Path(directory) / "none"), \
                    mock.patch.object(detect, "converted_devices", return_value=locked):
                self.assertEqual(source.find_current_firmware(self.ctx()), "sonoff")

    def test_a_run_carried_on_from_tasmota_is_asked_before_any_step(self):
        ctx = self.ctx(device_ip="192.0.2.137", source="tasmota")
        ctx.device = mock.Mock(wants_password=mock.Mock(return_value=True))
        with mock.patch.object(prompts, "ask_choice", return_value=0), \
                mock.patch.object(prompts, "ask_secret", return_value="s3cret-71"):
            unlock.unlock_tasmota(ctx)
        self.assertEqual(ctx.web_password, "s3cret-71")
        # Past L1 the device runs only firmware this tool put on; from ESPHome, none of the device's Tasmota.
        for inputs in ({"source": "tasmota", "done": ("L1",)}, {"source": "esphome"}, {}):
            ctx = self.ctx(device_ip="192.0.2.137", **inputs)
            ctx.device = mock.Mock()
            unlock.unlock_tasmota(ctx)
            ctx.device.wants_password.assert_not_called()


class DiscoverySearchTests(FirmwareSearchCase):
    """The run searches the network its internet goes through first, and reads
    this computer's other networks only if no device is there - once, however
    often it looks again while a device it converted restarts."""

    def found(self):
        return [detect.Found("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef", "tasmota-ABCDEF")]

    def test_the_default_network_first_its_others_not_read_when_the_device_is_there(self):
        searched = []

        def search(near, *, others=None, **_):
            searched.append(near)
            return self.found()
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(detect, "converted_devices", side_effect=search), \
                mock.patch.object(hostsys, "lan_networks", return_value={"10.0.5.9": 24}) as lan:
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        self.assertEqual(searched, ["192.0.2.110"])   # local_ip_towards: the default network
        lan.assert_not_called()

    def test_it_looks_with_the_stages_spinner_turning(self):
        # Seen: "Looking for supported devices on your network..." still for
        # its 5-10 s, as if nothing were happening.
        waits = []

        def search(near, **_):
            waits.append(("searched", near))
            return self.found()

        @contextlib.contextmanager
        def working(text):
            waits.append(text)
            yield
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(output, "working", working), \
                mock.patch.object(detect, "converted_devices", side_effect=search):
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        self.assertEqual(waits, ["Looking for supported devices on your network...", ("searched", "192.0.2.110")])

    def test_the_other_networks_are_read_once_however_often_it_looks(self):
        looks = []

        def search(near, *, others=None, **_):
            looks.append(dict(others()))
            others()
            return self.found() if len(looks) == 3 else []
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(source, "converted_here", return_value=True), \
                mock.patch.object(time, "sleep") as slept, \
                mock.patch.object(detect, "converted_devices", side_effect=search), \
                mock.patch.object(hostsys, "lan_networks", return_value={"10.0.5.9": 24}) as lan:
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        self.assertEqual(looks, [{"10.0.5.9": 24}] * 3)
        self.assertEqual(slept.call_args_list, [mock.call(5)] * 2)   # a pause between looks
        lan.assert_called_once()


class EsphomeWebLoginTests(unittest.TestCase):
    """A device on ESPHome with a web login (web_server's auth:): asked for it,
    user name and password, before anything needs it, where someone can type
    it; stopped, saying what to give, where nobody can.  Each try is checked
    on the device's page."""

    LOGIN = ("admin", "w3b-pass")

    def setUp(self):
        self.private = mock.Mock()
        for patch in (mock.patch.object(output, "say"), mock.patch.object(output, "tell"),
                      mock.patch.object(prompts, "section"),
                      mock.patch.object(output, "transient_begin"),
                      mock.patch.object(output, "transient_end"),
                      mock.patch.object(output, "private", self.private),
                      mock.patch.object(prompts, "UNATTENDED", False),
                      mock.patch.object(prompts, "answered_unattended"),
                      mock.patch.object(source, "adopt_device"),
                      mock.patch.object(source, "converted_here", return_value=False),
                      mock.patch.object(records, "DEVICES", Path("/nonexistent/devices")),
                      mock.patch.object(hostsys, "local_ip_towards", return_value="192.0.2.110"),
                      mock.patch.object(hostsys, "mac_of", return_value="12:34:56:ab:cd:ef"),
                      # A question no test expects fails it, rather than waiting on a keyboard.
                      mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked")),
                      mock.patch.object(prompts, "ask_text", side_effect=AssertionError("asked")),
                      mock.patch.object(prompts, "ask_secret", side_effect=AssertionError("asked"))):
            patch.start()
            self.addCleanup(patch.stop)
        # The device's page wants this login, and none other - until removed (None).
        self.wanted, self.given = self.LOGIN, None
        for patch in (mock.patch.object(esphome, "use_login",
                                        side_effect=lambda host, *login: setattr(self, "given", login)),
                      mock.patch.object(esphome, "page",
                                        side_effect=lambda host, *_: "login" if self.wanted and
                                        self.given != self.wanted else "esphome")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: False)
        return SimpleNamespace(state=state, web_password=None, esphome_login=None)

    def identify(self, host, password=None, esphome_login=None, **_):
        return "esphome" if not self.wanted or esphome_login == self.wanted else "esphome-login"

    def test_a_device_given_by_address_is_asked_for_it_then_found(self):
        ctx = self.ctx(device_ip="192.0.2.50")
        with mock.patch.object(detect, "identify", side_effect=self.identify), \
                mock.patch.object(prompts, "ask_choice", return_value=0) as menu, \
                mock.patch.object(prompts, "ask_text", return_value="admin"), \
                mock.patch.object(prompts, "ask_secret", side_effect=["not-it-9", "w3b-pass"]) as asked:
            self.assertEqual(source.find_current_firmware(ctx), "esphome")
        self.assertEqual(asked.call_count, 2)  # the wrong one refused on the device
        self.assertEqual(menu.call_args.args[1][0], "Type its web login")
        self.assertEqual(ctx.esphome_login, self.LOGIN)
        self.private.assert_any_call("w3b-pass", "esphome-web-password")  # kept out of the log

    def test_unattended_it_stops_saying_what_to_give(self):
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(detect, "identify", side_effect=self.identify):
            with self.assertRaisesRegex(errors.Abort, "web login: give it with --esphome-web-user.*"
                                        "AUTOFLASH_ESPHOME_WEB_PASSWORD"):
                source.find_current_firmware(self.ctx(device_ip="192.0.2.50"))

    def test_trying_again_once_it_is_removed_goes_on_without_one(self):
        ctx = self.ctx(device_ip="192.0.2.50")
        choices = []

        def try_again(*_):
            choices.append(1)
            if len(choices) == 2:
                self.wanted = None  # removed from its config, and that installed
            return 1
        with mock.patch.object(detect, "identify", side_effect=self.identify), \
                mock.patch.object(prompts, "ask_choice", side_effect=try_again):
            self.assertEqual(source.find_current_firmware(ctx), "esphome")
        self.assertIsNone(ctx.esphome_login)
        output.tell.assert_any_call("    ESPHome there still has a web login.")  # tried too soon

    def test_found_on_the_network_it_is_offered_and_asked_for_once_chosen(self):
        locked = detect.Found("192.0.2.42", "esphome-login", "12:34:56:ab:cd:ef")
        with mock.patch.object(detect, "converted_devices", return_value=[locked]), \
                mock.patch.object(detect, "identify", side_effect=self.identify), \
                mock.patch.object(prompts, "ask_choice", side_effect=[0, 0]) as menu, \
                mock.patch.object(prompts, "ask_text", return_value="admin"), \
                mock.patch.object(prompts, "ask_secret", return_value="w3b-pass"):
            self.assertEqual(source.find_current_firmware(self.ctx()), "esphome")
        self.assertIn("web login", menu.call_args_list[0].args[1][0])  # the device, then its login
        chosen = source.adopt_device.call_args.args[1]
        self.assertEqual((chosen.host, chosen.firmware), ("192.0.2.42", "esphome"))

    def test_unattended_one_found_with_a_login_is_left_out_and_named(self):
        found = [detect.Found("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef", "tasmota-ABCDEF"),
                 detect.Found("192.0.2.42", "esphome-login", "aa:bb:cc:dd:ee:ff")]
        with mock.patch.object(prompts, "UNATTENDED", True), \
                mock.patch.object(detect, "converted_devices", return_value=found):
            self.assertEqual(source.find_current_firmware(self.ctx()), "tasmota")
        told = " ".join(str(call.args[0]) for call in output.tell.call_args_list)
        self.assertIn("the ESPHome at 192.0.2.42", told)
        self.assertIn("--esphome-web-user", told)


class NothingAnsweringTests(unittest.TestCase):
    """A device given by its address with nothing at all there - not on the
    network - stops the run there, saying so, rather than being taken for one
    on Sonoff's and sent to eWeLink (seen on a real device switched off,
    2026-11-07)."""

    def run_on(self, mac, known):
        with tempfile.TemporaryDirectory() as tmp:
            state = RunState.open(Path(tmp) / "run")
            state.set_input("device_ip", "192.0.2.52")
            ctx = SimpleNamespace(state=state)
            with mock.patch.object(source, "identify_once_restarted", return_value=None), \
                    mock.patch.object(source.hostsys, "mac_of", return_value=mac), \
                    mock.patch.object(source.detect, "answers_as_tasmota", return_value=False), \
                    mock.patch.object(source, "known_macs", return_value=known):
                return source.find_current_firmware(ctx)

    def test_nothing_at_the_address(self):
        with self.assertRaisesRegex(routes.Abort, "nothing answers at 192.0.2.52"):
            self.run_on(None, set())

    def test_one_on_the_network_that_answers_nothing_is_still_asked_of_ewelink(self):
        # An S61 on Sonoff's firmware answers nothing on the LAN, converted here before or not.
        self.assertEqual(self.run_on("12:34:56:ab:cd:01", set()), "sonoff")
        self.assertEqual(self.run_on("12:34:56:ab:cd:ef", {"12:34:56:ab:cd:ef"}), "sonoff")



class FoundByAddressTests(FirmwareSearchCase):
    def test_a_converted_device_given_by_address_is_the_runs(self):
        with mock.patch.object(detect, "identify", return_value="tasmota"), \
                mock.patch.object(espressif_source, "past_the_commit", side_effect=lambda ctx, running: running):
            self.assertEqual(source.find_current_firmware(self.ctx(device_ip="192.0.2.137")), "tasmota")
        found = source.adopt_device.call_args.args[1]
        self.assertEqual((found.host, found.firmware, found.mac), ("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef"))


class FoundOnTheNetworkTests(FirmwareSearchCase):
    def search(self, *found, choice=None):
        asked = mock.Mock(return_value=choice)
        with mock.patch.object(detect, "converted_devices", return_value=list(found)), \
                mock.patch.object(prompts, "ask_choice", asked), \
                mock.patch.object(espressif_source, "esphome_safe_mode", side_effect=lambda ctx, running: "esphome") \
                as self.safe_mode, \
                mock.patch.object(espressif_source, "past_the_commit", side_effect=lambda ctx, running: running):
            return source.find_current_firmware(self.ctx()), asked.call_args

    def test_attended_one_found_is_still_asked_about(self):
        # It may be another device: the one the run is for may be on Sonoff's firmware.
        tasmota = detect.Found("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef", "tasmota-ABCDEF")
        running, asked = self.search(tasmota, choice=1)
        self.assertEqual(running, "sonoff")
        self.assertEqual(len(asked.args[1]), 2)
        source.adopt_device.assert_not_called()

    def test_the_mover_found_is_checked_for_esphomes_safe_mode(self):
        mover = detect.Found("192.0.2.137", "midway", "12:34:56:ab:cd:ef", "the mover")
        self.assertEqual(self.search(mover, choice=0)[0], "esphome")
        startup = detect.Found("192.0.2.137", "midway", "12:34:56:ab:cd:ef", "the startup firmware")
        self.assertEqual(self.search(startup, choice=0)[0], "midway")
        self.safe_mode.assert_not_called()
        hub = detect.Found("192.0.2.137", "tasmota", "12:34:56:ab:cd:ef", "the mover")
        self.assertEqual(self.search(hub, choice=0)[0], "tasmota")
        self.safe_mode.assert_not_called()

    def test_an_installer_no_conversion_left_goes_on_from_sonoffs_firmware(self):
        from tools.autoflash.routes.espressif import installer
        bridge = detect.Found("192.0.2.137", "installer", "12:34:56:ab:cd:ef", "the installer")
        with mock.patch.object(source, "last_run", return_value="readout"), \
                mock.patch.object(installer, "restart_into_sonoff", return_value=True) as restarted:
            self.assertEqual(self.search(bridge, choice=0)[0], "sonoff")
        restarted.assert_called_once_with(mock.ANY, "192.0.2.137")
        source.adopt_device.assert_not_called()


class ReadoutFirmwareTests(FirmwareSearchCase):
    """A Beken device a readout stopped part way left on its readout firmware."""

    def test_given_by_address_it_is_taken_as_that(self):
        with mock.patch.object(detect, "identify", return_value="readout"):
            self.assertEqual(source.find_current_firmware(self.ctx(device_ip="192.0.2.137")), "readout")
        self.assertEqual(source.adopt_device.call_args.args[1].firmware, "readout")

    def test_never_taken_for_tasmota_without_its_template(self):
        # As a fault run found it: an OpenBeken not told apart, at a MAC this
        # computer knows, answering Tasmota's commands - sent the Espressif way.
        tasmota = mock.Mock(return_value=mock.Mock(command=mock.Mock(return_value={"Status": {}})))
        with mock.patch.object(detect, "identify", return_value=None), \
                mock.patch.object(detect, "Device", tasmota), \
                mock.patch.object(detect.openbeken, "is_openbeken", return_value=True), \
                mock.patch.object(source, "known_macs", return_value={"12:34:56:ab:cd:ef"}), \
                mock.patch.object(source, "RESTART_GRACE", 0.0):   # not waited for as restarting
            self.assertNotEqual(source.find_current_firmware(self.ctx(device_ip="192.0.2.137")), "untemplated")


class InstallerFoundTests(FirmwareSearchCase):
    """This tool's installer (the bridge) found running is carried on as a
    conversion only where one left it: this computer's unfinished conversion,
    or no Sonoff firmware left to start, which only a conversion's upload takes.
    Any other - a readout's, the same program, or one no run here left - is
    restarted into Sonoff's firmware and the run goes on from there; where it
    will not restart, nothing is done."""

    def find(self, last, fallback=True, restarts=True):
        from tools.autoflash.routes.espressif import installer
        with mock.patch.object(detect, "identify", return_value="installer"), \
                mock.patch.object(installer, "has_fallback", return_value=fallback), \
                mock.patch.object(installer, "restart_into_sonoff", return_value=restarts) as self.restarted, \
                mock.patch.object(espressif_source, "past_the_commit", side_effect=lambda ctx, running: running), \
                mock.patch.object(source, "last_run", return_value=last) as self.asked:
            return source.find_current_firmware(self.ctx(device_ip="192.0.2.137"))

    def test_left_by_a_readout_it_is_restarted_into_sonoff(self):
        self.assertEqual(self.find("readout"), "sonoff")
        self.restarted.assert_called_once_with(mock.ANY, "192.0.2.137")
        self.assertEqual(self.asked.call_args.args[1], "12:34:56:ab:cd:ef")

    def test_left_by_a_conversion_here_it_carries_on(self):
        self.assertEqual(self.find("conversion"), "installer")
        self.restarted.assert_not_called()

    def test_with_no_run_here_and_sonoff_there_it_is_restarted(self):
        # A readout run on another computer, say: never taken for a conversion.
        self.assertEqual(self.find(None, fallback=True), "sonoff")
        self.restarted.assert_called_once()

    def test_with_no_run_here_and_no_sonoff_left_it_carries_on(self):
        # An upload into Sonoff's slot cut off: only a conversion makes one.
        self.assertEqual(self.find(None, fallback=False), "installer")
        self.restarted.assert_not_called()

    def test_one_that_will_not_restart_has_nothing_done(self):
        with self.assertRaisesRegex(errors.Abort, "left by a readout stopped part way, and would not restart: "
                                                  "switch it off and on"):
            self.find("readout", restarts=False)
        source.adopt_device.assert_not_called()

    def test_the_last_run_on_that_device_says_what_it_was(self):
        from tools.autoflash import readout, routes
        with tempfile.TemporaryDirectory() as tmp:
            runs_made = []

            def make(name, mac, **inputs):
                path = Path(tmp) / name
                state = RunState.open(path)
                for key, value in inputs.items():
                    state.set_input(key, value)
                runs_made.append((path, mac))
            make("1", "12:34:56:ab:cd:ef", **{readout.STATE_KEY: "sent"})
            make("2", "aa:bb:cc:dd:ee:ff", source="sonoff", firmware="tasmota")
            make("4", "aa:bb:cc:dd:ee:01", firmware="tasmota")   # its firmware chosen, its source not yet found
            make("3", "12:34:56:ab:cd:ef")   # stopped before it knew where it went: passed over
            here = SimpleNamespace(dir=Path(tmp) / "now")
            macs = dict(runs_made)
            with mock.patch.object(runs, "run_directories", return_value=[path for path, _ in runs_made]), \
                    mock.patch.object(records, "mac_of_run", side_effect=lambda path: macs.get(path)):
                self.assertEqual(source.last_run(here, "12:34:56:ab:cd:ef"), "readout")
                self.assertEqual(source.last_run(here, "aa:bb:cc:dd:ee:ff"), "conversion")
                self.assertEqual(source.last_run(here, "aa:bb:cc:dd:ee:01"), "conversion")
                with mock.patch.object(routes, "steps_of", return_value=[("A1", "", None), ("H1", "", None)]), \
                        mock.patch.object(RunState, "is_done", side_effect=lambda step: step == "H1"):
                    self.assertIsNone(source.last_run(here, "aa:bb:cc:dd:ee:ff"))   # finished
                self.assertIsNone(source.last_run(here, "99:99:99:99:99:99"))


if __name__ == "__main__":
    unittest.main()
