"""
The push gate: a Python file that does not compile must never reach main.

Commit 7d41606 shipped a `with` block whose body was not indented. The
only thing that noticed was the import — `manage.py test` died with

    IndentationError: expected an indented block after 'with' statement
    on line 84 (tests_alerts.py, line 85)

— and the broken file was pushed to GitHub and to the host repo anyway,
because nothing checked that the tree compiles before it was pushed
(GlitchTip issue #39524). `push` now refuses to push a tree whose
tracked Python files do not compile.
"""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECK_SCRIPT = REPO_ROOT / "ops" / "check_syntax.py"

BROKEN_SOURCE = '''def f():
    with mock.patch("x") as live_call:
    r = 1
'''

GOOD_SOURCE = "x = 1\n"


def _load_check_syntax():
    """Import ops/check_syntax.py — ops/ is a tooling directory, not a package."""
    spec = importlib.util.spec_from_file_location("check_syntax", CHECK_SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FindSyntaxErrorsTests(unittest.TestCase):
    def test_a_file_that_does_not_compile_is_reported(self):
        module = _load_check_syntax()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "broken.py"
            path.write_text(BROKEN_SOURCE)
            problems = module.find_syntax_errors([path])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("broken.py", problems[0])

    def test_a_good_file_is_not_reported(self):
        module = _load_check_syntax()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "good.py"
            path.write_text(GOOD_SOURCE)
            self.assertEqual(module.find_syntax_errors([path]), [])

    def test_a_deleted_file_is_not_a_syntax_error(self):
        module = _load_check_syntax()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gone.py"
            self.assertEqual(module.find_syntax_errors([path]), [])

    def test_every_tracked_file_of_this_repository_compiles(self):
        # The same check CI runs.
        module = _load_check_syntax()
        self.assertEqual(
            module.find_syntax_errors(module.tracked_python_files()), []
        )
