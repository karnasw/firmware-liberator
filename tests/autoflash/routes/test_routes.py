"""Which family's steps a run takes: its kind's."""
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.routes.espressif.test_espressif import SUPLA, RouteTestCase
from tools.autoflash import cli, errors, kinds, prompts, routes
from tools.autoflash.device import DeviceError
from tools.autoflash.routes import beken, espressif, update
from tools.autoflash.routes.espressif import identity
from tools.autoflash.state import DONE, RunState


def a_kind(key, family):
    """A made-up kind of device of `family`, for a run to record."""
    return kinds.Kind(key=key, family=family, chip="made-up", ecosystem="ewelink", name="Made Up",
                      maker="Nobody", short="MU", product=re.compile("MU"),
                      stock_model="MU-01", wrapper_model="MU", tasmota_gpio=(),
                      esphome_legacy_setup_prefix="", esphome_legacy_prefix="", layout="made-up",
                      has_meter=False, has_relay=True, sensors=())


class FamilyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # The route tables apply_target leaves behind are the test's to put back.
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")

    def state(self, **inputs):
        self.runs = getattr(self, "runs", 0) + 1
        state = RunState.open(Path(self.tmp.name) / f"run{self.runs}")
        for key, value in inputs.items():
            state.set_input(key, value)
        return state

    def test_a_run_takes_its_kinds_familys_steps(self):
        # A made-up family: its own steps, its own names for them.
        step = ("Z1", "Something of its own", lambda ctx: {})
        family = SimpleNamespace(FIRMWARES={"esphome": "ESPHome"}, SOURCES=("sonoff",),
                                 steps_for=lambda firmware, source="sonoff": [step],
                                 STAGE_NAMES={"Z1": "Doing its own thing"}, TARGET_STAGE_NAMES={},
                                 POINT_OF_NO_RETURN="Z1", RESUME_CHECK=None)
        other = a_kind("made-up", "madeup")
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}):
            state = self.state(device_kind="made-up", firmware="esphome", source="sonoff")
            self.assertIs(routes.family_of(state), family)
            self.assertEqual([s for s, _, _ in routes.steps_of(state)], ["A0", "A1", "A3", "Z1"])
            self.assertFalse(routes.past_the_point_of_no_return(state))
            state.mark("Z1", "done")
            self.assertTrue(routes.past_the_point_of_no_return(state))
            routes.apply_target("esphome", "sonoff", family)
            self.assertIs(routes.FAMILY, family)
            self.assertEqual(routes.stage_name("Z1"), "Doing its own thing")
        # An S60's run, and one from before kinds were recorded, take the Espressif steps:
        # the shared start until the firmware is chosen, then the route.
        for state in (self.state(device_kind="sonoff-s60"), self.state()):
            self.assertIs(routes.family_of(state), espressif)
            self.assertEqual([s for s, _, _ in routes.steps_of(state)], ["A0", "A1", "A3"])
            state.set_input("firmware", "tasmota")
            self.assertEqual(routes.steps_of(state), routes.steps_for("tasmota", "sonoff", espressif))
        # A run of 1.x recorded its steps and no choice: it was for Tasmota.
        legacy = self.state()
        legacy.mark("C2", "done")
        self.assertEqual([s for s, _, _ in routes.steps_of(legacy)][-1], "H1")

    def test_a_run_from_sonoff_lays_its_stages_out_the_same_before_and_after_its_route_is_known(self):
        # The kind of device, and with it the rest of the route, is known only
        # after A1 and A3: the same columns before and after, and no total.
        begun, layouts = [], []
        with mock.patch.object(routes.output, "stage_begin", side_effect=lambda *a: begun.append(a)), \
                mock.patch.object(routes.output, "set_stage_width", side_effect=lambda *a: layouts.append(a)):
            routes.apply_target(None, "sonoff", beken)
            routes.begin_stage("A3")
            routes.apply_target("openbeken", "sonoff", beken)
            routes.begin_stage("B1")
            routes.apply_target("esphome", "sonoff", espressif)
            routes.begin_stage("A3")
        self.assertEqual(begun, [(3, "Choosing the firmware"), (4, "Querying Sonoff firmware"),
                                 (3, "Choosing the firmware")])
        self.assertEqual(len(set(layouts)), 1)
        longest = max(len(routes.steps_for(firmware, "sonoff", family)) for family in (espressif, beken)
                      for firmware in family.FIRMWARES if firmware != "sonoff")
        self.assertEqual(layouts[0][1], longest)

    def test_a_run_from_sonoff_lays_its_stages_out_for_the_readout_it_may_turn_into(self):
        # Seen in an attended emulator run: a run's readout (readout.from_run)
        # drew its stages a column wider than the run's own above them.
        from tools.autoflash import readout
        routes.apply_target(None, "sonoff", espressif)
        self.assertGreaterEqual(routes.stage_layout()[0], max(len(name) for _, name in readout.STAGES))

    def test_a_converted_devices_stages_are_laid_out_alike_before_and_after_its_firmware_is_chosen(self):
        # Seen in the emulator, from ESPHome to Tasmota: stages 1 and 2
        # drawn for a route of two, the rest for one of fifteen.
        for family in (espressif, beken):
            for source in family.SOURCES:
                for firmware in family.FIRMWARES:
                    if source == "sonoff" or not routes.routable(firmware, source) \
                            or not routes.steps_for(firmware, source, family):
                        continue
                    with self.subTest(family=family.__name__, source=source, firmware=firmware):
                        # An update is laid out before as what it updates.
                        before = firmware if source == "outdated" else source
                        routes.apply_target(None, before, family)
                        unchosen = routes.stage_layout()
                        routes.apply_target(firmware, source, family)
                        self.assertEqual(routes.stage_layout(), unchosen)
                        self.assertGreaterEqual(unchosen[1], len(routes.STEPS))

    def test_before_you_start_lists_what_is_needed_for_any_device(self):
        for source, needed in (("sonoff", "- Your device set up in the app and connected to Wi-Fi."),
                               ("tasmota", "- Your device connected to Wi-Fi, as it is now.")):
            shown = []
            ctx = SimpleNamespace(state=self.state(source=source))
            with mock.patch.object(routes.prompts, "section", side_effect=lambda title, body: shown.append((title, body))), \
                    mock.patch.object(routes.prompts, "wait_for_key"), \
                    mock.patch.object(routes.output, "transient_begin"), mock.patch.object(routes.output, "transient_end"):
                routes.step_prerequisites(ctx)
            title, body = shown[0]
            self.assertEqual(title, "Before you start, you'll need:")
            self.assertIn(needed, body)
            self.assertEqual(body[-1], "- This computer on the same network as the device.")
            self.assertFalse(any("plug" in line for line in body))
            # The device is found through eWeLink, whatever its app settings.
            self.assertFalse(any("LAN Control" in line for line in body))

    def test_every_family_gives_the_runner_the_same_names(self):
        for name, family in routes.FAMILIES.items():
            with self.subTest(family=name):
                for attribute in ("FIRMWARES", "SOURCES", "steps_for", "POINT_OF_NO_RETURN",
                                  "DESTRUCTIVE_STEPS", "RETRY_STEPS", "ERASED_CHECK_STEPS", "STAGE_NAMES",
                                  "TARGET_STAGE_NAMES", "PLAIN_DESCRIPTION", "ATTEMPTED_EVIDENCE",
                                  "STEP_VERIFIERS", "SONOFF_FROM_BACKUP", "RESUME_CHECK",
                                  "published_version"):
                    self.assertTrue(hasattr(family, attribute), attribute)
                if family.SONOFF_FROM_BACKUP:
                    self.assertTrue(callable(family.sonoff_without_backup))
                # Every step of every route has a name for its stage line and a plain description:
                # from an earlier release of this tool's firmware, only to this one's.
                kind = next(kind for kind in kinds.ALL if routes.FAMILIES[kind.family] is family)
                steps = {step for firmware in family.FIRMWARES for source in family.SOURCES
                         if source != "outdated" or family.published_version(kind, firmware)
                         for step, _, _ in family.steps_for(firmware, source)}
                self.assertEqual(steps - set(family.STAGE_NAMES), set())
                self.assertEqual(steps - set(family.PLAIN_DESCRIPTION), set())
                self.assertIn(family.POINT_OF_NO_RETURN, family.DESTRUCTIVE_STEPS)

    def test_the_command_line_offers_every_familys_firmware(self):
        for family in routes.FAMILIES.values():
            self.assertLessEqual(set(family.FIRMWARES), set(routes.FIRMWARES))

    def test_a_firmware_another_family_takes_is_refused_for_this_device(self):
        other = a_kind("made-up", "madeup")
        family = SimpleNamespace(FIRMWARES={"openbeken": "OpenBeken", "sonoff": "Sonoff"}, ONLY_FOR={},
                                 SONOFF_FROM_BACKUP=True,
                                 published_version=lambda kind, firmware: None,
                                 sonoff_without_backup=lambda ctx, source: None)
        state = self.state(device_kind="made-up")
        ctx = SimpleNamespace(state=state, replace_own_build=False, firmware_given="tasmota", esphome_firmware=None)
        with mock.patch.object(kinds, "ALL", (kinds.SONOFF_S60, other)), \
                mock.patch.dict(routes.FAMILIES, {"madeup": family}), \
                mock.patch.object(routes.source, "find_current_firmware", return_value="sonoff"):
            # From Sonoff's firmware the kind is known only at A1; A3 refuses.
            self.assertEqual(routes.settle_route(ctx), (None, "sonoff"))
            with self.assertRaisesRegex(routes.Abort, "a Made Up takes OpenBeken"):
                routes.step_choose(ctx)
            # A converted device's kind is known when the route is settled.
            family.FIRMWARES["esphome"] = "ESPHome"
            converted = SimpleNamespace(state=self.state(device_kind="made-up"), replace_own_build=False, firmware_given="tasmota",
                                        esphome_firmware=None)
            with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"):
                with self.assertRaisesRegex(routes.Abort, "takes OpenBeken or Sonoff or ESPHome"):
                    routes.settle_route(converted)


