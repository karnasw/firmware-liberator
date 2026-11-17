"""ESPHome and the way to and from it (tools/autoflash/routes/espressif/mover.py):
its check (I2), the signal read before leaving it (X1), Tasmota left for the
mover (L1) and the identity carried off it, the mover's own network,
ESPHome from the mover (M1), the startup firmware confirmed (X2), and an
earlier release updated (U1, U2)."""
import contextlib
import io
import time
import unittest
from pathlib import Path
from unittest import mock

from tests.autoflash.routes.espressif.test_espressif import SUPLA, RouteTestCase
from tests.autoflash.routes.espressif.test_identity import Fixture, IDENTITY, on_tasmota
from tools.autoflash import cli, detect, device, errors, esphome, esphome_ota, handover, join, kinds, output
from tools.autoflash import plans, prompts, report, routes, runs, setup_network, unlock
from tools.autoflash.routes import espressif, update
from tools.autoflash.device import DeviceError
from tools.autoflash.routes.espressif import evidence, hub, identity, images, mover, source as espressif_source
from tools.autoflash.state import DONE
from tools.espressif import nvs_image, sonoff_identity


class CheckStepTests(RouteTestCase):
    def test_it_recognises_esphome_itself(self):
        with mock.patch.object(esphome, "is_esphome", return_value=True):
            self.assertIn("seconds", mover.step_esphome_check(self.ctx(firmware="esphome")))

    def test_esphome_answering_is_given_the_devices_factory_calibration(self):
        with mock.patch.object(esphome, "is_esphome", return_value=True), \
                mock.patch.object(mover, "calibrate_esphome_meter", return_value={"calibration": "from backup"}):
            result = mover.step_esphome_check(self.ctx(firmware="esphome"))
        self.assertEqual(result["meter"], {"calibration": "from backup"})

    def test_waiting_in_vain_points_at_the_wifi(self):
        with mock.patch.object(esphome, "is_esphome", return_value=False), \
                mock.patch.object(time, "sleep"), mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(errors.Abort, "esphome-xxxxxx"):
                mover.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)

    def test_not_back_the_person_is_asked_to_switch_it_off_and_on_and_it_is_counted(self):
        # As the Beken route's K2 asks (arrival.until_back).
        ctx = self.ctx(firmware="esphome")
        with mock.patch.object(esphome, "is_esphome", side_effect=lambda *a, **k: ctx.state.input("power_cycles")
                               is not None), \
                mock.patch.object(time, "sleep"), mock.patch.object(mover, "calibrate_esphome_meter", return_value={}), \
                mock.patch.object(prompts, "section"), mock.patch.object(prompts, "ask_choice", return_value=0) as asked:
            self.assertIn("seconds", mover.step_esphome_check(ctx, wait=0.05))
        asked.assert_called_once()
        self.assertEqual(ctx.state.input("power_cycles"), {"by hand": 1})

    def test_on_its_own_network_it_is_set_up_through_that(self):
        with mock.patch.object(esphome, "is_esphome", return_value=False), \
                mock.patch.object(time, "sleep"), \
                mock.patch.object(join, "set_up_through_own_network", return_value=("Home", "pw")) as own:
            result = mover.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)
        self.assertEqual(result["set_up_through"], "its own network")
        self.assertEqual(own.call_args.args[1], "ESPHome")

    def test_back_on_your_wifi_by_itself_it_carries_on(self):
        with mock.patch.object(esphome, "is_esphome", return_value=False), \
                mock.patch.object(time, "sleep"), mock.patch.object(mover, "calibrate_esphome_meter", return_value={}), \
                mock.patch.object(join, "set_up_through_own_network", side_effect=join.BackByItself):
            result = mover.step_esphome_check(self.ctx(firmware="esphome"), wait=0.05)
        self.assertNotIn("set_up_through", result)

    def test_a_mover_back_on_your_wifi_by_itself_is_not_set_up(self):
        ctx = self.ctx(firmware="esphome")
        ctx._wifi = ("Home", "pw")
        with mock.patch.object(join, "on_a_device_network", return_value=False), \
                mock.patch.object(join, "wait_for", side_effect=errors.Abort("not on your Wi-Fi")), \
                mock.patch.object(join, "set_up_through_own_network", side_effect=join.BackByItself):
            self.assertFalse(mover.wait_for_mover(ctx, "The mover"))

    def test_joined_already_it_stops_waiting_on_the_home_wifi(self):
        with mock.patch.object(esphome, "is_esphome", return_value=False), \
                mock.patch.object(join, "on_a_device_network", return_value=True), \
                mock.patch.object(time, "sleep") as slept, \
                mock.patch.object(join, "set_up_through_own_network", return_value=("Home", "pw")):
            mover.step_esphome_check(self.ctx(firmware="esphome"), wait=600)
        slept.assert_not_called()  # straight on, not three minutes first


