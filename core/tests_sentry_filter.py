"""
Tests for the Sentry before_send noise filter in khidr.settings.

The filter drops Django's DisallowedHost exceptions before they reach
GlitchTip. ALLOWED_HOSTS already rejects these at the HTTP layer (Django
returns 400), so capturing them as Sentry errors is pure noise — GlitchTip
issue #35695 is the canonical example: count=1, Host=testserver, no
follow-ups. The unit test below pins the filter's behaviour so a future
edit can't accidentally start forwarding these again.

It also scrubs upstream LLM-provider 4xx errors whose body is an HTML
default-page (GlitchTip issue #36442: MiniMax 404 returning the nginx
default page, 8 events in 6 minutes for one transient outage).

And it drops BrokenPipeError raised inside Daphne's access logger
(daphne/access.py:write_entry → stream.write) when its stdout pipe closes
mid-request (GlitchTip issue #36611). Daphne doesn't catch it, so without
this filter we get one alert per torn-down request.
"""

from __future__ import annotations

import unittest

from django.core.exceptions import DisallowedHost


class SentryBeforeSendFilterTests(unittest.TestCase):
    def _call(self, exc, event=None):
        """Invoke the filter with a synthetic exc_info hint."""
        from khidr.settings import _sentry_before_send

        return _sentry_before_send(
            event=event if event is not None else {"some": "event"},
            hint={"exc_info": (type(exc), exc, None)},
        )

    def test_disallowed_host_is_dropped(self):
        exc = DisallowedHost("Invalid HTTP_HOST header: 'testserver'.")
        self.assertIsNone(self._call(exc))

    def test_unrelated_exception_passes_through(self):
        try:
            raise ValueError("keep me")
        except ValueError as exc:
            result = self._call(exc)
        self.assertEqual(result, {"some": "event"})

    def test_no_exc_info_passes_through(self):
        from khidr.settings import _sentry_before_send

        self.assertEqual(
            _sentry_before_send(event={"some": "event"}, hint={}),
            {"some": "event"},
        )


class _FakeApiError(Exception):
    """Stand-in for openai.NotFoundError — has status_code + HTML body."""

    def __init__(self, status_code, body):
        super().__init__(body)
        self.status_code = status_code


class SentryBeforeSendUpstreamScrubTests(unittest.TestCase):
    """Pin behaviour of _scrub_upstream_html against provider 4xx HTML bodies."""

    def _base_event(self, exc_value):
        return {
            "title": f"{type(exc_value).__name__}: <html>",
            "exception": {
                "values": [
                    {"type": type(exc_value).__name__, "value": str(exc_value)}
                ]
            },
        }

    def test_404_html_body_is_trimmed_and_renamed(self):
        body = (
            "<html>\r\n<head><title>404 Not Found</title></head>\r\n"
            "<body><center><h1>404 Not Found</h1></center></body>\r\n"
            "</html>"
        )
        exc = _FakeApiError(404, body)
        from khidr.settings import _sentry_before_send

        result = _sentry_before_send(
            event=self._base_event(exc),
            hint={"exc_info": (type(exc), exc, None)},
        )
        self.assertIsNotNone(result)
        # Title no longer starts with "<html>" — that's the whole point.
        self.assertNotIn("<html>", result["title"])
        self.assertIn("404", result["title"])
        # Body value got trimmed (the full original body is < 200 chars, so
        # we expect exact preservation here; the truncation branch is tested
        # separately below).
        self.assertEqual(
            result["exception"]["values"][0]["value"], body.replace("\r", "")
        )

    def test_large_404_body_is_truncated(self):
        body = "<html>" + ("x" * 5000) + "</html>"
        exc = _FakeApiError(404, body)
        from khidr.settings import _sentry_before_send

        result = _sentry_before_send(
            event=self._base_event(exc),
            hint={"exc_info": (type(exc), exc, None)},
        )
        trimmed = result["exception"]["values"][0]["value"]
        self.assertIn("<truncated", trimmed)
        # The truncation marker must mention the original size, so operators
        # know how much got dropped.
        self.assertIn("5013", trimmed)
        # A meaningful tail chunk from the original 5000-byte body must not
        # have leaked through verbatim — only the first ~200 chars should.
        self.assertNotIn("x" * 300, trimmed)

    def test_non_html_4xx_passes_through(self):
        exc = _FakeApiError(401, "login fail: API key invalid")
        from khidr.settings import _sentry_before_send

        event = self._base_event(exc)
        result = _sentry_before_send(
            event=event, hint={"exc_info": (type(exc), exc, None)}
        )
        # We didn't recognise it as upstream-HTML noise — original event unchanged.
        self.assertEqual(result["exception"]["values"][0]["value"], str(exc))

    def test_500_html_body_gets_scrubbed_too(self):
        # Any HTTP 4xx/5xx with an HTML body should be scrubbed, not just 404.
        exc = _FakeApiError(503, "<html>Service Unavailable</html>")
        from khidr.settings import _sentry_before_send

        result = _sentry_before_send(
            event=self._base_event(exc),
            hint={"exc_info": (type(exc), exc, None)},
        )
        self.assertIn("503", result["title"])
        self.assertNotIn("<html>", result["title"])

    def test_exception_without_status_code_passes_through(self):
        exc = ValueError("totally unrelated ValueError, no HTTP semantics")
        from khidr.settings import _sentry_before_send

        event = self._base_event(exc)
        result = _sentry_before_send(
            event=event, hint={"exc_info": (type(exc), exc, None)}
        )
        # Non-HTTP exception without an HTML body must pass through
        # completely untouched.
        self.assertEqual(result["exception"]["values"][0]["value"], str(exc))
        self.assertEqual(result["title"], "ValueError: <html>")  # original title

    def test_wrapped_html_exception_gets_scrubbed(self):
        # Simulates the OpenHarness OpenAICompatibleClient path: it wraps
        # openai.NotFoundError into a RequestFailure whose status_code is
        # stripped. The HTML body, however, survives in str(exc). The
        # filter must still scrub it because "<html" is in the message.
        body = (
            "<html><head><title>404 Not Found</title></head>"
            "<body><h1>404 Not Found</h1></body></html>"
        )
        exc = _FakeApiError(None, body)  # status_code intentionally None
        # Force no status_code: the FakeApiError ctor sets it from args[1]
        exc.status_code = None
        from khidr.settings import _sentry_before_send

        result = _sentry_before_send(
            event=self._base_event(exc),
            hint={"exc_info": (type(exc), exc, None)},
        )
        # Title is rewritten so "<html>" doesn't show up in alert feeds.
        self.assertNotIn("<html>", result["title"])
        # Body survives (this body is shorter than the 200-byte limit) but
        # the type field is now the actual exception type.
        self.assertEqual(result["exception"]["values"][0]["type"], "_FakeApiError")


