"""
Harnesses: the coding-agent CLIs Khidr drives.

Each harness turns a run into one headless CLI invocation (argv, env, stdin)
and translates the CLI's JSON event stream into Khidr events. All of them
reach Khidr's own tools through the HTTP MCP endpoint (core/mcp.py), so the
only thing a harness needs to know about Khidr is a URL and a bearer token.

Supported:
- ``claude``   Claude Code — subscription (``claude setup-token`` / login) or
               any Anthropic-compatible API (Anthropic, MiniMax, Qwen, …)
- ``codex``    OpenAI Codex CLI — ChatGPT subscription or an OpenAI-compatible API
- ``gemini``   Gemini CLI — Google login or API key
- ``qwen``     Qwen Code — Qwen login or an OpenAI-compatible API
- ``opencode`` opencode — any provider it is configured for
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.mcp import strip_tool_prefix
from core.models import Agent

from .events import ContextUpdate, Done, Event, TextChunk, ToolInvocation

log = logging.getLogger(__name__)

_DEFAULT_CONTEXT_LIMIT = 200_000

_LIMIT_PATTERNS = re.compile(
    r"rate[ _-]?limit|usage limit|too many requests|\b429\b|quota|limit reached|"
    r"resets? (at|in)|out of (credits|extra usage)|insufficient_quota|overloaded",
    re.IGNORECASE,
)


def is_rate_limit(text: str) -> bool:
    return bool(_LIMIT_PATTERNS.search(text or ""))


def fold_messages(messages: list[dict]) -> str:
    """The chat history as one prompt: CLIs start fresh each run and can't be
    seeded with earlier assistant turns, so those go in as a transcript."""
    if not messages:
        return "Take this card."
    *history, last = messages
    if last.get("role") == "user":
        current = last.get("content") or ""
    else:
        history.append(last)
        current = "continue"
    if not history:
        return current
    lines = ["# Earlier conversation on this card", ""]
    for m in history:
        who = "assistant" if m.get("role") == "assistant" else "user"
        lines.append(f"## {who}\n\n{(m.get('content') or '').strip()}\n")
    lines.append("---\n\n# New message\n")
    lines.append(current)
    return "\n".join(lines)


@dataclass
class RunContext:
    agent: Agent
    system_prompt: str
    prompt: str
    mcp_url: str               # as seen from the sandbox
    mcp_token: str
    workspace: str             # sandbox path
    run_dir: str               # sandbox path
    run_dir_host: Path         # where to write files the harness needs
    home: str                  # sandbox path
    max_turns: int
    api_key: str | None        # resolved value of agent.api_key_env
    base_url: str              # agent.base_url, reachable from the sandbox


@dataclass
class Invocation:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    stdin: str | None = None


def _text_of(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
            elif isinstance(block, dict):
                parts.append(f"[{block.get('type', 'content')}]")
            else:
                parts.append(str(block))
        return "\n".join(parts)
    if isinstance(content, dict):
        return _text_of(content.get("content")) or json.dumps(content, ensure_ascii=False)[:4000]
    return str(content)


def combined_prompt(ctx: RunContext) -> str:
    """For CLIs without a way to append to their system prompt."""
    return f"{ctx.system_prompt}\n\n---\n\n# Your task now\n\n{ctx.prompt}"


class Harness:
    type = ""
    binary = ""

    def __init__(self, agent: Agent):
        self.agent = agent
        self.context_limit = agent.context_length or _DEFAULT_CONTEXT_LIMIT
        self.error: str = ""
        self.finished = False

    def build(self, ctx: RunContext) -> Invocation:
        raise NotImplementedError

    def parse(self, payload: dict) -> list[Event]:
        raise NotImplementedError

    def done(self, returncode: int | None, stderr_tail: str) -> Done:
        if self.error:
            reason = "rate_limited" if is_rate_limit(self.error) else "error"
            return Done(reason=reason, message=self.error[:2000])
        if returncode not in (0, None) and not self.finished:
            msg = f"{self.binary} exited with code {returncode}. stderr: {stderr_tail[-1500:]}"
            return Done(reason="rate_limited" if is_rate_limit(stderr_tail) else "error", message=msg)
        if not self.finished:
            return Done(reason="error",
                        message=f"{self.binary} ended without a final result. stderr: {stderr_tail[-1500:]}")
        return Done(reason="finished", message="agent loop ended")


# ---------------------------------------------------------------------------
# Claude Code
# ---------------------------------------------------------------------------

# Built-ins that don't fit a headless card run (we have our own ask_user;
# WebSearch is an Anthropic server tool third-party endpoints don't run).
CLAUDE_DISALLOWED = [
    "AskUserQuestion", "EnterPlanMode", "ExitPlanMode", "EnterWorktree", "ExitWorktree",
    "WebSearch", "Agent", "Task", "Skill", "NotebookEdit", "CronCreate", "CronDelete",
    "CronList", "RemoteTrigger", "SendMessage",
]


class ClaudeHarness(Harness):
    type = Agent.TYPE_CLAUDE
    binary = "claude"

    def __init__(self, agent: Agent):
        super().__init__(agent)
        self.pending: dict[str, tuple[str, dict]] = {}

    def build(self, ctx: RunContext) -> Invocation:
        mcp = {"mcpServers": {"khidr": {
            "type": "http",
            "url": ctx.mcp_url,
            "headers": {"Authorization": f"Bearer {ctx.mcp_token}"},
        }}}
        (ctx.run_dir_host / "mcp.json").write_text(json.dumps(mcp))
        (ctx.run_dir_host / "system.md").write_text(ctx.system_prompt)

        argv = [
            "claude", "-p",
            "--output-format", "stream-json", "--verbose",
            "--permission-mode", "bypassPermissions",
            "--mcp-config", f"{ctx.run_dir}/mcp.json", "--strict-mcp-config",
            "--append-system-prompt-file", f"{ctx.run_dir}/system.md",
            "--max-turns", str(ctx.max_turns),
            "--disallowedTools", ",".join(CLAUDE_DISALLOWED),
            # Only the project's own CLAUDE.md / .claude/settings.json.
            "--setting-sources", "project",
        ]
        if self.agent.model:
            argv += ["--model", self.agent.model]

        env = {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ENABLE_TOOL_SEARCH": "false",
            "BASH_DEFAULT_TIMEOUT_MS": "300000",
            "IS_SANDBOX": "1",
        }
        if self.agent.auth == Agent.AUTH_SUBSCRIPTION:
            if ctx.api_key:
                env["CLAUDE_CODE_OAUTH_TOKEN"] = ctx.api_key
        else:
            if ctx.base_url:
                env["ANTHROPIC_BASE_URL"] = ctx.base_url
                if ctx.api_key:
                    env["ANTHROPIC_AUTH_TOKEN"] = ctx.api_key
                if self.agent.model:
                    # Third-party model ids: use them for every tier.
                    for var in ("ANTHROPIC_MODEL", "ANTHROPIC_DEFAULT_OPUS_MODEL",
                                "ANTHROPIC_DEFAULT_SONNET_MODEL", "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                                "CLAUDE_CODE_SUBAGENT_MODEL"):
                        env[var] = self.agent.model
            elif ctx.api_key:
                env["ANTHROPIC_API_KEY"] = ctx.api_key
        if self.agent.context_length:
            env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"] = str(self.agent.context_length)
        return Invocation(argv=argv, env=env, stdin=ctx.prompt)

    def parse(self, payload: dict) -> list[Event]:
        kind = payload.get("type")
        events: list[Event] = []
        if kind == "system" and payload.get("subtype") == "init":
            for server in payload.get("mcp_servers") or []:
                if server.get("name") == "khidr" and server.get("status") not in ("connected", None):
                    log.warning("claude: khidr MCP server status %s", server.get("status"))
        elif kind == "assistant":
            message = payload.get("message") or {}
            for block in message.get("content") or []:
                if block.get("type") == "text" and block.get("text"):
                    events.append(TextChunk(content=block["text"]))
                elif block.get("type") == "tool_use":
                    self.pending[block.get("id")] = (block.get("name") or "", block.get("input") or {})
            usage = message.get("usage") or {}
            used = sum(int(usage.get(k) or 0) for k in (
                "input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
            if used:
                events.append(ContextUpdate(context_used=used, context_limit=self.context_limit,
                                            tokens_per_second=0.0))
        elif kind == "user":
            content = (payload.get("message") or {}).get("content")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        name, args = self.pending.pop(block.get("tool_use_id"), ("?", {}))
                        events.append(ToolInvocation(
                            name=strip_tool_prefix(name), args=args,
                            result_content=_text_of(block.get("content")),
                            detail={"is_error": bool(block.get("is_error"))},
                        ))
        elif kind == "result":
            self.finished = True
            subtype = payload.get("subtype") or ""
            if subtype == "error_max_turns":
                self.error = ""
                self.max_turns_hit = True
            elif payload.get("is_error") or subtype not in ("success", ""):
                errors = payload.get("errors") or []
                self.error = "; ".join(map(str, errors)) or str(payload.get("result") or subtype)
            usage = payload.get("usage") or {}
            out = int(usage.get("output_tokens") or 0)
            ms = payload.get("duration_api_ms") or 0
            if out and ms:
                events.append(ContextUpdate(context_used=0, context_limit=self.context_limit,
                                            tokens_per_second=round(out / (ms / 1000), 1)))
        return events

    def done(self, returncode, stderr_tail):
        if getattr(self, "max_turns_hit", False):
            return Done(reason="max_iterations", message="the run reached its turn limit")
        return super().done(returncode, stderr_tail)


# ---------------------------------------------------------------------------
# Codex
# ---------------------------------------------------------------------------


def _toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


class CodexHarness(Harness):
    type = Agent.TYPE_CODEX
    binary = "codex"

    def build(self, ctx: RunContext) -> Invocation:
        argv = [
            "codex", "exec", "--json", "--skip-git-repo-check",
            "--dangerously-bypass-approvals-and-sandbox",
            "-C", ctx.workspace,
            "-c", f"mcp_servers.khidr.url={_toml_str(ctx.mcp_url)}",
            "-c", 'mcp_servers.khidr.bearer_token_env_var="KHIDR_MCP_TOKEN"',
        ]
        env = {"KHIDR_MCP_TOKEN": ctx.mcp_token}
        if self.agent.auth == Agent.AUTH_API_KEY:
            if ctx.base_url:
                argv += [
                    "-c", 'model_provider="khidr"',
                    "-c", 'model_providers.khidr.name="khidr"',
                    "-c", f"model_providers.khidr.base_url={_toml_str(ctx.base_url)}",
                    "-c", 'model_providers.khidr.env_key="KHIDR_AGENT_API_KEY"',
                    "-c", 'model_providers.khidr.wire_api="chat"',
                ]
                if ctx.api_key:
                    env["KHIDR_AGENT_API_KEY"] = ctx.api_key
            elif ctx.api_key:
                env["OPENAI_API_KEY"] = ctx.api_key
        if self.agent.model:
            argv += ["-m", self.agent.model]
        argv.append("-")  # prompt from stdin
        return Invocation(argv=argv, env=env, stdin=combined_prompt(ctx))

    def parse(self, payload: dict) -> list[Event]:
        kind = payload.get("type") or ""
        events: list[Event] = []
        if kind == "item.completed":
            item = payload.get("item") or {}
            itype = item.get("type") or item.get("item_type")
            if itype in ("agent_message", "assistant_message"):
                if item.get("text"):
                    events.append(TextChunk(content=item["text"]))
            elif itype == "command_execution":
                events.append(ToolInvocation(
                    name="shell", args={"command": item.get("command")},
                    result_content=str(item.get("aggregated_output") or "")[-4000:],
                    detail={"is_error": item.get("status") == "failed", "exit_code": item.get("exit_code")},
                ))
            elif itype == "file_change":
                events.append(ToolInvocation(
                    name="edit", args={"changes": item.get("changes")}, result_content="",
                    detail={"is_error": item.get("status") == "failed"},
                ))
            elif itype == "mcp_tool_call":
                result = item.get("result") or {}
                text = _text_of(result.get("content") if isinstance(result, dict) else result)
                error = item.get("error")
                events.append(ToolInvocation(
                    name=strip_tool_prefix(item.get("tool") or ""),
                    args=item.get("arguments") or {},
                    result_content=text or (str(error) if error else ""),
                    detail={"is_error": bool(error) or item.get("status") == "failed"},
                ))
            elif itype == "error" and item.get("message"):
                self.error = item["message"]
        elif kind == "turn.completed":
            self.finished = True
            usage = payload.get("usage") or {}
            used = int(usage.get("input_tokens") or 0) + int(usage.get("output_tokens") or 0)
            if used:
                events.append(ContextUpdate(context_used=used, context_limit=self.context_limit,
                                            tokens_per_second=0.0))
        elif kind == "turn.failed":
            self.finished = True
            self.error = str((payload.get("error") or {}).get("message") or payload)
        elif kind == "error":
            self.error = str(payload.get("message") or payload)
        return events


# ---------------------------------------------------------------------------
# Gemini CLI and Qwen Code (Qwen Code is a Gemini CLI fork)
# ---------------------------------------------------------------------------


class GeminiHarness(Harness):
    type = Agent.TYPE_GEMINI
    binary = "gemini"
    settings_env = "GEMINI_CLI_SYSTEM_SETTINGS_PATH"

    def __init__(self, agent: Agent):
        super().__init__(agent)
        self.tool_calls: dict[str, tuple[str, dict]] = {}
        self.text_parts: list[str] = []

    def mcp_settings(self, ctx: RunContext) -> dict:
        return {"mcpServers": {"khidr": {
            "httpUrl": ctx.mcp_url,
            "headers": {"Authorization": f"Bearer {ctx.mcp_token}"},
            "trust": True,
        }}}

    def auth_env(self, ctx: RunContext) -> dict[str, str]:
        if self.agent.auth == Agent.AUTH_API_KEY and ctx.api_key:
            return {"GEMINI_API_KEY": ctx.api_key}
        return {}

    def build(self, ctx: RunContext) -> Invocation:
        settings_file = ctx.run_dir_host / "settings.json"
        settings_file.write_text(json.dumps(self.mcp_settings(ctx)))
        argv = [self.binary, "--output-format", "stream-json", "--yolo"]
        if self.agent.model:
            argv += ["-m", self.agent.model]
        # -p forces headless mode; the prompt itself comes on stdin, which the
        # CLI puts ahead of -p (argv is too small for a long history).
        argv += ["-p", "Follow the instructions above."]
        env = {self.settings_env: f"{ctx.run_dir}/settings.json", **self.auth_env(ctx)}
        return Invocation(argv=argv, env=env, stdin=combined_prompt(ctx))

    def _flush_text(self) -> list[Event]:
        if not self.text_parts:
            return []
        text, self.text_parts = "".join(self.text_parts), []
        return [TextChunk(content=text)] if text.strip() else []

    def parse(self, payload: dict) -> list[Event]:
        kind = payload.get("type")
        # Qwen Code emits Claude Code-style stream-json.
        if kind in ("assistant", "user", "system") or (kind == "result" and "subtype" in payload):
            return self._parse_claude_style(payload)
        events: list[Event] = []
        if kind == "message" and payload.get("role") == "assistant":
            self.text_parts.append(payload.get("content") or "")
            if not payload.get("delta"):
                events += self._flush_text()
        elif kind == "tool_use":
            events += self._flush_text()
            self.tool_calls[payload.get("tool_id")] = (
                payload.get("tool_name") or "", payload.get("parameters") or {})
        elif kind == "tool_result":
            name, args = self.tool_calls.pop(payload.get("tool_id"), ("?", {}))
            output = payload.get("output")
            if output is None and isinstance(payload.get("error"), dict):
                output = payload["error"].get("message")
            events.append(ToolInvocation(
                name=strip_tool_prefix(name), args=args, result_content=_text_of(output),
                detail={"is_error": payload.get("status") == "error"},
            ))
        elif kind == "error":
            self.error = str(payload.get("message") or payload)
        elif kind == "result":
            events += self._flush_text()
            self.finished = True
            if payload.get("status") not in (None, "success"):
                err = payload.get("error") or {}
                self.error = str(err.get("message") if isinstance(err, dict) else err) or "run failed"
            stats = payload.get("stats") or {}
            used = int(stats.get("total_tokens") or stats.get("input_tokens") or 0)
            if used:
                events.append(ContextUpdate(context_used=used, context_limit=self.context_limit,
                                            tokens_per_second=0.0))
        return events

    def _parse_claude_style(self, payload: dict) -> list[Event]:
        if not hasattr(self, "_claude"):
            self._claude = ClaudeHarness(self.agent)
        events = self._claude.parse(payload)
        if payload.get("type") == "result":
            self.finished = True
            self.error = self._claude.error
        return events


class QwenHarness(GeminiHarness):
    type = Agent.TYPE_QWEN
    binary = "qwen"
    settings_env = "QWEN_CODE_SYSTEM_SETTINGS_PATH"

    def auth_env(self, ctx: RunContext) -> dict[str, str]:
        if self.agent.auth != Agent.AUTH_API_KEY:
            return {}
        env = {}
        if ctx.api_key:
            env["OPENAI_API_KEY"] = ctx.api_key
        if ctx.base_url:
            env["OPENAI_BASE_URL"] = ctx.base_url
        if self.agent.model:
            env["OPENAI_MODEL"] = self.agent.model
        return env

    def mcp_settings(self, ctx: RunContext) -> dict:
        data = super().mcp_settings(ctx)
        if self.agent.auth == Agent.AUTH_API_KEY:
            data["security"] = {"auth": {"selectedType": "openai"}}
        return data


# ---------------------------------------------------------------------------
# opencode
# ---------------------------------------------------------------------------


class OpencodeHarness(Harness):
    type = Agent.TYPE_OPENCODE
    binary = "opencode"

    def build(self, ctx: RunContext) -> Invocation:
        config: dict[str, Any] = {
            "$schema": "https://opencode.ai/config.json",
            "mcp": {"khidr": {
                "type": "remote",
                "url": ctx.mcp_url,
                "headers": {"Authorization": f"Bearer {ctx.mcp_token}"},
                "enabled": True,
            }},
            "permission": {"edit": "allow", "bash": "allow", "webfetch": "allow"},
            "autoupdate": False,
            "share": "disabled",
        }
        model = self.agent.model
        env: dict[str, str] = {}
        if self.agent.auth == Agent.AUTH_API_KEY and ctx.base_url:
            # A custom OpenAI-compatible endpoint becomes the "khidr" provider.
            name = model.split("/", 1)[-1] if model else "default"
            config["provider"] = {"khidr": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "khidr",
                "options": {"baseURL": ctx.base_url, "apiKey": "{env:KHIDR_AGENT_API_KEY}"},
                "models": {name: {"name": name}},
            }}
            model = f"khidr/{name}"
            if ctx.api_key:
                env["KHIDR_AGENT_API_KEY"] = ctx.api_key
        (ctx.run_dir_host / "opencode.json").write_text(json.dumps(config))
        env["OPENCODE_CONFIG"] = f"{ctx.run_dir}/opencode.json"
        (ctx.run_dir_host / "prompt.md").write_text(combined_prompt(ctx))
        argv = ["opencode", "run", "--format", "json", "--dangerously-skip-permissions"]
        if model:
            argv += ["-m", model]
        argv += ["--file", f"{ctx.run_dir}/prompt.md",
                 "Do the task described in the attached prompt.md."]
        return Invocation(argv=argv, env=env, stdin=None)

    def parse(self, payload: dict) -> list[Event]:
        kind = payload.get("type")
        part = payload.get("part") or {}
        events: list[Event] = []
        if kind == "text" and part.get("text"):
            events.append(TextChunk(content=part["text"]))
        elif kind == "tool_use":
            state = part.get("state") or {}
            if state.get("status") in ("completed", "error"):
                events.append(ToolInvocation(
                    name=strip_tool_prefix(part.get("tool") or ""),
                    args=state.get("input") or {},
                    result_content=_text_of(state.get("output") or state.get("error")),
                    detail={"is_error": state.get("status") == "error"},
                ))
        elif kind == "step_finish":
            self.finished = True
            tokens = part.get("tokens") or {}
            cache = tokens.get("cache") or {}
            used = int(tokens.get("input") or 0) + int(cache.get("read") or 0) + int(tokens.get("output") or 0)
            if used:
                events.append(ContextUpdate(context_used=used, context_limit=self.context_limit,
                                            tokens_per_second=0.0))
        elif kind == "error":
            err = payload.get("error") or {}
            data = err.get("data") if isinstance(err, dict) else None
            self.error = str((data or {}).get("message") or (err.get("name") if isinstance(err, dict) else err))
        return events


HARNESSES: dict[str, type[Harness]] = {
    h.type: h for h in (ClaudeHarness, CodexHarness, GeminiHarness, QwenHarness, OpencodeHarness)
}


def harness_for(agent: Agent) -> Harness:
    try:
        return HARNESSES[agent.type](agent)
    except KeyError:
        raise ValueError(f"unknown agent type {agent.type!r} (agent {agent.slug})") from None
