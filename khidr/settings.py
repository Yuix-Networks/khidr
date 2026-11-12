"""
Django settings for Khidr.
"""

import os
import re
import socket
import time
from pathlib import Path

import sentry_sdk

BASE_DIR = Path(__file__).resolve().parent.parent

# Configuration comes from environment variables. A `.env` file in the repo
# root is loaded for convenience (variables already set in the shell win).
# A test run (test_settings.py sets KHIDR_TESTS) reads no .env at all: the
# suite must pass with no secrets on the machine.
if not os.environ.get("KHIDR_TESTS"):
    from dotenv import load_dotenv

    load_dotenv(BASE_DIR / ".env", override=False)

# Stamped once per process start — good enough to bust browser caches for
# static JS/CSS across server restarts without rehashing on every request.
TIME_VERSION = "%s" % int(time.time())


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


def env_bool(key: str, default: bool = False) -> bool:
    val = os.environ.get(key)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

SECRET_KEY = env(
    "DJANGO_SECRET_KEY",
    "django-insecure-dev-key-change-me-before-prod",
)
DEBUG = env_bool("DJANGO_DEBUG", False)


def env_list(key: str, default: str = "") -> list[str]:
    return [v.strip() for v in env(key, default).split(",") if v.strip()]


ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    # daphne must come before django.contrib.staticfiles so runserver picks
    # it up as the ASGI server (Channels requirement).
    "daphne",
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "channels",
    "core",
    "chat",
]

CHANNEL_LAYERS = {
    "default": {"BACKEND": "channels.layers.InMemoryChannelLayer"},
}

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # WhiteNoise serves /static/* directly from STATIC_ROOT — daphne (ASGI)
    # does not serve static files, and we don't run an nginx layer. Must
    # come right after SecurityMiddleware per the whitenoise docs.
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "khidr.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "chat.context_processors.common",
            ],
        },
    },
]

WSGI_APPLICATION = "khidr.wsgi.application"
ASGI_APPLICATION = "khidr.asgi.application"

# ---------------------------------------------------------------------------
# Database — PostgreSQL. Agents running in Docker containers connect to the
# same database, so PG_HOST must be reachable from them too.
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("PG_NAME", "khidr"),
        "USER": env("PG_USER", "khidr"),
        "PASSWORD": env("PG_PASS", ""),
        "HOST": env("PG_HOST", "127.0.0.1"),
        "PORT": env("PG_PORT", "5432"),
        "CONN_MAX_AGE": 60,
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
]

# ---------------------------------------------------------------------------
# i18n / tz
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "UTC")
USE_I18N = True
USE_TZ = True

# ---------------------------------------------------------------------------
# Static
# ---------------------------------------------------------------------------

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"

# WhiteNoise: gzip/brotli pre-compressed copies alongside each file, served
# directly by the app. Not using ManifestStaticFilesStorage because templates
# already cache-bust via `?v={{ TIME_VERSION }}`.
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}

# ---------------------------------------------------------------------------
# Sentry / GlitchTip
# ---------------------------------------------------------------------------

SENTRY_DSN = env("SENTRY_DSN", "")

# GlitchTip API token (for MCP integration)
GLITCHTIP_API_KEY = env("GLITCHTIP_API_KEY", "") or env("GLITCHTIP_API", "")

# GlitchTip instance base URL
GLITCHTIP_URL = env("GLITCHTIP_URL", "")

# A GlitchTip alert opens a card only if the issue's firstSeen is within this
# window; older issues re-alerting are dropped. Needs GLITCHTIP_API_KEY.
GLITCHTIP_NEW_ISSUE_MAX_AGE_HOURS = float(env("GLITCHTIP_NEW_ISSUE_MAX_AGE_HOURS", "24"))

# Secret path segment of /webhooks/uptime-kuma/<token>/ (configure it as the Uptime Kuma webhook URL)
WEBHOOK_TOKEN = env("KHIDR_API_TOKEN", "")

# An Uptime Kuma DOWN card waits this long before an agent may take it; an UP
# inside the window closes it as a blip.
UPTIME_DOWN_GRACE_SECONDS = int(env("UPTIME_DOWN_GRACE_SECONDS", "60"))


# Maximum length of an upstream error body that we'll keep verbatim in the
# Sentry event. Beyond this we truncate to the head + marker so a 30KB nginx
# default-page doesn't pollute GlitchTip's alert feed.
_UPSTREAM_ERROR_BODY_LIMIT = 200


def _scrub_upstream_html(event, exc_type, exc_value):
    """
    Collapse upstream-API NotFoundErrors / HTML-body errors into a compact,
    scannable form before they hit GlitchTip.

    Background: when an OpenAI-compat provider (e.g. MiniMax) returns 404 from
    a missing route, the OpenAI SDK raises ``NotFoundError`` whose message is
    the **entire HTML response body** (the nginx default error page). GlitchTip
    groups events by title, so every distinct page becomes its own alert
    (GlitchTip issue #36442 saw 8 events in 6 minutes for one transient
    outage). Trimming the body and rewriting the title keeps the alert useful
    without burying the operator in noise.

    Note: OpenHarness' OpenAICompatibleClient wraps the SDK error into a
    ``RequestFailure`` whose ``status_code`` attribute is gone — so we can't
    rely on duck-typing ``status_code`` alone. The fallback path is "message
    contains an HTML tag", which works in both shapes (raw SDK exception and
    OpenHarness wrapper).

    Returns a possibly-mutated event dict, or None to drop it entirely.
    """
    if exc_type is None:
        return event
    status_code = getattr(exc_value, "status_code", None)
    raw_value = str(exc_value) if exc_value is not None else ""
    has_html = "<html" in raw_value.lower()
    has_known_status = isinstance(status_code, int) and 400 <= status_code < 600
    if not (has_html or has_known_status):
        return event
    if not has_html:
        # 4xx/5xx without an HTML body — leave alone; the body is useful.
        return event

    head = raw_value[:_UPSTREAM_ERROR_BODY_LIMIT].replace("\r", "").strip()
    if len(raw_value) > _UPSTREAM_ERROR_BODY_LIMIT:
        head = f"{head}…<truncated, {len(raw_value)} bytes total>"

    # Walk the exception chain to surface the SDK frame's URL/method if we
    # can find one — GlitchTip's breadcrumb already records it but the title
    # is what an on-call engineer reads first.
    provider = ""
    request_url = ""
    request_method = ""
    tb = getattr(exc_value, "__traceback__", None)
    # __cause__ / __context__ chain often carries the original SDK exception
    # (e.g. openai.NotFoundError from under an openharness RequestFailure).
    chain_extras = []
    cur = getattr(exc_value, "__cause__", None) or getattr(exc_value, "__context__", None)
    while cur is not None and cur is not exc_value:
        chain_extras.append(cur)
        cur = getattr(cur, "__cause__", None) or getattr(cur, "__context__", None)

    def _inspect_frame(tb):
        nonlocal provider, request_url, request_method
        if tb is None:
            return
        frame_locals = tb.tb_frame.f_locals or {}
        for key in ("url", "request_url"):
            v = frame_locals.get(key)
            if isinstance(v, str) and v.startswith(("http://", "https://")):
                request_url = v
                break
        request_method = request_method or str(
            frame_locals.get("method", "") or ""
        )
        base_url = frame_locals.get("base_url") or frame_locals.get("_base_url")
        if isinstance(base_url, str) and base_url:
            provider = base_url

    while tb is not None:
        _inspect_frame(tb)
        tb = tb.tb_next
    for chained in chain_extras:
        _inspect_frame(getattr(chained, "__traceback__", None))

    nice_title = f"{exc_type.__name__} {status_code}" if has_known_status else exc_type.__name__
    if request_url:
        nice_title += f" on {request_method or 'POST'} {request_url}"
    elif provider:
        nice_title += f" from {provider}"

    event["title"] = nice_title
    event["message"] = {"formatted": nice_title, "params": []}
    # Mutate the topmost exception frame's value so the body is also trimmed.
    try:
        values = event["exception"]["values"]
        values[0]["value"] = head
        values[0]["type"] = exc_type.__name__
    except (KeyError, TypeError, IndexError):
        pass

    # Tag the event so future alert-routing rules can match provider+status
    # without scraping titles.
    existing_tags = event.setdefault("tags", {})
    if isinstance(existing_tags, dict):
        if has_known_status:
            existing_tags["http_status"] = status_code
        if provider:
            existing_tags["provider"] = provider
    return event


