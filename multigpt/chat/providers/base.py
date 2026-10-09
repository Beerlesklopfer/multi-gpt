"""Gemeinsame Schnittstelle aller Anbieter-Adapter (Plan Abschnitt 7, M3-01).

Ein Adapter liefert beim Streamen eine Folge von Events. Garantie für alle
Adapter: Das letzte Event ist genau ein ``Done`` oder ein ``Error``; Ausnahmen
werden intern zu ``Error`` und gelangen nie roh zum Aufrufer.
"""

from __future__ import annotations

import errno
import json
import re
import socket
import ssl
import uuid
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

import httpx

if TYPE_CHECKING:
    from multigpt.chat.models import Provider


# --- Events ------------------------------------------------------------------


@dataclass(frozen=True)
class Delta:
    """Ein Stück Antworttext."""

    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    """Das Modell möchte ein Werkzeug aufrufen (M4a).

    ``id`` ist immer gesetzt (fehlt sie beim Anbieter, vergibt der Adapter
    eine). ``provider_state`` ist undurchsichtig und anbieterspezifisch (z. B.
    Gemini ``thoughtSignature``); unverändert im Verlauf zurückgeben.
    """

    id: str
    name: str
    arguments: dict
    provider_state: dict | None = None


@dataclass(frozen=True)
class Usage:
    """Tokenverbrauch der Anfrage."""

    tokens_in: int
    tokens_out: int


@dataclass(frozen=True)
class Error:
    """Fehler; ``message`` ist ein deutscher Text für Nutzer, ohne Keys und Interna."""

    message: str
    retryable: bool = False


# Vereinheitlichter ``finish_reason``, wenn das Modell Werkzeuge aufrufen will.
FINISH_TOOL_CALLS = "tool_calls"


@dataclass(frozen=True)
class Done:
    """Regulärer Abschluss des Streams.

    ``finish_reason`` ist ``"tool_calls"``, wenn Werkzeugaufrufe (``ToolCallEvent``) kamen.
    ``provider_state`` ist der undurchsichtige, JSON-serialisierbare Zustand der
    Assistant-Antwort (Anthropic: Inhaltsblöcke mit thinking-Signaturen;
    Gemini: ``parts`` mit ``thoughtSignature``). Er gehört unverändert in
    ``ChatMessage.provider_state`` der Assistant-Nachricht dieser Antwort.
    """

    finish_reason: str | None = None
    provider_state: dict | None = None


Event = Delta | ToolCallEvent | Usage | Error | Done


# --- Werkzeuge und Nachrichten -----------------------------------------------

Role = Literal["user", "assistant", "system", "tool"]


@dataclass(frozen=True)
class ToolSpec:
    """Werkzeugdefinition, anbieterneutral (Plan 7, 8g).

    ``parameters`` ist ein JSON-Schema vom Typ ``object`` (wie MCP
    ``inputSchema``).
    """

    name: str
    description: str
    parameters: dict


@dataclass
class ChatMessage:
    """Eine Nachricht im Verlauf.

    - ``assistant``: ``content`` (Text, darf leer sein) und optional
      ``tool_calls`` sowie ``provider_state`` aus ``Done.provider_state``.
    - ``tool``: Ergebnis eines Werkzeugaufrufs; ``content`` ist der
      Ergebnistext, ``tool_call_id`` verweist auf ``ToolCallEvent.id``,
      ``name`` ist der Werkzeugname, ``is_error`` markiert Fehlschläge.
    """

    role: Role
    content: str = ""
    tool_calls: list[ToolCallEvent] = field(default_factory=list)
    tool_call_id: str | None = None
    name: str | None = None
    is_error: bool = False
    provider_state: dict | None = None


