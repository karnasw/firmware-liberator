"""Which devices a change checks, and what each device's check runs."""

import hashlib
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import kinds
from tools.autoflash.routes import public_copies
from tools.repo import emulator_check as check

ROOT = Path(__file__).resolve().parents[2]


def keys(changed):
    return {job["device"] for job in check.jobs(changed)}


class WhichDevicesTests(unittest.TestCase):
    ALL = {kind.key for kind in kinds.READY} - check.NOTHING_TO_RUN
    ESPRESSIF = {key for key in ALL if check.family_of(kinds.find(key)) == "espressif"}

    def test_a_check_missing_a_package_says_what_to_install_before_it_starts(self):
        # Seen on a machine run with a Python without the NVS generator: the POW
        # Elite's check stopped part way through making its public dump, a traceback.
        pow_elite = next(kind for kind in kinds.READY if kind.key == "sonoff-pow-elite")
        with mock.patch("importlib.util.find_spec", return_value=None), \
                mock.patch.object(check, "check_device") as checked, \
                mock.patch("sys.stderr") as said:
            self.assertEqual(check.main(["sonoff-pow-elite"]), 2)
            self.assertIn("esp_idf_nvs_partition_gen", check.missing_packages(pow_elite))
        checked.assert_not_called()
        told = "".join(call.args[0] for call in said.write.call_args_list)
        self.assertIn("esp_idf_nvs_partition_gen", told)
        self.assertIn("requirements/ci.txt", told)
        s61 = next(kind for kind in kinds.READY if kind.key == "sonoff-s61")
        with mock.patch("importlib.util.find_spec", return_value=None):
            self.assertIsNone(check.missing_packages(s61), "a Beken device's check needs none of them")

    def test_every_device_with_no_change_named(self):
        self.assertEqual(keys(None), self.ALL)

    def test_a_device_that_is_ready_has_a_check_by_being_added(self):
        for kind in kinds.READY:
            self.assertIn(check.family_of(kind), check.EMULATORS, kind.key)

    def test_the_s61s_own_files_check_the_s61_alone(self):
        self.assertEqual(keys(["devices/sonoff-s61/esphome.yaml"]), {"sonoff-s61"})

    def test_the_basic_gen5_with_no_public_dump_has_a_check_of_its_own(self):
        self.assertIn("sonoff-basic-1gs", keys(None))
        self.assertEqual(keys(["devices/sonoff-basic-1gs/esphome.yaml"]), {"sonoff-basic-1gs"})

    def test_the_beken_tools_check_every_beken_device(self):
        # rebuild_dump.py among them, which makes the Basic Gen5's flash.
        for path in ("tools/beken/rbl.py", "tools/beken/rebuild_dump.py"):
            self.assertEqual(keys([path]), {"sonoff-s61", "sonoff-basic-1gs"}, path)

    def test_one_devices_config_checks_that_device(self):
        self.assertEqual(keys(["devices/sonoff-powct/esphome.yaml"]), {"sonoff-powct"})

    def test_the_espressif_tools_check_every_espressif_device(self):
        self.assertEqual(keys(["tools/espressif/flashmap.py"]), self.ESPRESSIF)

    def test_what_all_of_them_run_on_checks_all_of_them(self):
        for path in ("tools/autoflash/plans.py", "artifacts/firmware.json", "tools/ewelink/ota_push.py",
                     ".github/workflows/emulator-check.yml", "tools/repo/emulator_check.py"):
            self.assertEqual(keys([path]), self.ALL, path)

    def test_a_chips_shared_files_check_the_devices_on_it(self):
        for chip in ("esp32", "esp32c3"):
            found = keys([f"devices/{chip}/mover.yaml"])
            self.assertEqual(found, {kind.key for kind in kinds.READY if kind.chip == chip} - check.NOTHING_TO_RUN, chip)
        self.assertNotIn("sonoff-s60", keys(["devices/esp32/mover.yaml"]))

    def test_nothing_a_conversion_runs_checks_nothing(self):
        self.assertEqual(keys(["docs/testing.md", "tests/repo/test_layout.py", "website/src/site.mjs"]), set())

    def test_a_commit_git_cannot_compare_with_checks_every_device(self):
        self.assertIsNone(check.changed_since("0" * 40))


