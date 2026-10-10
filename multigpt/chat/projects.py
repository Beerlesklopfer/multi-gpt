"""Projekte (M5-07): Chats gruppieren, mit gemeinsamen Vorgaben.

Ein Projekt (``models.Project``) gehört einem Konto und ist privat. Es bündelt
Chats des Besitzers (``Conversation.project``) und gibt ihnen vor:

- **Anweisungen** (``instructions``): zusätzlicher Teil des System-Prompts
  aller Chats im Projekt, nach dem festen Prompt der Rolle und vor dem
  System-Prompt des Chats. Sie sind Nutzerinhalt und werden als solcher
  gekennzeichnet (``instruction_block``), nicht als Hinweis von MultiGPT.
  Sie gelten für jede Antwort im Chat, auch wenn ein Empfänger eines geteilten
  Chats schreibt – der Chat verhält sich für alle gleich; das Projekt selbst
  (Name, Anweisungen) sieht der Empfänger in der Oberfläche nicht.
- **Standardmodell** und **Sammlungen**: nur Vorauswahl im Eingabefeld
  (``page_context``). Modellfreigabe (``can(USE_MODEL)``) und Leserechte der
  Sammlungen gelten weiter; nicht mehr lesbare Sammlungen fallen weg.

Rechte: Nur der Besitzer sieht und ändert ein Projekt; fremde Projekte gibt
es für alle anderen nicht (404, auch für Verwalter). Chats ordnet nur ihr
Besitzer zu, und nur einem eigenen Projekt.

Löschen: Die Chats bleiben (ohne Projekt, ``on_delete=SET_NULL``) oder werden
ausdrücklich mitgelöscht (``delete_project(..., delete_chats=True)``, mit
Anhängen). Archivieren blendet das Projekt samt Chats aus der Seitenleiste aus;
im Archiv erscheinen archivierte Projekte und archivierte Chats.

Später:

- **Teilen:** Freigabe über ``Share`` mit Ziel Projekt (weiterer nullable
  Fremdschlüssel, Constraint „genau ein Ziel“), ``can()`` prüft bei Chats
  zusätzlich die Freigaben ihres Projekts. Noch nicht umgesetzt.
- **Scratchpad (Sammelmappe):** eigenes Modell mit Fremdschlüssel auf
  ``Project`` (CASCADE); ``delete_project`` und die Projektseite sind die
  Stellen, an denen es dazukommt.
"""

from __future__ import annotations

from django.db import transaction
from django.db.models import Count, Q

from multigpt.accounts.permissions import Action, can, model_permitted

from . import attachments as chat_attachments
from . import sharing
from .models import AIModel, Collection, Conversation, Project

NAME_MAX_LENGTH = Project._meta.get_field("name").max_length
DESCRIPTION_MAX_LENGTH = 2_000
INSTRUCTIONS_MAX_LENGTH = 20_000
# Seitenleiste: so viele Chats je Projekt, der Rest auf der Projektseite.
SIDEBAR_CHATS_PER_PROJECT = 20
# Obergrenze der Chats, die für die Seitenleiste insgesamt gelesen werden.
SIDEBAR_CHAT_SCAN = 1_000
SEARCH_MAX_LENGTH = 100

MSG_NOT_FOUND = "Projekt nicht gefunden."


# --- Laden ------------------------------------------------------------------------


def own_projects(user, archived: bool | None = False):
    """Projekte des Kontos; ``archived=None``: alle."""
    if user is None or not user.is_authenticated:
        return Project.objects.none()
    qs = Project.objects.filter(owner=user)
    if archived is not None:
        qs = qs.filter(archived=archived)
    return qs


def get_own_project(user, pk) -> Project | None:
    """Eigenes Projekt oder None (fremd, unbekannt, ungültige ID)."""
    if isinstance(pk, bool):
        return None
    try:
        pk = int(pk)
    except (TypeError, ValueError):
        return None
    return own_projects(user, archived=None).filter(pk=pk).first()


def with_chat_count(queryset):
    return queryset.annotate(chat_count=Count("conversations", distinct=True))


# --- Prompt -----------------------------------------------------------------------


