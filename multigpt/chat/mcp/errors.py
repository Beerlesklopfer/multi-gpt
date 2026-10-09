"""Fehlerklassen des MCP-Clients.

Meldungen sind deutsch und enthalten nie Zugangsdaten (sie können im Chat und
im Protokoll erscheinen).
"""


class McpError(Exception):
    """Ein MCP-Server ist nicht erreichbar, bricht ab oder antwortet fehlerhaft."""


class McpTimeout(McpError):
    """Ein Aufruf hat das Zeitlimit überschritten und wurde abgebrochen."""