class SignalBeforeLeavingESPHomeTests(RouteTestCase):
    """X1 reads the signal before anything is written, where ESPHome gives it."""

    def x1(self, signal, carry_on=True):
        ctx = self.ctx(source="esphome")
        with mock.patch.object(esphome, "signal", return_value=signal), \
                mock.patch.object(handover, "consent_to_leave") as consent, \
                mock.patch.object(detect, "identify", return_value="startup"), \
                mock.patch.object(prompts, "ask_yes_no", return_value=carry_on) as asked, \
                mock.patch.object(prompts, "section"), \
                mock.patch.object(unlock, "unlock_esphome") as self.unlocked:
            try:
                self.result = mover.step_startup_from_esphome(ctx)
            except errors.Abort as exc:
                return ctx, consent, asked, exc
        return ctx, consent, asked, None

    def test_a_weak_signal_is_asked_about_before_the_go_ahead(self):
        _, consent, asked, stopped = self.x1(-82, carry_on=False)
        self.assertRegex(str(stopped), "weak Wi-Fi signal")
        asked.assert_called_once()
        consent.assert_not_called()

    def test_a_good_signal_is_recorded(self):
        ctx, consent, asked, stopped = self.x1(-58)
        self.assertIsNone(stopped)
        self.assertEqual(ctx.state.data["captures"]["X1.signal_dbm"], -58)
        asked.assert_not_called()
        consent.assert_called_once()
        self.assertEqual(self.result, {"already": "startup"})   # past it: nothing sent
        self.unlocked.assert_called_once_with(ctx)   # its password or key asked for first, where it has one

    def test_an_esphome_without_the_sensor_goes_on_unasked(self):
        ctx, consent, asked, stopped = self.x1(None)
        self.assertIsNone(stopped)
        self.assertIsNone(ctx.state.data["captures"]["X1.signal_dbm"])
        asked.assert_not_called()
        consent.assert_called_once()


class ConfirmedOnlyAfterAMinuteTests(RouteTestCase):
    """ESPHome sent to the mover is confirmed only after a minute running."""

    def check(self, source, answers, probes=()):
        ctx = self.ctx(source=source, firmware="esphome")
        ctx.state.mark("M1", DONE)
        self.clock = 1000.0   # not from 0: times are measured from the start
        self.answered_at = []

        def tick():
            self.clock += 3
            return self.clock

        def asked(host):
            answer = next(answers)
            if answer:
                self.answered_at.append(self.clock)
            return answer
        answers = iter(answers)
        with mock.patch.object(time, "monotonic", side_effect=tick), \
                mock.patch.object(time, "sleep"), \
                mock.patch.object(esphome, "is_esphome", side_effect=asked), \
                mock.patch.object(esphome_ota, "probe", side_effect=list(probes) or None), \
                mock.patch.object(mover, "step_esphome_from_mover",
                                  side_effect=lambda ctx: self.answered_at.clear()) as self.resent:
            return mover.step_esphome_check(ctx), self.resent

    def test_from_tasmota_it_is_only_done_once_esphome_has_confirmed_itself(self):
        result, _ = self.check("tasmota", [True] * 40)
        self.assertEqual(result["resent"], 0)
        # Asked again until 70 s after it first answered.
        self.assertGreaterEqual(self.answered_at[-1] - self.answered_at[0], mover.ESPHOME_CONFIRMS_AFTER)
        self.assertLess(result["seconds"], 10)   # from the start of the wait to its first answer

    def test_the_minute_starts_again_once_it_has_been_sent_again(self):
        result, resent = self.check("tasmota", [True] * 5 + [False] + [True] * 40, probes=[object()])
        resent.assert_called_once()
        self.assertGreaterEqual(self.answered_at[-1] - self.answered_at[0], mover.ESPHOME_CONFIRMS_AFTER)

    def test_one_that_keeps_going_back_to_the_mover_is_sent_esphome_twice_then_stops(self):
        with self.assertRaisesRegex(errors.Abort, "keeps going back to the startup firmware"):
            self.check("tasmota", [False] * 10, probes=[object()] * 10)
        self.assertEqual(self.resent.call_count, 2)

    def test_from_tasmota_esphome_has_its_minute_on_top_of_the_wait(self):
        # First answering late in the wait, it still has the minute to confirm itself.
        late = int(150 / 6)   # two clock reads a step: 6 s
        result, _ = self.check("tasmota", [False] * late + [True] * 60, probes=[None] * late)
        self.assertGreaterEqual(result["seconds"], 140)

    def test_esphome_sent_again_late_in_the_wait_has_the_whole_wait_again(self):
        late = 35   # 210 s in, two clock reads a step
        result, resent = self.check("tasmota", [False] * (late + 11) + [True] * 40,
                                    probes=[None] * late + [object()] + [None] * 10)
        resent.assert_called_once()
        self.assertEqual(result["resent"], 1)

    def test_its_own_network_up_ends_the_wait_without_the_rest_of_it(self):
        ctx = self.ctx(source="tasmota", firmware="esphome")
        self.clock = 1000.0

        def tick():
            self.clock += 3
            return self.clock
        scans = iter([None, "esphome-abcdef"])
        with mock.patch.object(time, "monotonic", side_effect=tick), mock.patch.object(time, "sleep"), \
                mock.patch.object(esphome, "is_esphome", return_value=False), \
                mock.patch.object(esphome_ota, "probe", return_value=None), \
                mock.patch.object(join.prompts, "find_ssid", side_effect=lambda *a, **k: next(scans)), \
                mock.patch.object(join, "set_up_through_own_network", return_value=("Home-2G", "pw")) as own:
            result = mover.step_esphome_check(ctx)
        own.assert_called_once()
        self.assertEqual(result["set_up_through"], "its own network")
        self.assertLess(result["seconds"], 2 * join.OWN_NETWORK_LOOK_EVERY + 20, "not the three minutes")

    def test_the_device_is_followed_to_a_new_address(self):
        ctx = self.ctx(source="tasmota", firmware="esphome")

        def moved():
            ctx.state.set_input("device_ip", "192.0.2.51")
            return "192.0.2.51"
        ctx.finder = mock.Mock(poll=mock.Mock(side_effect=moved))
        with mock.patch.object(esphome, "is_esphome", side_effect=lambda host: host == "192.0.2.51"), \
                mock.patch.object(time, "sleep"), mock.patch.object(join, "set_up_through_own_network") as own:
            mover.step_esphome_check(ctx)
        own.assert_not_called()

    def test_back_on_the_mover_it_is_sent_esphome_again(self):
        result, resent = self.check("tasmota", [False] + [True] * 40, probes=[object()])
        resent.assert_called_once()
        self.assertEqual(result["resent"], 1)

    def test_from_tasmota_by_way_of_the_hub_nothing_is_waited_for(self):
        # Written there (I1), with the boot selection cleared: not an update.
        with mock.patch.object(esphome, "is_esphome", return_value=True) as asked:
            mover.step_esphome_check(self.ctx(source="tasmota", firmware="esphome"))
        self.assertEqual(asked.call_count, 1)

    def test_from_stock_nothing_is_waited_for(self):
        with mock.patch.object(esphome, "is_esphome", return_value=True) as asked:
            ctx = self.ctx(firmware="esphome")
            mover.step_esphome_check(ctx)
        self.assertEqual(asked.call_count, 1)