def normalize_tools(tools: Iterable[ToolSpec | dict] | None) -> list[ToolSpec]:
    """``ToolSpec`` oder Dicts (``parameters``/``input_schema``/``inputSchema``)
    in eine Liste von ``ToolSpec`` überführen."""
    result: list[ToolSpec] = []
    for tool in tools or []:
        if isinstance(tool, ToolSpec):
            result.append(tool)
            continue
        if not isinstance(tool, dict):
            raise ProviderError("Ungültige Werkzeugdefinition.")
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            tool = tool["function"]  # OpenAI-Form
        name = str(tool.get("name") or "")
        if not name:
            raise ProviderError("Werkzeugdefinition ohne Namen.")
        schema = tool.get("parameters") or tool.get("input_schema") or tool.get("inputSchema")
        result.append(
            ToolSpec(
                name=name,
                description=str(tool.get("description") or ""),
                parameters=dict(schema) if isinstance(schema, dict) else {},
            )
        )
    return result


def tool_schema(spec: ToolSpec) -> dict:
    """JSON-Schema der Parameter; leer oder ohne ``type`` -> Objekt ohne Felder."""
    schema = dict(spec.parameters or {})
    schema.setdefault("type", "object")
    if schema["type"] == "object":
        schema.setdefault("properties", {})
    return schema


def new_tool_call_id() -> str:
    """Eigene ID für Werkzeugaufrufe ohne Anbieter-ID (Gemini, manche lokale Server)."""
    return f"call_{uuid.uuid4().hex[:24]}"


MSG_TOOL_ARGUMENTS = "Das Modell hat ungültige Argumente für das Werkzeug „{name}“ geliefert."
MSG_TOOL_NO_RESULT = "Kein Ergebnis vorhanden: Das Werkzeug wurde nicht ausgeführt."


def parse_tool_arguments(raw: str | None, name: str) -> dict | Error:
    """Gestreamte Argumente (JSON-Text) als Dict; leer -> ``{}``; sonst ``Error``."""
    if raw is None or not raw.strip():
        return {}
    try:
        value = json.loads(raw)
    except ValueError:
        return Error(MSG_TOOL_ARGUMENTS.format(name=name or "?"), retryable=True)
    if not isinstance(value, dict):
        return Error(MSG_TOOL_ARGUMENTS.format(name=name or "?"), retryable=True)
    return value


def pair_tool_messages(messages: list[ChatMessage]) -> list[ChatMessage]:
    """Werkzeugaufrufe und -ergebnisse paaren, wie es alle drei APIs verlangen.

    - Ergebnisse (Rolle ``tool``) gelten nur direkt nach der Assistant-Nachricht
      mit dem passenden Aufruf; verwaiste Ergebnisse fallen weg, doppelte auch.
    - Fehlt zu einem Aufruf das Ergebnis, wird es als Fehler ergänzt
      (``MSG_TOOL_NO_RESULT``), bevor der Verlauf weitergeht.
    - Ergebnisse stehen in der Reihenfolge der Aufrufe.
    """
    result: list[ChatMessage] = []
    i = 0
    while i < len(messages):
        message = messages[i]
        i += 1
        if message.role == "tool":
            continue  # verwaist (kein direkt vorangehender Aufruf)
        result.append(message)
        if message.role != "assistant" or not message.tool_calls:
            continue
        found: dict[str, ChatMessage] = {}
        while i < len(messages) and messages[i].role == "tool":
            tool_msg = messages[i]
            i += 1
            if tool_msg.tool_call_id and tool_msg.tool_call_id not in found:
                found[tool_msg.tool_call_id] = tool_msg
        for call in message.tool_calls:
            result.append(
                found.get(call.id)
                or ChatMessage(
                    "tool",
                    MSG_TOOL_NO_RESULT,
                    tool_call_id=call.id,
                    name=call.name,
                    is_error=True,
                )
            )
    return result


