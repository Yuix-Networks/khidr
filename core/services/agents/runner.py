"""
Run one harness on one task: workspace → run token → sandbox → events.

    AgentSession ──▶ HarnessRunner.run()
                       ├─ prepare the task workspace (git, on the host)
                       ├─ Run row + token for the MCP endpoint
                       ├─ harness.build(): argv / env / prompt / config files
                       ├─ sandbox.popen(): docker run --rm … | local process
                       └─ stdout JSON lines ─▶ harness.parse() ─▶ Events

When the agent calls ask_user, the run is stopped as soon as the tool result
comes back, whatever the harness: the task waits for the user's reply.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path
from typing import Iterator

from django.conf import settings

from core.models import Run
from core.services import workspace as ws

from .base import AgentRunner
from .events import AgentStatus, Done, Event, TextChunk, ToolInvocation
from .harnesses import RunContext, fold_messages, harness_for
from .sandbox import Launch, sandbox_for

log = logging.getLogger(__name__)


def run_dir_for(run: Run) -> Path:
    path = Path(settings.KHIDR_DATA_DIR) / "runs" / str(run.pk)
    path.mkdir(parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


class HarnessRunner(AgentRunner):
    # A harness that says nothing for this long is considered hung.
    IDLE_TIMEOUT_SECONDS = 1800

    def run(self) -> Iterator[Event]:
        agent, task = self.agent, self.task
        sandbox = sandbox_for(agent)
        ok, why = sandbox.available()
        if not ok:
            yield Done(reason="error", message=f"sandbox unavailable: {why}")
            return

        try:
            harness = harness_for(agent)
        except ValueError as exc:
            yield Done(reason="error", message=str(exc))
            return

        api_key = None
        if agent.api_key_env:
            api_key = os.environ.get(agent.api_key_env)
            if not api_key:
                yield Done(reason="error", message=(
                    f"agent {agent.slug}: env var {agent.api_key_env!r} is not set on the "
                    "orchestrator host"))
                return

        yield AgentStatus(status="thinking", detail="preparing workspace")
        try:
            workspace = ws.prepare_workspace(task, agent.slug)
        except ws.GitError as exc:
            yield Done(reason="error", message=f"workspace setup failed: {exc}")
            return

        run, token = Run.start(task, agent)
        run_dir = run_dir_for(run)
        home = sandbox.agent_home(agent)
        paths = sandbox.paths(workspace, run_dir, home)
        mcp_url = sandbox.reachable_url(f"{settings.KHIDR_INTERNAL_URL}/mcp/")

        ctx = RunContext(
            agent=agent,
            system_prompt=self.system_prompt,
            prompt=fold_messages(self.messages),
            mcp_url=mcp_url,
            mcp_token=token,
            workspace=paths.workspace,
            run_dir=paths.run_dir,
            run_dir_host=run_dir,
            home=paths.home,
            max_turns=self.max_iterations,
            api_key=api_key,
            base_url=sandbox.reachable_url(agent.base_url or ""),
        )
        invocation = harness.build(ctx)
        readonly = []
        if (task.project.repo_url or "").strip():
            readonly.append(ws.cache_path(task.project))
        launch = Launch(
            argv=invocation.argv,
            env=invocation.env,
            workspace=workspace,
            run_dir=run_dir,
            home=home,
            readonly_mounts=readonly,
            name=f"khidr-run-{run.pk}",
        )
        log.info("run %s: task=%s agent=%s sandbox=%s argv=%s",
                 run.pk, task.pk, agent.slug, sandbox.kind, invocation.argv[:3])

        try:
            proc = sandbox.popen(launch)
        except OSError as exc:
            run.end("spawn_failed")
            shutil.rmtree(run_dir, ignore_errors=True)
            yield Done(reason="error", message=f"could not start {harness.binary}: {exc}")
            return

        stderr_tail: list[str] = []
        lock = threading.Lock()

        def drain_stderr() -> None:
            for line in proc.stderr:
                with lock:
                    stderr_tail.append(line)
                    del stderr_tail[:-200]

        threading.Thread(target=drain_stderr, daemon=True, name=f"run-{run.pk}-stderr").start()

        def feed_stdin() -> None:
            try:
                if invocation.stdin:
                    proc.stdin.write(invocation.stdin)
                proc.stdin.close()
            except OSError as exc:
                log.warning("run %s: stdin write failed: %s", run.pk, exc)

        threading.Thread(target=feed_stdin, daemon=True, name=f"run-{run.pk}-stdin").start()

        # Watchdog: kill a harness that goes silent for too long.
        last_output = [time.monotonic()]
        stop_watchdog = threading.Event()

        def watchdog() -> None:
            while not stop_watchdog.wait(30):
                if time.monotonic() - last_output[0] > self.IDLE_TIMEOUT_SECONDS:
                    log.warning("run %s: no output for %ss, stopping", run.pk, self.IDLE_TIMEOUT_SECONDS)
                    self._stop(proc, sandbox, launch)
                    return

        threading.Thread(target=watchdog, daemon=True, name=f"run-{run.pk}-watchdog").start()

        asked_user = False
        status = None
        final: Done | None = None
        try:
            for raw in proc.stdout:
                last_output[0] = time.monotonic()
                line = raw.strip()
                if not line.startswith("{"):
                    if line:
                        log.debug("run %s non-json: %s", run.pk, line[:300])
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                for event in harness.parse(payload):
                    if isinstance(event, ToolInvocation) and status != ("tool_call", event.name):
                        status = ("tool_call", event.name)
                        yield AgentStatus(status="tool_call", detail=event.name[:128])
                    elif isinstance(event, TextChunk) and status != ("writing", ""):
                        status = ("writing", "")
                        yield AgentStatus(status="writing", detail="")
                    yield event
                    if isinstance(event, ToolInvocation) and event.name == "ask_user" \
                            and not event.detail.get("is_error"):
                        asked_user = True
                if asked_user:
                    # The question is out; the answer comes in a later run.
                    break
        finally:
            stop_watchdog.set()
            self._stop(proc, sandbox, launch)
            with lock:
                tail = "".join(stderr_tail).strip()
            if tail:
                log.info("run %s stderr (tail):\n%s", run.pk, tail[-3000:])

            run.refresh_from_db(fields=["asked_user"])
            asked_user = asked_user or run.asked_user
            if asked_user:
                final = Done(reason="waiting_for_user", message="ask_user")
            else:
                final = harness.done(proc.returncode, tail)
            run.end(final.reason)
            shutil.rmtree(run_dir, ignore_errors=True)

        yield AgentStatus(status="done", detail=final.reason)
        yield final

    @staticmethod
    def _stop(proc: subprocess.Popen, sandbox, launch: Launch) -> None:
        if proc.poll() is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        sandbox.stop(launch)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            proc.wait(timeout=5)
