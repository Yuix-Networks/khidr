"""
Generate a bcrypt hash for the chat UI login password.

    python manage.py set_password
    # (prompts for password; prints WEB_PASSWORD_HASH=<hash>)

Paste the resulting line into .env and restart the server.
"""

from __future__ import annotations

import getpass

from django.core.management.base import BaseCommand

from chat.auth import hash_password


class Command(BaseCommand):
    help = "Generate a bcrypt hash for WEB_PASSWORD_HASH in .env."

    def add_arguments(self, parser):
        parser.add_argument(
            "--password",
            help="Password to hash. If omitted, prompt interactively.",
        )

    def handle(self, *args, password=None, **options):
        if password is None:
            pw1 = getpass.getpass("Password: ")
            pw2 = getpass.getpass("Confirm:  ")
            if pw1 != pw2:
                self.stderr.write(self.style.ERROR("Passwords do not match."))
                return
            password = pw1
        if not password:
            self.stderr.write(self.style.ERROR("Empty password."))
            return
        hashed = hash_password(password)
        self.stdout.write("Add this line to .env:\n")
        self.stdout.write(self.style.SUCCESS(f"WEB_PASSWORD_HASH={hashed}"))
