"""GlitchTip dedup / ignore rules and the Uptime Kuma webhook (core.services.alerts)."""
import json
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from unittest import mock

import httpx
from django.test import TestCase, override_settings
from django.utils import timezone

from core.models import ChatMessage, IgnoreRule, Project, Task
from core.services.alerts import glitchtip_issue_first_seen, normalize_title
from core.services.tools import Toolbox


def _glitchtip(title, project="acme", issue=1):
    return {
        "text": "GlitchTip Alert",
        "attachments": [{
            "title": title,
            "title_link": f"https://glitchtip.example.com/acme/issues/{issue}",
            "fields": [{"title": "Project", "value": project}],
        }],
    }


def _kuma(status, name="Acme", msg=""):
    return {
        "heartbeat": {"status": status, "msg": msg, "time": "2026-11-06 20:00:00"},
        "monitor": {"id": 3, "name": name, "url": "https://example.com"},
        "msg": f"[{name}] {'Up' if status else 'Down'}",
    }


class NormalizeTitleTests(TestCase):
    def test_masks_values(self):
        self.assertEqual(
            normalize_title("ValueError: invalid literal for int() with base 10: \"1' AND 1=1 UNION SELECT NULL-- -\""),
            normalize_title("ValueError: invalid literal for int() with base 10: '2''"),
        )
        self.assertEqual(
            normalize_title("Failed to fetch from source https://app.example.com/api/blogs/x/?lang=nb&page=1&i…"),
            normalize_title("Failed to fetch from source https://app.example.com/api/blogs/x/?lang=fr&page=1&i…"),
        )

    def test_keeps_distinct_errors_apart(self):
        self.assertNotEqual(
            normalize_title("AttributeError: 'NoneType' object has no attribute 'slug'"),
            normalize_title("AttributeError: 'Project' object has no attribute 'owner'"),
        )
        self.assertNotEqual(
            normalize_title("TemplateDoesNotExist: terms.html"),
            normalize_title("TemplateDoesNotExist: faq.html"),
        )


class GlitchtipWebhookTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Acme", glitchtip_project="acme")
        # The webhook asks GlitchTip when the issue was first seen, so it
        # only opens a card for genuinely new issues. That is a live API
        # call — mock it, or the tests depend on a configured token and on
        # whatever the real issue's firstSeen happens to be.
        patcher = mock.patch(
            "core.services.alerts.glitchtip_issue_first_seen", return_value=None
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, payload):
        return self.client.post("/webhooks/glitchtip/", json.dumps(payload), content_type="application/json")

    def test_webhook_never_calls_the_live_api(self):
        """The webhook must not depend on a real GlitchTip issue.

        Without the setUp mock the ingest looks up the issue's firstSeen
        over HTTP; the answer then depends on the token being configured
        and on how old that particular production issue happens to be —
        which turned this suite red (200 instead of 201) once the token
        started being read from the environment.
        """
        # A token is configured on purpose here: the setUp mock is then the
        # only thing between the webhook and the live API, so this test
        # fails loudly if that mock ever goes away.
        with override_settings(GLITCHTIP_API_KEY="test-token"):
            with mock.patch("core.services.alerts.httpx.get") as live_call:
                r = self.post(_glitchtip("KeyError: 'slug'", issue=2303))
        self.assertEqual(r.status_code, 201)
        # Only the URLs, never the call kwargs — those carry the API token.
        urls = [c.args[0] for c in live_call.call_args_list]
        self.assertEqual(urls, [], f"the webhook called the live GlitchTip API: {urls}")

    def test_first_error_creates_card_repeats_only_count(self):
        r = self.post(_glitchtip("TemplateDoesNotExist: terms.html", issue=2303))
        self.assertEqual(r.status_code, 201)
        task = Task.objects.get()
        self.assertEqual((task.source, task.status), (Task.SOURCE_GLITCHTIP, Task.STATUS_TODO))

        Task.objects.filter(pk=task.pk).update(status=Task.STATUS_DONE)
        r = self.post(_glitchtip("TemplateDoesNotExist: terms.html", issue=2307))
        self.assertEqual(r.json()["result"], "duplicate")
        self.assertEqual(Task.objects.count(), 1)
        task.refresh_from_db()
        self.assertEqual(task.occurrences, 2)

        self.post(_glitchtip("TemplateDoesNotExist: faq.html", issue=2310))
        self.assertEqual(Task.objects.count(), 2)

    def test_ignore_rule(self):
        # seeded by migration 0002
        r = self.post(_glitchtip("BrokenPipeError: [Errno 32] Broken pipe"))
        self.assertEqual(r.json()["result"], "ignored")
        self.assertFalse(Task.objects.exists())
        self.assertEqual(IgnoreRule.objects.get(pattern=r"^BrokenPipeError\b").hits, 1)

    def test_project_scoped_rule_does_not_leak(self):
        other = Project.objects.create(name="River", glitchtip_project="river")
        IgnoreRule.objects.create(pattern="SSL SYSCALL", project=other)
        self.post(_glitchtip("OperationalError: SSL SYSCALL error: EOF detected"))
        self.assertEqual(Task.objects.count(), 1)


    def test_inactive_project_opens_no_card(self):
        Project.objects.filter(pk=self.project.pk).update(is_active=False)
        self.assertEqual(self.post(_glitchtip("KeyError: 'slug'")).json()["result"], "inactive")
        self.assertFalse(Task.objects.exists())

    def test_card_asks_for_triage(self):
        self.post(_glitchtip("KeyError: 'slug'"))
        self.assertIn("reject_task", Task.objects.get().description)

    @override_settings(GLITCHTIP_NEW_ISSUE_MAX_AGE_HOURS=24)
    def test_only_first_time_issues_open_a_card(self):
        old = timezone.now() - timedelta(days=3)
        with mock.patch("core.services.alerts.glitchtip_issue_first_seen", return_value=old) as lookup:
            r = self.post(_glitchtip("KeyError: 'slug'", issue=4242))
        lookup.assert_called_once_with("https://glitchtip.example.com/acme/issues/4242")
        self.assertEqual(r.json()["result"], "not_new")
        self.assertFalse(Task.objects.exists())

        fresh = timezone.now() - timedelta(minutes=5)
        with mock.patch("core.services.alerts.glitchtip_issue_first_seen", return_value=fresh):
            self.assertEqual(self.post(_glitchtip("KeyError: 'id'")).status_code, 201)

    def test_lookup_failure_still_opens_card(self):
        with mock.patch("core.services.alerts.glitchtip_issue_first_seen", return_value=None):
            self.assertEqual(self.post(_glitchtip("KeyError: 'slug'")).status_code, 201)