class SentryBeforeSendBrokenPipeTests(unittest.TestCase):
    """Pin behaviour against Daphne's BrokenPipeError noise."""

    def _call(self, exc):
        from khidr.settings import _sentry_before_send

        return _sentry_before_send(
            event={"title": "BrokenPipeError: [Errno 32] Broken pipe"},
            hint={"exc_info": (type(exc), exc, None)},
        )

    def test_broken_pipe_is_dropped(self):
        # Stand-in for the exact exception type Daphne raises. We don't
        # import BrokenPipeError via daphne — we just raise one and verify
        # the filter drops it by class name.
        exc = BrokenPipeError(32, "Broken pipe")
        self.assertIsNone(self._call(exc))

    def test_connection_reset_passes_through(self):
        # We deliberately drop ONLY BrokenPipeError, not the related
        # ConnectionResetError — those signal a different problem (peer
        # abruptly closed a request mid-response, not a torn-down log
        # pipe) and operators may want to see them.
        exc = ConnectionResetError(104, "Connection reset by peer")
        self.assertIsNotNone(self._call(exc))

    def test_oserror_with_epipe_is_dropped_too(self):
        # Python's built-in BrokenPipeError is just OSError with errno=EPIPE;
        # OSError(errno=32) auto-instantiates as BrokenPipeError, so it has
        # to be dropped the same way. This is the path Daphne actually takes
        # because stream.write() raises the bare errno, not a typed class.
        exc = OSError(32, "Broken pipe")
        self.assertIsNone(self._call(exc))