class WaysTests(unittest.TestCase):
    def test_each_final_firmware_then_the_other(self):
        self.assertEqual(check.ESPRESSIF_WAYS, (("tasmota", "esphome"), ("esphome", "tasmota")))

    def test_every_device_has_something_to_run_from_or_is_named_as_having_nothing(self):
        for kind in kinds.READY:
            self.assertTrue(kind.key in runs_from() or kind.key in check.NOTHING_TO_RUN, kind.key)

    def test_a_device_named_as_having_nothing_to_run_really_has_nothing(self):
        self.assertFalse(check.NOTHING_TO_RUN & runs_from())

    def test_only_a_beken_device_with_no_public_dump_runs_from_a_rebuilt_flash(self):
        self.assertTrue(check.rebuilt(kinds.find("sonoff-basic-1gs")))
        self.assertFalse(check.rebuilt(kinds.find("sonoff-s61")))


def runs_from():
    """Each device that has something for its check to start from."""
    from tools.espressif.emulator import public_dump
    return {*public_dump.DUMPS, *public_dump.UPDATES,
            "sonoff-s60",              # the published public devices
            check.REBUILT_FROM,        # upstream's public dump (beken-emu.sh)
            # a flash rebuilt from the S61's and its official file
            *(kind.key for kind in kinds.EVERY if kind.family == "beken")}


class BekenCheckTests(unittest.TestCase):
    """A Beken device's check: the fault suite's, on the emulators asked for,
    its logs where an Espressif device's go, from the S61's dump or a flash
    rebuilt for the device."""

    def run_check(self, key, environ=None):
        calls = []

        def call(command, env):
            calls.append((command, env))
            return 0
        flash = Path("/nowhere/sonoff-basic-1gs.bin")
        with mock.patch.dict(os.environ, environ or {}), mock.patch.object(check.subprocess, "run"), \
                mock.patch.object(check.subprocess, "call", side_effect=call), \
                mock.patch.object(check, "rebuilt_flash", return_value=flash) as rebuilt:
            self.assertEqual(check.check_device(kinds.find(key), [3, 4]), 0)
        ((command, env),) = calls
        return command, env, rebuilt

    def test_the_basic_gen5_runs_from_its_rebuilt_flash_on_the_emulators_asked_for(self):
        command, env, rebuilt = self.run_check("sonoff-basic-1gs")
        self.assertEqual(command[1:], ["check", "--instances", "3,4"])
        self.assertEqual(env["AUTOFLASH_BEKEN_EMU_FLASH"], "/nowhere/sonoff-basic-1gs.bin")
        self.assertEqual(env["AUTOFLASH_BEKEN_EMU_SUITE_DIR"], str(check.LOGS / "sonoff-basic-1gs"))
        rebuilt.assert_called_once()

    def test_the_s61_runs_from_its_public_dump_whatever_flash_was_named(self):
        command, env, rebuilt = self.run_check("sonoff-s61", {"AUTOFLASH_BEKEN_EMU_FLASH": "captures/other.bin"})
        self.assertEqual(command[1:], ["check", "--instances", "3,4"])
        self.assertNotIn("AUTOFLASH_BEKEN_EMU_FLASH", env)
        self.assertEqual(env["AUTOFLASH_BEKEN_EMU_SUITE_DIR"], str(check.LOGS / "sonoff-s61"))
        rebuilt.assert_not_called()


