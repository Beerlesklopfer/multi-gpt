"""Chat-Ablauf (M3): Verlauf aufbauen, System-Prompt, Adapter aufrufen,
Antwort speichern, Kosten berechnen.

Entscheidungen:

- **Zwischenstatus:** Die Assistant-Nachricht wird vor dem Anbieteraufruf mit
  ``status=aborted`` angelegt und erst am Ende auf ``complete``/``error``
  gesetzt. Stirbt der Prozess mitten im Stream (Worker-Timeout, Neustart),
  bleibt sie korrekt als abgebrochen stehen – ohne eigenen Status "läuft"
  im Modell. Folge: Wer die Seite während einer laufenden Antwort neu lädt,
  sieht sie als "abgebrochen". Das stimmt in aller Regel: Mit dem Neuladen
  bricht der Browser den fetch ab, der Server erkennt das beim nächsten
  Delta und speichert endgültig ``aborted``. Nur ein zweiter Tab sähe eine
  tatsächlich noch laufende Antwort vorläufig als abgebrochen.
- **Text nach Abbruch:** Der Server erkennt den Abbruch erst, wenn das
  Schreiben eines Deltas scheitert. Dieses Delta (und ggf. bereits im
  Sendepuffer liegende) ist schon gespeichert, hat den Browser aber nicht
  mehr erreicht. Nach dem Neuladen kann die Antwort daher etwas länger sein
  als zuvor angezeigt – gewollt, es geht kein empfangener Text verloren.
- **Verlauf:** Nachrichten mit ``complete`` und ``aborted`` (mit Inhalt) gehen
  an das Modell. Abgebrochene Antworten hat der Nutzer gesehen; der weitere
  Chat bezieht sich darauf. Antworten mit ``error`` und leere Nachrichten
  bleiben draußen (sie enthalten keine verwertbare Antwort).
- **Versionen (Gesprächsbaum):** Jede Nachricht hängt über ``parent`` an
  ihrer Vorgängerin; ``Conversation.current_leaf`` ist das Ende des
  angezeigten Zweigs. Oberfläche, Export und Verlauf an das Modell sehen nur
  den Pfad Wurzel → current_leaf (``conversation_path``). Bearbeiten
  (``edit_of``) legt eine Geschwister-Nutzernachricht an, Neu erzeugen eine
  Geschwister-Antwort; die alten Zweige bleiben vollständig erhalten und
  zählen weiter für Verbrauch und Budget (M6). Umschalten (``switch_branch``)
  setzt current_leaf auf das neueste Blatt im Teilbaum der gewählten Version.
  Der Baum wird mit einer schlanken Abfrage (pk, parent) des ganzen Chats in
  Python berechnet, danach werden nur die Nachrichten des Pfads geladen.
- **Titel beim Bearbeiten:** bleibt unverändert. Der Titel ist der Name des
  Chats in der Seitenleiste (ggf. von Hand umbenannt); er soll nicht beim
  Umschalten zwischen Versionen springen. Nur ein noch leerer Titel wird wie
  bisher aus der (bearbeiteten) Nachricht gesetzt.
- **Abriss mitten in der Antwort (M4-05):** Meldet der Adapter einen
  wiederholbaren Fehler (``retryable``, z. B. LM Studio beendet, Verbindung
  abgerissen), nachdem schon Text kam, wird die Antwort als ``aborted`` mit
  dem empfangenen Text und dem Fehlertext gespeichert – wie ein Abbruch, der
  Nutzer kann mit einem anderen Modell neu erzeugen. Ohne Teiltext oder bei
  nicht wiederholbaren Fehlern bleibt es ``error``. Bei Anbietern mit
  Statusprüfung wird außerdem der Status-Cache verworfen, damit die nächste
  Abfrage sofort neu prüft.
- **Quellmaterial (M7 Dokumente, M8 Websuche):** Vor dem ersten
  Anbieteraufruf laufen die registrierten Kontext-Abläufe
  (``tooling.register_context_provider``, gesteuert über ``Turn.options``,
  z. B. ``web_search``). Ihr Material hängt als ein ``<quellmaterial>``-Block
  nur im Verlauf an die Nutzerfrage dieser Runde (nicht in
  ``Message.content``, nicht in den System-Prompt) und steht in
  ``tool_state["context"]``, damit es nach einer Rückfrage wörtlich wieder
  mitgeht; der System-Prompt bekommt den festen Hinweis
  ``sources.SYSTEM_NOTE``. Quellen werden über ``SourceCollector``
  durchgehend nummeriert als ``SourceRef`` gespeichert und als SSE
  ``sources`` (immer die vollständige Liste) gemeldet. Scheitert ein Ablauf,
  entsteht die Antwort trotzdem: SSE ``status`` mit Hinweis, Hinweis an das
  Modell, ``tool_state["notices"][<Schlüssel>]``. Eingebaute Werkzeuge
  (``tooling.register_builtin``, z. B. ``web_search``) laufen ohne Rückfrage;
  ``available`` wird vor jedem Aufruf erneut geprüft.
- **Logs:** nur IDs und Fehlerarten, nie Inhalte oder Keys (Plan 9).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import close_old_connections, connection, transaction
from django.db.models import Q
from django.utils import timezone

from multigpt.accounts import usage
from multigpt.accounts.permissions import model_permitted
from multigpt.billing import booking as billing
from multigpt.billing.pricing import Round, Tally

from . import attachments as chat_attachments
from . import (
    citations,
    images,  # registriert generate_image (Bilderzeugung, M9-01), System-Hinweis
    sharing,
    tooling,
    tools_python,  # noqa: F401 - registriert run_python (Berechnungen, M4a-10)
    websearch,  # registriert Websuche (Kontext und Werkzeug), System-Hinweis
)
from . import projects as chat_projects
from . import sources as source_refs
from . import status as provider_status
from .mcp import status as mcp_status
from .models import (
    AIModel,
    Attachment,
    ChatSettings,
    Conversation,
    McpServer,
    Message,
    ToolCall,
)
from .providers import registry
from .providers.base import ChatMessage, Delta, Done, Error, ToolCallEvent, Usage
from .rag import chat as rag_chat  # noqa: F401 - registriert Dokumentsuche (Kontext und Werkzeug)
from .rag import doc_tools  # noqa: F401 - registriert list_documents, document_info, read_document
from .titles import title_from

logger = logging.getLogger(__name__)

# Zwischenspeichern des Teiltexts höchstens alle SAVE_INTERVAL Sekunden.
SAVE_INTERVAL = 2.0
MAX_CONTENT_LENGTH = 100_000
TITLE_LENGTH = 60
GENERIC_ERROR = "Die Antwort konnte nicht erzeugt werden. Bitte später erneut versuchen."


class TurnError(Exception):
    """Ungültige Anfrage vor dem Stream; ``message`` ist ein Text für Nutzer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


@dataclass
class Turn:
    """Eine vorbereitete Runde: gespeicherte Nachrichten und Kontext."""

    user: object
    conversation: Conversation
    ai_model: AIModel
    user_message: Message | None
    assistant_message: Message
    resume: bool = False  # Fortsetzung nach einer Rückfrage (confirm)
    # Feste Kontext-Abläufe, z. B. {"web_search": True, "collections": [ids]}.
    options: dict = field(default_factory=dict)
    query: str = ""  # Text der Nutzerfrage dieser Runde (Suchanfrage)


# --- Hilfen --------------------------------------------------------------------


