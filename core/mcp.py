"""
Khidr's tools as an MCP server over Streamable HTTP (`POST /mcp/`).

Every harness (Claude Code, Codex, Gemini CLI, Qwen Code, opencode) speaks
MCP, so serving the orchestrator tools here — instead of inside one SDK's
process — is what lets any of them be an agent. The sandbox needs nothing
but this URL and its run token: no database, no Django, no secrets.

Auth: `Authorization: Bearer <run token>`. A token belongs to one `Run`
(one harness process on one task) and stops working when the run ends.

Only the request/response half of Streamable HTTP is implemented: each POST
carries JSON-RPC messages and gets a JSON reply. Khidr never pushes
server-initiated messages, so there is no SSE stream (GET → 405).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt

from core.models import Run
from core.services.tools import Toolbox

log = logging.getLogger(__name__)

SERVER_NAME = "khidr"
SUPPORTED_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602


def strip_tool_prefix(name: str) -> str:
    """Normalize the names harnesses report for our tools to the bare name.

    `mcp__khidr__note` (Claude), `khidr__note` / `khidr.note` (others),
    `khidr_note` → `note`. Built-in tool names pass through.
    """
    name = (name or "").strip()
    for prefix in (f"mcp__{SERVER_NAME}__", f"{SERVER_NAME}__", f"{SERVER_NAME}.", f"{SERVER_NAME}/"):
        if name.startswith(prefix):
            return name[len(prefix):]
    return name


def tool_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": spec["function"]["name"],
            "description": spec["function"]["description"],
            "inputSchema": spec["function"]["parameters"],
        }
        for spec in Toolbox.specs()
    ]


def _bearer(request: HttpRequest) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


def _error(msg_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _result(msg_id: Any, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def handle_message(run: Run, msg: Any) -> dict | None:
    """Answer one JSON-RPC message. None for notifications (no reply)."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _error(msg.get("id") if isinstance(msg, dict) else None, INVALID_REQUEST, "invalid request")

    method = msg["method"]
    msg_id = msg.get("id")
    params = msg.get("params") or {}
    if msg_id is None:  # notification (initialized, cancelled, …)
        return None

    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_VERSIONS else SUPPORTED_VERSIONS[0]
        return _result(msg_id, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": "1"},
            "instructions": (
                "Khidr orchestrator tools for the card you are working on: report "
                "progress (note), ask the user (ask_user), finish (set_status), "
                "project memory and kanban."
            ),
        })
    if method == "ping":
        return _result(msg_id, {})
    if method == "tools/list":
        return _result(msg_id, {"tools": tool_definitions()})
    if method == "tools/call":
        name = strip_tool_prefix(params.get("name") or "")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            return _error(msg_id, INVALID_PARAMS, "arguments must be an object")
        toolbox = Toolbox(run.task, run=run)
        result = toolbox.call(name, args)
        return _result(msg_id, {
            "content": [{"type": "text", "text": result.content or ""}],
            "isError": bool(result.detail.get("error")),
        })
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return _result(msg_id, {key: []})
    return _error(msg_id, METHOD_NOT_FOUND, f"method not found: {method}")


@csrf_exempt
def mcp_endpoint(request: HttpRequest) -> HttpResponse:
    if request.method != "POST":
        response = HttpResponse(status=405)
        response["Allow"] = "POST"
        return response

    run = Run.active_for_token(_bearer(request))
    if run is None:
        return JsonResponse(_error(None, INVALID_REQUEST, "unknown or finished run token"), status=401)

    try:
        payload = json.loads(request.body or b"null")
    except json.JSONDecodeError:
        return JsonResponse(_error(None, PARSE_ERROR, "parse error"), status=400)

    if isinstance(payload, list):
        replies = [r for r in (handle_message(run, m) for m in payload) if r is not None]
        if not replies:
            return HttpResponse(status=202)
        return JsonResponse(replies, safe=False)

    reply = handle_message(run, payload)
    if reply is None:
        return HttpResponse(status=202)
    return JsonResponse(reply)
