"""How the tool's fixed text sits on the screen, read from the code and
wrapped as output.py wraps it in an 80-column window.  How tall: each
section (a "-- " heading and its lines, prompts.section) and each warning
box - a stage taller than the window cannot be taken off the screen again
once it has passed (output._erase): the rows scrolled away are out of reach;
seen in the fault suite, a stage 48 rows tall left all of itself behind.
And how a sentence broken over lines by hand fits: each line wider than a
row wraps on its own, leaving a word or two on a row of their own - seen in
the emulator, "at all -" and "and on:" under a note."""

import ast
import unittest
from pathlib import Path
from unittest import mock

from tools.autoflash import output, prompts

ROOT = Path(__file__).resolve().parents[2]
COLUMNS = 80
# Half the shortest window the tool goes on in without asking for a taller
# one: the rest is for the stage lines above and the question below.
MOST_ROWS = output.MIN_ROWS // 2
# A value only known at run time - a name, an address, an error - as long
# as a typical one.
FILLED = "x" * 12


def text(node: ast.expr) -> str:
    """The longest a line can be, its run-time parts filled in."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(part.value if isinstance(part, ast.Constant) else FILLED for part in node.values)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return text(node.left) + text(node.right)
    if isinstance(node, ast.IfExp):
        return max(text(node.body), text(node.orelse), key=len)
    return FILLED


def lines(node: ast.expr, names: dict[str, list[ast.expr]]) -> list[str] | None:
    """The most lines a section's body can hold; None when it is made at run
    time (a function's argument, another function's result).  A part made at
    run time inside a list - a device for each one found, say - counts as one."""
    if isinstance(node, (ast.List, ast.Tuple)):
        found: list[str] = []
        for item in node.elts:
            if isinstance(item, ast.Starred):
                found += lines(item.value, names) or [FILLED]
            else:
                found.append(text(item))
        return found
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = lines(node.left, names), lines(node.right, names)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        return lines(node.left, names)  # [""] * bool(...): at most once
    if isinstance(node, ast.BoolOp):
        options = [lines(value, names) for value in node.values]
        return max((option for option in options if option is not None), key=len, default=None)
    if isinstance(node, ast.IfExp):
        options = [lines(node.body, names), lines(node.orelse, names)]
        return max((option for option in options if option is not None), key=len, default=None)
    if isinstance(node, ast.Name) and node.id in names:
        options = [lines(value, names) for value in names[node.id]]
        return max((option for option in options if option is not None), key=len, default=None)
    return None


def assignments(function: ast.AST) -> dict[str, list[ast.expr]]:
    """What each name in a function is set to: `body += [...]` as the sum."""
    names: dict[str, list[ast.expr]] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.setdefault(target.id, []).append(node.value)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            names[node.target.id] = [ast.BinOp(left=value, op=ast.Add(), right=node.value)
                                     for value in names.get(node.target.id, [ast.List(elts=[])])]
    return names


def sections() -> list[tuple[str, str, list[str] | None]]:
    """(where, heading, its lines) for every section and warning box in the tool."""
    found = []
    for path in sorted((ROOT / "tools" / "autoflash").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            names = assignments(function)
            for call in ast.walk(function):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr in ("section", "warning_box") and len(call.args) >= 2):
                    where = f"{path.relative_to(ROOT)}:{call.lineno}"
                    found.append((where, text(call.args[0]), lines(call.args[1], names)))
    return sorted(set((where, title, tuple(body) if body is not None else None)
                      for where, title, body in found))


def rows(title: str, body: list[str]) -> int:
    """Rows on the screen: a blank, the heading, then each line wrapped."""
    width = COLUMNS - 1 - len(output.MARGIN)
    return 2 + len(output._wrap(f"-- {title}", width)) - 1 + sum(
        len(output._wrap(prompts.BULLET + line, width)) for line in body)


# Where a line ends its sentence: the next line starts afresh.
ENDS = (".", "!", "?", ":", ")", "...")


def broken_lines() -> list[tuple[str, str, str]]:
    """(where, a line, the next) for every sentence the tool breaks over lines
    by hand: a section's or warning box's lines, and the lines told one after
    another (output.tell, say, show). Run-time parts count as empty: a line
    that does not fit even so never does."""
    found = []
    with mock.patch.dict(globals(), {"FILLED": ""}):
        for where, _, body in sections():
            body = list(body or [])
            found += [(where, prompts.BULLET + line, after) for line, after in zip(body, body[1:])]
        for path in sorted((ROOT / "tools" / "autoflash").rglob("*.py")):
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                for field in ("body", "orelse", "finalbody"):
                    statements = getattr(node, field, None)
                    if not isinstance(statements, list):
                        continue   # a lambda's body, an expression
                    run = [told(statement) for statement in statements]
                    for (line, said), (_, after) in zip(run, run[1:]):
                        if said is not None and after is not None:
                            found.append((f"{path.relative_to(ROOT)}:{line}", said, after))
    return found


def told(statement: ast.stmt) -> tuple[int, str | None]:
    """A statement's line and what it tells the screen, if it does (output.tell, say, show)."""
    call = statement.value if isinstance(statement, ast.Expr) else None
    if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
            and call.func.attr in ("tell", "say", "show") and isinstance(call.func.value, ast.Name)
            and call.func.value.id == "output" and call.args
            and isinstance(call.args[0], (ast.Constant, ast.JoinedStr))):
        return statement.lineno, text(call.args[0])
    return statement.lineno, None


