"""
Expose `Toolbox` as an in-process MCP server for the Claude Agent SDK.

Every spec in `Toolbox.specs()` becomes one MCP tool whose handler calls
`Toolbox.call(name, args)` in a worker thread (the tools hit the Django ORM,
which must not run on the event loop). The model sees the tools as
`mcp__khidr__<name>`.
"""

from __future__ import annotations

from typing import Any

from asgiref.sync import sync_to_async
from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server, tool

from core.services.tools import Toolbox

SERVER_NAME = "khidr"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"


def strip_prefix(name: str) -> str:
    """`mcp__khidr__note` → `note`; built-in tool names pass through."""
    return name[len(TOOL_PREFIX):] if name.startswith(TOOL_PREFIX) else name


def _make_tool(toolbox: Toolbox, spec: dict) -> SdkMcpTool[Any]:
    fn = spec["function"]
    name = fn["name"]

    async def handler(args: dict[str, Any]) -> dict[str, Any]:
        result = await sync_to_async(toolbox.call, thread_sensitive=True)(name, args or {})
        out: dict[str, Any] = {"content": [{"type": "text", "text": result.content or ""}]}
        if result.detail.get("error"):
            out["is_error"] = True
        return out

    return tool(name, fn["description"], fn["parameters"])(handler)


def build_mcp_server(toolbox: Toolbox):
    """Return the `mcp_servers` entry for ClaudeAgentOptions."""
    return create_sdk_mcp_server(
        name=SERVER_NAME,
        tools=[_make_tool(toolbox, spec) for spec in Toolbox.specs()],
    )
