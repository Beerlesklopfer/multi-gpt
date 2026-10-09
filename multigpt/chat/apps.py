from django.apps import AppConfig


class ChatConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.chat"
    label = "chat"
    verbose_name = "Chat"

    def ready(self):
        from .mcp import signals  # noqa: F401 - registriert Signal-Empfänger
