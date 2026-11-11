"""Seed alert ignore rules for obvious noise (client disconnects, shutdown)."""

from django.db import migrations

IGNORE = [
    (r"^BrokenPipeError\b", "client hung up mid-response"),
    (r"^ConnectionResetError\b", "client hung up mid-response"),
    (r"^CancelledError\b", "asyncio cancellation on shutdown/disconnect"),
]


def forward(apps, schema_editor):
    IgnoreRule = apps.get_model("core", "IgnoreRule")
    for pattern, note in IGNORE:
        IgnoreRule.objects.get_or_create(pattern=pattern, project=None, defaults={"note": note})


class Migration(migrations.Migration):
    dependencies = [("core", "0001_initial")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
