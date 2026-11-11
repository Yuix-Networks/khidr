#!/usr/bin/env python3
"""Fail if any Python file this repository tracks does not compile.

Every agent container and the production daemon import this code, so a
file that does not compile is not a typo -- it is an outage. The only
thing that catches it is the import itself, and that happens at the
worst possible moment: an agent running the test suite, or the daemon
restarting after a deploy.

It reached main once already. Commit 7d41606 shipped a `with` block
whose body was not indented, and `manage.py test` died with

    IndentationError: expected an indented block after 'with' statement
    on line 84 (tests_alerts.py, line 85)

which Sentry -- the agent's test process carries the production DSN --
reported as if it were a production incident (GlitchTip issue #39524).

The check is deliberately dependency-free and side-effect free: it
compiles the source in memory, so it needs nothing but a Python
interpreter, never writes .pyc files, and never dirties the working
tree (which would trip the orchestrator's clean-tree guard).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def tracked_python_files(root: Path | None = None) -> list[Path]:
    """Every .py file git tracks in this repository, as absolute paths.

    `git ls-files` is used rather than a filesystem walk so that the
    check covers exactly what a deploy would ship -- untracked scratch
    files in an agent container are none of the host's business.
    """
    root = root or REPO_ROOT
    result = subprocess.run(
        ["git", "ls-files", "-z", "*.py"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [root / name for name in result.stdout.split("\0") if name]


def find_syntax_errors(files) -> list[str]:
    """Return one human-readable line per file that does not compile."""
    problems = []
    for path in files:
        try:
            source = path.read_bytes()
        except FileNotFoundError:
            # Tracked but deleted from the working tree: that is a
            # deletion, not a syntax error.
            continue
        try:
            compile(source, str(path), "exec")
        except SyntaxError as exc:
            problems.append(f"{path}:{exc.lineno}: {exc.msg}")
        except ValueError as exc:
            # e.g. source containing null bytes
            problems.append(f"{path}: {exc}")
    return problems


def main() -> int:
    problems = find_syntax_errors(tracked_python_files())
    if problems:
        print("Refusing to push: these files do not compile:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