class ESPHomeUpdateTests(RouteTestCase):
    """This tool's ESPHome, an earlier release, is offered this one as an
    update; any other ESPHome is not."""

    def settle(self, info, *, given=None, answer=0, run="run", **inputs):
        ctx = self.ctx(given=given, run=run, device_kind="sonoff-s60", **inputs)
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(esphome, "api_device_info", return_value=info), \
                mock.patch.object(espressif, "published_version", return_value="1.6.0"), \
                mock.patch.object(routes, "sonoff_backup_exists", return_value=True), \
                mock.patch.object(espressif_source, "leaves_tasmota_direct", return_value=False), \
                mock.patch.object(prompts, "ask_choice", return_value=answer) as asked:
            route = routes.settle_route(ctx)
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        return ctx, route, (asked.call_args.args[1] if asked.called else None)

    @staticmethod
    def ours(release="1.4.0", **changed):
        return {"name": "esphome-abcdef", "esphome_version": "2026.10.0", "project_name": "liberator.sonoff-s60",
                "project_version": release, "built": images.PINS["sonoff-s60"]["esphome"]["built"], "encrypted": False, **changed}

    def test_an_earlier_release_is_offered_the_update(self):
        ctx, route, offered = self.settle(self.ours(), answer=1)
        self.assertEqual(offered, ["Tasmota", "ESPHome, update to 1.6.0", SUPLA,
                                   "Sonoff (eWeLink)"])
        self.assertEqual(route, ("esphome", "outdated"))
        self.assertEqual([step for step, _, _ in routes.steps_of(ctx.state)], ["A0", "A3", "A5", "U1", "U2"])
        self.assertEqual(update.versions(ctx), ("1.4.0", "1.6.0"))

    def test_one_named_as_before_2_4_0_is_offered_it_too(self):
        # The images published before 3.0.0 call themselves adamsthws.sonoff-s60.
        _, route, offered = self.settle(self.ours(project_name="adamsthws.sonoff-s60"), answer=1)
        self.assertEqual(route, ("esphome", "outdated"))
        self.assertIn("ESPHome, update to 1.6.0", offered)

    def test_asked_for_on_the_command_line_it_is_updated_unasked(self):
        _, route, offered = self.settle(self.ours(), given="esphome")
        self.assertEqual((route, offered), (("esphome", "outdated"), None))

    def test_leaving_it_is_leaving_any_esphome(self):
        ctx, route, _ = self.settle(self.ours(), answer=0)
        self.assertEqual(route, ("tasmota", "esphome"))
        self.assertEqual(routes.steps_of(ctx.state), routes.steps_for("tasmota", "esphome"))

    def test_any_other_esphome_is_not_offered_it(self):
        for why, info in (("this release", self.ours("1.6.0")), ("a later one", self.ours("1.10.0")),
                          ("another project", self.ours(project_name="someone.else")),
                          ("another kind's", self.ours(project_name="liberator.sonoff-s61")),
                          ("another kind's, named as before 3.0.0", self.ours(project_name="adamsthws.sonoff-s61")),
                          ("adopted, its API given a key", self.ours(encrypted=True)),
                          ("adopted without a key: not a build published",
                           self.ours(built="2026-11-04 21:19:12 +0000")),
                          ("not one of our releases", self.ours("dev")), ("not saying", None)):
            with self.subTest(why):
                with self.assertRaisesRegex(routes.NothingToDo, "already runs ESPHome"):
                    self.settle(info, given="esphome", run=why)

    def test_not_with_an_esphome_built_by_hand(self):
        ctx = self.ctx(given="esphome", device_kind="sonoff-s60")
        ctx.esphome_firmware = Path("own.bin")
        with mock.patch.object(esphome, "api_device_info", return_value=self.ours()):
            self.assertFalse(update.outdated(ctx, "esphome", "1.6.0"))

    def test_status_says_an_update_is_available_and_records_nothing(self):
        ctx = self.ctx(device_kind="sonoff-s60", firmware="esphome", source="sonoff")
        with mock.patch.object(esphome, "api_device_info", return_value=self.ours()), \
                mock.patch.object(espressif, "published_version", return_value="1.6.0"):
            self.assertEqual(routes.update_available(ctx),
                             "ESPHome 1.4.0 is on the device, and 1.6.0 is available: run the tool again")
        self.assertIsNone(ctx.state.input("update_from"))

    def test_status_shows_it_with_the_run(self):
        ctx = self.ctx(device_kind="sonoff-s60", firmware="esphome", source="sonoff")
        ctx.run, ctx.manifest_path = "12:34:56:ab:cd:ef", "manifest.json"
        with mock.patch.object(routes, "update_available", return_value="1.6.0 is available"), \
                contextlib.redirect_stdout(io.StringIO()) as shown:
            self.assertEqual(cli.cmd_status(ctx), 0)
        self.assertIn("Update:    1.6.0 is available", shown.getvalue())

    def test_status_says_nothing_where_there_is_no_update_to_offer(self):
        for why, firmware, info in (("this release", "esphome", self.ours("1.6.0")),
                                    ("adopted", "esphome", self.ours(encrypted=True)),
                                    ("not answering", "esphome", None),
                                    ("no firmware chosen", None, self.ours()),
                                    ("back on Sonoff's", "sonoff", self.ours())):
            with self.subTest(why):
                ctx = self.ctx(run=why, device_kind="sonoff-s60", firmware=firmware, source="sonoff")
                with mock.patch.object(esphome, "api_device_info", return_value=info), \
                        mock.patch.object(espressif, "published_version", return_value="1.6.0"):
                    self.assertIsNone(routes.update_available(ctx))

    def test_status_survives_a_device_that_fails_when_asked(self):
        ctx = self.ctx(device_kind="sonoff-s60", firmware="esphome", source="sonoff")
        with mock.patch.object(esphome, "api_device_info", side_effect=OSError("no route")), \
                mock.patch.object(espressif, "published_version", return_value="1.6.0"):
            self.assertIsNone(routes.update_available(ctx))

    OWN = "2026-11-04 21:19:12 +0000"

    def replacing(self, info, *, word=True, **inputs):
        ctx = self.ctx(given="esphome", run=str(id(info)) + str(word), device_kind="sonoff-s60", **inputs)
        ctx.replace_own_build = True
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(esphome, "api_device_info", **(
                    {"side_effect": info} if isinstance(info, BaseException) else {"return_value": info})), \
                mock.patch.object(espressif, "published_version", return_value="1.6.0"), \
                mock.patch.object(routes, "sonoff_backup_exists", return_value=True), \
                mock.patch.object(prompts, "warning_box") as warned, \
                mock.patch.object(prompts, "require_word", return_value=word):
            self.addCleanup(routes.apply_target, "tasmota", "sonoff")
            return ctx, routes.settle_route(ctx), warned

    def test_replacing_an_own_build_without_a_key_of_the_same_version(self):
        ctx, route, warned = self.replacing(self.ours("1.6.0", built=self.OWN))
        self.assertEqual(route, ("esphome", "outdated"))
        self.assertEqual(update.versions(ctx), (update.OWN_BUILD, "1.6.0"))
        self.assertEqual([step for step, _, _ in routes.steps_of(ctx.state)], ["A0", "A3", "A5", "U1", "U2"])
        said = " ".join(warned.call_args.args[1])
        self.assertIn("Your config changes and API encryption key are lost", said)
        self.assertIn("A renamed relay reverts", said)

    def test_replacing_an_own_build_with_a_key_given(self):
        ctx, route, _ = self.replacing(self.ours("1.6.0", built=self.OWN, encrypted=True))
        self.assertEqual(route, ("esphome", "outdated"))

    def test_with_no_key_to_give_nothing_is_replaced(self):
        with mock.patch.object(unlock, "ask_esphome_key", return_value=False):
            with self.assertRaisesRegex(errors.Abort, "API encryption key, and none was given"):
                self.replacing(esphome.ApiLocked())
            with self.assertRaisesRegex(errors.Abort, "API encryption key, and the one given is not it"):
                self.replacing(esphome.ApiLocked(wrong=True))

    def test_not_agreeing_stops_before_anything_is_recorded(self):
        with self.assertRaisesRegex(errors.Abort, "not confirmed"):
            self.replacing(self.ours("1.6.0", built=self.OWN), word=False)

    def test_another_projects_esphome_is_never_replaced(self):
        for info in (self.ours(project_name="someone.else"), self.ours(project_name="liberator.sonoff-s61"),
                     self.ours(project_name="adamsthws.sonoff-s61"), None):
            with self.subTest(info=info), self.assertRaisesRegex(errors.Abort, "only replaces this project's"):
                self.replacing(info)

    def test_a_published_build_has_nothing_to_replace(self):
        with self.assertRaisesRegex(routes.NothingToDo, "nothing to convert\\.$"):
            self.replacing(self.ours("1.6.0"))

    def test_a_published_earlier_release_is_just_updated(self):
        ctx, route, warned = self.replacing(self.ours())
        self.assertEqual(update.versions(ctx), ("1.4.0", "1.6.0"))
        warned.assert_not_called()

    def test_any_other_firmware_is_not_replaced(self):
        ctx = self.ctx(given="esphome", device_kind="sonoff-s60")
        with self.assertRaisesRegex(errors.Abort, "does not answer as that: it runs Tasmota"):
            update.replace_own_build(ctx, "tasmota", "16.0.0")

    def test_the_update_is_only_done_once_the_published_build_answers(self):
        # Often the same version: an own build that kept running, or a device that
        # went back to it, must not pass for the release sent.
        ctx = self.ctx(device_kind="sonoff-s60", firmware="esphome", replaced_build=self.OWN)
        with mock.patch.object(esphome, "api_device_info", return_value=self.ours("1.6.0", built=self.OWN)):
            self.assertEqual(update.esphome_release(ctx), update.OWN_BUILD)
        with mock.patch.object(esphome, "api_device_info", return_value=self.ours("1.6.0")):
            self.assertEqual(update.esphome_release(ctx), "1.6.0")

    def test_a_refused_update_points_to_the_option_for_an_own_build_alone(self):
        for info, says in ((self.ours("1.6.0", built=self.OWN), True), (self.ours("1.6.0"), False),
                           (esphome.ApiLocked(), True), (None, False)):
            ctx = self.ctx(given="esphome", run=f"hint{id(info)}", device_kind="sonoff-s60")
            with self.subTest(info=info), mock.patch.object(esphome, "api_device_info", **(
                    {"side_effect": info} if isinstance(info, BaseException) else {"return_value": info})):
                with self.assertRaises(routes.NothingToDo) as stopped:
                    routes.nothing_to_convert(ctx, "esphome")
                self.assertEqual("--replace-own-build" in str(stopped.exception), says)

    def test_a_build_published_for_an_earlier_release_is_offered_it(self):
        ctx, route, _ = self.settle(self.ours(built=images.PINS["sonoff-s60"]["esphome"]["earlier_builds"][0]), answer=1)
        self.assertEqual(route, ("esphome", "outdated"))

    def test_published_builds_are_each_kinds_own(self):
        s60, s61 = (update.published_builds(kinds.by_key(key)) for key in ("sonoff-s60", "sonoff-s61"))
        self.assertIn(images.PINS["sonoff-s60"]["esphome"]["built"], s60)
        self.assertIn(images.FIRMWARE["beken"]["sonoff-s61"]["esphome"]["built"], s61)
        self.assertEqual(s60 & s61, set())

    def test_releases_are_compared_as_numbers(self):
        self.assertTrue(update.older("1.9.0", "1.10.0"))
        self.assertFalse(update.older("1.10.0", "1.9.0"))
        self.assertFalse(update.older("1.6.0", "1.6.0"))
        self.assertFalse(update.older("", "1.6.0"))

    def test_openbekens_builds_of_a_release_are_compared_too(self):
        self.assertTrue(update.older("1.1.0", "1.18.320-1"))
        self.assertTrue(update.older("1.18.320-1", "1.18.320-2"))
        self.assertTrue(update.older("1.18.320-2", "1.18.321-1"))
        self.assertFalse(update.older("1.18.320-2", "1.18.320-2"))
        self.assertFalse(update.older("16.0.0(release-tasmota32)", "16.0.0"))

    def check(self, versions, confirm=0.0, wait=60.0):
        """`versions`: what the device answers, in turn; the last, from then on.
        Time passes only as the run sleeps."""
        ctx = self.ctx(source="outdated", firmware="esphome", update_from="1.4.0", update_to="1.6.0",
                       device_kind="sonoff-s60")
        clock = [0.0]
        said = list(versions)

        def answer(*a, **k):
            version = said.pop(0) if len(said) > 1 else said[0]
            return self.ours(version) if version else None

        def sleep(seconds):
            clock[0] += seconds
        with mock.patch.object(esphome, "api_device_info", side_effect=answer), \
                mock.patch.object(time, "sleep", side_effect=sleep), \
                mock.patch.object(time, "monotonic", side_effect=lambda: clock[0]):
            return update.wait_for_update(ctx, wait=wait, confirm=confirm)

    def test_the_check_waits_for_the_new_release(self):
        result = self.check([None, "1.4.0", "1.6.0"])
        self.assertEqual((result["from"], result["to"]), ("1.4.0", "1.6.0"))

    def test_one_gone_back_to_its_old_release_is_not_done(self):
        # The S60's ESPHome takes an update back if it restarts before confirming it.
        with self.assertRaisesRegex(errors.Abort, "went back to ESPHome 1.4.0"):
            self.check(["1.6.0", "1.6.0", "1.4.0"], confirm=70.0)
        self.assertEqual(self.check(["1.6.0"], confirm=70.0)["to"], "1.6.0")

    def test_one_not_answering_during_its_minute_is_not_taken_to_have_kept_it(self):
        # Only a device seen to run the new release a whole minute on end has kept it.
        with self.assertRaisesRegex(errors.Abort, "was not seen running for a minute"):
            self.check(["1.6.0"] * 6 + [None], confirm=60.0)

    def test_the_minute_starts_again_once_a_device_that_stopped_answering_is_back(self):
        # update>esphome:U2:deviceoff-180: switched off 24 s into the minute, back
        # three minutes later; said done before, unseen.
        answers = ["1.6.0"] * 5 + [None] * 38 + ["1.6.0"]
        with mock.patch.object(output, "say") as said:
            self.assertEqual(self.check(answers, confirm=60.0, wait=180.0)["to"], "1.6.0")   # U2's own wait
        self.assertTrue(any("stopped answering" in c.args[0] for c in said.call_args_list))

    def test_one_that_keeps_restarting_is_not_waited_on_for_ever(self):
        with self.assertRaisesRegex(errors.Abort, "was not seen running for a minute"):
            self.check(["1.6.0"] + ["1.6.0", None] * 200, confirm=60.0)

    def test_a_send_cut_off_is_judged_by_the_release_it_runs(self):
        ctx = self.ctx(source="outdated", firmware="esphome", update_from="1.4.0", update_to="1.6.0",
                       device_kind="sonoff-s60")
        for version, finished in (("1.6.0", True), ("1.4.0", False), (None, None)):
            with mock.patch.object(esphome, "api_device_info", return_value=self.ours(version) if version else None):
                self.assertIs(evidence.ATTEMPTED_EVIDENCE["U1"](ctx)[0], finished, version)
                self.assertIs(espressif.STEP_VERIFIERS["U1"](ctx)[0], finished is not False, version)

    def test_its_report_says_it_was_updated(self):
        for running, firmware, result in (("outdated", "esphome", "Updated"), ("esphome", "tasmota", "Converted")):
            ctx = self.ctx(source=running, firmware=firmware, run=running)
            with mock.patch.object(report, "check_esphome_hardware", return_value={}), \
                    mock.patch.object(report, "check_hardware", return_value={}), \
                    mock.patch.object(report, "offer_report") as offered:
                report.invite_report(ctx, 60.0, False)
            self.assertEqual(report.answer(dict(offered.call_args.args[1])["Result"]), result, running)

    def test_the_wifi_is_read_from_esphome_not_tasmota(self):
        ctx = self.ctx(source="outdated", firmware="esphome")
        self.assertFalse(runs.device_runs_tasmota(ctx.state))