class GlitchtipFirstSeenTests(TestCase):
    """The first-seen lookup itself — mocked at the HTTP boundary.

    `glitchtip_issue_first_seen` is the only place in the webhook path that
    talks to GlitchTip over the network, so its own behaviour (URL, auth
    header, timestamp parsing, failure handling) is covered here instead of
    through the live API.
    """

    URL = "https://glitchtip.example.com/acme/issues/2303"

    @override_settings(GLITCHTIP_API_KEY="tok", GLITCHTIP_URL="https://glitchtip.example.com")
    def test_reads_first_seen_from_the_api(self):
        resp = mock.Mock()
        resp.json.return_value = {"firstSeen": "2026-11-06T21:00:00Z"}
        with mock.patch("core.services.alerts.httpx.get", return_value=resp) as get:
            first_seen = glitchtip_issue_first_seen(self.URL)
        self.assertEqual(first_seen, datetime(2026, 11, 6, 21, 0, tzinfo=dt_timezone.utc))
        self.assertEqual(get.call_args.args[0], "https://glitchtip.example.com/api/0/issues/2303/")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer tok")

    @override_settings(GLITCHTIP_API_KEY="")
    def test_without_a_token_it_does_not_call_the_api(self):
        with mock.patch("core.services.alerts.httpx.get") as get:
            self.assertIsNone(glitchtip_issue_first_seen(self.URL))
        get.assert_not_called()

    @override_settings(GLITCHTIP_API_KEY="tok")
    def test_a_url_without_an_issue_id_does_not_call_the_api(self):
        with mock.patch("core.services.alerts.httpx.get") as get:
            self.assertIsNone(glitchtip_issue_first_seen("https://glitchtip.example.com/acme/"))
        get.assert_not_called()

    @override_settings(GLITCHTIP_API_KEY="tok")
    def test_an_api_error_is_not_fatal(self):
        # A GlitchTip outage must not stop the webhook from opening a card.
        with mock.patch("core.services.alerts.httpx.get", side_effect=httpx.HTTPError("500")):
            self.assertIsNone(glitchtip_issue_first_seen(self.URL))

    @override_settings(GLITCHTIP_API_KEY="tok")
    def test_an_unreadable_timestamp_is_not_fatal(self):
        resp = mock.Mock()
        resp.json.return_value = {"firstSeen": "not-a-date"}
        with mock.patch("core.services.alerts.httpx.get", return_value=resp):
            self.assertIsNone(glitchtip_issue_first_seen(self.URL))


class RejectTaskToolTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Acme", glitchtip_project="acme")
        self.task = Task.objects.create(
            project=self.project, title="ModuleNotFoundError: No module named 'pyotp'",
            status=Task.STATUS_IN_PROGRESS, source=Task.SOURCE_GLITCHTIP,
        )

    @mock.patch("core.services.tools.notify_task")
    def test_reject_closes_card_and_adds_rule(self, _notify):
        r = Toolbox(self.task).call("reject_task", {
            "reason": "deploy sırasında geçici", "ignore_pattern": r"No module named 'pyotp'",
        })
        self.assertFalse(r.detail.get("error"), r.content)
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.STATUS_DONE)
        rule = IgnoreRule.objects.get(pattern=r"No module named 'pyotp'")
        self.assertEqual(rule.project, self.project)
        self.assertTrue(ChatMessage.objects.filter(task=self.task, content__contains="reddedildi").exists())

    def test_bad_pattern_is_refused_and_card_stays(self):
        for pattern in ("(", "TemplateDoesNotExist"):
            r = Toolbox(self.task).call("reject_task", {"reason": "x", "ignore_pattern": pattern})
            self.assertEqual(r.detail.get("error"), "bad_regex")
        self.task.refresh_from_db()
        self.assertEqual(self.task.status, Task.STATUS_IN_PROGRESS)

    @mock.patch("core.services.tools.notify_task")
    def test_pattern_refused_on_manual_card(self, _notify):
        self.task.source = Task.SOURCE_MANUAL
        r = Toolbox(self.task).call("reject_task", {"reason": "x", "ignore_pattern": "pyotp"})
        self.assertEqual(r.detail.get("error"), "bad_arg")
        r = Toolbox(self.task).call("reject_task", {"reason": "zaten yapılmış"})
        self.assertFalse(r.detail.get("error"), r.content)


@override_settings(WEBHOOK_TOKEN="s3cret", UPTIME_DOWN_GRACE_SECONDS=60)
class UptimeKumaWebhookTests(TestCase):
    def setUp(self):
        self.project = Project.objects.create(name="Acme", uptime_monitors="Acme, River")

    def post(self, payload, token="s3cret"):
        return self.client.post(
            f"/webhooks/uptime-kuma/{token}/", json.dumps(payload), content_type="application/json",
        )

    def test_rejects_bad_token(self):
        self.assertEqual(self.post(_kuma(0), token="nope").status_code, 403)
        self.assertFalse(Task.objects.exists())

    def test_down_opens_one_top_card_with_grace(self):
        Task.objects.create(project=self.project, title="old", status=Task.STATUS_TODO, position=5)
        self.assertEqual(self.post(_kuma(0, msg="timeout")).status_code, 201)
        self.assertEqual(self.post(_kuma(0)).json()["result"], "duplicate")
        card = Task.objects.get(source=Task.SOURCE_UPTIME)
        self.assertEqual(card.occurrences, 2)
        self.assertLess(card.position, 5)
        self.assertGreater(card.next_dispatch_after, timezone.now())
        self.assertIn("https://example.com", card.description)

    def test_up_before_pickup_closes_blip(self):
        self.post(_kuma(0))
        self.assertEqual(self.post(_kuma(1)).json()["result"], "resolved")
        self.assertEqual(Task.objects.get().status, Task.STATUS_DONE)

    def test_up_after_pickup_is_posted_to_chat(self):
        self.post(_kuma(0))
        Task.objects.update(status=Task.STATUS_IN_PROGRESS)
        self.assertEqual(self.post(_kuma(1, msg="200 - OK")).json()["result"], "recovered")
        self.assertTrue(ChatMessage.objects.filter(content__contains="tekrar ayakta").exists())

    def test_new_incident_after_review_opens_new_card(self):
        self.post(_kuma(0))
        Task.objects.update(status=Task.STATUS_REVIEW)
        self.assertEqual(self.post(_kuma(0)).status_code, 201)
        self.assertEqual(Task.objects.count(), 2)

    def test_server_push_monitor_card_names_the_server(self):
        Project.objects.filter(pk=self.project.pk).update(uptime_monitors="Acme, Server neo2")
        payload = {
            "heartbeat": {"status": 0, "msg": "health-check: no answer", "time": "2026-11-07 15:00:00"},
            "monitor": {"id": 45, "name": "Server neo2", "url": ""},
        }
        self.assertEqual(self.post(payload).status_code, 201)
        card = Task.objects.get(source=Task.SOURCE_UPTIME)
        self.assertIn("ssh neo2", card.description)
        self.assertIn("health-check: no answer", card.description)

    def test_unmapped_monitor_and_test_button_ignored(self):
        self.assertEqual(self.post(_kuma(0, name="Relay usa1")).json()["result"], "ignored")
        self.assertEqual(self.post({"msg": "Test"}).json()["result"], "ignored")
        self.assertFalse(Task.objects.exists())
