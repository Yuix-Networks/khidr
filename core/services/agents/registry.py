"""Pick the runner for an Agent row."""

from __future__ import annotations

from core.models import Agent, Task
from core.services.tools import Toolbox

from .base import AgentRunner
from .runner import HarnessRunner


def build_runner(
    agent: Agent,
    task: Task,
    system_prompt: str,
    messages: list[dict],
    toolbox: Toolbox | None = None,
    **kwargs,
) -> AgentRunner:
    """Every agent type runs through the same runner; the harness decides
    how its CLI is invoked and parsed."""
    from django.conf import settings

    kwargs.setdefault("max_iterations", settings.KHIDR_MAX_TURNS)
    return HarnessRunner(agent, task, system_prompt, messages, toolbox=None, **kwargs)
