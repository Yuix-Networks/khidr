"""
Start and provision the per-agent Docker containers (docs/agent-containers.md).

    python manage.py provision_agents                 # every active agent + project
    python manage.py provision_agents --agent foo     # one agent
    python manage.py provision_agents --project Acme  # only this project
    python manage.py provision_agents --dry-run       # print what would run

For every active agent this:

1. starts `khidr-agent-<slug>` if it is missing (`docker run`) or stopped
   (`docker start`), creating the `khidr-agents` network first if needed;
2. clones Khidr into /home/khidr/workspace/khidr (or fast-forwards an
   existing clone), creates the `khidr` virtualenv, installs
   requirements.txt and copies the host's .env;
3. does the same for every active project: clones `repo_url` into
   `repo_path`, adds its extra git remotes and, when `pyenv_path` is set,
   creates that virtualenv and installs the project's requirements.txt.

It is idempotent: re-run it after adding an agent or a project, or after
updating Khidr. Requirements are only reinstalled when requirements.txt
changed, using the same stamp file the worker checks before every run
(core.services.agents.claude_runner.ensure_container_deps).
"""

from __future__ import annotations

import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.models import Agent, Project
from core.services.agents.docker_exec import container_name_for

HOME = "/home/khidr"
KHIDR_DIR = f"{HOME}/workspace/khidr"
KHIDR_VENV = f"{HOME}/.pyenv/versions/khidr"
PYENV_VERSIONS = f"{HOME}/.pyenv/versions"

# Must match claude_runner._container_deps_script so the worker and
# provisioning agree on when a venv is up to date.
STAMP_NAME = ".container-pip.md5"
LOCK_NAME = ".container-pip.lock"


def container_path(path: str) -> str:
    """A project path as seen inside the container (`~` → /home/khidr)."""
    path = (path or "").strip()
    if path == "~" or path.startswith("~/"):
        return HOME + path[1:]
    return path


@dataclass
class RepoSpec:
    label: str
    directory: str
    url: str
    venv: str = ""
    python_version: str = ""
    remotes: list[tuple[str, str]] = field(default_factory=list)


def repo_script(spec: RepoSpec) -> str:
    """Bash (run inside the container as khidr) that clones/updates one repo
    and syncs its virtualenv. Prints one `STEP <name> <verdict>` line per step."""
    q = shlex.quote
    d = spec.directory
    lines = [
        "set -u",
        'export PYENV_ROOT="$HOME/.pyenv"; export PATH="$PYENV_ROOT/bin:$PATH"',
        f"if [ -d {q(d + '/.git')} ]; then",
        f"  if git -C {q(d)} pull --ff-only -q; then echo 'STEP git UPDATED';"
        " else echo 'STEP git PULL_FAILED'; fi",
        "else",
        f"  mkdir -p {q(str(Path(d).parent))}",
        f"  git clone -q {q(spec.url)} {q(d)} && echo 'STEP git CLONED'"
        " || { echo 'STEP git CLONE_FAILED'; exit 1; }",
        "fi",
    ]
    for name, url in spec.remotes:
        lines.append(
            f"git -C {q(d)} remote add {q(name)} {q(url)} 2>/dev/null"
            f" || git -C {q(d)} remote set-url {q(name)} {q(url)}"
        )
    if not spec.venv:
        lines.append("echo 'STEP venv SKIPPED'")
        return "\n".join(lines) + "\n"

    venv = spec.venv.rstrip("/")
    base_python = f"$PYENV_ROOT/versions/{q(spec.python_version)}/bin/python"
    req = f"{d}/requirements.txt"
    stamp = f"{venv}/{STAMP_NAME}"
    lock = f"{venv}/{LOCK_NAME}"
    if str(Path(venv).parent) == PYENV_VERSIONS:
        create = f"pyenv virtualenv {q(spec.python_version)} {q(Path(venv).name)}"
    else:
        create = f"{base_python} -m venv {q(venv)}"
    lines += [
        f"if [ -x {q(venv + '/bin/python')} ]; then echo 'STEP venv EXISTS'; else",
        f"  {create} >/dev/null && echo 'STEP venv CREATED'"
        " || { echo 'STEP venv CREATE_FAILED'; exit 1; }",
        "fi",
        f"if [ ! -f {q(req)} ]; then echo 'STEP pip NO_REQUIREMENTS'; exit 0; fi",
        f"CUR=$(md5sum {q(req)} | awk '{{print $1}}')",
        f"if [ -f {q(stamp)} ] && grep -qx \"$CUR\" {q(stamp)}; then echo 'STEP pip IN_SYNC'; exit 0; fi",
        f"if ! mkdir {q(lock)} 2>/dev/null; then echo 'STEP pip LOCKED'; exit 1; fi",
        f"if {q(venv + '/bin/pip')} install -q -r {q(req)}; then",
        f"  echo \"$CUR\" > {q(stamp)}; echo 'STEP pip INSTALLED'; RC=0",
        "else echo 'STEP pip INSTALL_FAILED'; RC=1; fi",
        f"rmdir {q(lock)} 2>/dev/null",
        "exit $RC",
    ]
    return "\n".join(lines) + "\n"