def chat_models_for(user) -> list[AIModel]:
    """Aktive Chat-Modelle aktiver Anbieter, die die Rolle von ``user`` erlaubt –
    auch solche, die das ausgeschöpfte Monatsbudget gerade sperrt.

    Jedes Modell bekommt ``blocked_by_budget`` (bool), ``budget_reason`` (Text
    je Abrechnungskonto) und ``billing_title`` (Preisinfo). Der Budgetstand wird
    dafür höchstens einmal abgefragt (nicht je Modell).
    """
    qs = (
        AIModel.objects.filter(
            capability=AIModel.Capability.CHAT, active=True, provider__active=True
        )
        .select_related("provider__billing_account")
        .prefetch_related("mcp_servers", "prices")
    )
    models = [m for m in qs if model_permitted(user, m)]
    for m in models:
        m._prices_cache = list(m.prices.all())  # billing.pricing.price_at ohne Abfrage
    reasons = usage.blocked_reasons(user, models)
    for m in models:
        m.budget_reason = reasons.get(m.pk, "")
        m.blocked_by_budget = bool(m.budget_reason)
        m.billing_title = usage.billing_title(m)
    return models


def available_chat_models(user) -> list[AIModel]:
    """Aktive Chat-Modelle aktiver Anbieter, die ``user`` jetzt nutzen darf
    (Rolle und Monatsbudget)."""
    return [m for m in chat_models_for(user) if not m.blocked_by_budget]


CHAT_ROLES = (Message.Role.USER, Message.Role.ASSISTANT)
_UNSET = object()


class Tree:
    """Gesprächsbaum eines Chats aus (pk, parent_id) in der Reihenfolge (created, id)."""

    def __init__(self, conversation: Conversation):
        rows = list(
            conversation.messages.filter(role__in=CHAT_ROLES)
            .order_by("created", "id")
            .values_list("pk", "parent_id")
        )
        self.order = {pk: i for i, (pk, _) in enumerate(rows)}
        self.parent = dict(rows)
        self.children: dict[int | None, list[int]] = {}
        for pk, parent_id in rows:
            self.children.setdefault(parent_id, []).append(pk)
        self.newest = rows[-1][0] if rows else None

    def __contains__(self, pk) -> bool:
        return pk in self.parent

    def path_to(self, leaf_id: int | None) -> list[int]:
        """IDs von der Wurzel bis ``leaf_id`` (einschließlich)."""
        path: list[int] = []
        seen = set()
        node = leaf_id
        while node is not None and node in self.parent and node not in seen:
            seen.add(node)
            path.append(node)
            node = self.parent[node]
        path.reverse()
        return path

    def siblings(self, pk: int) -> list[int]:
        return self.children.get(self.parent.get(pk), [])

    def newest_leaf_below(self, pk: int) -> int:
        """Zuletzt erzeugte Nachricht im Teilbaum von ``pk`` – immer ein Blatt,
        weil Kinder nach ihren Eltern entstehen."""
        best = pk
        stack = [pk]
        while stack:
            node = stack.pop()
            if self.order[node] > self.order[best]:
                best = node
            stack.extend(self.children.get(node, []))
        return best


def _current_leaf_id(conversation: Conversation, tree: Tree) -> int | None:
    """current_leaf frisch aus der DB (die Instanz kann veraltet sein). Fehlt
    er (Altdaten, gelöschte Nachricht), gilt die neueste Nachricht.

    Geteilte Chats: Für einen Empfänger (``sharing.bind_viewer``) gilt seine
    eigene Ansicht (``ConversationView``), sonst der Hauptpfad."""
    viewer_id = sharing.foreign_viewer_id(conversation)
    if viewer_id is not None:
        leaf_id = sharing.view_leaf_id(conversation, viewer_id)
        return leaf_id if leaf_id in tree else tree.newest
    leaf_id = (
        Conversation.objects.filter(pk=conversation.pk)
        .values_list("current_leaf_id", flat=True)
        .first()
    )
    conversation.current_leaf_id = leaf_id
    return leaf_id if leaf_id in tree else tree.newest


def _load_path(ids: list[int], tree: Tree, *, prefetch: bool) -> list[Message]:
    qs = Message.objects.filter(pk__in=ids).select_related("model", "author")
    if prefetch:
        qs = qs.prefetch_related(
            "tool_calls__server", "tool_calls__attachments", "sources__chunk", "attachments"
        )
    by_id = {m.pk: m for m in qs}
    messages = [by_id[pk] for pk in ids if pk in by_id]
    for m in messages:
        if prefetch:
            # Hochgeladene Anhänge (ohne Dateien aus Werkzeugaufrufen).
            m.upload_attachments = [a for a in m.attachments.all() if a.tool_call_id is None]
        m.sibling_ids = tree.siblings(m.pk)
        m.sibling_index = m.sibling_ids.index(m.pk)
        m.sibling_count = len(m.sibling_ids)
    return messages


def visible_messages(conversation: Conversation) -> list[Message]:
    """Angezeigter Pfad (Wurzel → current_leaf) für Oberfläche, API und Export.

    Jede Nachricht trägt zusätzlich ``sibling_ids`` (Versionen, sortiert nach
    created/id, inkl. sich selbst), ``sibling_index`` (0-basiert) und
    ``sibling_count``.
    """
    tree = Tree(conversation)
    return _load_path(tree.path_to(_current_leaf_id(conversation, tree)), tree, prefetch=True)


def append_message(
    conversation: Conversation, *, parent=_UNSET, move_leaf: bool = True, **fields
) -> Message:
    """Neue Nachricht im Baum anlegen und als angezeigtes Ende setzen.

    Ohne ``parent`` hängt sie an das Ende des angezeigten Pfads;
    ``parent=None`` macht sie zur (weiteren) Wurzel, z. B. beim Bearbeiten der
    ersten Nachricht. ``move_leaf=False`` lässt current_leaf stehen
    (weitere Spalten im Vergleichsmodus).
    """
    if parent is _UNSET:
        tree = Tree(conversation)
        parent_id = _current_leaf_id(conversation, tree)
    else:
        parent_id = parent.pk if isinstance(parent, Message) else parent
    message = Message.objects.create(conversation=conversation, parent_id=parent_id, **fields)
    if move_leaf:
        # Besitzer: Hauptpfad; Empfänger geteilter Chats: eigene Ansicht bzw.
        # Vorspulen des Hauptpfads (sharing.move_leaf).
        sharing.move_leaf(conversation, message)
    return message


def switch_branch(conversation: Conversation, message_id, *, adopt_model: bool = False) -> None:
    """Version umschalten: current_leaf = neuestes Blatt unter ``message_id``.

    Wechselt der angezeigte Zweig, werden offene Rückfragen geschlossen (wie
    bei einer neuen Nachricht). ``updated`` bleibt, damit der Chat in der
    Seitenleiste nicht nach oben springt. ``adopt_model`` (Wahl im
    Vergleichsmodus): das Modell dieser Antwort wird Standardmodell des Chats.
    """
    tree = Tree(conversation)
    if isinstance(message_id, bool) or not isinstance(message_id, int) or message_id not in tree:
        raise TurnError("Diese Nachricht gibt es in diesem Chat nicht.")
    leaf_id = tree.newest_leaf_below(message_id)
    if sharing.foreign_viewer_id(conversation) is not None:
        # Empfänger eines geteilten Chats: nur die eigene Ansicht, keine
        # Rückfragen schließen (gemeinsamer Zustand bleibt unberührt).
        if leaf_id != _current_leaf_id(conversation, tree):
            sharing.set_view_leaf(conversation, leaf_id)
    elif leaf_id != _current_leaf_id(conversation, tree):
        close_pending(conversation)
        Conversation.objects.filter(pk=conversation.pk).update(current_leaf_id=leaf_id)
        conversation.current_leaf_id = leaf_id
    if adopt_model:
        model_id = (
            conversation.messages.filter(pk=message_id, role=Message.Role.ASSISTANT)
            .values_list("model_id", flat=True)
            .first()
        )
        if model_id is not None:
            Conversation.objects.filter(pk=conversation.pk).update(default_model_id=model_id)
            conversation.default_model_id = model_id


