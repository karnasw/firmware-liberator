import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.espressif.test_inspect_stock_nvs import entry, page, variable
from tools.espressif.emulator import redact


def flash(nvs: bytes) -> bytes:
    return (b"\xff" * redact.NVS_OFFSET + nvs).ljust(redact.NVS_OFFSET + redact.NVS_SIZE, b"\xff")


ROOT = Path(__file__).resolve().parents[3]


def emulator_folder(script: str, temp: str) -> Path:
    folder = Path(temp, "work/tools/espressif/emulator")
    folder.mkdir(parents=True)
    (folder / script).write_bytes((ROOT / "tools/espressif/emulator" / script).read_bytes())
    return folder


def under_an_earlier_release(script: str, temp: str) -> Path:
    """The script where the device's supervisor runs it under an earlier
    release's tool (unattended-test.sh --code): that release's tools/ around it -
    one from before the reorganisation has no tools/autoflash or flashmap -
    and this checkout's kept in its folder."""
    folder = emulator_folder(script, temp)
    for package in ("tools", "tools/espressif"):
        Path(temp, "work", package, "__init__.py").write_text("", encoding="utf-8")
    (folder / "checkout").mkdir()
    for kept in ("tools", "devices"):
        (folder / "checkout" / kept).symlink_to(ROOT / kept)
    return folder / script


def after_an_earlier_release(script: str, temp: str) -> Path:
    """The script in this checkout's copy again, after such a run: copy_checkout
    removed the kept copy's files, but not its folders."""
    folder = emulator_folder(script, temp)
    work = Path(temp, "work")
    for linked in ("tools/autoflash", "tools/espressif/flashmap.py", "tools/espressif/inspect_stock_nvs.py",
                   "tools/espressif/__init__.py", "tools/__init__.py", "devices"):
        (work / linked).parent.mkdir(parents=True, exist_ok=True)
        (work / linked).symlink_to(ROOT / linked)
    for emptied in ("tools/autoflash", "tools/espressif", "devices/sonoff-s60"):
        (folder / "checkout" / emptied).mkdir(parents=True)
    return folder / script


class RedactTests(unittest.TestCase):
    def test_masks_a_dumps_secret(self):
        nvs = page([entry(0, 0x01, "user_usual", data=b"\x01"), *variable(1, 0x21, "content", b'{"pw":"hunter22"}\0')])
        apply = redact.redactor(flash(nvs))
        self.assertEqual(apply(b"key hunter22\n"), b"key <redacted:user_usual.pw>\n")

    def test_flash_with_no_secrets_passes_lines_through(self):
        # A public device's flash has no Sonoff NVS: the log is kept, not lost.
        apply = redact.redactor(b"\xff" * 0x400000)
        self.assertEqual(apply(b"boot line\n"), b"boot line\n")
        self.assertEqual(apply(b"0x3fc8a000: 0x00000000 0x12345678\n"), b"")

    def test_it_runs_under_an_earlier_releases_tool(self):
        # Its import failing stopped the device at every start, after an earlier
        # release's run swapped its tree in, and nothing reached the log.
        with tempfile.TemporaryDirectory() as temp:
            script = under_an_earlier_release("redact.py", temp)
            Path(temp, "flash.bin").write_bytes(b"\xff" * 0x400000)
            done = subprocess.run([sys.executable, "-I", str(script), str(Path(temp, "flash.bin"))],
                                  input=b"boot line\n", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"boot line\n")

    def test_it_runs_after_an_earlier_releases_tool(self):
        # The kept copy emptied by the next copy of the checkout: taken for a
        # copy still, it stopped the device at every start, as the first had.
        with tempfile.TemporaryDirectory() as temp:
            script = after_an_earlier_release("redact.py", temp)
            Path(temp, "flash.bin").write_bytes(b"\xff" * 0x400000)
            done = subprocess.run([sys.executable, "-I", str(script), str(Path(temp, "flash.bin"))],
                                  input=b"boot line\n", capture_output=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout, b"boot line\n")


if __name__ == "__main__":
    unittest.main()
