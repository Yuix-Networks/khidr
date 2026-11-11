"""
Claude Agent SDK runner — thin docker-exec wrapper around the agent_worker
Django management command.

    [orchestrator daemon]
            │ subprocess.Popen(
            │   docker exec -i khidr-agent-<slug>
            │     /home/khidr/.pyenv/versions/khidr/bin/python
            │     /home/khidr/workspace/khidr/manage.py
            │     agent_worker --task-id N
            │ )
            ↓
    [agent container]
        manage.py agent_worker
            ├─ reads {system_prompt, messages} JSON envelope from stdin
            ├─ scrubs orchestrator secrets from os.environ
            ├─ runs a Claude Agent SDK session: built-in tools + our task
            │  tools served as the in-process `khidr` MCP server
            └─ emits JSONL events on stdout

We translate each stdout line into a TextChunk / ToolInvocation / Done.
Everything the agent executes (Bash, file tools, git) runs container-local
by construction; if the container is gone, `docker exec` fails and we
surface that instead of falling back to the host.
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
import threading
from pathlib import Path
from typing import Iterator

from .base import AgentRunner
from .docker_exec import agent_container_if_running, wrap_with_docker_exec
from .events import AgentStatus, ContextUpdate, Done, Event, TextChunk, ToolInvocation

log = logging.getLogger(__name__)


# Container-internal absolute paths. The agent image installs the orchestrator
# repo into /home/khidr/workspace/khidr (rw clone, not bind-mount,
# kept up to date by your provisioning step). The 'khidr' venv mirrors the host.
_CT_PYTHON = "/home/khidr/.pyenv/versions/khidr/bin/python"
_CT_MANAGE_PY = "/home/khidr/workspace/khidr/manage.py"

# The code the worker runs and the Python packages it imports live in two
# different places inside the container: the clone (see _CT_MANAGE_PY) and
# the venv (see _CT_PYTHON). They are provisioned separately — provisioning pulls the clone and installs requirements.txt — so they can
# drift apart: an agent that ran `git pull` inside its own container
# updated the code long before the new dependency was installed there.
# The worker then died with `ModuleNotFoundError: No module named
# 'claude_agent_sdk'`. `ensure_container_deps()` re-checks the pair before
# every run and repairs it when they disagree.
_CT_REPO_DIR = "/home/khidr/workspace/khidr"
_CT_VENV_DIR = str(Path(_CT_PYTHON).parent.parent)


def _container_deps_script(
    repo_dir: str,
    venv_dir: str,
    wait_attempts: int = 15,
    wait_seconds: int = 2,
) -> str:
    """Shell snippet (bash, run inside the container) that syncs the venv.

    Compares the clone's requirements.txt against the stamp file written
    by the last successful install and reinstalls when they differ. The
    stamp convention is the one provisioning scripts use, so both paths agree
    on when the venv is up to date.

    `wait_attempts`/`wait_seconds` bound how long we wait for somebody
    else (a concurrent dispatch, or a provisioning run) who holds the lock.

    Prints exactly one verdict on stdout:
      IN_SYNC         — the venv already matches requirements.txt
      INSTALLED       — it was stale; requirements.txt was reinstalled
      INSTALL_FAILED  — the reinstall failed (network?); deps may be missing
      NO_REQUIREMENTS — the clone has no requirements.txt (nothing to do)
      NO_REPO         — the clone isn't there (unexpected)
      STALE           — still out of sync and we could not repair it
    """
    req = f"{repo_dir}/requirements.txt"
    stamp = f"{venv_dir}/.container-pip.md5"
    lock = f"{venv_dir}/.container-pip.lock"
    pip = f"{venv_dir}/bin/pip"
    return (
        f"cd {shlex.quote(repo_dir)} 2>/dev/null || {{ echo NO_REPO; exit 0; }}\n"
        f"if [ ! -f {shlex.quote(req)} ]; then echo NO_REQUIREMENTS; exit 0; fi\n"
        f"CUR=$(md5sum {shlex.quote(req)} | awk '{{print $1}}')\n"
        # Someone else may be installing right now (a concurrent dispatch,
        # or a provisioning run): wait for them instead of racing pip.
        f"for _ in $(seq 1 {max(1, int(wait_attempts))}); do\n"
        f"  if [ -f {shlex.quote(stamp)} ] && grep -qx \"$CUR\" {shlex.quote(stamp)}; "
        "then echo IN_SYNC; exit 0; fi\n"
        f"  if mkdir {shlex.quote(lock)} 2>/dev/null; then\n"
        f"    if {shlex.quote(pip)} install -q -r {shlex.quote(req)}; then\n"
        f"      echo \"$CUR\" > {shlex.quote(stamp)} && echo INSTALLED || echo INSTALL_FAILED\n"
        "    else echo INSTALL_FAILED; fi\n"
        f"    rmdir {shlex.quote(lock)} 2>/dev/null\n"
        "    exit 0\n"
        "  fi\n"
        f"  sleep {max(0, int(wait_seconds))}\n"
        "done\n"
        "echo STALE\n"
    )


def _parse_deps_verdict(stdout: str) -> str:
    """The probe's verdict — the last non-empty line of its output."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line:
            return line.upper()
    return ""


