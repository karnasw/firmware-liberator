"""Supla GUI-Generic: the image the builder gave, the layout it runs on, and
the plans and steps that put it on the device."""
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
from tools.autoflash import kinds, plans, supla
from tools.autoflash.errors import Abort
from tools.autoflash.routes import espressif
from tools.autoflash import join, prompts
from tools.autoflash.routes.espressif import evidence, hub, images
from tools.autoflash.routes.espressif import supla as route
from tools.autoflash.state import RunState

ROOT = Path(__file__).resolve().parents[2]


def template(gpio=kinds.SONOFF_S60.tasmota_gpio) -> bytes:
    return json.dumps({"NAME": "S60", "GPIO": list(gpio), "FLAG": 0, "BASE": 1},
                      separators=(",", ":")).encode()


def gui_generic(*, extra=b"", markers=supla.MARKERS, size=0) -> bytes:
    """A made-up GUI-Generic app: an ESP32-C3 app description, Supla's
    markers and a device template - none of GUI-Generic itself."""
    description = struct.pack("<I", images.APP_DESC_MAGIC) + b"\0" * 12 + b"6671d0b".ljust(32, b"\0")
    description += b"arduino-lib-builder".ljust(32, b"\0") + b"\0" * 32 + b"v5.5.5".ljust(32, b"\0")
    payload = description + b"\0".join(markers) + b"\0" + extra
    return build_image(payload.ljust(size, b"\0"))


def bootloader() -> bytes:
    """A made-up ESP32-C3 bootloader: an image with no app description."""
    return build_image(b"made-up second stage bootloader".ljust(0x4000, b"\0"))


def boot_app0() -> bytes:
    """A boot selection starting app0, as the builder's boot_app0.bin."""
    return plans.otadata_selecting(0)


def builder_zip(path: Path, app: bytes, **replaced) -> Path:
    """The builder's .zip, as it gives one; `replaced` swaps or (None) drops a part."""
    parts = {"GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin": app,
             "bootloader.bin": bootloader(), "partitions.bin": supla.table()[:0xC00],
             "boot_app0.bin": boot_app0()}
    parts.update(replaced)
    with zipfile.ZipFile(path, "w") as zipped:
        for name, data in parts.items():
            if data is not None:
                zipped.writestr(name, data)
        zipped.writestr("README.txt", "flash at 0x10000")
    return path


def plan_for(image: bytes) -> list:
    return plans.supla(image, bootloader(), supla.table(), boot_app0())


