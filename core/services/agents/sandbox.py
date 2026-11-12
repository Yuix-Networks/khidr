"""
Where a harness process runs.

- ``docker``: a fresh container per run from one image (KHIDR_SANDBOX_IMAGE),
  removed when the run ends. It sees only the task workspace (read-write),
  the project's git cache (read-only, at its host path so ``origin`` resolves),
  the run's files (read-only) and the agent's home directory, where its CLI
  login lives. No SSH keys, no database credentials, no orchestrator code.
- ``local``: the CLI runs directly on this host with a scrubbed environment.
  No isolation — meant for development and single-user setups.

An agent is an identity (harness + login/key + model), not a machine: any
number of runs of the same agent can be live at once, up to its
``max_concurrency``.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse, urlunparse

from django.conf import settings

from core.models import Agent

log = logging.getLogger(__name__)

# Host environment a local run may inherit. Everything else — DJANGO_*,
# PG_*, API keys of other agents, the parent's CLAUDE_* session vars — stays
# out of the agent's shell.
_LOCAL_ENV_ALLOWLIST = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TMPDIR", "TERM", "TZ", "PYENV_ROOT", "NVM_DIR", "SSL_CERT_FILE",
)


@dataclass
class Paths:
    """Locations as the harness process sees them."""

    workspace: str
    run_dir: str
    home: str


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str]
    workspace: Path            # host path
    run_dir: Path              # host path
    home: Path                 # host path
    readonly_mounts: list[Path] = field(default_factory=list)
    name: str = ""


class Sandbox:
    kind = ""

    def paths(self, workspace: Path, run_dir: Path, home: Path) -> Paths:
        raise NotImplementedError

    def reachable_url(self, url: str) -> str:
        return url

    def available(self) -> tuple[bool, str]:
        return True, ""

    def popen(self, launch: Launch) -> subprocess.Popen:
        raise NotImplementedError

    def stop(self, launch: Launch) -> None:
        pass

    def agent_home(self, agent: Agent) -> Path:
        raise NotImplementedError


def _popen(argv: list[str], env: dict[str, str], cwd: Path | None) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        start_new_session=True,
    )


class LocalSandbox(Sandbox):
    kind = Agent.SANDBOX_LOCAL

    def paths(self, workspace: Path, run_dir: Path, home: Path) -> Paths:
        return Paths(workspace=str(workspace), run_dir=str(run_dir), home=str(home))

    def agent_home(self, agent: Agent) -> Path:
        # Local runs use your own home, so the CLIs find the logins you
        # already have on this machine.
        return Path(os.path.expanduser("~"))

    def popen(self, launch: Launch) -> subprocess.Popen:
        env = {k: v for k, v in os.environ.items() if k in _LOCAL_ENV_ALLOWLIST}
        extra = [str(Path("~/.local/bin").expanduser()), "/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = os.pathsep.join([env.get("PATH", "/usr/bin:/bin"), *extra])
        env.update(launch.env)
        return _popen(launch.argv, env, launch.workspace)

    def stop(self, launch: Launch) -> None:
        pass  # the runner terminates the process group itself


class DockerSandbox(Sandbox):
    kind = Agent.SANDBOX_DOCKER
    WORKSPACE = "/workspace"
    RUN_DIR = "/khidr/run"
    HOME = "/home/khidr"

    _availability: tuple[float, bool, str] = (0.0, False, "")

    def paths(self, workspace: Path, run_dir: Path, home: Path) -> Paths:
        return Paths(workspace=self.WORKSPACE, run_dir=self.RUN_DIR, home=self.HOME)

    def agent_home(self, agent: Agent) -> Path:
        home = Path(settings.KHIDR_DATA_DIR) / "agents" / agent.slug / "home"
        home.mkdir(parents=True, exist_ok=True)
        return home

    def reachable_url(self, url: str) -> str:
        """localhost on the host is host.docker.internal from a container."""
        if not url:
            return url
        p = urlparse(url)
        if p.hostname in ("127.0.0.1", "localhost", "0.0.0.0"):
            netloc = f"host.docker.internal:{p.port}" if p.port else "host.docker.internal"
            return urlunparse(p._replace(netloc=netloc))
        return url

    def available(self) -> tuple[bool, str]:
        checked_at, ok, why = DockerSandbox._availability
        if time.monotonic() - checked_at < 30:
            return ok, why
        if not shutil.which("docker"):
            result = (False, "docker is not installed")
        else:
            try:
                proc = subprocess.run(
                    ["docker", "image", "inspect", "--format", "{{.Id}}", settings.KHIDR_SANDBOX_IMAGE],
                    capture_output=True, text=True, timeout=10,
                )
                if proc.returncode == 0:
                    result = (True, "")
                elif "No such image" in proc.stderr or "No such object" in proc.stderr:
                    result = (False, f"image {settings.KHIDR_SANDBOX_IMAGE} is not built "
                                     "(docker build -t khidr-sandbox -f docker/Dockerfile.sandbox docker/)")
                else:
                    result = (False, f"docker is not reachable: {proc.stderr.strip()[:200]}")
            except Exception as exc:  # noqa: BLE001
                result = (False, f"docker check failed: {exc}")
        DockerSandbox._availability = (time.monotonic(), *result)
        return result

    def docker_argv(self, launch: Launch) -> list[str]:
        argv = [
            "docker", "run", "--rm", "-i",
            "--name", launch.name,
            "--label", "khidr.run=1",
            "--add-host", "host.docker.internal:host-gateway",
            "-v", f"{launch.workspace}:{self.WORKSPACE}",
            "-v", f"{launch.run_dir}:{self.RUN_DIR}:ro",
            "-v", f"{launch.home}:{self.HOME}",
            "-w", self.WORKSPACE,
            "-e", f"HOME={self.HOME}",
        ]
        for path in launch.readonly_mounts:
            argv += ["-v", f"{path}:{path}:ro"]
        if settings.KHIDR_SANDBOX_NETWORK:
            argv += ["--network", settings.KHIDR_SANDBOX_NETWORK]
        user = sandbox_user()
        if user:
            argv += ["--user", user]
        argv += os.environ.get("KHIDR_SANDBOX_DOCKER_ARGS", "").split()
        for key, value in launch.env.items():
            argv += ["-e", f"{key}={value}"]
        argv.append(settings.KHIDR_SANDBOX_IMAGE)
        argv += launch.argv
        return argv

    def popen(self, launch: Launch) -> subprocess.Popen:
        user = sandbox_user()
        if user and os.getuid() == 0:
            # Khidr itself runs as root (the Docker Compose setup): hand the
            # workspace and the agent's home to the sandbox user, or the agent
            # can't write files that Khidr's git operations created.
            for path in (launch.workspace, launch.home):
                subprocess.run(["chown", "-R", user, str(path)], capture_output=True, timeout=300)
        return _popen(self.docker_argv(launch), {**os.environ}, None)

    def stop(self, launch: Launch) -> None:
        subprocess.run(["docker", "rm", "-f", launch.name], capture_output=True, timeout=30)


def sandbox_user() -> str | None:
    """uid:gid the sandbox runs as, or None for the image's own user.

    KHIDR_SANDBOX_USER wins. Otherwise on Linux, a non-root Khidr runs
    sandboxes as itself so bind-mounted files stay its own; a root Khidr
    (inside Docker Compose) uses the image's `khidr` user (1000).
    """
    if settings.KHIDR_SANDBOX_USER:
        return settings.KHIDR_SANDBOX_USER
    if not sys.platform.startswith("linux"):
        return None
    if os.getuid() == 0:
        return "1000:1000"
    return f"{os.getuid()}:{os.getgid()}"


def sandbox_for(agent: Agent) -> Sandbox:
    kind = agent.effective_sandbox
    if kind == Agent.SANDBOX_LOCAL:
        return LocalSandbox()
    if kind == Agent.SANDBOX_DOCKER:
        return DockerSandbox()
    raise ValueError(f"unknown sandbox {kind!r} (agent {agent.slug})")
