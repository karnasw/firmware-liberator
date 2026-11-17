"""A run's record on disk (tools/autoflash/runs.py): filed under its device's
MAC, what it ran from, and its secrets removed."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import cli, context, credentials, errors, output, records, report, routes, runs
from tools.autoflash.routes.espressif import bundle
from tools.autoflash.state import RunState


class FromTasmotasSafebootTests(unittest.TestCase):
    """A run from Tasmota's safeboot is a run from Tasmota once T1 has put it
    back (seen on the emulator: its report stopped at KeyError: 'safeboot')."""

    def state(self, done=()):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        state = RunState.open(Path(directory.name))
        state.set_input("source", "safeboot")
        for step in done:
            state.mark(step, "done")
        return state

    def test_every_starting_point_is_named_in_the_report(self):
        self.assertEqual(set(report.CONVERTED_FROM), {s for f in routes.FAMILIES.values() for s in f.SOURCES})

    def test_tasmota_runs_from_t1_until_it_is_left(self):
        self.assertFalse(runs.device_runs_tasmota(self.state()))           # its safeboot
        self.assertTrue(runs.device_runs_tasmota(self.state(["T1"])))      # Tasmota back
        self.assertFalse(runs.device_runs_tasmota(self.state(["T1", "L1"])))  # on the mover

    def test_a_run_from_esphome_is_on_tasmota_from_x2_until_it_is_left(self):
        def runs_tasmota(*done):
            state = self.state(done)
            state.set_input("source", "esphome")
            return runs.device_runs_tasmota(state)
        self.assertIs(runs_tasmota(), False)              # ESPHome still
        self.assertIs(runs_tasmota("X2"), True)           # the interim Tasmota
        self.assertIs(runs_tasmota("X2", "I1"), False)    # past it
        self.assertIs(runs_tasmota("X2", "R1"), False)


class RunFilingTests(unittest.TestCase):
    """A run moves under its device's MAC once found; older runs are adopted."""

    def setUp(self):
        self.devices = records
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.captures = Path(directory.name)
        for patch in (mock.patch.object(records, "CAPTURES", self.captures),
                      mock.patch.object(records, "DEVICES", self.captures / "devices"),
                      mock.patch.object(runs, "CURRENT_POINTER", self.captures / ".current"),
                      mock.patch.object(output, "say")):
            patch.start()
            self.addCleanup(patch.stop)

    def test_a_new_run_waits_unidentified_then_moves_under_the_mac(self):
        from types import SimpleNamespace
        name = runs.new_run_name(None)
        self.assertTrue(name.startswith("devices/unidentified/runs/"))
        ctx = context.Context(SimpleNamespace(
            run=name, manifest=bundle.DEFAULT_MANIFEST, dry_run=False, device_ip=None,
            email=None, web_user=None, web_password=None, esphome_ota_password=None,
            esphome_web_user=None, esphome_web_password=None, esphome_api_key=None))
        ctx.state.set_input("device_ip", "10.0.0.5")
        ctx.file_under_device("58:CF:79:01:02:03")
        self.assertEqual(ctx.dir.parent, self.captures / "devices/58-cf-79-01-02-03/runs")
        self.assertEqual(ctx.state.input("device_ip"), "10.0.0.5")
        ctx.state.set_input("device_mac", "58:cf:79:01:02:03")  # saved where it now is
        self.assertTrue((ctx.dir / "autoflash-state.json").is_file())
        self.assertFalse((self.captures / name).exists())
        self.assertEqual(runs.read_current(), ctx.run)

    def test_the_current_run_and_settings_are_kept_on_a_computer_with_no_folder_for_them_yet(self):
        with mock.patch.object(runs, "CURRENT_POINTER", self.captures / "new" / ".current"), \
                mock.patch.object(runs, "SETTINGS_PATH", self.captures / "other" / "settings.json"):
            for _ in range(2):   # into a new folder, then into the one there is
                runs.remember_current("devices/unidentified/runs/20261013-031500")
                self.assertEqual(runs.read_current(), "devices/unidentified/runs/20261013-031500")
                runs.save_settings({"language": "en"})
                self.assertEqual(runs.load_settings(), {"language": "en"})

    def test_the_last_run_as_a_release_before_2_4_0_wrote_it_down_is_found(self):
        (self.captures / ".current").write_text("plugs/58-cf-79-01-02-03/runs/20261013-031500\n", encoding="utf-8")
        self.assertEqual(runs.read_current(), "devices/58-cf-79-01-02-03/runs/20261013-031500")

    def test_firmware_passwords_come_from_the_flags_then_the_environment(self):
        from types import SimpleNamespace
        name = runs.new_run_name(None)

        def args(**over):
            base = dict(run=name, manifest=bundle.DEFAULT_MANIFEST, dry_run=False,
                        device_ip=None, email=None, web_user=None, web_password=None,
                        esphome_ota_password=None, esphome_web_user=None, esphome_web_password=None,
                        esphome_api_key=None)
            base.update(over)
            return SimpleNamespace(**base)
        key = base64.b64encode(bytes(range(32))).decode()
        # The flags win, each kept out of the log under its label.
        private = mock.Mock()
        with mock.patch.object(output, "private", private):
            ctx = context.Context(args(web_password="tas-pw", esphome_ota_password="esp-pw",
                                              esphome_web_user="admin", esphome_web_password="web-pw",
                                              esphome_api_key=key))
        self.assertEqual((ctx.web_password, ctx.esphome_password), ("tas-pw", "esp-pw"))
        self.assertEqual((ctx.esphome_login, ctx.esphome_key), (("admin", "web-pw"), bytes(range(32))))
        private.assert_any_call("tas-pw", "tasmota-password")
        private.assert_any_call("esp-pw", "esphome-password")
        private.assert_any_call("web-pw", "esphome-web-password")
        private.assert_any_call(key, "esphome-api-key")
        # With no flag, the environment (credentials) is the fallback.
        with mock.patch.object(credentials, "get",
                               side_effect={"AUTOFLASH_TASMOTA_PASSWORD": "env-tas",
                                            "AUTOFLASH_ESPHOME_OTA_PASSWORD": "env-esp",
                                            "AUTOFLASH_ESPHOME_WEB_USER": "env-admin",
                                            "AUTOFLASH_ESPHOME_WEB_PASSWORD": "env-web",
                                            "AUTOFLASH_ESPHOME_API_KEY": key}.get):
            ctx = context.Context(args())
        self.assertEqual((ctx.web_password, ctx.esphome_password), ("env-tas", "env-esp"))
        self.assertEqual((ctx.esphome_login, ctx.esphome_key), (("env-admin", "env-web"), bytes(range(32))))
        # A web login is both, or none; a key is one, or the run is refused at once.
        self.assertIsNone(context.Context(args(esphome_web_password="web-pw")).esphome_login)
        with self.assertRaisesRegex(errors.Abort, "API encryption key given.*is not one.*base64"):
            context.Context(args(esphome_api_key="not-a-key"))

    def test_an_account_given_in_advance_that_cannot_be_one_stops_the_run_at_once(self):
        from types import SimpleNamespace
        name = runs.new_run_name(None)

        def args(email=None):
            return SimpleNamespace(run=name, manifest=bundle.DEFAULT_MANIFEST, dry_run=False, device_ip=None,
                                   email=email, web_user=None, web_password=None, esphome_ota_password=None,
                                   esphome_web_user=None, esphome_web_password=None, esphome_api_key=None)
        for email, given in (("07700 900123", None), (None, "test@test")):
            with self.subTest(email=email, given=given), \
                    mock.patch.object(credentials, "get", {"EWELINK_EMAIL": given}.get), \
                    self.assertRaisesRegex(errors.Abort, "neither an email address nor a phone number"):
                context.Context(args(email))
        with mock.patch.object(credentials, "get", {"EWELINK_EMAIL": "+44-7700-900123"}.get):
            self.assertEqual(context.Context(args())._given_email, "+447700900123")
        with mock.patch.object(runs, "SETTINGS_PATH", self.captures / "settings.json"):
            self.assertEqual(context.Context(args("me@example.com")).state.input("email"), "me@example.com")

    def test_older_runs_with_a_mac_are_adopted(self):
        old = RunState.open(self.captures / "conversion-20261011-1431")
        old.set_input("device_mac", "58:cf:79:01:02:03")
        RunState.open(self.captures / "conversion-unknown").save()
        runs.remember_current("conversion-20261011-1431")
        cli.adopt_older_runs()
        moved = self.captures / "devices/58-cf-79-01-02-03/runs/conversion-20261011-1431"
        self.assertTrue((moved / "autoflash-state.json").is_file())
        self.assertTrue((self.captures / "conversion-unknown").is_dir())
        self.assertEqual(runs.read_current(), runs.run_name(moved))
        self.assertEqual(len(runs.run_directories()), 2)