def _is_daphne_broken_pipe_event(event):
    """
    Return True iff ``event`` is the Daphne access-logger BrokenPipe
    noise event that arrives via the logging integration as a
    log_message (no ``exception`` field, no ``exc_info`` hint).

    We match on the combined presence of "daphne/access.py" and
    "Broken pipe" / "BrokenPipeError" across the logentry formatted
    body, the logentry raw message, and the event title. Both halves
    of the conjunction must match to drop the event -- checking only
    "Broken pipe" would also catch unrelated socket teardown noise
    from other libraries, and checking only "daphne/access.py" would
    be too broad. The daphne/access.py frame is part of every variant
    Daphne produces, so this combination is specific to that one
    upstream bug.
    """
    if not isinstance(event, dict):
        return False

    logentry = event.get("logentry")
    if not isinstance(logentry, dict):
        return False

    formatted = logentry.get("formatted") or ""
    raw_message = logentry.get("message") or ""
    title = event.get("title") or ""

    parts = []
    for chunk in (formatted, raw_message, title):
        if isinstance(chunk, str):
            parts.append(chunk)
    blob = "\n".join(parts)
    return "daphne/access.py" in blob and (
        "Broken pipe" in blob or "BrokenPipeError" in blob
    )


# The compile-time errors Python raises for a file that does not parse.
_SYNTAX_ERROR_TYPES = frozenset({"SyntaxError", "IndentationError", "TabError"})

# A Python file mentioned in a syntax error's message, e.g. the
# "(tests_alerts.py, line 85)" tail Python appends to the message.
_PY_FILE_RE = re.compile(r"([A-Za-z0-9_.\-/\\]+\.py)\b")


def _is_test_database_conn_error(event, exc_value=None):
    """
    Return True iff this event is the "Django could not reach its test
    database" error — an agent container ran the test suite against
    PostgreSQL and the server refused the connection to the throw-away
    database:

        OperationalError: connection failed: connection to server at
        "10.0.0.5", port 5432 failed: FATAL:  no pg_hba.conf entry
        for host "172.18.0.2", user "khidr", database "test_khidr",
        SSL encryption

    The live PostgreSQL server cannot host Django's test suite: the
    `khidr` role has no CREATEDB privilege, and pg_hba.conf only allows
    the `khidr` database from the container network. `manage.py` now
    defaults the test commands to `khidr.test_settings` (SQLite), so
    this should not happen any more — but if someone pins
    DJANGO_SETTINGS_MODULE to the production settings, the error is
    still development noise, not an incident worth a card. See GlitchTip
    issue #39521.

    The same run reports a second, near-identical error: before it can
    create the throw-away test database, Django opens a connection to the
    `postgres` maintenance database, which pg_hba refuses for the same
    reason. The orchestrator itself only ever uses the `khidr` database,
    so a pg_hba failure naming `postgres` can only come from a test run.
    Both errors share one GlitchTip title (it is truncated at 100
    characters, before the database name), so the card would look exactly
    the same either way.

    The match is deliberately narrow: the database name must be a test
    database (`test_*`) or the `postgres` maintenance database, so a
    pg_hba failure against the real `khidr` database is still reported.
    """
    if not isinstance(event, dict):
        return False

    parts = []
    if exc_value is not None:
        parts.append(str(exc_value))
    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for value in values:
                if isinstance(value, dict) and isinstance(value.get("value"), str):
                    parts.append(value["value"])

    blob = "\n".join(parts)
    if not blob:
        return False

    if "no pg_hba.conf entry" in blob and (
        'database "test_' in blob or 'database "postgres"' in blob
    ):
        return True
    # The same root cause, earlier in the sequence: Django could not
    # create the test database in the first place.
    if "test database" in blob and "permission denied to create database" in blob:
        return True
    return False


