"""
Task workspaces and git, done by the orchestrator so agents need no keys.

Layout under ``settings.KHIDR_DATA_DIR``:

    git/<project>.git        bare cache of the project's remote (Khidr fetches it
                             with the project's credentials)
    workspaces/task-<id>/    the task's own clone of that cache; the sandbox
                             mounts it read-write as the agent's working dir

A workspace's ``origin`` is the local cache, and sandboxes mount the cache
read-only at the same absolute path, so ``git fetch``/``git rebase origin/…``
work inside the sandbox while ``git push`` cannot. Publishing (rebase + push
to the real remote) happens here, on the host, when the agent moves its card
to Review — see :func:`publish`.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from django.conf import settings

from core.models import Project, Task

log = logging.getLogger(__name__)


class GitError(RuntimeError):
    pass


@dataclass
class PublishResult:
    ok: bool
    message: str
    pushed: bool = False


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------


def data_dir() -> Path:
    path = Path(settings.KHIDR_DATA_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "project"


def cache_path(project: Project) -> Path:
    return data_dir() / "git" / f"{project.pk}-{_safe(project.slug or project.name)}.git"


def workspace_path(task: Task) -> Path:
    return data_dir() / "workspaces" / f"task-{task.pk}"


def task_branch(task: Task) -> str:
    return f"khidr/task-{task.pk}"


# ---------------------------------------------------------------------------
# git plumbing
# ---------------------------------------------------------------------------


def git_env(project: Project) -> dict[str, str]:
    """Environment for git commands that talk to the project's real remote."""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    if project.git_auth == Project.GIT_AUTH_TOKEN:
        token = os.environ.get(project.git_token_env or "", "")
        if not token:
            raise GitError(
                f"project {project.name!r}: git_auth=token but env var "
                f"{project.git_token_env!r} is empty on the orchestrator host"
            )
        env.update({
            "KHIDR_GIT_TOKEN": token,
            # Reset any configured helper, then answer with the token.
            "GIT_CONFIG_COUNT": "2",
            "GIT_CONFIG_KEY_0": "credential.helper",
            "GIT_CONFIG_VALUE_0": "",
            "GIT_CONFIG_KEY_1": "credential.helper",
            "GIT_CONFIG_VALUE_1": (
                '!f() { echo username=x-access-token; echo "password=$KHIDR_GIT_TOKEN"; }; f'
            ),
        })
    elif project.git_auth == Project.GIT_AUTH_SSH_KEY:
        key = str(Path(project.git_ssh_key_path or "").expanduser())
        if not project.git_ssh_key_path or not Path(key).is_file():
            raise GitError(f"project {project.name!r}: SSH key file {key!r} not found")
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i '{key}' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new"
        )
    return env


def git(args: list[str], cwd: Path | None = None, env: dict | None = None,
        timeout: int = 300, check: bool = True) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env or {**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        capture_output=True, text=True, timeout=timeout,
    )
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:3])} failed: {(proc.stderr or proc.stdout).strip()[:1500]}")
    return proc


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def update_cache(project: Project) -> Path:
    """Clone or fetch the project's bare cache from its real remote."""
    cache = cache_path(project)
    env = git_env(project)
    url = project.repo_url.strip()
    if not (cache / "HEAD").exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--bare", url, str(cache)], env=env, timeout=900)
    else:
        current = git(["remote", "get-url", "origin"], cwd=cache, check=False).stdout.strip()
        if current != url:
            git(["remote", "set-url", "origin", url], cwd=cache)
    git(["fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*"], cwd=cache, env=env, timeout=900)
    return cache


def default_branch(project: Project) -> str:
    if project.default_branch:
        return project.default_branch
    head = git(["symbolic-ref", "--short", "HEAD"], cwd=cache_path(project), check=False)
    return head.stdout.strip() or "main"


# ---------------------------------------------------------------------------
# workspace
# ---------------------------------------------------------------------------


def _is_clean(ws: Path) -> bool:
    return not git(["status", "--porcelain"], cwd=ws).stdout.strip()


def _ahead(ws: Path, base_ref: str) -> int:
    out = git(["rev-list", "--count", f"{base_ref}..HEAD"], cwd=ws, check=False).stdout.strip()
    return int(out or 0)