class RunSecretsTests(unittest.TestCase):
    def test_only_the_secret_files_are_removed(self):
        with tempfile.TemporaryDirectory(dir=runs.ROOT / "tests") as directory:
            run = Path(directory)
            (run / "live").mkdir()
            for name in runs.SECRET_FILES:
                (run / name).write_bytes(b"secret")
            kept = [run / "autoflash-state.json", run / "live" / "stage-report.json"]
            for path in kept:
                path.write_text("{}")
            removed = runs.forget_run_secrets(run)
            self.assertEqual(len(removed), len(runs.SECRET_FILES))
            for name in runs.SECRET_FILES:
                self.assertFalse((run / name).exists(), name)
            for path in kept:
                self.assertTrue(path.exists(), path.name)


class FileDeviceTests(unittest.TestCase):
    def test_the_record_keeps_only_a_product_model_ewelink_gave(self):
        # Where it gave none, AccountDevice.model is the kind's short name, for
        # the screen: kept, it would be sent as the device's model.
        from tools.autoflash.account import AccountDevice
        mac = "12:34:56:ab:cd:ef"
        ctx = mock.Mock(spec=[])
        for model, kept in (("S60", None), ("S60TPG", "S60TPG")):
            device = AccountDevice("1001234abc", "Kitchen", model, True, mac, None, "sonoff-s60")
            with mock.patch.object(runs, "hold_device"), mock.patch.object(records, "find_backup", return_value=None), \
                    mock.patch.object(records, "remember_device") as remember:
                runs.file_device(ctx, mac, device)
            self.assertEqual(remember.call_args.kwargs["model"], kept)


if __name__ == "__main__":
    unittest.main()
