"""
Claude Agent SDK worker — runs *inside* the agent's Docker container.

Spawned by `ClaudeAgentRunner` via:

    docker exec -i khidr-agent-<slug> \
        /home/khidr/.pyenv/versions/khidr/bin/python \
        /home/khidr/workspace/khidr/manage.py \
        agent_worker --task-id <N>

Running inside the container keeps every built-in tool (Bash, Read, Edit,
Glob, Grep, …) container-local by construction. Our own task tools (note,
set_status, ask_user, kanban, memory, …) live in `Toolbox` and
are served to the agent as the in-process MCP server `khidr`; they use the
Django ORM, which reaches Postgres over the LAN from the container.

The agent's model is any Anthropic Messages API-compatible endpoint
(`Agent.base_url`, e.g. https://api.minimax.io/anthropic). The SDK spawns
its bundled Claude Code CLI; we point it at that endpoint via env.

Protocol:
    stdin  — single JSON envelope:
        {"system_prompt": "...", "messages": [{"role": "user", "content": "..."}]}
    stdout — newline-delimited JSON, one event per line:
        {"event": "text", "content": "..."}
        {"event": "tool", "name": "...", "args": {...},
                          "result": "...", "is_error": false}
        {"event": "context", "context_used": N, "context_limit": N,
                             "tokens_per_second": F}
        {"event": "done", "reason": "finished|waiting_for_user|error",
                          "message": "..."}
    stderr — Python logging + CLI stderr (informational; orchestrator logs them).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from asgiref.sync import sync_to_async
from django.core.management.base import BaseCommand

log = logging.getLogger(__name__)


# Built-in Claude Code tools the agents must not use: we have our own
# ask_user (Telegram + waiting_for_user), WebSearch is an Anthropic server
# tool that third-party endpoints don't run, and the rest (plan mode,
# worktrees, subagents, scheduling, skills) don't fit a headless card run.
_DISALLOWED_TOOLS = [
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
    "EnterWorktree",
    "ExitWorktree",
    "WebSearch",
    "Agent",
    "Task",
    "Skill",
    "NotebookEdit",
    "CronCreate",
    "CronDelete",
    "CronList",
    "RemoteTrigger",
    "SendMessage",
]

# Tools Claude Code only enables by default for some Anthropic models;
# naming them here opts the session in for third-party model ids too.
_ALLOWED_TOOLS = [
    "TodoWrite",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskUpdate",
    "TaskStop",
]

# Linux caps one argv entry at ~128 KB and the CLI receives the system
# prompt on argv; past this we hand it over as a file instead.
_MAX_ARGV_PROMPT = 100_000

_DEFAULT_CONTEXT_LIMIT = 200_000
_CONTEXT_REFRESH_SECONDS = 30.0


def _emit(payload: dict[str, Any]) -> None:
    """Serialize one event to stdout and flush. ensure_ascii=False keeps
    Turkish chars readable in the daemon's log if it ever tees the output."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Pre-run setup
# ---------------------------------------------------------------------------


def _scrub_orchestrator_env() -> None:
    """
    Strip orchestrator-owned env vars from os.environ *after* Django setup
    has loaded them via dotenv. The CLI subprocess (and therefore the
    agent's Bash tool) inherits os.environ, so removing them here keeps
    PG_PASS, DJANGO_*, … out of the agent's shell.

    Django's DATABASES dict was already populated at settings-import time,
    so removing the env vars after import doesn't break ORM reconnects.
    """
    from core.services.env_scrub import _SCRUB_EXACT, _SCRUB_PREFIXES

    for key in list(os.environ.keys()):
        if key in _SCRUB_EXACT or any(key.startswith(p) for p in _SCRUB_PREFIXES):
            del os.environ[key]


def _rewrite_base_url(url: str | None) -> str | None:
    """
    Rewrite 127.0.0.1 / localhost in `url` to `khidr`, the host-gateway
    alias every agent container has via `--add-host=khidr:host-gateway`.

    Agent rows store base_url as it makes sense from the orchestrator's
    perspective (llama.cpp on the host's loopback); from inside the
    container that loopback is the container itself.
    """
    if not url:
        return url
    p = urlparse(url)
    if p.hostname in ("127.0.0.1", "localhost"):
        netloc = f"khidr:{p.port}" if p.port else "khidr"
        return urlunparse(p._replace(netloc=netloc))
    return url