def _is_test_module_syntax_error(event, exc_value=None):
    """
    Return True iff this event is a compile-time error (SyntaxError,
    IndentationError, TabError) in a *test* module — e.g.

        IndentationError: expected an indented block after 'with'
        statement on line 84 (tests_alerts.py, line 85)

    A test module is imported by nothing but the test runner, so a file
    that does not compile can never affect the running application: it
    breaks the test suite and nothing else. It still reaches GlitchTip
    because an agent container's test process carries the production
    Sentry DSN, and because the SyntaxError itself contributes no frame
    — its location is only in the message, which is what there is to
    match on. See GlitchTip issue #39524.

    The match is deliberately narrow: the error must be one of the
    SyntaxError family *and* it must name a test module, so a syntax
    error in application code is still reported.
    """
    if not isinstance(event, dict):
        return False

    # (exception type name, message) pairs, from every place the event
    # can carry them.
    candidates = []
    if exc_value is not None:
        candidates.append((type(exc_value).__name__, str(exc_value)))

    exception = event.get("exception")
    if isinstance(exception, dict):
        values = exception.get("values")
        if isinstance(values, list):
            for value in values:
                if not isinstance(value, dict):
                    continue
                type_name = value.get("type")
                message = value.get("value")
                if isinstance(type_name, str) and isinstance(message, str):
                    candidates.append((type_name, message))

    # The title is "<Type>: <message>"; it is the last place the file
    # name can show up.
    title = event.get("title")
    if isinstance(title, str):
        for type_name in _SYNTAX_ERROR_TYPES:
            if title.startswith(f"{type_name}: "):
                candidates.append((type_name, title))
                break

    for type_name, message in candidates:
        if type_name not in _SYNTAX_ERROR_TYPES:
            continue
        for match in _PY_FILE_RE.finditer(message):
            name = match.group(1).replace("\\", "/").rsplit("/", 1)[-1]
            if name.startswith("test_") or name.startswith("tests_"):
                return True
    return False


def _sentry_before_send(event, hint):
    """
    Drop / scrub noise events before they leave the orchestrator.

    1. DisallowedHost is raised by Django whenever a request comes in with a
       Host header that isn't in ALLOWED_HOSTS — e.g. "testserver" from a
       stray Django test client inside an agent container that somehow
       landed on the production Daphne. Django already returns a 400 to
       the client, so the exception is doing its job. Reporting it to
       GlitchTip is pure noise (count=1 in our case) and makes the alert
       feed look broken. Don't add "testserver" to ALLOWED_HOSTS as a fix —
       that's a Host-header spoofing footgun in production.

    2. Upstream LLM-provider 4xx errors (e.g. MiniMax 404 returning the
       nginx default page) get their HTML body trimmed and their title
       rewritten so transient outages don't flood the alert feed. See
       _scrub_upstream_html.

    3. BrokenPipeError raised inside Daphne's access logger
       (daphne/access.py:write_entry -> stream.write) when its stdout
       pipe closes mid-request -- typically the supervisor log driver or
       the container's log sink tearing down. Daphne doesn't catch it,
       and Sentry's logging integration forwards it to GlitchTip in two
       shapes:

         a. As a captured exception (hint["exc_info"] carries
            BrokenPipeError). Caught by the exc_type check below.

         b. As a *log message* event when Daphne's own logger logs the
            traceback without exc_info=True (which is what happens by
            default for the access logger's own error path; see GlitchTip
            #37390). In that shape event["exception"] is empty, so the
            exc_info branch is never reached -- we have to inspect the
            formatted log body and the title instead.

       In both shapes the payload always contains the substring
       "daphne/access.py" alongside "Broken pipe" / "BrokenPipeError",
       which is specific enough to drop without false positives from
       unrelated I/O errors. Daphne isn't ours to patch; suppressing it
       server-side is the cheapest fix. See GlitchTip issues #36611
       (exception path) and #37390 (log-message path).

    4. A test run inside an agent container that could not reach its
       PostgreSQL test database ("FATAL: no pg_hba.conf entry for host
       ..., database \"test_khidr\""). The test suite is a
       development-time concern and the host's PostgreSQL cannot host
       it; `manage.py` now runs the tests on SQLite. See
       _is_test_database_conn_error and GlitchTip issue #39521.

    5. A compile-time error in a *test* module ("IndentationError:
       expected an indented block after 'with' statement on line 84
       (tests_alerts.py, line 85)"). Only the test runner imports those,
       so the running application is unaffected — but the agent's test
       process carries the production Sentry DSN, so the failure lands
       in GlitchTip as if it were an outage. See
       _is_test_module_syntax_error and GlitchTip issue #39524.
    """
    exc_value = None
    if "exc_info" in hint:
        exc_type, exc_value, _tb = hint["exc_info"]
        if exc_type is not None and exc_type.__name__ == "DisallowedHost":
            return None
        if exc_type is not None and exc_type.__name__ == "BrokenPipeError":
            return None
        event = _scrub_upstream_html(event, exc_type, exc_value)

    # A test run that could not reach its PostgreSQL test database. Checked
    # outside the exc_info branch as well: the message can arrive with the
    # event payload only. See _is_test_database_conn_error (GlitchTip #39521).
    if _is_test_database_conn_error(event, exc_value):
        return None

    # Daphne access-logger BrokenPipe noise delivered via the logging
    # integration as a log_message event (no exc_info in the hint).
    # See case 3b in the docstring above.
    if _is_daphne_broken_pipe_event(event):
        return None

    # A test module that does not compile: development noise from an
    # agent container's test run, never an application failure. See
    # _is_test_module_syntax_error (GlitchTip #39524).
    if _is_test_module_syntax_error(event, exc_value):
        return None

    return event


