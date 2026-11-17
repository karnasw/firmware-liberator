"""Each device's records, under its MAC (tools/autoflash/records.py), and one run on
a device at a time - a run that dies lets go of it."""
import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import cli, errors, output, records, runs


def efuse_with_mac(mac):
    """eFuse registers whose base MAC is `mac`, as the chip stores it."""
    data = bytearray(1024)
    data[0x18:0x1E] = bytes.fromhex(mac.replace(":", ""))[::-1]
    return bytes(data)


class DeviceLayoutTests(unittest.TestCase):
    """Each device's records live under its MAC (tools/autoflash/records.py)."""

    def setUp(self):
        self.records = records
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch = mock.patch.object(records, "DEVICES", Path(directory.name) / "devices")
        patch.start()
        self.addCleanup(patch.stop)

    def test_each_device_converted_here_is_numbered_once_in_turn_with_no_cap(self):
        self.assertEqual(records.number_conversion("12:34:56:ab:cd:01"), 1)
        self.assertEqual(records.number_conversion("12:34:56:ab:cd:02"), 2)
        self.assertEqual(records.number_conversion("12:34:56:ab:cd:01"), 1, "converted again: the same")
        records.remember_device("12:34:56:ab:cd:03", model="S60TPF")   # seen, never converted
        records.remember_device("12:34:56:ab:cd:04", **{records.CONVERTED_NUMBER: 57})
        self.assertEqual(records.number_conversion("12:34:56:ab:cd:03"), 58)
        self.assertEqual(records.read_device("12:34:56:ab:cd:03")["model"], "S60TPF", "the rest of its record kept")

    def test_a_mac_names_a_folder_windows_can_hold(self):
        self.assertEqual(self.records.folder_name("AA:BB:CC:DD:EE:0F"), "aa-bb-cc-dd-ee-0f")
        self.assertEqual(self.records.folder_name("aa-bb-cc-dd-ee-0f"), "aa-bb-cc-dd-ee-0f")
        with self.assertRaises(ValueError):
            self.records.folder_name("../etc")

    def test_the_mac_is_read_from_the_chips_efuses(self):
        self.assertEqual(self.records.mac_from_efuse(efuse_with_mac("58:cf:79:01:02:03")),
                         "58:cf:79:01:02:03")

    def test_the_first_backup_is_kept_read_only_and_never_replaced(self):
        efuse = efuse_with_mac("58:cf:79:01:02:03")
        first, saved = self.records.save_backup(b"first", efuse, {}, "1.2.0")
        self.assertTrue(saved)
        self.assertEqual(first, self.records.backup_dir("58:cf:79:01:02:03"))
        again, saved = self.records.save_backup(b"second", efuse, {}, "1.3.0")
        self.assertFalse(saved)
        self.assertEqual((again / "flash.bin").read_bytes(), b"first")
        if sys.platform != "win32":
            self.assertEqual((first / "flash.bin").stat().st_mode & 0o777, 0o400)
        self.assertIn("58:cf:79:01:02:03", (first / "README.txt").read_text())

    def test_the_note_beside_a_backup_is_true_of_any_espressif_device(self):
        # It once said every device was an S60: an ESP32-C3 in a welded case.
        saved, _ = self.records.save_backup(b"F", efuse_with_mac("58:cf:79:01:02:03"), {}, "1.2.0", kind="Sonoff POW Elite")
        note = (saved / "README.txt").read_text(encoding="utf-8")
        self.assertIn("Sonoff POW Elite", note)
        self.assertIn("esptool.py write_flash 0x0 flash.bin", note)
        for only_the_s60s in ("esp32c3", "ESP32-C3", "welded", "socket", "Tasmota"):
            self.assertNotIn(only_the_s60s, note)

    def test_backed_up_devices_are_listed_by_mac(self):
        self.records.save_backup(b"F", efuse_with_mac("58:cf:79:01:02:03"), {}, "1.2.0")
        self.records.remember_device("58:cf:79:01:02:03", ewelink_name="Kettle")
        (found,) = self.records.backups()
        self.assertEqual(found.mac, "58:cf:79:01:02:03")
        self.assertTrue(found.describe().startswith("58:cf:79:01:02:03"))
        self.assertIn("Kettle", found.describe())
        self.assertIn("1.2.0", found.describe())

    def test_a_beken_devices_own_data_runs_to_the_end_of_its_4_mb(self):
        start, size = records.BEKEN_REGION
        self.assertEqual((start, start + size), (0x3E0000, 4 * 1024 * 1024))

    def test_the_records_are_kept_in_this_checkout(self):
        self.assertEqual(records.ROOT / "tools" / "autoflash" / "records.py", Path(records.__file__).resolve())

    def test_what_a_backup_holds_and_when_it_was_taken(self):
        # A device whose folder is there already, as a readout leaves it.
        self.records.remember_device(MAC, ewelink_name="Kettle")
        with mock.patch.object(records.time, "strftime", return_value="2026-11-10"):
            saved, _ = self.records.save_backup(b"F", efuse_with_mac(MAC), {"checked": True}, None)
        self.assertEqual(json.loads((saved / "report.json").read_text(encoding="utf-8")), {"checked": True})
        note = (saved / "README.txt").read_text(encoding="utf-8")
        self.assertIn("read it on 2026-11-10", note)
        self.assertIn("Sonoff\n(unknown version)", note)
        self.assertEqual(self.records.read_device(MAC)["backed_up"], "2026-11-10")
        if sys.platform != "win32":
            self.assertEqual(saved.parent.stat().st_mode & 0o777, 0o700)

    def test_a_devices_record_names_it_by_its_mac_and_is_its_own(self):
        self.records.remember_device("12-34-56-AB-CD-EF", ewelink_name="Kettle", stock_version="")
        self.assertEqual(self.records.read_device(MAC), {"ewelink_name": "Kettle", "mac": MAC})
        if sys.platform != "win32":
            self.assertEqual(self.records.device_dir(MAC).stat().st_mode & 0o777, 0o700)

    def test_only_a_whole_backup_is_offered_newest_first_named_only_by_its_own_name(self):
        older, newer = "12:34:56:ab:cd:01", "12:34:56:ab:cd:02"
        for mac, date in ((newer, "2026-11-02"), (older, "2026-11-01")):
            self.records.save_backup(b"F", efuse_with_mac(mac), {}, "1.2.0", date=date)
        self.records.remember_device(older, ewelink_name=records.kinds.ALL[0].name)  # eWeLink's name for the model
        part = self.records.backup_dir("12:34:56:ab:cd:03")
        part.mkdir(parents=True)
        (part / "flash.bin").write_bytes(b"F")  # its eFuses never read
        self.assertEqual([backup.mac for backup in self.records.backups()], [newer, older])
        if sys.platform != "win32":
            self.assertEqual(self.records.device_dir(newer).stat().st_mode & 0o777, 0o700)
        for backup in self.records.backups():
            self.assertNotIn('"', backup.describe())

    def test_a_run_knows_its_device_from_where_it_is_kept(self):
        run = self.records.runs_dir("58:cf:79:01:02:03") / "20261013-031500"
        self.assertEqual(self.records.mac_of_run(run), "58:cf:79:01:02:03")
        self.assertIsNone(self.records.mac_of_run(self.records.runs_dir(None) / "x"))