def build_system_prompt(user, conversation: Conversation, notes=()) -> str | None:
    """Reihenfolge: Grundregeln (``ChatSettings``, leer = keine), fester Prompt
    der Rolle, Hinweise von MultiGPT (``notes``: Quellmaterial, Websuche), dann
    die Anweisungen des Projekts (gekennzeichnet als Nutzerinhalt,
    chat/projects.py), zuletzt der System-Prompt des Chats."""
    parts = []
    base = ChatSettings.base_text()
    if base:
        parts.append(base)
    role = user.role if getattr(user, "role_id", None) else None
    if role is not None and role.fixed_system_prompt.strip():
        parts.append(role.fixed_system_prompt.strip())
    parts += [note for note in notes if note]
    project_block = chat_projects.instruction_block(conversation)
    if project_block:
        parts.append(project_block)
    if conversation.system_prompt.strip():
        parts.append(conversation.system_prompt.strip())
    return "\n\n".join(parts) or None


def build_history(
    conversation: Conversation,
    exclude_ids=(),
    *,
    provider_id: int | None = None,
    with_tools: bool = False,
    leaf: Message | int | None = None,
    vision: bool = False,
    for_user=None,
) -> list[ChatMessage]:
    """Verlauf für das Modell (siehe Moduldoku zu complete/aborted): nur der
    Pfad bis ``leaf`` (Standard: angezeigter Zweig), ohne ``exclude_ids``.

    ``for_user`` (Absender, geteilte Chats): Werkzeugrunden von Antworten, die
    jemand anderes ausgelöst hat, gehen nur als Text mit (``sharing.own_rounds``).

    Anhänge der Nutzernachrichten (``attachments.apply_to_history``): Bilder
    nur mit ``vision`` und nur aus den letzten Nachrichten mit Bildern, sonst
    Platzhalter; Dokumenttext als ``<quellmaterial>`` vor der Nachricht.

    Antworten mit Werkzeugrunden werden mit ``with_tools`` vollständig
    (Aufrufe, Ergebnisse) übergeben, sonst nur als Text (ohne angebotene
    Werkzeuge darf kein tool_use im Verlauf stehen). ``provider_state`` geht
    nur an denselben Anbieter zurück (``provider_id``).
    """
    tree = Tree(conversation)
    if leaf is None:
        leaf_id = _current_leaf_id(conversation, tree)
    else:
        leaf_id = leaf.pk if isinstance(leaf, Message) else leaf
    excluded = set(exclude_ids)
    ids = [pk for pk in tree.path_to(leaf_id) if pk not in excluded]
    usable = {Message.Status.COMPLETE, Message.Status.ABORTED}
    path = _load_path(ids, tree, prefetch=False)
    by_message: dict[int, list[Attachment]] = {}
    user_ids = [m.pk for m in path if m.role == Message.Role.USER]
    if user_ids:
        for att in Attachment.objects.filter(
            message_id__in=user_ids, tool_call__isnull=True
        ).order_by("created", "id"):
            by_message.setdefault(att.message_id, []).append(att)
    history: list[ChatMessage] = []
    with_attachments: list[tuple[Message, ChatMessage]] = []
    for m in path:
        if m.status not in usable:
            continue
        if m.role == Message.Role.ASSISTANT:
            tools = with_tools and sharing.own_rounds(m, conversation, for_user)
            history += expand_assistant(m, provider_id=provider_id, with_tools=tools)
        elif m.content or by_message.get(m.pk):
            item = ChatMessage(role=m.role, content=m.content)
            history.append(item)
            m.chat_attachments = by_message.get(m.pk, [])
            if m.chat_attachments:
                with_attachments.append((m, item))
    if with_attachments:
        chat_attachments.apply_to_history(with_attachments, vision=vision)
    return history


def _same_provider(message: Message, provider_id: int | None) -> bool:
    return (
        provider_id is not None
        and message.model is not None
        and (message.model.provider_id == provider_id)
    )


def round_messages(rnd: dict, *, keep_state: bool = True) -> list[ChatMessage]:
    """Eine gespeicherte Werkzeugrunde als Assistant- plus Tool-Nachrichten."""
    calls = [
        ToolCallEvent(
            id=c["id"],
            name=c["name"],
            arguments=c.get("arguments") or {},
            provider_state=c.get("provider_state") if keep_state else None,
        )
        for c in rnd.get("calls", [])
    ]
    out = [
        ChatMessage(
            "assistant",
            rnd.get("text", ""),
            tool_calls=calls,
            provider_state=rnd.get("provider_state") if keep_state else None,
        )
    ]
    for res in rnd.get("results", []):
        out.append(
            ChatMessage(
                "tool",
                res.get("content", ""),
                tool_call_id=res.get("tool_call_id"),
                name=res.get("name"),
                is_error=bool(res.get("is_error")),
            )
        )
    return out


def expand_assistant(
    message: Message, *, provider_id: int | None = None, with_tools: bool = False
) -> list[ChatMessage]:
    """Assistant-Nachricht für den Verlauf, ggf. mit ihren Werkzeugrunden."""
    state = message.tool_state or {}
    rounds = state.get("rounds") or []
    keep = _same_provider(message, provider_id)
    if not rounds:
        if not message.content:
            return []
        final_state = state.get("final_provider_state") if keep else None
        return [ChatMessage("assistant", message.content, provider_state=final_state)]
    if not with_tools:
        return [ChatMessage("assistant", message.content)] if message.content.strip() else []
    out: list[ChatMessage] = []
    for rnd in rounds:
        out += round_messages(rnd, keep_state=keep)
    final = message.content[state.get("text_offset", 0) :].strip()
    final_state = state.get("final_provider_state") if keep else None
    if final or final_state:
        out.append(ChatMessage("assistant", final, provider_state=final_state))
    return out


def compute_cost(ai_model: AIModel, tokens_in: int, tokens_out: int) -> Decimal | None:
    """Kosten in Euro nach dem jetzt gültigen Preis (Vorschau, ohne Buchung).

    Token- und Pauschalkonten (z. B. lokal) 0, ohne Preis oder Kurs None.
    Gebucht wird über ``_book`` (billing.booking).
    """
    return billing.estimate_eur(ai_model, Tally([Round(tokens_in, tokens_out)]))


def _book(turn: Turn, tally: Tally) -> Decimal | None:
    """Antwort buchen (eine Buchung je Antwort, billing.booking); liefert den
    Wert für ``Message.cost`` (EUR). Fehler verhindern den Abschluss nie."""
    try:
        entry = billing.book_answer(turn.assistant_message, turn.ai_model, tally)
        return billing.compat_cost(entry)
    except Exception as exc:
        logger.error(
            "Buchung für Antwort %s fehlgeschlagen: %s",
            turn.assistant_message.pk,
            type(exc).__name__,
        )
        return None


