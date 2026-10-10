from django.apps import AppConfig


class NodeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "multigpt.node"
    label = "node"
    verbose_name = "Knoten (API-Keys und MCP-Server)"

    def ready(self):
        # Eingebaute Chat-Werkzeuge index_status, start_reindex, start_scan, cancel_run.
        from . import chat_tools  # noqa: F401
