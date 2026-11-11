"""HTML + JSON endpoints for the chat UI."""

from __future__ import annotations

import logging
from datetime import timedelta

from django.db.models import Count, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.models import Agent, ChatMessage, Project, Task
from core.services.context import build_messages, build_system_prompt

from .auth import SESSION_KEY, is_authenticated, login_required, verify_password

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


def login_view(request):
    if is_authenticated(request):
        return redirect(request.GET.get("next") or "/")
    error = None
    if request.method == "POST":
        pwd = request.POST.get("password", "")
        if verify_password(pwd):
            request.session[SESSION_KEY] = True
            request.session.set_expiry(30 * 24 * 3600)  # 30 days
            return redirect(request.GET.get("next") or "/")
        error = "Yanlış şifre."
    return render(request, "chat/login.html", {"error": error})


def logout_view(request):
    request.session.flush()
    return redirect("/login/")


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------


@login_required
def project_list(request):
    projects = (
        Project.objects.filter(is_active=True)
        .annotate(
            total=Count("tasks"),
            in_progress=Count("tasks", filter=Q(tasks__status=Task.STATUS_IN_PROGRESS)),
            review=Count("tasks", filter=Q(tasks__status=Task.STATUS_REVIEW)),
            todo=Count("tasks", filter=Q(tasks__status=Task.STATUS_TODO)),
            waiting=Count("tasks", filter=Q(tasks__waiting_for_user=True)),
        )
        .order_by("name")
    )
    return render(request, "chat/project_list.html", {"projects": projects})


@login_required
def project_detail(request, project_id: int):
    project = get_object_or_404(
        Project.objects.prefetch_related("agents"), pk=project_id, is_active=True
    )
    _done_cutoff = timezone.now() - timedelta(days=3)
    tasks = list(
        project.tasks.select_related("current_agent", "assigned_agent")
        .exclude(status=Task.STATUS_DONE, updated_at__lt=_done_cutoff)
        .order_by("status", "position")
    )
    cols_by_status: dict[str, list] = {s: [] for s, _ in Task.STATUS_CHOICES}
    for t in tasks:
        cols_by_status.setdefault(t.status, []).append(t)
    columns = [
        {"status": s, "label": label, "tasks": cols_by_status.get(s, [])}
        for s, label in Task.STATUS_CHOICES
    ]
    agents = list(project.agents.filter(is_active=True).order_by("priority", "slug"))
    return render(
        request,
        "chat/project_detail.html",
        {"project": project, "columns": columns, "agents": agents},
    )


@login_required
@require_POST
def create_project_view(request):
    name = (request.POST.get("name") or "").strip()
    description = (request.POST.get("description") or "").strip()
    if not name:
        return JsonResponse({"error": "name required"}, status=400)
    if Project.objects.filter(name=name).exists():
        return JsonResponse({"error": f"Project '{name}' already exists"}, status=400)
    import secrets

    api_token = secrets.token_hex(32)
    project = Project.objects.create(
        name=name, description=description, is_active=True, api_token=api_token
    )
    return JsonResponse({"ok": True, "id": project.pk, "url": f"/projects/{project.pk}/"})


@login_required
def task_detail(request, task_id: int):
    task = get_object_or_404(
        Task.objects.select_related("project", "current_agent", "assigned_agent"),
        pk=task_id,
    )
    messages = list(task.messages.select_related("agent").order_by("id"))
    project_agents = list(
        task.project.agents.filter(is_active=True).order_by("priority", "slug")
    )
    # Build the same prompts the agent sees on the next session so the user
    # can audit what the model is actually going to read.
    try:
        system_prompt = build_system_prompt(task)
    except Exception as exc:  # noqa: BLE001
        system_prompt = f"(system prompt could not be built: {exc})"
    try:
        initial_messages = build_messages(task, seed_if_empty=True)
    except Exception as exc:  # noqa: BLE001
        initial_messages = [{"role": "system", "content": f"(failed: {exc})"}]

    return render(
        request,
        "chat/task_detail.html",
        {
            "task": task,
            "messages": messages,
            "project_agents": project_agents,
            "system_prompt": system_prompt,
            "initial_messages": initial_messages,
            # Pass the raw list; the template uses {% json_script %} so that
            # content like "</script>" or "<system-reminder>" embedded in
            # message bodies gets escaped as \u003c/\u003e and can't break
            # out of the <script> element before JSON.parse runs.
            "messages_payload": [_serialize_message(m) for m in messages],
        },
    )


def _serialize_message(m: ChatMessage) -> dict:
    return {
        "id": m.id,
        "role": m.role,
        "agent": m.agent.slug if m.agent_id else None,
        "content": m.content or "",
        "created_at": m.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# POST actions
# ---------------------------------------------------------------------------


@login_required
@require_POST
def send_message(request, task_id: int):
    task = get_object_or_404(
        Task.objects.select_related("current_agent", "assigned_agent"),
        pk=task_id,
    )
    text = (request.POST.get("text") or "").strip()
    if not text:
        return JsonResponse({"error": "empty"}, status=400)

    ChatMessage.objects.create(task=task, role=ChatMessage.ROLE_USER, content=text)

    # A user message is an interrupt/signal: whatever state the task is in,
    # bring it back to Todo so the dispatcher hands it to the same agent on
    # the next tick with full chat history (including this new message).
    #
    # For an in-flight session, clearing current_agent flips _still_own_task
    # to False; the running session flushes, writes a "lost ownership" note,
    # and exits on its next event. Then the fresh dispatch picks up with the
    # user's new input in context.
    agent_to_retain = task.current_agent or task.assigned_agent
    updates: dict = {
        "status": Task.STATUS_TODO,
        "current_agent": None,
        "last_heartbeat": None,
        "waiting_for_user": False,
        # User input bypasses any post-requeue cooldown — their message is
        # the interrupt, they want immediate re-dispatch.
        "next_dispatch_after": None,
    }
    if agent_to_retain:
        updates["assigned_agent"] = agent_to_retain
    Task.objects.filter(pk=task.pk).update(**updates)
    return JsonResponse({"ok": True})


@login_required
@require_POST
def approve_task(request, task_id: int):
    task = get_object_or_404(Task.objects.select_related("project"), pk=task_id)
    Task.objects.filter(pk=task.pk).update(
        status=Task.STATUS_DONE, current_agent=None, last_heartbeat=None
    )
    ChatMessage.objects.create(
        task=task,
        role=ChatMessage.ROLE_USER,
        content="✓ Approved — task marked Done.",
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def move_to_todo(request, task_id: int):
    task = get_object_or_404(Task.objects.select_related("project"), pk=task_id)
    Task.objects.filter(pk=task.pk).update(
        status=Task.STATUS_TODO, current_agent=None, last_heartbeat=None
    )
    ChatMessage.objects.create(
        task=task,
        role=ChatMessage.ROLE_USER,
        content="→ Moved to Todo.",
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def request_changes(request, task_id: int):
    task = get_object_or_404(Task.objects.select_related("project"), pk=task_id)
    text = (request.POST.get("text") or "").strip()
    if not text:
        return JsonResponse({"error": "empty message"}, status=400)

    ChatMessage.objects.create(
        task=task, role=ChatMessage.ROLE_USER, content=f"[request changes]\n{text}"
    )
    Task.objects.filter(pk=task.pk).update(
        status=Task.STATUS_TODO,
        current_agent=None,
        last_heartbeat=None,
        waiting_for_user=False,
        next_dispatch_after=None,
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def set_task_status(request, task_id: int):
    """Manually move a task between columns. Valid for non-agent transitions."""
    task = get_object_or_404(Task, pk=task_id)
    new_status = (request.POST.get("status") or "").strip().lower()
    valid = {s for s, _ in Task.STATUS_CHOICES}
    if new_status not in valid:
        return JsonResponse({"error": f"unknown status {new_status}"}, status=400)

    updates = {"status": new_status}
    # Moving out of in_progress => release the agent.
    if task.status == Task.STATUS_IN_PROGRESS and new_status != Task.STATUS_IN_PROGRESS:
        updates["current_agent"] = None
        updates["last_heartbeat"] = None

    Task.objects.filter(pk=task.pk).update(**updates)
    ChatMessage.objects.create(
        task=task,
        role=ChatMessage.ROLE_USER,
        content=f"(moved task to **{new_status}**)",
    )
    return JsonResponse({"ok": True, "status": new_status})


@login_required
@require_POST
def edit_task(request, task_id: int):
    task = get_object_or_404(Task, pk=task_id)
    title = (request.POST.get("title") or "").strip()
    description = request.POST.get("description", "")
    complexity_raw = request.POST.get("complexity") or str(task.complexity)
    assigned_slug = (request.POST.get("assigned_agent") or "").strip()

    if not title:
        return JsonResponse({"error": "title required"}, status=400)

    try:
        complexity = max(1, min(5, int(complexity_raw)))
    except ValueError:
        complexity = task.complexity

    assigned_agent = None
    if assigned_slug:
        assigned_agent = Agent.objects.filter(slug=assigned_slug).first()

    updates: dict = {
        "title": title,
        "description": description,
        "complexity": complexity,
        "assigned_agent": assigned_agent,
    }
    # Assigning a new agent is a signal to dispatch: move to Todo so the
    # dispatcher can hand it to that agent on the next tick.
    if assigned_slug:
        updates |= {
            "status": Task.STATUS_TODO,
            "current_agent": None,
            "last_heartbeat": None,
            "waiting_for_user": False,
            "next_dispatch_after": None,
        }

    Task.objects.filter(pk=task.pk).update(**updates)
    ChatMessage.objects.create(
        task=task,
        role=ChatMessage.ROLE_USER,
        content="(task edited: title / description / complexity / assignment)",
    )
    return JsonResponse({"ok": True})


@login_required
@require_POST
def delete_task(request, task_id: int):
    task = get_object_or_404(Task, pk=task_id)
    project_id = task.project_id
    # Hard delete — cascades ChatMessage rows. If a session thread is still
    # running for this task, its ownership check + touch-heartbeat filters
    # no longer match and it exits on the next event without side effects.
    task.delete()
    return JsonResponse({"ok": True, "project_id": project_id})


@login_required
@require_POST
def close_and_create_task(request, task_id: int):
    """Mark current task done, summarize its chat, create a follow-up task."""
    import httpx
    import os

    old_task = get_object_or_404(
        Task.objects.select_related("project"),
        pk=task_id,
    )
    new_title = (request.POST.get("title") or "").strip()
    if not new_title:
        return JsonResponse({"error": "title required"}, status=400)

    messages = list(old_task.messages.order_by("created_at"))
    chat_text = "\n".join(f"[{m.role}] {m.content}" for m in messages if m.content)

    summary = None
    if chat_text:
        try:
            key = os.environ.get("MINIMAX_API_KEY") or ""
            if key:
                prompt = (
                    "Aşağıdaki sohbet geçmişinin özetini çıkar. Yapılan işleri, kararları "
                    "ve önemli detayları 2-3 cümlede özetle. Gelecekteki bir ajanın bu görev "
                    "serisini anlaması için gerekli tüm bilgiyi koru:\n\n" + chat_text[:8000]
                )
                resp = httpx.post(
                    "https://api.minimax.io/minimax-api/v1/text/chatcompletion_v2",
                    json={
                        "model": "MiniMax-M2.5",
                        "messages": [{"role": "user", "content": prompt}],
                        "max_tokens": 512,
                    },
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                    timeout=30.0,
                )
                resp.raise_for_status()
                summary = (
                    resp.json()
                    .get("choices", [{}])[0]
                    .get("message", {})
                    .get("content", "")
                    .strip()
                )
        except Exception:  # noqa: BLE001
            summary = None

    Task.objects.filter(pk=old_task.pk).update(
        status=Task.STATUS_DONE, current_agent=None, last_heartbeat=None
    )
    ChatMessage.objects.create(
        task=old_task,
        role=ChatMessage.ROLE_USER,
        content=f"✓ Kapatıldı ve özetlendi → yeni görev açıldı: {new_title}",
    )

    last = (
        Task.objects.filter(project=old_task.project, status=Task.STATUS_TODO)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    position = (last or 0) + 1024

    description = summary or ""
    if old_task.description:
        description = f"{old_task.description}\n\n---\n{summary or '(özet alınamadı)'}".strip()

    new_task = Task.objects.create(
        project=old_task.project,
        title=new_title,
        description=description,
        complexity=old_task.complexity,
        status=Task.STATUS_TODO,
        position=position,
        assigned_agent=old_task.assigned_agent,
    )
    return JsonResponse({"ok": True, "task_id": new_task.pk, "url": f"/tasks/{new_task.pk}/"})


@login_required
@require_POST
def create_task(request):
    """Create a new task in a project from the task-list page."""
    project_id = request.POST.get("project_id")
    title = (request.POST.get("title") or "").strip()
    description = (request.POST.get("description") or "").strip()
    complexity_raw = request.POST.get("complexity") or "3"
    status = (request.POST.get("status") or Task.STATUS_TODO).strip().lower()
    assigned_slug = (request.POST.get("assigned_agent") or "").strip()

    project = get_object_or_404(Project, pk=project_id)
    if not title:
        return JsonResponse({"error": "title required"}, status=400)

    try:
        complexity = max(1, min(5, int(complexity_raw)))
    except ValueError:
        complexity = 3

    valid = {s for s, _ in Task.STATUS_CHOICES}
    if status not in valid:
        status = Task.STATUS_TODO

    assigned_agent = None
    if assigned_slug:
        assigned_agent = Agent.objects.filter(slug=assigned_slug).first()

    # Next "position" in the column (append to end).
    last = (
        Task.objects.filter(project=project, status=status)
        .order_by("-position")
        .values_list("position", flat=True)
        .first()
    )
    position = (last or 0) + 1024

    task = Task.objects.create(
        project=project,
        title=title,
        description=description,
        complexity=complexity,
        status=status,
        position=position,
        assigned_agent=assigned_agent,
    )
    return JsonResponse({"ok": True, "task_id": task.pk})
