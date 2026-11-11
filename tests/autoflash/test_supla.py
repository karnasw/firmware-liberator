"""Supla GUI-Generic: the image the builder gave, the layout it runs on, and
the plans and steps that put it on the plug."""
import hashlib
import json
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.autoflash.test_imprint import build_image
from tools.autoflash import esphome, kinds, plans, supla
from tools.autoflash.errors import Abort
from tools.autoflash.routes import espressif
from tools.autoflash import join, prompts
from tools.autoflash.routes.espressif import evidence, hub
from tools.autoflash.routes.espressif import supla as route
from tools.autoflash.state import RunState

ROOT = Path(__file__).resolve().parents[2]


def template(gpio=kinds.SONOFF_S60.tasmota_gpio) -> bytes:
    return json.dumps({"NAME": "S60", "GPIO": list(gpio), "FLAG": 0, "BASE": 1},
                      separators=(",", ":")).encode()


def gui_generic(*, extra=b"", markers=supla.MARKERS, size=0) -> bytes:
    """A made-up GUI-Generic app: an ESP32-C3 app description, Supla's
    markers and a device template - none of GUI-Generic itself."""
    description = struct.pack("<I", esphome.APP_DESC_MAGIC) + b"\0" * 12 + b"6671d0b".ljust(32, b"\0")
    description += b"arduino-lib-builder".ljust(32, b"\0") + b"\0" * 32 + b"v5.5.5".ljust(32, b"\0")
    payload = description + b"\0".join(markers) + b"\0" + extra
    return build_image(payload.ljust(size, b"\0"))


class LayoutTests(unittest.TestCase):
    def test_the_table_is_written_as_esp_idf_writes_one(self):
        # Sonoff's own table, read and written again, is the same to the byte.
        sonoff = esphome.sonoff_table()
        self.assertEqual(supla.encode_table(supla.decode_table(sonoff)), sonoff)

    def test_the_layout_is_pinned(self):
        self.assertEqual(hashlib.sha256(supla.table()).hexdigest(), plans.SUPLA_TABLE_SHA256)
        self.assertEqual(len(supla.table()), plans.SECTOR)

    def test_it_keeps_sonoffs_partitions_where_they_are(self):
        # The settings, the boot selection and both slots stay where Sonoff
        # had them: the Tasmota running from ota_1 still starts, cut off or not.
        sonoff = supla.decode_table(esphome.sonoff_table())
        layout = supla.decode_table(supla.table())
        self.assertEqual(layout[:5], sonoff[:5])
        self.assertEqual(layout[5][:4], sonoff[5][:4])
        self.assertEqual(layout[5][4], plans.SUPLA_OTA1_SIZE)

    def test_it_has_what_gui_generic_looks_for_and_fills_the_flash(self):
        layout = {label: (kind, subtype, address, size) for label, kind, subtype, address, size, _ in
                  supla.decode_table(supla.table())}
        self.assertEqual(layout["spiffs"], (supla.DATA, 0x82, plans.SUPLA_TAIL, supla.LITTLEFS_SIZE))
        self.assertEqual(layout["coredump"][:2], (supla.DATA, 0x03))
        spans = sorted((address, address + size) for _, _, address, size in layout.values())
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start)
        self.assertEqual(spans[-1][1], 0x400000)
        for label in ("ota_0", "ota_1"):
            self.assertEqual(layout[label][2] % 0x10000, 0, label)

    def test_the_berry_tasmota_fits_the_smaller_ota_1(self):
        # It runs from ota_1 while Supla is written, and starts from it after
        # a cut before the switch: the tail cleared for Supla is past its end.
        berry = (ROOT / "artifacts" / "tasmota32c3-bluetooth.bin").stat().st_size
        self.assertLessEqual(plans.SONOFF_OTA[1] + berry, plans.SUPLA_TAIL)


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def test_a_gui_generic_app_is_taken(self):
        supla.check(gui_generic(), "made-up.bin")

    def test_anything_else_is_refused(self):
        with self.assertRaisesRegex(supla.ImageError, "not Supla's GUI-Generic"):
            supla.check(gui_generic(markers=(b"ESPHome",)), "esphome.bin")
        with self.assertRaisesRegex(supla.ImageError, "not an ESP32 firmware image"):
            supla.check(b"\xaa\x50" + b"\xff" * 3070, "partitions.bin")
        broken = bytearray(gui_generic())
        broken[40] ^= 1
        with self.assertRaisesRegex(supla.ImageError, "integrity"):
            supla.check(bytes(broken), "broken.bin")

    def test_one_too_big_for_the_smaller_slot_is_refused(self):
        # An update GUI-Generic takes later goes into ota_1.
        with self.assertRaisesRegex(supla.ImageError, "each of its two slots"):
            supla.check(gui_generic(size=supla.OTA1_SIZE), "big.bin")

    def test_the_builders_zip_gives_its_one_app(self):
        archive = self.dir / "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin.zip"
        app = gui_generic()
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin", app)
            zipped.writestr("bootloader.bin", build_image(b"no app description"))
            zipped.writestr("partitions.bin", supla.table())
            zipped.writestr("boot_app0.bin", b"\xff" * 0x2000)
            zipped.writestr("README.txt", "flash at 0x10000")
        self.assertEqual(supla.load(archive), (app, "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin"))

    def test_a_zip_of_two_apps_is_refused(self):
        archive = self.dir / "two.zip"
        with zipfile.ZipFile(archive, "w") as zipped:
            zipped.writestr("a.bin", gui_generic())
            zipped.writestr("b.bin", gui_generic())
        with self.assertRaisesRegex(supla.ImageError, "2 firmware apps"):
            supla.load(archive)

    def test_its_template_is_compared_with_the_devices_wiring(self):
        kind = kinds.SONOFF_S60
        self.assertIs(supla.template_matches(gui_generic(extra=template()), kind), True)
        self.assertIs(supla.template_matches(gui_generic(extra=template((0,) * 22)), kind), False)
        self.assertIsNone(supla.template_matches(gui_generic(), kind))

    def test_the_builders_release_is_read_from_the_file_name(self):
        self.assertEqual(supla.builder_version("GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin"),
                         "NX26.11.08.2")
        self.assertIsNone(supla.builder_version("firmware.bin"))


