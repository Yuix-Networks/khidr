"""
`manage.py test` must not touch the live PostgreSQL server.

The host's PostgreSQL cannot host Django's test database: the `khidr`
role has no CREATEDB privilege and pg_hba.conf only allows the `khidr`
database from the container network, so `manage.py test` inside an agent
container died with

    OperationalError: connection failed: connection to server at
    "10.0.0.5", port 5432 failed: FATAL:  no pg_hba.conf entry for
    host "172.18.0.2", user "khidr", database "test_khidr", SSL encryption

(GlitchTip #39521) — which broke the "run the tests" step for every
agent. `manage.py` therefore defaults the test commands to
`khidr.test_settings`, which swaps in an in-memory SQLite database.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from khidr.manage_helpers import (
    DEFAULT_SETTINGS_MODULE,
    TEST_SETTINGS_MODULE,
    settings_module_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGE_PY = REPO_ROOT / "manage.py"

# A probe test that asserts the database engine it was run with. It lives
# outside the repo (written to a temp dir) so the subprocess below runs
# exactly one test and nothing else.
PROBE_MODULE = """
from django.conf import settings
from django.test import SimpleTestCase


class ProbeTests(SimpleTestCase):
    def test_engine_is_sqlite(self):
        engine = settings.DATABASES["default"]["ENGINE"]
        self.assertTrue(engine.endswith("sqlite3"), engine)
"""


class SettingsModuleForTests(SimpleTestCase):
    def test_test_command_uses_the_test_settings(self):
        self.assertEqual(
            settings_module_for(["manage.py", "test"]), TEST_SETTINGS_MODULE
        )

    def test_test_command_with_labels_uses_the_test_settings(self):
        self.assertEqual(
            settings_module_for(["manage.py", "test", "core.tests_alerts", "-v", "2"]),
            TEST_SETTINGS_MODULE,
        )

    def test_testserver_command_uses_the_test_settings(self):
        self.assertEqual(
            settings_module_for(["manage.py", "testserver"]), TEST_SETTINGS_MODULE
        )

    def test_other_commands_use_the_production_settings(self):
        for argv in (
            ["manage.py"],
            ["manage.py", "migrate"],
            ["manage.py", "collectstatic"],
            ["manage.py", "runserver"],
            ["manage.py", "shell"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(settings_module_for(argv), DEFAULT_SETTINGS_MODULE)


class ManagePyRunsTestsOnSqliteTests(SimpleTestCase):
    """End-to-end: `manage.py test` really runs against SQLite."""

    def _run(self, label: str, extra_path: str = "") -> subprocess.CompletedProcess:
        env = dict(os.environ)
        # An explicit settings module would defeat the point of the test.
        env.pop("DJANGO_SETTINGS_MODULE", None)
        env["PYTHONPATH"] = os.pathsep.join(
            [p for p in (str(REPO_ROOT), extra_path, env.get("PYTHONPATH", "")) if p]
        )
        return subprocess.run(
            [sys.executable, str(MANAGE_PY), "test", label, "-v", "1"],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )

    def test_manage_py_test_uses_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "probe_settings_tests.py").write_text(PROBE_MODULE)
            proc = self._run("probe_settings_tests", extra_path=tmp)

        # Django's runner writes "Found N test(s)" to stdout, unittest's
        # result block to stderr.
        output = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, output)
        self.assertIn("OK", output)
        # The probe asserts the engine; a PostgreSQL run would have failed
        # the probe (or died creating the test database).
        self.assertNotIn("test_khidr", output)
