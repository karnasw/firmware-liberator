"""A run's context (tools/autoflash/context.py): its eWeLink account and a
password given for it, its Wi-Fi typed only as the fallback, and the port
its phases are served on."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tools.autoflash import context, errors, firewall, hostsys, output, prompts, runs, wifi
from tools.autoflash.state import RunState
from tools.espressif.safeboot import prepare_imprinted_bundle


class WifiFallbackTests(unittest.TestCase):
    """Typing the Wi-Fi is the fallback: explained, asked once, and needs no rebuild."""

    def ctx(self, *, found=None, bundle_for=None):
        from types import SimpleNamespace
        ctx = SimpleNamespace(args=SimpleNamespace(wifi=False), manifest_path=Path("unused"),
                              state=SimpleNamespace(set_input=lambda *_: None, input=lambda *_: None,
                                                    is_done=lambda _: False),
                              _wifi=None, _recovery=None, esphome_api_locked=False)
        ctx.remember_wifi = lambda ssid, password, image: context.Context.remember_wifi(
            ctx, ssid, password, image)
        ctx.adopt_wifi = lambda ssid, password: context.Context.adopt_wifi(ctx, ssid, password)
        self.patches = [
            mock.patch.object(wifi, "read_wifi_from_device", return_value=found),
            mock.patch.object(context, "bundle_ssid", return_value=bundle_for),
            mock.patch.object(output, "say"),
        ]
        for patch in self.patches:
            patch.start()
        self.addCleanup(lambda: [patch.stop() for patch in self.patches])
        return ctx

    def test_details_read_off_the_device_are_pinned_without_asking_or_downloading(self):
        ctx = self.ctx(found=("Home-2G", "pw"))
        with mock.patch.object(context, "recovery_image",
                               side_effect=prepare_imprinted_bundle.BundleError("no Wi-Fi details yet")), \
             mock.patch.object(context, "pin_wifi", return_value=b"image") as pin, \
             mock.patch.object(context, "build_bundle") as rebuild, \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(context, "ask_credentials") as asked:
            self.assertEqual(context.Context.wifi(ctx), ("Home-2G", "pw"))
        pin.assert_called_once_with(Path("unused"), "Home-2G", "pw")
        rebuild.assert_not_called()
        asked.assert_not_called()
        shown.assert_not_called()

    def test_details_read_off_the_device_are_not_reported_as_typed(self):
        # A run stopped at the question, then resumed and read off the device,
        # must not keep the earlier "typed" mark.
        ctx = self.ctx(found=("Home-2G", ""))
        recorded = {}
        ctx.state.set_input = lambda key, value: recorded.__setitem__(key, value)
        with mock.patch.object(context, "recovery_image", return_value=b"image"):
            self.assertEqual(context.Context.wifi(ctx), ("Home-2G", ""))
        self.assertIs(recorded.get("wifi_typed"), False)

    def test_when_they_cannot_be_read_the_reason_is_shown_then_both_are_asked(self):
        ctx = self.ctx(found=None, bundle_for=None)
        with mock.patch.object(context, "recovery_image",
                               side_effect=prepare_imprinted_bundle.BundleError("no Wi-Fi details yet")), \
             mock.patch.object(context, "pin_wifi", return_value=b"image"), \
             mock.patch.object(prompts, "section") as shown, \
             mock.patch.object(context, "ask_credentials", return_value=("Home-2G", "pw")) as asked:
            details = context.Context.wifi(ctx, why=["Tasmota is waiting on its setup network."])
        self.assertEqual(details, ("Home-2G", "pw"))
        asked.assert_called_once()
        self.assertIn("Tasmota is waiting on its setup network.", shown.call_args.args[1])

    def test_details_typed_are_kept_out_of_the_log_and_reported_as_typed(self):
        ctx = self.ctx(found=None, bundle_for="Home-2G")
        recorded = {}
        ctx.state.set_input = lambda key, value: recorded.__setitem__(key, value)
        with mock.patch.object(context, "recovery_image", return_value=b"image"), \
             mock.patch.object(prompts, "section"), mock.patch.object(prompts, "ask_secret", return_value="pw"), \
             mock.patch.object(output, "private") as private:
            self.assertEqual(context.Context.wifi(ctx), ("Home-2G", "pw"))
        self.assertIn(mock.call("Home-2G", "wifi-name"), private.call_args_list)
        self.assertIn(mock.call("pw", "wifi-password"), private.call_args_list)
        self.assertIs(recorded.get("wifi_typed"), True)

    def test_wifi_forces_typing_over_details_given_in_advance(self):
        ctx = self.ctx(found=None)
        ctx.args.wifi = True
        given = {"AUTOFLASH_WIFI_SSID": "Given-2G", "AUTOFLASH_WIFI_PASSWORD": "given"}
        with mock.patch.object(context.credentials, "get", side_effect=given.get), \
             mock.patch.object(context, "recovery_image", return_value=b"image"), \
             mock.patch.object(prompts, "section"), \
             mock.patch.object(context, "ask_credentials", return_value=("Home-2G", "pw")) as asked:
            self.assertEqual(context.Context.wifi(ctx), ("Home-2G", "pw"))
        asked.assert_called_once()

    def test_a_beken_devices_are_asked_for_with_no_bundle_or_tasmota(self):
        # Its ESPHome, unable to join with its Sonoff store's details, waits on
        # its own network: nothing on the home network to read, no bundle.
        ctx = self.ctx(found=("Home-2G", "old"))
        ctx.state.input = lambda key: "sonoff-s61" if key == "device_kind" else None
        with mock.patch.object(context, "recovery_image") as image, \
             mock.patch.object(context, "bundle_ssid") as bundled, \
             mock.patch.object(wifi, "wifi_from_backup") as backup, \
             mock.patch.object(prompts, "section"), \
             mock.patch.object(context, "ask_credentials", return_value=("Home-2G", "new")) as asked:
            self.assertEqual(context.Context.wifi(ctx, why=["ESPHome is waiting on its own network."]),
                             ("Home-2G", "new"))
        asked.assert_called_once()
        wifi.read_wifi_from_device.assert_not_called()
        for untouched in (image, bundled, backup):
            untouched.assert_not_called()

    def test_a_beken_devices_given_in_advance_are_taken_as_they_are(self):
        ctx = self.ctx(found=None)
        ctx.state.input = lambda key: "sonoff-s61" if key == "device_kind" else None
        given = {"AUTOFLASH_WIFI_SSID": "Given-2G", "AUTOFLASH_WIFI_PASSWORD": "given"}
        with mock.patch.object(context.credentials, "get", side_effect=given.get), \
             mock.patch.object(context, "recovery_image") as image:
            self.assertEqual(context.Context.wifi(ctx), ("Given-2G", "given"))
        image.assert_not_called()

    def test_a_bundle_the_checkout_cannot_use_is_built_again(self):
        ctx = self.ctx(found=("Home-2G", "pw"))
        with mock.patch.object(context, "recovery_image", side_effect=prepare_imprinted_bundle.BundleError("x")), \
             mock.patch.object(context, "pin_wifi", side_effect=prepare_imprinted_bundle.BundleError("old")), \
             mock.patch.object(context, "build_bundle", return_value=(Path("m"), b"image")) as rebuild:
            context.Context.wifi(ctx)
        rebuild.assert_called_once()


class EwelinkAccountTests(unittest.TestCase):
    """The last eWeLink account is offered, not assumed."""

    def email(self, *, remembered=None, chosen=None, choice=0, typed="new@example.com", given=None):
        from types import SimpleNamespace
        inputs = {"email": chosen} if chosen else {}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get,
                                                    set_input=inputs.__setitem__),
                              remember_email=lambda e: (inputs.__setitem__("email", e),
                                                        setattr(self, "remembered", e)),
                              _given_email=given)
        with mock.patch.object(runs, "load_settings",
                               return_value={"ewelink_email": remembered} if remembered else {}), \
             mock.patch.object(prompts, "ask_choice", return_value=choice) as menu, \
             mock.patch.object(prompts, "ask_text", return_value=typed) as asked:
            return context.Context.email(ctx), menu, asked

    def test_last_time_s_account_is_offered_and_can_be_kept(self):
        email, menu, asked = self.email(remembered="old@example.com", choice=0)
        self.assertEqual(email, "old@example.com")
        self.assertIn("old@example.com", menu.call_args.args[1][0])
        asked.assert_not_called()

    def test_a_different_account_can_be_entered(self):
        email, _, asked = self.email(remembered="old@example.com", choice=1)
        self.assertEqual(email, "new@example.com")
        asked.assert_called_once()
        self.assertEqual(self.remembered, "new@example.com")   # offered first next time

    def test_one_given_in_advance_is_used_without_asking(self):
        email, menu, asked = self.email(remembered="old@example.com", given="env@example.com")
        self.assertEqual(email, "env@example.com")
        menu.assert_not_called()
        asked.assert_not_called()

    def test_with_none_remembered_it_is_simply_asked(self):
        email, menu, _ = self.email()
        self.assertEqual(email, "new@example.com")
        menu.assert_not_called()

    def test_a_phone_number_is_kept_as_it_signs_in(self):
        email, _, asked = self.email(typed="+44 7700 900123")
        self.assertEqual(email, "+447700900123")
        valid = asked.call_args.kwargs["validate"]
        for answer in ("+44 7700 900123", "+44-7700-900123", "new@example.com",
                       "first.last+tag@mail.example.co.uk"):
            self.assertTrue(valid(answer)[0], answer)
        for answer in ("07700 900123", "test@test", "test@", "@example.com", "test@example.",
                       "test@.com", "test@example.c", "test@example.123", "two@at@example.com"):
            self.assertFalse(valid(answer)[0], answer)

    def test_once_chosen_in_a_run_it_is_not_asked_again(self):
        email, menu, asked = self.email(remembered="old@example.com", chosen="run@example.com")
        self.assertEqual(email, "run@example.com")
        menu.assert_not_called()
        asked.assert_not_called()


class OneSignInTests(unittest.TestCase):
    """A run signs in to eWeLink once: each sign-in ends the one before, and
    eWeLink turns away an account that signs in too often."""

    def ctx(self, sign_in):
        from types import SimpleNamespace
        ctx = SimpleNamespace(ewelink=context.Account(sign_in), state=SimpleNamespace(set_input=lambda *a: None),
                              _given_email=None, _given_ewelink_password=None)
        ctx._ewelink_password = "pw"
        return ctx

    def test_the_devices_are_listed_again_with_the_same_sign_in(self):
        sign_in = mock.Mock(return_value=("us", {"at": "t"}))
        ctx = self.ctx(sign_in)
        with mock.patch.object(context, "get_devices", return_value=[{"deviceid": "1000abcdef"}]) as listed:
            for _ in range(3):
                self.assertEqual(context.Context.account_devices(ctx), [{"deviceid": "1000abcdef"}])
        sign_in.assert_called_once()
        self.assertEqual({call.args for call in listed.call_args_list}, {("us", "t")})

    def test_a_helper_is_handed_the_run_s_sign_in_once_there_is_one(self):
        ctx = self.ctx(mock.Mock())
        ctx.ewelink_env = lambda: {"EWELINK_PASSWORD": "pw"}
        self.assertNotIn(context.SESSION_ENV, context.Context.helper_env(ctx))
        ctx.ewelink.held = ("us", {"at": "t", "user": {"apikey": "a"}})
        env = context.Context.helper_env(ctx)
        self.assertEqual(env["EWELINK_PASSWORD"], "pw")
        self.assertEqual(json.loads(env[context.SESSION_ENV])["at"], "t")

    def test_another_account_is_signed_in_to_afresh(self):
        sign_in = mock.Mock(side_effect=[("eu", {"at": "a"}), ("eu", {"at": "b"})])
        ctx = self.ctx(sign_in)
        with mock.patch.object(context, "get_devices", return_value=[]) as listed:
            context.Context.account_devices(ctx)
            context.Context.forget_account(ctx)
            context.Context.account_devices(ctx)
        self.assertEqual([call.args[1] for call in listed.call_args_list], ["a", "b"])

    def test_the_account_list_is_kept_out_of_the_log(self):
        # It holds each device's LAN key.
        ctx = self.ctx(mock.Mock(return_value=("us", {"at": "t"})))
        devices = [{"deviceid": "1000abcdef", "devicekey": "secret"}]
        with mock.patch.object(context, "get_devices", return_value=devices), \
                mock.patch.object(context.account, "keep_account_private") as kept:
            context.Context.account_devices(ctx)
        kept.assert_called_once_with(devices)

    def test_the_sign_in_uses_the_password_and_keeps_its_token_out_of_the_log(self):
        from types import SimpleNamespace
        ctx = SimpleNamespace(email=lambda: "you@example.com", _ewelink_password=None,
                              _last_region=lambda: "eu", _remember_region=lambda region: None)
        ctx.ewelink_env = lambda: setattr(ctx, "_ewelink_password", "typed")
        with mock.patch.object(context, "login_session", return_value=("eu", {"at": "token"})) as signed, \
                mock.patch.object(output, "private") as private:
            self.assertEqual(context.Context._sign_in(ctx), ("eu", {"at": "token"}))
        self.assertEqual(signed.call_args.args[:2], ("you@example.com", "typed"))
        private.assert_called_once_with("token", "access-token")

    def test_another_accounts_region_is_not_written_over(self):
        from types import SimpleNamespace
        settings = {"ewelink_email": "other@example.com", "ewelink_region": "us"}
        ctx = SimpleNamespace(email=lambda: "you@example.com")
        with mock.patch.object(runs, "load_settings", lambda: dict(settings)), \
                mock.patch.object(runs, "save_settings") as saved:
            context.Context._remember_region(ctx, "as")
        saved.assert_not_called()

    def test_forgetting_the_account_forgets_its_password_and_any_given(self):
        ctx = self.ctx(mock.Mock(return_value=("eu", {"at": "a"})))
        inputs = {"email": "you@example.com"}
        ctx.state = SimpleNamespace(set_input=inputs.__setitem__)
        ctx._given_email, ctx._given_ewelink_password = "you@example.com", "given"
        context.Context.forget_account(ctx)
        self.assertEqual((inputs["email"], ctx._ewelink_password, ctx._given_email, ctx._given_ewelink_password),
                         (None, None, None, None))

    def test_the_sign_in_starts_where_the_account_was_last_found(self):
        from types import SimpleNamespace
        settings = {"ewelink_email": "you@example.com", "ewelink_region": "us"}
        ctx = SimpleNamespace(email=lambda: "you@example.com", ewelink_env=lambda: {}, _ewelink_password="pw")
        ctx._last_region = lambda: context.Context._last_region(ctx)
        ctx._remember_region = lambda region: context.Context._remember_region(ctx, region)
        with mock.patch.object(runs, "load_settings", lambda: dict(settings)), \
                mock.patch.object(runs, "save_settings", settings.update), \
                mock.patch.object(context, "login_session", return_value=("as", {"at": "t"})) as signed:
            context.Context._sign_in(ctx)
            self.assertEqual(signed.call_args.args[3], "us")
            self.assertEqual(settings["ewelink_region"], "as")   # where eWeLink sent it
            ctx.email = lambda: "other@example.com"
            self.assertEqual(context.Context._last_region(ctx), "eu")   # another account's is not its


class NextDeviceSignInTests(unittest.TestCase):
    """The next device's run in the same sitting keeps the last one's eWeLink
    sign-in: account, password and session, never asked again."""

    def test_the_last_runs_account_and_sign_in_are_kept(self):
        from types import SimpleNamespace
        last = SimpleNamespace(state=SimpleNamespace(input={"email": "you@example.com"}.get),
                               _ewelink_password="secret-1", ewelink=context.Account(mock.Mock()))
        last.ewelink.held = ("eu", {"at": "token"})
        inputs = {}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__),
                              _ewelink_password=None, ewelink=context.Account(mock.Mock()))
        context.Context.keep_sign_in(ctx, last)
        self.assertEqual((inputs, ctx._ewelink_password, ctx.ewelink.session()),
                         ({"email": "you@example.com"}, "secret-1", ("eu", {"at": "token"})))
        with mock.patch.object(prompts, "ask_choice") as menu, mock.patch.object(prompts, "ask_text") as asked:
            ctx.remember_email = lambda email: inputs.__setitem__("email", email)
            ctx._given_email = None
            self.assertEqual(context.Context.email(ctx), "you@example.com")
        menu.assert_not_called()
        asked.assert_not_called()

    def test_a_run_from_a_converted_device_had_none_to_keep(self):
        from types import SimpleNamespace
        last = SimpleNamespace(state=SimpleNamespace(input={}.get), _ewelink_password=None,
                               ewelink=context.Account(mock.Mock()))
        inputs = {}
        ctx = SimpleNamespace(state=SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__),
                              _ewelink_password=None, ewelink=context.Account(mock.Mock()))
        context.Context.keep_sign_in(ctx, last)
        self.assertEqual((inputs, ctx._ewelink_password, ctx.ewelink.held), ({}, None, None))


class PhasePortTests(unittest.TestCase):
    """The migration steps are served on 8089 unless something else has it."""

    def runner(self, busy, delivered="8088"):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            state = RunState.open(Path(directory))
            state.set_input("delivery_port", delivered)
            ctx = SimpleNamespace(state=state, manifest_path=Path("m.json"), evidence=Path("e"),
                                  listen_ip=lambda: "192.0.2.141", device=object(),
                                  recovery_bytes=lambda: b"recovery")
            opened = []
            with mock.patch.object(hostsys, "port_is_free", lambda ip, port: port not in busy), \
                 mock.patch.object(firewall, "ensure_firewall",
                                   lambda c: opened.append(firewall.firewall_ports(c))), \
                 mock.patch.object(context, "PhaseRunner") as runner, \
                 mock.patch.object(output, "say"):
                try:
                    self.assertIs(context.Context.phase_runner(ctx), runner.return_value)
                    self.assertIs(ctx.last_runner, runner.return_value)
                    # Given the startup firmware unless told otherwise (a restore).
                    self.assertEqual(runner.call_args.kwargs["recovery"], b"recovery")
                    error = None
                except errors.Abort as exc:
                    error = str(exc)
            port = runner.call_args.kwargs["listen_port"] if runner.called else None
            return port, opened, state.input("phase_port"), error

    def test_8089_is_used_when_free(self):
        self.assertEqual(self.runner(busy=set())[:3], (8089, [[8088, 8089]], "8089"))

    def test_a_busy_8089_moves_to_8100_and_the_firewall_follows(self):
        # The bridge may have been served on 8090-8099; the phases stay clear of those.
        self.assertEqual(self.runner(busy={8089}, delivered="8090")[:3], (8100, [[8090, 8100]], "8100"))

    def test_every_port_busy_stops_before_anything_is_sent(self):
        port, opened, _, error = self.runner(busy={8089, *range(8100, 8110)})
        self.assertIsNone(port)
        self.assertEqual(opened, [])
        self.assertIn("all in use", error)


class GivenPasswordTests(unittest.TestCase):
    def context(self, given, email="me@example.com", given_email=None):
        import tools.autoflash.cli as autoflash
        ctx = SimpleNamespace(_ewelink_password=None, _given_ewelink_password=given,
                              _given_email=given_email, email=lambda: email)
        quiet = mock.patch.object(output, "say")
        quiet.start()
        self.addCleanup(quiet.stop)
        return autoflash, ctx

    def test_it_is_used_once_then_asked_for(self):
        autoflash, ctx = self.context("from-env")
        with mock.patch.object(prompts, "ask_secret", return_value="typed") as asked:
            self.assertEqual(context.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "from-env")
            asked.assert_not_called()
            ctx._ewelink_password = None  # eWeLink refused it
            self.assertEqual(context.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "typed")

    def test_not_for_another_account_than_the_one_given(self):
        autoflash, ctx = self.context("from-env", email="other@example.com",
                                      given_email="me@example.com")
        with mock.patch.object(prompts, "ask_secret", return_value="typed"):
            self.assertEqual(context.Context.ewelink_env(ctx)["EWELINK_PASSWORD"], "typed")


if __name__ == "__main__":
    unittest.main()
