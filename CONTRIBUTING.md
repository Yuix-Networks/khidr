# Contributing to Khidr

Thanks for your interest! Bug reports, docs fixes and pull requests are all welcome.

## Development setup

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
python manage.py test
```

Tests run on SQLite and need no secrets or external services.

## Pull requests

- Keep each PR focused on one change, and add or update tests for behaviour changes.
- Run `python manage.py test` and `python ops/check_syntax.py` before pushing; CI runs both.
- If you change a model, include the migration (`python manage.py makemigrations`).
- Write code, comments and commit messages in English.

## Reporting security issues

Please do not open public issues for security problems. Use GitHub's
"Report a vulnerability" (private security advisory) on this repository instead.

## License

By contributing you agree that your contributions are licensed under the
Apache License 2.0.