def instruction_block(conversation: Conversation) -> str | None:
    """Anweisungen des Projekts für den System-Prompt, als Nutzerinhalt markiert."""
    if not conversation.project_id:
        return None
    project = conversation.project
    if project is None or project.owner_id != conversation.user_id:
        return None
    text = project.instructions.replace("\r\n", "\n").strip()
    if not text:
        return None
    # Den Block nicht vorzeitig schließen lassen.
    text = text.replace("</projekt_anweisungen", "<\\/projekt_anweisungen")
    name = " ".join(project.name.split()).replace('"', "'")
    return (
        "Der Nutzer hat für alle Chats seines Projekts die folgenden Anweisungen "
        "festgelegt. Sie stammen vom Nutzer (nicht von MultiGPT) und gelten wie "
        "seine eigenen Vorgaben für diesen Chat.\n"
        f'<projekt_anweisungen projekt="{name}">\n{text}\n</projekt_anweisungen>'
    )


# --- Vorauswahl im Eingabefeld ------------------------------------------------------


def usable_default_model(user, project: Project | None) -> AIModel | None:
    """Standardmodell des Projekts, wenn das Konto es nutzen darf (online prüft chat.js)."""
    if project is None or project.default_model_id is None:
        return None
    model = project.default_model
    if model.capability != AIModel.Capability.CHAT or not can(user, Action.USE_MODEL, model):
        return None
    return model


def readable_collection_ids(user, project: Project | None) -> list[int]:
    """Sammlungen des Projekts, die ``user`` (noch) lesen darf."""
    if project is None:
        return []
    from .rag.search import readable_collections

    return sorted(
        project.collections.filter(pk__in=readable_collections(user).values("pk")).values_list(
            "pk", flat=True
        )
    )


def page_context(request, conversation: Conversation | None = None) -> dict:
    """Projekt der Chatansicht: eigener Chat mit Projekt bzw. Startseite mit
    ``?projekt=<id>`` („Neuer Chat im Projekt“). Für Empfänger geteilter Chats
    bleibt das Projekt des Besitzers verborgen."""
    user = request.user
    project = None
    if conversation is not None:
        if conversation.user_id == user.pk and conversation.project_id:
            project = conversation.project
    elif request.GET.get("projekt"):
        project = get_own_project(user, request.GET["projekt"])
    model = usable_default_model(user, project)
    return {
        "chat_project": project,
        "project_default_model_id": model.pk if model else None,
        "project_collection_ids": ",".join(
            str(pk) for pk in readable_collection_ids(user, project)
        ),
    }


# --- Ändern -----------------------------------------------------------------------


def move_conversation(conversation: Conversation, project: Project | None) -> None:
    """Chat einem Projekt zuordnen bzw. herausnehmen (Rechte prüft der Aufrufer).

    ``update()`` statt ``save()``: ``updated`` bleibt, die Reihenfolge der
    Chatliste ändert sich durch das Verschieben nicht.
    """
    if project is not None and project.owner_id != conversation.user_id:
        raise ValueError("Projekt gehört nicht dem Besitzer des Chats.")
    Conversation.objects.filter(pk=conversation.pk).update(project=project)
    conversation.project = project


def delete_project(project: Project, *, delete_chats: bool) -> int:
    """Projekt löschen; mit ``delete_chats`` auch alle seine Chats (mit Anhängen).

    Ohne werden die Chats zu Chats ohne Projekt. Liefert die Zahl der
    gelöschten Chats.
    """
    deleted = 0
    with transaction.atomic():
        if delete_chats:
            for conversation in Conversation.objects.filter(project=project):
                # Dateien der Anhänge nach dem Commit entfernen (wie api_manage).
                chat_attachments.delete_conversation_files(conversation)
                conversation.delete()
                deleted += 1
        else:
            Conversation.objects.filter(project=project).update(project=None)
        project.delete()
    return deleted


# --- Prüfen (API und Formular) ---------------------------------------------------------


def clean_name(raw) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "Ungültiger Name."
    name = " ".join(raw.replace("\x00", " ").split())
    if not name:
        return None, "Bitte einen Namen eingeben."
    if len(name) > NAME_MAX_LENGTH:
        return None, f"Der Name darf höchstens {NAME_MAX_LENGTH} Zeichen lang sein."
    return name, None


def clean_text(raw, max_length: int, label: str) -> tuple[str | None, str | None]:
    if raw is None:
        raw = ""
    if not isinstance(raw, str):
        return None, f"Ungültiger Wert für „{label}“."
    text = raw.replace("\x00", "").replace("\r\n", "\n").strip()
    if len(text) > max_length:
        return None, f"„{label}“ darf höchstens {max_length} Zeichen lang sein."
    return text, None