class PlanTests(unittest.TestCase):
    def test_the_biggest_image_is_two_plans_each_inside_the_busy_limit(self):
        first, second = plans.supla(b"\xe9" * plans.SUPLA_OTA1_SIZE, supla.table())
        for plan in (first, second):
            self.assertLessEqual(plan.busy_estimate(), plans.BUSY_LIMIT, plan.name)
            self.assertEqual((plan.table_sha256, plan.running), (plans.SONOFF_TABLE_SHA256, 1))

    def test_the_first_only_writes_the_app_and_the_second_switches_last(self):
        image = gui_generic(size=0x180000)
        first, second = plans.supla(image, supla.table())
        self.assertFalse(first.restart)
        self.assertEqual(first.final_table_sha256(), plans.SONOFF_TABLE_SHA256)
        written = b"".join(plan.blobs["supla"][step.start:step.start + plan.span(step)[1]]
                           for plan in (first, second) for step in plan.steps
                           if isinstance(step, plans.Write) and step.blob == "supla")
        self.assertEqual(written, image)
        self.assertEqual(second.steps[-2:], [plans.Write("table", plans.TABLE_ADDRESS, whole=True),
                                             plans.Erase(plans.SONOFF_OTADATA, plans.OTADATA_SIZE)])
        self.assertTrue(second.restart)
        self.assertEqual(second.final_table_sha256(), plans.SUPLA_TABLE_SHA256)

    def test_nothing_is_written_where_the_tasmota_runs(self):
        ota1 = (plans.SONOFF_OTA[1], plans.SONOFF_OTA[1] + plans.SUPLA_OTA1_SIZE)
        for plan in plans.supla(gui_generic(size=0x180000), supla.table()):
            for start, end, step in plan.regions():
                self.assertFalse(start < ota1[1] and ota1[0] < end, step)

    def test_a_plan_travels_whole(self):
        _, second = plans.supla(gui_generic(size=0x10000), supla.table())
        again = plans.Plan.from_bundle(second.bundle())
        self.assertEqual(again.expected(), second.expected())

    def test_anything_but_its_table_or_an_image_too_big_is_refused(self):
        with self.assertRaisesRegex(plans.PlanError, "Supla's partition table"):
            plans.supla(gui_generic(size=0x10000), esphome.sonoff_table())
        with self.assertRaisesRegex(plans.PlanError, "fit"):
            plans.supla(b"\xe9" * (plans.SUPLA_OTA1_SIZE + 1), supla.table())

    def test_the_split_stays_inside_an_image_of_any_size(self):
        # Rounded down to a sector: rounded up, a small image ran past its end.
        for size in (plans.SECTOR + 1, 0x2800, 0x10001):
            first, second = plans.supla(b"\xe9" * size, supla.table())
            self.assertEqual(first.span(first.steps[0])[1] + second.span(second.steps[0])[1], size)
        with self.assertRaisesRegex(plans.PlanError, "too small"):
            plans.supla(b"\xe9" * plans.SECTOR, supla.table())