if SENTRY_DSN:
    sentry_sdk.init(
        dsn=SENTRY_DSN,
        send_default_pii=False,
        traces_sample_rate=0.1,
        before_send=_sentry_before_send,
    )

# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

HEARTBEAT_TIMEOUT_SECONDS = int(env("HEARTBEAT_TIMEOUT_SECONDS", "1800"))
SUMMARIZE_THRESHOLD = int(env("SUMMARIZE_THRESHOLD", "20000"))

# Minimum delay between re-dispatches of the same task to its assigned agent
# after a "finished without Review" session. Stops rapid-fire requeue loops
# when a model keeps ending its stream without calling tools.
REQUEUE_COOLDOWN_SECONDS = int(env("REQUEUE_COOLDOWN_SECONDS", "300"))

# Where Khidr keeps its git caches, task workspaces, per-run files and the
# agents' home directories (CLI logins).
KHIDR_DATA_DIR = Path(env("KHIDR_DATA_DIR", str(BASE_DIR / "data"))).expanduser()

# Default sandbox for agents that don't pick one: "docker" (a fresh container
# per run) or "local" (run the CLI directly on this host — no isolation).
KHIDR_SANDBOX = env("KHIDR_SANDBOX", "docker")
KHIDR_SANDBOX_IMAGE = env("KHIDR_SANDBOX_IMAGE", "khidr-sandbox:latest")
# Docker network for sandboxes (Compose: "khidr", so they reach http://web:8000)
# and the uid:gid they run as (blank = automatic, see sandbox.sandbox_user).
KHIDR_SANDBOX_NETWORK = env("KHIDR_SANDBOX_NETWORK", "")
KHIDR_SANDBOX_USER = env("KHIDR_SANDBOX_USER", "")

# How a sandboxed agent reaches this Khidr instance (the /mcp/ endpoint).
# From Docker, localhost/127.0.0.1 is rewritten to host.docker.internal.
KHIDR_INTERNAL_URL = env("KHIDR_INTERNAL_URL", "http://127.0.0.1:8000").rstrip("/")

# Turn budget per run, and how long an agent rests after its provider reports
# a rate or usage limit (subscriptions reset in windows of a few hours).
KHIDR_MAX_TURNS = int(env("KHIDR_MAX_TURNS", "200"))
KHIDR_RATE_LIMIT_COOLDOWN_SECONDS = int(env("KHIDR_RATE_LIMIT_COOLDOWN_SECONDS", "1800"))

# ---------------------------------------------------------------------------
# Web login (simple single-user auth for chat UI)
# ---------------------------------------------------------------------------

# bcrypt hash of the web UI password: `python manage.py set_password`.
WEB_PASSWORD_HASH = env("WEB_PASSWORD_HASH", "")

# Root URL for building chat links in notifications / card descriptions.
CHAT_BASE_URL = env("CHAT_BASE_URL", "http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------------------
# Telegram notifications (one-way: orchestrator → user)
# ---------------------------------------------------------------------------

TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = env("TELEGRAM_CHAT_ID", "")
