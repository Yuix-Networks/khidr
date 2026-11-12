"""Harness invocations and event parsing, the sandboxes, server_exec, and a
full run through HarnessRunner with a fake CLI."""

import json
import os
import shutil
import stat
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings

from core.models import Agent, ChatMessage, Project, Run, Task
from core.services import server_exec
from core.services.agents.events import ContextUpdate, Done, TextChunk, ToolInvocation
from core.services.agents.harnesses import (
    ClaudeHarness, CodexHarness, GeminiHarness, OpencodeHarness, QwenHarness, RunContext,
)
from core.services.agents.sandbox import DockerSandbox, Launch, LocalSandbox


def ctx_for(agent, run_dir, **kw):
    defaults = dict(
        agent=agent, system_prompt="SYSTEM", prompt="PROMPT",
        mcp_url="http://host.docker.internal:8000/mcp/", mcp_token="tok",
        workspace="/workspace", run_dir="/khidr/run", run_dir_host=run_dir, home="/home/khidr",
        max_turns=50, api_key=None, base_url="",
    )
    defaults.update(kw)
    return RunContext(**defaults)


class HarnessBuildTests(SimpleTestCase):
    def setUp(self):
        self.run_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.run_dir, ignore_errors=True)

    def test_claude_subscription(self):
        agent = Agent(slug="c", type="claude", auth="subscription", model="sonnet")
        inv = ClaudeHarness(agent).build(ctx_for(agent, self.run_dir, api_key="oauth-tok"))
        self.assertEqual(inv.argv[:2], ["claude", "-p"])
        self.assertIn("--strict-mcp-config", inv.argv)
        self.assertEqual(inv.env["CLAUDE_CODE_OAUTH_TOKEN"], "oauth-tok")
        self.assertNotIn("ANTHROPIC_API_KEY", inv.env)
        self.assertEqual(inv.stdin, "PROMPT")
        mcp = json.loads((self.run_dir / "mcp.json").read_text())["mcpServers"]["khidr"]
        self.assertEqual(mcp["type"], "http")
        self.assertEqual(mcp["headers"]["Authorization"], "Bearer tok")
        self.assertEqual((self.run_dir / "system.md").read_text(), "SYSTEM")

    def test_claude_third_party_api(self):
        agent = Agent(slug="m", type="claude", auth="api_key", model="MiniMax-M2")
        inv = ClaudeHarness(agent).build(ctx_for(
            agent, self.run_dir, api_key="k", base_url="https://api.minimax.io/anthropic"))
        self.assertEqual(inv.env["ANTHROPIC_BASE_URL"], "https://api.minimax.io/anthropic")
        self.assertEqual(inv.env["ANTHROPIC_AUTH_TOKEN"], "k")
        self.assertEqual(inv.env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "MiniMax-M2")

    def test_codex(self):
        agent = Agent(slug="x", type="codex", auth="subscription", model="gpt-5")
        inv = CodexHarness(agent).build(ctx_for(agent, self.run_dir))
        self.assertEqual(inv.argv[:3], ["codex", "exec", "--json"])
        self.assertIn('mcp_servers.khidr.url="http://host.docker.internal:8000/mcp/"', inv.argv)
        self.assertEqual(inv.env["KHIDR_MCP_TOKEN"], "tok")
        self.assertIn("SYSTEM", inv.stdin)
        self.assertEqual(inv.argv[-1], "-")

    def test_codex_compatible_api(self):
        agent = Agent(slug="x", type="codex", auth="api_key", model="qwen3-coder")
        inv = CodexHarness(agent).build(ctx_for(agent, self.run_dir, api_key="k",
                                                base_url="https://example.com/v1"))
        self.assertIn('model_provider="khidr"', inv.argv)
        self.assertEqual(inv.env["KHIDR_AGENT_API_KEY"], "k")

    def test_gemini_and_qwen_settings(self):
        for cls, env_var in ((GeminiHarness, "GEMINI_CLI_SYSTEM_SETTINGS_PATH"),
                             (QwenHarness, "QWEN_CODE_SYSTEM_SETTINGS_PATH")):
            agent = Agent(slug="g", type=cls.type, auth="subscription")
            inv = cls(agent).build(ctx_for(agent, self.run_dir))
            self.assertEqual(inv.env[env_var], "/khidr/run/settings.json")
            settings = json.loads((self.run_dir / "settings.json").read_text())
            self.assertEqual(settings["mcpServers"]["khidr"]["httpUrl"], "http://host.docker.internal:8000/mcp/")
            self.assertIn("--yolo", inv.argv)

    def test_opencode_custom_endpoint(self):
        agent = Agent(slug="o", type="opencode", auth="api_key", model="qwen3-coder")
        inv = OpencodeHarness(agent).build(ctx_for(agent, self.run_dir, api_key="k",
                                                   base_url="http://host.docker.internal:8080/v1"))
        config = json.loads((self.run_dir / "opencode.json").read_text())
        self.assertEqual(config["mcp"]["khidr"]["type"], "remote")
        self.assertEqual(config["provider"]["khidr"]["options"]["baseURL"], "http://host.docker.internal:8080/v1")
        self.assertIn("khidr/qwen3-coder", inv.argv)
        self.assertEqual(inv.env["OPENCODE_CONFIG"], "/khidr/run/opencode.json")


