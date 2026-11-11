"""Agent runner abstractions."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Iterator

from core.models import Agent, Task
from core.services.tools import Toolbox

from .events import Event


class AgentRunner(ABC):
    def __init__(
        self,
        agent: Agent,
        task: Task,
        system_prompt: str,
        messages: list[dict],
        toolbox: Toolbox | None = None,
        max_iterations: int = 30,
    ):
        self.agent = agent
        self.task = task
        self.system_prompt = system_prompt
        self.messages = list(messages)  # mutable working copy
        self.toolbox = toolbox
        self.max_iterations = max_iterations

    @abstractmethod
    def run(self) -> Iterator[Event]:
        """Drive the model with tools until it stops, yielding events."""
        raise NotImplementedError
