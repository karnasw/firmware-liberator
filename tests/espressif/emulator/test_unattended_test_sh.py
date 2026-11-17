"""unattended-test.sh takes --instance=N as emu.sh does, 1-99; sends readouts to
the API Worker's own code, in a container of its own beside the emulator, unless
told --no-api; and runs the device as with LAN Control off if told
--no-lan-control."""

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "tools" / "espressif" / "emulator" / "unattended-test.sh"
ROOM = ROOT / "tools" / "repo" / "emulator-room.sh"


@unittest.skipIf(shutil.which("bash") is None, "no bash")
class InstanceTests(unittest.TestCase):
    def run_script(self, *args):
        return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=60)

    def test_up_to_99_is_taken(self):
        for instance in ("1", "30", "31", "99"):
            # --keep with --save is refused next, before anything is started.
            done = self.run_script(f"--instance={instance}", "--keep", "--save=x")
            self.assertEqual(done.returncode, 2, instance)
            self.assertIn("--save turns the device off", done.stderr, instance)

    def test_others_are_refused(self):
        for instance in ("0", "05", "100", "x"):
            done = self.run_script(f"--instance={instance}", "--keep", "--save=x")
            self.assertEqual(done.returncode, 2, instance)
            self.assertIn("--instance=N", done.stdout, instance)  # the usage
            self.assertNotIn("--save turns the device off", done.stderr, instance)


class ApiTests(unittest.TestCase):
    """Seen: run on this computer, under its Node, the API was out of reach of
    the emulated computer wherever a firewall turned the emulators' network
    away, and every readout-all scenario failed as if nothing had been sent.
    Its container is emulator-room.sh's, which both emulators share."""

    def setUp(self):
        self.text = SCRIPT.read_text(encoding="utf-8")
        self.room = ROOM.read_text(encoding="utf-8")

    def test_the_api_runs_in_a_container_on_the_emulators_network(self):
        start = re.search(r'docker run -d --name "\$name" --network "\$network".*?\n(?:.*\\\n)*.*', self.room)
        self.assertIsNotNone(start, "the API's docker run")
        self.assertIn('"$LOCAL_API_IMAGE" node /website/api/local.mjs', start.group(0))
        self.assertIn('local_api_start "$api_name" "$network"', self.text)

    def test_its_image_is_pinned_by_digest(self):
        self.assertRegex(self.room, r"(?m)^LOCAL_API_IMAGE=node:22\.\d+\.\d+-[a-z-]+@sha256:[0-9a-f]{64}$")

    def test_nothing_runs_node_on_this_computer(self):
        for text in (self.text, self.room):
            for line in text.splitlines():
                code = line.split("#", 1)[0]
                if re.search(r"(^|[\s\"'])(\$\{AUTOFLASH_NODE[^}]*\}|node)[\"']?\s+\S*local\.mjs", code):
                    self.assertIn('"$LOCAL_API_IMAGE" node', code, line)

    def test_readouts_go_to_it_unless_told_no_api(self):
        # Asked for: both sides of a readout seen here, the tool's and the API's.
        self.assertRegex(self.text, r"(?m)^emu_args=.* api=1$")
        self.assertIn("--no-api) api=0 ;;", self.text)
        # Kept unsent with --no-api even where emu.sh gave the emulator an API of its own.
        self.assertIn('stub_env+=(-e AUTOFLASH_READOUT_URL="${AUTOFLASH_READOUT_URL:-}")', self.text)

    def test_the_emulator_it_starts_has_no_api_of_its_own(self):
        # emu.sh's would outlive the run, which starts one afresh, its own.
        self.assertRegex(self.text, r"(?m)^emu_args=\(--detach --no-api\)")

    def test_no_api_is_in_its_usage(self):
        done = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 2)
        self.assertIn("--no-api", done.stdout)

    def test_its_usage_is_its_whole_header(self):
        done = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60)
        self.assertTrue(done.stdout.startswith("A whole conversion of the emulated device"), done.stdout)
        self.assertTrue(done.stdout.endswith("is a placeholder the stub ignores.\n"), done.stdout)


class SavedDeviceTests(unittest.TestCase):
    text = SCRIPT.read_text(encoding="utf-8")

    def test_the_stubs_record_of_the_device_goes_with_it(self):
        # eWeLink keeps its record of a device however it is started again.
        self.assertIn('cp "$STATE/stub-reported.json" "$SAVED/$save/stub-reported.json"', self.text)
        self.assertIn('cp "$SAVED/$from/stub-reported.json" "$STATE/stub-reported.json"', self.text)
        self.assertIn('rm -f "$STATE/stub-reported.json"', self.text)   # not the last device's, if it has none
        emu = (SCRIPT.parent / "emu.sh").read_text(encoding="utf-8")
        self.assertIn('"$STATE/.flash-mode" "$STATE/stub-reported.json"', emu)   # --fresh: newly paired


class LanControlTests(unittest.TestCase):
    """--no-lan-control: the device as with LAN Control off in the app, its
    discovery answers and LAN port kept from the tool for that run alone."""
    text = SCRIPT.read_text(encoding="utf-8")

    def test_its_discovery_answers_and_lan_port_are_kept_from_the_tool(self):
        self.assertIn("--no-lan-control) lan_control=0 ;;", self.text)
        self.assertIn('"INPUT -s $DEVICE -p udp --sport 5353 -j DROP"', self.text)
        self.assertIn('"OUTPUT -d $DEVICE -p tcp --dport 8081 -j REJECT --reject-with tcp-reset"', self.text)
        # Taken off first, so a run without it, on an emulator kept running, has them.
        self.assertIn('while docker exec "$NAME" iptables -D $rule', self.text)

    @unittest.skipIf(shutil.which("bash") is None, "no bash")
    def test_it_is_in_the_usage(self):
        done = subprocess.run(["bash", str(SCRIPT), "--bogus"], capture_output=True, text=True, timeout=60)
        self.assertIn("--no-lan-control", done.stdout)

if __name__ == "__main__":
    unittest.main()
