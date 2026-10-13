"""Runner-Agent von MultiGPT (M14-08/09).

Läuft als Container auf dem Rechner, an dem der Browser läuft, und steuert dort
über den Podman-Socket wegwerfbare Arbeitscontainer. Zum Server baut er nur eine
ausgehende WebSocket-Verbindung auf (``wss://<host>/runner/ws``). Ohne Django;
``protocol.py`` ist eine unveränderte Kopie von ``multigpt/runner/protocol.py``.
"""

VERSION = "0.1.0"
