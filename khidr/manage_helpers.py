"""
Which Django settings module should `manage.py` use?

Kept in its own module (instead of inline in `manage.py`) so the decision
can be unit-tested without booting Django.
"""
from __future__ import annotations

# The production settings: PostgreSQL on the host.
DEFAULT_SETTINGS_MODULE = "khidr.settings"

# The test settings: an in-memory SQLite database, no server needed.
TEST_SETTINGS_MODULE = "khidr.test_settings"

# Commands that need a throw-away database.
TEST_COMMANDS = ("test", "testserver")


def settings_module_for(argv) -> str:
    """Return the settings module `manage.py` should use for `argv`.

    The test commands run against an in-memory SQLite database
    (`khidr.test_settings`). The live PostgreSQL server cannot host the
    test suite: the `khidr` role has no CREATEDB privilege and the host's
    pg_hba.conf only allows the `khidr` database from the container
    network, so Django's test database (`test_khidr`) can neither be
    created nor reached from an agent container — it dies with

        FATAL:  no pg_hba.conf entry for host "172.18.0.2", user "khidr",
        database "test_khidr", SSL encryption

    (GlitchTip #39521.) Every other command keeps the production
    settings, and an explicit DJANGO_SETTINGS_MODULE in the environment
    always wins — `manage.py` only *defaults* the variable.
    """
    if len(argv) > 1 and argv[1] in TEST_COMMANDS:
        return TEST_SETTINGS_MODULE
    return DEFAULT_SETTINGS_MODULE
