"""Tests for session outcomes, prompt folding, limit detection and dispatch."""

from datetime import timedelta
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from core.mcp import strip_tool_prefix
from core.models import Agent, Project, Task
from core.services.agents.events import Done
from core.services.agents.harnesses import fold_messages, is_rate_limit


class FoldMessagesTests(SimpleTestCase):
    def test_empty_history(self):
        self.assertEqual(fold_messages([]), "Take this card.")

    def test_single_user_message_passes_through(self):
        self.assertEqual(fold_messages([{"role": "user", "content": "hello"}]), "hello")

    def test_history_is_folded_ahead_of_latest_message(self):
        prompt = fold_messages([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "answer"},
            {"role": "user", "content": "last"},
        ])
        self.assertIn("## user\n\nfirst", prompt)
        self.assertIn("## assistant\n\nanswer", prompt)
        self.assertTrue(prompt.endswith("last"))

    def test_trailing_assistant_turn_continues(self):
        prompt = fold_messages([
            {"role": "user", "content": "first"},
            {"role": "assistant", "content": "half-done"},
        ])
        self.assertTrue(prompt.endswith("continue"))


class ToolNamingTests(SimpleTestCase):
    def test_strip_prefix(self):
        self.assertEqual(strip_tool_prefix("mcp__khidr__note"), "note")
        self.assertEqual(strip_tool_prefix("khidr__ask_user"), "ask_user")
        self.assertEqual(strip_tool_prefix("khidr.set_status"), "set_status")
        self.assertEqual(strip_tool_prefix("Bash"), "Bash")


class RateLimitDetectionTests(SimpleTestCase):
    def test_api_rate_limits(self):
        self.assertTrue(is_rate_limit("Error code: 429 - rate_limit_error"))
        self.assertTrue(is_rate_limit("HTTP 429: too many requests"))

    def test_subscription_windows(self):
        self.assertTrue(is_rate_limit("Claude AI usage limit reached|1762900000"))
        self.assertTrue(is_rate_limit("You've hit your usage limit. Try again at 5:00 PM."))
        self.assertTrue(is_rate_limit("Quota exceeded for quota metric"))

    def test_ignores_other_errors(self):
        self.assertFalse(is_rate_limit("prompt too long"))
        self.assertFalse(is_rate_limit("internal server error 500"))


class PermanentErrorTests(SimpleTestCase):
    def _reason(self, detail):
        from core.services.session import AgentSession

        return AgentSession._permanent_error_reason(detail)

    def test_auth_error(self):
        self.assertEqual(self._reason("HTTP 401: invalid api key"), "auth/permission error")

    def test_setup_errors(self):
        self.assertEqual(self._reason("sandbox unavailable: image not built"), "agent setup error")
        self.assertEqual(self._reason("could not start codex: No such file"), "agent setup error")

    def test_out_of_credit(self):
        self.assertEqual(self._reason("API Error: 402 insufficient balance (1008)"), "out of credit")

    def test_transient_error_is_not_permanent(self):
        self.assertIsNone(self._reason("connection reset by peer"))


def _agent(slug="a1", **kw):
    defaults = {"type": Agent.TYPE_CLAUDE, "role": Agent.ROLE_WORKER, "talent": 3}
    defaults.update(kw)
    return Agent.objects.create(slug=slug, **defaults)


class SessionOutcomeTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="p", slug="p")
        self.agent = _agent()
        self.task = Task.objects.create(
            project=self.project, title="t", status=Task.STATUS_IN_PROGRESS,
            current_agent=self.agent,
        )

    def _finalize(self, done):
        from core.services.session import AgentSession

        session = AgentSession(self.task.pk)
        session.agent = self.agent
        session._agent_id = self.agent.pk
        session._finalize(self.task, done)
        self.task.refresh_from_db()
        self.agent.refresh_from_db()

    @override_settings(KHIDR_RATE_LIMIT_COOLDOWN_SECONDS=600)
    def test_rate_limit_rests_the_agent_not_the_task(self):
        self._finalize(Done(reason="rate_limited", message="usage limit reached"))
        self.assertEqual(self.task.status, Task.STATUS_TODO)
        self.assertIsNone(self.task.current_agent)
        self.assertFalse(self.task.waiting_for_user)
        self.assertGreater(self.agent.cooldown_until, timezone.now() + timedelta(seconds=500))

    def test_max_turns_requeues_for_the_same_agent(self):
        self._finalize(Done(reason="max_iterations", message="turn limit"))
        self.assertEqual(self.task.status, Task.STATUS_TODO)
        self.assertEqual(self.task.assigned_agent, self.agent)
        self.assertIsNotNone(self.task.next_dispatch_after)

    def test_ask_user_parks_the_task(self):
        self._finalize(Done(reason="waiting_for_user", message="ask_user"))
        self.assertTrue(self.task.waiting_for_user)
        self.assertEqual(self.task.assigned_agent, self.agent)


@mock.patch("core.services.dispatcher._agent_sandbox_ready", return_value=True)
class DispatchConcurrencyTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="p", slug="p")

    def _todo(self, n):
        return [Task.objects.create(project=self.project, title=f"t{i}", position=i) for i in range(n)]

    def test_agent_takes_up_to_max_concurrency_tasks(self, _ready):
        from core.services.dispatcher import dispatch_for_project

        agent = _agent(max_concurrency=2)
        agent.projects.add(self.project)
        self._todo(3)
        self.assertIsNotNone(dispatch_for_project(self.project))
        self.assertIsNotNone(dispatch_for_project(self.project))
        self.assertIsNone(dispatch_for_project(self.project))
        self.assertEqual(Task.objects.filter(current_agent=agent).count(), 2)

    def test_cooling_down_agent_is_skipped(self, _ready):
        from core.services.dispatcher import dispatch_for_project

        resting = _agent("resting", cooldown_until=timezone.now() + timedelta(hours=1), priority=1)
        fresh = _agent("fresh", priority=2)
        resting.projects.add(self.project)
        fresh.projects.add(self.project)
        self._todo(1)
        task = dispatch_for_project(self.project)
        self.assertEqual(task.current_agent, fresh)

    def test_pinned_task_waits_for_its_resting_agent(self, _ready):
        from core.services.dispatcher import dispatch_for_project

        resting = _agent("resting", cooldown_until=timezone.now() + timedelta(hours=1))
        resting.projects.add(self.project)
        task = self._todo(1)[0]
        Task.objects.filter(pk=task.pk).update(assigned_agent=resting)
        self.assertIsNone(dispatch_for_project(self.project))


class AskUserToolTests(TestCase):
    def test_ask_user_marks_the_run(self):
        from core.models import Run
        from core.services.tools import Toolbox

        project = Project.objects.create(name="p", slug="p")
        agent = _agent()
        task = Task.objects.create(project=project, title="t", current_agent=agent)
        run, _ = Run.start(task, agent)
        with mock.patch("core.services.tools.notify_task"):
            Toolbox(task, run=run).call("ask_user", {"message": "which db?"})
        run.refresh_from_db()
        task.refresh_from_db()
        self.assertTrue(run.asked_user)
        self.assertTrue(task.waiting_for_user)


class SessionOwnershipTests(TestCase):
    def test_finishing_the_task_does_not_lose_ownership(self):
        from core.services.session import AgentSession

        project = Project.objects.create(name="p", slug="p")
        agent = _agent()
        task = Task.objects.create(project=project, title="t", status=Task.STATUS_REVIEW,
                                   current_agent=agent)
        session = AgentSession(task.pk)
        session._agent_id = agent.pk
        self.assertTrue(session._still_own_task())
        Task.objects.filter(pk=task.pk).update(current_agent=None)
        self.assertFalse(session._still_own_task())