@dataclass
class Turn:
    """Ein Zug im streng abwechselnden Verlauf (Anthropic, Gemini).

    ``role`` ist ``user`` oder ``assistant``; Werkzeugergebnisse gehören zum
    ``user``-Zug und stehen dort vor dem Text.
    """

    role: Literal["user", "assistant"]
    messages: list[ChatMessage]

    @property
    def tool_results(self) -> list[ChatMessage]:
        return [m for m in self.messages if m.role == "tool"]

    @property
    def text(self) -> str:
        return "\n\n".join(
            m.content for m in self.messages if m.role != "tool" and (m.content or "").strip()
        )


def sse_data(line: str) -> str | None:
    """Inhalt einer SSE-``data:``-Zeile, sonst None (Leerzeile, Kommentar,
    ``event:``/``id:``/``retry:``)."""
    line = line.rstrip("\r")
    if not line or line.startswith(":"):
        return None
    name, _, value = line.partition(":")
    if name != "data":
        return None
    return value.removeprefix(" ")


def alternate_turns(messages: list[ChatMessage]) -> tuple[list[ChatMessage], list[str]]:
    """Verlauf für APIs mit streng abwechselnden Rollen (Anthropic, Gemini).

    - ``system``-Nachrichten werden herausgelöst und als zweiter Wert geliefert
      (die APIs haben dafür einen eigenen Parameter).
    - Leere Nachrichten fallen weg; aufeinanderfolgende Nachrichten derselben
      Rolle werden mit Leerzeile zusammengefasst (z. B. zwei Nutzernachrichten,
      wenn die Antwort dazwischen fehlerhaft war).
    - Der Verlauf beginnt mit ``user``: führende Assistant-Nachrichten fallen weg.
    - ``tool``-Nachrichten und Werkzeugaufrufe werden ausgelassen; mit
      Werkzeugen gilt ``group_turns``.
    """
    turns: list[ChatMessage] = []
    system_parts: list[str] = []
    for message in messages:
        text = message.content or ""
        if message.role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if message.role not in ("user", "assistant") or not text.strip():
            continue
        if not turns and message.role != "user":
            continue
        if turns and turns[-1].role == message.role:
            turns[-1] = ChatMessage(message.role, f"{turns[-1].content}\n\n{text}")
        else:
            turns.append(ChatMessage(message.role, text))
    return turns, system_parts


def group_turns(messages: list[ChatMessage]) -> tuple[list[Turn], list[str]]:
    """Wie ``alternate_turns``, aber mit Werkzeugaufrufen und -ergebnissen.

    - ``system`` wird herausgelöst, Paarung über ``pair_tool_messages``.
    - Werkzeugergebnisse zählen zum ``user``-Zug.
    - Leere Nachrichten ohne Werkzeugaufruf fallen weg; aufeinanderfolgende
      Nachrichten derselben Rolle bilden einen Zug.
    - Der Verlauf beginnt mit ``user``: führende Assistant-Züge fallen samt
      ihren Ergebnissen weg.
    """
    turns: list[Turn] = []
    system_parts: list[str] = []
    for message in pair_tool_messages(messages):
        text = message.content or ""
        if message.role == "system":
            if text.strip():
                system_parts.append(text.strip())
            continue
        if message.role == "assistant":
            if not text.strip() and not message.tool_calls and not message.provider_state:
                continue
            role = "assistant"
        elif message.role == "user":
            if not text.strip():
                continue
            role = "user"
        elif message.role == "tool":
            # Folgt immer direkt auf einen Assistant-Zug mit Aufrufen
            # (pair_tool_messages). Fiel der als führender Zug weg, ist
            # ``turns`` noch leer und das Ergebnis fällt mit.
            if not turns:
                continue
            role = "user"
        else:
            continue
        if not turns and role != "user":
            continue
        if turns and turns[-1].role == role:
            turns[-1].messages.append(message)
        else:
            turns.append(Turn(role, [message]))
    return turns, system_parts


# --- Fehlertexte -------------------------------------------------------------

