from django.apps import AppConfig


class RunnerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.runner"
    label = "runner"
    verbose_name = "Runner (Container auf dem Browser-Rechner)"
