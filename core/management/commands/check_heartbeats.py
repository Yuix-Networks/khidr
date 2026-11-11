"""
One-shot heartbeat checker. Resets stale in-progress tasks.

Useful for manual inspection or as a cron job if you'd rather not run the
full daemon. The `run_orchestrator` daemon does this too, on each tick.
"""

from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core.models import Task
from core.services.dispatcher import revert_task_to_todo


class Command(BaseCommand):
    help = "Reset in-progress tasks whose agents have been silent past HEARTBEAT_TIMEOUT_SECONDS."

    def handle(self, *args, **options):
        cutoff = timezone.now() - timedelta(seconds=settings.HEARTBEAT_TIMEOUT_SECONDS)
        stale = Task.objects.filter(
            status=Task.STATUS_IN_PROGRESS,
            current_agent__isnull=False,
            waiting_for_user=False,
            last_heartbeat__lt=cutoff,
        )
        stale_list = list(stale.select_related("project"))
        for task in stale_list:
            self.stdout.write(
                f"- resetting task {task.pk} ({task.title[:50]}) "
                f"last_heartbeat={task.last_heartbeat}"
            )
            revert_task_to_todo(task, reason="heartbeat_timeout")
        self.stdout.write(self.style.SUCCESS(f"done — reset {len(stale_list)} task(s)"))
