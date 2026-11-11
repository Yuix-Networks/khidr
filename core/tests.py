"""
Tests for agent_worker helpers: prompt folding, rate-limit detection and
parking a task for the user (max turns / rate limit), which previously
left tasks stuck in_progress.
"""
from unittest import mock

from django.test import SimpleTestCase, TestCase

from core.management.commands.agent_worker import (
    _build_prompt,
    _cli_env,
    _is_rate_limit,
    _park_task_for_user,
    _tool_result_text,
)
from core.services.agents.mcp_tools import strip_prefix


class BuildPromptTests(SimpleTestCase):
    def test_empty_history(self):
        self.assertEqual(_build_prompt([]), "Bu kartı al.")

    def test_single_user_message_passes_through(self):
        self.assertEqual(_build_prompt([{"role": "user", "content": "selam"}]), "selam")

    def test_history_is_folded_ahead_of_latest_message(self):
        prompt = _build_prompt([
            {"role": "user", "content": "ilk"},
            {"role": "assistant", "content": "cevap"},
            {"role": "user", "content": "son"},
        ])
        self.assertIn("## user\n\nilk", prompt)
        self.assertIn("## assistant\n\ncevap", prompt)
        self.assertTrue(prompt.endswith("son"))

    def test_trailing_assistant_turn_continues(self):
        prompt = _build_prompt([
            {"role": "user", "content": "ilk"},
            {"role": "assistant", "content": "yarım"},
        ])
        self.assertIn("## assistant\n\nyarım", prompt)
        self.assertTrue(prompt.endswith("devam et"))


class CliEnvTests(SimpleTestCase):
    def _agent(self, **kw):
        defaults = {"model": "MiniMax-M3", "base_url": "https://api.minimax.io/anthropic", "context_length": 0}
        defaults.update(kw)
        return mock.Mock(**defaults)

    def test_endpoint_model_and_token(self):
        env = _cli_env(self._agent(), "secret")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "https://api.minimax.io/anthropic")
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "secret")
        self.assertEqual(env["ANTHROPIC_MODEL"], "MiniMax-M3")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "MiniMax-M3")
        self.assertNotIn("CLAUDE_CODE_AUTO_COMPACT_WINDOW", env)

    def test_loopback_rewritten_to_host_gateway(self):
        env = _cli_env(self._agent(base_url="http://127.0.0.1:8083", context_length=256000), None)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://khidr:8083")
        self.assertNotIn("ANTHROPIC_AUTH_TOKEN", env)
        self.assertEqual(env["CLAUDE_CODE_AUTO_COMPACT_WINDOW"], "256000")


class ToolNamingTests(SimpleTestCase):
    def test_strip_prefix(self):
        self.assertEqual(strip_prefix("mcp__khidr__note"), "note")
        self.assertEqual(strip_prefix("Bash"), "Bash")

    def test_tool_result_text(self):
        self.assertEqual(_tool_result_text(None), "")
        self.assertEqual(_tool_result_text("ok"), "ok")
        self.assertEqual(
            _tool_result_text([{"type": "text", "text": "a"}, {"type": "image"}]),
            "a\n[image]",
        )


class RateLimitDetectionTests(SimpleTestCase):
    """Glitchtip #36195 — 429s must park the task, not loop re-dispatching."""

    def test_status_code(self):
        self.assertTrue(_is_rate_limit("", 429))

    def test_anthropic_payload_shape(self):
        self.assertTrue(_is_rate_limit(
            "Error code: 429 - rate_limit_error: Number of request "
            "tokens per minute exceeds the rate limit"
        ))

    def test_substrings(self):
        self.assertTrue(_is_rate_limit("HTTP 429: too many requests"))
        self.assertTrue(_is_rate_limit("rate-limit exceeded, slow down"))

    def test_ignores_other_errors(self):
        self.assertFalse(_is_rate_limit("prompt too long", 400))
        self.assertFalse(_is_rate_limit("internal server error 500"))


class PermanentErrorTests(SimpleTestCase):
    """Which session errors must not be retried — see
    AgentSession._permanent_error_reason."""

    def _reason(self, detail):
        from core.services.session import AgentSession

        return AgentSession._permanent_error_reason(detail)

    def test_auth_error_stays_an_auth_error(self):
        self.assertEqual(
            self._reason("HTTP 401: invalid api key"), "auth/permission error"
        )

    def test_missing_module_is_a_setup_error(self):
        self.assertEqual(
            self._reason("ModuleNotFoundError: No module named 'claude_agent_sdk'"),
            "agent container setup error",
        )

    def test_transient_error_is_not_permanent(self):
        self.assertIsNone(self._reason("connection reset by peer"))


class ParkTaskForUserTests(TestCase):
    """After 200 turns the worker must release the task, not just emit a
    stream event. Otherwise the card sits forever on the agent."""

    def setUp(self):
        from core.models import Agent, Project, Task

        self.proj = Project.objects.create(
            name="test-proj", slug="test-proj-park", repo_path="/tmp"
        )
        self.agent, _ = Agent.objects.get_or_create(
            slug="park-test-agent",
            defaults={
                "type": Agent.TYPE_CLAUDE,
                "role": Agent.ROLE_WORKER,
                "model": "test-model",
                "base_url": "http://x",
                "api_key_env": "",
                "context_length": 32000,
                "talent": 3,
            },
        )
        self.task = Task.objects.create(
            project=self.proj,
            title="park test",
            description="x",
            status="in_progress",
            current_agent=self.agent,
            assigned_agent=self.agent,
        )

    def test_park_releases_agent_and_sets_waiting(self):
        _park_task_for_user(self.task.pk, self.agent.slug)

        self.task.refresh_from_db()
        self.assertEqual(self.task.status, "todo")
        self.assertIsNone(self.task.current_agent)
        self.assertTrue(self.task.waiting_for_user)
        self.assertIsNone(self.task.last_heartbeat)
        # assigned_agent stays so a user reply routes back to the same model.
        self.assertEqual(self.task.assigned_agent_id, self.agent.id)

        # A system note should have been written for the UI / Telegram ping
        from core.models import ChatMessage

        notes = ChatMessage.objects.filter(task=self.task, role="system")
        self.assertEqual(notes.count(), 1)
        self.assertIn("MaxTurnsExceeded", notes.first().content)

    def test_park_tolerates_telegram_failure(self):
        # Telegram notifications can fail; the DB update must still land.
        from core.services import telegram as telegram_module

        with mock.patch.object(
            telegram_module,
            "notify_task",
            side_effect=RuntimeError("network down"),
        ):
            _park_task_for_user(self.task.pk, self.agent.slug)

        self.task.refresh_from_db()
        self.assertTrue(self.task.waiting_for_user)
