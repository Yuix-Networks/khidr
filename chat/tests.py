from unittest import mock

from django.test import TestCase
from django.urls import reverse

from core.models import Agent, Project, Task


class EditTaskTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Test Project", is_active=True)
        self.agent = Agent.objects.create(
            slug="claude-test",
            type=Agent.TYPE_CLAUDE,
            role=Agent.ROLE_WORKER,
            talent=3,
            is_active=True,
        )
        # Bypass session auth — set the SESSION_KEY directly.
        session = self.client.session
        session["khidr_logged_in"] = True
        session.save()

    def test_assigning_agent_moves_task_to_todo(self):
        """Editing a task and assigning an agent moves it from Done → Todo."""
        task = Task.objects.create(
            project=self.project,
            title="Test Task",
            complexity=3,
            status=Task.STATUS_DONE,
        )
        response = self.client.post(
            reverse("edit_task", args=[task.pk]),
            {
                "title": "Test Task",
                "description": "",
                "complexity": "3",
                "assigned_agent": "claude-test",
            },
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.STATUS_TODO)
        self.assertEqual(task.assigned_agent, self.agent)
        self.assertIsNone(task.current_agent)

    def test_assigning_agent_clears_current_agent(self):
        """Assigning a new agent releases the current_agent slot."""
        busy_agent = Agent.objects.create(
            slug="busy-agent",
            type=Agent.TYPE_CLAUDE,
            role=Agent.ROLE_WORKER,
            talent=3,
            is_active=True,
        )
        task = Task.objects.create(
            project=self.project,
            title="Busy Task",
            complexity=3,
            status=Task.STATUS_IN_PROGRESS,
            current_agent=busy_agent,
        )
        response = self.client.post(
            reverse("edit_task", args=[task.pk]),
            {
                "title": "Busy Task",
                "description": "",
                "complexity": "3",
                "assigned_agent": "claude-test",
            },
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertIsNone(task.current_agent)
        self.assertEqual(task.status, Task.STATUS_TODO)

    def test_editing_without_agent_does_not_change_status(self):
        """Editing title/description without assigning an agent does not reset status."""
        task = Task.objects.create(
            project=self.project,
            title="Stay Done",
            complexity=3,
            status=Task.STATUS_DONE,
        )
        response = self.client.post(
            reverse("edit_task", args=[task.pk]),
            {
                "title": "Stay Done — edited",
                "description": "new desc",
                "complexity": "3",
                "assigned_agent": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        task.refresh_from_db()
        self.assertEqual(task.status, Task.STATUS_DONE)

class NewTaskTelegramTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Acme", is_active=True)

    def test_new_card_pings_telegram_once(self):
        with mock.patch("chat.signals.Thread") as thread, \
                mock.patch("core.services.telegram.notify_task") as notify, \
                self.captureOnCommitCallbacks(execute=True):
            task = Task.objects.create(project=self.project, title="Fix login", source=Task.SOURCE_UPTIME)
        thread.return_value.start.assert_called_once()
        thread.call_args.kwargs["target"]()
        notify.assert_called_once()
        self.assertEqual(notify.call_args.args[0].pk, task.pk)
        self.assertEqual(notify.call_args.args[1], "🆕 New task · Acme (Uptime Kuma)")

    def test_update_does_not_ping(self):
        task = Task.objects.create(project=self.project, title="Fix login")
        with mock.patch("chat.signals.Thread") as thread, self.captureOnCommitCallbacks(execute=True):
            task.title = "Fix signup"
            task.save()
        thread.assert_not_called()
