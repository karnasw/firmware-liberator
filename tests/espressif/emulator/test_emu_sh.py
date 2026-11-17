"""emu.sh puts the emulator on a /24 Docker network of its own, on a subnet
nothing on the computer uses yet - its routes, its other Docker networks - so
that making it never takes another network's addresses; and takes --instance
1-99."""

import json
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "tools" / "espressif" / "emulator" / "emu.sh"


def pick(taken: str) -> str:
    """The subnet emu.sh's picker chooses, given what `ip -4 route` and the Docker
    networks' subnets say is taken (one per line)."""
    program = re.search(r"\"\$PY\" -I -c '\n(.*?)'\)", SCRIPT.read_text(encoding="utf-8"), re.S).group(1)
    done = subprocess.run([sys.executable, "-I", "-c", program], input=taken,
                          capture_output=True, text=True, timeout=60)
    if done.returncode:
        raise AssertionError(done.stderr)
    return done.stdout.strip()


def product_seen(folder: Path, before: str = "") -> subprocess.CompletedProcess:
    """emu.sh's product_seen, on the captures, saved devices and instance
    states under `folder`, for device 12-34-56-ab-cd-ef; `before` runs first."""
    program = re.search(r"product_seen\(\) \{\n.*?<<'EOF'\n(.*?)\nEOF\n", SCRIPT.read_text(encoding="utf-8"),
                        re.S).group(1)
    arguments = [str(folder / "captures"), str(folder / "saved"), str(folder / "emulation"), "12-34-56-ab-cd-ef"]
    return subprocess.run([sys.executable, "-I", "-c", before + program, *arguments],
                          capture_output=True, text=True, timeout=60)


class ProductModelTests(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.folder)
        run = self.folder / "emulation/state-2/work/captures/devices/12-34-56-ab-cd-ef/runs/20261113-120000"
        run.mkdir(parents=True)
        (run / "autoflash-state.json").write_text(json.dumps({"inputs": {"product_model": "s60tpg"}}),
                                                  encoding="utf-8")

    def test_another_instances_run_is_where_the_model_is_found(self):
        self.assertEqual(product_seen(self.folder).stdout.strip(), "S60TPG")

    def test_a_run_deleted_while_it_is_read_breaks_nothing(self):
        # Another instance's run removing its record just after it was opened,
        # as asking for its path's time would then find.
        gone = "import os.path\ndef gone(path): raise FileNotFoundError(path)\nos.path.getmtime = gone\n"
        done = product_seen(self.folder, gone)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "S60TPG")


class EmulatorNetworkTests(unittest.TestCase):
    def test_the_first_free_candidate_is_taken(self):
        self.assertEqual(pick("default\n192.0.2.0/24\n172.17.0.0/16\n"), "198.18.60.0/24")

    def test_one_that_overlaps_a_route_or_network_is_passed_over(self):
        self.assertEqual(pick("198.18.0.0/15\n"), "172.31.60.0/24")          # a route wider than it
        self.assertEqual(pick("198.18.60.128/25\n172.16.0.0/12\n"), "10.231.60.0/24")

    def test_none_free_is_none(self):
        self.assertEqual(pick("198.18.0.0/15\n172.16.0.0/12\n10.0.0.0/8\n"), "")

    def test_ipv6_and_blank_lines_are_ignored(self):
        self.assertEqual(pick("\nfd00::/64\ndefault\n"), "198.18.60.0/24")


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class InstanceTests(unittest.TestCase):
    """--instance N is 1-99: enough for every core of a big computer, and a
    range of numbers of its own for each session sharing one."""

    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)

    def test_up_to_99_is_taken(self):
        for instance in ("1", "30", "31", "99"):
            # --cloud with --stub-cloud is refused next, before anything is started.
            for args in (("--instance", instance), (f"--instance={instance}",)):
                done = self.run_script(*args, "--cloud", "--stub-cloud")
                self.assertEqual(done.returncode, 2, args)
                self.assertIn("--stub-cloud cannot be combined with --cloud", done.stderr, args)

    def test_others_are_refused(self):
        for instance in ("0", "05", "100", "x"):
            done = self.run_script(f"--instance={instance}", "--cloud", "--stub-cloud")
            self.assertEqual(done.returncode, 2, instance)
            self.assertIn("--instance needs a number, 1-99", done.stderr, instance)



class ApiTests(unittest.TestCase):
    """Its computer's readouts go to the API's own code beside it, for both
    sides of a readout to be seen here, unless told --no-api; the API goes
    with the emulator (emulator-room.sh's local_api_goes_with)."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")

    def test_the_api_starts_beside_it_before_it_and_goes_with_it(self):
        start = self.text.index('local_api_start "$NAME-api" "$NETWORK" "$STATE/api"')
        self.assertLess(start, self.text.index('exec "${awake[@]}" docker run'))
        self.assertIn('local_api_goes_with "$NAME" "$NAME-api"', self.text)
        self.assertIn('api_env=(-e AUTOFLASH_READOUT_URL="$LOCAL_API_READOUT_URL")', self.text)
        self.assertIn('-e AUTOFLASH_EMULATOR_URL="http://localhost:$PORT" "${api_env[@]}"', self.text)

    def test_no_api_leaves_it_out_and_windows_has_none(self):
        self.assertIn("--no-api) api=0 ;;", self.text)
        self.assertIn('if [ "$api" = 1 ] && [ "$windows" = 0 ]; then', self.text)

    @unittest.skipIf(shutil.which("bash") is None, "no bash")
    def test_no_api_is_in_its_usage(self):
        done = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn("--no-api", done.stdout)

    @unittest.skipIf(shutil.which("bash") is None, "no bash")
    def test_its_usage_is_its_whole_header(self):
        done = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60)
        self.assertTrue(done.stdout.endswith("each device's flash is kept there between runs.\n"), done.stdout)


if __name__ == "__main__":
    unittest.main()