def _cli_env(agent, api_key: str | None) -> dict[str, str]:
    """Env for the Claude Code CLI subprocess: endpoint, model, quiet mode."""
    env = {
        "ANTHROPIC_MODEL": agent.model,
        "ANTHROPIC_DEFAULT_OPUS_MODEL": agent.model,
        "ANTHROPIC_DEFAULT_SONNET_MODEL": agent.model,
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": agent.model,
        "CLAUDE_CODE_SUBAGENT_MODEL": agent.model,
        # No auto-update / telemetry / feature-flag traffic from containers.
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
        # Show the khidr MCP tools up front instead of behind ToolSearch —
        # third-party models handle a flat tool list more reliably.
        "ENABLE_TOOL_SEARCH": "false",
        # Long builds / crawls: give Bash the same 5-minute default as before.
        "BASH_DEFAULT_TIMEOUT_MS": "300000",
        "CLAUDE_AGENT_SDK_CLIENT_APP": "khidr",
    }
    base_url = _rewrite_base_url(agent.base_url)
    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
    if api_key:
        env["ANTHROPIC_AUTH_TOKEN"] = api_key
    if agent.context_length:
        env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(agent.context_length)
    return env


def _system_prompt(text: str) -> dict[str, Any]:
    """Claude Code's own prompt (tool usage, git hygiene) + ours appended;
    a plain file prompt when ours is too big to ride on argv."""
    if len(text.encode("utf-8")) <= _MAX_ARGV_PROMPT:
        return {"type": "preset", "preset": "claude_code", "append": text}
    fd, path = tempfile.mkstemp(prefix="khidr-system-", suffix=".md")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    return {"type": "file", "path": path}


def _build_prompt(messages: list[dict]) -> str:
    """
    Fold the chat history into one prompt. The SDK starts a fresh session
    per run and can't be seeded with prior assistant turns, so earlier
    turns go in as a transcript ahead of the latest user message.
    """
    if not messages:
        return "Bu kartı al."
    *history, last = messages
    if last.get("role") == "user":
        current = last.get("content") or ""
    else:
        history.append(last)
        current = "devam et"
    if not history:
        return current
    lines = ["# Bu karttaki önceki konuşma", ""]
    for m in history:
        who = "assistant" if m.get("role") == "assistant" else "user"
        lines.append(f"## {who}\n\n{(m.get('content') or '').strip()}\n")
    lines.append("---\n\n# Yeni mesaj\n")
    lines.append(current)
    return "\n".join(lines)


def _tool_result_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
        elif isinstance(block, dict):
            parts.append(f"[{block.get('type', 'content')}]")
    return "\n".join(parts)


def _is_rate_limit(text: str, status: int | None = None) -> bool:
    if status == 429:
        return True
    t = (text or "").lower()
    return (
        "rate_limit" in t
        or "rate limit" in t
        or "rate-limit" in t
        or ("429" in t and ("too many" in t or "rate" in t or "error code" in t))
    )


def _park_task_for_user(
    task_id: int,
    agent_slug: str | None,
    *,
    note: str = (
        "[orchestrator] agent tur limitine takıldı (MaxTurnsExceeded). "
        "Görev durduruldu ve kullanıcı müdahalesi bekleniyor. "
        "Devam etmek için chat'e bir mesaj yaz veya kartı manuel yeniden başlat."
    ),
    ping: str = "🛑 Maksimum tur aşıldı",
) -> None:
    """Park a task in waiting_for_user state (max turns / rate limit).

    Mirrors ``Session._stop_for_user``'s DB side-effects — status=todo, agent
    released, ``waiting_for_user=True`` so the dispatcher leaves the card
    alone and the user gets prompted — plus a system ChatMessage and a
    Telegram ping so the user actually sees why the run stopped.
    """
    from core.models import Agent as AgentModel
    from core.models import ChatMessage, Task
    from core.services.telegram import notify_task

    task = Task.objects.get(pk=task_id)
    Task.objects.filter(pk=task.pk).update(
        status=Task.STATUS_TODO,
        current_agent=None,
        last_heartbeat=None,
        assigned_agent=task.assigned_agent,  # keep so user reply routes back
        waiting_for_user=True,
    )
    agent_obj = AgentModel.objects.filter(slug=agent_slug).first() if agent_slug else None
    ChatMessage.objects.create(
        task=task, role=ChatMessage.ROLE_SYSTEM, agent=agent_obj, content=note,
    )
    try:
        notify_task(task, ping, body=f"Agent: {agent_slug or '?'}")
    except Exception as exc:  # noqa: BLE001
        log.warning("telegram notify (park) failed: %s", exc)
    log.warning("task %s: parked for user (agent=%s)", task_id, agent_slug)


# ---------------------------------------------------------------------------
# Agent driver
# ---------------------------------------------------------------------------