class CarriedOffTasmotaTests(Fixture):
    """L1: the settings of the next firmware hold the identity."""

    def test_the_kept_identity_goes_into_sonoffs_settings(self):
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        settings = mover.settings_to_carry(ctx, images.layout_of(kinds.SONOFF_S60))
        self.assertEqual(sonoff_identity.from_nvs(settings + b"\xff" * (0x10000 - len(settings))), IDENTITY)
        self.assertNotIn(b"Home-2G", settings)  # nothing of the Wi-Fi
        self.assertEqual(len(settings) % nvs_image.PAGE, 0)

    def test_a_device_converted_before_carries_its_backups(self):
        self.back_up()
        settings = mover.settings_to_carry(self.ctx(on_tasmota()), images.layout_of(kinds.SONOFF_S60))
        self.assertEqual(sonoff_identity.from_nvs(settings + b"\xff" * (0x10000 - len(settings))), IDENTITY)

    def test_on_a_layout_leaving_through_a_temporary_table_they_fit_its_settings(self):
        # Its settings end where the temporary table's boot selection begins.
        from tools.espressif.layouts import TEMPORARY_OTADATA
        layout = images.layout_of(kinds.SONOFF_TH_ELITE)
        self.assertTrue(layout.needs_temporary)
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        with mock.patch.object(sonoff_identity, "nvs", wraps=sonoff_identity.nvs) as built:
            mover.settings_to_carry(ctx, layout)
        self.assertEqual(built.call_args.args[1], TEMPORARY_OTADATA - layout.nvs)

    def test_with_neither_the_region_is_cleared_as_before(self):
        self.assertIsNone(mover.settings_to_carry(self.ctx(on_tasmota()), images.layout_of(kinds.SONOFF_S60)))

    def test_a_sector_that_cannot_be_read_is_passed_by(self):
        # It stays where it is, for going back; the backup is carried instead.
        self.back_up()
        ctx = self.ctx(on_tasmota(sector=sonoff_identity.to_sector(IDENTITY)))
        with mock.patch.object(identity, "kept_in_sector", side_effect=DeviceError("timed out")):
            settings = mover.settings_to_carry(ctx, images.layout_of(kinds.SONOFF_S60))
        self.assertEqual(sonoff_identity.from_nvs(settings + b"\xff" * (0x10000 - len(settings))), IDENTITY)


