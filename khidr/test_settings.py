"""Django test settings: use SQLite so tests don't need a live PostgreSQL server."""
from __future__ import annotations

import os

# khidr.settings reads no .env.enc in a test run: the suite needs no secrets on the machine
os.environ["KHIDR_TESTS"] = "1"

from khidr.settings import *  # noqa: F403, F401

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# No GlitchTip token in tests. Both live call sites — `alerts.glitchtip_
# issue_first_seen` and the `get_glitchtip_issue` tool — read this, and
# with a token they reach the production API: a test that forgets to
# mock them then depends on a real issue's age, which is how this suite
# went red once (the webhook answered "not_new" instead of "created").
# A test that needs the lookup mocks it explicitly.
GLITCHTIP_API_KEY = ""
os.environ.pop("GLITCHTIP_API_KEY", None)
