"""Pick the right runner class for an Agent row."""

from __future__ import annotations

from core.models import Agent, Task
from core.services.tools import Toolbox

from .base import AgentRunner
from .claude_runner import ClaudeAgentRunner


def build_runner(
    agent: Agent,
    task: Task,
    system_prompt: str,
    messages: list[dict],
    toolbox: Toolbox | None = None,
    **kwargs,
) -> AgentRunner:
    if agent.type == Agent.TYPE_CLAUDE:
        return ClaudeAgentRunner(
            agent, task, system_prompt, messages, toolbox=None, **kwargs
        )
    raise ValueError(f"Unknown agent type: {agent.type}")
