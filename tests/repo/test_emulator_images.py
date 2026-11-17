"""The two emulators' images, kept alike.

What an image is built from is hashed into its tag (image.sh tag), so a change
to any of it, a comment included, is a new image for every computer. A change
that would only make the two emulators alike there waits for that image's
next: write its test to let the old form be while `image.sh tag` is today's
tag, so it fails once an image comes without it.
"""

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EMULATORS = {"espressif": ROOT / "tools/espressif/emulator", "beken": ROOT / "tools/beken/emulator"}
PIN_FIELDS = ("repo", "commit", "archive", "sha256")
# A patch: its place in the order, then a few lowercase words saying what it does.
PATCH_NAME = re.compile(r"^\d{4}(-[a-z0-9]+){1,7}\.patch$")


def image_sh(folder: Path, *args: str) -> str:
    return subprocess.run(["bash", str(folder / "image.sh"), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def upstream(folder: Path) -> dict[str, str]:
    path = folder / "UPSTREAM"
    if not path.is_file():
        return {}
    return dict(line.split(" ", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#"))


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash and sha256sum")
class NextImageTests(unittest.TestCase):
    def test_each_upstream_pin_is_in_the_one_form(self):
        """repo, commit, archive and sha256 in UPSTREAM, and nowhere else."""
        for family, folder in EMULATORS.items():
            with self.subTest(family=family):
                self.assertEqual(tuple(upstream(folder)), PIN_FIELDS, "UPSTREAM: image.sh pin writes it")
                self.assertRegex(upstream(folder)["commit"], r"^[0-9a-f]{40}$")
        dockerfile = (EMULATORS["espressif"] / "emu.Dockerfile").read_text(encoding="utf-8")
        self.assertNotRegex(dockerfile, r"(?m)^ARG QEMU_SOURCE_SHA256=", "pinned twice: UPSTREAM is the pin")

    def test_each_source_release_is_named_after_its_upstream_project(self):
        """emulator-source-<the archive's project>-<commit>: the Beken one's was
        named after the family, and keeps that name for the archive it holds."""
        for family, folder in EMULATORS.items():
            with self.subTest(family=family):
                release = image_sh(folder, "release")
                pin = upstream(folder)
                if family == "beken" and release == "emulator-source-beken-d9a3772":
                    self.assertEqual(pin["sha256"], "f4e3169f0b240e1978febafe2bb8452c5f6f8727ebb8ff4ac36c18a890588abf",
                                     "the pin moved: take the earlier release's name out of image.sh")
                    continue
                archive = pin.get("archive", "qemu-")
                self.assertRegex(release, rf"^emulator-source-{re.escape(archive.split('-')[0])}-[0-9a-f]{{7}}$")

    def test_each_emulators_files_are_named_after_its_image(self):
        """emu.sh and emu.Dockerfile for autoflash-emu, beken-emu.sh and
        beken-emu.Dockerfile for autoflash-beken-emu."""
        beken = EMULATORS["beken"]
        self.assertTrue((beken / "beken-emu.sh").is_file())
        self.assertTrue((beken / "beken-emu.Dockerfile").is_file())
        self.assertTrue((EMULATORS["espressif"] / "emu.sh").is_file())
        self.assertTrue((EMULATORS["espressif"] / "emu.Dockerfile").is_file())
        self.assertEqual(sorted(path.name for path in beken.glob("bk-emu*")), [])
        hashed = [beken / "UPSTREAM", beken / "requirements.txt", beken / "beken-emu.Dockerfile",
                  *sorted((beken / "patches").glob("*.patch"))]
        self.assertEqual([path.name for path in hashed if "bk-emu" in path.read_text(encoding="utf-8")], [],
                         "names the script by its earlier name: beken-emu.sh")

    def test_the_espressif_image_speaks_utf8(self):
        """Its tmux session is the terminal a run is watched in: with no UTF-8
        locale the tmux client draws the spinner's braille as a still _.  The
        Beken emulator has no tmux, and Python writes UTF-8 without a locale."""
        dockerfile = (EMULATORS["espressif"] / "emu.Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(dockerfile, r"(?m)^ENV .*\bLANG=C\.UTF-8\b")

    def test_each_patch_is_named_alike(self):
        """NNNN-what-it-does.patch, numbered in the order they are applied."""
        for family, folder in EMULATORS.items():
            with self.subTest(family=family):
                names = sorted(path.name for path in (folder / "patches").glob("*.patch"))
                self.assertTrue(names)
                self.assertEqual([name for name in names if not PATCH_NAME.match(name)], [])
                self.assertEqual([name[:4] for name in names], [f"{n:04d}" for n in range(1, len(names) + 1)])


@unittest.skipUnless(shutil.which("bash") and shutil.which("sha256sum"), "needs bash and sha256sum")
class PinHelperTests(unittest.TestCase):
    """The helpers that move a pin write it in the one form."""

    def copy(self, family: str, names: tuple[str, ...]) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name) / "tools" / family / "emulator"
        folder.mkdir(parents=True)
        for name in names:
            source = EMULATORS[family] / name
            (shutil.copytree if source.is_dir() else shutil.copyfile)(source, folder / name)
        return folder

    def test_the_espressif_pin_moves_into_upstream(self):
        """From emu.Dockerfile the first time, and from UPSTREAM after."""
        pinned = ("UPSTREAM",) if upstream(EMULATORS["espressif"]) else ()
        folder = self.copy("espressif", ("image.sh", "emu.Dockerfile", "patches", "container", *pinned))
        before = image_sh(folder, "tag")
        archive = folder.parent / "qemu-lcgamboa-1234567.tar.xz"
        archive.write_bytes(b"a made-up archive")
        image_sh(folder, "pin", str(archive), "1234567" + "0" * 33)
        pin = upstream(folder)
        self.assertEqual(tuple(pin), PIN_FIELDS)
        self.assertEqual((pin["archive"], pin["commit"][:7]), (archive.name, "1234567"))
        self.assertNotRegex((folder / "emu.Dockerfile").read_text(encoding="utf-8"), r"(?m)^ARG QEMU_SOURCE_SHA256=")
        self.assertRegex((folder / "emu.Dockerfile").read_text(encoding="utf-8"), r"(?m)^ARG QEMU_SOURCE_SHA256$")
        self.assertEqual(image_sh(folder, "release"), "emulator-source-qemu-1234567")
        self.assertNotEqual(image_sh(folder, "tag"), before)

    def test_a_new_beken_pin_names_its_release_after_upstream(self):
        folder = self.copy("beken", ("image.sh", "UPSTREAM", "requirements.txt", "beken-emu.Dockerfile", "patches"))
        self.assertEqual(image_sh(folder, "release"), "emulator-source-beken-d9a3772")
        text = (folder / "UPSTREAM").read_text(encoding="utf-8")
        (folder / "UPSTREAM").write_text(re.sub(r"(?m)^sha256 .*$", "sha256 " + "0" * 64, text), encoding="utf-8")
        self.assertEqual(image_sh(folder, "release"), "emulator-source-bekenemulator-d9a3772")


if __name__ == "__main__":
    unittest.main()
