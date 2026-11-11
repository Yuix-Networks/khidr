"""
Tests for `manage.py provision_agents` and the deploy_command wording in the
agent context. Every docker/git call is mocked: the fake below records argv
(and stdin) and answers like the real CLIs would.
"""
import subprocess
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase, override_settings

from core.management.commands.provision_agents import (
    KHIDR_DIR,
    RepoSpec,
    container_path,
    repo_script,
)
from core.models import Agent, GitRemote, Project, Task
from core.services.context import build_messages, build_system_prompt


class FakeDocker:
    """Stand-in for subprocess.run. `containers` maps name → "true"/"false"."""

    def __init__(self, containers=None, network=True, fail_exec=False, origins=None):
        self.containers = dict(containers or {})
        self.network = network
        self.fail_exec = fail_exec
        self.origins = origins or {}
        self.calls = []

    def __call__(self, argv, input=None, **kwargs):
        self.calls.append((list(argv), input))
        ok = lambda out="": subprocess.CompletedProcess(argv, 0, out, "")  # noqa: E731
        err = lambda msg="": subprocess.CompletedProcess(argv, 1, "", msg)  # noqa: E731
        if argv[:2] == ["git", "-C"]:
            origin = self.origins.get(argv[2])
            return ok(origin + "\n") if origin else err("not a git repository")
        if argv[:3] == ["docker", "network", "inspect"]:
            return ok() if self.network else err("no such network")
        if argv[:3] == ["docker", "network", "create"]:
            self.network = True
            return ok()
        if argv[:2] == ["docker", "inspect"]:
            state = self.containers.get(argv[-1])
            return ok(state + "\n") if state else err("No such object")
        if argv[:2] == ["docker", "run"]:
            self.containers[argv[argv.index("--name") + 1]] = "true"
            return ok("abc123\n")
        if argv[:2] == ["docker", "start"]:
            self.containers[argv[-1]] = "true"
            return ok()
        if argv[:2] == ["docker", "exec"]:
            if self.fail_exec and argv[-2:] == ["bash", "-s"]:
                return subprocess.CompletedProcess(argv, 1, "STEP git CLONE_FAILED\n", "denied")
            if argv[-2:] == ["bash", "-s"]:
                return ok("STEP git CLONED\nSTEP venv CREATED\nSTEP pip INSTALLED\n")
            return ok()
        raise AssertionError(f"unexpected command: {argv}")

    def argvs(self, prefix):
        return [a for a, _ in self.calls if a[: len(prefix)] == prefix]

    def scripts(self):
        return [i for a, i in self.calls if a[-2:] == ["bash", "-s"]]


