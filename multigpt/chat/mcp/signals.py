"""Sitzung schließen, wenn ein MCP-Server gelöscht wird.

Gilt nur für den Prozess, der das Löschen ausführt; andere Prozesse schließen
die Sitzung nach der Leerlaufzeit oder beim Beenden. Änderungen an einem Server
erkennt der Client selbst (Fingerabdruck der Verbindungsdaten).
"""

from django.db.models.signals import post_delete
from django.dispatch import receiver

from multigpt.chat.models import McpServer

from . import bridge


@receiver(post_delete, sender=McpServer, dispatch_uid="mcp_close_deleted_server")
def close_deleted_server(sender, instance, **kwargs):
    if bridge.is_running():
        from . import client

        bridge.submit(client.close_server(instance.pk))
