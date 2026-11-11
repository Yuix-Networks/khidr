"""
Task → Agent assignment.

Policy:
- Serialization is **per-agent, across all projects**. An agent can own at
  most one active Task at any time (anywhere). When that Task pauses
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
from django.db.models import Q
from django.utils import timezone

from core.models import Agent, ChatMessage, Project, Task

log = logging.getLogger(__name__)


def idle_worker_agents(project: Project, min_talent: int = 1):
    """Worker agents in this project that are free and have talent >= min_talent.

    "Free" is measured globally: any in-progress task (in any project) with
    `current_agent=agent` counts as busy, so agents never double-book across
    projects. We restrict to status=in_progress so a stale current_agent
    slot on a Done/Review/Todo task — left behind by a UI status change or
    a session-finalize edge case — can't permanently lock the agent out of
    dispatch. Callers are expected to additionally confirm
    `_agent_container_ready()` before actually dispatching — we don't
    include that in the queryset because it's an out-of-band docker ps
    check per agent.
    """
    busy_ids = Task.objects.filter(
        current_agent__isnull=False,
        status=Task.STATUS_IN_PROGRESS,
    ).values_list("current_agent_id", flat=True)
    return (
        project.agents.filter(is_active=True)
        .filter(role__in=[Agent.ROLE_WORKER, Agent.ROLE_BOTH])
        .filter(talent__gte=min_talent)
        .exclude(pk__in=busy_ids)
        .order_by("priority", "slug")
    )


def _agent_is_busy(agent: Agent) -> bool:
    """True if this agent holds a current_agent slot on an in-progress task."""
    return Task.objects.filter(
        current_agent=agent, status=Task.STATUS_IN_PROGRESS
    ).exists()


def _agent_container_ready(agent: Agent) -> bool:
    """True if the agent's Docker container is up. Agents run in containers
    for isolation; there is no host-side fallback, so a missing container
    means we cannot dispatch."""
    # Deferred import to avoid circular dependency at module load (docker_exec
    # pulls core.models.Agent, which is imported here).
    from core.services.agents.docker_exec import agent_container_if_running

    return agent_container_if_running(agent) is not None


def _pick_idle_worker(project: Project, min_talent: int) -> Agent | None:
    """First agent from idle_worker_agents() whose container is running."""
    for a in idle_worker_agents(project, min_talent=min_talent):
        if _agent_container_ready(a):
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
            if not _agent_container_ready(task.assigned_agent):
                # Task is explicitly pinned to this agent but the container
                # is not up — wait instead of falling back to another agent
                # (would violate the assignment) or to the host (no
                # isolation, no .pyenv venv parity).
                log.info(
                    "task %s assigned to %s but container 'khidr-agent-%s' "
                    "is not running; holding task in Todo",
                    task.pk, task.assigned_agent.slug, task.assigned_agent.slug,
                )
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
