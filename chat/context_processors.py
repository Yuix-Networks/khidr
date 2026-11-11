"""Expose a few settings values to every template."""

from __future__ import annotations

from django.conf import settings


def common(request):
    return {
        "TIME_VERSION": settings.TIME_VERSION,
    }
