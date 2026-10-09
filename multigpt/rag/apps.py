from django.apps import AppConfig


class RagConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.rag"
    label = "rag"
    verbose_name = "Dokumente (RAG)"
