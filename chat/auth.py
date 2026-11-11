"""Single-user session auth backed by a bcrypt hash in settings."""

from __future__ import annotations

from functools import wraps
from urllib.parse import quote

import bcrypt
from django.conf import settings
from django.shortcuts import redirect


SESSION_KEY = "khidr_logged_in"


def hash_password(password: str) -> str:
    """Return a bcrypt hash for storage in WEB_PASSWORD_HASH."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str) -> bool:
    hashed = settings.WEB_PASSWORD_HASH
    if not hashed or not password:
        return False
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("ascii"))
    except Exception:  # noqa: BLE001
        return False


def is_authenticated(request) -> bool:
    return bool(request.session.get(SESSION_KEY))


def login_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not is_authenticated(request):
            return redirect(f"/login/?next={quote(request.get_full_path())}")
        return view(request, *args, **kwargs)
    return wrapper