def _refresh_connection():
    """Vor dem Speichern nach langer Streamzeit: tote/abgelaufene Verbindung
    verwerfen, damit die nächste Abfrage neu verbindet (CONN_HEALTH_CHECKS).
    Nicht innerhalb einer Transaktion (z. B. in Tests)."""
    if not connection.in_atomic_block:
        close_old_connections()


def _title_from(content: str) -> str:
    # Markdown-Zeichen entfernen, Codeblöcke überspringen (M5-02, titles.py).
    return title_from(content, TITLE_LENGTH)


# --- Vorbereitung ---------------------------------------------------------------


def prepare_turn(
    user,
    conversation: Conversation,
    ai_model: AIModel,
    content: str | None = None,
    regenerate: bool = False,
    mcp_servers: list[int] | None = None,
    edit_of: int | None = None,
    regenerate_of: int | None = None,
    options: dict | None = None,
    compare: bool = False,
    attachments: list[int] | None = None,
    expected_leaf: int | None = None,
) -> Turn:
    """Legt Nutzer- und (leere) Assistant-Nachricht an. Rechte prüft der Aufrufer.

    Beide Nachrichten tragen ``author=user`` (Anzeige, Rückfragen, Budget).
    ``expected_leaf`` (geteilte Chats): Ende, das der Absender sieht; weicht
    es bei einer neuen Nachricht vom Stand der Datenbank ab, -> 409.

    - neue Nachricht: Kind des angezeigten Endes (current_leaf);
    - ``edit_of`` (pk einer Nutzernachricht dieses Chats): neue Version davon,
      also Geschwister mit gleichem parent;
    - ``regenerate``: neue Version der Antwort ``regenerate_of`` (Standard:
      letzte Antwort des Pfads); endet der Pfad mit einer Frage, wird sie
      beantwortet.

    Die neue Antwort wird das angezeigte Ende. ``mcp_servers``: eingeschaltete
    MCP-Server (None: Voreinstellung = alle erlaubten). Nur bei Modellen mit
    ``supports_tools``. Offene Rückfragen des Chats werden vorher geschlossen
    (``close_pending``). ``options``: feste Kontext-Abläufe wie
    ``{"web_search": True, "collections": [ids]}`` (Rechte und Einstellungen
    prüft der Aufrufer; die Dokumentsuche filtert den Zugriff zusätzlich in SQL).

    ``compare`` (Vergleichsmodus, M6): ohne MCP-Werkzeuge (keine Rückfragen in
    parallelen Spalten). Eine weitere Spalte (``regenerate``) wird Geschwister,
    ohne current_leaf und default_model zu ändern – angezeigt bleibt die erste
    Spalte, bis der Nutzer per ``switch_branch`` wählt.

    ``attachments`` (IDs): eigene Entwürfe und beim Bearbeiten Anhänge der
    Originalnachricht; ``None`` beim Bearbeiten übernimmt alle Anhänge der
    Originalnachricht. Mit Anhängen darf ``content`` leer sein. Bilder an ein
    Modell ohne ``supports_vision`` -> ``TurnError`` 409 (alles zurückgerollt).
    """
    if not regenerate:
        content = (content or "").strip()
        if not content and not attachments and edit_of is None:
            raise TurnError("Die Nachricht ist leer.")
        if len(content) > MAX_CONTENT_LENGTH:
            raise TurnError("Die Nachricht ist zu lang.")
    servers = (
        tooling.enabled_server_ids(user, mcp_servers, ai_model=ai_model)
        if ai_model.supports_tools and not compare
        else []
    )
    extra_column = compare and regenerate

    with transaction.atomic():
        # Sperre gegen gleichzeitige Züge im selben Chat (gleicher current_leaf).
        Conversation.objects.select_for_update().filter(pk=conversation.pk).first()
        tree = Tree(conversation)
        user_message = None
        if regenerate:
            question_id = _question_for_regenerate(conversation, tree, regenerate_of)
            if Attachment.objects.filter(
                message_id=question_id, kind=Attachment.Kind.IMAGE, tool_call__isnull=True
            ).exists():
                _require_vision(ai_model)
        else:
            if edit_of is not None:
                original = conversation.messages.filter(pk=edit_of, role=Message.Role.USER).first()
                if original is None:
                    raise TurnError("Diese Nachricht kann nicht bearbeitet werden.")
                parent_id = original.parent_id
            else:
                parent_id = _current_leaf_id(conversation, tree)
                if expected_leaf is not None and expected_leaf != parent_id:
                    raise TurnError(sharing.STALE_MESSAGE, 409)
            drafts, originals = _resolve_attachments(user, attachments, edit_of)
            if not content and not (drafts or originals):
                raise TurnError("Die Nachricht ist leer.")
            if any(a.is_image for a in [*drafts, *originals]):
                _require_vision(ai_model)
            close_pending(conversation)
            user_message = append_message(
                conversation,
                parent=parent_id,
                role=Message.Role.USER,
                content=content,
                author=user,
            )
            question_id = user_message.pk
            for original_attachment in originals:
                chat_attachments.copy_for_message(original_attachment, user_message)
            if drafts:
                Attachment.objects.filter(pk__in=[a.pk for a in drafts]).update(
                    message=user_message, conversation=conversation
                )
            if not content:
                first = (drafts or originals)[0]
                content_for_title = first.display_name
            else:
                content_for_title = content

        assistant_message = append_message(
            conversation,
            parent=question_id,
            move_leaf=not extra_column,
            role=Message.Role.ASSISTANT,
            model=ai_model,
            author=user,
            status=Message.Status.ABORTED,  # Platzhalter, siehe Moduldoku
            tool_state=_initial_tool_state(servers, options),
        )

        fields = {"updated": timezone.now()}
        if not extra_column:
            fields["default_model"] = ai_model
        if not conversation.title and user_message is not None:
            fields["title"] = _title_from(content_for_title)
        Conversation.objects.filter(pk=conversation.pk).update(**fields)
        for key, value in fields.items():
            setattr(conversation, key, value)

    if user_message is not None:
        query = content
    else:
        query = (
            Message.objects.filter(pk=question_id).values_list("content", flat=True).first() or ""
        )
    return Turn(
        user,
        conversation,
        ai_model,
        user_message,
        assistant_message,
        options=dict(options or {}),
        query=query,
    )


def _resolve_attachments(user, ids, edit_of) -> tuple[list[Attachment], list[Attachment]]:
    """Anhänge der neuen Nutzernachricht: (eigene Entwürfe, Originalanhänge)."""
    if ids is None:
        if edit_of is None:
            return [], []
        # Bearbeiten ohne Angabe: alle Anhänge der Originalnachricht übernehmen.
        originals = Attachment.objects.filter(message_id=edit_of, tool_call__isnull=True)
        return [], list(originals.order_by("created", "id"))
    try:
        return chat_attachments.resolve_for_message(user, ids, edit_of=edit_of)
    except ValueError as exc:
        raise TurnError(str(exc)) from None


def _require_vision(ai_model: AIModel) -> None:
    if not ai_model.supports_vision:
        raise TurnError(chat_attachments.MSG_NO_VISION.format(model=ai_model.display_name), 409)


def _initial_tool_state(servers: list[int], options: dict | None) -> dict:
    """Startzustand der Antwort: eingeschaltete MCP-Server und gewählte
    Sammlungen (das Werkzeug ``search_documents`` sucht darin, auch nach einer
    Rückfrage)."""
    state: dict = {}
    if servers:
        state["servers"] = servers
    collections = (options or {}).get("collections")
    if collections:
        state["collections"] = list(collections)
    return state