async def _run(
    task_id: int,
    system_prompt: str,
    messages: list[dict],
    max_turns: int,
    api_key: str | None,
) -> int:
    try:
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ClaudeSDKClient,
            HookMatcher,
            ResultMessage,
            TextBlock,
            ToolResultBlock,
            ToolUseBlock,
            UserMessage,
        )
    except ImportError as exc:  # pragma: no cover - depends on the container's venv
        # The container's code is newer than its installed dependencies
        # (the clone is pulled separately from the venv's packages). Say
        # so in plain words — a bare ModuleNotFoundError leaves the reader
        # guessing which of the two is stale.
        #
        # This is a deploy condition, not a bug in our code, so it must not
        # become a Sentry event: the runner reinstalls the container's
        # requirements before every run (claude_runner.ensure_container_deps)
        # and only reaches here when that didn't work. Report it as the
        # run's outcome — the session parks the task for the user with this
        # message instead of retrying the same broken setup three times.
        message = (
            "claude-agent-sdk is not installed in this agent container's Python "
            f"environment ({exc}). The container's code is newer than its "
            "dependencies — reinstall "
            "requirements.txt inside the agent containers."
        )
        log.warning("agent_worker: %s", message)
        _emit({"event": "done", "reason": "error", "message": message})
        return 1

    from core.models import Task
    from core.services.agents.mcp_tools import SERVER_NAME, TOOL_PREFIX, build_mcp_server, strip_prefix
    from core.services.tools import Toolbox

    task = await sync_to_async(
        lambda: Task.objects.select_related("project", "current_agent").get(pk=task_id)
    )()
    agent = task.current_agent
    if agent is None:
        _emit({"event": "done", "reason": "error", "message": "task has no current_agent"})
        return 1

    cwd = str(Path(task.project.repo_path or os.getcwd()).expanduser())
    context_limit = agent.context_length or _DEFAULT_CONTEXT_LIMIT
    toolbox = Toolbox(task)

    async def _stop_after_ask_user(input_data, tool_use_id, context):
        # ask_user ends the turn: the loop resumes when the user replies.
        return {"continue_": False, "stopReason": "waiting for the user's reply"}

    options = ClaudeAgentOptions(
        model=agent.model,
        system_prompt=_system_prompt(system_prompt),
        cwd=cwd,
        max_turns=max_turns,
        permission_mode="bypassPermissions",
        # The project's CLAUDE.md + .claude/settings.json; never the
        # container user's ~/.claude.
        setting_sources=["project"],
        strict_mcp_config=True,
        mcp_servers={SERVER_NAME: build_mcp_server(toolbox)},
        allowed_tools=[*_ALLOWED_TOOLS, f"{TOOL_PREFIX}*"],
        disallowed_tools=_DISALLOWED_TOOLS,
        hooks={
            "PostToolUse": [
                HookMatcher(matcher=f"{TOOL_PREFIX}ask_user", hooks=[_stop_after_ask_user])
            ]
        },
        env=_cli_env(agent, api_key),
        stderr=lambda line: log.info("cli: %s", line.rstrip()),
    )

    pending: dict[str, tuple[str, dict]] = {}
    asked_user = False
    context_used = 0
    last_context_check = 0.0
    result: ResultMessage | None = None

    async def _refresh_context(client) -> None:
        # Streamed assistant messages carry no usage, and the result's usage
        # is cumulative across turns — ask the CLI what is in the window now.
        nonlocal context_used, context_limit, last_context_check
        last_context_check = loop.time()
        try:
            usage = await client.get_context_usage()
        except Exception as exc:  # noqa: BLE001
            log.debug("get_context_usage failed: %s", exc)
            return
        context_used = int(usage.get("totalTokens") or 0)
        context_limit = int(usage.get("rawMaxTokens") or context_limit)
        _emit({
            "event": "context",
            "context_used": context_used,
            "context_limit": context_limit,
            "tokens_per_second": 0.0,
        })

    loop = asyncio.get_running_loop()

    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(_build_prompt(messages))
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, TextBlock) and block.text:
                            _emit({"event": "text", "content": block.text})
                        elif isinstance(block, ToolUseBlock):
                            pending[block.id] = (block.name, block.input or {})
                            if block.name == f"{TOOL_PREFIX}ask_user":
                                asked_user = True
                elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
                    for block in msg.content:
                        if isinstance(block, ToolResultBlock):
                            name, args = pending.pop(block.tool_use_id, ("?", {}))
                            _emit({
                                "event": "tool",
                                "name": strip_prefix(name),
                                "args": args,
                                "result": _tool_result_text(block.content),
                                "is_error": bool(block.is_error),
                            })
                    if loop.time() - last_context_check > _CONTEXT_REFRESH_SECONDS:
                        await _refresh_context(client)
                elif isinstance(msg, ResultMessage):
                    result = msg
            await _refresh_context(client)
    finally:
        if options.system_prompt.get("type") == "file":
            Path(options.system_prompt["path"]).unlink(missing_ok=True)

    if result is None:
        _emit({"event": "done", "reason": "error", "message": "CLI ended without a result"})
        return 1

    usage = result.usage or {}
    out_tokens = int(usage.get("output_tokens") or 0)
    if out_tokens and result.duration_api_ms:
        _emit({
            "event": "context",
            "context_used": context_used,
            "context_limit": context_limit,
            "tokens_per_second": round(out_tokens / (result.duration_api_ms / 1000), 1),
        })
    log.info(
        "task %s: result subtype=%s turns=%s terminal=%s cost=%s",
        task_id, result.subtype, result.num_turns, result.terminal_reason, result.total_cost_usd,
    )

    if asked_user:
        _emit({"event": "done", "reason": "waiting_for_user", "message": "ask_user"})
        return 0

    if result.subtype == "error_max_turns":
        await sync_to_async(_park_task_for_user)(task.pk, agent.slug)
        _emit({
            "event": "done",
            "reason": "waiting_for_user",
            "message": f"Maksimum {max_turns} tur aşıldı. Görevi kısa adımlara bölerek devam edin.",
        })
        return 0

    error_text = "; ".join(result.errors or []) or (result.result or "")
    failed = result.is_error or result.subtype != "success" or result.terminal_reason == "api_error"
    if not failed:
        _emit({"event": "done", "reason": "finished", "message": "agent loop ended"})
        return 0

    if _is_rate_limit(error_text, result.api_error_status):
        # The CLI already retried with backoff; park instead of re-dispatching
        # straight into the same limit.
        await sync_to_async(_park_task_for_user)(
            task.pk,
            agent.slug,
            note=(
                "[orchestrator] API rate limit (429) CLI'ın tekrar denemelerinden "
                "sonra da aşıldı. Görev park edildi — birkaç dakika bekleyip "
                "chat'e 'devam' yazın."
            ),
            ping="🛑 API rate limit aşıldı",
        )
        _emit({"event": "done", "reason": "waiting_for_user", "message": f"rate limit: {error_text[:300]}"})
        return 0

    _emit({
        "event": "done",
        "reason": "error",
        "message": f"{result.subtype} ({result.terminal_reason or '-'}): {error_text[:1000]}",
    })
    return 1