MSG_UNREACHABLE = "Der Anbieter ist nicht erreichbar."
MSG_TIMEOUT = "Der Anbieter hat nicht rechtzeitig geantwortet."
MSG_INTERRUPTED = "Die Verbindung zum Anbieter wurde während der Antwort unterbrochen."
MSG_UNEXPECTED = "Unerwarteter Fehler bei der Anfrage an den Anbieter."
MSG_STREAM_ERROR = "Der Anbieter hat die Antwort mit einem Fehler abgebrochen."


def http_error_message(status: int) -> tuple[str, bool]:
    """Deutscher Text und ``retryable`` zu einem HTTP-Status des Anbieters."""
    if status in (401, 403):
        return "Der Anbieter hat den Zugang abgelehnt. Bitte den API-Key prüfen.", False
    if status == 402:
        return "Beim Anbieter ist kein Guthaben mehr vorhanden.", False
    if status == 404:
        return "Modell oder Adresse beim Anbieter nicht gefunden.", False
    if status == 429:
        return (
            "Der Anbieter meldet zu viele Anfragen oder ein erschöpftes Kontingent. "
            "Bitte später erneut versuchen.",
            True,
        )
    if status >= 500:
        return "Der Anbieter hat einen Serverfehler gemeldet. Bitte später erneut versuchen.", True
    return f"Der Anbieter hat die Anfrage abgelehnt (HTTP {status}).", False


def exception_to_error(exc: Exception, *, started: bool) -> Error:
    """Netz- und sonstige Ausnahmen in ein ``Error``-Event übersetzen."""
    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        return Error(MSG_UNREACHABLE, retryable=True)
    if isinstance(exc, httpx.TimeoutException):
        return Error(MSG_TIMEOUT, retryable=True)
    if isinstance(exc, httpx.TransportError):
        # RemoteProtocolError, ReadError, ... – meist ein abgerissener Stream.
        return Error(MSG_INTERRUPTED if started else MSG_UNREACHABLE, retryable=True)
    if isinstance(exc, ProviderError):
        return Error(str(exc), retryable=False)
    return Error(MSG_UNEXPECTED, retryable=False)


# --- Fehler und Basisklasse --------------------------------------------------


class ProviderError(Exception):
    """Fehler bei Konfiguration oder Aufruf eines Anbieters (Text ohne Keys).

    ``retryable``: Ein späterer Versuch kann gelingen (Netz, 429, 5xx).
    ``unreachable``: Der Anbieter war gar nicht erreichbar (Verbindung
    abgelehnt, Verbindungsaufbau zu langsam) – z. B. LM-Studio-PC aus.
    """

    def __init__(self, message: str = "", *, retryable: bool = False, unreachable: bool = False):
        super().__init__(message)
        self.retryable = retryable or unreachable
        self.unreachable = unreachable


def is_unreachable(exc: BaseException) -> bool:
    """Verbindung kam nicht zustande (Anbieter aus oder nicht im Netz)."""
    return isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout)


class ProviderHTTPError(ProviderError):
    """``ProviderError`` mit dem HTTP-Status der Anbieterantwort (für ``check``)."""

    def __init__(self, message: str, status: int, *, retryable: bool = False, code: str = ""):
        super().__init__(message, retryable=retryable)
        self.status = status
        # Maschinenlesbarer Fehlercode des Anbieters (z. B. ``insufficient_permissions``),
        # nur für die Anzeige im Admin; nie der Fehlertext (kann Key-Teile enthalten).
        self.code = safe_error_code(code)


_SAFE_CODE = re.compile(r"[A-Za-z0-9_.-]{1,60}")


def safe_error_code(value) -> str:
    """Nur unbedenkliche Kurzcodes durchlassen (Buchstaben, Ziffern, ``_.-``)."""
    text = str(value or "").strip()
    return text if _SAFE_CODE.fullmatch(text) else ""