class EsphomeSignalTests(unittest.TestCase):
    """On the way to ESPHome the signal is read too, off the Tasmota the device
    still runs, before what then has to join the Wi-Fi on its own is written."""

    def setUp(self):
        from unittest import mock
        self.run_plan = mock.Mock(return_value={"plan": "written"})
        self.consent = mock.Mock()
        self.wait_for_mover = mock.Mock()
        for patch in (mock.patch.object(output, "say"),
                      mock.patch.object(prompts, "section"),
                      mock.patch.object(hub, "run_plan", self.run_plan),
                      mock.patch.object(handover, "consent_to_leave", self.consent),
                      mock.patch.object(mover, "wait_for_mover", self.wait_for_mover),
                      mock.patch.object(esphome, "is_esphome", return_value=False),
                      mock.patch.object(images, "image_for", return_value=b"\0" * 64),
                      mock.patch.object(images, "mover_for", return_value=b"\0" * 64),
                      mock.patch.object(images, "leaving_table", return_value=b""),
                      mock.patch.object(esphome_ota, "probe", return_value=None),
                      mock.patch.object(plans, "esphome", return_value="plan"),
                      mock.patch.object(mover, "what_tasmota_has", return_value=(None, None)),
                      mock.patch.object(plans, "leave_tasmota", return_value="plan")):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, status):
        from types import SimpleNamespace

        def command(_):
            if isinstance(status, Exception):
                raise status
            return status

        inputs = {"device_ip": "192.168.60.50"}
        state = SimpleNamespace(input=inputs.get, set_input=inputs.__setitem__)
        return SimpleNamespace(state=state, wifi=lambda: ("Home-2G", "pass"), esphome_firmware=None,
                               dry_run=False, device=SimpleNamespace(command=command))

    def signal(self, dbm):
        return {"StatusSTS": {"Wifi": {"SSId": "Home-2G", "Signal": dbm}}}

    def test_the_signal_is_recorded_with_the_install(self):
        for step in (mover.step_esphome_install, mover.step_leave_tasmota):
            self.run_plan.reset_mock()
            self.assertEqual(step(self.ctx(self.signal(-58)))["signal_dbm"], -58, step.__name__)
            self.run_plan.assert_called_once()

    def test_tasmota_is_left_only_with_the_go_ahead_and_the_mover_then_waited_for(self):
        ctx = self.ctx(self.signal(-58))
        mover.step_leave_tasmota(ctx)
        self.consent.assert_called_once_with(ctx, "Tasmota")
        self.wait_for_mover.assert_called_once()

    def test_a_device_already_on_the_mover_is_not_written_again(self):
        # The mover answers ESPHome's update service, but is not ESPHome.
        with mock.patch.object(esphome_ota, "probe", return_value=object()):
            self.assertEqual(mover.step_leave_tasmota(self.ctx(self.signal(-58))), {"already": True})
        self.run_plan.assert_not_called()
        self.consent.assert_not_called()

    def test_a_weak_signal_stops_before_anything_is_written(self):
        from unittest import mock
        for step in (mover.step_esphome_install, mover.step_leave_tasmota):
            with mock.patch.object(prompts, "ask_yes_no", return_value=False):
                with self.assertRaisesRegex(errors.Abort, "weak Wi-Fi signal"):
                    step(self.ctx(self.signal(-82)))
        self.run_plan.assert_not_called()
        self.consent.assert_not_called()  # asked first, before the go-ahead to leave Tasmota

    def test_a_device_that_does_not_say_is_installed_all_the_same(self):
        from tools.autoflash.device import DeviceError
        for status in ({"StatusSTS": {}}, DeviceError("no answer")):
            self.run_plan.reset_mock()
            self.assertIsNone(mover.step_esphome_install(self.ctx(status))["signal_dbm"])
            self.run_plan.assert_called_once()

    def test_a_weak_signal_agreed_to_is_not_asked_about_again(self):
        from unittest import mock
        ctx = self.ctx(self.signal(-82))
        with mock.patch.object(prompts, "ask_yes_no", return_value=True) as asked:
            mover.step_leave_tasmota(ctx)        # the earliest reading: asked
            mover.step_esphome_install(ctx)      # a later one: told, not asked
        asked.assert_called_once()
        self.assertEqual(self.run_plan.call_count, 2)