def _question_for_regenerate(conversation: Conversation, tree: Tree, regenerate_of) -> int:
    """Frage (pk), zu der eine neue Antwortversion entsteht; schließt offene Rückfragen."""
    no_question = "Es gibt keine Frage, zu der eine Antwort erzeugt werden kann."
    if regenerate_of is not None:
        target = conversation.messages.filter(pk=regenerate_of, role=Message.Role.ASSISTANT).first()
        if target is None:
            raise TurnError("Diese Antwort kann nicht neu erzeugt werden.")
    else:
        path = tree.path_to(_current_leaf_id(conversation, tree))
        if not path:
            raise TurnError(no_question)
        target = Message.objects.only("pk", "role", "parent_id").get(pk=path[-1])
    if target.role == Message.Role.USER:
        question_id = target.pk  # Pfad endet mit einer unbeantworteten Frage
    else:
        question_id = target.parent_id
    if (
        question_id is None
        or not conversation.messages.filter(pk=question_id, role=Message.Role.USER).exists()
    ):
        raise TurnError(no_question)
    close_pending(conversation)
    return question_id


# --- Rückfrage (M4a-05) --------------------------------------------------------


def _unanswered_results(state: dict, text: str) -> None:
    """Fehlende Ergebnisse der letzten Runde als Fehler ergänzen."""
    rounds = state.get("rounds") or []
    if not rounds:
        return
    rnd = rounds[-1]
    done = {r["tool_call_id"] for r in rnd.setdefault("results", [])}
    for call in rnd.get("calls", []):
        if call["id"] not in done:
            rnd["results"].append(
                {
                    "tool_call_id": call["id"],
                    "name": call["name"],
                    "content": text,
                    "is_error": True,
                }
            )


def close_pending(conversation: Conversation) -> None:
    """Offene Rückfragen schließen, bevor der Chat weitergeht (neue Nachricht,
    Bearbeiten, Neu erzeugen, Umschalten): wartende Aufrufe -> ``rejected``, Antwort -> ``aborted``.
    So bleiben keine verwaisten Aufrufe zurück."""
    awaiting = Q(status=Message.Status.AWAITING_CONFIRMATION) | Q(
        tool_calls__status=ToolCall.Status.AWAITING_CONFIRMATION
    )
    # Zweiter Fall: confirm angenommen, Stream aber nie gestartet (Client weg).
    pending = conversation.messages.filter(awaiting).distinct()
    for message in pending:
        state = dict(message.tool_state or {})
        _unanswered_results(state, tooling.MSG_UNANSWERED)
        for tool_call in message.tool_calls.filter(status=ToolCall.Status.AWAITING_CONFIRMATION):
            tooling.close_call(tool_call, ToolCall.Status.REJECTED, tooling.MSG_UNANSWERED)
        fields = {"tool_state": state}
        if message.status == Message.Status.AWAITING_CONFIRMATION:
            fields["status"] = Message.Status.ABORTED
        Message.objects.filter(pk=message.pk).update(**fields)


def pending_message(conversation: Conversation) -> Message | None:
    """Die Antwort, die auf eine Bestätigung wartet (höchstens eine je Chat)."""
    return (
        conversation.messages.filter(
            role=Message.Role.ASSISTANT, status=Message.Status.AWAITING_CONFIRMATION
        )
        .select_related("model__provider")
        .order_by("-created", "-id")
        .first()
    )


DECISIONS = {"approve", "reject"}


def prepare_resume(user, conversation: Conversation, message: Message, decisions) -> Turn:
    """Entscheidungen übernehmen und die Fortsetzung vorbereiten.

    ``decisions``: ``{"<ToolCall.pk>": "approve"|"reject"}`` für **genau** die
    wartenden Aufrufe dieser Antwort. Die Antwort wechselt atomar von
    ``awaiting_confirmation`` auf den Platzhalter ``aborted`` – ein zweites,
    gleichzeitiges confirm bekommt 409. Rechte prüft die Schleife vor jedem
    Aufruf erneut.
    """
    if not isinstance(decisions, dict) or not decisions:
        raise TurnError("Bitte über alle Werkzeugaufrufe entscheiden.")
    parsed: dict[int, str] = {}
    for key, value in decisions.items():
        try:
            pk = int(key)
        except (TypeError, ValueError):
            raise TurnError("Ungültige Entscheidung.") from None
        if value not in DECISIONS:
            raise TurnError("Ungültige Entscheidung.")
        parsed[pk] = value
    waiting = set(
        message.tool_calls.filter(status=ToolCall.Status.AWAITING_CONFIRMATION).values_list(
            "pk", flat=True
        )
    )
    if set(parsed) != waiting:
        raise TurnError("Bitte über alle wartenden Werkzeugaufrufe entscheiden.")
    claimed = Message.objects.filter(
        pk=message.pk, status=Message.Status.AWAITING_CONFIRMATION
    ).update(status=Message.Status.ABORTED)
    if claimed != 1:
        raise TurnError("Über diese Werkzeugaufrufe wurde bereits entschieden.", 409)
    state = dict(message.tool_state or {})
    for call in (state.get("rounds") or [{}])[-1].get("calls", []):
        if call.get("tool_call") in parsed:
            call["decision"] = parsed[call["tool_call"]]
    message.tool_state = state
    message.status = Message.Status.ABORTED
    Message.objects.filter(pk=message.pk).update(tool_state=state)
    Conversation.objects.filter(pk=conversation.pk).update(updated=timezone.now())
    return Turn(user, conversation, message.model, None, message, resume=True)


# --- Stream und Werkzeugschleife (M3, M4a-04) ---------------------------------------

# Höchstens so viele Anbieteraufrufe je Antwort; der letzte läuft mit
# ``tool_choice="none"`` (mit Werkzeugen, weil Anthropic sie verlangt, sobald
# der Verlauf tool_use enthält).
MAX_ROUNDS = 10
ROUND_SEPARATOR = "\n\n"


def _close(stream):
    if stream is not None and hasattr(stream, "close"):
        try:
            stream.close()
        except Exception as exc:  # Aufräumen darf den Abschluss nicht verhindern
            logger.warning("Adapter-Stream ließ sich nicht schließen: %s", type(exc).__name__)


def _save_partial(message_id: int, text: str):
    _refresh_connection()
    Message.objects.filter(pk=message_id).update(content=text)


def _finish(
    turn: Turn,
    text: str,
    status: str,
    error: str,
    tokens_in: int,
    tokens_out: int,
    tally: Tally | None = None,
):
    msg = turn.assistant_message
    msg.content = text
    msg.status = status
    msg.error = error
    msg.tokens_in = tokens_in
    msg.tokens_out = tokens_out
    if tally is None:
        tally = Tally([Round(tokens_in, tokens_out)] if tokens_in or tokens_out else [])
    msg.cost = None
    try:
        _refresh_connection()
        msg.cost = _book(turn, tally)  # Buchung zuerst; Message.cost kommt aus ihr
        Message.objects.filter(pk=msg.pk).update(
            content=text,
            status=status,
            error=error,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost=msg.cost,
            tool_state=msg.tool_state or {},
        )
        Conversation.objects.filter(pk=turn.conversation.pk).update(updated=timezone.now())
    except Exception as exc:
        logger.error("Antwort %s konnte nicht gespeichert werden: %s", msg.pk, type(exc).__name__)