def provider_error_code(body: bytes) -> str:
    """Fehlercode aus einer JSON-Fehlerantwort (OpenAI ``error.code``/``type``,
    Anthropic ``error.type``, Google ``error.status``/``details[].reason``)."""
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return ""
    for detail in err.get("details") or []:
        if isinstance(detail, dict) and safe_error_code(detail.get("reason")):
            return safe_error_code(detail.get("reason"))
    for key in ("code", "status", "type"):
        code = safe_error_code(err.get(key))
        if code:
            return code
    return ""


# --- Verbindungsprüfung --------------------------------------------------------


@dataclass(frozen=True)
class CheckResult:
    """Ergebnis von ``ProviderAdapter.check``.

    ``error`` ist bei ``online=False`` ein deutscher Text der Form
    „Kurzursache: Einzelheiten“ – ohne Key, ohne Rohtext des Anbieters.
    """

    online: bool
    models: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def short_error(self) -> str:
        return short_error(self.error)


def short_error(error: str | None) -> str:
    """Kurzursache vor dem ersten „: “ (für knappe Anzeigen)."""
    return (error or "").split(": ", 1)[0]


CHECK_REFUSED = (
    "Verbindung abgelehnt: {endpoint} nimmt keine Verbindung an. Läuft der Dienst, stimmt der Port?"
)
CHECK_DNS = "Rechnername unbekannt: {host} lässt sich nicht auflösen. Bitte die Basis-URL prüfen."
CHECK_UNREACHABLE = (
    "Rechner nicht erreichbar: Keine Verbindung zu {endpoint}. "
    "Ist der Rechner eingeschaltet und im Netz?"
)
CHECK_CONNECT = "Keine Verbindung: {endpoint} ist nicht erreichbar."
CHECK_TLS = (
    "TLS-Fehler: Die sichere Verbindung zu {endpoint} ist fehlgeschlagen "
    "(Zertifikat oder http/https verwechselt?)."
)
CHECK_CONNECT_TIMEOUT = (
    "Zeitüberschreitung: {endpoint} antwortet nicht auf den Verbindungsaufbau "
    "(Rechner aus oder Firewall?)."
)
CHECK_READ_TIMEOUT = "Zeitüberschreitung: {endpoint} hat nicht rechtzeitig geantwortet."
CHECK_INTERRUPTED = "Verbindung abgebrochen: {endpoint} hat die Verbindung unerwartet beendet."
CHECK_BAD_URL = "Ungültige Basis-URL: Bitte die Adresse prüfen (z. B. http://rechner:1234/v1)."
CHECK_AUTH = "Zugang abgelehnt (HTTP {status}): Bitte den API-Key prüfen."
CHECK_PAYMENT = "Kein Guthaben (HTTP 402): Beim Anbieter ist kein Guthaben mehr vorhanden."
CHECK_NOT_FOUND = (
    "Adresse nicht gefunden (HTTP 404): Bitte die Basis-URL prüfen – fehlt z. B. „/v1“?"
)
CHECK_RATE_LIMIT = (
    "Zu viele Anfragen (HTTP 429): Limit oder Kontingent erschöpft. Später erneut prüfen."
)
CHECK_SERVER = "Serverfehler beim Anbieter (HTTP {status}): Später erneut prüfen."
CHECK_HTTP_OTHER = "Anfrage abgelehnt (HTTP {status}): {message}"
CHECK_INVALID = (
    "Ungültige Antwort: Der Anbieter hat keine verwertbare Modellliste geliefert. "
    "Ist die Basis-URL richtig?"
)
CHECK_UNSUPPORTED = "Keine Modellliste: Dieser Anbietertyp kann keine Modelle melden."
CHECK_UNEXPECTED = "Unerwarteter Fehler: Die Prüfung ist fehlgeschlagen."