ORIGINAL_TABLE_WRITTEN = mover.table_written


class LeaveTasmotaStoppedTests(unittest.TestCase):
    """L1's plan stopped with Sonoff's table in place: the device is restarted
    into the mover only if the plan got as far as writing it."""

    def setUp(self):
        from types import SimpleNamespace
        self.run_plan = mock.Mock(side_effect=errors.Abort("plan leave_tasmota did not pass"))
        for patch in (mock.patch.object(output, "say"),
                      mock.patch.object(hub, "run_plan", self.run_plan),
                      mock.patch.object(handover, "consent_to_leave"),
                      mock.patch.object(join, "note_signal"),
                      mock.patch.object(join, "device_signal"),
                      mock.patch.object(mover, "wait_for_mover"),
                      mock.patch.object(mover, "table_written", return_value=True),
                      mock.patch.object(esphome_ota, "probe", return_value=None),
                      mock.patch.object(images, "mover_for", return_value=b""),
                      mock.patch.object(images, "leaving_table", return_value=b""),
                      mock.patch.object(mover, "what_tasmota_has", return_value=(None, None)),
                      mock.patch.object(plans, "leave_tasmota",
                                        return_value=SimpleNamespace(name="leave_tasmota", running=0))):
            patch.start()
            self.addCleanup(patch.stop)

    def ctx(self, slot):
        from types import SimpleNamespace
        device = mock.Mock(current_ota=mock.Mock(return_value=slot))
        inputs = {"device_ip": "192.168.60.50"}
        state = SimpleNamespace(input=inputs.get)
        return SimpleNamespace(state=state, wifi=lambda: ("Home-2G", "pass"), dry_run=False,
                                     device=device)

    def test_a_plan_stopped_after_its_writes_restarts_the_device_into_the_mover(self):
        ctx = self.ctx(0)
        self.assertEqual(mover.step_leave_tasmota(ctx)["restarted_after"], "a stopped report")
        ctx.device.command.assert_called_once_with("Restart 99")

    def test_a_plan_refused_on_the_interim_tasmota_is_not_taken_for_one_that_wrote(self):
        # Seen in the fault suite: the hub, on slot 1 with Sonoff's table
        # already, was restarted "into the mover", and L1 recorded as done.
        ctx = self.ctx(1)
        with self.assertRaisesRegex(errors.Abort, "did not pass"):
            mover.step_leave_tasmota(ctx)
        ctx.device.command.assert_not_called()

    def test_the_table_looked_for_is_the_one_the_plan_writes(self):
        # A temporary table, or a layout other than the S60's, is as written as Sonoff's.
        from types import SimpleNamespace
        plan = SimpleNamespace(final_table_sha256=lambda: "aa")
        ctx = self.ctx(0)
        written = ctx.device.sha_region
        for found, expected in (("aa", True), (plans.SONOFF_TABLE_SHA256, False)):
            written.return_value = found
            self.assertEqual(ORIGINAL_TABLE_WRITTEN(ctx, plan), expected)


