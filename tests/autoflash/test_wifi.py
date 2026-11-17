"""Where a run's Wi-Fi details come from (tools/autoflash/wifi.py): the device,
or its backup."""
import base64
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import context, credentials, esphome, output, records, prompts, wifi
from tools.espressif import inspect_stock_nvs
from tools.espressif.safeboot import prepare_imprinted_bundle


class WifiFromDeviceTests(unittest.TestCase):
    """The Wi-Fi details come off the device, so nobody types them."""

    def setUp(self):
        from tools.autoflash import tasmota_settings as ts
        from tests.autoflash.test_tasmota_settings import default_record
        self.ts = ts
        self.record = ts.fill(ts.make_template(default_record()), "Home-2G", "a good password")

    def ctx(self, *, connected="Home-2G", backup=None, fail=None, forced=False):
        from types import SimpleNamespace
        from tools.autoflash.device import DeviceError
        from tools.autoflash import tasmota_settings as ts

        def command(name, **kwargs):
            if fail == "command":
                raise DeviceError("no answer")
            return {"StatusSTS": {"Wifi": {"SSId": connected} if connected else {}}}

        def download_settings(**kwargs):
            if fail == "download":
                raise OSError("refused")
            return ts.encode_backup(self.record if backup is None else backup)

        device = SimpleNamespace(command=command, download_settings=download_settings)
        return SimpleNamespace(device=device, args=SimpleNamespace(wifi=forced),
                               state=SimpleNamespace(input=lambda name: None))

    def read(self, **kwargs):
        with mock.patch.object(output, "say"):
            return wifi.read_wifi_from_device(self.ctx(**kwargs))

    def test_the_connected_network_and_its_password_are_read(self):
        self.assertEqual(self.read(), ("Home-2G", "a good password"))

    def test_a_device_on_another_network_than_its_first_slot_is_not_used(self):
        # Tasmota holds two networks; only the one it is actually on is proven.
        self.assertIsNone(self.read(connected="Guest"))

    def test_a_device_that_will_not_say_or_will_not_give_its_backup_falls_back(self):
        for kwargs in ({"connected": None}, {"fail": "command"}, {"fail": "download"},
                       {"backup": b"not a settings record"}):
            self.assertIsNone(self.read(**kwargs), kwargs)

    def test_an_open_network_is_read_with_an_empty_password(self):
        # Connected with its first slot's details and that slot has no
        # password: the network is open, and there is nothing to type.
        self.record = self.ts.fill(self.ts.make_template(self.record), "Home-2G", "")
        self.assertEqual(self.read(), ("Home-2G", ""))

    def test_an_open_network_on_another_slot_is_still_not_used(self):
        self.record = self.ts.fill(self.ts.make_template(self.record), "Home-2G", "")
        self.assertIsNone(self.read(connected="Guest"))

    def test_the_flag_forces_typing_them_instead(self):
        ctx = self.ctx(forced=True)
        ctx._wifi = None
        with mock.patch.object(wifi, "read_wifi_from_device") as read, \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "say"):
            try:
                context.Context.wifi(ctx)
            except Exception:
                pass  # it goes on to the asking path, which needs a real bundle
        read.assert_not_called()

    def test_a_device_the_network_keeps_from_answering_is_asked_again(self):
        # Seen in the fault suite: a Wi-Fi blip as G1 began, and the run
        # asked the person to type the Wi-Fi details the device holds.
        import urllib.error
        for drops, expected in ((1, ("Home-2G", "a good password")),
                                (wifi.WIFI_READ_TRIES - 1, ("Home-2G", "a good password")),   # the last try
                                (wifi.WIFI_READ_TRIES, None)):
            ctx = self.ctx()
            answers = ctx.device.command
            left = [drops]

            def command(name, **kwargs):
                if left[0]:
                    left[0] -= 1
                    raise urllib.error.URLError("timed out")
                return answers(name, **kwargs)

            ctx.device.command = command
            ctx.device.wait_reachable = mock.Mock()
            with mock.patch.object(output, "say"):
                self.assertEqual(wifi.read_wifi_from_device(ctx), expected, drops)
            self.assertEqual(ctx.device.wait_reachable.call_count, min(drops, wifi.WIFI_READ_TRIES - 1))

    def test_what_is_read_is_kept_out_of_the_log(self):
        with mock.patch.object(output, "private") as private:
            self.assertEqual(self.read(), ("Home-2G", "a good password"))
        self.assertEqual({c.args for c in private.call_args_list},
                         {("Home-2G", "wifi-name"), ("a good password", "wifi-password")})
        # On another network than its first slot's, both names are said: both kept out.
        with mock.patch.object(output, "private") as private:
            self.assertIsNone(self.read(connected="Guest"))
        self.assertEqual({c.args for c in private.call_args_list},
                         {("Guest", "wifi-name"), ("Home-2G", "wifi-name"), ("a good password", "wifi-password")})

    def test_each_reason_for_not_reading_them_is_said(self):
        for kwargs, words in (({"connected": None}, "did not say which network"),
                              ({"fail": "command"}, "did not answer as Tasmota"),
                              ({"connected": "Guest"}, "not the network in its first slot")):
            with mock.patch.object(output, "say") as said:
                self.assertIsNone(wifi.read_wifi_from_device(self.ctx(**kwargs)))
            self.assertIn(words, " ".join(str(c.args[0]) for c in said.call_args_list), kwargs)