class LayoutTests(unittest.TestCase):
    def test_the_table_is_written_as_esp_idf_writes_one(self):
        # Sonoff's own table, read and written again, is the same to the byte.
        sonoff = images.sonoff_table()
        self.assertEqual(supla.encode_table(supla.decode_table(sonoff)), sonoff)

    def test_the_layout_is_pinned(self):
        self.assertEqual(hashlib.sha256(supla.table()).hexdigest(), plans.SUPLA_TABLE_SHA256)
        self.assertEqual(len(supla.table()), plans.SECTOR)

    def test_it_has_what_gui_generic_looks_for_and_fills_the_flash(self):
        layout = {label: (kind, subtype, address, size) for label, kind, subtype, address, size, _ in
                  supla.decode_table(supla.table())}
        self.assertEqual(layout["app0"], (supla.APP, 0x10, plans.SUPLA_APP0, plans.SUPLA_APP_SIZE))
        self.assertEqual(layout["otadata"][2], plans.SUPLA_OTADATA)
        self.assertEqual(layout["nvs"][2:], plans.SUPLA_NVS)
        self.assertEqual(layout["spiffs"], (supla.DATA, 0x82, plans.SUPLA_TAIL, supla.LITTLEFS_SIZE))
        self.assertEqual(layout["coredump"][:2], (supla.DATA, 0x03))
        spans = sorted((address, address + size) for _, _, address, size in layout.values())
        for (_, end), (start, _) in zip(spans, spans[1:]):
            self.assertLessEqual(end, start)
        self.assertEqual(spans[-1][1], 0x400000)

    def test_the_berry_tasmota_ends_before_the_tail_cleared_for_supla(self):
        # It runs from Sonoff's ota_1 while Supla is written, and starts from
        # it after a cut before the switch.
        berry = (ROOT / "artifacts" / "tasmota32c3-bluetooth.bin").stat().st_size
        self.assertLessEqual(plans.SONOFF_OTA[1] + berry, plans.SUPLA_TAIL)

    def test_the_boot_selection_is_read_as_the_bootloader_reads_it(self):
        self.assertEqual(supla.selected_slot(plans.otadata_selecting(0)), 0)
        self.assertEqual(supla.selected_slot(plans.otadata_selecting(1)), 1)
        self.assertIsNone(supla.selected_slot(b"\xff" * supla.OTADATA_SIZE))
        # The builder's boot_app0.bin: its second entry's sequence is 0, its CRC erased.
        builders = bytearray(plans.otadata_selecting(0))
        builders[0x1000:0x1004] = b"\0" * 4
        self.assertEqual(supla.selected_slot(bytes(builders)), 0)


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def load(self, app=None, **replaced) -> supla.Build:
        return supla.load(builder_zip(self.dir / "build.zip", app or gui_generic(), **replaced))

    def test_the_builders_zip_gives_its_app_and_what_starts_it(self):
        app = gui_generic()
        build = self.load(app)
        self.assertEqual((build.app, build.name), (app, "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin"))
        self.assertEqual((build.bootloader, build.table, build.otadata), (bootloader(), supla.table(), boot_app0()))
        supla.check(build)

    def test_the_app_alone_is_refused(self):
        # Supla is installed with the builder's bootloader and layout.
        path = self.dir / "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin"
        path.write_bytes(gui_generic())
        with self.assertRaisesRegex(supla.ImageError, "give the .zip as it is"):
            supla.load(path)

    def test_a_zip_missing_a_part_is_refused(self):
        for part in ("bootloader.bin", "partitions.bin", "boot_app0.bin"):
            with self.assertRaisesRegex(supla.ImageError, f"has no {part}"):
                self.load(**{part: None})

    def test_a_zip_of_two_apps_is_refused(self):
        with self.assertRaisesRegex(supla.ImageError, "2 firmware apps"):
            self.load(**{"other.bin": gui_generic()})

    def test_a_gui_generic_app_is_taken(self):
        supla.check_app(gui_generic(), "made-up.bin")

    def test_any_other_app_is_refused(self):
        with self.assertRaisesRegex(supla.ImageError, "not Supla's GUI-Generic"):
            supla.check_app(gui_generic(markers=(b"ESPHome",)), "esphome.bin")
        broken = bytearray(gui_generic())
        broken[40] ^= 1
        with self.assertRaisesRegex(supla.ImageError, "integrity"):
            supla.check_app(bytes(broken), "broken.bin")

    def test_one_too_big_for_a_slot_is_refused(self):
        # An update GUI-Generic takes later goes into app1, as big as app0.
        with self.assertRaisesRegex(supla.ImageError, "each of its two slots"):
            supla.check_app(gui_generic(size=supla.APP_SIZE), "big.bin")

    def test_another_layout_is_refused(self):
        sonoffs = images.sonoff_table()
        with self.assertRaisesRegex(supla.ImageError, "layout this version of the tool does not know"):
            supla.check(self.load(**{"partitions.bin": sonoffs}))

    def test_a_boot_selection_for_the_other_slot_is_refused(self):
        with self.assertRaisesRegex(supla.ImageError, "does not start the app's slot"):
            supla.check(self.load(**{"boot_app0.bin": plans.otadata_selecting(1)}))

    def test_a_bootloader_that_is_not_one_is_refused(self):
        broken = bytearray(bootloader())
        broken[40] ^= 1
        for given, reason in ((b"\xaa\x50" + b"\xff" * 62, "not an ESP32 bootloader"),
                              (bytes(broken), "integrity"),
                              (build_image(b"\0" * 0x8000), "too big")):
            with self.assertRaisesRegex(supla.ImageError, reason):
                supla.check(self.load(**{"bootloader.bin": given}))

    def test_a_change_to_any_part_changes_its_hash(self):
        build = self.load()
        for part in ("app", "bootloader", "table", "otadata"):
            changed = supla.Build(**{**build.__dict__, part: getattr(build, part) + b"x"})
            self.assertNotEqual(changed.sha256(), build.sha256(), part)

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
        for plan in plan_for(b"\0" * plans.SUPLA_APP_SIZE):
            self.assertLessEqual(plan.busy_estimate(), plans.BUSY_LIMIT, plan.name)
            self.assertEqual((plan.table_sha256, plan.running), (plans.SONOFF_TABLE_SHA256, 1))

    def test_the_app_goes_where_the_builder_puts_it(self):
        image = gui_generic(size=0x180000)
        flash = bytearray(b"\xff" * 0x400000)
        for plan in plan_for(image):
            for step in plan.steps:
                if isinstance(step, plans.Write) and step.blob == "supla":
                    start, length = plan.span(step)
                    flash[step.address:step.address + length] = image[start:start + length]
        self.assertEqual(bytes(flash[plans.SUPLA_APP0:plans.SUPLA_APP0 + len(image)]), image)

    def test_sonoffs_boot_selection_goes_only_once_ota_0_holds_no_app(self):
        # Until then the device starts the Tasmota in ota_1 as it did; after,
        # it tries each slot, and finds only that Tasmota.
        first, second = plan_for(gui_generic(size=0x180000))
        self.assertFalse(first.restart)
        self.assertEqual(first.final_table_sha256(), plans.SONOFF_TABLE_SHA256)
        self.assertEqual(first.regions()[0][0], plans.SONOFF_OTA[0])
        self.assertTrue(all(start >= plans.SONOFF_OTA[0] for start, _, _ in first.regions()))
        head = second.steps.index(plans.Write("supla", plans.SUPLA_APP0, 0, plans.SUPLA_HEAD))
        self.assertEqual(head, 1)
        self.assertTrue(all(start >= plans.SONOFF_OTA[0] for start, _, _ in second.regions()[:head]))

    def test_the_bootloader_and_the_table_come_last(self):
        _, second = plan_for(gui_generic(size=0x180000))
        # A longer bootloader's end, left after the builder's, is cleared once
        # the builder's is in place: before, it would cut short the one starting the device.
        end = len(plans.padded(bootloader()))
        self.assertEqual(second.steps[-4:], [plans.Write("otadata", plans.SUPLA_OTADATA, whole=True),
                                             plans.Write("bootloader", 0, whole=True),
                                             plans.Erase(end, plans.TABLE_ADDRESS - end),
                                             plans.Write("table", plans.TABLE_ADDRESS, whole=True)])
        self.assertIn(plans.Erase(plans.SUPLA_TAIL, 0x400000 - plans.SUPLA_TAIL), second.steps)
        self.assertIn(plans.Erase(*plans.SUPLA_NVS), second.steps)
        self.assertTrue(second.restart)
        self.assertEqual(second.final_table_sha256(), plans.SUPLA_TABLE_SHA256)

    def test_nothing_is_written_where_the_tasmota_runs(self):
        berry = (ROOT / "artifacts" / "tasmota32c3-bluetooth.bin").stat().st_size
        tasmota = (plans.SONOFF_OTA[1], plans.SONOFF_OTA[1] + berry)
        for plan in plan_for(b"\0" * plans.SUPLA_APP_SIZE):
            for start, end, step in plan.regions():
                self.assertFalse(start < tasmota[1] and tasmota[0] < end, step)

    def test_an_app_start_left_in_sonoffs_ota_0_is_refused(self):
        # A cut after Sonoff's boot selection is gone would start it.
        image = bytearray(b"\0" * 0x180000)
        image[plans.SUPLA_HEAD] = 0xE9
        with self.assertRaisesRegex(plans.PlanError, "looks like an app"):
            plan_for(bytes(image))

    def test_a_plan_travels_whole(self):
        _, second = plan_for(gui_generic(size=0x30000))
        again = plans.Plan.from_bundle(second.bundle())
        self.assertEqual(again.expected(), second.expected())

    def test_anything_but_its_table_or_an_image_too_big_is_refused(self):
        with self.assertRaisesRegex(plans.PlanError, "Supla's partition table"):
            plans.supla(b"\0" * 0x30000, bootloader(), images.sonoff_table(), boot_app0())
        with self.assertRaisesRegex(plans.PlanError, "fit"):
            plan_for(b"\0" * (plans.SUPLA_APP_SIZE + 1))

    def test_the_split_stays_inside_an_image_of_any_size(self):
        # Rounded down to a sector: rounded up, a small image ran past its end.
        for size in (plans.SUPLA_HEAD + plans.SECTOR + 1, 0x22800, 0x100001):
            first, second = plan_for(b"\0" * size)
            written = sum(plan.span(step)[1] for plan in (first, second) for step in plan.steps
                          if isinstance(step, plans.Write) and step.blob == "supla")
            self.assertEqual(written, size)
        with self.assertRaisesRegex(plans.PlanError, "too small"):
            plan_for(b"\0" * (plans.SUPLA_HEAD + plans.SECTOR))


