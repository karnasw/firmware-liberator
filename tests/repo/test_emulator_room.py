"""The emulators' shared shell: their copy of the checkout, waiting for room, and
the API's own code beside them."""

import os
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools" / "repo" / "emulator-room.sh"


def bash(command, env=None, timeout=60):
    # As the emulators run it: set -euo pipefail.
    return subprocess.run(["bash", "-c", f"set -euo pipefail; . {SCRIPT}; {command}"], capture_output=True, text=True,
                          timeout=timeout, env={**os.environ, **(env or {})})


@unittest.skipIf(shutil.which("bash") is None or shutil.which("rsync") is None, "no bash or rsync")
class CopyCheckoutTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.src, self.dest = Path(temp.name, "src"), Path(temp.name, "dest")
        self.src.mkdir()
        git = ["git", "-C", str(self.src), "-c", "user.name=t", "-c", "user.email=t@example.com",
               "-c", "commit.gpgsign=false"]
        subprocess.run([*git[:3], "init", "--quiet"], check=True)
        (self.src / ".gitignore").write_text("captures/\n.env\n", encoding="utf-8")
        (self.src / "tools").mkdir()
        (self.src / "tools" / "run.py").write_text("tracked\n", encoding="utf-8")
        subprocess.run([*git, "add", "."], check=True)
        subprocess.run([*git, "commit", "--quiet", "-m", "x"], check=True)
        (self.src / "new.py").write_text("not yet added\n", encoding="utf-8")
        (self.src / ".env").write_text("SECRET=1\n", encoding="utf-8")
        (self.src / "captures").mkdir()
        (self.src / "captures" / "backup.bin").write_text("private\n", encoding="utf-8")
        info = self.src / ".git" / "info" / "exclude"
        info.write_text("worktrees/\n", encoding="utf-8")
        (self.src / "worktrees" / "agent" / "toolchain").mkdir(parents=True)
        (self.src / "worktrees" / "agent" / "toolchain" / "gcc").write_text("big\n", encoding="utf-8")

    def copy(self):
        done = bash(f'copy_checkout "{self.src}" "{self.dest}"')
        self.assertEqual(done.returncode, 0, done.stderr)

    def test_it_takes_what_git_tracks_or_would_and_nothing_ignored(self):
        self.copy()
        self.assertTrue((self.dest / "tools" / "run.py").is_file())
        self.assertTrue((self.dest / "new.py").is_file())
        for ignored in (".env", "captures/backup.bin", "worktrees/agent/toolchain/gcc"):
            self.assertFalse((self.dest / ignored).exists(), ignored)

    def test_what_the_checkout_no_longer_has_goes_but_the_copys_own_captures_stay(self):
        (self.dest / "captures").mkdir(parents=True)
        (self.dest / "captures" / "run.log").write_text("the run's own\n", encoding="utf-8")
        (self.dest / ".venv").mkdir()
        (self.dest / "tools").mkdir()
        (self.dest / "tools" / "gone.py").write_text("stale\n", encoding="utf-8")
        locked = self.dest / ".claude" / "worktrees" / "x" / "toolchain"
        locked.mkdir(parents=True)
        (locked / "gcc").write_text("old copy\n", encoding="utf-8")
        locked.chmod(0o555)
        self.copy()
        self.assertTrue((self.dest / "captures" / "run.log").is_file())
        self.assertTrue((self.dest / ".venv").is_dir())
        self.assertFalse((self.dest / "tools" / "gone.py").exists())
        self.assertFalse((self.dest / ".claude").exists())

    def test_under_pipefail_no_folder_the_checkout_has_goes(self):
        # Enough names for grep -q to stop reading before the writer is done.
        for n in range(3000):
            (self.src / "tools" / f"module_{n:04}.py").write_text("x\n", encoding="utf-8")
        (self.src / "zz-last").mkdir()
        (self.src / "zz-last" / "status.json").write_text("{}\n", encoding="utf-8")
        for attempt in range(5):
            self.copy()
            self.assertTrue((self.dest / "zz-last" / "status.json").is_file(), attempt)
            self.assertTrue((self.dest / "tools" / "module_2999.py").is_file(), attempt)

    def test_a_list_git_cannot_give_leaves_the_copy_as_it_was(self):
        self.copy()
        before = sorted(path.relative_to(self.dest) for path in self.dest.rglob("*"))
        done = bash(f'copy_checkout "{self.dest / "nowhere"}" "{self.dest}"')
        self.assertNotEqual(done.returncode, 0)
        self.assertIn("left as it was", done.stderr)
        self.assertEqual(sorted(path.relative_to(self.dest) for path in self.dest.rglob("*")), before)

    def test_neither_emulator_copies_any_other_way(self):
        for script in ("tools/espressif/emulator/emu.sh", "tools/beken/emulator/beken-emu.sh"):
            text = (ROOT / script).read_text(encoding="utf-8")
            self.assertIn("copy_checkout", text, script)
            self.assertNotIn("rsync", text, script)

    def test_the_windows_vm_copies_only_the_files_git_lists(self):
        # A tar of the whole folder took .claude's worktrees too, and failed
        # when another session removed one part way through.
        self.assertIn('checkout_files "$ROOT" > "$CACHE/repo-files"',
                      (ROOT / "tools/espressif/emulator/windows-test.sh").read_text(encoding="utf-8"))
        vm_run = (ROOT / "tools/espressif/emulator/windows/vm-run.sh").read_text(encoding="utf-8")
        self.assertIn("--null --no-recursion --ignore-failed-read -T /cache/repo-files", vm_run)
        self.assertNotIn("--exclude", vm_run)

    def test_the_list_a_removed_file_is_in_is_still_copied_without_it(self):
        listed = Path(self.dest.parent, "repo-files")
        done = bash(f'checkout_files "{self.src}" > "{listed}"')
        self.assertEqual(done.returncode, 0, done.stderr)
        (self.src / "new.py").unlink()
        archive = Path(self.dest.parent, "repo.tar")
        done = subprocess.run(["tar", "-C", str(self.src), "-cf", str(archive), "--null", "--no-recursion",
                               "--ignore-failed-read", "-T", str(listed)], capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        names = subprocess.run(["tar", "-tf", str(archive)], capture_output=True, text=True).stdout.split()
        self.assertIn("tools/run.py", names)
        self.assertNotIn("new.py", names)
        self.assertFalse([name for name in names if name.startswith(("captures", ".env", "worktrees"))], names)


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class WaitForRoomTests(unittest.TestCase):
    def proc(self, free_mb, load):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        meminfo, loadavg = Path(temp.name, "meminfo"), Path(temp.name, "loadavg")
        meminfo.write_text(f"MemTotal: 8000000 kB\nMemAvailable: {free_mb * 1024} kB\n", encoding="utf-8")
        loadavg.write_text(f"{load} 1.0 1.0 1/100 1\n", encoding="utf-8")
        return {"AUTOFLASH_EMULATOR_MEMINFO": str(meminfo), "AUTOFLASH_EMULATOR_LOADAVG": str(loadavg)}

    def test_with_room_it_starts_at_once(self):
        done = bash("wait_for_room 350", self.proc(2000, 0.5))
        self.assertEqual((done.returncode, done.stderr), (0, ""))

    def test_short_of_memory_it_waits_then_starts_saying_so(self):
        done = bash("wait_for_room 350", {**self.proc(300, 0.5), "AUTOFLASH_EMULATOR_ROOM_WAIT": "0"})
        self.assertEqual(done.returncode, 0)
        self.assertIn("300 MB free", done.stderr)

    def test_a_load_twice_the_cores_counts_as_no_room(self):
        cores = os.cpu_count() or 1
        done = bash("wait_for_room 350", {**self.proc(4000, 2 * cores + 1), "AUTOFLASH_EMULATOR_ROOM_WAIT": "0"})
        self.assertIn("a failure may be the computer's", done.stderr)

    def test_it_waits_for_what_that_emulator_needs(self):
        done = bash("wait_for_room 350", {**self.proc(300, 0.5), "AUTOFLASH_EMULATOR_ROOM_WAIT": "0"})
        self.assertIn("Starting with 300 MB free", done.stderr)   # short of the 350 it needs
        self.assertEqual(bash("wait_for_room 250", self.proc(300, 0.5)).stderr, "")

    def test_each_emulator_says_what_it_needs(self):
        for script, needs in (("tools/espressif/emulator/emu.sh", "room=250; [ \"$chip\" = esp32 ] && room=350"),
                              ("tools/beken/emulator/beken-emu.sh", "wait_for_room 350")):
            self.assertIn(needs, (ROOT / script).read_text(encoding="utf-8"), script)

    def test_where_proc_says_nothing_it_does_not_wait(self):
        done = bash("wait_for_room 350", {"AUTOFLASH_EMULATOR_MEMINFO": "/nonexistent", "AUTOFLASH_EMULATOR_LOADAVG": "/nonexistent"})
        self.assertEqual((done.returncode, done.stderr), (0, ""))


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class LocalApiTests(unittest.TestCase):
    """The API's own code in a container beside an emulated computer, for its
    readouts: both emulators start it so. Docker here a fake that says what it
    was asked, its API container up at 192.0.2.7."""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.tmp = Path(temp.name)
        self.calls = self.tmp / "calls"
        (self.tmp / "bin").mkdir()
        (self.tmp / "bin" / "docker").write_text(
            '#!/bin/sh\necho "$*" >> %s\n'
            'case "$1 $2" in "logs "*) echo "API on 0.0.0.0:8080" ;; "inspect -f")\n'
            '  case "$3" in *Running*) echo true ;; *IPAddress*) echo 192.0.2.7 ;; esac ;; esac\n' % self.calls,
            encoding="utf-8")
        (self.tmp / "bin" / "docker").chmod(0o755)
        self.env = {"PATH": f"{self.tmp}/bin:{os.environ['PATH']}", "ROOT": str(ROOT)}

    def asked(self):
        return self.calls.read_text(encoding="utf-8").splitlines() if self.calls.exists() else []

    def test_it_starts_on_the_network_given_and_says_where_readouts_go(self):
        folder = self.tmp / "state" / "api"
        done = bash(f'local_api_start emu-3-api emu-net "{folder}" X; echo "$LOCAL_API_READOUT_URL"', self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "http://192.0.2.7:8080/v1/readout")
        self.assertTrue(folder.is_dir())
        run = next(line for line in self.asked() if line.startswith("run "))
        self.assertIn("--name emu-3-api --network emu-net", run)
        self.assertIn(f"-v {ROOT}/website:/website:ro -v {folder}:/api", run)
        self.assertTrue(run.endswith("node /website/api/local.mjs 0.0.0.0 8080 /api X"), run)
        self.assertEqual(self.asked()[0], "rm -f emu-3-api")   # one a killed run left goes first

    def test_it_is_stopped_with_what_it_said_kept(self):
        log = self.tmp / "api-server.out"
        done = bash(f'local_api_stop emu-3-api "{log}"', self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("API on", log.read_text(encoding="utf-8"))
        self.assertEqual(self.asked(), ["logs emu-3-api", "rm -f emu-3-api"])

    def test_it_goes_once_the_container_beside_it_stops(self):
        done = bash("local_api_goes_with emu-3 emu-3-api", self.env)
        self.assertEqual(done.returncode, 0, done.stderr)
        for _ in range(100):   # a watcher of its own, in the background
            if "rm -f emu-3-api" in self.asked():
                break
            time.sleep(0.1)
        self.assertEqual(self.asked(), ["inspect emu-3", "wait emu-3", "rm -f emu-3-api"])
