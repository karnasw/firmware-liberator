"""What prune_captures.py removes, and above all what it never does."""

import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from tools.repo import prune_captures as prune


class PruneTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.captures = Path(temp.name) / "captures"
        mock.patch.object(prune, "CAPTURES", self.captures).start()
        mock.patch.object(prune, "running_states", return_value=set()).start()
        self.addCleanup(mock.patch.stopall)

    def make(self, relative, old=True):
        path = self.captures / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x" * 100, encoding="utf-8")
        if old:
            then = time.time() - 30 * 86400
            os.utime(path, (then, then))
        return path

    def run_it(self, *argv):
        with redirect_stdout(io.StringIO()) as out:
            prune.main(list(argv))
        return out.getvalue()

    def test_without_delete_nothing_goes(self):
        flash = self.make("emulation/state-3/flash.bin")
        self.assertIn("would go", self.run_it())
        self.assertTrue(flash.exists())

    def test_an_old_emulator_state_and_build_cache_go(self):
        state = self.make("emulation/state-3/work/tools/x.py")
        build = self.make("esphome-build/cache.o")
        self.run_it("--delete")
        self.assertFalse(state.exists())
        self.assertFalse(build.exists())

    def test_a_beken_emulators_old_state_and_earlier_tree_go(self):
        made = [self.make("emulation/beken/state/flash.bin"), self.make("emulation/beken/state-2/work/x.py"),
                self.make("emulation/beken/legacy/v2.3.0/README.md")]
        self.run_it("--delete")
        for path in made:
            self.assertFalse(path.exists(), path)

    def test_a_backup_is_kept_wherever_it_is(self):
        backup = self.make("emulation/state-3/work/captures/devices/12-34-56-ab-cd-ef/sonoff-backup/flash.bin")
        other = self.make("emulation/state-3/flash.bin")
        self.run_it("--delete")
        self.assertTrue(backup.exists())
        self.assertFalse(other.exists())

    def test_what_is_not_listed_is_never_touched(self):
        kept = [self.make("devices/12-34-56-ab-cd-ef/sonoff-backup/flash.bin"), self.make("emulation/saved/suite-esphome/flash.bin"),
                self.make("emulation/beken/firmwares/dump.bin"),
                self.make("emulation/beken/saved/openbeken/flash.bin"), self.make("settings.json"),
                self.make("emulation/public-dumps/d.bin")]
        self.run_it("--delete")
        for path in kept:
            self.assertTrue(path.exists(), path)

    def test_recently_used_stays(self):
        recent = self.make("emulation/state-4/flash.bin", old=False)
        self.run_it("--delete")
        self.assertTrue(recent.exists())

    def test_a_running_emulators_state_stays(self):
        state = self.make("emulation/state-2/flash.bin")
        with mock.patch.object(prune, "running_states", return_value={self.captures / "emulation/state-2"}):
            self.run_it("--delete")
        self.assertTrue(state.exists())

    def test_where_docker_cannot_say_no_emulator_state_goes(self):
        state = self.make("emulation/state-2/flash.bin")
        logs = self.make("emulation/beken/suite/logs/run.log")
        with mock.patch.object(prune, "running_states", return_value=None), \
                redirect_stdout(io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            prune.main(["--delete"])
        self.assertTrue(state.exists())
        self.assertFalse(logs.exists())

    def test_a_suites_results_stay_with_only_its_logs_gone(self):
        results = self.make("emulation/beken/suite/results.jsonl")
        log = self.make("emulation/beken/suite/logs/x/tool.log")
        self.run_it("--delete")
        self.assertTrue(results.exists())
        self.assertFalse(log.exists())

    def test_what_lies_inside_another_is_counted_once(self):
        self.make("emulation/state-6/logs/run.log")
        listed = [path for path, _what in prune.candidates(self.captures)]
        self.assertEqual(listed, [self.captures / "emulation/state-6"])

    def test_a_read_only_folder_is_no_bar(self):
        locked = self.make("emulation/state-5/work/.claude/worktrees/a/toolchain/gcc")
        locked.parent.chmod(0o555)
        self.addCleanup(lambda: locked.parent.chmod(0o755) if locked.parent.exists() else None)
        self.run_it("--delete")
        self.assertFalse(locked.exists())