class SectionHeightTests(unittest.TestCase):
    def test_every_section_fits_half_the_shortest_window(self):
        measured = 0
        for where, title, body in sections():
            if body is None:
                continue
            measured += 1
            with self.subTest(where=where, title=title):
                self.assertLessEqual(rows(title, list(body)), MOST_ROWS,
                                     f"{where}: '{title}' is {rows(title, list(body))} rows in "
                                     f"{COLUMNS} columns; at most {MOST_ROWS}")
        # The reading of the code still finds them (it would pass, finding none).
        self.assertGreater(measured, 30)

    def test_a_section_is_measured_as_it_wraps(self):
        self.assertEqual(rows("A heading", ["A line.", "x " * 60]), 2 + 1 + 2)


class BrokenLineTests(unittest.TestCase):
    def test_a_sentence_broken_by_hand_fits_its_rows(self):
        # Told as one line instead, output.py wraps it to any window.
        width = COLUMNS - 1 - len(output.MARGIN)
        found = broken_lines()
        self.assertGreater(len(found), 100)   # the reading of the code still finds them
        for where, line, after in found:
            carries_on = not line.rstrip().endswith(ENDS) or after.lstrip()[:1].islower()
            if carries_on:
                with self.subTest(where=where, line=line.strip()[:40]):
                    self.assertLessEqual(len(line), width, f"{where}: {len(line)} wide, broken by hand mid-"
                                         f"sentence; at most {width} in {COLUMNS} columns, or one line")


def working_texts() -> list[tuple[str, str]]:
    """(where, its text) for every wait shown on a row of its own (output.working)."""
    found = []
    with mock.patch.dict(globals(), {"FILLED": "xx"}):   # a count of seconds, say
        for path in sorted((ROOT / "tools" / "autoflash").rglob("*.py")):
            for call in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)
                        and call.func.attr == "working" and call.args):
                    found.append((f"{path.relative_to(ROOT)}:{call.lineno}", text(call.args[0])))
    return found


class WorkingRowTests(unittest.TestCase):
    def test_a_wait_fits_its_row_with_the_spinner(self):
        # A row redrawn in place is cut short, not wrapped (output._clip): seen,
        # the readout's "Getting it ready to send: a puzzle..." lost its end.
        width = COLUMNS - 1 - len(output.MARGIN) - len(" | ")
        found = working_texts()
        self.assertGreater(len(found), 5)
        for where, said in found:
            with self.subTest(where=where):
                self.assertLessEqual(len(said), width, f"{where}: '{said}' is {len(said)} wide; at most {width}")


if __name__ == "__main__":
    unittest.main()