class HarnessParseTests(SimpleTestCase):
    def test_claude_stream(self):
        h = ClaudeHarness(Agent(slug="c", type="claude"))
        events = []
        for payload in [
            {"type": "system", "subtype": "init", "mcp_servers": [{"name": "khidr", "status": "connected"}]},
            {"type": "assistant", "message": {"content": [
                {"type": "text", "text": "Working on it"},
                {"type": "tool_use", "id": "t1", "name": "mcp__khidr__note", "input": {"text": "hi"}},
            ], "usage": {"input_tokens": 10, "cache_read_input_tokens": 990}}},
            {"type": "user", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": [{"type": "text", "text": "note posted"}]},
            ]}},
            {"type": "result", "subtype": "success", "is_error": False, "usage": {"output_tokens": 100},
             "duration_api_ms": 2000},
        ]:
            events += h.parse(payload)
        self.assertIsInstance(events[0], TextChunk)
        self.assertEqual(events[1].context_used, 1000)
        tool = [e for e in events if isinstance(e, ToolInvocation)][0]
        self.assertEqual((tool.name, tool.result_content), ("note", "note posted"))
        self.assertEqual(h.done(0, "").reason, "finished")

    def test_claude_usage_limit(self):
        h = ClaudeHarness(Agent(slug="c", type="claude"))
        h.parse({"type": "result", "subtype": "success", "is_error": True,
                 "result": "Claude AI usage limit reached|1762900000"})
        self.assertEqual(h.done(1, "").reason, "rate_limited")

    def test_claude_max_turns(self):
        h = ClaudeHarness(Agent(slug="c", type="claude"))
        h.parse({"type": "result", "subtype": "error_max_turns", "is_error": True})
        self.assertEqual(h.done(1, "").reason, "max_iterations")

    def test_codex_stream(self):
        h = CodexHarness(Agent(slug="x", type="codex"))
        events = []
        for payload in [
            {"type": "thread.started", "thread_id": "1"},
            {"type": "item.completed", "item": {"id": "0", "type": "agent_message", "text": "Plan: …"}},
            {"type": "item.completed", "item": {"id": "1", "type": "command_execution", "command": "ls",
                                                "aggregated_output": "a.py\n", "exit_code": 0, "status": "completed"}},
            {"type": "item.completed", "item": {"id": "2", "type": "mcp_tool_call", "server": "khidr",
                                                "tool": "set_status", "arguments": {"status": "review"},
                                                "result": {"content": [{"type": "text", "text": "status set"}]},
                                                "status": "completed"}},
            {"type": "turn.completed", "usage": {"input_tokens": 500, "output_tokens": 20}},
        ]:
            events += h.parse(payload)
        names = [e.name for e in events if isinstance(e, ToolInvocation)]
        self.assertEqual(names, ["shell", "set_status"])
        self.assertEqual(h.done(0, "").reason, "finished")

    def test_codex_failure(self):
        h = CodexHarness(Agent(slug="x", type="codex"))
        h.parse({"type": "turn.failed", "error": {"message": "You've hit your usage limit."}})
        self.assertEqual(h.done(1, "").reason, "rate_limited")

    def test_gemini_stream(self):
        h = GeminiHarness(Agent(slug="g", type="gemini"))
        events = []
        for payload in [
            {"type": "init", "session_id": "s"},
            {"type": "message", "role": "assistant", "content": "Hel", "delta": True},
            {"type": "message", "role": "assistant", "content": "lo", "delta": True},
            {"type": "tool_use", "tool_name": "khidr__note", "tool_id": "1", "parameters": {"text": "x"}},
            {"type": "tool_result", "tool_id": "1", "status": "success", "output": "note posted"},
            {"type": "result", "status": "success", "stats": {"total_tokens": 1234}},
        ]:
            events += h.parse(payload)
        self.assertEqual(events[0].content, "Hello")
        self.assertEqual(events[1].name, "note")
        self.assertIsInstance(events[-1], ContextUpdate)
        self.assertEqual(h.done(0, "").reason, "finished")

    def test_qwen_claude_style_stream(self):
        h = QwenHarness(Agent(slug="q", type="qwen"))
        events = h.parse({"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}})
        events += h.parse({"type": "result", "subtype": "success", "is_error": False})
        self.assertEqual(events[0].content, "hi")
        self.assertEqual(h.done(0, "").reason, "finished")

    def test_opencode_stream(self):
        h = OpencodeHarness(Agent(slug="o", type="opencode"))
        events = []
        for payload in [
            {"type": "text", "part": {"text": "Done."}},
            {"type": "tool_use", "part": {"tool": "khidr_note", "state": {
                "status": "completed", "input": {"text": "x"}, "output": "note posted"}}},
            {"type": "step_finish", "part": {"tokens": {"input": 100, "output": 5, "cache": {"read": 50}}}},
        ]:
            events += h.parse(payload)
        self.assertEqual(events[0].content, "Done.")
        self.assertEqual(events[2].context_used, 155)
        self.assertEqual(h.done(0, "").reason, "finished")

    def test_exit_without_result_is_an_error(self):
        h = ClaudeHarness(Agent(slug="c", type="claude"))
        done = h.done(127, "claude: command not found")
        self.assertEqual(done.reason, "error")
        self.assertIn("command not found", done.message)


class SandboxTests(SimpleTestCase):
    def test_docker_mounts_only_what_the_run_needs(self):
        launch = Launch(
            argv=["claude", "-p"], env={"KEY": "v"},
            workspace=Path("/data/workspaces/task-1"), run_dir=Path("/data/runs/9"),
            home=Path("/data/agents/a/home"), readonly_mounts=[Path("/data/git/1-p.git")],
            name="khidr-run-9",
        )
        argv = DockerSandbox().docker_argv(launch)
        joined = " ".join(argv)
        self.assertIn("-v /data/workspaces/task-1:/workspace", joined)
        self.assertIn("-v /data/runs/9:/khidr/run:ro", joined)
        self.assertIn("-v /data/git/1-p.git:/data/git/1-p.git:ro", joined)
        self.assertIn("--rm", argv)
        self.assertNotIn(".ssh", joined)
        self.assertEqual(argv[-2:], ["claude", "-p"])

    @override_settings(KHIDR_SANDBOX_NETWORK="khidr", KHIDR_SANDBOX_USER="1000:1000")
    def test_compose_network_and_user(self):
        launch = Launch(argv=["codex"], env={}, workspace=Path("/w"), run_dir=Path("/r"),
                        home=Path("/h"), name="khidr-run-1")
        argv = DockerSandbox().docker_argv(launch)
        self.assertEqual(argv[argv.index("--network") + 1], "khidr")
        self.assertEqual(argv[argv.index("--user") + 1], "1000:1000")

    def test_sandbox_user_defaults(self):
        from core.services.agents import sandbox

        with mock.patch.object(sandbox.sys, "platform", "darwin"):
            self.assertIsNone(sandbox.sandbox_user())
        with mock.patch.object(sandbox.sys, "platform", "linux"), \
                mock.patch.object(sandbox.os, "getuid", return_value=0):
            self.assertEqual(sandbox.sandbox_user(), "1000:1000")
        with mock.patch.object(sandbox.sys, "platform", "linux"), \
                mock.patch.object(sandbox.os, "getuid", return_value=1001), \
                mock.patch.object(sandbox.os, "getgid", return_value=1001):
            self.assertEqual(sandbox.sandbox_user(), "1001:1001")

    def test_docker_reaches_the_host_via_host_docker_internal(self):
        sb = DockerSandbox()
        self.assertEqual(sb.reachable_url("http://127.0.0.1:8000/mcp/"), "http://host.docker.internal:8000/mcp/")
        self.assertEqual(sb.reachable_url("https://api.example.com/v1"), "https://api.example.com/v1")

    def test_local_env_is_scrubbed(self):
        captured = {}

        def fake_popen(argv, env, cwd):
            captured.update(env)

        with mock.patch.dict(os.environ, {"PG_PASS": "secret", "CLAUDECODE": "1", "HOME": "/h"}), \
                mock.patch("core.services.agents.sandbox._popen", side_effect=fake_popen):
            LocalSandbox().popen(Launch(argv=["x"], env={"A": "1"}, workspace=Path("/w"),
                                        run_dir=Path("/r"), home=Path("/h")))
        self.assertNotIn("PG_PASS", captured)
        self.assertNotIn("CLAUDECODE", captured)
        self.assertEqual(captured["A"], "1")
        self.assertEqual(captured["HOME"], "/h")


class ServerExecTests(SimpleTestCase):
    def setUp(self):
        self.project = Project(name="p", server_connection="deploy@srv", server_path="/srv/app")

    def test_allowed(self):
        for cmd in ("tail -n 100 /var/log/app.log", "systemctl status app",
                    "journalctl -u app | grep ERROR | tail -n 20", "docker ps"):
            self.assertEqual(server_exec.validate(cmd, self.project), cmd)

    def test_denied(self):
        for cmd in ("rm -rf /", "tail log; rm x", "cat a > b", "ls && reboot", "echo $(id)",
                    "systemctl restart app", "cat /srv/app/.env", "cat ~/.ssh/id_ed25519",
                    "grep x f | sh"):
            with self.assertRaises(server_exec.ServerExecDenied, msg=cmd):
                server_exec.validate(cmd, self.project)

    def test_custom_allowlist(self):
        self.project.server_allowed_commands = "redis-cli info\n# comment\n"
        server_exec.validate("redis-cli info memory", self.project)
        with self.assertRaises(server_exec.ServerExecDenied):
            server_exec.validate("tail log", self.project)

    def test_runs_over_ssh_in_the_project_dir(self):
        with mock.patch("core.services.server_exec.subprocess.run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="ok", stderr="")
            code, out = server_exec.run("ls", self.project)
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ["ssh", "-o"])
        self.assertEqual(argv[-1], "cd /srv/app && ls")
        self.assertEqual((code, out), (0, "ok"))


FAKE_CLAUDE = r'''#!{python}
import json, sys
prompt = sys.stdin.read()
def out(obj):
    print(json.dumps(obj), flush=True)
out({{"type": "system", "subtype": "init", "mcp_servers": [{{"name": "khidr", "status": "connected"}}]}})
out({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "got: " + prompt[:20]}}]}}}})
if "ASK" in prompt:
    out({{"type": "assistant", "message": {{"content": [{{"type": "tool_use", "id": "1",
        "name": "mcp__khidr__ask_user", "input": {{"message": "which?"}}}}]}}}})
    out({{"type": "user", "message": {{"content": [{{"type": "tool_result", "tool_use_id": "1",
        "content": "sent"}}]}}}})
    import time; time.sleep(30)  # the runner must stop us
out({{"type": "result", "subtype": "success", "is_error": False}})
'''


class HarnessRunnerTests(TestCase):
    """A full run on the local sandbox with a fake `claude` binary."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        bin_dir = self.tmp / "bin"
        bin_dir.mkdir()
        fake = bin_dir / "claude"
        fake.write_text(FAKE_CLAUDE.format(python=sys.executable))
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        patcher = mock.patch.dict(os.environ, {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})
        patcher.start()
        self.addCleanup(patcher.stop)
        override = override_settings(KHIDR_DATA_DIR=self.tmp / "data")
        override.enable()
        self.addCleanup(override.disable)

        self.project = Project.objects.create(name="p", slug="p")
        self.agent = Agent.objects.create(slug="fake", type="claude", sandbox="local", auth="subscription")
        self.task = Task.objects.create(project=self.project, title="t", current_agent=self.agent,
                                        status=Task.STATUS_IN_PROGRESS)

    def run_events(self, prompt):
        from core.services.agents.runner import HarnessRunner

        runner = HarnessRunner(self.agent, self.task, "SYS", [{"role": "user", "content": prompt}])
        return list(runner.run())

    def test_successful_run(self):
        events = self.run_events("hello there")
        texts = [e.content for e in events if isinstance(e, TextChunk)]
        self.assertEqual(texts, ["got: hello there"])
        self.assertEqual(events[-1], Done(reason="finished", message="agent loop ended"))
        run = Run.objects.get()
        self.assertEqual(run.end_reason, "finished")
        self.assertIsNotNone(run.ended_at)
        self.assertFalse((self.tmp / "data" / "runs" / str(run.pk)).exists())

    def test_ask_user_stops_the_run(self):
        import time

        started = time.monotonic()
        events = self.run_events("please ASK me")
        self.assertLess(time.monotonic() - started, 20)
        self.assertEqual(events[-1].reason, "waiting_for_user")

    def test_missing_binary_is_a_setup_error(self):
        self.agent.type = "codex"
        self.agent.save()
        with mock.patch.dict(os.environ, {"PATH": "/nonexistent"}), \
                mock.patch("core.services.agents.sandbox.LocalSandbox.popen",
                           side_effect=FileNotFoundError("codex")):
            events = self.run_events("x")
        self.assertEqual(events[-1].reason, "error")
        self.assertIn("could not start", events[-1].message)