class WhereTheRebuiltFlashIsTests(unittest.TestCase):
    def test_in_the_beken_emulators_folders_in_captures_which_git_never_tracks(self):
        from tools.beken.emulator import folders
        self.assertEqual(folders.REBUILT, ROOT / "captures/emulation/beken/rebuilt")
        self.assertIn("captures/", (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines())


class RebuiltFlashTests(unittest.TestCase):
    """The Basic Gen5's flash, made from the S61's public dump and its official
    file, kept in the Beken emulator's folders under captures/."""

    def setUp(self):
        from tools.beken.emulator import folders
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dumps, self.rebuilt = Path(tmp.name, "firmwares"), Path(tmp.name, "rebuilt")
        self.dumps.mkdir()
        self.dump = b"the S61's dump"
        (self.dumps / check.S61_DUMP[0]).write_bytes(self.dump)
        for patch in (mock.patch.object(folders, "FIRMWARES", self.dumps), mock.patch.object(folders, "REBUILT", self.rebuilt),
                      mock.patch.object(check, "ROOT", Path(tmp.name)),
                      mock.patch.object(check, "S61_DUMP", (check.S61_DUMP[0], hashlib.sha256(self.dump).hexdigest()))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_made_from_the_s61s_dump_and_the_devices_official_file(self):
        import io
        from contextlib import redirect_stdout
        from tools.beken import rebuild_dump
        kind = kinds.find("sonoff-basic-1gs")
        with mock.patch.object(public_copies, "fetch", return_value=b"official") as official, \
                mock.patch.object(rebuild_dump, "rebuild", return_value=b"flash") as rebuild, \
                redirect_stdout(io.StringIO()):
            out = check.rebuilt_flash(kind)
        official.assert_called_once_with(kind)
        rebuild.assert_called_once_with(self.dump, b"official")
        self.assertEqual(out, self.rebuilt / "sonoff-basic-1gs.bin")
        self.assertEqual(out.read_bytes(), b"flash")
        self.assertEqual([path.name for path in self.rebuilt.iterdir()], ["sonoff-basic-1gs.bin"])

    def test_not_from_a_dump_other_than_the_one_pinned(self):
        (self.dumps / check.S61_DUMP[0]).write_bytes(b"another dump")
        with self.assertRaisesRegex(SystemExit, "not the public dump"):
            check.rebuilt_flash(kinds.find("sonoff-basic-1gs"))
        self.assertFalse(self.rebuilt.exists())

    def test_an_official_file_that_cannot_be_had_stops_the_check_saying_why(self):
        with mock.patch.object(public_copies, "fetch", side_effect=public_copies.Unavailable("could not be fetched")), \
                self.assertRaisesRegex(SystemExit, "could not be fetched"):
            check.rebuilt_flash(kinds.find("sonoff-basic-1gs"))


class SideBySideTests(unittest.TestCase):
    def test_a_job_that_raises_has_failed_and_says_why(self):
        import io
        from contextlib import redirect_stdout
        from tools.espressif.emulator import public_devices

        def broken():
            raise RuntimeError("the emulator did not start")
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(public_devices.side_by_side([lambda: 0, broken]), [0, 1])
        self.assertIn("the emulator did not start", out.getvalue())


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = (ROOT / ".github/workflows/emulator-check.yml").read_text(encoding="utf-8")

    def test_the_required_check_keeps_its_name(self):
        from tools.repo import github_settings
        self.assertIn("emulator", github_settings.REQUIRED_CHECKS)
        self.assertRegex(self.workflow, r"(?m)^  emulator:$")

    def test_only_a_run_on_main_is_given_the_emulator_stats_token(self):
        from tools.repo import emulator_stats
        for name in (emulator_stats.CLIENT_ID, emulator_stats.CLIENT_SECRET):
            uses = re.findall(rf"{name}: (.*)", self.workflow)
            self.assertEqual(uses, [f"${{{{ github.event_name != 'pull_request' && github.ref == 'refs/heads/main' "
                                    f"&& secrets.{name} || '' }}}}"], name)

    def test_each_device_has_its_own_job(self):
        self.assertIn("emulator_check.py list", self.workflow)
        self.assertIn('emulator_check.py "$DEVICE"', self.workflow)

    def test_every_familys_runs_are_kept_for_reading(self):
        # Each family's emulator keeps a device's computer's runs under its own state folders.
        from tools.beken.emulator import folders as beken_folders
        beken = beken_folders.state(1).parent.relative_to(ROOT).as_posix()
        for family in ("captures/emulation", beken):
            with self.subTest(family):
                self.assertIn(f"            {family}/state-*/work/captures/devices/*/runs/*/run.log\n", self.workflow)

    def test_a_release_checks_every_device(self):
        self.assertIn("every_device", self.workflow)
