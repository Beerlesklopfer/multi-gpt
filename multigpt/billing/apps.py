from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.billing"
    label = "billing"
    verbose_name = "Kosten und Abrechnung"

    def ready(self):
        from . import signals  # noqa: F401
