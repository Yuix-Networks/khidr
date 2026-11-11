"""
Upsert agents from a YAML file into the DB.

Default file: agents.yaml (in repo root; copy agents.yaml.example to start).
"""

from __future__ import annotations

from pathlib import Path

import yaml
from django.conf import settings
from django.core.management.base import BaseCommand

from core.models import Agent, Project


# Older configs called the Claude harness by its SDK name.
_TYPE_ALIASES = {"claude-agent-sdk": Agent.TYPE_CLAUDE}


class Command(BaseCommand):
    help = "Load/upsert agents from agents.yaml into the DB."

    def add_arguments(self, parser):
        parser.add_argument("--file", default="agents.yaml")
        parser.add_argument(
            "--prune",
            action="store_true",
            help="Delete agents not present in the file.",
        )

    def handle(self, *args, file=None, prune=False, **options):
        path: Path = settings.BASE_DIR / file
        if not path.exists():
            self.stdout.write(self.style.ERROR(f"File not found: {path}"))
            return

        data = yaml.safe_load(path.read_text()) or {}
        entries = data.get("agents", [])
        seen: set[str] = set()

        for entry in entries:
            slug = entry.get("id")
            if not slug:
                self.stdout.write(self.style.WARNING(f"skipping entry without id: {entry}"))
                continue
            seen.add(slug)

            project_names = entry.get("projects", []) or []
            defaults = {
                "type": _TYPE_ALIASES.get(entry.get("type", Agent.TYPE_CLAUDE), entry.get("type")),
                "auth": entry.get("auth", Agent.AUTH_API_KEY),
                "sandbox": entry.get("sandbox", "") or "",
                "max_concurrency": max(1, int(entry.get("max_concurrency", 1))),
                "context_length": int(entry.get("context_length", 0) or 0),
                "role": entry.get("role", Agent.ROLE_WORKER),
                "base_url": entry.get("base_url", "") or "",
                "model": entry.get("model", "") or "",
                "api_key_env": entry.get("api_key_env", "") or "",
                "priority": int(entry.get("priority", 100)),
                "talent": max(1, min(5, int(entry.get("talent", 3)))),
                "is_active": bool(entry.get("is_active", True)),
            }

            if defaults["type"] not in dict(Agent.TYPE_CHOICES):
                self.stdout.write(self.style.ERROR(f"skipping {slug}: unknown type {defaults['type']!r}"))
                continue
            agent, created = Agent.objects.update_or_create(slug=slug, defaults=defaults)

            if project_names:
                lookup = {p.name.lower(): p for p in Project.objects.all()}
                projects = [lookup[n.lower()] for n in project_names if n.lower() in lookup]
                missing = [n for n in project_names if n.lower() not in lookup]
                if missing:
                    self.stdout.write(
                        self.style.WARNING(f"  {slug}: unknown project(s) {missing}")
                    )
                agent.projects.set(projects)
            else:
                agent.projects.clear()

            tag = "+" if created else "~"
            self.stdout.write(f"{tag} {slug} ({agent.type}/{agent.auth}, role={agent.role})")

        if prune:
            stale = Agent.objects.exclude(slug__in=seen)
            if stale.exists():
                n = stale.count()
                stale.delete()
                self.stdout.write(self.style.WARNING(f"- pruned {n} agent(s)"))

        self.stdout.write(self.style.SUCCESS("Done."))
