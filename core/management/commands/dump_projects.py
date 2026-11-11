"""
Emit active projects as a single JSON line, consumed by container provisioning scripts.

Deliberately minimal: fab shells out, parses stdout, iterates. Paths have
the leading `~` expanded to `/home/khidr` so they're usable both on the
host and inside an agent container (both mount /home/khidr).
"""

from __future__ import annotations

import json

from django.core.management.base import BaseCommand

from core.models import Project


def _expand(path: str) -> str:
    return path.replace("~", "/home/khidr") if path else path


class Command(BaseCommand):
    help = "Emit active projects as JSON for container provisioning."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            help="Only this project, active or not (the orchestrator itself: "
            "every agent container runs its worker from it).",
        )

    def handle(self, *args, **opts):
        if opts.get("name"):
            projects = Project.objects.filter(name=opts["name"])
        else:
            projects = Project.objects.filter(is_active=True).order_by("name")
        payload = [
            {
                "name": p.name,
                "repo_path": _expand(p.repo_path or ""),
                "repo_url": p.repo_url or "",
                "pyenv_path": _expand(p.pyenv_path or ""),
                "python_path": _expand(p.python_path or ""),
                "git_remotes": [
                    {"name": r.name, "url": r.url}
                    for r in p.git_remotes.all()
                ],
            }
            for p in projects
        ]
        self.stdout.write(json.dumps(payload))