class SentryBeforeSendDaphneLogMessageBrokenPipeTests(unittest.TestCase):
    """
    Pin behaviour against Daphne's BrokenPipeError noise that arrives
    through the logging integration as a *log message* event (no
    ``exception`` payload, no ``exc_info`` in the hint). This is the
    shape GlitchTip #37390 takes: Daphne's access logger prints the
    traceback without ``exc_info=True``, so Sentry SDK's
    ``LoggingIntegration._emit`` builds a log_message event instead of
    running it through ``event_from_exception``. The filter has to drop
    it from the formatted log text + title because the exc_info hint
    branch is never reached.
    """

    DAPHNE_TRACE = (
        "Traceback (most recent call last):\n"
        '  File "/home/khidr/.pyenv/versions/3.14.4/envs/khidr/lib/'
        'python3.14/site-packages/daphne/access.py", line 59, in write_entry\n'
        "    self.stream.write(\n"
        "BrokenPipeError: [Errno 32] Broken pipe\n"
    )

    def _call(self, event):
        from khidr.settings import _sentry_before_send

        return _sentry_before_send(event=event, hint={})

    def test_log_message_event_is_dropped(self):
        # Real-shape event as GlitchTip forwards it: no exception, no
        # exc_info hint, logentry.formatted carries the full traceback
        # string including the daphne/access.py frame.
        event = {
            "logentry": {
                "formatted": self.DAPHNE_TRACE,
                "message": self.DAPHNE_TRACE,
            },
            "title": "BrokenPipeError: [Errno 32] Broken pipe",
        }
        self.assertIsNone(self._call(event))

    def test_logentry_formatted_match_is_dropped_when_only_title_carries_it(self):
        # Some Sentry ingestion paths collapse the traceback into the
        # title and leave logentry blank, but logentry itself is still
        # present as an empty dict (the SDK always populates the key).
        # We must still catch it -- the title carries the full match.
        event = {
            "logentry": {},
            "title": (
                "Traceback ... daphne/access.py write_entry -> "
                "BrokenPipeError: [Errno 32] Broken pipe"
            ),
        }
        self.assertIsNone(self._call(event))

    def test_unrelated_broken_pipe_passes_through(self):
        # Generic BrokenPipe from an unrelated subsystem must NOT be
        # dropped just because it mentions "Broken pipe". The daphne
        # frame is what makes the noise safe to ignore.
        event = {
            "logentry": {
                "formatted": (
                    "Traceback (most recent call last):\n"
                    "  File \"/srv/orchestrator/jobs/exporter.py\", line 12\n"
                    "BrokenPipeError: [Errno 32] Broken pipe\n"
                ),
                "message": "BrokenPipeError: [Errno 32] Broken pipe",
            },
            "title": "BrokenPipeError: [Errno 32] Broken pipe",
        }
        self.assertEqual(self._call(event), event)

    def test_daphne_frame_without_broken_pipe_passes_through(self):
        # Defensive: a daphne traceback that is NOT a broken pipe (say
        # a real upstream bug in daphne itself) must not be silenced by
        # this filter. Only the conjunction is dropped.
        event = {
            "logentry": {
                "formatted": (
                    "Traceback (most recent call last):\n"
                    '  File ".../daphne/access.py", line 59, in write_entry\n'
                    "ValueError: something else entirely\n"
                ),
                "message": "ValueError: something else entirely",
            },
            "title": "ValueError: something else entirely",
        }
        self.assertEqual(self._call(event), event)

    def test_non_dict_event_passes_through(self):
        # Hardening: Sentry normally only passes dict events, but if a
        # future SDK change forwards something else we must not crash.
        self.assertEqual(self._call(None), None)  # type: ignore[arg-type]
        self.assertEqual(self._call("not a dict"), "not a dict")  # type: ignore[arg-type]

    def test_event_without_logentry_passes_through(self):
        # No logentry and no exc_info -> the filter has nothing to
        # inspect, must pass through untouched.
        event = {"title": "Some unrelated message"}
        self.assertEqual(self._call(event), event)

