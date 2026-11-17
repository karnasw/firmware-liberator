"""Every script that finds the repository from its own path finds it: a file
moved to another depth (the 2026-10 reorganisation moved tools/emulator/ two
levels down) must count its parents again."""

import ast
import json
import os
import re
import subprocess
import tempfile
import unittest
from pathlib import Path

from tools.autoflash import hostsys, kinds

ROOT = Path(__file__).resolve().parents[2]
# `Path(__file__).resolve().parents[N]`, and any `/ "name"` parts joined to it.
PARENTS = re.compile(r'Path\(__file__\)\.resolve\(\)\.parents\[(\d+)\]((?:\s*/\s*"[^"]+")*)')
GITIGNORED = {line.strip().strip("/") for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
              if line.strip() and not line.startswith("#")}


def repository_files(*patterns):
    """The files git tracks or would, from its own lists: walking the checkout
    takes a minute under load, most of it in the ignored captures/."""
    listed = subprocess.run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *patterns],
                            cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / name for name in sorted(set(listed.split("\0")))
            if name and not name.startswith(".claude/") and (ROOT / name).is_file()]


class RepositoryRootTests(unittest.TestCase):
    def test_every_parents_index_reaches_the_repository(self):
        """In the tools, and in their tests, which move with them."""
        wrong = []
        for file in repository_files("tools/*.py", "tests/*.py"):
            for match in PARENTS.finditer(file.read_text(encoding="utf-8")):
                parent = file.resolve().parents[int(match.group(1))]
                parts = re.findall(r'"([^"]+)"', match.group(2))
                if not parts:
                    # Bare: the repository root, or a test's own tests/.
                    ok = parent == ROOT or parent == ROOT / "tests" and file.is_relative_to(ROOT / "tests")
                else:
                    target = parent.joinpath(*parts)
                    # Joined to it: something in the repository, or a directory
                    # the repository makes at run time (captures/) off its root.
                    ok = target.exists() or (parent == ROOT and parts[0] in GITIGNORED)
                if not ok:
                    wrong.append("%s: %s" % (file.relative_to(ROOT), match.group(0)))
        self.assertEqual(wrong, [])

    def test_every_relative_link_in_the_docs_names_a_file(self):
        """A Markdown link to a path, as GitHub follows it from the file it is in;
        code (where a link is only an example) aside."""
        link = re.compile(r"\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)|^\[[^\]]+\]:\s*(\S+)", re.MULTILINE)
        tracked = subprocess.run(["git", "ls-files", "*.md"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.split()
        wrong = []
        for name in tracked:
            text = (ROOT / name).read_text(encoding="utf-8")
            text = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.DOTALL))
            for match in link.finditer(text):
                href = match.group(1) or match.group(2)
                if re.match(r"[a-z][a-z0-9+.-]*:|[#/<]", href, re.IGNORECASE):
                    continue
                target = ((ROOT / name).parent / href.split("#")[0]).resolve()
                if not target.exists() or ROOT not in target.parents and target != ROOT:
                    wrong.append(f"{name}: {href}")
        self.assertEqual(wrong, [])

    def test_what_a_page_names_of_the_code_is_there(self):
        """Specifics live in the code (CONTRIBUTING.md): a page names where to
        look, and a rename leaves it pointing nowhere. Each path a page gives
        in backticks under the project's own folders is there; each
        UPPER_CASE name a maintainer's or contributor's page gives is in the
        code. (Device and research pages name firmware's own constants, which
        this project's code has no reason to hold.)"""
        own = ("tools/", "tests/", "website/", ".github/", "devices/", "docs/", "artifacts/")
        # Not the project's: examples of a shape, and paths in another project's tree.
        elsewhere = {"AGENTS.md": {"tools/a/b.py", "tests/a/test_b.py"},
                     "tools/beken/emulator/README.md": {"tests/self_test.py", "tests/tapnet.py",
                                                        "tests/wifi_peers.py"}}
        tracked = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                                 check=True).stdout.split()
        there = set(tracked) | {"/".join(name.split("/")[:n]) for name in tracked for n in range(1, name.count("/") + 1)}
        pages = [name for name in tracked if name.endswith(".md") and not name.startswith("archive/")
                 and name != "CHANGELOG.md"]   # released sections are history
        maintainers = [name for name in pages if name.startswith((".github/", "website/"))
                       or name in ("AGENTS.md", "CONTRIBUTING.md", "docs/testing.md")]
        code = "\n".join((ROOT / name).read_text(encoding="utf-8", errors="replace") for name in tracked
                         if not name.endswith(".md") and not name.startswith("artifacts/")
                         and (ROOT / name).is_file())
        constant = re.compile(r"[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
        wrong = []
        for name in pages:
            text = re.sub(r"```.*?```", "", (ROOT / name).read_text(encoding="utf-8"), flags=re.DOTALL)
            for quoted in re.findall(r"`([^`\n]+)`", text):
                path = (quoted.split() or [""])[0].rstrip("/").split("#")[0]
                if path.startswith(own) and not re.search(r"[<>*{}$]", path) and path not in there \
                        and path not in elsewhere.get(name, ()) \
                        and subprocess.run(["git", "check-ignore", "-q", path], cwd=ROOT).returncode:
                    wrong.append(f"{name}: `{quoted}` is not there")
                if name in maintainers and constant.fullmatch(quoted) and quoted not in code:
                    wrong.append(f"{name}: `{quoted}` is in no code")
        self.assertEqual(wrong, [])

    def test_every_windows_path_names_a_file(self):
        """`tools\\...\\name.ext` in the PowerShell, batch and prose: Windows runs them
        from the checkout root (C:\\Autoflash\\repo in the emulator's VM)."""
        windows = re.compile(r"tools\\[A-Za-z0-9_\\.-]+\.(?:ps1|py|cmd)")
        wrong = []
        for file in repository_files("*.ps1", "*.cmd", "*.md"):
            if file.relative_to(ROOT).parts[0] == "archive":
                continue
            for path in set(windows.findall(file.read_text(encoding="utf-8"))):
                if not (ROOT / path.replace("\\", "/")).is_file():
                    wrong.append("%s: %s" % (file.relative_to(ROOT), path))
        self.assertEqual(wrong, [])

    def test_every_script_the_docs_run_as_a_command_can_be_run(self):
        """`tools/.../name.py args` at the start of a line in a shell block - after
        any `NAME=value` settings - is run as it is, so it must be executable."""
        command = re.compile(r"^\s*(?:\\\s*)?(?:[A-Z_0-9]+=\S+\s+)*(tools/\S+\.(?:py|sh))\b", re.M)
        block = re.compile(r"^```(?:sh|bash)\n(.*?)^```", re.M | re.S)
        wrong = []
        for file in repository_files("*.md"):
            if file.relative_to(ROOT).parts[0] == "archive":
                continue
            for code in block.findall(file.read_text(encoding="utf-8")):
                for path in set(command.findall(code)):
                    if (ROOT / path).is_file() and not os.access(ROOT / path, os.X_OK):
                        wrong.append("%s: %s" % (file.relative_to(ROOT), path))
        self.assertEqual(sorted(set(wrong)), [])

    def test_every_helper_the_tool_launches_runs_on_a_bare_python(self):
        """The scripts the tool starts in a process of their own (hostsys.python_script),
        on a Python with nothing but the script's own folder on sys.path - the
        portable one start.cmd fetches, which -I stands in for here."""
        helpers = ("tools/ewelink/query_ota.py", "tools/espressif/serve_tasmota_ota.py",
                   "tools/espressif/safeboot/serve_safeboot_migration.py")
        with tempfile.TemporaryDirectory() as elsewhere:
            for helper in helpers:
                with self.subTest(helper=helper):
                    command = hostsys.python_script(ROOT / helper, "--help")
                    done = subprocess.run([command[0], "-I", *command[1:]], cwd=elsewhere,
                                          capture_output=True, text=True, timeout=120)
                    self.assertEqual(done.returncode, 0, done.stderr)
                    self.assertIn("usage:", done.stdout)


RESEARCH_INDEX = ROOT / "docs" / "device-research.md"
EMULATOR_PAGE = ROOT / "docs" / "emulator.md"


def research_pages():
    """Each device's research.md, beside its files, and each family's
    docs/<family>-research.md."""
    return [page for page in repository_files("*research.md")
            if page.name == "research.md" and page.relative_to(ROOT).parts[0] == "devices"
            or page.parent == ROOT / "docs" and page.name.endswith("-research.md") and page != RESEARCH_INDEX]


def emulator_readmes():
    """Each chip family's emulator, described beside its code: tools/<family>/emulator/."""
    return [page for page in repository_files("tools/*/emulator/README.md") if len(page.relative_to(ROOT).parts) == 4]


def unlisted(index: str, pages, where: Path = RESEARCH_INDEX) -> list[str]:
    """The pages a Markdown file at `where`, its text `index`, does not link to."""
    linked = {os.path.normpath(where.parent / href.split("#")[0])
              for href in re.findall(r"\]\(([^)\s]+)\)", index)}
    return [str(page.relative_to(ROOT)) for page in pages if os.path.normpath(page) not in linked]


class ResearchIndexTests(unittest.TestCase):
    def test_every_research_page_is_listed_in_the_index(self):
        """A device's research is kept with its files, and found from
        docs/device-research.md: a new one has to be listed there too."""
        self.assertEqual(unlisted(RESEARCH_INDEX.read_text(encoding="utf-8"), research_pages()), [])

    def test_there_is_research_to_list(self):
        pages = {str(page.relative_to(ROOT)) for page in research_pages()}
        self.assertLessEqual({"docs/espressif-research.md", "docs/beken-research.md",
                              "devices/sonoff-s61/research.md"}, pages)

    def test_a_page_the_index_leaves_out_is_named(self):
        index = "- [S61](../devices/sonoff-s61/research.md#its-wiring)\n- [Beken](beken-research.md)\n"
        pages = [ROOT / "devices/sonoff-s61/research.md", ROOT / "docs/beken-research.md",
                 ROOT / "devices/sonoff-x/research.md"]
        self.assertEqual(unlisted(index, pages), ["devices/sonoff-x/research.md"])


class EmulatorPageTests(unittest.TestCase):
    def test_the_emulator_page_links_each_chip_familys_emulator(self):
        """docs/emulator.md is how to use them; how each works is beside its code."""
        self.assertEqual(unlisted(EMULATOR_PAGE.read_text(encoding="utf-8"), emulator_readmes(), EMULATOR_PAGE), [])

    def test_there_are_both_emulators_to_link(self):
        self.assertLessEqual({"tools/espressif/emulator/README.md", "tools/beken/emulator/README.md"},
                             {str(page.relative_to(ROOT)) for page in emulator_readmes()})



class ChipFamilyTests(unittest.TestCase):
    def test_each_chip_family_is_laid_out_as_the_others(self):
        """Each family the tool converts has the same pages in the same places:
        its tooling's README, its emulator's, its research, and its part of the
        emulator page."""
        families = sorted({kind.family for kind in kinds.EVERY})
        self.assertEqual(families, ["beken", "espressif"])
        page = EMULATOR_PAGE.read_text(encoding="utf-8")
        for family in families:
            with self.subTest(family=family):
                for name in (f"tools/{family}/README.md", f"tools/{family}/emulator/README.md",
                             f"docs/{family}-research.md"):
                    self.assertTrue((ROOT / name).is_file(), name)
                self.assertIn(f"\n## The {family.capitalize()} emulator\n", page)

# A test module named for what it holds rather than for one module of tools/:
# a test across modules, or one whose subject lives elsewhere, each with why.
# Every other test module outside tests/repo/ - the repository's own tests,
# each named for what it holds - mirrors the module it tests.
NOT_MIRRORED = {
    "tests/autoflash/test_windows_uac.py": "Windows' permission prompt answered every way, from "
                                           "hostsys.elevated through the firewall rule it guards to "
                                           "what firewall.py then says",
    "tests/autoflash/test_route_screens.py": "every route's whole run, through runner.py's loop and "
                                             "output.py's drawing, read back with "
                                             "tools/repo/unattended_run.py",
    "tests/autoflash/test_end_screens.py": "every way a run ends, drawn by report.py, readout.py and "
                                           "output.py, read back and checked with "
                                           "tools/repo/unattended_run.py",
}


def mirrored_name(path: Path) -> str:
    """What tests/.../test_<name>.py calls a file of tools/: a module by its
    name, a script by its whole file name with '-' and '.' as '_' (emu.sh,
    test_emu_sh.py)."""
    return path.stem if path.suffix == ".py" else path.name.replace("-", "_").replace(".", "_")


def unmirrored(root: Path, tests: list[Path]) -> list[str]:
    """The test modules outside tests/repo/ that name no module or script at
    the same place under tools/ - nor the package itself, as
    tests/autoflash/routes/test_routes.py names routes/__init__.py."""
    wrong = []
    for test in tests:
        relative = test.relative_to(root / "tests")
        if relative.parts[0] == "repo" or not test.name.startswith("test_"):
            continue
        name, folder = test.stem.removeprefix("test_"), root.joinpath("tools", *relative.parts[:-1])
        names = {mirrored_name(path) for path in folder.iterdir() if path.is_file()} if folder.is_dir() else set()
        if folder.name == name and (folder / "__init__.py").is_file():
            names.add(name)
        if name not in names:
            wrong.append(str(test.relative_to(root)))
    return wrong


def after_main_guard(text):
    """What a test module has past `if __name__ == "__main__":` beyond its
    unittest.main(): a test indented under it is a function of the guard,
    never run, and a class after it is missed when the file is run itself."""
    tree = ast.parse(text)
    for i, node in enumerate(tree.body):
        if isinstance(node, ast.If) and ast.unparse(node.test) in ('__name__ == "__main__"', "__name__ == '__main__'"):
            return [ast.unparse(n).splitlines()[0] for n in node.body[1:] + tree.body[i + 1:]]
    return []


class TestLayoutTests(unittest.TestCase):
    def test_nothing_comes_after_a_test_modules_main_guard(self):
        """Found 2026-11-09: seven tests under test_github_settings.py's guard had
        never run, since the 2026-10 reorganisation, and one of them was wrong."""
        wrong = {str(test.relative_to(ROOT)): after_main_guard(test.read_text(encoding="utf-8"))
                 for test in repository_files("tests/**/test_*.py")}
        self.assertEqual({name: found for name, found in wrong.items() if found}, {},
                         'the guard goes last, holding unittest.main() alone')

    def test_a_test_under_the_guard_or_a_class_after_it_is_found(self):
        guard = 'if __name__ == "__main__":\n    unittest.main()\n'
        self.assertEqual(after_main_guard("class T:\n    pass\n\n\n" + guard), [])
        self.assertEqual(after_main_guard(guard + "\n    def test_hidden(self):\n        pass\n"),
                         ["def test_hidden(self):"])
        self.assertEqual(after_main_guard(guard + "\n\nclass Later:\n    pass\n"), ["class Later:"])

    def test_each_test_module_mirrors_the_module_it_tests(self):
        """tests/ mirrors tools/: the tests of tools/a/b.py are tests/a/test_b.py,
        of a script tools/a/c-d.sh tests/a/test_c_d_sh.py (AGENTS.md)."""
        tests = repository_files("tests/**/test_*.py")
        self.assertEqual(sorted(set(unmirrored(ROOT, tests)) - set(NOT_MIRRORED)), [],
                         "name it after the module it tests, beside that module's path; "
                         "a test across modules goes in NOT_MIRRORED, with why")

    def test_each_test_left_unmirrored_is_there_and_still_unmirrored(self):
        tests = [ROOT / name for name in NOT_MIRRORED]
        self.assertEqual(sorted(unmirrored(ROOT, tests)), sorted(NOT_MIRRORED), "named for its module now: "
                         "take it out of NOT_MIRRORED")

    def test_a_test_named_for_no_module_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("tools/autoflash/runner.py", "tools/autoflash/routes/__init__.py",
                         "tools/espressif/emulator/emu.sh"):
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text("", encoding="utf-8")
            tests = [root / "tests" / name for name in (
                "autoflash/test_runner.py", "autoflash/routes/test_routes.py", "espressif/emulator/test_emu_sh.py",
                "autoflash/test_runner_retries.py", "autoflash/routes/test_runner.py", "repo/test_anything.py")]
            self.assertEqual(unmirrored(root, tests), ["tests/autoflash/test_runner_retries.py",
                                                       "tests/autoflash/routes/test_runner.py"])


def heading_anchors(text: str) -> set[str]:
    """The anchors GitHub gives a Markdown page's headings, a repeated one
    numbered, and any it names itself (<a id=...>)."""
    found, seen, fence = set(), {}, False
    for line in text.splitlines():
        if line.startswith("```"):
            fence = not fence
            continue
        heading = None if fence else re.match(r"#{1,6} (.*)", line)
        if heading:
            anchor = re.sub(r"[^\w\- ]", "", heading.group(1).strip().lower()).replace(" ", "-")
            found.add(anchor if anchor not in seen else f"{anchor}-{seen[anchor]}")
            seen[anchor] = seen.get(anchor, 0) + 1
    found |= set(re.findall(r'<a (?:id|name)="([^"]+)"', text))
    return found


class AnchorTests(unittest.TestCase):
    def test_every_link_to_a_heading_names_one(self):
        """A link's #anchor, in this page or another, is a heading there: a moved
        or renamed section takes its links with it."""
        link = re.compile(r"\]\(([^)\s]*)#([^)\s]+)\)")
        wrong = []
        for page in repository_files("*.md"):
            text = page.read_text(encoding="utf-8")
            text = re.sub(r"`[^`\n]*`", "", re.sub(r"```.*?```", "", text, flags=re.DOTALL))
            for match in link.finditer(text):
                href, anchor = match.groups()
                if re.match(r"[a-z][a-z0-9+.-]*:", href, re.IGNORECASE):
                    continue
                target = (page.parent / href).resolve() if href else page
                if target.suffix == ".md" and target.is_file() \
                        and anchor not in heading_anchors(target.read_text(encoding="utf-8")):
                    wrong.append(f"{page.relative_to(ROOT)}: {href}#{anchor}")
        self.assertEqual(wrong, [])

    def test_anchors_are_given_as_github_gives_them(self):
        text = "# Title\n## The fork (`../tools/beken/emulator/`)\n## A, b\n## A, b\n```\n## code\n```\n<a id=\"kept\"></a>"
        self.assertEqual(heading_anchors(text), {"title", "the-fork-toolsbekenemulator", "a-b", "a-b-1", "kept"})


# A page a file names in its text, a comment say, rather than in a Markdown link.
NAMED_PAGE = re.compile(r"(?<![\w./-])((?:\.\./)*(?:docs|devices|tools|archive|website|artifacts)/[\w./-]*?\.md)\b")
# A published image's source that names a page since moved: its hash is pinned
# with the image (artifacts/firmware.json), so it cannot change alone. Each is
# let be only while that image is the one built from it; the build that replaces
# the image puts the name right, and takes it out of here.
UNTIL_REBUILT: dict[str, tuple[str, tuple[str, ...], str]] = {}


def missing_pages(file: Path, text: str) -> list[str]:
    """The pages `text` names, from the repository's root or from the file's
    own folder, that are not there."""
    return [name for name in NAMED_PAGE.findall(text)
            if not (ROOT / name).exists() and not (file.parent / name).exists()]


def image_pin(keys) -> dict:
    entry = json.loads((ROOT / "artifacts" / "firmware.json").read_text(encoding="utf-8"))
    for key in keys:
        entry = entry[key]
    return entry


class NamedPageTests(unittest.TestCase):
    def test_every_page_a_file_names_is_there(self):
        """Code, configs and docs name pages in prose and comments too: a moved
        page is followed there. Tests are left out: their pages are made up."""
        wrong = []
        for file in repository_files():
            name = str(file.relative_to(ROOT))
            if {"tests", "test"} & set(file.relative_to(ROOT).parts):
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):  # a firmware image
                continue
            for page in missing_pages(file, text):
                kept = UNTIL_REBUILT.get(name)
                if kept and kept[0] == page and image_pin(kept[1])["file"] == kept[2]:
                    continue
                wrong.append(f"{name}: {page}")
        self.assertEqual(wrong, [], "a page named here has moved; a published image's source "
                                    "is put right in the build that publishes the image again")

    def test_each_source_let_be_until_its_image_is_built_again_still_needs_it(self):
        for name, (page, keys, image) in UNTIL_REBUILT.items():
            with self.subTest(name=name):
                self.assertIn(page, missing_pages(ROOT / name, (ROOT / name).read_text(encoding="utf-8")),
                              "put right: take it out of UNTIL_REBUILT")
                self.assertIn(name, image_pin(keys)["sources"], "not a source of that image")
                self.assertTrue((ROOT / image).is_file(), "the image it waits on is not published")

    def test_a_moved_page_is_found_wherever_it_is_named(self):
        self.assertEqual(missing_pages(ROOT / "tools/x.py", "# see docs/emulator.md, docs/gone.md and ../docs/x/y.md"),
                         ["docs/gone.md", "../docs/x/y.md"])


if __name__ == "__main__":
    unittest.main()
