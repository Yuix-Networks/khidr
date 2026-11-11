"""
Run a single agent → task interaction with tools.

Drives the agent to completion (or until ask_user / max iterations). Streams
output to stdout and persists the full trace into ChatMessage rows.

    python manage.py run_once --task-id 3
    python manage.py run_once --task-id 3 --agent-slug local-qwen
    python manage.py run_once --task-id 3 --dry-run          # just print the prompt
    python manage.py run_once --task-id 3 --no-tools          # disable tool-calling
"""

from __future__ import annotations

import json
import sys
import time

from django.core.management.base import BaseCommand, CommandError

from core.models import Agent, ChatMessage, Task
from core.services.agents.events import Done, TextChunk, ToolInvocation
from core.services.agents.registry import build_runner
from core.services.context import build_messages, build_system_prompt
from core.services.tools import Toolbox


class Command(BaseCommand):
    help = "Run one agent interaction on a task (with tools), streaming output."

    def add_arguments(self, parser):
        parser.add_argument("--task-id", type=int, required=True)
        parser.add_argument("--agent-slug")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument(
            "--no-tools", action="store_true", help="Disable tool-calling (debug only)."
        )

    def handle(self, *args, task_id, agent_slug, dry_run, no_tools, **options):
        try:
            task = Task.objects.select_related(
                "project", "current_agent", "assigned_agent"
            ).get(pk=task_id)
        except Task.DoesNotExist:
            raise CommandError(f"Task {task_id} not found")

        agent = self._pick_agent(task, agent_slug)
        system_prompt = build_system_prompt(task)
        messages = build_messages(task)

        if dry_run:
            self.stdout.write(self.style.NOTICE("=== SYSTEM PROMPT ==="))
            self.stdout.write(system_prompt)
            self.stdout.write(self.style.NOTICE("\n=== MESSAGES ==="))
            for m in messages:
                self.stdout.write(f"[{m['role']}]\n{m['content']}\n")
            self.stdout.write(self.style.NOTICE("\n=== TOOL SPECS ==="))
            for spec in Toolbox.specs():
                fn = spec["function"]
                self.stdout.write(f"- {fn['name']}: {fn['description']}")
            return

        # Seed the first user message if this is the task's first run.
        if not task.messages.exists():
            ChatMessage.objects.create(
                task=task,
                role=ChatMessage.ROLE_USER,
                content=messages[0]["content"],
            )

        toolbox = None if no_tools else Toolbox(task=task)
        runner = build_runner(agent, task, system_prompt, messages, toolbox=toolbox)

        self.stdout.write(self.style.NOTICE(f"[{agent.slug}] running...\n"))

        text_buf: list[str] = []

        def flush_text_as_chat():
            nonlocal text_buf
            if text_buf:
                content = "".join(text_buf)
                ChatMessage.objects.create(
                    task=task, role=ChatMessage.ROLE_AGENT, agent=agent, content=content
                )
                text_buf = []

        try:
            for event in runner.run():
                if isinstance(event, TextChunk):
                    sys.stdout.write(event.content)
                    sys.stdout.flush()
                    text_buf.append(event.content)

                elif isinstance(event, ToolInvocation):
                    flush_text_as_chat()
                    line = f"\n  → tool: {event.name}({_short_args(event.args)})\n"
                    sys.stdout.write(line)
                    sys.stdout.flush()
                    # Persist the tool call as a system ChatMessage for the audit trail.
                    ChatMessage.objects.create(
                        task=task,
                        role=ChatMessage.ROLE_SYSTEM,
                        agent=agent,
                        content=self._format_tool_entry(event),
                    )

                elif isinstance(event, Done):
                    flush_text_as_chat()
                    reason = event.reason
                    if reason == "finished":
                        style = self.style.SUCCESS
                    elif reason == "waiting_for_user":
                        style = self.style.WARNING
                    else:
                        style = self.style.ERROR
                    sys.stdout.write("\n")
                    self.stdout.write(style(f"[done: {reason}] {event.message[:200]}"))
                    return

        except Exception as exc:  # noqa: BLE001
            flush_text_as_chat()
            raise CommandError(f"Runner crashed: {exc}") from exc

    # --- helpers -------------------------------------------------------------

    def _pick_agent(self, task: Task, slug: str | None) -> Agent:
        if slug:
            try:
                return Agent.objects.get(slug=slug)
            except Agent.DoesNotExist:
                raise CommandError(f"Agent '{slug}' not found")
        agent = task.current_agent or task.assigned_agent
        if agent is None:
            raise CommandError(
                "No agent set on task. Pass --agent-slug or set task.assigned_agent."
            )
        return agent

    def _format_tool_entry(self, event: ToolInvocation) -> str:
        payload = {
            "tool": event.name,
            "args": event.args,
            "detail": event.detail,
            "result": event.result_content[:4000],
        }
        return "```\n" + json.dumps(payload, ensure_ascii=False, indent=2) + "\n```"


def _short_args(args: dict) -> str:
    s = json.dumps(args, ensure_ascii=False)
    return s if len(s) <= 120 else s[:117] + "..."
