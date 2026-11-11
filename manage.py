#!/usr/bin/env python
"""Django's command-line utility for administrative tasks."""
import os
import sys

from khidr.manage_helpers import settings_module_for


def main():
    """Run administrative tasks."""
    # `manage.py test` defaults to the SQLite test settings — the live
    # PostgreSQL server cannot host Django's test database from an agent
    # container (see khidr.manage_helpers). setdefault: an explicit
    # DJANGO_SETTINGS_MODULE in the environment always wins.
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', settings_module_for(sys.argv))
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Couldn't import Django. Are you sure it's installed and "
            "available on your PYTHONPATH environment variable? Did you "
            "forget to activate a virtual environment?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == '__main__':
    main()