class SentryBeforeSendTestDatabaseNoiseTests(unittest.TestCase):
    """A test run that could not reach its PostgreSQL test database is
    development noise, not an incident (GlitchTip #39521).

    The host's PostgreSQL cannot host Django's test suite from an agent
    container: the `khidr` role has no CREATEDB privilege and pg_hba.conf
    only allows the `khidr` database from the container network. The
    filter drops that specific error so it does not open a card; a
    pg_hba failure against the *real* database must still be reported.
    """

    PG_HBA_MESSAGE = (
        'connection failed: connection to server at "10.0.0.5", port 5432 '
        'failed: FATAL:  no pg_hba.conf entry for host "172.18.0.2", user '
        '"khidr", database "test_khidr", SSL encryption\nconnection to server '
        'at "10.0.0.5", port 5432 failed: FATAL:  no pg_hba.conf entry '
        'for host "172.18.0.2", user "khidr", database "test_khidr", no encryption'
    )

    def _call(self, exc, event=None):
        from khidr.settings import _sentry_before_send

        return _sentry_before_send(
            event=event if event is not None else {"some": "event"},
            hint={"exc_info": (type(exc), exc, None)},
        )

    def _operational_error(self, message):
        from django.db.utils import OperationalError

        return OperationalError(message)

    def test_test_database_pg_hba_error_is_dropped(self):
        exc = self._operational_error(self.PG_HBA_MESSAGE)
        self.assertIsNone(self._call(exc))

    def test_same_error_is_dropped_from_the_event_payload_only(self):
        # No exc_info hint (e.g. the SDK captured it elsewhere): the
        # message still has to be recognised.
        from khidr.settings import _sentry_before_send

        event = {
            "exception": {"values": [{"type": "OperationalError", "value": self.PG_HBA_MESSAGE}]}
        }
        self.assertIsNone(_sentry_before_send(event=event, hint={}))

    def test_permission_denied_creating_the_test_database_is_dropped(self):
        exc = self._operational_error(
            "Got an error creating the test database: permission denied to create database"
        )
        self.assertIsNone(self._call(exc))

    def test_pg_hba_error_for_the_postgres_maintenance_database_is_dropped(self):
        # Django opens a connection to the "postgres" maintenance database
        # before it can create the throw-away test database, and pg_hba
        # refuses that from an agent container for the same reason. The
        # orchestrator itself only ever uses the "khidr" database, so this
        # error can only come from a test run.
        exc = self._operational_error(
            'connection failed: connection to server at "10.0.0.5", port 5432 '
            'failed: FATAL:  no pg_hba.conf entry for host "172.18.0.2", user '
            '"khidr", database "postgres", SSL encryption'
        )
        self.assertIsNone(self._call(exc))

    def test_pg_hba_error_for_the_real_database_passes_through(self):
        exc = self._operational_error(
            'connection failed: FATAL:  no pg_hba.conf entry for host "172.18.0.2", '
            'user "khidr", database "khidr", SSL encryption'
        )
        self.assertEqual(self._call(exc), {"some": "event"})

    def test_unrelated_database_error_passes_through(self):
        exc = self._operational_error(
            'connection failed: FATAL:  password authentication failed for user "khidr"'
        )
        self.assertEqual(self._call(exc), {"some": "event"})

    def test_non_dict_event_does_not_crash(self):
        from khidr.settings import _sentry_before_send

        exc = self._operational_error(self.PG_HBA_MESSAGE)
        self.assertIsNone(
            _sentry_before_send(event=None, hint={"exc_info": (type(exc), exc, None)})  # type: ignore[arg-type]
        )


class SentryBeforeSendTestModuleSyntaxErrorTests(unittest.TestCase):
    """A test module that does not compile breaks the test suite and
    nothing else (GlitchTip #39524).

    Commit 7d41606 shipped a `with` block whose body was not indented.
    `manage.py test` died at import time with

        IndentationError: expected an indented block after 'with'
        statement on line 84 (tests_alerts.py, line 85)

    and because an agent container's test process carries the production
    Sentry DSN, the failure was filed as a production incident. Only
    test modules are dropped: a syntax error in application code must
    still be reported.
    """

    MESSAGE = (
        "expected an indented block after 'with' statement on line 84 "
        "(tests_alerts.py, line 85)"
    )
    TITLE = f"IndentationError: {MESSAGE}"

    def _call(self, exc=None, event=None):
        from khidr.settings import _sentry_before_send

        hint = {"exc_info": (type(exc), exc, None)} if exc is not None else {}
        return _sentry_before_send(
            event=event if event is not None else {"some": "event"},
            hint=hint,
        )

    def _event(self):
        """The shape the event really had: the file name only in the
        message — the SyntaxError contributes no frame of its own."""
        return {
            "exception": {
                "values": [{"type": "IndentationError", "value": self.MESSAGE}]
            },
            "title": self.TITLE,
        }

    def test_indentation_error_in_a_test_module_is_dropped(self):
        exc = IndentationError(self.MESSAGE)
        self.assertIsNone(self._call(exc))

    def test_the_real_event_shape_is_dropped(self):
        # No exc_info in the hint: the event payload alone has to be
        # enough to recognise the noise.
        self.assertIsNone(self._call(event=self._event()))

    def test_syntax_error_in_application_code_passes_through(self):
        exc = SyntaxError("invalid syntax (core/services/alerts.py, line 12)")
        self.assertEqual(self._call(exc), {"some": "event"})

    def test_non_syntax_error_mentioning_a_test_module_passes_through(self):
        exc = ValueError("tests_alerts.py is not a valid fixture")
        self.assertEqual(self._call(exc), {"some": "event"})

    def test_syntax_error_without_a_file_name_passes_through(self):
        exc = SyntaxError("invalid syntax")
        self.assertEqual(self._call(exc), {"some": "event"})

    def test_test_settings_module_counts_as_a_test_module(self):
        # khidr/test_settings.py is imported by the test runner only.
        exc = IndentationError("expected an indented block (test_settings.py, line 3)")
        self.assertIsNone(self._call(exc))

    def test_tab_error_in_a_test_module_is_dropped(self):
        exc = TabError("inconsistent use of tabs (tests_alerts.py, line 12)")
        self.assertIsNone(self._call(exc))
