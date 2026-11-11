"""
AgentSession — drive one Task with its assigned Agent, persisting the full
trace to ChatMessage and updating lifecycle fields.

Designed to run in its own thread from the orchestrator daemon. Each
session opens and closes its own DB connection at the end.
"""

from __future__ import annotations

import json
import logging
from datetime import timedelta

from django.conf import settings
from django.db import close_old_connections
from django.utils import timezone

from core.models import ChatMessage, Task
from core.services.agents.events import AgentStatus, ContextUpdate, Done, TextChunk, ToolInvocation
from core.services.agents.registry import build_runner
from core.services.context import build_messages, build_system_prompt
from core.services.dispatcher import revert_task_to_todo
from core.services.telegram import notify_task
from core.services.tools import Toolbox

log = logging.getLogger(__name__)


class AgentSession:
    def __init__(self, task_id: int):
        self.task_id = task_id

    def run(self) -> str:
        """Run to completion. Returns the Done.reason."""
        # Each session explicitly re-reads task + project + current_agent from
        # the DB at startup, so admin tweaks to Project/Agent fields (repo_path,
        # model, command, python_path, …) take effect on the next dispatch
        # without a daemon restart.
        try:
            task = (
                Task.objects.select_related("project", "current_agent")
                .get(pk=self.task_id)
            )
        except Task.DoesNotExist:
            log.warning("AgentSession: task %s disappeared", self.task_id)
            return "error"

        agent = task.current_agent
        if agent is None:
            log.warning("AgentSession: task %s has no current_agent", self.task_id)
            return "error"

        # Defensive refresh — if something upstream cached these objects,
        # make sure the session sees current values anyway.
        task.project.refresh_from_db()
        agent.refresh_from_db()

        self.agent = agent                  # frozen reference for _finalize
        self._agent_id = agent.pk           # for ownership checks
        self._touch_heartbeat()

        system_prompt = build_system_prompt(task)
        messages = build_messages(task)

        # Seed the first user message (if this is a fresh task).
        if not task.messages.exists():
            ChatMessage.objects.create(
                task=task,
                role=ChatMessage.ROLE_USER,
                content=messages[0]["content"],
            )

        toolbox = Toolbox(task=task)
        runner = build_runner(agent, task, system_prompt, messages, toolbox=toolbox)

        # Mark agent as thinking at start
        Task.objects.filter(pk=task.pk).update(
            agent_status="thinking",
            agent_status_detail="",
        )

        log.info(
            "session start: task=%s agent=%s branch=%s",
            task.pk, agent.slug, task.branch_name,
        )

        text_buf: list[str] = []

        def flush_text():
            nonlocal text_buf
            if text_buf:
                ChatMessage.objects.create(
                    task=task,
                    role=ChatMessage.ROLE_AGENT,
                    agent=agent,
                    content="".join(text_buf),
                )
                text_buf = []

        reason = "error"
        try:
            for event in runner.run():
                if not self._still_own_task():
                    # Orchestrator reset us mid-stream (heartbeat timeout,
                    # manual intervention, etc.). Drop events and exit quietly.
                    log.warning(
                        "session for task %s lost ownership mid-stream; aborting",
                        self.task_id,
                    )
                    flush_text()
                    ChatMessage.objects.create(
                        task=task,
                        role=ChatMessage.ROLE_SYSTEM,
                        agent=agent,
                        content="[orchestrator] Session lost ownership of this task "
                                "(likely a heartbeat timeout or manual reset). Aborting.",
                    )
                    reason = "abandoned"
                    break

                self._touch_heartbeat()

                if isinstance(event, TextChunk):
                    text_buf.append(event.content)

                elif isinstance(event, ToolInvocation):
                    flush_text()
                    ChatMessage.objects.create(
                        task=task,
                        role=ChatMessage.ROLE_SYSTEM,
                        agent=agent,
                        content=self._format_tool(event),
                    )

                elif isinstance(event, Done):
                    flush_text()
                    reason = event.reason
                    self._finalize(task, event)
                    break

                elif isinstance(event, ContextUpdate):
                    Task.objects.filter(pk=task.pk).update(
                        context_used=event.context_used,
                        context_limit=event.context_limit,
                        tokens_per_second=event.tokens_per_second,
                    )

                elif isinstance(event, AgentStatus):
                    Task.objects.filter(pk=task.pk).update(
                        agent_status=event.status,
                        agent_status_detail=event.detail,
                    )
        except Exception as exc:  # noqa: BLE001
            log.exception("session crashed for task %s", task.pk)
            flush_text()
            ChatMessage.objects.create(
                task=task,
                role=ChatMessage.ROLE_SYSTEM,
                agent=agent,
                content=f"SESSION CRASH: {type(exc).__name__}: {exc}",
            )
            revert_task_to_todo(task, reason=f"crash: {type(exc).__name__}")
            reason = "error"
        finally:
            close_old_connections()

        log.info("session end: task=%s reason=%s", task.pk, reason)
        return reason

    # ---- helpers -----------------------------------------------------------

    def _touch_heartbeat(self) -> None:
        # Only touch if we still own the task — prevents a zombie session from
        # resurrecting state after a heartbeat revert.
        Task.objects.filter(
            pk=self.task_id, current_agent_id=self._agent_id
        ).update(last_heartbeat=timezone.now())

    def _still_own_task(self) -> bool:
        return Task.objects.filter(
            pk=self.task_id,
            current_agent_id=self._agent_id,
            status=Task.STATUS_IN_PROGRESS,
        ).exists()

    # Silent re-queue cap. Agent's `done` event may fire while it is still
    # mid-work (token budget, max-turns, whatever). Rather than nagging the
    # model with "are you done?" prompts — which just pollute the chat
    # history and confuse it — we silently re-queue the task for the same
    # agent and let it continue where it left off. Only after this many
    # consecutive "finished-without-review" events do we give up and hand
    # control to the user.
    REQUEUE_CAP = 10
    REQUEUE_MARKER = "[orchestrator/requeue]"

    # Error cap: after this many consecutive "error" finalizes (since the
    # last user input) we stop re-dispatching and wait for the user to
    # intervene. Auth errors (401/403) short-circuit to this immediately
    # since no amount of retrying will fix a bad key.
    ERROR_CAP = 3
    ERROR_MARKER = "[orchestrator] session ended with error"

    _PERMANENT_ERROR_PATTERNS = (
        "401",
        "403",
        "authenticationerror",
        "user not found",
        "invalid api key",
        "unauthorized",
        "permissiondenied",
    )

    # Setup errors: the agent's own environment is broken, so another attempt
    # at the same run cannot fix it. The agent container's venv missing a
    # module its code needs (the container's code was updated but its
    # dependencies were not) is the common one — the container has to be
    # re-synced.
    _SETUP_ERROR_PATTERNS = (
        "no module named",
        "modulenotfounderror",
        "is not installed in this agent container",
    )

    @classmethod
    def _permanent_error_reason(cls, detail: str) -> str | None:
        """
        The reason to park the task for the user, or None when the error is
        transient (worth another attempt after a cooldown).
        """
        low = (detail or "").lower()
        if any(pat in low for pat in cls._PERMANENT_ERROR_PATTERNS):
            return "auth/permission error"
        if any(pat in low for pat in cls._SETUP_ERROR_PATTERNS):
            return "agent container setup error"
        return None

    def _finalize(self, task: Task, event: Done) -> None:
        if event.reason == "finished":
            # Refresh: the agent may have called set_status during this turn.
            task.refresh_from_db()
            if task.status in (Task.STATUS_REVIEW, Task.STATUS_DONE):
                Task.objects.filter(pk=task.pk).update(
                    current_agent=None, last_heartbeat=None
                )
            else:
                self._requeue_or_stop(task, event=event)

        elif event.reason == "waiting_for_user":
            # Park the task: release the project-level in-progress lock so
            # other Todo cards in the same project can dispatch while we
            # wait for the user. Keep the agent stashed as assigned_agent
            # so send_message will route back to the same agent when the
            # user replies (picked up immediately if that agent is idle,
            # queued behind whatever they're working on otherwise).
            Task.objects.filter(pk=task.pk).update(
                status=Task.STATUS_TODO,
                current_agent=None,
                assigned_agent=self.agent,
                last_heartbeat=None,
                waiting_for_user=True,
            )

        else:
            # error / max_iterations -> release and put back in Todo.
            self._handle_error_done(task, event)

    def _handle_error_done(self, task: Task, event: Done) -> None:
        """
        Session finished with reason=error. Always surface the diagnostic in
        chat. Then either:
          - for a permanent-looking error (auth, not-found, or a broken
            agent environment like a container whose venv is missing a
            module): mark waiting_for_user immediately, no retry, Telegram
            ping
          - for transient errors: revert + apply cooldown so we don't
            hammer the provider; after ERROR_CAP consecutive errors (since
            last user input), also flip waiting_for_user
        """
        detail = (event.message or "").strip()

        ChatMessage.objects.create(
            task=task,
            role=ChatMessage.ROLE_SYSTEM,
            agent=self.agent,
            content=(
                f"{self.ERROR_MARKER}: {detail[:500]}"
                if detail
                else f"{self.ERROR_MARKER}"
            ),
        )

        permanent_reason = self._permanent_error_reason(detail)
        if permanent_reason:
            # Either the credentials are wrong or the agent's environment is
            # broken (e.g. its container's venv is missing a module). Neither
            # gets better by retrying — park the task for the user.
            self._stop_for_user(
                task,
                reason=permanent_reason,
                detail=detail or event.reason,
                nudge_cooldown_s=None,
            )
            return

        # Consecutive transient errors since the last user message
        last_user_id = (
            ChatMessage.objects.filter(task=task, role=ChatMessage.ROLE_USER)
            .order_by("-id").values_list("id", flat=True).first() or 0
        )
        recent_errors = ChatMessage.objects.filter(
            task=task, role=ChatMessage.ROLE_SYSTEM,
            content__startswith=self.ERROR_MARKER,
            id__gt=last_user_id,
        ).count()

        if recent_errors >= self.ERROR_CAP:
            self._stop_for_user(
                task,
                reason=f"{recent_errors} errors in a row",
                detail=detail or event.reason,
                nudge_cooldown_s=None,
            )
            return

        # Transient: revert with a cooldown so dispatcher doesn't re-pick
        # next tick.
        cooldown = int(getattr(settings, "REQUEUE_COOLDOWN_SECONDS", 300))
        revert_task_to_todo(task, reason=event.reason)
        Task.objects.filter(pk=task.pk).update(
            next_dispatch_after=timezone.now() + timedelta(seconds=cooldown),
        )
        log.info(
            "task %s: error (%d/%d), cooldown %ss",
            task.pk, recent_errors, self.ERROR_CAP, cooldown,
        )

    def _stop_for_user(self, task: Task, *, reason: str, detail: str, nudge_cooldown_s: int | None) -> None:
        """Park the task: status=todo, no agent, waiting_for_user=True, cooldown if requested."""
        updates = {
            "status": Task.STATUS_TODO,
            "current_agent": None,
            "last_heartbeat": None,
            "waiting_for_user": True,
        }
        if nudge_cooldown_s is not None:
            updates["next_dispatch_after"] = timezone.now() + timedelta(seconds=nudge_cooldown_s)
        Task.objects.filter(pk=task.pk).update(**updates)
        ChatMessage.objects.create(
            task=task,
            role=ChatMessage.ROLE_SYSTEM,
            content=(
                f"{self.ERROR_MARKER}: stopped after {reason}. "
                "Waiting for the user — fix the agent/API key or assign "
                "another agent, then write a message in the chat."
            ),
        )
        try:
            notify_task(
                task,
                "🛑 Agent stopped",
                body=f"{reason}\n\n{detail[:400]}" if detail else reason,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram notify (stop) failed: %s", exc)
        log.warning("task %s: stopped for user (%s)", task.pk, reason)

    def _requeue_or_stop(self, task: Task, event: Done | None = None) -> None:
        """
        Session ended before the agent moved the task to Review/Done. Quietly
        put it back in the queue for the same agent so they continue. After
        REQUEUE_CAP consecutive runs (since the last user input) we stop and
        flag the task for the user.
        """
        agent = self.agent
        detail = ((event.message if event else "") or "").strip()

        # Count re-queues since the most recent user message. Any user action
        # (chat reply, request-changes, manual restart) naturally resets this
        # counter so the agent always gets fresh rope after user intervention.
        last_user_msg_id = (
            ChatMessage.objects.filter(task=task, role=ChatMessage.ROLE_USER)
            .order_by("-id").values_list("id", flat=True).first() or 0
        )
        consecutive = ChatMessage.objects.filter(
            task=task, role=ChatMessage.ROLE_SYSTEM,
            content__startswith=self.REQUEUE_MARKER,
            id__gt=last_user_msg_id,
        ).count()

        if consecutive >= self.REQUEUE_CAP:
            ChatMessage.objects.create(
                task=task,
                role=ChatMessage.ROLE_SYSTEM,
                content=(
                    f"{self.REQUEUE_MARKER} handed back to the same agent {self.REQUEUE_CAP} "
                    "times but still no Review/Done. Waiting for the user. "
                    "Steer it by writing a message in the chat "
                    "or move the card manually."
                ),
            )
            Task.objects.filter(pk=task.pk).update(
                status=Task.STATUS_TODO,
                current_agent=None,
                assigned_agent=agent,
                last_heartbeat=None,
                waiting_for_user=True,
            )
            log.warning(
                "task %s: requeue cap (%d) reached; waiting for user",
                task.pk, self.REQUEUE_CAP,
            )
            try:
                notify_task(
                    task,
                    f"🆘 Stuck after {self.REQUEUE_CAP} requeues",
                    body=f"Agent: {agent.slug if agent else '?'}. Manual intervention needed.",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram notify (requeue cap) failed: %s", exc)
            return

        cooldown = int(getattr(settings, "REQUEUE_COOLDOWN_SECONDS", 300))
        resume_at = timezone.now() + timedelta(seconds=cooldown)

        detail_suffix = f"  •  {detail[:300]}" if detail else ""
        # Minimal breadcrumb — no directive, no "are you done" prompt.
        ChatMessage.objects.create(
            task=task,
            role=ChatMessage.ROLE_SYSTEM,
            agent=agent,
            content=(
                f"{self.REQUEUE_MARKER} session ended (no tool call) — "
                f"cooldown {cooldown}s before re-dispatching to "
                f"{agent.slug if agent else '?'}{detail_suffix}"
            ),
        )
        Task.objects.filter(pk=task.pk).update(
            status=Task.STATUS_TODO,
            current_agent=None,
            assigned_agent=agent,
            last_heartbeat=None,
            next_dispatch_after=resume_at,
        )
        log.info(
            "task %s: silent re-queue %d/%d (agent=%s, cooldown %ss)",
            task.pk, consecutive + 1, self.REQUEUE_CAP,
            agent.slug if agent else "?", cooldown,
        )

    @staticmethod
    def _format_tool(event: ToolInvocation) -> str:
        payload = {
            "tool": event.name,
            "args": event.args,
            "detail": event.detail,
            "result": event.result_content[:4000],
        }
        return "```\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"
