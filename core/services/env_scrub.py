"""
Env-var sanitizer for subprocesses spawned by agents.

The orchestrator daemon itself runs with DJANGO_SETTINGS_MODULE=khidr.settings
and a pile of other project secrets in its environment. When we spawn an
agent's Bash subprocess, we DON'T want it to inherit those — otherwise
a command like

    python manage.py check

inside a project suddenly tries to import khidr.settings (from
our own env) and blows up with ModuleNotFoundError. Same risk for leaked
API keys showing up in the agent's shell env without anyone asking.

`sanitized_env()` returns a copy of `os.environ` with all orchestrator-
owned variables stripped. Agents still get PATH / HOME / USER / PYENV_ROOT
/ SHELL / LANG / TERM / etc., which is what you actually want.
"""

from __future__ import annotations

import os

# Exact-match drops. Covers Django, DB, API keys, Telegram, Sentry, web login
# secrets, and orchestrator-specific tuning knobs.
_SCRUB_EXACT: frozenset[str] = frozenset({
    # Django
    "DJANGO_SETTINGS_MODULE",
    "DJANGO_DEBUG",
    "DJANGO_SECRET_KEY",
    "DJANGO_ALLOWED_HOSTS",
    # Postgres / DB
    "PG_USER", "PG_PASS", "PG_HOST", "PG_PORT", "DATABASE_URL",
    # Agent / provider API keys
    "OPENROUTER_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
    "DASHSCOPE_API_KEY", "LOCAL_API_KEY", "NIM_API_KEY",
    "MOONSHOT_API_KEY", "DEEPSEEK_API_KEY", "GLM_API_KEY",
    # Telegram
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    # Sentry
    "SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_TRACES_SAMPLE_RATE",
    # Web login
    "WEB_PASSWORD_HASH", "WEB_SESSION_SECRET",
    # Orchestrator tuning
    "CHAT_BASE_URL",
    "HEARTBEAT_TIMEOUT_SECONDS", "REQUEUE_COOLDOWN_SECONDS",
    "SUMMARIZE_THRESHOLD",
    "TIME_ZONE",
})

# Drop anything starting with these (covers KHIDR_TASK_ID, custom dev prefixes).
_SCRUB_PREFIXES: tuple[str, ...] = (
    "KHIDR_",
    "ORCHESTRATOR_",
)


def sanitized_env(overrides: dict[str, str] | None = None) -> dict[str, str]:
    """
    Return os.environ minus orchestrator-owned variables, optionally
    overlaid with explicit `overrides` for things we *do* want to pass.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in _SCRUB_EXACT
        and not any(k.startswith(p) for p in _SCRUB_PREFIXES)
    }
    if overrides:
        env.update(overrides)
    return env