class ReadoutLeftTests(unittest.TestCase):
    def test_a_device_on_the_readout_firmware_gets_sonoffs_back_and_the_run_ends(self):
        from tools.autoflash import readout_beken
        state = SimpleNamespace(input={"device_ip": "192.0.2.60"}.get, require=lambda *keys: ["192.0.2.60"],
                                is_done=lambda step: False)
        ctx = SimpleNamespace(state=state, replace_own_build=False, firmware_given="openbeken")
        with mock.patch.object(routes.source, "find_current_firmware", return_value="readout"), \
                mock.patch.object(readout_beken, "put_back", return_value="Sonoff's firmware is back.") as back:
            with self.assertRaisesRegex(routes.NothingToDo, "Sonoff's firmware is back"):
                routes.settle_route(ctx)
        self.assertEqual(back.call_args.args[1], "192.0.2.60")


class BekenTargetTests(unittest.TestCase):
    """An S61 goes back to Sonoff's firmware from the public dump, not a
    backup; and a run of one that stopped before its firmware was chosen is
    resumed as that, not as an S60's of 1.x (both seen in the fault suite)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def state(self, **inputs):
        state = RunState.open(Path(self.tmp.name) / "run")
        for key, value in inputs.items():
            state.set_input(key, value)
        return state

    def test_an_s61_on_esphome_can_go_back_to_sonoff_without_a_backup(self):
        ctx = SimpleNamespace(state=self.state(device_kind="sonoff-s61", device_mac="12:34:56:ab:cd:ef"),
                              replace_own_build=False, firmware_given="sonoff", esphome_firmware=None)
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(routes.records, "find_backup", return_value=None), \
                mock.patch.object(routes.output, "say"):
            self.assertEqual(routes.settle_route(ctx), ("sonoff", "esphome"))

    def test_the_run_says_where_it_goes_from_whatever_the_device_runs(self):
        # Left on the startup firmware past E3 ("committed"), a device's run to
        # ESPHome crashed saying so: KeyError 'committed' (the fault suite's
        # esphome>tasmota:E3:cancel-switch). Part way, there is no firmware to
        # name, only where it goes.
        from tools.autoflash.routes import espressif
        from tools.autoflash.routes import update
        # "outdated" is no device found, but a firmware the route finds older (routes/update.py).
        for running in set(espressif.SOURCES) - {"sonoff", "esphome", "outdated"}:
            with self.subTest(running=running):
                state = RunState.open(Path(self.tmp.name) / running)
                state.set_input("device_kind", "sonoff-s60")
                state.set_input("device_mac", "12:34:56:ab:cd:ef")
                ctx = SimpleNamespace(state=state, replace_own_build=False, firmware_given="esphome", esphome_firmware=None)
                with mock.patch.object(routes.source, "find_current_firmware", return_value=running), \
                        mock.patch.object(routes.records, "find_backup", return_value=object()), \
                        mock.patch.dict(update.RELEASE, tasmota=lambda ctx: None), \
                        mock.patch.object(routes.espressif_source, "leaves_tasmota_direct", return_value=False), \
                        mock.patch.object(routes.output, "say") as said:
                    self.assertEqual(routes.settle_route(ctx), ("esphome", running))
                line = said.call_args.args[0]
                self.assertTrue(line.startswith("Converting the device ") and line.endswith(" to ESPHome."), line)
                if running in ("midway", "committed", "installer"):
                    self.assertEqual(line, "Converting the device to ESPHome.")

    def test_an_s60_with_no_backup_needs_its_identity(self):
        # On Tasmota, kept in its sector; failing that, or from ESPHome (whose
        # settings only the hub can read), asked for: from the account too.
        for running, kept, record, offered in (("tasmota", None, None, False), ("tasmota", "kept", None, True),
                                               ("tasmota", None, {"deviceid": "x"}, True),
                                               ("esphome", None, None, True)):
            with self.subTest(running=running, kept=kept, record=record):
                state = RunState.open(Path(self.tmp.name) / f"{running}-{kept}-{bool(record)}")
                state.set_input("device_kind", "sonoff-s60")
                state.set_input("device_mac", "12:34:56:ab:cd:ef")
                ctx = SimpleNamespace(state=state,
                                      replace_own_build=False, firmware_given="sonoff", esphome_firmware=None)
                with mock.patch.object(routes.source, "find_current_firmware", return_value=running), \
                        mock.patch.object(routes.records, "find_backup", return_value=None), \
                        mock.patch.object(identity, "kept_in_sector", return_value=kept), \
                        mock.patch.object(identity, "account_record", return_value=record), \
                        mock.patch.object(update, "outdated", return_value=False), \
                        mock.patch.object(routes.output, "say"):
                    if offered:
                        self.assertEqual(routes.settle_route(ctx), ("sonoff", running))
                    else:
                        with self.assertRaisesRegex(routes.Abort, "no backup"):
                            routes.settle_route(ctx)

    def test_sonoff_without_a_backup_uses_what_the_device_keeps_or_the_account(self):
        ctx = SimpleNamespace()
        # On Tasmota, the identity it keeps, read off it: no sign-in needed.
        with mock.patch.object(identity, "kept_in_sector", return_value="kept"), \
                mock.patch.object(identity, "account_record", side_effect=AssertionError("not asked")):
            self.assertIn("keeps", espressif.sonoff_without_backup(ctx, "tasmota"))
        for kept, record, expected in ((DeviceError("timed out"), {"deviceid": "x"}, "account"),
                                       (None, None, None)):
            with self.subTest(kept=kept, record=record), \
                    mock.patch.object(identity, "kept_in_sector", side_effect=[kept]), \
                    mock.patch.object(identity, "account_record", return_value=record):
                said = espressif.sonoff_without_backup(ctx, "tasmota")
                self.assertIn(expected, said) if expected else self.assertIsNone(said)
        # From ESPHome, what it keeps can be read only part way: counted on,
        # the account's record named as the fallback where it has one.
        for record in (None, {"deviceid": "x"}):
            with self.subTest(source="esphome", record=record), \
                    mock.patch.object(identity, "account_record", return_value=record):
                said = espressif.sonoff_without_backup(ctx, "esphome")
                self.assertIn("the Sonoff identity the device keeps", said)
                self.assertEqual("eWeLink account" in said, record is not None)

    def test_an_s61_run_with_no_firmware_chosen_has_the_start_alone(self):
        state = self.state(device_kind="sonoff-s61", source="esphome")
        self.assertIsNone(routes.recorded_firmware(state))
        self.assertEqual(routes.steps_of(state), routes.START)
        # An S60's of 1.x, past the start, was for Tasmota; stopped at A3, not yet.
        state = self.state(device_kind="sonoff-s60", source="esphome")
        state.mark("A0", DONE)
        self.assertIsNone(routes.recorded_firmware(state))
        state.mark("A5", DONE)
        self.assertEqual(routes.recorded_firmware(state), "tasmota")

    def test_a_converted_devices_firmware_is_asked_after_its_prerequisites(self):
        # As from Sonoff's firmware: the question comes at "Choosing the
        # firmware", once the list of what is needed has been read - not
        # before the first stage, ahead of it.
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        state = self.state(device_kind="sonoff-s61", device_mac="12:34:56:ab:cd:ef")
        ctx = SimpleNamespace(state=state, replace_own_build=False, firmware_given=None, esphome_firmware=None,
                              firmware_choice=None)
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"), \
                mock.patch.object(routes.records, "find_backup", return_value=object()), \
                mock.patch.object(routes, "ask_firmware", return_value="openbeken") as asked, \
                mock.patch.object(routes.output, "say") as said:
            self.assertEqual(routes.settle_route(ctx, defer=True), (None, "esphome"))
            asked.assert_not_called()
            said.assert_not_called()
            self.assertEqual(state.input("source"), "esphome")
            self.assertEqual([step for step, _, _ in routes.steps_of(state)], ["A0", "A3"])
            self.assertEqual(routes.step_choose(ctx), {"firmware": "openbeken", "asked": True})
            asked.assert_called_once()
        self.assertEqual(said.call_args.args[0], "Converting the device from ESPHome to OpenBeken.")
        self.assertEqual(state.input("firmware"), "openbeken")
        self.assertEqual([step for step, _, _ in routes.STEPS][:3], ["A0", "A3", "E1"])
        self.assertIsNone(ctx.firmware_choice)

    def test_going_on_instead_keeps_the_steps_taken_and_numbers_on(self):
        # R1 with nothing to put Sonoff's firmware together from: back to
        # ESPHome from the interim Tasmota, the stages numbered on from R1's.
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        routes.apply_target("sonoff", "esphome", espressif)
        routes.go_on_instead("R1", "esphome", "midway")
        self.assertEqual([step for step, _, _ in routes.STEPS],
                         ["A0", "A3", "A5", "X1", "X2", "D4", "D5", "R1", "K1", "I1", "I2"])
        self.assertEqual(routes.ROUTE, ("esphome", "midway"))
        self.assertNotIn("R2", routes.STEP_INDEX)

    def test_kept_on_tasmota_at_a3_the_run_ends_with_nothing_to_do(self):
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")
        ctx = SimpleNamespace(state=self.state(device_kind="sonoff-s60", device_mac="12:34:56:ab:cd:ef", source="tasmota"),
                              replace_own_build=False, firmware_given=None, esphome_firmware=None,
                              firmware_choice=routes.FirmwareChoice("tasmota", "tasmota", ["tasmota", "esphome"]))
        with mock.patch.object(routes, "ask_firmware", return_value="tasmota"), \
                self.assertRaisesRegex(errors.Finished, "already runs Tasmota"):
            routes.step_choose(ctx)
        self.assertEqual(routes.steps_of(ctx.state), [])  # complete: nothing to carry on


class StepTableTests(unittest.TestCase):
    def test_the_docs_table_has_each_familys_steps_from_sonoff(self):
        """docs/what-happens.md lists both families' steps side by side, by ID."""
        text = (Path(__file__).resolve().parents[3] / "docs" / "what-happens.md").read_text(encoding="utf-8")
        start = text.index("| | An Espressif device | A Beken device |")
        rows = [line.split("|")[2:4] for line in text[start:].splitlines()[2:] if line.startswith("|")]
        for column, (family, firmwares) in enumerate(((espressif, ("tasmota", "esphome")),
                                                     (beken, ("openbeken", "esphome")))):
            listed = {code for row in rows for code in re.findall(r"`([A-Z]\d)`", row[column])}
            steps = {step for firmware in firmwares for step, _, _ in routes.steps_for(firmware, "sonoff", family)}
            self.assertEqual(listed, steps, family.__name__)