class BerryFromStartupTests(unittest.TestCase):
    """X2 - the startup firmware confirms itself only once used: switched off
    and on before then, the device starts the firmware it ran before (seen in the
    fault suite, a power cut early in X2), which once passed for Tasmota."""

    def x2(self, running, versions=("15.6.0.1(f5b34a2-bluetooth)",)):
        from types import SimpleNamespace
        from unittest import mock
        ctx = SimpleNamespace(dry_run=False, device=SimpleNamespace(host="192.168.60.50"),
                              state=SimpleNamespace(input=lambda name: None),
                              web_password=None, esphome_login=None)
        answers = iter(versions)
        seen = []

        def wait_for(ctx_, check, what, timeout=240.0, own_network=()):
            seen.append(check("192.168.60.50"))

        with mock.patch.object(detect, "identify", return_value=running), \
             mock.patch.object(mover, "step_startup_from_esphome") as x1, \
             mock.patch.object(hub, "upload_or_prompt", return_value="http:12s") as upload, \
             mock.patch.object(join, "wait_for", side_effect=wait_for), \
             mock.patch.object(device, "Device", lambda address, timeout=None: SimpleNamespace(
                 firmware_version=lambda: next(answers))), \
             mock.patch.object(output, "tell"), mock.patch.object(output, "say"):
            result = mover.step_berry_from_startup(ctx)
        self.upload = upload
        return result, x1, upload.called, seen

    def test_already_on_tasmota_does_nothing(self):
        result, x1, uploaded, _ = self.x2("hub")
        self.assertEqual(result, {"already": True})
        self.assertFalse(uploaded or x1.called)

    def test_back_on_the_mover_the_startup_firmware_goes_on_again_first(self):
        result, x1, uploaded, _ = self.x2("mover")
        x1.assert_called_once()
        self.assertEqual(x1.call_args.kwargs, {"again": True})  # no second go-ahead
        self.assertTrue(uploaded)

    def test_on_the_startup_firmware_it_is_uploaded(self):
        result, x1, uploaded, seen = self.x2("startup")
        self.assertFalse(x1.called)
        self.assertTrue(uploaded)
        self.assertEqual(seen, [True])
        self.assertEqual(result, {"upload": "http:12s"})   # how it went, for the report

    def test_an_upload_cut_off_is_not_cleared_by_a_restart(self):
        # Restarted before it has been used, the startup firmware starts what
        # the device ran before.
        self.x2("startup")
        self.assertEqual(self.upload.call_args.kwargs, {"restart_to_retry": False})

    def test_a_page_not_answering_is_not_tasmota(self):
        _, _, _, seen = self.x2("startup", versions=("",))
        self.assertEqual(seen, [False])


