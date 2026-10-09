from django.apps import AppConfig


class KontenConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.konten"
    label = "konten"
    verbose_name = "Konten und Gruppen"