def ensure_container_deps(container: str) -> str:
    """Make sure the container's venv matches the code it is about to run.

    Returns the probe verdict ("IN_SYNC", "INSTALLED", …). Never raises:
    a failed probe is logged and the run continues — the worker itself
    reports a missing dependency with an actionable message.
    """
    script = _container_deps_script(_CT_REPO_DIR, _CT_VENV_DIR)
    try:
        res = subprocess.run(
            ["docker", "exec", "-u", "khidr", container, "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        log.warning("agent_worker: dependency check timed out in %s", container)
        return "TIMEOUT"
    except Exception as exc:  # noqa: BLE001
        log.warning("agent_worker: dependency check failed in %s: %s", container, exc)
        return "ERROR"

    verdict = _parse_deps_verdict(res.stdout)
    if verdict in ("IN_SYNC", "INSTALLED"):
        log.info("agent_worker: container %s dependencies %s", container, verdict)
    else:
        log.warning(
            "agent_worker: container %s dependency state %r (rc=%s): %s",
            container, verdict, res.returncode, (res.stderr or "").strip()[-500:],
        )
    return verdict


class ClaudeAgentRunner(AgentRunner):
    """
    Spawn `manage.py agent_worker` inside the agent's container,
    feed it the system prompt + message history via stdin, and translate
    its JSONL stdout into our Event stream.
    """

    def run(self) -> Iterator[Event]:
        # Container readiness — belt-and-suspenders. The dispatcher already
        # filters on _agent_container_ready before assigning, but a
        # container can die between dispatch and now. No host fallback —
        # bail with an error so the orchestrator surfaces the real cause
        # instead of running the worker on the host.
        container = agent_container_if_running(self.agent)
        if container is None:
            msg = (
                f"Agent container 'khidr-agent-{self.agent.slug}' is not "
                f"running. See docs/agent-containers.md to start it."
            )
            log.error("agent_worker: %s", msg)
            yield Done(reason="error", message=msg)
            return

        # Agent's working directory inside the container — matches the host
        # layout (paths are bind-compatible). expanduser handles any '~'
        # prefix the agent row might carry.
        cwd_raw = self.task.project.repo_path or "/home/khidr/workspace/khidr"
        cwd = str(Path(cwd_raw).expanduser())

        # API key passthrough. The agent row names a host-side env var
        # (api_key_env). We resolve it on the host and forward the *value*
        # to the worker via AGENT_API_KEY — so secrets don't need to live
        # in the container's own env, and so an empty api_key_env (e.g.
        # llama.cpp) just becomes an absent var → worker uses 'no-key'.
        worker_env: dict[str, str] = {"PYTHONUNBUFFERED": "1"}
        if self.agent.api_key_env:
            key = os.environ.get(self.agent.api_key_env)
            if not key:
                yield Done(
                    reason="error",
                    message=(
                        f"Agent {self.agent.slug}: env var "
                        f"{self.agent.api_key_env!r} is not set on the orchestrator host"
                    ),
                )
                return
            worker_env["AGENT_API_KEY"] = key

        # The container's code and its Python dependencies are installed
        # separately, so they can be out of sync (new code, old deps).
        # Repair that before the worker starts, otherwise it dies on the
        # missing import.
        ensure_container_deps(container)

        inner_argv = [
            _CT_PYTHON,
            _CT_MANAGE_PY,
            "agent_worker",
            "--task-id", str(self.task.pk),
        ]
        cmd = wrap_with_docker_exec(
            inner_argv,
            container=container,
            cwd=cwd,
            env=worker_env,
            interactive=True,
        )

        log.info(
            "agent_worker: task=%s agent=%s container=%s cwd=%s",
            self.task.pk, self.agent.slug, container, cwd,
        )
        log.debug("agent_worker command: %s", shlex.join(cmd))

        envelope = json.dumps(
            {"system_prompt": self.system_prompt, "messages": self.messages},
            ensure_ascii=False,
        )

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            encoding="utf-8",
        )

        # Drain stderr in a daemon thread so the worker doesn't block on a
        # full pipe when it's chatty (the CLI's stderr is forwarded at INFO).
        # We collect the tail for diagnostics if the worker dies mid-stream.
        stderr_tail: list[str] = []
        stderr_lock = threading.Lock()

        def _drain_stderr() -> None:
            assert proc.stderr is not None
            for line in proc.stderr:
                with stderr_lock:
                    stderr_tail.append(line)
                    # Keep ~200 lines max; oldest dropped.
                    if len(stderr_tail) > 200:
                        del stderr_tail[: len(stderr_tail) - 200]

        stderr_thread = threading.Thread(
            target=_drain_stderr,
            name=f"agent-worker-stderr-{self.task.pk}",
            daemon=True,
        )
        stderr_thread.start()

        # Feed the envelope and close stdin so the worker's
        # `sys.stdin.read()` returns. Anything that fails here is fatal
        # for the run — surface as Done(error).
        try:
            assert proc.stdin is not None
            proc.stdin.write(envelope)
            proc.stdin.flush()
            proc.stdin.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("agent_worker: failed to write stdin: %s", exc)
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
            yield Done(reason="error", message=f"stdin write failed: {exc}")
            return

        saw_done = False
        current_status: str | None = None

        def _emit_status(status: str, detail: str = "") -> None:
            nonlocal current_status
            if status == current_status:
                return
            current_status = status
            yield AgentStatus(status=status, detail=detail)

        try:
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    log.debug("agent_worker non-json stdout: %s", line[:200])
                    continue

                event = _parse_worker_event(payload)
                if event is None:
                    continue

                if isinstance(event, ToolInvocation):
                    yield from _emit_status("tool_call", event.name)
                elif isinstance(event, TextChunk):
                    yield from _emit_status("writing", "")
                elif isinstance(event, Done):
                    yield from _emit_status("done", event.reason)

                yield event
                if isinstance(event, Done):
                    saw_done = True
                    return

        finally:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
            except Exception:  # noqa: BLE001
                pass
            stderr_thread.join(timeout=2)
            with stderr_lock:
                tail = "".join(stderr_tail).strip()
            if tail:
                log.info("agent_worker stderr (last %d chars):\n%s",
                         len(tail), tail[-4000:])

        if not saw_done:
            # Worker exited without an explicit Done event. Most likely a
            # crash, an OOM, or `docker exec` itself failed (container
            # gone, daemon hiccup). Synthesize an error Done so the
            # session finalizer doesn't hang.
            with stderr_lock:
                tail = "".join(stderr_tail).strip()
            yield Done(
                reason="error",
                message=(
                    f"agent_worker exited without a done event "
                    f"(rc={proc.returncode}). stderr tail: {tail[-1000:]}"
                ),
            )


def _parse_worker_event(payload: dict) -> Event | None:
    """Translate one JSONL payload from the worker into our Event types."""
    kind = payload.get("event")
    if kind == "text":
        text = payload.get("content") or ""
        if not text:
            return None
        return TextChunk(content=text)
    if kind == "tool":
        return ToolInvocation(
            name=payload.get("name") or "",
            args=payload.get("args") or {},
            result_content=payload.get("result") or "",
            detail={"is_error": bool(payload.get("is_error"))},
        )
    if kind == "context":
        return ContextUpdate(
            context_used=payload.get("context_used", 0),
            context_limit=payload.get("context_limit", 0),
            tokens_per_second=payload.get("tokens_per_second", 0.0),
        )
    if kind == "done":
        reason = payload.get("reason") or "finished"
        # Clamp to the literal set Done accepts; anything else → error.
        if reason not in ("finished", "waiting_for_user", "max_iterations", "error"):
            reason = "error"
        return Done(reason=reason, message=payload.get("message") or "")
    log.debug("agent_worker: unknown event kind %r", kind)
    return None