def prepare_workspace(task: Task, agent_slug: str = "agent") -> Path:
    """Create or refresh the task's workspace and return its path.

    A fresh workspace starts at the latest default branch (or a
    ``khidr/task-<id>`` branch off it). An existing one is fast-forwarded only
    when it holds no unpublished work — otherwise the agent's commits are left
    alone and get rebased when they are published.
    """
    project = task.project
    ws = workspace_path(task)

    if not (project.repo_url or "").strip():
        ws.mkdir(parents=True, exist_ok=True)
        return ws

    cache = update_cache(project)
    base = default_branch(project)

    if not (ws / ".git").exists():
        if ws.exists():
            shutil.rmtree(ws)
        ws.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", "--no-hardlinks", str(cache), str(ws)], timeout=900)
        git(["config", "user.name", f"{agent_slug} (Khidr agent)"], cwd=ws)
        git(["config", "user.email", f"{agent_slug}@agents.khidr.invalid"], cwd=ws)
        branch = task_branch(task) if project.publish_mode == Project.PUBLISH_TASK_BRANCH else base
        git(["checkout", "-B", branch, f"origin/{base}"], cwd=ws)
        return ws

    git(["fetch", "--prune", "origin"], cwd=ws)
    if _is_clean(ws) and _ahead(ws, f"origin/{base}") == 0:
        git(["merge", "--ff-only", f"origin/{base}"], cwd=ws, check=False)
    return ws


def remove_workspace(task: Task) -> None:
    shutil.rmtree(workspace_path(task), ignore_errors=True)


def publish(task: Task) -> PublishResult:
    """Push the agent's committed work with the orchestrator's credentials.

    ``default_branch`` mode rebases onto the latest default branch and pushes
    it; ``task_branch`` mode pushes ``khidr/task-<id>``. Refuses (ok=False)
    with a message the agent can act on when the tree is dirty or the rebase
    conflicts.
    """
    project = task.project
    ws = workspace_path(task)
    if not (project.repo_url or "").strip() or not (ws / ".git").exists():
        return PublishResult(ok=True, message="no repository to publish")

    status = git(["status", "--porcelain"], cwd=ws).stdout.strip()
    if status:
        return PublishResult(
            ok=False,
            message=(
                "Uncommitted changes in the workspace:\n"
                f"{status[:2000]}\n\nCommit them (or discard them), then call set_status again."
            ),
        )

    try:
        update_cache(project)
    except GitError as exc:
        return PublishResult(ok=False, message=f"Could not fetch the remote: {exc}")
    base = default_branch(project)
    git(["fetch", "--prune", "origin"], cwd=ws)

    if project.publish_mode == Project.PUBLISH_TASK_BRANCH:
        target = task_branch(task)
        if _ahead(ws, f"origin/{base}") == 0:
            return PublishResult(ok=True, message="no new commits to publish")
        log_lines = git(["log", "--oneline", f"origin/{base}..HEAD"], cwd=ws).stdout.strip()
        push = git(["push", "--force", project.repo_url.strip(), f"HEAD:refs/heads/{target}"],
                   cwd=ws, env=git_env(project), check=False, timeout=600)
        if push.returncode != 0:
            return PublishResult(ok=False, message=f"Push failed: {push.stderr.strip()[:1500]}")
        return PublishResult(ok=True, pushed=True,
                             message=f"Pushed branch {target}:\n{log_lines}")

    for attempt in (1, 2):
        rebase = git(["rebase", f"origin/{base}"], cwd=ws, check=False)
        if rebase.returncode != 0:
            git(["rebase", "--abort"], cwd=ws, check=False)
            return PublishResult(
                ok=False,
                message=(
                    f"Your commits conflict with the latest {base}. Run "
                    f"`git fetch origin && git rebase origin/{base}`, resolve the conflicts, "
                    "commit, and call set_status again."
                ),
            )
        if _ahead(ws, f"origin/{base}") == 0:
            return PublishResult(ok=True, message="no new commits to publish")
        log_lines = git(["log", "--oneline", f"origin/{base}..HEAD"], cwd=ws).stdout.strip()
        push = git(["push", project.repo_url.strip(), f"HEAD:refs/heads/{base}"],
                   cwd=ws, env=git_env(project), check=False, timeout=600)
        if push.returncode == 0:
            try:
                update_cache(project)
                git(["fetch", "--prune", "origin"], cwd=ws)
            except GitError as exc:  # pushed already; a stale cache is harmless
                log.warning("publish: cache refresh after push failed: %s", exc)
            return PublishResult(ok=True, pushed=True, message=f"Pushed to {base}:\n{log_lines}")
        if attempt == 1 and ("rejected" in push.stderr or "fetch first" in push.stderr):
            # Someone pushed meanwhile: refetch and rebase once more.
            update_cache(project)
            git(["fetch", "--prune", "origin"], cwd=ws)
            continue
        return PublishResult(ok=False, message=f"Push failed: {push.stderr.strip()[:1500]}")
    return PublishResult(ok=False, message="Push failed twice; try again.")
