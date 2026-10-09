"""Verbindungsdaten eines MCP-Servers, im Request-Thread aus der DB gelesen.

Format der Zugangsdaten (``McpServer.credentials``, verschlüsselt gespeichert):

* leer: keine Zugangsdaten.
* JSON-Objekt mit den Schlüsseln
  - ``"env"``: Objekt Name → Wert, Umgebungsvariablen für den ``stdio``-Prozess
    (zusätzlich zur minimalen Grundumgebung des SDK: HOME, PATH, …),
  - ``"headers"``: Objekt Name → Wert, HTTP-Header für Streamable HTTP,
  - ``"bearer_token"``: Text, ergibt den Header ``Authorization: Bearer …`` (nur HTTP).
* Kurzform: ein JSON-Objekt nur aus Texten ohne diese Schlüssel gilt bei
  ``stdio`` als ``env``, bei HTTP als ``headers``.
* Kurzform: reiner Text (kein JSON) gilt bei HTTP als Bearer-Token.

Der ``stdio``-Befehl kommt ausschließlich aus der DB (nur Verwalter legen
Server an) und wird mit :func:`shlex.split` zerlegt – keine Shell.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field

from .errors import McpError

STRUCTURED_KEYS = {"env", "headers", "bearer_token"}
DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True)
class Credentials:
    env: dict[str, str] = field(default_factory=dict, repr=False)
    headers: dict[str, str] = field(default_factory=dict, repr=False)


def _str_map(value, label: str) -> dict[str, str]:
    if not isinstance(value, dict) or not all(
        isinstance(k, str) and k and isinstance(v, str) for k, v in value.items()
    ):
        raise ValueError(f"„{label}“ muss ein JSON-Objekt aus Texten sein.")
    return dict(value)


def parse_credentials(raw: str, transport: str) -> Credentials:
    """Zerlegt die Zugangsdaten; ``ValueError`` mit deutscher Meldung bei Formfehlern.

    Die Meldung enthält nie den Inhalt der Zugangsdaten.
    """
    raw = (raw or "").strip()
    if not raw:
        return Credentials()
    try:
        data = json.loads(raw)
    except ValueError:
        if transport == "http":
            return Credentials(headers={"Authorization": f"Bearer {raw}"})
        raise ValueError(
            'Zugangsdaten für stdio müssen ein JSON-Objekt sein, z. B. {"env": {"API_KEY": "…"}}.'
        ) from None
    if not isinstance(data, dict):
        raise ValueError("Zugangsdaten müssen ein JSON-Objekt sein.")
    if data and set(data) <= STRUCTURED_KEYS:
        env = _str_map(data.get("env", {}), "env")
        headers = _str_map(data.get("headers", {}), "headers")
        token = data.get("bearer_token")
        if token is not None:
            if not isinstance(token, str) or not token:
                raise ValueError("„bearer_token“ muss ein nicht leerer Text sein.")
            headers["Authorization"] = f"Bearer {token}"
        if transport == "stdio" and headers:
            raise ValueError("HTTP-Header und Token gelten nur für den Transport HTTP.")
        if transport == "http" and env:
            raise ValueError("Umgebungsvariablen gelten nur für den Transport stdio.")
        for name in headers:
            if any(c in name for c in " :\r\n") or any(c in headers[name] for c in "\r\n"):
                raise ValueError("Ungültiger HTTP-Header in den Zugangsdaten.")
        return Credentials(env=env, headers=headers)
    flat = _str_map(data, "Zugangsdaten")
    if transport == "http":
        return Credentials(headers=flat)
    return Credentials(env=flat)


def split_command(command: str) -> list[str]:
    try:
        parts = shlex.split(command or "")
    except ValueError:
        raise ValueError("Der Befehl ist nicht lesbar (Anführungszeichen prüfen).") from None
    if not parts:
        raise ValueError("Für stdio fehlt der Befehl.")
    return parts


@dataclass(frozen=True)
class ServerConfig:
    """Unveränderliche Momentaufnahme eines ``McpServer`` für den Loop-Thread."""

    pk: int
    name: str
    transport: str
    argv: tuple[str, ...]
    url: str
    # repr=False: Zugangsdaten nie in Logs oder Tracebacks.
    env: tuple[tuple[str, str], ...] = field(repr=False)
    headers: tuple[tuple[str, str], ...] = field(repr=False)
    timeout: float = DEFAULT_TIMEOUT

    @property
    def fingerprint(self) -> str:
        """Ändert sich, sobald sich etwas an der Verbindung ändert (dann neu verbinden)."""
        material = json.dumps(
            [self.transport, self.argv, self.url, self.env, self.headers], sort_keys=True
        )
        return hashlib.sha256(material.encode()).hexdigest()

    @classmethod
    def from_model(cls, server) -> ServerConfig:
        if not server.active:
            raise McpError(f"Der MCP-Server „{server.name}“ ist deaktiviert.")
        try:
            creds = parse_credentials(server.credentials, server.transport)
            argv: tuple[str, ...] = ()
            if server.transport == "stdio":
                argv = tuple(split_command(server.command))
            elif server.transport == "http":
                if not server.url:
                    raise ValueError("Für HTTP fehlt die URL.")
            else:
                raise ValueError("Unbekannter Transport.")
        except ValueError as exc:
            raise McpError(f"MCP-Server „{server.name}“ ist falsch eingerichtet: {exc}") from None
        timeout = float(getattr(server, "timeout_seconds", None) or DEFAULT_TIMEOUT)
        return cls(
            pk=server.pk,
            name=server.name,
            transport=server.transport,
            argv=argv,
            url=server.url,
            env=tuple(sorted(creds.env.items())),
            headers=tuple(sorted(creds.headers.items())),
            timeout=timeout,
        )