class MoverOwnNetworkTests(unittest.TestCase):
    """A mover that cannot join the home Wi-Fi raises its own network, and is
    set up through it - with the details the run has first, new ones only if
    those fail - not left as a device only a serial cable reaches."""

    def wait(self, answers, network=None, sends=(None,), joined_already=False, unattended=False):
        from types import SimpleNamespace
        from unittest import mock
        tries = iter(answers)
        outcomes = iter(sends)

        def wait_for(ctx, check, what, timeout=240.0, own_network=()):
            if not next(tries):
                raise errors.Abort(f"{what} did not answer within 4 minutes")

        def send(ssid, password):
            outcome = next(outcomes, None)
            if outcome:
                raise setup_network.SetupError(outcome)

        adopted = []
        ctx = SimpleNamespace(state=SimpleNamespace(input=lambda key: "12:34:56:ab:cd:ef",
                                                    set_input=lambda key, value: None),
                              _wifi=("Home", "pw0"), wifi=lambda why=None: ("Home", "pw0"),
                              adopt_wifi=lambda ssid, pw: adopted.append((ssid, pw)))
        self.home_waits = 0
        with mock.patch.object(join, "wait_for", side_effect=wait_for) as waited, \
             mock.patch.object(prompts, "find_ssid", return_value=network), \
             mock.patch.object(join, "on_a_device_network", return_value=joined_already), \
             mock.patch.object(join, "ask_credentials", return_value=("NewHome", "pw")), \
             mock.patch.object(prompts, "UNATTENDED", unattended), \
             mock.patch.object(join, "wait_until_joined"), \
             mock.patch.object(time, "sleep"), \
             mock.patch.object(setup_network, "send_esphome_wifi", side_effect=send) as sent, \
             mock.patch.object(prompts, "section"), mock.patch.object(output, "tell"):
            try:
                result = mover.wait_for_mover(ctx, "the mover")
            except errors.Abort as exc:
                result = exc
            self.home_waits = waited.call_count
        return result, sent, adopted

    def test_a_mover_on_the_home_wifi_is_used_as_ever(self):
        result, sent, _ = self.wait([True])
        self.assertIs(result, False)
        sent.assert_not_called()

    def test_after_a_router_outage_the_runs_own_details_do(self):
        result, sent, adopted = self.wait([False, True], network="autoflash-recovery-CDEF")
        self.assertIs(result, False)  # the same details: ESPHome has them already
        sent.assert_called_once_with("Home", "pw0")
        self.assertEqual(adopted, [])

    def test_new_details_when_those_fail_which_esphome_gets_too(self):
        result, sent, adopted = self.wait([False, False, True], network="autoflash-recovery-CDEF")
        self.assertIs(result, True)
        self.assertEqual([c.args for c in sent.call_args_list], [("Home", "pw0"), ("NewHome", "pw")])
        self.assertEqual(adopted, [("NewHome", "pw")])

    def test_unattended_it_stops_rather_than_asking(self):
        result, _, _ = self.wait([False, False], network="autoflash-recovery-CDEF", unattended=True)
        self.assertIsInstance(result, errors.Abort)
        self.assertIn("did not join 'Home'", str(result))

    def test_a_send_that_gets_no_answer_is_tried_again(self):
        result, sent, _ = self.wait([False, True], network="autoflash-recovery-CDEF",
                                    sends=("no answer", "no answer", None))
        self.assertIs(result, False)
        self.assertEqual(sent.call_count, 3)

    def test_joined_already_it_carries_on_through_that(self):
        result, sent, _ = self.wait([True], network="autoflash-recovery-CDEF", joined_already=True)
        self.assertIs(result, False)
        sent.assert_called_once()
        self.assertEqual(self.home_waits, 1)  # only after sending: no wait first

    def test_neither_says_to_switch_it_off_and_on(self):
        result, sent, _ = self.wait([False], network=None)
        self.assertIsInstance(result, errors.Abort)
        self.assertIn("switch the device off and on", str(result))
        self.assertIn("autoflash-recovery-CDEF", str(result))

    def test_the_details_go_to_esphomes_own_page(self):
        from unittest import mock
        from tools.autoflash import setup_network
        with mock.patch.object(setup_network, "page", return_value="ok") as page:
            setup_network.send_esphome_wifi("My Wi-Fi", "p&ss")
        self.assertEqual(page.call_args.args[1], "/wifisave?ssid=My+Wi-Fi&psk=p%26ss")


if __name__ == "__main__":
    unittest.main()
