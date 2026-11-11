"""
Read-only server inspection for agents, run by Khidr on the orchestrator host.

Agents never get SSH keys. When an agent needs to look at a server (logs,
service status, disk), it calls the `server_exec` tool; Khidr checks the
command against the project's allowlist and runs it over the host's own SSH.
"""

from __future__ import annotations

import re
import shlex
import subprocess

from core.models import Project

DEFAULT_ALLOWED = [
    "tail", "head", "cat", "less", "ls", "grep", "zgrep", "egrep", "wc", "sort", "uniq",
    "cut", "awk", "find", "stat", "file", "date", "uname", "uptime", "whoami", "id",
    "df", "du", "free", "ps", "top -bn1", "ss", "netstat", "lsof -i",
    "journalctl", "systemctl status", "systemctl is-active", "systemctl list-units",
    "supervisorctl status", "docker ps", "docker logs", "docker inspect", "docker stats --no-stream",
    "git log", "git status", "git rev-parse", "git show --stat", "nginx -t",
]

# Shell features that could chain or redirect commands. Pipes are allowed,
# but every piped segment must itself be allowlisted.
_FORBIDDEN = re.compile(r"[;&`<>\n\r]|\$\(|\|\|")
# Arguments that point at secrets.
_SECRET_PATHS = re.compile(r"(^|[\s/])(\.env\b|\.env\.|id_rsa|id_ed25519|id_ecdsa|\.ssh/|\.pgpass|\.netrc)")


class ServerExecDenied(ValueError):
    pass


def allowed_prefixes(project: Project) -> list[str]:
    custom = [line.strip() for line in (project.server_allowed_commands or "").splitlines()]
    return [c for c in custom if c and not c.startswith("#")] or DEFAULT_ALLOWED


def _segment_allowed(segment: str, prefixes: list[str]) -> bool:
    words = segment.split()
    for prefix in prefixes:
        pw = prefix.split()
        if words[: len(pw)] == pw:
            return True
    return False


def validate(command: str, project: Project) -> str:
    command = (command or "").strip()
    if not command:
        raise ServerExecDenied("empty command")
    if _FORBIDDEN.search(command):
        raise ServerExecDenied("only plain commands and `|` pipes are allowed (no ; & > < $( ) ` ||)")
    if _SECRET_PATHS.search(command):
        raise ServerExecDenied("reading secret files (.env, SSH keys, …) is not allowed")
    prefixes = allowed_prefixes(project)
    for segment in command.split("|"):
        if not _segment_allowed(segment.strip(), prefixes):
            raise ServerExecDenied(
                f"`{segment.strip()}` is not in this project's allowlist: {', '.join(prefixes)}"
            )
    return command


def run(command: str, project: Project, timeout: int = 60) -> tuple[int, str]:
    if not (project.server_connection or "").strip():
        raise ServerExecDenied("this project has no server_connection configured")
    command = validate(command, project)
    remote = command
    if project.server_path:
        remote = f"cd {shlex.quote(project.server_path)} && {command}"
    argv = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        project.server_connection.strip(), "--", remote,
    ]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    output = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr.strip() else "")
    if len(output) > 20000:
        output = output[:2000] + "\n…[truncated]…\n" + output[-18000:]
    return proc.returncode, output