class RouteTests(unittest.TestCase):
    def test_its_image_is_checked_before_anything_is_written(self):
        for source in ("sonoff", "tasmota", "esphome", "midway", "installer", "committed"):
            steps = [step for step, _, _ in espressif.steps_for("supla", source)]
            self.assertEqual(steps[:2], ["A5", "S0"], source)
            self.assertEqual(steps[-2:], ["S1", "S2"], source)
            self.assertEqual(steps.count("S0"), 1, source)

    def test_its_install_is_asked_about_when_cut_off(self):
        self.assertIn("S1", espressif.DESTRUCTIVE_STEPS)
        self.assertIs(evidence.ATTEMPTED_EVIDENCE["S1"], route.evidence_supla_installed)
        self.assertEqual(espressif.TARGET_STAGE_NAMES["supla"]["D4"], "Making room for Supla")


class StepTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def ctx(self, image: bytes, **extra):
        path = self.dir / "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin"
        path.write_bytes(image)
        state = RunState.open(self.dir / "run")
        state.set_input("plug_kind", kinds.SONOFF_S60.key)
        return SimpleNamespace(state=state, supla_firmware=path, dry_run=False, device=None, **extra)

    def test_the_image_is_recorded_once_checked(self):
        image = gui_generic(extra=template())
        ctx = self.ctx(image)
        result = route.step_image(ctx)
        self.assertEqual(result["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual((result["builder_version"], result["template"]), ("NX26.11.08.2", True))
        self.assertEqual(ctx.state.input("supla_image"), str(ctx.supla_firmware))

    def test_another_devices_build_is_refused(self):
        with self.assertRaisesRegex(Abort, "another device's template"):
            route.step_image(self.ctx(gui_generic(extra=template((0,) * 22))))

    def test_a_file_changed_since_it_was_checked_is_refused(self):
        ctx = self.ctx(gui_generic(extra=template()))
        route.step_image(ctx)
        ctx.supla_firmware.write_bytes(gui_generic(extra=template() + b"changed"))
        with self.assertRaisesRegex(Abort, "has changed since"):
            route.checked_image(ctx)

    def test_the_first_plan_is_not_run_again_once_it_finished(self):
        ctx = self.ctx(gui_generic(extra=template(), size=0x100000))
        route.step_image(ctx)
        with mock.patch.object(join, "plug_signal", return_value=None), \
                mock.patch.object(join, "note_signal", return_value=None), \
                mock.patch.object(hub, "run_plan", side_effect=[{}, RuntimeError("cut off")]) as run:
            with self.assertRaises(RuntimeError):
                route.step_supla_install(ctx)
        self.assertEqual([call.args[1].name for call in run.call_args_list], ["supla_app", "supla_rest"])
        with mock.patch.object(join, "plug_signal", return_value=None), \
                mock.patch.object(join, "note_signal", return_value=None), \
                mock.patch.object(hub, "run_plan", return_value={"plan": "supla_rest"}) as run:
            route.step_supla_install(ctx)
        self.assertEqual([call.args[1].name for call in run.call_args_list], ["supla_rest"])


    def test_supla_not_seen_on_the_wifi_still_ends_the_run(self):
        # It is written and checked; setting it up is GUI-Generic's own page's.
        ctx = self.ctx(gui_generic())
        ctx.state.set_input("plug_ip", "192.0.2.50")
        with mock.patch.object(prompts, "wait_for_ssid", return_value="SUPLA-ESP32C3-ABCD"), \
                mock.patch.object(prompts, "section") as shown, \
                mock.patch.object(prompts, "wait_for_key"):
            with mock.patch.object(join, "wait_for", side_effect=Abort("not seen")):
                self.assertEqual(route.step_supla_check(ctx), {"network": "SUPLA-ESP32C3-ABCD", "seen": False})
            with mock.patch.object(join, "wait_for", return_value=True):
                self.assertEqual(route.step_supla_check(ctx), {"network": "SUPLA-ESP32C3-ABCD", "seen": True})
        self.assertIn("    SUPLA-ESP32C3-ABCD      (open - no password)", shown.call_args.args[1])

    def test_supla_seen_nowhere_is_not_called_converted(self):
        # As in the emulator, which cannot run GUI-Generic: it restarts over
        # and over, raising no network and answering nothing.
        ctx = self.ctx(gui_generic())
        ctx.state.set_input("plug_ip", "192.0.2.50")
        with mock.patch.object(prompts, "wait_for_ssid", return_value=None), \
                mock.patch.object(prompts, "section"), mock.patch.object(prompts, "wait_for_key"), \
                mock.patch.object(join, "wait_for", side_effect=Abort("not seen")):
            with self.assertRaisesRegex(Abort, "neither its own network"):
                route.step_supla_check(ctx)


if __name__ == "__main__":
    unittest.main()
