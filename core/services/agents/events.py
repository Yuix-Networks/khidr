"""Events emitted by an AgentRunner while driving a task."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


@dataclass
class TextChunk:
    content: str


@dataclass
class ToolInvocation:
    name: str
    args: dict
    result_content: str
    detail: dict = field(default_factory=dict)


@dataclass
class Done:
    reason: Literal[
        "finished",
        "waiting_for_user",
        "max_iterations",
        "error",
    ]
    message: str = ""


@dataclass
class ContextUpdate:
    context_used: int
    context_limit: int
    tokens_per_second: float


@dataclass
class AgentStatus:
    status: Literal["thinking", "tool_call", "writing", "idle", "done"]
    detail: str = ""


Event = TextChunk | ToolInvocation | Done | ContextUpdate | AgentStatus
