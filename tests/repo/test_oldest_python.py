import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from tools.repo import newer_python, oldest_python


class ImageTests(unittest.TestCase):
    def test_it_is_the_oldest_python_pinned_to_its_digest(self):
        name, digest = oldest_python.IMAGE.split("@")
        self.assertTrue(name.startswith(f"python:{newer_python.said(newer_python.OLDEST)}."), name)
        self.assertRegex(digest, r"^sha256:[0-9a-f]{64}$")

    def test_every_test_is_run_with_cis_packages_on_the_clone_as_you(self):
        command = oldest_python.docker_command(Path("/scratch/tree"))
        self.assertEqual(command[:3], ["docker", "run", "--rm"])
        self.assertIn("/scratch/tree:/tree", command)
        self.assertIn(oldest_python.IMAGE, command)
        self.assertIn("--require-hashes --no-deps -r requirements/ci.txt", command[-1])
        self.assertTrue(command[-1].endswith("tests/run.py"))
        if hasattr(os, "getuid"):
            self.assertIn(f"{os.getuid()}:{os.getgid()}", command)


class CloneTests(unittest.TestCase):
    def git(self, *args, cwd):
        subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "-c", "commit.gpgsign=false", *args], cwd=cwd, check=True, capture_output=True)

    def test_head_is_cloned_and_not_what_is_uncommitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, "source")
            source.mkdir()
            self.git("init", "-q", cwd=source)
            (source / "a.txt").write_text("committed", encoding="utf-8")
            self.git("add", "a.txt", cwd=source)
            self.git("commit", "-q", "-m", "one", cwd=source)
            (source / "a.txt").write_text("not committed", encoding="utf-8")
            with mock.patch.object(oldest_python, "ROOT", source):
                commit = oldest_python.clone_head(Path(tmp, "clone"))
            self.assertEqual(Path(tmp, "clone", "a.txt").read_text(encoding="utf-8"), "committed")
            head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=source, capture_output=True,
                                  text=True, check=True).stdout.strip()
            self.assertEqual(commit, head)


class MainTests(unittest.TestCase):
    def test_without_docker_it_says_so(self):
        with mock.patch.object(oldest_python.shutil, "which", return_value=None), \
                redirect_stderr(io.StringIO()) as err:
            self.assertEqual(oldest_python.main([]), 2)
        self.assertIn("Docker is needed", err.getvalue())

    def test_its_answer_is_the_tests(self):
        with mock.patch.object(oldest_python.shutil, "which", return_value="/usr/bin/docker"), \
                mock.patch.object(oldest_python, "clone_head", return_value="abc1234"), \
                mock.patch.object(oldest_python.subprocess, "run", return_value=mock.Mock(returncode=1)), \
                mock.patch("sys.stdout", io.StringIO()) as out:
            self.assertEqual(oldest_python.main([]), 1)
        self.assertIn(f"Python {newer_python.said(newer_python.OLDEST)}: FAILED", out.getvalue())


if __name__ == "__main__":
    unittest.main()
