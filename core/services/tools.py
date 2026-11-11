"""
Task-level custom tools (note / set_status / reject_task / ask_user / …).

Harness-agnostic: `Toolbox.specs()` gives JSON schemas and
`Toolbox.call(name, args)` runs a tool. The agent worker exposes them to
the Claude Agent SDK as an in-process MCP server
(core.services.agents.mcp_tools).
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from django.db.models import Q

from core.models import Agent, ChatMessage, IgnoreRule, Task
from core.services.telegram import notify_task

log = logging.getLogger(__name__)

_MINIMAX_BASE_URL = "https://api.minimax.io"


def _summarize_with_minimax(content: str, hint: str = "") -> str | None:
    """Summarize long content via MiniMax M2.5. Returns None on failure."""
    try:
        key = os.environ.get("MINIMAX_API_KEY") or ""
        if not key:
            return None
        prompt = (
            f"{hint}\n\n"
            f"Summarize the following in 2-3 concise sentences that capture "
            f"the essential information for future reference:\n\n{content}"
        )
        payload = {
            "model": "MiniMax-M2.5",
            "messages": [{"role": "user", "content": prompt}],
            "max_tokens": 256,
        }
        resp = httpx.post(
            f"{_MINIMAX_BASE_URL}/minimax-api/v1/text/chatcompletion_v2",
            json=payload,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            timeout=30.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return (
            data.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            .strip()
        )
    except Exception:
        log.debug("MiniMax summarization failed, keeping original content")
        return None

def _minimax_request(path: str, payload: dict) -> dict:
    key = os.environ.get("MINIMAX_API_KEY") or ""
    if not key:
        raise RuntimeError("MINIMAX_API_KEY not set")
    resp = httpx.post(
        f"{_MINIMAX_BASE_URL}{path}",
        json=payload,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json()


# True when this Toolbox is running inside an agent's Docker container.
# False when running in the orchestrator daemon on the khidr host.
# The distinction matters for git state checks in `set_status`:
# host-side Toolbox must peek at the agent's container workspace,
# not the orchestrator's stale clone.
_IN_CONTAINER = os.environ.get("KHIDR_IN_CONTAINER") == "1"


@dataclass
class ToolResult:
    """What a tool returns."""

    content: str
    detail: dict = field(default_factory=dict)


class Toolbox:
    """Task-level tool bag, exposed to agents as the `khidr` MCP server."""

    def __init__(self, task: Task):
        self.task = task
        self.project = task.project

    # ------------------------------------------------------------------ specs

    @staticmethod
    def specs() -> list[dict]:
        """OpenAI-style tool schemas (name / description / JSON schema)."""
        return [
            _spec(
                "note",
                "Post a system note on this task's chat. Use for progress "
                "updates, handoffs, and completion summaries — shows up as a "
                "highlighted entry in the UI.",
                {"text": ("string", "Note markdown.")},
                required=["text"],
            ),
            _spec(
                "set_status",
                "Change this task's status column. Call with 'review' when "
                "the work is complete and ready for user review.",
                {
                    "status": (
                        "string",
                        "One of backlog, todo, in_progress, review, done.",
                    ),
                },
                required=["status"],
            ),
            _spec(
                "reject_task",
                "Close this card as unnecessary without doing any work: the "
                "error is transient/noise, already fixed, or not a code "
                "problem. Moves the card to done with your reason. Leave the "
                "repo untouched.",
                {
                    "reason": ("string", "Why this card needs no work (1-3 sentences)."),
                    "ignore_pattern": (
                        "string",
                        "Optional, GlitchTip cards only: Python regex matched "
                        "against future alert titles of this project; matches "
                        "never become a card again. Keep it narrow.",
                    ),
                },
                required=["reason"],
            ),
            _spec(
                "ask_user",
                "Pause and ask the user a question. Use when you need "
                "clarification/approval and cannot continue without it. Your "
                "turn ends after this call; the loop resumes when the user "
                "replies. Also fires a Telegram notification.",
                {"message": ("string", "Question to the user.")},
                required=["message"],
            ),
            _spec(
                "get_kanbancard",
                "Read another kanban card's details: title, description, "
                "status, assignment, recent chat excerpts. Useful when the "
                "current card references or depends on another one. "
                "(Do NOT confuse with `task_get` / `task_output` — those "
                "are for background shell processes.)",
                {
                    "card_id": ("integer", "ID of the kanban card to read."),
                    "recent_messages": (
                        "integer",
                        "How many most-recent chat messages to include "
                        "(default 5, max 20).",
                    ),
                },
                required=["card_id"],
            ),
            _spec(
                "search_kanbancards",
                "Free-text search across this project's kanban cards "
                "(title + description, case-insensitive). Returns a short "
                "list of `#id [status] title` lines. Use it to find "
                "related cards before creating a new one or to pick up "
                "where a prior card left off.",
                {
                    "query": ("string", "Substring to match."),
                    "max_results": ("integer", "Cap on returned cards (default 20, max 50)."),
                    "include_done": (
                        "boolean",
                        "Include cards already in Done. Default true — set "
                        "false to only see active work.",
                    ),
                },
                required=["query"],
            ),
            _spec(
                "get_project_memory",
                "Read this project's shared memory (its description field). "
                "Long-lived cross-agent notes live here — conventions, "
                "gotchas, paths that moved, 'X now uses Y' changes. Check "
                "it before starting non-trivial work so you don't repeat "
                "discoveries others have already written down.",
                {},
                required=[],
            ),
            _spec(
                "add_project_memory",
                "Append a note to this project's shared memory (description). "
                "Use it when you discover or change something that OTHER "
                "agents working on this project need to know later — e.g. "
                "'settings.py now reads PG_* from env', 'myproject requires "
                "python 3.12 since Django 6.0 bump'. Keep it short (one or "
                "two sentences). A timestamp + your agent slug are added "
                "automatically. For progress updates on the current task "
                "use `note` instead — that stays in this card's chat.",
                {"text": ("string", "What other agents should remember.")},
                required=["text"],
            ),
            _spec(
                "create_kanbancard",
                "Create a new kanban card in THIS project (same as the "
                "current card's project). Use for splitting off subtasks "
                "or follow-ups the orchestrator should pick up later. "
                "Starts in Todo by default; returns the new card id. "
                "(Do NOT confuse with `task_create` — that starts a "
                "background shell process.)",
                {
                    "title": ("string", "Short card title."),
                    "description": ("string", "Longer description (optional)."),
                    "complexity": ("integer", "1-5, default 3."),
                    "status": (
                        "string",
                        "'todo' (default) or 'backlog'. Other statuses are "
                        "not accepted here — the dispatcher owns the rest "
                        "of the lifecycle.",
                    ),
                    "assigned_agent": (
                        "string",
                        "Optional agent slug. If set, only that agent may "
                        "pick up the new card. Leave blank to let the "
                        "dispatcher choose.",
                    ),
                },
                required=["title"],
            ),
            _spec(
                "save_memory",
                "Save an important decision, context, or warning to project "
                "memory. Use it to record things other agents working on "
                "this project should know — conventions, tricky workarounds, "
                "unexpected side-effects discovered during a task. The content "
                "is automatically summarized if it's long. For quick "
                "progress notes use `note` instead — that goes to the card's "
                "chat thread.",
                {
                    "content": ("string", "What to remember (will be summarized if long)."),
                    "importance": (
                        "string",
                        "low | medium | high | critical. Default medium. "
                        "Used to filter and prioritize memories.",
                    ),
                },
                required=["content"],
            ),
            _spec(
                "search_memory",
                "Search project memories by keyword to find relevant context "
                "from previous work. Returns matched entries with their "
                "importance level and timestamp.",
                {
                    "query": ("string", "Keyword to search for in memories."),
                    "limit": (
                        "integer",
                        "Max results to return (default 10, max 50).",
                    ),
                },
                required=["query"],
            ),
            _spec(
                "list_memories",
                "List recent memories for context. Returns all memories "
                "sorted by recency, with optional filtering by importance. "
                "Useful at the start of a new task to recall relevant "
                "history.",
                {
                    "limit": ("integer", "Max memories to return (default 20, max 50)."),
                    "importance_filter": (
                        "string",
                        "Optional: filter by importance level "
                        "(low | medium | high | critical). If omitted, "
                        "returns all levels.",
                    ),
                },
                required=[],
            ),
            _spec(
                "web_search",
                "Performs a web search based on a query string, returning "
                "search results and related suggestions. Use this to look up "
                "current information, documentation, or anything that "
                "requires internet access.",
                {"query": ("string", "Search query.")},
                required=["query"],
            ),
            _spec(
                "understand_image",
                "Analyzes and understands image content. Supports HTTP/HTTPS "
                "URLs or local file paths. Provide a clear question or "
                "analysis request in the prompt. Supported formats: JPEG, "
                "PNG, GIF, WebP (max 20MB).",
                {
                    "prompt": ("string", "Question or analysis request for the image."),
                    "image_url": ("string", "Image source URL or local file path."),
                },
                required=["prompt", "image_url"],
            ),
            _spec(
                "get_glitchtip_issue",
                "Fetch detailed information about a GlitchTip error issue "
                "from its URL. Use this when an error report or alert arrives "
                "as a link. Returns: title, status, level, culprit, tags, "
                "user count, first/last seen.",
                {
                    "issue_url": (
                        "string",
                        "The full GlitchTip issue URL, e.g. "
                        "https://glitchtip.example.com/acme/issues/3812/",
                    ),
                },
                required=["issue_url"],
            ),
        ]

    # ------------------------------------------------------------------ dispatch

    def call(self, name: str, args: dict[str, Any]) -> ToolResult:
        # Some models occasionally emit tool names with leading/trailing
        # whitespace (e.g. " bash"). Normalize so they don't bounce off
        # the registry as "Unknown tool".
        name = (name or "").strip()
        method = getattr(self, f"_tool_{name}", None)
        if method is None:
            msg = f"Unknown tool: {name}"
            return ToolResult(content=msg, detail={"error": msg})
        try:
            return method(args)
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return ToolResult(
                content=f"{type(exc).__name__}: {exc}",
                detail={"error": type(exc).__name__},
            )

    # ------------------------------------------------------------------ tools

    def _tool_note(self, args: dict) -> ToolResult:
        text = args["text"]
        ChatMessage.objects.create(
            task=self.task,
            role=ChatMessage.ROLE_SYSTEM,
            content=f"📝 **note**\n\n{text}",
        )
        return ToolResult(content="note posted", detail={"text": text[:160]})

    def _tool_set_status(self, args: dict) -> ToolResult:
        raw = (args.get("status") or "").strip().lower().replace(" ", "_")
        valid = {s for s, _ in Task.STATUS_CHOICES}
        if raw not in valid:
            return ToolResult(
                content=f"Unknown status '{raw}'. Known: {sorted(valid)}",
                detail={"error": "unknown_status"},
            )

        if raw in (Task.STATUS_REVIEW, Task.STATUS_DONE):
            guard = self._check_repo_clean_on_main()
            if guard is not None:
                return guard

        Task.objects.filter(pk=self.task.pk).update(status=raw)
        self.task.status = raw

        if raw == Task.STATUS_REVIEW:
            try:
                notify_task(self.task, "✅ Ready for review")
            except Exception as exc:  # noqa: BLE001
                log.warning("telegram notify (review) failed: %s", exc)

        return ToolResult(
            content=f"status set to '{raw}'",
            detail={"status": raw},
        )

    def _tool_reject_task(self, args: dict) -> ToolResult:
        reason = (args.get("reason") or "").strip()
        if not reason:
            return ToolResult(content="reason is required", detail={"error": "bad_arg"})
        pattern = (args.get("ignore_pattern") or "").strip()
        if pattern:
            if self.task.source != Task.SOURCE_GLITCHTIP:
                return ToolResult(
                    content="ignore_pattern only applies to GlitchTip cards; call again without it.",
                    detail={"error": "bad_arg"},
                )
            try:
                matches = re.search(pattern, self.task.title, re.IGNORECASE)
            except re.error as exc:
                return ToolResult(content=f"invalid regex: {exc}", detail={"error": "bad_regex"})
            if not matches:
                return ToolResult(
                    content=f"ignore_pattern doesn't match this card's title {self.task.title!r}; fix it.",
                    detail={"error": "bad_regex"},
                )

        guard = self._check_repo_clean_on_main()
        if guard is not None:
            return guard

        if pattern:
            IgnoreRule.objects.create(
                pattern=pattern, project=self.project, note=f"#{self.task.pk}: {reason}"[:256],
            )
        Task.objects.filter(pk=self.task.pk).update(status=Task.STATUS_DONE)
        self.task.status = Task.STATUS_DONE
        ChatMessage.objects.create(
            task=self.task,
            role=ChatMessage.ROLE_SYSTEM,
            content=f"🚫 **reddedildi**\n\n{reason}"
            + (f"\n\nIgnore rule eklendi: `{pattern}`" if pattern else ""),
        )
        try:
            notify_task(self.task, f"🚫 Reddedildi: {reason}")
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram notify (reject) failed: %s", exc)
        return ToolResult(
            content="card rejected and moved to done" + (f"; ignore rule added: {pattern}" if pattern else ""),
            detail={"status": Task.STATUS_DONE, "ignore_pattern": pattern},
        )

    def _tool_get_kanbancard(self, args: dict) -> ToolResult:
        raw_id = args.get("card_id") or args.get("task_id")
        try:
            tid = int(raw_id)
        except (TypeError, ValueError):
            return ToolResult(
                content="card_id is required and must be an integer",
                detail={"error": "bad_arg"},
            )
        try:
            other = Task.objects.select_related(
                "project", "assigned_agent", "current_agent"
            ).get(pk=tid)
        except Task.DoesNotExist:
            return ToolResult(
                content=f"card {tid} not found",
                detail={"error": "not_found"},
            )

        try:
            n = int(args.get("recent_messages") or 5)
        except (TypeError, ValueError):
            n = 5
        n = max(1, min(20, n))

        lines = [
            f"# Kanban card #{other.pk}: {other.title}",
            f"- Project: {other.project.name}",
            f"- Status: {other.get_status_display()}  (complexity: {other.complexity})",
            f"- Assigned: {other.assigned_agent.slug if other.assigned_agent_id else '(none)'}",
            f"- Currently working: {other.current_agent.slug if other.current_agent_id else '(none)'}",
            "",
            "## Description",
            other.description.strip() if other.description else "(empty)",
        ]

        recent = list(
            other.messages.select_related("agent").order_by("-id")[:n]
        )[::-1]
        if recent:
            lines.append("")
            lines.append(f"## Last {len(recent)} chat messages")
            for m in recent:
                who = m.agent.slug if m.agent_id else m.role
                body = (m.content or "").strip().replace("\n", " ")
                if len(body) > 300:
                    body = body[:300] + "…"
                lines.append(f"- [{who}] {body}")

        return ToolResult(
            content="\n".join(lines),
            detail={"card_id": other.pk, "project_id": other.project_id},
        )

    # ---- project memory (shared notes written to Project.description) ------

    _MEMORY_MARKER = "## Agent memory"

    def _tool_get_project_memory(self, args: dict) -> ToolResult:
        desc = self.project.description or ""
        return ToolResult(
            content=desc if desc.strip() else "(project description is empty)",
            detail={"project_id": self.project.pk, "length": len(desc)},
        )

    def _tool_add_project_memory(self, args: dict) -> ToolResult:
        text = (args.get("text") or "").strip()
        if not text:
            return ToolResult(
                content="text is required — nothing to remember",
                detail={"error": "empty_text"},
            )

        from django.utils import timezone

        current = self.project.description or ""
        agent_slug = (
            self.task.current_agent.slug if self.task.current_agent_id else "?"
        )
        stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{stamp} · {agent_slug}] {text}"

        # Append under a dedicated "## Agent memory" section so project
        # descriptions stay legible — each note becomes a bullet under that
        # header. If the section doesn't exist yet, create it at the end.
        if self._MEMORY_MARKER in current:
            updated = current.rstrip() + "\n" + entry + "\n"
        else:
            sep = "\n\n" if current.strip() else ""
            updated = (
                current.rstrip()
                + f"{sep}{self._MEMORY_MARKER}\n\n"
                + entry
                + "\n"
            )

        from core.models import Project as _Project  # avoid shadowing

        _Project.objects.filter(pk=self.project.pk).update(description=updated)
        self.project.description = updated

        return ToolResult(
            content=f"project memory updated ({len(updated)} chars total).",
            detail={"project_id": self.project.pk, "appended": entry},
        )

    # ---- structured project memory (importance + search/listing) -----------

    _IMPORTANCE_LABELS = {
        "critical": "🔴 CRITICAL",
        "high": "🟠 HIGH",
        "medium": "🟡 MEDIUM",
        "low": "🟢 LOW",
    }

    def _tool_save_memory(self, args: dict) -> ToolResult:
        import re

        content = (args.get("content") or "").strip()
        if not content:
            return ToolResult(
                content="content is required — nothing to remember",
                detail={"error": "empty_content"},
            )

        importance_raw = (args.get("importance") or "medium").lower().strip()
        importance = self._IMPORTANCE_LABELS.get(
            importance_raw,
            self._IMPORTANCE_LABELS["medium"],
        )
        if importance_raw not in self._IMPORTANCE_LABELS:
            return ToolResult(
                content=(
                    f"Unknown importance '{importance_raw}'. "
                    f"Use one of: {', '.join(self._IMPORTANCE_LABELS.keys())}"
                ),
                detail={"error": "bad_importance"},
            )

        # Auto-summarize long content via MiniMax M2.5
        if len(content) > 2000:
            summarized = _summarize_with_minimax(
                content,
                f"Structured project memory entry (importance={importance_raw}):",
            )
            if summarized:
                content = summarized
                log.debug("save_memory auto-summarized %d chars → %d", len(args["content"]), len(content))

        from django.utils import timezone

        current = self.project.description or ""
        agent_slug = (
            self.task.current_agent.slug if self.task.current_agent_id else "?"
        )
        stamp = timezone.now().strftime("%Y-%m-%d %H:%M")
        entry = f"- [{importance} · {stamp} · {agent_slug}] {content}"

        if self._MEMORY_MARKER in current:
            updated = current.rstrip() + "\n" + entry + "\n"
        else:
            sep = "\n\n" if current.strip() else ""
            updated = (
                current.rstrip()
                + f"{sep}{self._MEMORY_MARKER}\n\n"
                + entry
                + "\n"
            )

        from core.models import Project as _Project

        _Project.objects.filter(pk=self.project.pk).update(description=updated)
        self.project.description = updated

        return ToolResult(
            content=f"memory saved ({importance_raw} importance, {len(updated)} chars total).",
            detail={
                "project_id": self.project.pk,
                "importance": importance_raw,
                "appended": entry,
                "was_summarized": len(content) <= 2000,
            },
        )

    def _tool_search_memory(self, args: dict) -> ToolResult:
        import re

        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(
                content="query is required",
                detail={"error": "missing_query"},
            )

        try:
            limit = max(1, min(50, int(args.get("limit") or 10)))
        except (TypeError, ValueError):
            limit = 10

        desc = self.project.description or ""
        if not desc or self._MEMORY_MARKER not in desc:
            return ToolResult(
                content=f"No project memories found (no '## Agent memory' section found).",
                detail={"count": 0},
            )

        section_start = desc.find(self._MEMORY_MARKER)
        section = desc[section_start:]

        # Parse bullets: "- [{importance} · {stamp} · {agent}] {content}"
        bullet_pat = re.compile(
            r"^\- \[([^\]]+)\] (.*)$",
            re.MULTILINE,
        )

        query_lower = query.lower()
        matched = []
        for m in bullet_pat.finditer(section):
            header_part, text = m.group(1), m.group(2)
            if query_lower in (header_part + " " + text).lower():
                matched.append(f"- [{header_part}] {text}")

        matched = matched[:limit]
        if not matched:
            return ToolResult(
                content=f"No memories matching '{query}'.",
                detail={"count": 0},
            )

        output_lines = [
            f"Found {len(matched)} memory/ies matching '{query}':",
            *matched,
        ]
        return ToolResult(
            content="\n".join(output_lines),
            detail={"count": len(matched)},
        )

    def _tool_list_memories(self, args: dict) -> ToolResult:
        import re

        try:
            limit = max(1, min(50, int(args.get("limit") or 20)))
        except (TypeError, ValueError):
            limit = 20

        importance_filter = (args.get("importance_filter") or "").lower().strip()

        desc = self.project.description or ""
        if not desc or self._MEMORY_MARKER not in desc:
            return ToolResult(
                content="No project memories found (no '## Agent memory' section yet).",
                detail={"count": 0},
            )

        section_start = desc.find(self._MEMORY_MARKER)
        section = desc[section_start:]

        bullet_pat = re.compile(
            r"^\- \[([^\]]+)\] (.*)$",
            re.MULTILINE,
        )

        bullets = []
        for m in bullet_pat.finditer(section):
            header_part, text = m.group(1), m.group(2)
            bullets.append(f"- [{header_part}] {text}")

        if not bullets:
            return ToolResult(
                content="No project memories found.",
                detail={"count": 0},
            )

        if importance_filter:
            if importance_filter not in self._IMPORTANCE_LABELS:
                return ToolResult(
                    content=(
                        f"Unknown importance '{importance_filter}'. "
                        f"Use one of: {', '.join(self._IMPORTANCE_LABELS.keys())}"
                    ),
                    detail={"error": "bad_importance"},
                )
            importance_label = self._IMPORTANCE_LABELS[importance_filter]
            filtered = [b for b in bullets if b.startswith(f"- [{importance_label}")]
            shown = filtered[:limit]
            prefix = f"Showing {len(shown)} {importance_filter.upper()} memories: "
        else:
            shown = bullets[:limit]
            prefix = f"Showing {len(shown)} recent memories: "

        return ToolResult(
            content=prefix + "\n" + "\n".join(shown),
            detail={"count": len(shown)},
        )

    def _tool_search_kanbancards(self, args: dict) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(
                content="query is required",
                detail={"error": "missing_query"},
            )
        try:
            max_results = max(1, min(50, int(args.get("max_results") or 20)))
        except (TypeError, ValueError):
            max_results = 20
        raw_include = args.get("include_done")
        include_done = True if raw_include is None else bool(raw_include)

        qs = Task.objects.filter(project=self.project).filter(
            Q(title__icontains=query) | Q(description__icontains=query)
        )
        if not include_done:
            qs = qs.exclude(status=Task.STATUS_DONE)

        matches = list(
            qs.order_by("-updated_at")
            .values("id", "title", "status")[: max_results + 1]
        )
        truncated = len(matches) > max_results
        matches = matches[:max_results]

        if not matches:
            return ToolResult(
                content=f"No kanban cards matching '{query}' in {self.project.name}.",
                detail={"count": 0},
            )

        lines = [
            f"Found {len(matches)} kanban card(s) matching '{query}' in "
            f"{self.project.name}:"
        ]
        for m in matches:
            lines.append(f"- #{m['id']} [{m['status']}] {m['title']}")
        if truncated:
            lines.append(f"... truncated at {max_results} matches")

        return ToolResult(
            content="\n".join(lines),
            detail={"count": len(matches), "truncated": truncated},
        )

    def _tool_create_kanbancard(self, args: dict) -> ToolResult:
        title = (args.get("title") or "").strip()
        if not title:
            return ToolResult(
                content="title is required",
                detail={"error": "missing_title"},
            )
        description = args.get("description") or ""
        try:
            complexity = max(1, min(5, int(args.get("complexity") or 3)))
        except (TypeError, ValueError):
            complexity = 3
        status = (args.get("status") or Task.STATUS_TODO).strip().lower()
        if status not in (Task.STATUS_TODO, Task.STATUS_BACKLOG):
            return ToolResult(
                content=(
                    f"Unsupported status '{status}' for create_kanbancard. "
                    "Only 'todo' and 'backlog' are accepted here."
                ),
                detail={"error": "bad_status"},
            )

        assigned_slug = (args.get("assigned_agent") or "").strip()
        assigned_agent = None
        if assigned_slug:
            assigned_agent = Agent.objects.filter(slug=assigned_slug).first()
            if assigned_agent is None:
                return ToolResult(
                    content=f"Unknown agent slug '{assigned_slug}'",
                    detail={"error": "bad_agent"},
                )

        last_position = (
            Task.objects.filter(project=self.project, status=status)
            .order_by("-position")
            .values_list("position", flat=True)
            .first()
        )
        position = (last_position or 0) + 1024

        new_task = Task.objects.create(
            project=self.project,
            title=title,
            description=description,
            complexity=complexity,
            status=status,
            position=position,
            assigned_agent=assigned_agent,
        )
        ChatMessage.objects.create(
            task=new_task,
            role=ChatMessage.ROLE_SYSTEM,
            content=(
                f"[created by card-{self.task.pk}] parent card spawned this subtask."
            ),
        )

        return ToolResult(
            content=(
                f"Created kanban card #{new_task.pk} '{title}' in project "
                f"{self.project.name} (status={status}, complexity={complexity}"
                f"{', agent=' + assigned_slug if assigned_slug else ''})."
            ),
            detail={
                "card_id": new_task.pk,
                "project_id": self.project.pk,
                "status": status,
            },
        )

    def _tool_ask_user(self, args: dict) -> ToolResult:
        msg = args["message"]
        Task.objects.filter(pk=self.task.pk).update(waiting_for_user=True)
        self.task.waiting_for_user = True
        try:
            notify_task(self.task, "⏸ Agent soru soruyor", body=msg)
        except Exception as exc:  # noqa: BLE001
            log.warning("telegram notify (ask_user) failed: %s", exc)
        return ToolResult(
            content="message sent; your turn ends now, loop resumes when user replies",
            detail={"message": msg, "waiting_for_user": True},
        )

    def _tool_web_search(self, args: dict) -> ToolResult:
        query = (args.get("query") or "").strip()
        if not query:
            return ToolResult(content="query is required", detail={"error": "missing_query"})
        try:
            result = _minimax_request("/minimax-api/v1/imagesearch", {"query": query})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                content=f"web_search failed: {type(exc).__name__}: {exc}",
                detail={"error": type(exc).__name__},
            )
        return ToolResult(content=json.dumps(result, ensure_ascii=False, indent=2))

    def _tool_understand_image(self, args: dict) -> ToolResult:
        prompt = (args.get("prompt") or "").strip()
        image_url = (args.get("image_url") or "").strip()
        if not prompt or not image_url:
            return ToolResult(
                content="prompt and image_url are required", detail={"error": "bad_arg"}
            )
        try:
            result = _minimax_request(
                "/minimax-api/v1/vision",
                {
                    "model": "minimax-vision",
                    "messages": [
                        {
                            "role": "user",
                            "content": [
                                {"type": "text", "text": prompt},
                                {"type": "image_url", "image_url": {"url": image_url}},
                            ],
                        }
                    ],
                },
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                content=f"understand_image failed: {type(exc).__name__}: {exc}",
                detail={"error": type(exc).__name__},
            )
        return ToolResult(content=json.dumps(result, ensure_ascii=False, indent=2))

    # ---- GlitchTip --------------------------------------------------------

    def _tool_get_glitchtip_issue(self, args: dict) -> ToolResult:
        from django.conf import settings

        api_key = getattr(settings, "GLITCHTIP_API_KEY", "") or os.environ.get("GLITCHTIP_API_KEY", "")
        base_url = getattr(settings, "GLITCHTIP_URL", "https://glitchtip.example.com")
        if not api_key:
            return ToolResult(
                content="GLITCHTIP_API_KEY is not configured on the orchestrator.",
                detail={"error": "not_configured"},
            )

        url = (args.get("issue_url") or "").strip()
        m = re.search(r"/issues/(\d+)/?$", url)
        if not m:
            return ToolResult(
                content=(
                    f"Could not extract issue ID from URL: {url}\n"
                    "Expected format: https://glitchtip.example.com/<org>/issues/<id>/"
                ),
                detail={"error": "bad_arg"},
            )
        issue_id = m.group(1)

        try:
            resp = httpx.get(
                f"{base_url}/api/0/issues/{issue_id}/",
                headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
                timeout=15.0,
            )
            if resp.status_code == 404:
                return ToolResult(
                    content=f"Issue #{issue_id} not found in GlitchTip",
                    detail={"error": "not_found"},
                )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            return ToolResult(
                content=f"GlitchTip API error: {type(exc).__name__}: {exc}",
                detail={"error": type(exc).__name__},
            )

        lines = [
            f"# {data.get('title', 'Unknown Issue')}",
            "",
            f"**Status:** {data.get('status', 'unknown')}",
            f"**Level:** {data.get('level', 'unknown')}",
            f"**Culprit:** {data.get('culprit') or 'unknown'}",
            f"**Project:** {(data.get('project') or {}).get('name', 'unknown')}",
            f"**Platform:** {data.get('platform', 'unknown')}",
            "",
            f"**First Seen:** {data.get('firstSeen', 'unknown')}",
            f"**Last Seen:** {data.get('lastSeen', 'unknown')}",
            f"**Occurrences:** {data.get('count', 0)}",
            f"**User Count:** {data.get('userCount', 0)}",
            "",
        ]
        tags = data.get("tags") or []
        if tags:
            lines.append("## Tags")
            lines.extend(f"- {t.get('key')}: {t.get('value')}" for t in tags)
            lines.append("")
        meta = data.get("metadata") or {}
        if meta:
            lines.append("## Metadata")
            for key in ("title", "type", "culprit"):
                if meta.get(key):
                    lines.append(f"**{key.capitalize()}:** {meta[key]}")
            lines.append("")
        if data.get("shortId"):
            lines.append(f"**Short ID:** {data['shortId']}")
        lines.append("")
        lines.append(f"**URL:** {url}")

        return ToolResult(content="\n".join(lines), detail={"issue_id": issue_id})

    # ---- helpers ----------------------------------------------------------

    def _run_git_in_agent_repo(
        self, argv: list[str], repo: Path
    ) -> subprocess.CompletedProcess | None:
        """Run `git *argv` against the agent's working tree.

        Returns None if we're on the host but the agent has no running
        container — the guard treats that as "can't verify" and fails safe.

        Two execution paths:
        - In-container ( Toolbox running inside khidr-agent-<slug>):
          just run locally; cwd is the agent's own workspace.
        - On-host (Toolbox running in the orchestrator daemon): host's own /home/khidr/workspace/<project> is a stale
          clone (agent never touches it); route through `docker exec` so we
          inspect the right filesystem.
        """
        if _IN_CONTAINER:
            return subprocess.run(
                ["git", *argv],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
            )

        # Lazy imports — docker_exec pulls core.models back in, which is
        # fine but keeps the module graph shallow for simple callers.
        from core.services.agents.docker_exec import (
            agent_container_if_running,
            wrap_with_docker_exec,
        )

        agent = self.task.current_agent or self.task.assigned_agent
        container = agent_container_if_running(agent) if agent else None
        if container is None:
            return None

        cmd = wrap_with_docker_exec(
            ["git", *argv],
            container=container,
            cwd=str(repo),
            interactive=False,
        )
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def _deploy_hint(self) -> str:
        """' (+ `<deploy_command>`)' for the project, or '' when it has none."""
        deploy = (self.task.project.deploy_command or "").strip()
        return f" (+ `{deploy}`)" if deploy else ""

    def _default_branch_in_agent_repo(self, repo: Path) -> str | None:
        """Resolve the repo's actual default branch (main, master, …).

        Reads origin's HEAD symref rather than assuming a name, so projects
        that default to `master` (or anything else) aren't punished by a
        guard written with `main` in mind.
        """
        ref = self._run_git_in_agent_repo(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], repo
        )
        if ref is None or ref.returncode != 0:
            return None
        value = ref.stdout.strip()
        if value.startswith("origin/"):
            value = value[len("origin/"):]
        return value or None

    def _check_repo_clean_on_main(self) -> ToolResult | None:
        """Return a ToolResult (error) if the repo isn't clean on its default branch."""
        if not self.project.repo_path:
            return None
        repo = Path(self.project.repo_path).expanduser()
        # The .git check runs against whatever filesystem we're on; that's
        # fine because both host and container layouts have the repo at
        # the same path (bind-mount or real clone).
        if not (repo / ".git").exists() and _IN_CONTAINER:
            return None

        dirty = self._run_git_in_agent_repo(["status", "--porcelain"], repo)
        if dirty is None:
            return ToolResult(
                content=(
                    "Refusing set_status: can't reach the agent's container "
                    "to verify its working tree is clean. The container for "
                    "this task's agent isn't running."
                ),
                detail={"error": "no_container"},
            )
        if dirty.stdout.strip():
            return ToolResult(
                content=(
                    "Refusing set_status: repo has uncommitted changes.\n"
                    f"git status --porcelain:\n{dirty.stdout[:2000]}\n\n"
                    f"Commit and push{self._deploy_hint()} — or discard the changes "
                    "— and then try set_status again."
                ),
                detail={"error": "dirty_repo", "status": dirty.stdout[:400]},
            )

        default_branch = self._default_branch_in_agent_repo(repo) or "main"

        branch = self._run_git_in_agent_repo(["rev-parse", "--abbrev-ref", "HEAD"], repo)
        current = (branch.stdout.strip() if branch else "") or ""
        if current and current != default_branch:
            return ToolResult(
                content=(
                    f"Refusing set_status: current branch is '{current}', not "
                    f"'{default_branch}'. Agents work directly on {default_branch}; "
                    f"`git checkout {default_branch}`, make sure there's nothing "
                    "stranded, and try again."
                ),
                detail={"error": "not_on_default_branch", "branch": current, "default_branch": default_branch},
            )

        ahead = self._run_git_in_agent_repo(
            ["rev-list", "--count", "@{u}..HEAD"], repo
        )
        try:
            n_unpushed = int((ahead.stdout.strip() if ahead else "") or "0")
        except ValueError:
            n_unpushed = 0
        if n_unpushed > 0:
            return ToolResult(
                content=(
                    f"Refusing set_status: {n_unpushed} local commit(s) on "
                    f"{default_branch} are not pushed. `git push`{self._deploy_hint()} "
                    "before calling set_status again."
                ),
                detail={"error": "unpushed", "ahead": n_unpushed},
            )

        return None


# ---------------------------------------------------------------------------
# spec helper
# ---------------------------------------------------------------------------


def _spec(name: str, description: str, params: dict, required: list[str]) -> dict:
    properties = {
        key: {"type": typ, "description": desc} for key, (typ, desc) in params.items()
    }
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }
