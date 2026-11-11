"""
Main orchestrator daemon.

Per tick:
1. Recover: if a Task is in_progress but no thread is alive, reset it
2. Heartbeat: stale in_progress tasks get reset
3. Dispatch: for each active project, pick one Todo card and launch a session

Runs forever; supervise with supervisor/systemd. Tolerates transient errors
per tick (logs + continues).
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.db import close_old_connections, connections
from django.utils import timezone

from core.models import Project, Task
from core.services.dispatcher import dispatch_for_project, revert_task_to_todo
from core.services.session import AgentSession

log = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "Run the orchestrator loop: sync, dispatch, heartbeat."

    def add_arguments(self, parser):
        parser.add_argument("--tick", type=int, default=5, help="Seconds between ticks.")
        parser.add_argument(
            "--once",
            action="store_true",
            help="Run a single tick then exit (debug).",
        )

    def handle(self, *args, tick, once, **options):
        self._running: dict[int, threading.Thread] = {}
        self._stop = False

        def stop(signum, frame):
            self.stdout.write(self.style.WARNING(f"\ngot signal {signum}, stopping..."))
            self._stop = True

        signal.signal(signal.SIGINT, stop)
        signal.signal(signal.SIGTERM, stop)

        self._recover_orphans()

        while not self._stop:
            try:
                self._reap_threads()
                self._check_heartbeats()
                self._dispatch_all()
            except Exception:  # noqa: BLE001
                log.exception("orchestrator tick failed")
            finally:
                close_old_connections()

            if once:
                break

            # Responsive stop: sleep in small chunks so SIGTERM is seen quickly.
            slept = 0.0
            while not self._stop and slept < tick:
                time.sleep(min(0.5, tick - slept))
                slept += 0.5

        self._drain()
        self.stdout.write(self.style.SUCCESS("orchestrator stopped"))

    # ------------------------------------------------------------ lifecycle

    def _recover_orphans(self) -> None:
        """Any in-progress task from a previous process is stale."""
        orphans = list(
            Task.objects.filter(status=Task.STATUS_IN_PROGRESS).select_related("project")
        )
        for task in orphans:
            revert_task_to_todo(task, reason="daemon_startup")
        if orphans:
            self.stdout.write(f"recovered {len(orphans)} orphan task(s)")

    def _reap_threads(self) -> None:
        dead = [tid for tid, th in self._running.items() if not th.is_alive()]
        for tid in dead:
            self._running.pop(tid, None)

    def _check_heartbeats(self) -> None:
        cutoff = timezone.now() - timedelta(seconds=settings.HEARTBEAT_TIMEOUT_SECONDS)
        stale = Task.objects.filter(
            status=Task.STATUS_IN_PROGRESS,
            current_agent__isnull=False,
            waiting_for_user=False,
            last_heartbeat__lt=cutoff,
        )
        for task in stale:
            log.warning(
                "task %s heartbeat stale (>%ss); resetting",
                task.pk, settings.HEARTBEAT_TIMEOUT_SECONDS,
            )
            revert_task_to_todo(task, reason="heartbeat_timeout")
            # If a thread is still attached, let it die on its own; reset frees
            # the agent for a fresh dispatch next tick.
            self._running.pop(task.pk, None)

    def _dispatch_all(self) -> None:
        # Fresh query each tick — project rows and their related agents
        # (via project.agents.filter(...) inside the dispatcher) are re-read
        # from the DB, so admin edits take effect without a daemon restart.
        for project in Project.objects.filter(is_active=True):
            task = dispatch_for_project(project)
            if task is not None and task.pk not in self._running:
                self._launch(task.pk)

    def _launch(self, task_id: int) -> None:
        def target():
            try:
                AgentSession(task_id).run()
            except Exception:  # noqa: BLE001
                log.exception("unhandled error in session for task %s", task_id)
            finally:
                # This thread dies right after; close_old_connections() only
                # closes connections older than CONN_MAX_AGE (60s), so a
                # session that finished quickly would leak its Postgres
                # backend until GC happened to collect it. Force-close
                # unconditionally instead.
                connections.close_all()

        th = threading.Thread(target=target, name=f"session-{task_id}", daemon=True)
        th.start()
        self._running[task_id] = th
        log.info("launched session thread for task %s", task_id)

    def _drain(self) -> None:
        if not self._running:
            return
        self.stdout.write(f"waiting for {len(self._running)} session(s) to finish...")
        deadline = time.monotonic() + 10
        for th in list(self._running.values()):
            remaining = deadline - time.monotonic()
            if remaining > 0:
                th.join(timeout=remaining)