_REFUSED_TEXT = ("connection refused", "verbindungsaufbau abgelehnt")
_DNS_TEXT = (
    "name or service not known",
    "nodename nor servname",
    "temporary failure in name resolution",
    "no address associated",
    "getaddrinfo failed",
)
_UNREACHABLE_TEXT = ("no route to host", "network is unreachable", "host is unreachable")
_UNREACHABLE_ERRNOS = {errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EHOSTDOWN, errno.ENETDOWN}


def endpoint_of(url: str) -> tuple[str, str]:
    """(Host, „Host:Port“) einer URL – ohne Zugangsdaten, Pfad oder Query."""
    try:
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or {"https": 443, "http": 80}.get(parts.scheme)
    except ValueError:
        host, port = "", None
    if not host:
        return "der Anbieter", "der Anbieter"
    shown = f"[{host}]" if ":" in host else host
    return host, f"{shown}:{port}" if port else shown


def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _connect_cause(exc: BaseException) -> str:
    """Ursache eines Verbindungsfehlers: refused, dns, unreachable, tls oder other."""
    for item in _exception_chain(exc):
        if isinstance(item, ssl.SSLError):
            return "tls"
        if isinstance(item, socket.gaierror):
            return "dns"
        if isinstance(item, ConnectionRefusedError):
            return "refused"
        if isinstance(item, OSError) and item.errno in _UNREACHABLE_ERRNOS:
            return "unreachable"
    text = " ".join(str(item) for item in _exception_chain(exc)).lower()
    if any(t in text for t in _REFUSED_TEXT):
        return "refused"
    if any(t in text for t in _DNS_TEXT):
        return "dns"
    if any(t in text for t in _UNREACHABLE_TEXT):
        return "unreachable"
    if "ssl" in text or "certificate" in text or "tls" in text:
        return "tls"
    return "other"


def check_error_message(exc: BaseException, url: str) -> str:
    """Deutsche Ursache zu einer Ausnahme beim Abruf der Modellliste.

    Nennt Host:Port, aber nie den Key oder den Fehlertext des Anbieters.
    """
    host, endpoint = endpoint_of(url)
    if isinstance(exc, ProviderHTTPError):
        status = exc.status
        if status in (401, 403):
            message = CHECK_AUTH.format(status=status)
            return f"{message} (Code des Anbieters: {exc.code})" if exc.code else message
        if status == 402:
            return CHECK_PAYMENT
        if status == 404:
            return CHECK_NOT_FOUND
        if status == 429:
            return CHECK_RATE_LIMIT
        if status >= 500:
            return CHECK_SERVER.format(status=status)
        return CHECK_HTTP_OTHER.format(status=status, message=str(exc))
    if isinstance(exc, ProviderError):
        if str(exc) == MSG_INVALID_MODEL_LIST:
            return CHECK_INVALID
        return str(exc)  # eigene, deutsche Texte der Adapter
    if isinstance(exc, NotImplementedError):
        return CHECK_UNSUPPORTED
    if isinstance(exc, httpx.ConnectTimeout):
        return CHECK_CONNECT_TIMEOUT.format(endpoint=endpoint)
    if isinstance(exc, httpx.TimeoutException):
        return CHECK_READ_TIMEOUT.format(endpoint=endpoint)
    if isinstance(exc, httpx.ConnectError):
        cause = _connect_cause(exc)
        if cause == "refused":
            return CHECK_REFUSED.format(endpoint=endpoint)
        if cause == "dns":
            return CHECK_DNS.format(host=host)
        if cause == "unreachable":
            return CHECK_UNREACHABLE.format(endpoint=endpoint)
        if cause == "tls":
            return CHECK_TLS.format(endpoint=endpoint)
        return CHECK_CONNECT.format(endpoint=endpoint)
    if isinstance(exc, httpx.InvalidURL | httpx.UnsupportedProtocol):
        return CHECK_BAD_URL
    if isinstance(exc, httpx.TransportError):
        return CHECK_INTERRUPTED.format(endpoint=endpoint)
    return CHECK_UNEXPECTED