class _Loop:
    """Eine Antwort mit Werkzeugrunden; ``run()`` liefert die SSE-Events.

    Zustand in ``Message.tool_state`` (eine Nachricht je Antwort, siehe
    Moduldoku unten bei ``run_turn``)::

        {"servers": [id, ...],            # eingeschaltete Server
         "model_calls": n,                # Anbieteraufrufe bisher
         "rounds": [{"text", "provider_state",
                     "calls": [{"id", "name", "arguments", "provider_state",
                                "server_id", "tool", "tool_call", "decision"?}],
                     "results": [{"tool_call_id", "name", "content", "is_error"}]}],
         "text_offset": int,              # Länge von content nach der letzten Runde
         "final_provider_state": {...}}   # provider_state der letzten Antwort
    """

    def __init__(self, turn: Turn):
        self.turn = turn
        self.msg = turn.assistant_message
        self.state = dict(self.msg.tool_state or {})
        self.state.setdefault("rounds", [])
        self.msg.tool_state = self.state
        self.servers = list(self.state.get("servers") or [])
        self.content = self.msg.content if turn.resume else ""
        self.tokens_in = self.msg.tokens_in if turn.resume else 0
        self.tokens_out = self.msg.tokens_out if turn.resume else 0
        # Verbrauch je Anbieteraufruf mit Cache/Reasoning (Buchung, billing).
        self.tally = billing.tally_of(self.msg) if turn.resume else Tally()
        self.stream = None
        self.paused = False
        self.bindings: dict[str, tooling.Binding] = {}
        # Quellen der Antwort (durchgehend nummeriert) und Quellmaterial-Block
        # der festen Abläufe (bleibt über eine Rückfrage hinweg erhalten).
        # Zitierstil des Absenders (geteilte Chats: nie der des Besitzers).
        self.sources = source_refs.SourceCollector(self.msg, citations.prefs_for(turn.user))
        self.context = str(self.state.get("context") or "")

    # --- Hilfen ---

    def _persist(self):
        _refresh_connection()
        Message.objects.filter(pk=self.msg.pk).update(content=self.content, tool_state=self.state)

    def _abort_open_calls(self):
        for tool_call in ToolCall.objects.filter(
            message=self.msg,
            status__in=[ToolCall.Status.RUNNING, ToolCall.Status.AWAITING_CONFIRMATION],
        ):
            tooling.close_call(tool_call, ToolCall.Status.ERROR, tooling.MSG_ABORTED)
        _unanswered_results(self.state, tooling.MSG_ABORTED)

    def _add_result(self, rnd: dict, call: dict, text: str, is_error: bool):
        rnd.setdefault("results", []).append(
            {
                "tool_call_id": call["id"],
                "name": call["name"],
                "content": text,
                "is_error": is_error,
            }
        )

    def _tool_call_for(self, call: dict, server, status: str) -> ToolCall:
        if call.get("tool_call"):
            tool_call = ToolCall.objects.filter(pk=call["tool_call"], message=self.msg).first()
            if tool_call is not None:
                return tool_call
        tool_call = ToolCall.objects.create(
            message=self.msg,
            server=server,
            tool=(call.get("tool") or call["name"])[:200],
            provider_call_id=call["id"][:200],
            arguments=call.get("arguments") or {},
            status=status,
        )
        call["tool_call"] = tool_call.pk
        return tool_call

    def _new_round(self, text: str, provider_state, calls: list[ToolCallEvent]) -> dict:
        stored = []
        for event in calls:
            binding = self.bindings.get(event.name)
            stored.append(
                {
                    "id": event.id,
                    "name": event.name,
                    "arguments": event.arguments,
                    "provider_state": event.provider_state,
                    "server_id": binding.server_id if binding else None,
                    "tool": binding.tool if binding else None,
                    "builtin": binding.builtin if binding else None,
                    "tool_call": None,
                }
            )
        rnd = {"text": text, "provider_state": provider_state, "calls": stored, "results": []}
        self.state["rounds"].append(rnd)
        self.state["text_offset"] = len(self.content)
        return rnd

    # --- Runde ausführen ---

    def _process_round(self, rnd: dict):
        """Aufrufe der Runde in Reihenfolge ausführen oder für die Rückfrage
        pausieren. Generator; Rückgabewert True = pausiert.

        Braucht ein Aufruf eine Bestätigung, wartet die **ganze Runde** (auch
        Aufrufe ohne Rückfrage), damit die Reihenfolge der Aufrufe erhalten
        bleibt – ein Lesezugriff nach einem Schreibzugriff sieht sonst den
        alten Stand.
        """
        done = {r["tool_call_id"] for r in rnd.get("results", [])}
        open_calls = [c for c in rnd["calls"] if c["id"] not in done]
        need = []
        for call in open_calls:
            if call.get("builtin"):
                # Eingebaute Werkzeuge ohne Rückfrage, außer der Verwalter
                # verlangt sie (z. B. generate_image, kostet Geld).
                if call.get("decision") is None and tooling.builtin_needs_confirmation(
                    call["builtin"]
                ):
                    need.append((call, None))
                continue
            server, err = tooling.resolve_server(
                self.turn.user, call, self.servers, self.turn.ai_model
            )
            if server is not None and call.get("decision") is None:
                if tooling.needs_confirmation(server, call["tool"]):
                    need.append((call, server))
        if need:
            yield from self._pause(need)
            return True
        for call in open_calls:
            yield from self._execute(rnd, call)
        return False

    def _pause(self, need):
        with transaction.atomic():
            tool_calls = []
            for call, server in need:
                tool_call = self._tool_call_for(call, server, ToolCall.Status.AWAITING_CONFIRMATION)
                tool_call.status = ToolCall.Status.AWAITING_CONFIRMATION
                tool_call.save(update_fields=["status"])
                tool_calls.append((tool_call, server))
            cost = _book(self.turn, self.tally)
            Message.objects.filter(pk=self.msg.pk).update(
                content=self.content,
                status=Message.Status.AWAITING_CONFIRMATION,
                error="",
                tokens_in=self.tokens_in,
                tokens_out=self.tokens_out,
                cost=cost,
                tool_state=self.state,
            )
            Conversation.objects.filter(pk=self.turn.conversation.pk).update(updated=timezone.now())
        self.msg.status = Message.Status.AWAITING_CONFIRMATION
        self.paused = True
        logger.info("Antwort %s wartet auf Bestätigung (%d Aufrufe)", self.msg.pk, len(need))
        for tool_call, server in tool_calls:
            yield "tool_call", tooling.call_event(tool_call, server.name if server else "")
        yield "confirmation_required", {"tool_call_ids": [tc.pk for tc, _ in tool_calls]}
        yield "usage", {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out}
        yield "done", {"status": Message.Status.AWAITING_CONFIRMATION}

    def _execute(self, rnd: dict, call: dict):
        if call.get("builtin"):
            yield from self._execute_builtin(rnd, call)
            return
        user = self.turn.user
        # Rechte vor jedem Aufruf erneut prüfen (auch nach einer Bestätigung).
        server, err = tooling.resolve_server(user, call, self.servers, self.turn.ai_model)
        decision = call.get("decision")
        if decision == "reject":
            tool_call = self._tool_call_for(call, server, ToolCall.Status.REJECTED)
            tooling.close_call(tool_call, ToolCall.Status.REJECTED, tooling.MSG_REJECTED)
            self._add_result(rnd, call, tooling.MSG_REJECTED, True)
            self._persist()
            yield "tool_result", tooling.result_event(tool_call, [])
            return
        if server is not None and decision != "approve":
            if tooling.needs_confirmation(server, call["tool"]):
                err = tooling.MSG_NOT_CONFIRMED  # nie ohne Bestätigung ausführen
        tool_call = self._tool_call_for(call, server, ToolCall.Status.RUNNING)
        if not err:
            tool_call.status = ToolCall.Status.RUNNING
            tool_call.save(update_fields=["status"])
        yield "tool_call", {**tooling.call_event(tool_call), "status": ToolCall.Status.RUNNING}
        if err:
            tooling.close_call(tool_call, ToolCall.Status.ERROR, err)
            self._add_result(rnd, call, err, True)
            self._persist()
            yield "tool_result", tooling.result_event(tool_call, [])
            return
        outcome = tooling.execute(server, tool_call, self.msg)
        self._add_result(rnd, call, outcome.text, outcome.is_error)
        self._persist()
        yield "tool_result", tooling.result_event(tool_call, outcome.attachment_ids)

    def _budget_status(self):
        """Hinweis ab 80 % eines Budgets als SSE ``status`` (Stufe warning).

        Nach den festen Abläufen, damit deren Info-Status ihn nicht verdrängt.
        Stand vor dieser Antwort; die Sperre selbst prüft ``can(USE_MODEL)``.
        """
        try:
            texts = usage.warnings_for(self.turn.user, self.turn.ai_model)
        except Exception as exc:  # der Hinweis darf die Antwort nie verhindern
            logger.error("Budgetstand für Antwort %s: %s", self.msg.pk, type(exc).__name__)
            return
        if texts:  # Konto des Modells und Gesamtbudget (billing.budgets)
            yield "status", {"text": " ".join(texts), "level": "warning"}

    # --- Quellmaterial und eingebaute Werkzeuge (M7, M8) ---

    def _run_context_providers(self):
        """Feste Abläufe vor dem ersten Anbieteraufruf (Generator).

        Danach ein ``sources``-Event (falls Quellen dazukamen) und ein gemeinsamer
        ``<quellmaterial>``-Block für die Nutzerfrage.
        """
        entries, notes, notices = [], [], {}
        for key, provider in tooling.context_providers():
            try:
                result = yield from provider(self.turn, self.sources)
            except Exception as exc:  # ein Ablauf darf die Antwort nie verhindern
                logger.error("Kontext %s für Antwort %s: %s", key, self.msg.pk, type(exc).__name__)
                continue
            if result is None:
                continue
            entries += list(result.entries)
            notes += list(result.notes)
            if result.notice:
                notices[key] = result.notice
        if entries or notes:
            self.context = source_refs.context_block(entries, notes)
            self.state["context"] = self.context
        if notices:
            self.state["notices"] = notices
        if entries or notes or notices:
            self._persist()
        if self.sources.changed:
            yield "sources", self.sources.event()

    def _execute_builtin(self, rnd: dict, call: dict):
        """Eingebautes Werkzeug: ``available`` vor jedem Aufruf; Rückfrage nur mit
        ``BuiltinTool.confirm`` (abgelehnt bzw. nicht bestätigt -> nicht ausgeführt)."""
        decision = call.get("decision")
        if decision == "reject":
            tool_call = self._tool_call_for(call, None, ToolCall.Status.REJECTED)
            tooling.close_call(tool_call, ToolCall.Status.REJECTED, tooling.MSG_REJECTED)
            self._add_result(rnd, call, tooling.MSG_REJECTED, True)
            self._persist()
            yield "tool_result", tooling.result_event(tool_call, [])
            return
        tool_call = self._tool_call_for(call, None, ToolCall.Status.RUNNING)
        if tool_call.status != ToolCall.Status.RUNNING:
            tool_call.status = ToolCall.Status.RUNNING
            tool_call.save(update_fields=["status"])
        yield "tool_call", {**tooling.call_event(tool_call), "status": ToolCall.Status.RUNNING}
        started = time.monotonic()
        builtin = tooling.get_builtin(call.get("builtin"))
        if builtin is None or not builtin.available(self.turn.user, self.turn.ai_model):
            outcome = tooling.Outcome(ToolCall.Status.ERROR, tooling.MSG_NOT_ALLOWED, True)
        elif decision != "approve" and tooling.builtin_needs_confirmation(builtin.name):
            outcome = tooling.Outcome(ToolCall.Status.ERROR, tooling.MSG_NOT_CONFIRMED, True)
        else:
            try:
                result = builtin.run(self.turn.user, call.get("arguments") or {}, self.sources)
            except Exception as exc:
                logger.error("Werkzeug %s (%s): %s", builtin.name, tool_call.pk, type(exc).__name__)
                outcome = tooling.Outcome(ToolCall.Status.ERROR, tooling.MSG_FAILED, True)
            else:
                status = ToolCall.Status.ERROR if result.is_error else ToolCall.Status.OK
                text = result.text or tooling.MSG_EMPTY
                outcome = tooling.Outcome(status, text, bool(result.is_error))
        elapsed = tooling.record(tool_call, outcome, started)
        logger.info(
            "Werkzeugaufruf %s (eingebaut): %s in %d ms",
            tool_call.pk,
            outcome.status,
            int(elapsed * 1000),
        )
        self._add_result(rnd, call, outcome.text, outcome.is_error)
        self._persist()
        yield "tool_result", tooling.result_event(tool_call, [])
        if self.sources.changed:
            yield "sources", self.sources.event()

    def _over_limit(self, rnd: dict):
        """Aufrufe trotz ``tool_choice="none"`` in der letzten Runde: protokollieren,
        nicht ausführen."""
        for call in rnd["calls"]:
            server = None
            if call.get("server_id"):
                server = McpServer.objects.filter(pk=call["server_id"]).first()
            tool_call = self._tool_call_for(call, server, ToolCall.Status.ERROR)
            tooling.close_call(tool_call, ToolCall.Status.ERROR, tooling.MSG_ROUND_LIMIT)
            self._add_result(rnd, call, tooling.MSG_ROUND_LIMIT, True)
            yield "tool_call", {**tooling.call_event(tool_call), "status": ToolCall.Status.RUNNING}
            yield "tool_result", tooling.result_event(tool_call, [])
        self._persist()

    # --- Ablauf ---

    def run(self) -> Iterator[tuple[str, dict]]:
        turn, msg = self.turn, self.msg
        status = Message.Status.COMPLETE
        error = ""
        try:
            yield (
                "start",
                {
                    "user_message_id": turn.user_message.pk if turn.user_message else None,
                    "assistant_message_id": msg.pk,
                    # parent der ersten neuen Nachricht (Nutzer- bzw. Antwortversion)
                    "parent_id": (turn.user_message or msg).parent_id,
                },
            )
            if not turn.resume:
                yield from self._run_context_providers()
                yield from self._budget_status()
            if turn.ai_model.supports_tools:
                # MCP-Werkzeuge der eingeschalteten Server plus eingebaute
                # Werkzeuge (web_search) nach Recht und Einstellung.
                if self.servers:
                    offline = []
                    self.bindings = tooling.collect_tools(
                        turn.user, self.servers, turn.ai_model, offline=offline
                    )
                    if offline:  # Hinweis statt Fehler mitten in der Antwort
                        hint = mcp_status.offline_hint(offline)
                        yield "status", {"text": hint, "level": "warning"}
                self.bindings.update(tooling.builtin_bindings(turn.user, turn.ai_model))
            if turn.resume and self.state["rounds"]:
                paused = yield from self._process_round(self.state["rounds"][-1])
                if paused:
                    return
            specs = [b.spec for b in self.bindings.values()]
            provider_id = turn.ai_model.provider_id
            history = build_history(
                turn.conversation,
                exclude_ids=[msg.pk],
                provider_id=provider_id,
                with_tools=bool(specs),
                leaf=msg,  # Pfad dieser Antwort, auch wenn inzwischen umgeschaltet wurde
                vision=turn.ai_model.supports_vision,
                for_user=turn.user,
            )
            if self.context:
                # Quellmaterial nur im Verlauf an die Frage dieser Runde hängen.
                for item in reversed(history):
                    if item.role == "user":
                        item.content = source_refs.wrap_question(item.content, self.context)
                        break
            if specs:
                for rnd in self.state["rounds"]:
                    history += round_messages(rnd)
            elif self.state["rounds"]:
                # Ohne Werkzeuge kein tool_use im Verlauf: bisherige Runden als Text.
                text = self.content.strip()
                if text:
                    history.append(ChatMessage("assistant", text))
            notes = []
            documents = chat_attachments.has_documents(history)
            if self.context or documents or any(b.builtin for b in self.bindings.values()):
                notes.append(source_refs.SYSTEM_NOTE)
            # Websuche: Werkzeug angeboten bzw. Hinweis auf den Schalter (M8).
            notes.append(
                websearch.system_hint(turn.user, turn.options, "web_search" in self.bindings)
            )
            notes.append(websearch.pages.system_hint(self.bindings))  # fetch_url, crawl_site
            notes.append(images.system_hint(self.bindings))  # generate_image (M9-01)
            system = build_system_prompt(turn.user, turn.conversation, notes)
            adapter = registry.get_adapter(turn.ai_model.provider)

            while True:
                sharing.check_turn(turn)  # Freigabe entzogen? (geteilte Chats)
                calls_so_far = int(self.state.get("model_calls", 0))
                last = calls_so_far >= MAX_ROUNDS - 1
                params = {}
                if specs:
                    params = {"tools": specs, "tool_choice": "none" if last else "auto"}
                self.stream = adapter.stream(
                    turn.ai_model.model_id, history, system=system, **params
                )
                self.state["model_calls"] = calls_so_far + 1
                round_parts: list[str] = []
                calls: list[ToolCallEvent] = []
                round_in = round_out = 0
                round_usage: Usage | None = None
                done: Done | None = None
                last_save = time.monotonic()
                for event in self.stream:
                    if isinstance(event, Delta):
                        if not event.text:
                            continue
                        if not round_parts and self.state["rounds"] and self.content.strip():
                            if not self.content.endswith(ROUND_SEPARATOR):
                                self.content += ROUND_SEPARATOR
                                yield "delta", {"text": ROUND_SEPARATOR}
                        round_parts.append(event.text)
                        self.content += event.text
                        yield "delta", {"text": event.text}
                        if time.monotonic() - last_save >= SAVE_INTERVAL:
                            _save_partial(msg.pk, self.content)
                            last_save = time.monotonic()
                            sharing.check_turn(turn)
                    elif isinstance(event, ToolCallEvent):
                        calls.append(event)
                    elif isinstance(event, Usage):
                        round_in, round_out = event.tokens_in, event.tokens_out
                        round_usage = event
                    elif isinstance(event, Error):
                        error = event.message or GENERIC_ERROR
                        if event.retryable and self.content.strip():
                            status = Message.Status.ABORTED  # Teiltext bleibt
                        else:
                            status = Message.Status.ERROR
                        if event.retryable:
                            provider_status.invalidate(turn.ai_model.provider_id)
                        logger.info(
                            "Anbieterfehler bei Antwort %s (Modell %s)", msg.pk, turn.ai_model.pk
                        )
                        break
                    elif isinstance(event, Done):
                        done = event
                        break
                _close(self.stream)
                self.stream = None
                self.tokens_in += round_in
                if round_usage is not None:
                    self.tally.add(round_usage)
                self.tokens_out += round_out
                if error:
                    break
                if calls and specs:
                    rnd = self._new_round(
                        "".join(round_parts), done.provider_state if done else None, calls
                    )
                    if last:
                        yield from self._over_limit(rnd)
                        break
                    paused = yield from self._process_round(rnd)
                    if paused:
                        return
                    history += round_messages(rnd)
                    continue
                self.state["final_provider_state"] = done.provider_state if done else None
                break
        except GeneratorExit:
            _close(self.stream)
            if self.paused:
                raise  # Zustand ist gespeichert, die Antwort wartet weiter.
            self._abort_open_calls()
            _finish(
                turn,
                self.content,
                Message.Status.ABORTED,
                "",
                self.tokens_in,
                self.tokens_out,
                self.tally,
            )
            logger.info("Antwort %s vom Client abgebrochen", msg.pk)
            raise
        except sharing.AccessRevoked:
            # Widerruf greift sofort: Teiltext bleibt, Antwort gilt als abgebrochen.
            logger.info("Antwort %s: Freigabe entzogen", msg.pk)
            status, error = Message.Status.ABORTED, sharing.MSG_REVOKED
            _close(self.stream)
            self._abort_open_calls()
        except Exception as exc:
            logger.error("Stream für Antwort %s fehlgeschlagen: %s", msg.pk, type(exc).__name__)
            status, error = Message.Status.ERROR, GENERIC_ERROR
            _close(self.stream)
            try:
                self._abort_open_calls()
            except Exception:
                logger.error("Werkzeugaufrufe von Antwort %s nicht abgeschlossen", msg.pk)

        if self.paused:
            return
        _finish(turn, self.content, status, error, self.tokens_in, self.tokens_out, self.tally)
        if error:
            yield "error", {"message": error}
        cost = msg.cost
        usage = {"tokens_in": self.tokens_in, "tokens_out": self.tokens_out}
        usage["cost"] = None if cost is None else str(cost)
        yield "usage", usage
        yield "done", {"status": status}