MAC = "12:34:56:ab:cd:ef"


class EarlierNamesTests(unittest.TestCase):
    """What a release before 3.0.0 kept as captures/plugs/ and plug.json is
    renamed where it is, once, and never while that could lose anything."""

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.captures = Path(directory.name)
        self.earlier = self.captures / "plugs"
        self.devices = self.captures / "devices"
        patcher = mock.patch.object(records, "DEVICES", self.devices)
        patcher.start()
        self.addCleanup(patcher.stop)

    def keep(self, folder):
        """A device with a backup and an unfinished run, as an earlier release left it."""
        device = folder / "12-34-56-ab-cd-ef"
        (device / "sonoff-backup").mkdir(parents=True)
        (device / "sonoff-backup" / "flash.bin").write_bytes(b"the original")
        (device / "runs" / "20261013-031500").mkdir(parents=True)
        (device / "runs" / "20261013-031500" / "autoflash-state.json").write_text("{}", encoding="utf-8")
        (device / "plug.json").write_text('{"ewelink_name": "Kettle"}', encoding="utf-8")
        return device

    def test_the_folder_is_renamed_where_it_is_and_each_devices_file_with_it(self):
        backup = self.keep(self.earlier) / "sonoff-backup" / "flash.bin"
        before = backup.stat()
        records.take_over_earlier_names()
        self.assertFalse(self.earlier.exists())
        kept = self.devices / "12-34-56-ab-cd-ef"
        after = (kept / "sonoff-backup" / "flash.bin").stat()
        self.assertEqual((after.st_ino, after.st_mtime_ns), (before.st_ino, before.st_mtime_ns))  # the same file
        self.assertTrue((kept / "runs" / "20261013-031500" / "autoflash-state.json").is_file())
        self.assertFalse((kept / "plug.json").exists())
        self.assertEqual(records.read_device(MAC), {"ewelink_name": "Kettle"})

    def test_done_again_it_changes_nothing(self):
        self.keep(self.earlier)
        records.take_over_earlier_names()
        records.remember_device(MAC, stock_version="1.2.0")
        records.take_over_earlier_names()
        self.assertEqual(records.read_device(MAC)["stock_version"], "1.2.0")

    def test_with_nothing_kept_nothing_is_made(self):
        records.take_over_earlier_names()
        self.assertEqual(list(self.captures.iterdir()), [])

    def test_a_file_left_behind_part_way_is_renamed_the_next_time(self):
        device = self.keep(self.devices)   # the folder renamed, its file not yet
        records.take_over_earlier_names()
        self.assertEqual(sorted(path.name for path in device.glob("*.json")), ["device.json"])

    def test_a_newer_file_is_never_written_over(self):
        device = self.keep(self.devices)
        (device / "device.json").write_text('{"ewelink_name": "Lamp"}', encoding="utf-8")
        records.take_over_earlier_names()
        self.assertEqual(records.read_device(MAC), {"ewelink_name": "Lamp"})
        self.assertTrue((device / "plug.json").exists())

    def test_both_folders_stop_it_and_nothing_moves(self):
        self.keep(self.earlier)
        self.devices.mkdir()
        with self.assertRaisesRegex(records.EarlierNames, "both exist"):
            records.take_over_earlier_names()
        self.assertTrue((self.earlier / "12-34-56-ab-cd-ef" / "sonoff-backup" / "flash.bin").is_file())
        self.assertEqual(list(self.devices.iterdir()), [])

    def test_a_run_of_the_earlier_release_still_going_stops_it(self):
        self.keep(self.earlier)
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from tools.autoflash import records
            from pathlib import Path
            records.DEVICES = Path({str(self.earlier)!r})
            records.hold({MAC!r}, "plugs/the-earlier-run")
            print("held", flush=True)
            time.sleep(30)
        """)
        other = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        try:
            self.assertEqual(other.stdout.readline().strip(), "held")
            with self.assertRaisesRegex(records.EarlierNames, "converting a device right now"):
                records.take_over_earlier_names()
            self.assertTrue(self.earlier.is_dir())
            self.assertFalse(self.devices.exists())
        finally:
            other.kill()
            other.wait()
            other.stdout.close()

    def test_a_rename_another_run_made_meanwhile_is_no_failure(self):
        self.keep(self.earlier)
        rename = Path.rename

        def renamed_by_the_other_run(path, target):
            rename(path, target)
            if path == self.earlier:
                raise FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(Path, "rename", renamed_by_the_other_run):
            records.take_over_earlier_names()
        self.assertTrue((self.devices / "12-34-56-ab-cd-ef" / "sonoff-backup" / "flash.bin").is_file())
        self.assertTrue((self.devices / "12-34-56-ab-cd-ef" / "device.json").is_file())

    def test_a_rename_refused_while_both_folders_are_there_says_so(self):
        self.keep(self.earlier)

        def refused_as_another_folder_appears(path, target):
            self.devices.mkdir()
            raise PermissionError(13, "Access is denied")
        with mock.patch.object(Path, "rename", refused_as_another_folder_appears):
            with self.assertRaisesRegex(records.EarlierNames, "could not rename"):
                records.take_over_earlier_names()

    def test_a_rename_refused_says_so_and_leaves_it(self):
        self.keep(self.earlier)
        with mock.patch.object(Path, "rename", side_effect=PermissionError(13, "Access is denied")):
            with self.assertRaisesRegex(records.EarlierNames, "could not rename"):
                records.take_over_earlier_names()
        self.assertTrue(self.earlier.is_dir())

    def test_every_command_that_reads_the_records_renames_them_first(self):
        """The tool, and each script with a main() of its own that reads or
        writes captures/devices/: run first after an update, it must find what
        the earlier release kept, and never start a second folder beside it."""
        root = Path(__file__).resolve().parents[2]
        for script in ("tools/autoflash/cli.py", "tools/espressif/emulator/choose_device.py",
                       "tools/espressif/emulator/copy_device.py", "tools/espressif/emulator/public_dump.py",
                       "tools/espressif/dump_stock_flash.py", "tools/repo/private_backup.py", "tools/beken/backup.py"):
            with self.subTest(script=script):
                self.assertIn("records.take_over_earlier_names()", (root / script).read_text(encoding="utf-8"))

    def test_a_run_named_as_the_earlier_release_named_it_is_found(self):
        self.assertEqual(records.earlier_run_name("plugs/12-34-56-ab-cd-ef/runs/20261013-031500"),
                         "devices/12-34-56-ab-cd-ef/runs/20261013-031500")
        for name in ("devices/12-34-56-ab-cd-ef/runs/20261013-031500", "plugs", "20261001-120000", ""):
            self.assertEqual(records.earlier_run_name(name), name)


class DeviceLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        patcher = mock.patch.object(records, "DEVICES", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        held = mock.patch.dict(records._HELD, {}, clear=True)
        held.start()
        self.addCleanup(held.stop)

    def other_run(self, script):
        """A second process holding the device, as another run would."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from tools.autoflash import records
            from pathlib import Path
            records.DEVICES = Path({str(self.root)!r})
            records.hold({MAC!r}, "devices/the-other-run")
            print("held", flush=True)
            {script}
        """)
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "held")
        return process

    def test_a_second_run_is_refused_and_told_which_holds_it(self):
        other = self.other_run("time.sleep(30)")
        try:
            with self.assertRaises(records.DeviceBusy) as busy:
                records.hold(MAC, "devices/this-run")
            self.assertTrue(str(busy.exception).startswith("run devices/the-other-run, process "))
        finally:
            other.kill()
            other.wait()

    def test_a_run_that_died_leaves_no_lock_behind(self):
        other = self.other_run("import os; os._exit(9)")  # gone without a word, as in a crash
        other.wait()
        records.hold(MAC, "devices/this-run")  # not refused
        self.assertIn(MAC, records._HELD)

    def held_here(self, mac=MAC, run="devices/this-run"):
        records.hold(mac, run)
        self.addCleanup(records._HELD[mac].close)
        return records.device_dir(mac) / "run.lock"

    def test_the_first_run_on_a_new_records_folder_makes_it(self):
        with mock.patch.object(records, "DEVICES", self.root / "captures" / "devices"):
            self.assertTrue(self.held_here().is_file())

    def test_the_lock_says_only_which_run_holds_it_now(self):
        lock = records.device_dir(MAC) / "run.lock"
        lock.parent.mkdir(parents=True)
        lock.write_text("run devices/a-long-gone-run-with-a-long-name, process 1, since 00:00:00\n" * 3,
                        encoding="utf-8")
        self.held_here()
        self.assertRegex(lock.read_text(encoding="utf-8"), r"^run devices/this-run, process \d+, since [\d:]+\n$")

    def test_the_run_refused_is_told_plainly(self):
        from types import SimpleNamespace
        with mock.patch.object(records, "hold", side_effect=records.DeviceBusy("run devices/x, process 7")):
            with self.assertRaisesRegex(errors.Abort, "another run is converting this device"):
                runs.hold_device(SimpleNamespace(), MAC)

    def test_the_run_refused_is_told_nothing_was_done_once(self):
        # Its caller says so first: "Stopped before anything was done: ...".
        from types import SimpleNamespace
        with mock.patch.object(records, "hold", side_effect=records.DeviceBusy("run devices/x, process 7")):
            with self.assertRaises(errors.Abort) as refused:
                runs.hold_device(SimpleNamespace(), MAC)
        self.assertNotIn("Nothing was done", str(refused.exception))
        self.assertTrue(str(refused.exception).endswith("then run this again"))

    def test_busy_names_the_devices_others_hold(self):
        self.assertEqual(records.busy(), [])
        other = self.other_run("time.sleep(30)")
        try:
            busy = records.busy()
            self.assertEqual(len(busy), 1)
            self.assertIn(MAC, busy[0])
            self.assertIn("the-other-run", busy[0])
        finally:
            other.kill()
            other.wait()
        self.assertEqual(records.busy(), [])  # let go with the process

    def ctx(self, mac=MAC):
        from types import SimpleNamespace
        return SimpleNamespace(state=SimpleNamespace(input=lambda key: mac if key == "device_mac" else None),
                               dir=self.root / "runs" / "this-run")

    def test_a_carried_on_run_is_refused_before_it_touches_anything(self):
        # Its log, report, firewall rule and safety locks are the other run's
        # too: the run's main stops before any of them (seen in the fault
        # suite: a refused run took the other's firewall rule away).
        other = self.other_run("time.sleep(30)")
        try:
            with mock.patch.object(output, "tell") as told:
                self.assertTrue(cli.refused_for_another_run("run", self.ctx()))
            self.assertIn("another run is converting this device", told.call_args.args[0])
        finally:
            other.kill()
            other.wait()
        self.assertFalse(cli.refused_for_another_run("run", self.ctx()))  # then it is this run's

    def test_cleanup_waits_for_every_run_to_finish(self):
        other = self.other_run("time.sleep(30)")
        try:
            with mock.patch.object(output, "tell") as told:
                self.assertTrue(cli.refused_for_another_run("cleanup", self.ctx(mac=None)))
            self.assertIn("Not cleaning up", told.call_args.args[0])
        finally:
            other.kill()
            other.wait()
        self.assertFalse(cli.refused_for_another_run("cleanup", self.ctx(mac=None)))

    def test_looking_is_never_refused(self):
        other = self.other_run("time.sleep(30)")
        try:
            for command in ("status", "list"):
                self.assertFalse(cli.refused_for_another_run(command, self.ctx()))
        finally:
            other.kill()
            other.wait()


class ConvertedHereTests(unittest.TestCase):
    """How many devices this computer has converted, as a readout's report says."""

    def test_the_highest_number_given_none_for_none(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(records, "DEVICES", Path(tmp)):
            self.assertEqual(records.converted_here(), 0)
            self.assertEqual(records.number_conversion("12:34:56:ab:cd:01"), 1)
            self.assertEqual(records.number_conversion("12:34:56:ab:cd:02"), 2)
            self.assertEqual(records.number_conversion("12:34:56:ab:cd:01"), 1, "a device keeps its number")
            self.assertEqual(records.converted_here(), 2)


if __name__ == "__main__":
    unittest.main()
