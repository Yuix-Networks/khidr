"""
Create a new project in the local DB.

    python manage.py create_project "Acme" "Acme hosting..."

Project description is read straight from the DB at agent-context time
(no project.md file needed).

Optional flags:
    --repo-url git@github.com:org/repo.git   repository Khidr clones and pushes
    --server "ssh acme"          SSH command for read-only inspection
    --server-path /root/acme     path on the server (reference only)
    --deploy "make deploy"            deploy command agents run after push (default: none)
    --force                           overwrite an existing Project row
"""

from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from core.models import Project


class Command(BaseCommand):
    help = "Register a new project in the DB."

    def add_arguments(self, parser):
        parser.add_argument("name")
        parser.add_argument("description", nargs="?", default="")
        parser.add_argument("--repo-url", default="")
        parser.add_argument("--server", default="", help='SSH command, e.g. "ssh acme"')
        parser.add_argument("--server-path", default="")
        parser.add_argument("--deploy", default="", help="Deploy command run after push (default: none)")
        parser.add_argument("--force", action="store_true")

    def handle(self, *args, **opts):
        name = opts["name"].strip()
        if not name:
            raise CommandError("project name is required")

        existing = Project.objects.filter(name=name).first()
        if existing and not opts["force"]:
            raise CommandError(
                f"Project '{name}' already exists (id={existing.pk}). "
                f"Use --force to update its fields."
            )

        defaults = {
            "description": opts["description"] or "",
            "repo_url": opts["repo_url"] or "",
            "server_connection": opts["server"] or "",
            "server_path": opts["server_path"] or "",
            "deploy_command": opts["deploy"] or "",
            "is_active": True,
        }

        project, created = Project.objects.update_or_create(name=name, defaults=defaults)
        action = "Created" if created else "Updated"
        self.stdout.write(self.style.SUCCESS(f"{action} project #{project.pk}: {project.name}"))

        self.stdout.write("")
        self.stdout.write("Next steps:")
        if not project.repo_url:
            self.stdout.write(f"  - Set repo_url in admin: /admin/core/project/{project.pk}/change/")
        self.stdout.write("  - Attach one or more agents to this project")
        self.stdout.write(
            f"      python manage.py shell -c \"from core.models import *; "
            f"Agent.objects.get(slug='my-agent').projects.add(Project.objects.get(pk={project.pk}))\""
        )
        self.stdout.write("  - Open / in the browser; create tasks from the UI.")