def clean_default_model(user, raw) -> tuple[AIModel | None, str | None]:
    """Chat-Modell, das das Konto nutzen darf (ohne Budgetprüfung: das Budget
    sperrt nur vorübergehend). ``None`` = kein Standardmodell."""
    if raw is None or raw == "":
        return None, None
    if isinstance(raw, bool):
        return None, "Unbekanntes Modell."
    try:
        pk = int(raw)
    except (TypeError, ValueError):
        return None, "Unbekanntes Modell."
    model = AIModel.objects.select_related("provider").filter(pk=pk).first()
    if model is None or model.capability != AIModel.Capability.CHAT:
        return None, "Unbekanntes Modell."
    if not model_permitted(user, model):
        return None, "Dieses Modell steht dir nicht zur Verfügung."
    return model, None


def clean_collections(user, raw) -> tuple[list[Collection] | None, str | None]:
    """Liste lesbarer Sammlungen; unbekannte oder nicht lesbare -> Fehler."""
    from .rag.search import readable_collections

    if raw is None:
        return [], None
    if not isinstance(raw, list) or any(
        isinstance(pk, bool) or not isinstance(pk, int) for pk in raw
    ):
        return None, "Ungültige Auswahl der Sammlungen."
    ids = set(raw)
    found = list(readable_collections(user).filter(pk__in=ids))
    if len(found) != len(ids):
        return None, "Sammlung nicht gefunden."
    return found, None


def clean_color(raw) -> tuple[str | None, str | None]:
    if raw is None:
        return "", None
    if not isinstance(raw, str) or raw not in Project.Color.values:
        return None, "Ungültige Farbe."
    return raw, None


# --- Anzeige ----------------------------------------------------------------------------


def serialize_project(project: Project, user, *, detail: bool = False) -> dict:
    data = {
        "id": project.pk,
        "name": project.name,
        "color": project.color,
        "pinned": project.pinned,
        "archived": project.archived,
        "chat_count": getattr(project, "chat_count", None),
        "url": project_url(project),
    }
    if data["chat_count"] is None:
        data["chat_count"] = project.conversations.count()
    if detail:
        data.update(
            {
                "description": project.description,
                "instructions": project.instructions,
                "default_model": project.default_model_id,
                "temperature": (
                    None if project.temperature is None else float(project.temperature)
                ),
                "reasoning_effort": project.reasoning_effort,
                "collections": sorted(c.pk for c in project.collections.all()),
                "created": project.created.isoformat(),
                "updated": project.updated.isoformat(),
            }
        )
    return data


def project_url(project: Project) -> str:
    from django.urls import reverse

    return reverse("chat:project_detail", args=[project.pk])


def project_chats(project: Project):
    """Alle Chats des Projekts für die Projektseite, neueste zuerst."""
    return sharing.with_shared_flag(
        Conversation.objects.filter(project=project, user_id=project.owner_id)
    ).order_by("archived", "-updated", "-pk")


def sidebar_projects(
    user, query: str = "", archived: bool = False, active_project_id: int | None = None
) -> list[Project]:
    """Projekte der Seitenleiste mit ihren Chats (``sidebar_chats``).

    Aktive Ansicht: nicht archivierte Projekte mit ihren nicht archivierten
    Chats. Archiv: archivierte Projekte (mit allen Chats) und Projekte mit
    archivierten Chats (nur diese). Suche: Projekte, deren Name passt (mit
    allen Chats der Ansicht), und Chats, deren Titel passt.
    ``more_count``: weitere Chats, die nur die Projektseite zeigt.
    """
    projects = Project.objects.filter(owner=user)
    chats = sharing.with_shared_flag(
        Conversation.objects.filter(user=user, project__isnull=False, project__owner=user)
    )
    if archived:
        projects = projects.filter(Q(archived=True) | Q(conversations__archived=True)).distinct()
        chats = chats.filter(Q(archived=True) | Q(project__archived=True))
    else:
        projects = projects.filter(archived=False)
        chats = chats.filter(archived=False, project__archived=False)
    name_hits: set[int] = set()
    if query:
        name_hits = set(projects.filter(name__icontains=query).values_list("pk", flat=True))
        chats = chats.filter(Q(title__icontains=query) | Q(project_id__in=name_hits))
    groups: dict[int, list[Conversation]] = {}
    for chat in chats.order_by("-updated", "-pk")[:SIDEBAR_CHAT_SCAN]:
        groups.setdefault(chat.project_id, []).append(chat)
    result = []
    for project in projects:
        items = groups.get(project.pk, [])
        if query and project.pk not in name_hits and not items:
            continue
        project.sidebar_chats = items[:SIDEBAR_CHATS_PER_PROJECT]
        project.more_count = max(0, len(items) - SIDEBAR_CHATS_PER_PROJECT)
        project.is_open = bool(query) or project.pk == active_project_id
        result.append(project)
    return result