class KindTests(unittest.TestCase):
    def test_every_kind_names_a_family_the_routes_know(self):
        for kind in kinds.ALL:
            self.assertIn(kind.family, routes.FAMILIES, kind.key)
            self.assertEqual(kind.ecosystem, "ewelink")


class FirmwareChoiceTests(RouteTestCase):
    """From Sonoff's firmware the choice waits for A1 to say what kind of device
    it is (step_choose, A3); a converted device's kind is known when the route
    is settled."""

    def setUp(self):
        super().setUp()
        self.setUp_found()
        self.addCleanup(routes.apply_target, "tasmota", "sonoff")

    def test_it_is_asked_once_the_device_is_known_then_kept(self):
        ctx = self.ctx()
        self.assertEqual(routes.settle_route(ctx), (None, "sonoff"))
        self.assertEqual([step for step, _, _ in routes.steps_of(ctx.state)], ["A0", "A1", "A3"])
        with mock.patch.object(prompts, "ask_choice", return_value=1) as asked:
            self.assertEqual(routes.step_choose(ctx)["firmware"], "esphome")
        self.assertEqual(asked.call_args.args[1], ["Tasmota", "ESPHome", SUPLA])
        self.assertEqual(ctx.state.input("firmware"), "esphome")
        self.assertEqual([step for step, _, _ in routes.STEPS][-2:], ["I1", "I2"])
        with mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked again")):
            self.assertEqual(routes.step_choose(ctx)["firmware"], "esphome")
            self.assertEqual(routes.settle_firmware(ctx), "esphome")

    def test_given_on_the_command_line_it_is_not_asked(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(routes.step_choose(ctx), {"firmware": "esphome", "asked": False})

    def test_a_firmware_the_devices_family_does_not_take_is_refused(self):
        ctx = self.ctx(given="openbeken")
        with self.assertRaisesRegex(errors.Abort, "a Sonoff S60 takes Tasmota or ESPHome"):
            routes.step_choose(ctx)
        with self.assertRaisesRegex(errors.Abort, "already runs Sonoff"):
            routes.step_choose(self.ctx(given="sonoff"))

    def test_supla_is_only_for_the_device_its_route_knows(self):
        # Its route knows the S60's layout and template alone (tools/autoflash/supla.py).
        for kind in kinds.EVERY:
            self.assertEqual("supla" in routes.firmwares_of(kind), kind is kinds.SONOFF_S60, kind.key)
        ctx = self.ctx(given="supla", device_kind=kinds.SONOFF_POW_ELITE.key)
        with self.assertRaisesRegex(errors.Abort, "cannot go to Supla: a Sonoff POW Elite 16A takes "
                                                  "Tasmota or ESPHome$"):
            routes.step_choose(ctx)

    def test_a_conversion_cannot_switch_part_way(self):
        ctx = self.ctx(given="tasmota", firmware="esphome")
        with self.assertRaisesRegex(errors.Abort, "cannot switch"):
            routes.settle_firmware(ctx)

    def test_a_run_begun_before_there_was_a_choice_installs_tasmota(self):
        ctx = self.ctx()
        ctx.state.mark("A1", DONE)
        with mock.patch.object(prompts, "ask_choice", side_effect=AssertionError("asked")):
            self.assertEqual(routes.settle_firmware(ctx), "tasmota")

    def test_an_unattended_run_must_be_told(self):
        ctx = self.ctx()
        with mock.patch.object(prompts, "UNATTENDED", True):
            with self.assertRaisesRegex(errors.Abort, "--firmware esphome"):
                routes.step_choose(ctx)
        self.assertIsNone(ctx.state.input("firmware"))


class AlreadyThereTests(RouteTestCase):
    def test_asking_for_what_it_runs_ends_the_run_done(self):
        ctx = self.ctx(given="esphome")
        with mock.patch.object(routes.source, "find_current_firmware", return_value="esphome"):
            with self.assertRaisesRegex(routes.NothingToDo, "already runs ESPHome"):
                routes.settle_route(ctx)
        # Never offered back as an unfinished run.
        self.assertTrue(cli.run_is_complete(Path(self.tmp.name) / "run"))

