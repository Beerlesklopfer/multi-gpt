from django.apps import AppConfig


class ScratchpadConfig(AppConfig):
    name = "multigpt.scratchpad"
    verbose_name = "Scratchpad"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import signals  # noqa: F401
