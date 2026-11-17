"""The safety locks, lifted by more than one run at once (two devices) - put
back only when the last is done, and never left lifted by a run that died."""
import subprocess
import sys
import tempfile
import textwrap
import threading
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output, runs, safety_locks

NAME = "RECOVERY_LOCK"


class SafetyLockTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.lock = self.root / safety_locks.DIR / NAME
        self.lock.parent.mkdir(parents=True)
        self.lock.write_text("shipped lock\n")

    def lock_in_place(self):
        return self.lock.exists()

    def lifters_left(self):
        """The marks of runs with it lifted, as files: none once each has ended."""
        owners = self.root / "captures" / ".safety-locks" / f"{NAME}.lifted-by"
        return sorted(owners.iterdir()) if owners.exists() else []

    def other_run(self, then):
        """Another run, in a process of its own, with the lock lifted."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
            from pathlib import Path
            from tools.autoflash import safety_locks
            with safety_locks.lifted(Path({str(self.root)!r}), {NAME!r}):
                print("lifted", flush=True)
                {then}
        """)
        process = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                                   stdin=subprocess.PIPE, text=True)
        self.assertEqual(process.stdout.readline().strip(), "lifted")
        self.addCleanup(lambda: (process.kill(), process.wait()))
        return process

    def test_lifted_for_the_block_and_put_back_after(self):
        with safety_locks.lifted(self.root, NAME):
            self.assertFalse(self.lock_in_place())
        self.assertTrue(self.lock_in_place())
        self.assertEqual(self.lock.read_text(), "shipped lock\n")
        self.assertEqual(self.lifters_left(), [])

    def test_put_back_even_when_the_block_fails(self):
        with self.assertRaises(RuntimeError):
            with safety_locks.lifted(self.root, NAME):
                raise RuntimeError("the helper failed")
        self.assertTrue(self.lock_in_place())

    def test_kept_lifted_while_another_run_still_needs_it(self):
        other = self.other_run("sys.stdin.readline()")
        with safety_locks.lifted(self.root, NAME):
            self.assertFalse(self.lock_in_place())
        self.assertFalse(self.lock_in_place())  # the other run's helper still runs
        self.assertTrue(safety_locks.in_place(self.root, NAME))  # but it is accounted for
        self.assertEqual(safety_locks.restore_left(self.root), [])  # and not put back under it
        other.stdin.write("done\n")
        other.stdin.flush()
        other.wait(timeout=30)
        self.assertTrue(self.lock_in_place())  # the last one out put it back

    def test_one_left_lifted_by_a_run_that_died_is_put_back(self):
        other = self.other_run("import os; os._exit(9)")  # gone without a word, as in a crash
        other.wait(timeout=30)
        self.assertFalse(self.lock_in_place())
        self.assertFalse(safety_locks.in_place(self.root, NAME))
        self.assertEqual(safety_locks.restore_left(self.root), [NAME])
        self.assertTrue(self.lock_in_place())
        self.assertEqual(self.lifters_left(), [], "the dead run's mark cleared away")

    def test_one_run_at_a_time_looks_at_a_lock_and_moves_it(self):
        # Else two runs could each find the other's lift ended and both move it.
        self.lock.rename(self.lock.with_name(f"{NAME}.owner-authorized"))
        restored = []
        with safety_locks._guarded(self.root, NAME):
            other = threading.Thread(target=lambda: restored.extend(safety_locks.restore_left(self.root)))
            other.start()
            other.join(0.3)
            self.assertTrue(other.is_alive(), "waits for the guard")
            self.assertFalse(self.lock_in_place())
        other.join(30)
        self.assertEqual(restored, [NAME])

    def test_a_lift_from_before_this_record_is_put_back(self):
        # A run of an earlier version moved it aside and stopped there.
        self.lock.rename(self.lock.with_name(f"{NAME}.owner-authorized"))
        self.assertEqual(safety_locks.restore_left(self.root), [NAME])
        self.assertTrue(self.lock_in_place())


class LeftLockTests(unittest.TestCase):
    def test_a_lock_left_moved_aside_by_a_stopped_run_is_put_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / safety_locks.DIR).mkdir(parents=True)
            (root / safety_locks.DIR / "RECOVERY_LOCK.owner-authorized").write_text("locked\n")
            (root / safety_locks.DIR / "REPARTITION_LOCK").write_text("locked\n")
            with mock.patch.object(runs, "ROOT", root), mock.patch.object(output, "say"):
                safety_locks.restore_left_locks(runs.ROOT)
            self.assertTrue((root / safety_locks.DIR / "RECOVERY_LOCK").exists())
            self.assertFalse((root / safety_locks.DIR / "RECOVERY_LOCK.owner-authorized").exists())
            self.assertTrue((root / safety_locks.DIR / "REPARTITION_LOCK").exists())


if __name__ == "__main__":
    unittest.main()