@override_settings(BASE_DIR="/srv/khidr")
class ProvisionAgentsTests(TestCase):
    def setUp(self):
        Agent.objects.create(slug="alpha", type=Agent.TYPE_CLAUDE)
        Agent.objects.create(slug="beta", type=Agent.TYPE_CLAUDE)
        Agent.objects.create(slug="retired", type=Agent.TYPE_CLAUDE, is_active=False)
        self.acme = Project.objects.create(
            name="Acme", repo_path="~/workspace/acme", repo_url="git@example.com:acme.git",
            pyenv_path="~/.pyenv/versions/acme",
        )
        GitRemote.objects.create(project=self.acme, name="production", url="ssh://prod/srv/acme")
        Project.objects.create(name="Static", repo_path="/home/khidr/workspace/site",
                               repo_url="git@example.com:site.git")
        Project.objects.create(name="Old", repo_path="~/workspace/old", is_active=False)

    def run_cmd(self, fake, *args):
        out = StringIO()
        with mock.patch("core.management.commands.provision_agents.subprocess.run", fake), \
                mock.patch("core.management.commands.provision_agents.Path.mkdir"):
            call_command("provision_agents", "--khidr-repo", "git@example.com:khidr.git",
                         "--env-file", "/nonexistent/.env", *args, stdout=out)
        return out.getvalue()

    def test_creates_missing_container_and_provisions_every_repo(self):
        fake = FakeDocker(containers={"khidr-agent-beta": "true"}, network=False)
        out = self.run_cmd(fake)

        self.assertEqual(len(fake.argvs(["docker", "network", "create"])), 1)
        runs = fake.argvs(["docker", "run"])
        self.assertEqual(len(runs), 1)
        run = runs[0]
        self.assertIn("khidr-agent-alpha", run)
        self.assertIn("/home/khidr/workspace-agents/alpha:/home/khidr/workspace", run)
        self.assertIn("--add-host=khidr:host-gateway", run)
        self.assertEqual(run[-1], "khidr-agent:latest")
        self.assertFalse(any("retired" in " ".join(a) for a, _ in fake.calls))

        # khidr + Acme + Static, for each of the two agents; Old is inactive.
        scripts = fake.scripts()
        self.assertEqual(len(scripts), 6)
        self.assertIn(f"git clone -q git@example.com:khidr.git {KHIDR_DIR}", scripts[0])
        self.assertIn("pyenv virtualenv 3.14.4 khidr", scripts[0])
        self.assertIn("/home/khidr/workspace/acme", scripts[1])
        self.assertIn("pyenv virtualenv 3.12.10 acme", scripts[1])
        self.assertIn("remote add production ssh://prod/srv/acme", scripts[1])
        self.assertIn("STEP venv SKIPPED", scripts[2])
        self.assertNotIn("pip", scripts[2])
        self.assertIn("Provisioned 2 container(s) with 3 repo(s) each.", out)

    def test_stopped_container_is_started_not_recreated(self):
        fake = FakeDocker(containers={"khidr-agent-alpha": "false", "khidr-agent-beta": "true"})
        self.run_cmd(fake, "--skip-projects")
        self.assertEqual(fake.argvs(["docker", "start"]), [["docker", "start", "khidr-agent-alpha"]])
        self.assertEqual(fake.argvs(["docker", "run"]), [])
        self.assertEqual(len(fake.scripts()), 2)

    def test_no_start_skips_missing_containers(self):
        fake = FakeDocker(containers={"khidr-agent-beta": "true"})
        self.run_cmd(fake, "--no-start", "--skip-projects")
        self.assertEqual(fake.argvs(["docker", "run"]), [])
        self.assertEqual(fake.argvs(["docker", "network"]), [])
        self.assertEqual(len(fake.scripts()), 1)

    def test_filters_by_agent_and_project(self):
        fake = FakeDocker(containers={"khidr-agent-alpha": "true", "khidr-agent-beta": "true"})
        self.run_cmd(fake, "--agent", "beta", "--project", "Static")
        execs = fake.argvs(["docker", "exec"])
        self.assertTrue(all("khidr-agent-beta" in a for a in execs))
        self.assertEqual(len(fake.scripts()), 2)
        self.assertIn("/home/khidr/workspace/site", fake.scripts()[1])

    def test_unknown_agent_is_an_error(self):
        with self.assertRaisesMessage(CommandError, "unknown agent(s): ghost"):
            self.run_cmd(FakeDocker(), "--agent", "ghost")

    def test_env_file_is_copied_into_the_khidr_clone(self):
        fake = FakeDocker(containers={"khidr-agent-alpha": "true", "khidr-agent-beta": "true"})
        out = StringIO()
        with mock.patch("core.management.commands.provision_agents.subprocess.run", fake), \
                mock.patch("core.management.commands.provision_agents.Path.is_file", return_value=True), \
                mock.patch("core.management.commands.provision_agents.Path.read_text",
                           return_value="PG_HOST=khidr\n"):
            call_command("provision_agents", "--khidr-repo", "x", "--skip-projects", stdout=out)
        copies = [(a, i) for a, i in fake.calls if a[-3:-1] == ["sh", "-c"]]
        self.assertEqual(len(copies), 2)
        self.assertIn(f"cat > {KHIDR_DIR}/.env", copies[0][0][-1])
        self.assertEqual(copies[0][1], "PG_HOST=khidr\n")

    def test_khidr_url_defaults_to_host_origin(self):
        fake = FakeDocker(containers={"khidr-agent-alpha": "true", "khidr-agent-beta": "true"},
                          origins={"/srv/khidr": "git@github.com:me/khidr.git"})
        with mock.patch("core.management.commands.provision_agents.subprocess.run", fake):
            call_command("provision_agents", "--skip-projects", "--env-file", "/nonexistent",
                         stdout=StringIO())
        self.assertIn("git clone -q git@github.com:me/khidr.git", fake.scripts()[0])

    def test_project_without_url_falls_back_to_host_origin_or_is_skipped(self):
        Project.objects.filter(name="Static").update(repo_url="")
        fake = FakeDocker(containers={"khidr-agent-alpha": "true", "khidr-agent-beta": "true"})
        out = self.run_cmd(fake, "--agent", "alpha")
        self.assertIn("skip Static: no repo_url", out)
        self.assertEqual(len(fake.scripts()), 2)

    def test_repo_failure_is_reported_and_fails_the_command(self):
        fake = FakeDocker(containers={"khidr-agent-alpha": "true", "khidr-agent-beta": "true"},
                          fail_exec=True)
        with self.assertRaisesMessage(CommandError, "khidr-agent-alpha khidr"):
            self.run_cmd(fake, "--skip-projects")

    def test_dry_run_changes_nothing(self):
        fake = FakeDocker(containers={"khidr-agent-beta": "true"}, network=False)
        out = self.run_cmd(fake, "--dry-run")
        self.assertEqual(fake.argvs(["docker", "run"]), [])
        self.assertEqual(fake.argvs(["docker", "exec"]), [])
        self.assertEqual(fake.argvs(["docker", "network", "create"]), [])
        self.assertIn("$ docker run -d --name khidr-agent-alpha", out)
        self.assertIn("$ docker exec -i -u khidr khidr-agent-beta bash -s", out)


