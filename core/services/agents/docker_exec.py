"""
Helpers for wrapping subprocess calls so they run inside an agent's
Docker container instead of on the khidr host itself.

Convention: each active Agent maps to a long-lived container named
`khidr-agent-<slug>`, created as described in docs/agent-containers.md. The container
mirrors the host layout — same /home/khidr/workspace, same /home/khidr/.ssh,
same /home/khidr/.pyenv/versions — so any host path the orchestrator hands
out is valid inside the container too.

These helpers are used by:

  • `ClaudeAgentRunner` — wraps `manage.py agent_worker` so the whole
    agent loop + tool execution runs inside the agent's container.
  • `Toolbox._git_in_container` — wraps the repo-clean-on-main probe so
    it reads the agent's clone, not the orchestrator's stale checkout.

There is intentionally no host fallback for the agent execution path:
the dispatcher (`_agent_container_ready`) refuses to assign work to an
agent whose container isn't running, and both runners re-check at
spawn time. If a container vanishes mid-stream, we surface the error
rather than silently re-routing to the orchestrator host.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Mapping, Sequence

from core.models import Agent

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Container discovery
# ---------------------------------------------------------------------------


def container_name_for(agent: Agent | str) -> str:
    slug = agent.slug if isinstance(agent, Agent) else agent
    return f"khidr-agent-{slug}"


def container_running(name: str) -> bool:
    """
    True if a container named `name` is currently running.
    Silent-fail on any docker CLI issue so callers can use this as a feature
    gate without needing to branch on exceptions.
    """
    if not shutil.which("docker"):
        return False
    try:
        res = subprocess.run(
            ["docker", "ps", "--filter", f"name=^{name}$", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return False
    return res.stdout.strip() == name


def agent_container_if_running(agent: Agent | str) -> str | None:
    """Return the container name if the agent's container is up, else None."""
    name = container_name_for(agent)
    return name if container_running(name) else None


# ---------------------------------------------------------------------------
# Argv wrapping
# ---------------------------------------------------------------------------


def wrap_with_docker_exec(
    inner_argv: Sequence[str],
    *,
    container: str,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    interactive: bool = True,
    user: str = "khidr",
) -> list[str]:
    """
    Build a `docker exec …` argv that, when spawned on the host, runs
    `inner_argv` inside `container`.

    - `cwd`: container-internal working directory. Host paths work because
      /home/khidr is bind-mounted identically.
    - `env`: extra environment variables passed as -e KEY=VALUE. The container
      inherits its own env for everything else (PYENV_ROOT, PATH, LANG, …).
    - `interactive`: include -i so stdin can be piped (required for Claude CLI
      stream-json input and for MCP server stdio).
    """
    argv: list[str] = ["docker", "exec"]
    if interactive:
        argv.append("-i")
    argv.extend(["-u", user])
    if cwd is not None:
        argv.extend(["-w", str(cwd)])
    if env:
        for key, val in env.items():
            argv.extend(["-e", f"{key}={val}"])
    argv.append(container)
    argv.extend(inner_argv)
    return argv


def maybe_wrap_for_agent(
    inner_argv: Sequence[str],
    agent: Agent | str,
    *,
    cwd: str | Path | None = None,
    env: Mapping[str, str] | None = None,
    interactive: bool = True,
) -> tuple[list[str], str | None]:
    """
    Convenience wrapper: if the agent's container is up, return the wrapped
    argv and the container name. Otherwise return the unwrapped argv and None
    so the caller can keep its legacy host-side spawn.
    """
    container = agent_container_if_running(agent)
    if container is None:
        return list(inner_argv), None
    wrapped = wrap_with_docker_exec(
        inner_argv,
        container=container,
        cwd=cwd,
        env=env,
        interactive=interactive,
    )
    return wrapped, container


def docker_env_args(env: Mapping[str, str] | None) -> list[str]:
    """Build `-e KEY=VALUE` flags for docker exec."""
    if not env:
        return []
    out: list[str] = []
    for k, v in env.items():
        out.extend(["-e", f"{k}={v}"])
    return out
