"""Host-side git: task workspaces, publishing, and the set_status gate.

Uses real git against a throw-away bare repository standing in for GitHub.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from django.test import TestCase, override_settings

from core.models import Agent, Project, Task
from core.services import workspace as ws
from core.services.tools import Toolbox

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com",
}


def sh(*args, cwd):
    return subprocess.run(args, cwd=cwd, env=GIT_ENV, check=True, capture_output=True, text=True).stdout


class WorkspaceTestBase(TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.remote = self.tmp / "remote.git"
        sh("git", "init", "-q", "--bare", "-b", "main", str(self.remote), cwd=self.tmp)
        self.seed = self.tmp / "seed"
        sh("git", "clone", "-q", str(self.remote), str(self.seed), cwd=self.tmp)
        (self.seed / "README.md").write_text("hello\n")
        sh("git", "add", "-A", cwd=self.seed)
        sh("git", "commit", "-q", "-m", "init", cwd=self.seed)
        sh("git", "push", "-q", "origin", "main", cwd=self.seed)

        override = override_settings(KHIDR_DATA_DIR=self.tmp / "data")
        override.enable()
        self.addCleanup(override.disable)

        self.project = Project.objects.create(name="p", slug="p", repo_url=str(self.remote))
        self.agent = Agent.objects.create(slug="bot", type=Agent.TYPE_CLAUDE)
        self.task = Task.objects.create(project=self.project, title="t", current_agent=self.agent)

    def remote_log(self, ref="main"):
        return sh("git", "--git-dir", str(self.remote), "log", "--format=%s", ref, cwd=self.tmp).splitlines()

    def commit_in(self, repo, name, content, msg):
        (repo / name).write_text(content)
        sh("git", "add", "-A", cwd=repo)
        sh("git", "commit", "-q", "-m", msg, cwd=repo)


class PrepareWorkspaceTests(WorkspaceTestBase):
    def test_fresh_workspace_is_a_clone_of_the_default_branch(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.assertEqual((path / "README.md").read_text(), "hello\n")
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip(), "main")
        self.assertEqual(sh("git", "config", "user.name", cwd=path).strip(), "bot (Khidr agent)")
        # origin is the local cache, not the real remote: the sandbox can fetch, not push.
        origin = sh("git", "remote", "get-url", "origin", cwd=path).strip()
        self.assertEqual(origin, str(ws.cache_path(self.project)))

    def test_clean_workspace_is_fast_forwarded(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.commit_in(self.seed, "new.txt", "x", "upstream change")
        sh("git", "push", "-q", "origin", "main", cwd=self.seed)
        ws.prepare_workspace(self.task, "bot")
        self.assertTrue((path / "new.txt").exists())

    def test_work_in_progress_is_left_alone(self):
        path = ws.prepare_workspace(self.task, "bot")
        (path / "wip.txt").write_text("draft")
        ws.prepare_workspace(self.task, "bot")
        self.assertEqual((path / "wip.txt").read_text(), "draft")

    def test_project_without_repo_gets_an_empty_dir(self):
        self.project.repo_url = ""
        self.project.save()
        path = ws.prepare_workspace(self.task, "bot")
        self.assertTrue(path.is_dir())
        self.assertFalse((path / ".git").exists())


class PublishTests(WorkspaceTestBase):
    def test_commits_are_pushed_to_the_default_branch(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.commit_in(path, "feature.txt", "x", "add feature")
        result = ws.publish(self.task)
        self.assertTrue(result.ok, result.message)
        self.assertTrue(result.pushed)
        self.assertEqual(self.remote_log(), ["add feature", "init"])

    def test_rebases_onto_upstream_changes(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.commit_in(path, "feature.txt", "x", "feature")
        self.commit_in(self.seed, "other.txt", "y", "upstream")
        sh("git", "push", "-q", "origin", "main", cwd=self.seed)
        result = ws.publish(self.task)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.remote_log()[:3], ["feature", "upstream", "init"])

    def test_conflicts_are_refused_with_instructions(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.commit_in(path, "README.md", "mine\n", "mine")
        self.commit_in(self.seed, "README.md", "theirs\n", "theirs")
        sh("git", "push", "-q", "origin", "main", cwd=self.seed)
        result = ws.publish(self.task)
        self.assertFalse(result.ok)
        self.assertIn("git rebase origin/main", result.message)
        # The failed rebase was aborted; the agent's commit is intact.
        self.assertEqual(sh("git", "log", "-1", "--format=%s", cwd=path).strip(), "mine")

    def test_dirty_workspace_is_refused(self):
        path = ws.prepare_workspace(self.task, "bot")
        (path / "loose.txt").write_text("x")
        result = ws.publish(self.task)
        self.assertFalse(result.ok)
        self.assertIn("Uncommitted", result.message)

    def test_nothing_to_publish(self):
        ws.prepare_workspace(self.task, "bot")
        result = ws.publish(self.task)
        self.assertTrue(result.ok)
        self.assertFalse(result.pushed)

    def test_task_branch_mode(self):
        self.project.publish_mode = Project.PUBLISH_TASK_BRANCH
        self.project.save()
        path = ws.prepare_workspace(self.task, "bot")
        self.assertEqual(sh("git", "rev-parse", "--abbrev-ref", "HEAD", cwd=path).strip(),
                         f"khidr/task-{self.task.pk}")
        self.commit_in(path, "f.txt", "x", "on-branch")
        result = ws.publish(self.task)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(self.remote_log(f"khidr/task-{self.task.pk}")[0], "on-branch")
        self.assertEqual(self.remote_log("main"), ["init"])

    def test_token_auth_needs_its_env_var(self):
        self.project.git_auth = Project.GIT_AUTH_TOKEN
        self.project.git_token_env = "KHIDR_TEST_MISSING_TOKEN"
        with self.assertRaises(ws.GitError):
            ws.git_env(self.project)


class SetStatusPublishesTests(WorkspaceTestBase):
    def test_review_publishes_and_moves_the_card(self):
        path = ws.prepare_workspace(self.task, "bot")
        self.commit_in(path, "feature.txt", "x", "feature")
        from unittest import mock

        with mock.patch("core.services.tools.notify_task"):
            result = Toolbox(self.task).call("set_status", {"status": "review"})
        self.assertNotIn("error", result.detail, result.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.STATUS_REVIEW)
        self.assertEqual(self.remote_log()[0], "feature")

    def test_review_is_refused_while_dirty(self):
        path = ws.prepare_workspace(self.task, "bot")
        (path / "loose.txt").write_text("x")
        result = Toolbox(self.task).call("set_status", {"status": "review"})
        self.assertEqual(result.detail.get("error"), "publish_refused")
        self.task.refresh_from_db()
        self.assertNotEqual(self.task.status, Task.STATUS_REVIEW)