class RouteTests(unittest.TestCase):
    def test_its_image_is_checked_before_anything_is_written(self):
        for source in ("sonoff", "tasmota", "esphome", "midway", "installer", "committed"):
            steps = [step for step, _, _ in espressif.steps_for("supla", source)]
            self.assertEqual(steps[:2], ["A5", "S0"], source)
            self.assertEqual(steps[-2:], ["S1", "S2"], source)
            self.assertEqual(steps.count("S0"), 1, source)
            # Its own bootloader goes on at S1, in place of ESPHome's.
            self.assertNotIn("D5", steps, source)
        self.assertIn("D5", [step for step, _, _ in espressif.steps_for("esphome", "sonoff")])

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
        path = builder_zip(self.dir / "GUI-Generic.ESP32C3.vNX26.11.08.2.S60.json.tmpl.pl.bin.zip", image)
        state = RunState.open(self.dir / "run")
        state.set_input("device_kind", kinds.SONOFF_S60.key)
        return SimpleNamespace(state=state, supla_firmware=path, dry_run=False, device=None, **extra)

    def test_the_image_is_recorded_once_checked(self):
        image = gui_generic(extra=template())
        ctx = self.ctx(image)
        result = route.step_image(ctx)
        self.assertEqual(result["sha256"], hashlib.sha256(image).hexdigest())
        self.assertEqual((result["builder_version"], result["template"]), ("NX26.11.08.2", True))
        self.assertEqual(result["bootloader_sha256"], hashlib.sha256(bootloader()).hexdigest())
        self.assertEqual(ctx.state.input("supla_image"), str(ctx.supla_firmware))

    def test_another_devices_build_is_refused(self):
        with self.assertRaisesRegex(Abort, "another device's template"):
            route.step_image(self.ctx(gui_generic(extra=template((0,) * 22))))

    def test_a_file_changed_since_it_was_checked_is_refused(self):
        ctx = self.ctx(gui_generic(extra=template()))
        route.step_image(ctx)
        builder_zip(ctx.supla_firmware, gui_generic(extra=template()),
                    **{"bootloader.bin": build_image(b"another bootloader")})
        with self.assertRaisesRegex(Abort, "has changed since"):
            route.checked_build(ctx)

    def test_the_first_plan_is_not_run_again_once_it_finished(self):
        ctx = self.ctx(gui_generic(extra=template(), size=0x100000))
        route.step_image(ctx)
        with mock.patch.object(join, "device_signal", return_value=None), \
                mock.patch.object(join, "note_signal", return_value=None), \
                mock.patch.object(hub, "run_plan", side_effect=[{}, RuntimeError("cut off")]) as run:
            with self.assertRaises(RuntimeError):
                route.step_supla_install(ctx)
        self.assertEqual([call.args[1].name for call in run.call_args_list], ["supla_app", "supla_rest"])
        with mock.patch.object(join, "device_signal", return_value=None), \
                mock.patch.object(join, "note_signal", return_value=None), \
                mock.patch.object(hub, "run_plan", return_value={"plan": "supla_rest"}) as run:
            route.step_supla_install(ctx)
        self.assertEqual([call.args[1].name for call in run.call_args_list], ["supla_rest"])

    def test_supla_not_seen_on_the_wifi_still_ends_the_run(self):
        # It is written and checked; setting it up is GUI-Generic's own page's.
        ctx = self.ctx(gui_generic())
        ctx.state.set_input("device_ip", "192.0.2.50")
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
        ctx.state.set_input("device_ip", "192.0.2.50")
        with mock.patch.object(prompts, "wait_for_ssid", return_value=None), \
                mock.patch.object(prompts, "section"), mock.patch.object(prompts, "wait_for_key"), \
                mock.patch.object(join, "wait_for", side_effect=Abort("not seen")):
            with self.assertRaisesRegex(Abort, "neither its own network"):
                route.step_supla_check(ctx)


if __name__ == "__main__":
    unittest.main()