class Command(BaseCommand):
    help = "Start missing agent containers and clone/update Khidr and every project inside them."

    def add_arguments(self, parser):
        parser.add_argument("--agent", action="append", default=[], metavar="SLUG",
                            help="Only this agent (repeatable). Default: every active agent.")
        parser.add_argument("--project", action="append", default=[], metavar="NAME",
                            help="Only this project (repeatable). Default: every active project.")
        parser.add_argument("--image", default="khidr-agent:latest")
        parser.add_argument("--network", default="khidr-agents")
        parser.add_argument("--agents-root", default=f"{HOME}/workspace-agents",
                            help="Host folder holding each agent's workspace (<root>/<slug>).")
        parser.add_argument("--ssh-dir", default=str(Path("~/.ssh").expanduser()),
                            help="Host SSH folder mounted read-only into every container.")
        parser.add_argument("--khidr-repo", default="",
                            help="Git URL to clone Khidr from. Default: origin of this checkout.")
        parser.add_argument("--khidr-python", default="3.14.4",
                            help="pyenv Python version for the khidr virtualenv.")
        parser.add_argument("--python", default="3.12.10",
                            help="pyenv Python version for project virtualenvs.")
        parser.add_argument("--env-file", default=str(Path(settings.BASE_DIR) / ".env"),
                            help="Copied into each container's Khidr clone, if it exists.")
        parser.add_argument("--no-start", action="store_true",
                            help="Don't create or start containers; skip agents whose container isn't running.")
        parser.add_argument("--skip-projects", action="store_true",
                            help="Only provision Khidr itself.")
        parser.add_argument("--dry-run", action="store_true",
                            help="Print the docker commands instead of running them.")

    # -- process helpers --------------------------------------------------

    def _run(self, argv: list[str], *, input: str | None = None, timeout: int = 1800,
             mutating: bool = True) -> subprocess.CompletedProcess:
        if self.dry_run and mutating:
            self.stdout.write("  $ " + shlex.join(argv) + ("  <<stdin" if input is not None else ""))
            return subprocess.CompletedProcess(argv, 0, "", "")
        try:
            return subprocess.run(argv, input=input, capture_output=True, text=True,
                                  timeout=timeout, check=False)
        except FileNotFoundError:
            raise CommandError(f"{argv[0]} is not installed or not on PATH")
        except subprocess.TimeoutExpired:
            return subprocess.CompletedProcess(argv, 124, "", f"timed out after {timeout}s")

    def _host_origin(self, path: str) -> str:
        if not path:
            return ""
        res = self._run(["git", "-C", str(Path(path).expanduser()), "remote", "get-url", "origin"],
                        timeout=30, mutating=False)
        return res.stdout.strip() if res.returncode == 0 else ""

    # -- main --------------------------------------------------------------

    def handle(self, *args, **opts):
        self.dry_run = opts["dry_run"]
        self.failures: list[str] = []

        agents = Agent.objects.filter(is_active=True).order_by("slug")
        if opts["agent"]:
            agents = Agent.objects.filter(slug__in=opts["agent"]).order_by("slug")
            missing = set(opts["agent"]) - set(agents.values_list("slug", flat=True))
            if missing:
                raise CommandError(f"unknown agent(s): {', '.join(sorted(missing))}")
        agents = list(agents)
        if not agents:
            raise CommandError("no active agents — load some with `manage.py load_agents` first")

        khidr_url = opts["khidr_repo"] or self._host_origin(str(settings.BASE_DIR))
        if not khidr_url:
            raise CommandError("can't tell where to clone Khidr from: pass --khidr-repo")
        specs = [RepoSpec("khidr", KHIDR_DIR, khidr_url, KHIDR_VENV, opts["khidr_python"])]
        if not opts["skip_projects"]:
            specs += self._project_specs(opts)

        env_text = None
        env_file = Path(opts["env_file"]).expanduser()
        if env_file.is_file():
            env_text = env_file.read_text()
        else:
            self.stdout.write(self.style.WARNING(f"no {env_file}; the containers' Khidr clone gets no .env"))

        if not opts["no_start"]:
            self._ensure_network(opts["network"])

        for agent in agents:
            name = container_name_for(agent)
            self.stdout.write(self.style.MIGRATE_HEADING(f"{name}"))
            if not self._ensure_container(agent.slug, name, opts):
                continue
            for spec in specs:
                self._provision_repo(name, spec)
                if spec.label == "khidr" and env_text is not None:
                    self._copy_env(name, env_text)

        if self.failures:
            raise CommandError("provisioning failed for: " + "; ".join(self.failures))
        self.stdout.write(self.style.SUCCESS(
            f"Provisioned {len(agents)} container(s) with {len(specs)} repo(s) each."
        ))

    def _project_specs(self, opts) -> list[RepoSpec]:
        if opts["project"]:
            projects = Project.objects.filter(name__in=opts["project"])
            missing = set(opts["project"]) - set(projects.values_list("name", flat=True))
            if missing:
                raise CommandError(f"unknown project(s): {', '.join(sorted(missing))}")
        else:
            projects = Project.objects.filter(is_active=True)
        specs = []
        for p in projects.order_by("name").prefetch_related("git_remotes"):
            directory = container_path(p.repo_path or "")
            if not directory:
                self.stdout.write(self.style.WARNING(f"skip {p.name}: repo_path is not set"))
                continue
            if directory.rstrip("/") == KHIDR_DIR:
                continue  # Khidr itself is always provisioned first.
            url = (p.repo_url or "").strip() or self._host_origin(p.repo_path or "")
            if not url:
                self.stdout.write(self.style.WARNING(
                    f"skip {p.name}: no repo_url and no origin in a host clone at {p.repo_path}"
                ))
                continue
            specs.append(RepoSpec(
                label=p.name,
                directory=directory,
                url=url,
                venv=container_path(p.pyenv_path or ""),
                python_version=opts["python"],
                remotes=[(r.name, r.url) for r in p.git_remotes.all() if r.name != "origin"],
            ))
        return specs

    def _ensure_network(self, network: str) -> None:
        res = self._run(["docker", "network", "inspect", network], timeout=30, mutating=False)
        if res.returncode != 0:
            res = self._run(["docker", "network", "create", network], timeout=60)
            if res.returncode != 0:
                raise CommandError(f"docker network create {network} failed: {res.stderr.strip()}")

    def _ensure_container(self, slug: str, name: str, opts) -> bool:
        res = self._run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                        timeout=30, mutating=False)
        state = res.stdout.strip() if res.returncode == 0 else "missing"
        if state == "true":
            self.stdout.write("  container running")
            return True
        if opts["no_start"]:
            self.stdout.write(self.style.WARNING(f"  container {state}; skipped (--no-start)"))
            return False
        if state == "missing":
            workspace = Path(opts["agents_root"]) / slug
            if not self.dry_run:
                workspace.mkdir(parents=True, exist_ok=True)
            argv = [
                "docker", "run", "-d", "--name", name,
                "--network", opts["network"], "--restart=unless-stopped",
                "--add-host=khidr:host-gateway",
                "-v", f"{workspace}:{HOME}/workspace",
                "-v", f"{opts['ssh_dir']}:{HOME}/.ssh:ro",
                opts["image"],
            ]
            verb = "created"
        else:
            argv = ["docker", "start", name]
            verb = "started"
        res = self._run(argv, timeout=120)
        if res.returncode != 0:
            self.stdout.write(self.style.ERROR(f"  container: {res.stderr.strip()}"))
            self.failures.append(f"{name} (container)")
            return False
        self.stdout.write(f"  container {verb}")
        return True

    def _provision_repo(self, container: str, spec: RepoSpec) -> None:
        res = self._run(
            ["docker", "exec", "-i", "-u", "khidr", container, "bash", "-s"],
            input=repo_script(spec),
        )
        steps = [
            line.split(None, 2)[1:] for line in res.stdout.splitlines()
            if line.startswith("STEP ")
        ]
        summary = ", ".join(f"{s[0]} {s[1].lower()}" for s in steps if len(s) == 2)
        if res.returncode != 0:
            self.stdout.write(self.style.ERROR(
                f"  {spec.label}: {summary or 'failed'} {res.stderr.strip()[-300:]}".rstrip()
            ))
            self.failures.append(f"{container} {spec.label}")
        elif not self.dry_run:
            self.stdout.write(f"  {spec.label}: {summary}")

    def _copy_env(self, container: str, text: str) -> None:
        target = f"{KHIDR_DIR}/.env"
        res = self._run(
            ["docker", "exec", "-i", "-u", "khidr", container, "sh", "-c",
             f"umask 077 && cat > {shlex.quote(target)}"],
            input=text, timeout=60,
        )
        if res.returncode != 0:
            self.stdout.write(self.style.ERROR(f"  .env: {res.stderr.strip()}"))
            self.failures.append(f"{container} .env")
        elif not self.dry_run:
            self.stdout.write("  .env copied")
