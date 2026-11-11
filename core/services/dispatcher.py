"""
Task → Agent assignment.

Policy:
- Concurrency is **per agent, across all projects**: an agent works on at
  most `max_concurrency` tasks at once (anywhere). When a Task pauses
  (ask_user) or gets stuck (heartbeat timeout, requeue-cap, error-cap),
  the agent's `current_agent` slot clears and they're free to pick up the
  next Todo card — possibly in a different project.
- No per-project lock: if two different agents are in the same project and
  both are idle, both can work concurrently (the orchestrator uses one
  branch — main — so agents just need to not step on each other's files).
- If a Task has `assigned_agent`, only that agent may take it (talent must
  still be >= complexity). Otherwise the lowest-priority idle worker agent
  in the project whose talent >= task.complexity takes it.
- User replies (send_message) never preempt — if the agent the paused task
  is assigned to is busy on another card, the reply queues until that
  agent frees up.

UI is the source of truth — no external kanban.
"""

from __future__ import annotations

import logging

from django.db import transaction
from django.db.models import Count, F, Q
from django.utils import timezone

from core.models import Agent, ChatMessage, Project, Task

log = logging.getLogger(__name__)


def idle_worker_agents(project: Project, min_talent: int = 1):
    """Worker agents in this project with a free slot and talent >= min_talent.

    "Free" is measured globally: in-progress tasks in any project count
    against the agent's `max_concurrency`. Only status=in_progress counts, so
    a stale current_agent on a Done/Review/Todo task can't lock the agent out.
    Agents resting after a rate/usage limit (`cooldown_until`) are skipped.
    """
    now = timezone.now()
    return (
        project.agents.filter(is_active=True)
        .filter(role__in=[Agent.ROLE_WORKER, Agent.ROLE_BOTH])
        .filter(talent__gte=min_talent)
        .filter(Q(cooldown_until__isnull=True) | Q(cooldown_until__lte=now))
        .annotate(active=Count(
            "current_tasks", filter=Q(current_tasks__status=Task.STATUS_IN_PROGRESS)))
        .filter(active__lt=F("max_concurrency"))
        .order_by("priority", "slug")
    )


def _agent_is_busy(agent: Agent) -> bool:
    """True if the agent has no free slot (or is cooling down)."""
    if agent.cooldown_until and agent.cooldown_until > timezone.now():
        return True
    active = Task.objects.filter(current_agent=agent, status=Task.STATUS_IN_PROGRESS).count()
    return active >= max(1, agent.max_concurrency)


def _agent_sandbox_ready(agent: Agent) -> bool:
    """True if the agent's sandbox can start runs (docker up + image built)."""
    from core.services.agents.sandbox import sandbox_for

    try:
        ok, why = sandbox_for(agent).available()
    except ValueError as exc:
        ok, why = False, str(exc)
    if not ok:
        log.info("agent %s: sandbox not ready: %s", agent.slug, why)
    return ok


def _pick_idle_worker(project: Project, min_talent: int) -> Agent | None:
    """First agent from idle_worker_agents() whose sandbox is ready."""
    for a in idle_worker_agents(project, min_talent=min_talent):
        if _agent_sandbox_ready(a):
            return a
    return None


def dispatch_for_project(project: Project) -> Task | None:
    """
    Try to assign one Todo task in `project` to an idle agent.

    Returns the assigned Task, or None if nothing was dispatched.
    """
    now = timezone.now()
    candidates = (
        Task.objects.filter(
            project=project,
            status=Task.STATUS_TODO,
            current_agent__isnull=True,
            # Tasks flagged waiting_for_user (ask_user, error cap, manual
            # parking) stay out of the pool until the user acts — send a
            # chat message, approve, or edit the task. send_message clears
            # the flag.
            waiting_for_user=False,
        )
        # Honor the post-requeue cooldown — tasks with a future
        # next_dispatch_after sit this tick out.
        .filter(Q(next_dispatch_after__isnull=True) | Q(next_dispatch_after__lte=now))
        .order_by("position", "created_at")
        .select_related("assigned_agent")
    )

    for task in candidates:
        if task.assigned_agent:
            if _agent_is_busy(task.assigned_agent):
                continue
            if task.assigned_agent.talent < task.complexity:
                log.warning(
                    "task %s explicitly assigned to %s but talent %d < complexity %d; skipping",
                    task.pk, task.assigned_agent.slug,
                    task.assigned_agent.talent, task.complexity,
                )
                continue
            if not _agent_sandbox_ready(task.assigned_agent):
                # Pinned to this agent but its sandbox can't start runs —
                # wait instead of handing it to another agent.
                continue
            agent = task.assigned_agent
        else:
            agent = _pick_idle_worker(project, min_talent=task.complexity)
            if agent is None:
                continue

        with transaction.atomic():
            fresh = Task.objects.select_for_update().get(pk=task.pk)
            if fresh.status != Task.STATUS_TODO or fresh.current_agent_id is not None:
                continue
            fresh.current_agent = agent
            fresh.status = Task.STATUS_IN_PROGRESS
            fresh.branch_name = ""  # no branches — agents work on main
            fresh.last_heartbeat = timezone.now()
            fresh.next_dispatch_after = None  # clear cooldown once we actually pick up
            fresh.save(
                update_fields=[
                    "current_agent", "status", "branch_name",
                    "last_heartbeat", "next_dispatch_after", "updated_at",
                ]
            )
            task = fresh

        # Inline system note so the pickup is visible in chat.
        ChatMessage.objects.create(
            task=task,
            role=ChatMessage.ROLE_SYSTEM,
            agent=agent,
            content=f"[dispatcher] picked up by **{agent.slug}**",
        )
        log.info(
            "dispatched project=%s task=%s -> agent=%s",
            project.name, task.pk, agent.slug,
        )
        return task

    return None


def revert_task_to_todo(task: Task, reason: str = "") -> None:
    """Release a Task back to Todo: clear current_agent, reset heartbeat, log."""
    Task.objects.filter(pk=task.pk).update(
        current_agent=None,
        status=Task.STATUS_TODO,
        last_heartbeat=None,
    )
    ChatMessage.objects.create(
        task=task,
        role=ChatMessage.ROLE_SYSTEM,
        content=f"[orchestrator] task-{task.pk} reverted to Todo ({reason or 'no reason'})",
    )
    log.info("task %s reverted to Todo (%s)", task.pk, reason)