# ---------------------------------------------------------------------------
# Django management command
# ---------------------------------------------------------------------------


class Command(BaseCommand):
    help = (
        "Run a Claude Agent SDK session for one task. "
        "Reads {system_prompt, messages} JSON envelope from stdin, "
        "emits JSONL events on stdout."
    )

    def add_arguments(self, parser):
        parser.add_argument("--task-id", type=int, required=True)
        parser.add_argument("--max-turns", type=int, default=200)

    def handle(self, *, task_id, max_turns, **options):
        # Logs go to stderr so they don't pollute the stdout JSONL stream.
        logging.basicConfig(
            stream=sys.stderr,
            level=os.environ.get("AGENT_WORKER_LOG", "INFO"),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )

        envelope_raw = sys.stdin.read()
        if envelope_raw.strip():
            try:
                envelope = json.loads(envelope_raw)
            except json.JSONDecodeError as exc:
                _emit({"event": "done", "reason": "error", "message": f"invalid stdin envelope: {exc}"})
                sys.exit(1)
        else:
            envelope = {}

        # Secrets the worker needs but the agent's shell must not see: take
        # them into process memory, then scrub the environment before the
        # CLI subprocess (which inherits os.environ) is spawned.
        api_key = os.environ.pop("AGENT_API_KEY", None)
        _scrub_orchestrator_env()

        try:
            rc = asyncio.run(_run(
                task_id,
                envelope.get("system_prompt") or "",
                envelope.get("messages") or [],
                max_turns,
                api_key,
            ))
        except Exception as exc:  # noqa: BLE001
            log.exception("agent_worker: top-level failure for task %s", task_id)
            message = f"{type(exc).__name__}: {exc}"
            if _is_rate_limit(message):
                message = f"rate limit: {message}"
            _emit({"event": "done", "reason": "error", "message": message})
            rc = 1
        sys.exit(rc)
