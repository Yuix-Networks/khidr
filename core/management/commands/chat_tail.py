"""
Live tail of a task's chat log — stand-in until M4 (real chat UI) ships.

    python manage.py chat_tail --task-id 12
    python manage.py chat_tail --task-id 12 --since 20    # last 20 first
"""

from __future__ import annotations

import time

from django.core.management.base import BaseCommand, CommandError

from core.models import ChatMessage, Task


_COLORS = {
    "user": "\033[36m",     # cyan
    "agent": "\033[32m",    # green
    "system": "\033[90m",   # grey
}
_RESET = "\033[0m"


class Command(BaseCommand):
    help = "Live-tail ChatMessage rows for a task."

    def add_arguments(self, parser):
        parser.add_argument("--task-id", type=int, required=True)
        parser.add_argument("--since", type=int, default=20, help="Show the last N messages first.")
        parser.add_argument("--interval", type=float, default=1.0, help="Poll interval seconds.")

    def handle(self, *args, task_id, since, interval, **options):
        try:
            task = Task.objects.get(pk=task_id)
        except Task.DoesNotExist:
            raise CommandError(f"Task {task_id} not found")

        self.stdout.write(self.style.NOTICE(
            f"=== {task.project.name} / {task.title} [c={task.complexity}] ==="
        ))

        recent = list(
            ChatMessage.objects.filter(task_id=task_id).order_by("-id")[:since]
        )
        recent.reverse()
        last_id = 0
        for m in recent:
            self._print(m)
            last_id = max(last_id, m.id)

        try:
            while True:
                time.sleep(interval)
                new = ChatMessage.objects.filter(task_id=task_id, id__gt=last_id).order_by("id")
                for m in new:
                    self._print(m)
                    last_id = m.id
        except KeyboardInterrupt:
            self.stdout.write("\n" + self.style.WARNING("stopped"))

    def _print(self, m: ChatMessage):
        color = _COLORS.get(m.role, "")
        who = m.agent.slug if m.agent_id else m.role
        ts = m.created_at.strftime("%H:%M:%S")
        header = f"{color}[{ts}] {m.role:6}{(' @ ' + who) if m.agent_id else ''}{_RESET}"
        self.stdout.write(header)
        for line in (m.content or "").splitlines() or [""]:
            self.stdout.write(f"  {line}")
        self.stdout.write("")
