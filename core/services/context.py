"""
Build the agent's initial context: shared memory + shared tools + project
description (from the DB) + server info + current task + chat summary.
"""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

from core.models import ChatMessage, ChatSummary, Task


def _read(path: Path, label: str) -> str:
    if path.exists():
        return path.read_text()
    return f"(missing: {label} at {path})"


def build_system_prompt(task: Task) -> str:
    base: Path = settings.BASE_DIR
    proj = task.project

    parts: list[str] = []

    parts.append(
        "# Shared Memory\n\n" + _read(base / "shared" / "memory.md", "shared/memory.md")
    )
    parts.append(
        "# Available Tools\n\n"
        + _read(base / "shared" / "shared_tools.md", "shared/shared_tools.md")
    )
    parts.append(
        f"# Project: {proj.name}\n\n"
        + (
            proj.description.strip()
            if proj.description
            else "(no description yet)"
        )
    )

    deploy = (proj.deploy_command or "").strip()
    env_lines = [
        f"- Local: {proj.repo_path or '(not set — ask user)'}",
        f"- Deploy: {deploy or '(none — this project has no deploy step)'}",
    ]
    if proj.python_path:
        env_lines.append(f"- Python: {proj.python_path}")
    if proj.pyenv_path:
        env_lines.append(f"- pyenv: {proj.pyenv_path}")
    if proj.server_connection:
        env_lines.append(
            f"- Remote: {proj.server_connection} (path: {proj.server_path or 'not set'})"
        )

    local = proj.repo_path or "(project path)"
    env_lines.append("")
    env_lines.append(
        f"⚠️ Shell cwd persists across calls. File tools resolve against `{local}`. "
        f"Use absolute paths or `cd {local} && <cmd>`. "
        + (
            f"Never edit server files directly — ship with `{deploy}`."
            if deploy
            else "Never edit server files directly."
        )
    )

    parts.append("# Environment\n\n" + "\n".join(env_lines) + "\n")

    summary = ChatSummary.objects.filter(task=task).first()
    if summary:
        parts.append(
            f"# Current Task (id={task.pk}): {task.title}\n\n"
            f"## Description\n\n{task.description or '(empty)'}\n\n"
            f"# Earlier (summarized)\n\n{summary.summary_text}"
        )
    else:
        parts.append(
            f"# Current Task (id={task.pk}): {task.title}\n\n"
            f"## Description\n\n{task.description or '(empty)'}"
        )

    return "\n\n---\n\n".join(parts)


# System messages that are just orchestrator/dispatcher bookkeeping — they're
# useful in the UI timeline but add noise to the agent's context, so we skip
# them when building the message list that gets handed to the model.
_UI_ONLY_SYSTEM_PREFIXES = (
    "[dispatcher]",
    "[orchestrator]",
    "[orchestrator/",
    "SESSION CRASH",
)


def _is_ui_only_system(content: str) -> bool:
    c = (content or "").lstrip()
    return any(c.startswith(p) for p in _UI_ONLY_SYSTEM_PREFIXES)


def build_messages(task: Task, seed_if_empty: bool = True) -> list[dict]:
    """Chat history in OpenAI format (excluding system prompt)."""
    summary = ChatSummary.objects.filter(task=task).first()
    qs = task.messages.all().order_by("created_at")
    if summary and summary.up_to_message_id:
        qs = qs.filter(created_at__gt=summary.up_to_message.created_at)

    messages: list[dict] = []
    for m in qs:
        if m.role == ChatMessage.ROLE_USER:
            messages.append({"role": "user", "content": m.content})
        elif m.role == ChatMessage.ROLE_AGENT:
            messages.append({"role": "assistant", "content": m.content})
        elif m.role == ChatMessage.ROLE_SYSTEM:
            # Skip orchestrator's own bookkeeping rows — they're UI audit
            # trail, not context the model needs.
            if _is_ui_only_system(m.content):
                continue
            # Keep tool traces, notes, user-initiated "[user] ..." entries.
            messages.append({"role": "user", "content": f"[system] {m.content}"})

    if not messages and seed_if_empty:
        deploy = (task.project.deploy_command or "").strip()
        ship = f"commit + push + `{deploy}`" if deploy else "commit + push"
        messages.append(
            {
                "role": "user",
                "content": (
                    "Take this card. Start by following the flow in Shared Memory: "
                    f"first a short plan, then work on main, test, {ship}, "
                    "move it to Review."
                ),
            }
        )

    return messages