MSG_INVALID_MODEL_LIST = "Der Anbieter hat eine unerwartete Modellliste geliefert."


class ProviderAdapter:
    """Basisklasse. Nicht unterstützte Fähigkeiten werfen ``NotImplementedError``."""

    @classmethod
    def default_base_url(cls) -> str:
        """Standard-Basis-URL des Anbieters (Modulkonstante ``DEFAULT_BASE_URL``)."""
        import sys

        return getattr(sys.modules[cls.__module__], "DEFAULT_BASE_URL", "")

    def __init__(self, provider: Provider):
        self.provider = provider

    def stream(
        self,
        model_id: str,
        messages: list[ChatMessage],
        system: str | None = None,
        tools: list[ToolSpec | dict] | None = None,
        **params,
    ) -> Iterator[Event]:
        """Antwort als Events; letztes Event ist genau ein ``Done`` oder ``Error``.

        ``tools``: angebotene Werkzeuge. ``params["tool_choice"]``: ``"auto"``,
        ``"none"``, ``"required"`` oder ein Werkzeugname (erzwingt dieses).
        """
        raise NotImplementedError

    def list_models(self, timeout: float | None = None) -> list[str]:
        """Modell-IDs des Anbieters. ``timeout`` in Sekunden (None: Standard des
        Adapters). Fehler als ``ProviderError``."""
        raise NotImplementedError

    def _fetch_models(self, timeout: httpx.Timeout | float) -> list[str]:
        """Modellliste ohne Übersetzung der Netzfehler (für ``check``).

        Adapter mit eigenem HTTP-Abruf überschreiben das; die Vorgabe nutzt
        ``list_models`` (dann sind nur dessen Texte verfügbar).
        """
        return self.list_models(timeout=timeout)

    def check(self, timeout: float = 2) -> CheckResult:
        """Modellliste abrufen und das Ergebnis samt Ursache liefern; nie eine Ausnahme."""
        try:
            models = self._fetch_models(timeout)
        except Exception as exc:
            url = getattr(self, "base_url", "") or getattr(self.provider, "base_url", "") or ""
            return CheckResult(online=False, error=check_error_message(exc, url))
        return CheckResult(online=True, models=list(dict.fromkeys(models)))

    def is_online(self, timeout: float = 2) -> bool:
        """Kurzer Abruf der Modellliste; True/False, nie eine Ausnahme."""
        try:
            self.list_models(timeout=timeout)
        except Exception:
            return False
        return True

    def embed(
        self, model_id: str, texts: list[str], dimensions: int | None = None
    ) -> list[list[float]]:
        """Ein Vektor je Text, gleiche Reihenfolge. ``dimensions``: gewünschte
        Länge (nur Modelle, die das können). Fehler als ``ProviderError``."""
        raise NotImplementedError

    def describe_image(
        self,
        model_id: str,
        image: bytes,
        prompt: str,
        *,
        mime_type: str = "image/png",
        **params,
    ) -> str:
        """Ein Bild mit Textanweisung an ein Vision-Modell, nicht streamend
        (z. B. olmOCR für gescannte Seiten). Liefert den Antworttext.

        ``params``: z. B. ``temperature``, ``max_tokens``. Fehler als
        ``ProviderError`` (deutscher Text ohne Key und ohne Rohtext des Anbieters).
        """
        raise NotImplementedError

    def transcribe(self, model_id: str, audio):
        raise NotImplementedError

    def speak(self, model_id: str, text: str, voice: str | None = None):
        raise NotImplementedError

    def generate_image(self, model_id: str, prompt: str, **params):
        raise NotImplementedError

    def edit_image(self, model_id: str, image, prompt: str, **params):
        raise NotImplementedError

    def __repr__(self):
        # Bewusst ohne Key oder URL-Interna.
        return f"<{type(self).__name__} provider={getattr(self.provider, 'pk', None)}>"