def run_turn(turn: Turn) -> Iterator[tuple[str, dict]]:
    """Generator der SSE-Events als (Name, Daten).

    Reihenfolge: start, (delta | tool_call | tool_result)*, [error], usage,
    done – oder bei einer Rückfrage: …, tool_call (awaiting_confirmation)*,
    confirmation_required, usage, done ``awaiting_confirmation``. Wird der
    Generator geschlossen (Client hat abgebrochen), wird der bisherige Text
    mit ``aborted`` gespeichert, laufende Werkzeugaufrufe werden als Fehler
    abgeschlossen und der Adapter-Stream geschlossen.

    **Speicherung (Entscheidung):** Eine Antwort bleibt *eine* ``Message``.
    Zwischenrunden (Text, Aufrufe, Ergebnisse, ``provider_state``) stehen als
    JSON in ``Message.tool_state``, jeder Aufruf zusätzlich als ``ToolCall``
    (Protokoll und Anzeige). Eigene Zwischen-Nachrichten (role assistant/tool)
    hätten jede Abfrage auf sichtbare Nachrichten, Neu erzeugen, Kosten und
    Export mit Sonderfällen belastet; so bleibt eine Blase = eine Nachricht,
    Kosten summieren sich an einer Stelle, und der Verlauf geht trotzdem
    wörtlich (inkl. ``provider_state``) an das Modell – auch nach einer Pause.

    Usage und Kosten werden über alle Runden der Antwort summiert.
    """
    return _Loop(turn).run()