class RepoScriptTests(TestCase):
    def test_container_path(self):
        self.assertEqual(container_path("~/workspace/x"), "/home/khidr/workspace/x")
        self.assertEqual(container_path("/opt/x"), "/opt/x")
        self.assertEqual(container_path(""), "")

    def test_venv_outside_pyenv_uses_python_m_venv(self):
        script = repo_script(RepoSpec("x", "/home/khidr/workspace/x", "u", "/home/khidr/venvs/x", "3.12.10"))
        self.assertIn("/versions/3.12.10/bin/python -m venv /home/khidr/venvs/x", script)

    def test_stamp_matches_the_worker_dependency_check(self):
        from core.services.agents.claude_runner import _container_deps_script

        script = repo_script(RepoSpec("k", "/r", "u", "/v", "3.14.4"))
        worker = _container_deps_script("/r", "/v")
        for path in ("/v/.container-pip.md5", "/v/.container-pip.lock"):
            self.assertIn(path, script)
            self.assertIn(path, worker)

    def test_script_is_valid_bash(self):
        script = repo_script(RepoSpec("a", "/w/a", "git@x:a.git", "/home/khidr/.pyenv/versions/a",
                                      "3.12.10", [("prod", "ssh://p")]))
        res = subprocess.run(["bash", "-n"], input=script, text=True, capture_output=True)
        self.assertEqual(res.returncode, 0, res.stderr)


class DeployCommandContextTests(TestCase):
    def make_task(self, deploy):
        project = Project.objects.create(name=f"P{deploy!r}", repo_path="/w/p", deploy_command=deploy)
        return Task.objects.create(project=project, title="t")

    def test_new_projects_have_no_deploy_command(self):
        self.assertEqual(Project.objects.create(name="Fresh").deploy_command, "")

    def test_prompt_uses_the_projects_deploy_command(self):
        task = self.make_task("make ship")
        self.assertIn("- Deploy: make ship", build_system_prompt(task))
        self.assertIn("ship with `make ship`", build_system_prompt(task))
        self.assertIn("commit + push + `make ship`", build_messages(task)[0]["content"])

    def test_prompt_without_deploy_command(self):
        task = self.make_task("")
        prompt = build_system_prompt(task)
        self.assertIn("- Deploy: (none", prompt)
        self.assertNotIn("fab deploy", prompt)
        self.assertNotIn("`", build_messages(task)[0]["content"])
