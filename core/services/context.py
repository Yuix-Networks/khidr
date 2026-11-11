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
    has_repo = bool((proj.repo_url or "").strip())
    env_lines = [
        "- Your working directory is the task's own workspace"
        + (f": a clone of {proj.repo_url}." if has_repo else " (this project has no repository)."),
    ]
    if has_repo:
        env_lines += [
            "- Commit your work locally. You cannot push: when you call "
            "set_status('review'), Khidr rebases your commits onto the latest "
            "default branch and pushes them with its own credentials.",
            "- `git fetch origin` works (read-only) if you need the latest upstream.",
        ]
    if deploy and proj.allow_agent_deploy:
        env_lines.append(f"- Deploy: call the `deploy` tool after set_status('review') (runs `{deploy}`).")
    elif deploy:
        env_lines.append("- Deploy: the user deploys; don't try to deploy yourself.")
    if proj.server_connection:
        env_lines.append(
            "- Server: inspect it with the `server_exec` tool (read-only commands"
            + (f", project at {proj.server_path}" if proj.server_path else "")
            + "). You have no SSH access of your own."
        )
    env_lines.append("- Never edit files on servers directly.")

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
        messages.append(
            {
                "role": "user",
                "content": (
                    "Take this card. Follow the flow in Shared Memory: a short plan, "
                    "do the work, test it, commit, then set_status('review')."
                ),
            }
        )

    return messages
