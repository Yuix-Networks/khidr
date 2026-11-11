"""
Emit active Agent slugs as newline-separated text.

Consumed by container provisioning scripts: one line = one agent slug = one
`khidr-agent-<slug>` container.
"""

from __future__ import annotations

from django.core.management.base import BaseCommand

from core.models import Agent


class Command(BaseCommand):
    help = "Emit active agent slugs (one per line) for container provisioning."

    def handle(self, *args, **opts):
        for slug in Agent.objects.filter(is_active=True).order_by("slug").values_list(
            "slug", flat=True
        ):
            self.stdout.write(slug)
