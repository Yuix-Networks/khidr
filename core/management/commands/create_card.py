"""
Create a new kanban card (Task) in the local DB.

    python manage.py create_card <project_id> "Card Title" "Card description"

The card is created with status 'todo' by default.

Optional flags:
    --backlog    Create the card with status 'backlog' instead of 'todo'
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Project, Task


class Command(BaseCommand):
    help = "Create a new kanban card for a project."

    def add_arguments(self, parser):
        parser.add_argument("project_id", type=int)
        parser.add_argument("title")
        parser.add_argument("description", nargs="?", default="")
        parser.add_argument("--backlog", action="store_true", help="Set status to backlog instead of todo")

    def handle(self, *args, **opts):
        try:
            project = Project.objects.get(pk=opts["project_id"])
        except Project.DoesNotExist:
            raise CommandError(f"Project with id={opts['project_id']} does not exist.")

        title = opts["title"].strip()
        if not title:
            raise CommandError("Card title is required.")

        status = Task.STATUS_BACKLOG if opts["backlog"] else Task.STATUS_TODO

        card = Task.objects.create(
            project=project,
            title=title,
            description=opts["description"] or "",
            status=status,
        )

        self.stdout.write(
            self.style.SUCCESS(
                f"Created card #{card.pk}: '{card.title}' [{card.status}] in project '{project.name}'"
            )
        )
