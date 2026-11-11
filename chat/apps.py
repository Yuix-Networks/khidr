from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "chat"

    def ready(self):
        # Wire post_save -> Channels group broadcast.
        from . import signals  # noqa: F401
