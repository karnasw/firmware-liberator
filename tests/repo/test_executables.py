"""Scripts run from the command line, one rule: in tools/, a file with a
shebang is executable, an executable file has one, and a Python module with
a __main__ block is such a script. A module only imported has neither."""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Hashed into the Espressif emulator image's tag, and made executable by its
# Dockerfile: left as they are until the image changes for its own sake.
HASHED = "tools/espressif/emulator/container/"


def tracked() -> dict[str, str]:
    """Each file git tracks under tools/, and its mode as git records it."""
    listed = subprocess.run(["git", "ls-files", "-s", "-z", "--", "tools"], cwd=ROOT, capture_output=True,
                            check=True).stdout.decode("utf-8")
    found = {}
    for entry in listed.split("\0"):
        if entry:
            info, path = entry.split("\t", 1)
            if not path.startswith(HASHED):
                found[path] = info.split()[0]
    return found


def shebang(path: str) -> bool:
    with open(ROOT / path, "rb") as file:
        return file.read(2) == b"#!"


@unittest.skipIf(shutil.which("git") is None or not (ROOT / ".git").exists(), "not a git checkout")
class ExecutableTests(unittest.TestCase):
    def test_a_file_with_a_shebang_is_executable_and_only_such_a_file(self):
        wrong = [f"{path} ({'has' if shebang(path) else 'has no'} shebang, mode {mode})"
                 for path, mode in tracked().items() if shebang(path) != (mode == "100755")]
        self.assertEqual(wrong, [], "chmod them, as git update-index --chmod=+x (or -x) does")

    def test_a_module_with_a_main_block_is_a_script(self):
        wrong = [path for path in tracked() if path.endswith(".py") and not shebang(path)
                 and 'if __name__ == "__main__":' in (ROOT / path).read_text(encoding="utf-8")]
        self.assertEqual(wrong, [], "give each #!/usr/bin/env python3, and make it executable")


if __name__ == "__main__":
    unittest.main()