class BackupWifiTests(unittest.TestCase):
    """From a device whose firmware cannot say its Wi-Fi password (ESPHome, the
    mover), the details come from its Sonoff backup on this computer - when
    the bundle, or the network ESPHome is on, shows they are the device's still."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        (Path(self.tmp.name) / "flash.bin").write_bytes(b"\xff" * 0x20000)
        backup = mock.Mock(directory=Path(self.tmp.name))
        for patch in (mock.patch.object(records, "find_backup", return_value=backup),
                      mock.patch.object(wifi, "sonoff_backup_wifi", return_value=("Home-2G", "pw")),
                      mock.patch.object(output, "say"), mock.patch.object(output, "tell")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, **inputs):
        from types import SimpleNamespace
        inputs = {"device_mac": "12:34:56:ab:cd:ef", "device_ip": "192.0.2.50", **inputs}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__, is_done=lambda step: False)
        return SimpleNamespace(state=state, manifest_path=Path("unused"), args=SimpleNamespace(wifi=False),
                               _wifi=None, _recovery=None, esphome_key=None, esphome_api_locked=False)

    def found(self, *, pinned=None, rebuilds=True, joined=None):
        recovery = mock.Mock(return_value=b"image") if rebuilds else mock.Mock(
            side_effect=prepare_imprinted_bundle.BundleError("not the pinned password"))
        with mock.patch.object(wifi, "bundle_ssid", return_value=pinned), \
                mock.patch.object(wifi, "recovery_image", recovery), \
                mock.patch.object(esphome, "api_wifi",
                                  return_value={"signal": -50, "ssid": joined} if joined else None) as api:
            return wifi.wifi_from_backup(self.ctx()), api

    def test_the_bundle_set_up_for_them_proves_them(self):
        details, api = self.found(pinned="Home-2G")
        self.assertEqual(details, ("Home-2G", "pw"))
        api.assert_not_called()

    def test_an_older_password_than_the_bundles_is_not_used(self):
        self.assertIsNone(self.found(pinned="Home-2G", rebuilds=False)[0])

    def test_the_network_esphome_is_on_proves_them(self):
        self.assertEqual(self.found(joined="Home-2G")[0], ("Home-2G", "pw"))

    def test_another_network_or_no_proof_is_not_used(self):
        self.assertIsNone(self.found(joined="Guest")[0])
        self.assertIsNone(self.found()[0])

    def test_what_is_read_from_the_backup_is_kept_out_of_the_log(self):
        with mock.patch.object(output, "private") as private:
            self.found(joined="Guest")
        self.assertEqual(private.call_args_list, [mock.call("Home-2G", "wifi-name"), mock.call("pw", "wifi-password")])

    def test_no_backup_gives_nothing(self):
        with mock.patch.object(records, "find_backup", return_value=None):
            self.assertIsNone(wifi.wifi_from_backup(self.ctx()))

    def test_a_device_on_esphome_has_them_read_before_dotenv_or_asking(self):
        ctx = self.ctx(source="esphome")
        ctx.adopt_wifi = mock.Mock(side_effect=lambda ssid, password: setattr(ctx, "_wifi", (ssid, password)))
        # .env names another network: taken first, it would be the answer.
        with mock.patch.object(wifi, "wifi_from_esphome", return_value=None), \
                mock.patch.object(wifi, "wifi_from_backup", return_value=("Home-2G", "pw")), \
                mock.patch.object(credentials, "get", return_value="Elsewhere"), \
                mock.patch.object(wifi, "read_wifi_from_device") as off_tasmota:
            self.assertEqual(context.Context.wifi(ctx), ("Home-2G", "pw"))
        off_tasmota.assert_not_called()  # a Tasmota is not what runs
        ctx.adopt_wifi.assert_called_once_with("Home-2G", "pw")
        self.assertIs(ctx.state.input("wifi_typed"), False)

    def test_esphomes_own_details_come_before_the_backups(self):
        ctx = self.ctx(source="esphome")
        ctx.adopt_wifi = mock.Mock(side_effect=lambda ssid, password: setattr(ctx, "_wifi", (ssid, password)))
        with mock.patch.object(esphome, "api_wifi_details", return_value=("Home-5", "pw5")), \
                mock.patch.object(wifi, "wifi_from_backup") as backup:
            self.assertEqual(context.Context.wifi(ctx), ("Home-5", "pw5"))
        backup.assert_not_called()

    def test_what_esphome_gives_is_kept_out_of_the_log(self):
        with mock.patch.object(esphome, "api_wifi_details", return_value=("Home-5", "pw5")), \
                mock.patch.object(output, "private") as private:
            self.assertEqual(wifi.wifi_from_esphome(self.ctx()), ("Home-5", "pw5"))
        self.assertEqual(private.call_args_list, [mock.call("Home-5", "wifi-name"), mock.call("pw5", "wifi-password")])
        self.assertIn("'Home-5'", output.say.call_args.args[0])

    def test_an_encrypted_api_is_offered_its_key_before_the_wifi_is_asked(self):
        key = bytes(range(32))

        def details(host, key=None, **_):
            if key is None:
                raise esphome.ApiLocked()
            return ("Home-5", "pw5")
        for choice, typed in ((0, base64.b64encode(key).decode()), (1, None)):
            ctx = self.ctx(source="esphome")
            ctx.adopt_wifi = mock.Mock(side_effect=lambda ssid, password: setattr(ctx, "_wifi", (ssid, password)))
            with mock.patch.object(esphome, "api_wifi_details", side_effect=details), \
                    mock.patch.object(wifi, "wifi_from_backup", return_value=None), \
                    mock.patch.object(esphome, "api_access",
                                      side_effect=lambda host, given: "" if given == key else "key"), \
                    mock.patch.object(prompts, "UNATTENDED", False), \
                    mock.patch.object(prompts, "section"), mock.patch.object(output, "private"), \
                    mock.patch.object(output, "transient_begin"), \
                    mock.patch.object(output, "transient_end"), \
                    mock.patch.object(context, "bundle_ssid", return_value=None), \
                    mock.patch.object(context, "ask_credentials", return_value=("Typed", "pw")) as typed_wifi, \
                    mock.patch.object(prompts, "ask_choice", return_value=choice) as menu, \
                    mock.patch.object(prompts, "ask_secret", return_value=typed):
                got = context.Context.wifi(ctx)
            self.assertEqual(menu.call_args_list[0].args[1],
                             ["Type its API encryption key", "Type the Wi-Fi details instead"])
            if typed:  # ESPHome gives them, its API unlocked
                self.assertEqual((got, ctx.esphome_key), (("Home-5", "pw5"), key))
                typed_wifi.assert_not_called()
            else:  # typed instead
                self.assertEqual((got, ctx.esphome_key), (("Typed", "pw"), None))


class SonoffBackupWifiTests(unittest.TestCase):
    """The Wi-Fi Sonoff's firmware was set up with, as its backup holds it."""

    def read(self, content, *, namespace="frame_params", key="content"):
        from tools.espressif.inspect_stock_nvs import Value
        value = Value(namespace=3, key=key, kind=0x21, chunks={0: content})
        flash = bytes(range(256)) * 0x200
        with mock.patch.object(inspect_stock_nvs, "parse",
                               return_value=({3: namespace}, {(3, key): value})) as parse:
            found = wifi.sonoff_backup_wifi(flash)
        # Its NVS partition, where Sonoff's firmware keeps it.
        self.assertEqual(parse.call_args.args[0], flash[0x9000:0x19000])
        return found

    def test_the_name_and_password_come_from_frame_params(self):
        self.assertEqual(self.read(b'{"ssid":"Home-2G","passwd":"pw","model":"SN-ESP32C3-S60-01"}\x00'),
                         ("Home-2G", "pw"))

    def test_an_open_network_has_no_password(self):
        self.assertEqual(self.read(b'{"ssid":"Home-2G"}\x00\xff\xff'), ("Home-2G", ""))

    def test_no_network_gives_nothing(self):
        self.assertIsNone(self.read(b'{"ssid":"","passwd":""}\x00'))

    def test_only_frame_params_content_is_read(self):
        content = b'{"ssid":"Home-2G","passwd":"pw"}\x00'
        self.assertIsNone(self.read(content, namespace="user_unusual"))
        self.assertIsNone(self.read(content, key="other"))


if __name__ == "__main__":
    unittest.main()
