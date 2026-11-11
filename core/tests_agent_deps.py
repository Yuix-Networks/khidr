"""Agent container dependency provisioning.

Regression cover for the `ModuleNotFoundError: No module named
'claude_agent_sdk'` incident: the code inside an agent container was
updated (an agent ran `git pull` in its container) before the Python
dependencies that code needs were installed there, so the worker died on
the import.

The container's code and its venv are two separate things, so the two
have to be kept in sync — see `core.services.agents.claude_runner`.
"""

import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

from django.test import SimpleTestCase

from core.management.commands import agent_worker
from core.services.agents.claude_runner import (
    _container_deps_script,
    _parse_deps_verdict,
    ensure_container_deps,
)


class DepsScriptSandboxTests(SimpleTestCase):
    """Run the probe script for real, against a fake clone + fake venv."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = os.path.join(self.tmp, "repo")
        self.venv = os.path.join(self.tmp, "venv")
        os.makedirs(self.repo)
        os.makedirs(os.path.join(self.venv, "bin"))
        self.pip_marker = os.path.join(self.tmp, "pip-ran")

    def _write_pip(self, exit_code: int = 0) -> None:
        """A stand-in for `pip` that records that it ran."""
        pip = os.path.join(self.venv, "bin", "pip")
        with open(pip, "w", encoding="utf-8") as fh:
            fh.write(
                "#!/bin/sh\n"
                f"echo ran >> {self.pip_marker}\n"
                f"exit {exit_code}\n"
            )
        os.chmod(pip, 0o755)

    def _write_requirements(self, body: str = "django\n") -> str:
        req = os.path.join(self.repo, "requirements.txt")
        with open(req, "w", encoding="utf-8") as fh:
            fh.write(body)
        with open(req, "rb") as fh:
            return hashlib.md5(fh.read()).hexdigest()

    def _write_stamp(self, cur: str) -> None:
        with open(os.path.join(self.venv, ".container-pip.md5"), "w", encoding="utf-8") as fh:
            fh.write(cur + "\n")

    def _run(self, repo: str | None = None, venv: str | None = None) -> str:
        script = _container_deps_script(
            repo or self.repo,
            venv or self.venv,
            wait_attempts=1,
            wait_seconds=0,
        )
        res = subprocess.run(
            ["bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        return _parse_deps_verdict(res.stdout)

    def test_in_sync_when_stamp_matches_requirements(self):
        self._write_pip()
        cur = self._write_requirements()
        self._write_stamp(cur)
        self.assertEqual("IN_SYNC", self._run())
        # Nothing was reinstalled.
        self.assertFalse(os.path.exists(self.pip_marker))

    def test_installs_when_stamp_is_stale(self):
        self._write_pip()
        self._write_requirements()
        self.assertEqual("INSTALLED", self._run())
        self.assertTrue(os.path.exists(self.pip_marker))
        # The stamp is refreshed, so the next run is a no-op.
        with open(os.path.join(self.venv, ".container-pip.md5"), encoding="utf-8") as fh:
            stamp = fh.read().strip()
        self.assertEqual(
            hashlib.md5(open(os.path.join(self.repo, "requirements.txt"), "rb").read()).hexdigest(),
            stamp,
        )
        self.assertEqual("IN_SYNC", self._run())

    def test_reports_install_failed_when_pip_fails(self):
        self._write_pip(exit_code=1)
        self._write_requirements()
        self.assertEqual("INSTALL_FAILED", self._run())

    def test_does_not_race_a_concurrent_install(self):
        """Someone else holds the lock — we must not start a second pip."""
        self._write_pip()
        self._write_requirements()
        os.makedirs(os.path.join(self.venv, ".container-pip.lock"))
        self.assertEqual("STALE", self._run())
        self.assertFalse(os.path.exists(self.pip_marker))

    def test_no_requirements_is_a_no_op(self):
        self._write_pip()
        self.assertEqual("NO_REQUIREMENTS", self._run())
        self.assertFalse(os.path.exists(self.pip_marker))

    def test_missing_clone_is_reported(self):
        self._write_pip()
        self.assertEqual("NO_REPO", self._run(repo=os.path.join(self.tmp, "nope")))
        self.assertFalse(os.path.exists(self.pip_marker))


class DepsVerdictTests(SimpleTestCase):
    def test_last_non_empty_line_wins(self):
        self.assertEqual("IN_SYNC", _parse_deps_verdict("noise\nIN_SYNC\n"))
        self.assertEqual(
            "INSTALLED",
            _parse_deps_verdict("Requirement already satisfied: django\nINSTALLED\n"),
        )

    def test_empty_output(self):
        self.assertEqual("", _parse_deps_verdict(""))
        self.assertEqual("", _parse_deps_verdict(None))


class EnsureContainerDepsTests(SimpleTestCase):
    def test_reports_the_probe_verdict(self):
        with mock.patch(
            "core.services.agents.claude_runner.subprocess.run"
        ) as run:
            run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="IN_SYNC\n", stderr=""
            )
            self.assertEqual("IN_SYNC", ensure_container_deps("khidr-agent-x"))

    def test_probe_failure_does_not_raise(self):
        with mock.patch(
            "core.services.agents.claude_runner.subprocess.run",
            side_effect=OSError("docker is gone"),
        ):
            self.assertEqual("ERROR", ensure_container_deps("khidr-agent-x"))


class WorkerMissingDependencyTests(SimpleTestCase):
    async def test_missing_sdk_reports_an_actionable_error(self):
        # `None` in sys.modules makes the import fail the way a container
        # with an outdated venv would.
        #
        # The worker reports it as the run's outcome instead of raising: a
        # container whose venv is behind its code is a deploy condition, not
        # a bug in our code, so it must not turn into a Sentry event (that
        # is what opened the GlitchTip card in the first place).
        emitted = []
        with mock.patch.dict(sys.modules, {"claude_agent_sdk": None}):
            with mock.patch.object(
                agent_worker, "_emit", side_effect=emitted.append
            ):
                rc = await agent_worker._run(1, "", [], 1, None)

        self.assertEqual(rc, 1)
        self.assertEqual(len(emitted), 1)
        payload = emitted[0]
        self.assertEqual(payload["event"], "done")
        self.assertEqual(payload["reason"], "error")
        message = payload["message"]
        self.assertIn("claude-agent-sdk", message)
        self.assertIn("requirements.txt", message)

    def test_session_parks_a_missing_dependency_instead_of_retrying(self):
        # The session must treat a broken agent environment as permanent:
        # retrying the same container cannot fix it.
        from core.services.session import AgentSession

        reason = AgentSession._permanent_error_reason(
            "claude-agent-sdk is not installed in this agent container's Python "
            "environment (No module named 'claude_agent_sdk'). The container's "
            "code is newer than its dependencies — reinstall "
            "requirements.txt inside the agent containers."
        )
        self.assertEqual(reason, "agent container setup error")
