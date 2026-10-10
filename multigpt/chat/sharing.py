"""Chats teilen (RWUD): Rechte, Ansicht je Empfänger, Datenschutz, Kopie.

Freigaben liegen in ``Share`` (wie bei Sammlungen), an eine Gruppe oder ein
einzelnes Konto. Lesen (R) ist immer enthalten, dazu einzeln:

- **W** ``can_write``: Nachrichten senden (mit Anhängen), Antworten neu
  erzeugen (neuer Zweig), Modell für die eigene Nachricht wählen.
- **U** ``can_update``: Nachrichten bearbeiten (neue Version, braucht zum
  Senden zusätzlich W), Chat umbenennen, System-Prompt ändern.
- **D** ``can_delete``: Chat archivieren bzw. löschen – für alle.

Nur der Besitzer verwaltet Freigaben. Geprüft wird immer über
``permissions.can()``, also bei jeder Anfrage neu; ein Widerruf greift sofort,
im laufenden Stream spätestens beim nächsten Zwischenspeichern
(``check_turn``).

**Ansicht je Konto (current_leaf):** Der Besitzer sieht
``Conversation.current_leaf`` (Hauptpfad). Empfänger folgen dem Hauptpfad, bis
sie eine andere Version wählen oder auf einem Nebenzweig schreiben; dann
merkt sich ``ConversationView`` ihr eigenes Ende. Neue Nachrichten rücken den
Hauptpfad nur vor, wenn sie direkt an ihm hängen (Vorspulen); wer genau am
Elternteil steht, wandert mit. Konflikte (zwei senden gleichzeitig) löst die
Zeilensperre in ``services.prepare_turn``: der zweite Zug hängt an seinem
Elternteil und wird ggf. eine weitere Version; mit ``leaf`` im Request lehnt
der Server einen veralteten Stand mit 409 ab (``STALE_MESSAGE``).

**Datenschutz:** Antworten bekommen den Kontext des Absenders (Rolle,
Zitierstil, Sammlungen, Werkzeuge). Werkzeugrunden anderer Personen gehen nur
als Antworttext an das Modell (``own_rounds``). Dokumentquellen aus
Sammlungen, die der Betrachter nicht lesen darf, zeigen nur Titel und Seite
ohne Link (``redact_sources``).
"""

from __future__ import annotations

from dataclasses import dataclass

from django.db import transaction
from django.db.models import Count, Exists, Max, OuterRef, Q
from django.urls import reverse

from multigpt.accounts.permissions import Action, applicable_shares, can

from . import attachments as chat_attachments
from . import citations
from .models import (
    Attachment,
    Conversation,
    ConversationView,
    Message,
    Share,
    SourceRef,
)

MSG_REVOKED = "Die Freigabe für diesen Chat wurde beendet."
STALE_MESSAGE = (
    "Inzwischen gibt es neue Nachrichten in diesem Chat. Bitte neu laden und dann senden."
)
RIGHT_NAMES = (
    ("can_write", "W", "Schreiben"),
    ("can_update", "U", "Bearbeiten"),
    ("can_delete", "D", "Löschen"),
)


class AccessRevoked(Exception):
    """Die Freigabe wurde während einer laufenden Antwort entzogen."""


# --- Rechte ----------------------------------------------------------------------


def rights_label(write: bool, update: bool, delete: bool) -> str:
    """Kurzform „R“, „RW“, „RWUD“ …"""
    return "R" + "".join(
        letter
        for (_, letter, _), on in zip(RIGHT_NAMES, (write, update, delete), strict=True)
        if on
    )


def rights_text(write: bool, update: bool, delete: bool) -> str:
    """Langform für Menschen, z. B. „Lesen, Schreiben, Bearbeiten“."""
    names = ["Lesen"] + [
        name for (_, _, name), on in zip(RIGHT_NAMES, (write, update, delete), strict=True) if on
    ]
    return ", ".join(names)


def display_name(user) -> str:
    if user is None:
        return "Gelöschtes Konto"
    return str(user)


@dataclass(frozen=True)
class Access:
    """Wirksame Rechte eines Kontos an einem Chat (Vereinigung aller Freigaben)."""

    read: bool = False
    write: bool = False
    update: bool = False
    delete: bool = False
    owner: bool = False
    shared: bool = False  # Zugriff über eine Freigabe (Empfänger)

    @property
    def label(self) -> str:
        return rights_label(self.write, self.update, self.delete)

    @property
    def text(self) -> str:
        return rights_text(self.write, self.update, self.delete)


def access_for(user, conversation: Conversation) -> Access:
    """Rechte von ``user`` an ``conversation`` (über ``can()``)."""
    if conversation.user_id == user.pk:
        return Access(True, True, True, True, owner=True)
    if not can(user, Action.READ, conversation):
        return Access()
    shared = applicable_shares(user, conversation).exists()
    return Access(
        read=True,
        write=can(user, Action.WRITE, conversation),
        update=can(user, Action.UPDATE, conversation),
        delete=can(user, Action.DELETE, conversation),
        shared=shared,
    )


def is_shared(conversation: Conversation) -> bool:
    return Share.objects.filter(conversation=conversation).exists()


def has_other_authors(conversation: Conversation) -> bool:
    """Haben außer dem Besitzer noch andere Personen geschrieben?"""
    return (
        conversation.messages.filter(author__isnull=False)
        .exclude(author_id=conversation.user_id)
        .exists()
    )


# --- Ansicht je Konto (current_leaf) ---------------------------------------------


def bind_viewer(conversation: Conversation, user) -> Conversation:
    """Merkt sich am Objekt, wer den Chat ansieht (für ``services``)."""
    conversation.viewer_id = getattr(user, "pk", None)
    return conversation


def foreign_viewer_id(conversation: Conversation) -> int | None:
    """ID des Betrachters, wenn er nicht der Besitzer ist, sonst None."""
    viewer_id = getattr(conversation, "viewer_id", None)
    if viewer_id is None or viewer_id == conversation.user_id:
        return None
    return viewer_id


def _main_leaf_id(conversation: Conversation) -> int | None:
    return (
        Conversation.objects.filter(pk=conversation.pk)
        .values_list("current_leaf_id", flat=True)
        .first()
    )


def view_leaf_id(conversation: Conversation, viewer_id: int) -> int | None:
    """Ende des angezeigten Zweigs für einen Empfänger: eigener Eintrag, sonst Hauptpfad."""
    own = (
        ConversationView.objects.filter(conversation=conversation, user_id=viewer_id)
        .values_list("current_leaf_id", flat=True)
        .first()
    )
    return own if own is not None else _main_leaf_id(conversation)


def move_leaf(conversation: Conversation, message: Message) -> None:
    """Neue Nachricht wird das angezeigte Ende des Absenders (Aufruf unter der
    Zeilensperre aus ``services.append_message``).

    Besitzer: Hauptpfad wie bisher. Empfänger: Hauptpfad nur, wenn die
    Nachricht direkt daran hängt (Vorspulen), sonst eigener Eintrag. Andere
    Empfänger, deren Ansicht genau am Elternteil endet, wandern mit.
    """
    parent_id = message.parent_id
    viewer_id = foreign_viewer_id(conversation)
    if parent_id is not None:
        followers = ConversationView.objects.filter(
            conversation=conversation, current_leaf_id=parent_id
        )
        if viewer_id is not None:
            followers = followers.exclude(user_id=viewer_id)
        followers.update(current_leaf=message)
    if viewer_id is None:
        Conversation.objects.filter(pk=conversation.pk).update(current_leaf=message)
        conversation.current_leaf = message
        return
    advanced = Conversation.objects.filter(pk=conversation.pk, current_leaf_id=parent_id).update(
        current_leaf=message
    )
    if advanced:
        ConversationView.objects.filter(conversation=conversation, user_id=viewer_id).delete()
    else:
        ConversationView.objects.update_or_create(
            conversation=conversation, user_id=viewer_id, defaults={"current_leaf": message}
        )


def set_view_leaf(conversation: Conversation, leaf_id: int) -> None:
    """Empfänger schaltet die Version um: nur seine eigene Ansicht."""
    viewer_id = foreign_viewer_id(conversation)
    if viewer_id is None:
        raise ValueError("set_view_leaf nur für Empfänger")
    if leaf_id == _main_leaf_id(conversation):
        ConversationView.objects.filter(conversation=conversation, user_id=viewer_id).delete()
    else:
        ConversationView.objects.update_or_create(
            conversation=conversation, user_id=viewer_id, defaults={"current_leaf_id": leaf_id}
        )


def state_stamp(conversation: Conversation) -> str:
    """Kurzer Stand des Chats für den Hinweis „Neue Nachrichten“ (Abfrage beim Fokus).

    Neue Nachrichten und fertige Antworten ändern ihn; Umschalten von Versionen
    und Umbenennen nicht (``updated`` bleibt dabei stehen).
    """
    agg = conversation.messages.aggregate(n=Count("pk"), last=Max("pk"))
    updated = (
        Conversation.objects.filter(pk=conversation.pk).values_list("updated", flat=True).first()
    )
    stamp = int(updated.timestamp() * 1000) if updated else 0
    return f"{agg['last'] or 0}-{agg['n']}-{stamp}"


# --- Laufende Antwort und Verlauf ---------------------------------------------------


def check_turn(turn) -> None:
    """Darf der Absender noch schreiben? Sonst ``AccessRevoked`` (Widerruf im Stream)."""
    conversation = turn.conversation
    if turn.user.pk == conversation.user_id:
        return
    fresh = Conversation.objects.filter(pk=conversation.pk).first()
    if fresh is None or not can(turn.user, Action.WRITE, fresh):
        raise AccessRevoked


def payer_id(message: Message, conversation: Conversation) -> int:
    """Wer die Nachricht verfasst bzw. ausgelöst hat (Altdaten: Besitzer)."""
    return message.author_id or conversation.user_id


def own_rounds(message: Message, conversation: Conversation, user) -> bool:
    """Werkzeugrunden (Ergebnisse von MCP-Servern, Dokument-Werkzeugen) gehen
    nur an das Modell, wenn der Absender diese Antwort selbst ausgelöst hat."""
    if user is None:
        return True
    return payer_id(message, conversation) == user.pk


def may_confirm(user, message: Message, conversation: Conversation) -> bool:
    """MCP-Rückfrage bestätigt nur, wer die Antwort ausgelöst hat."""
    return user is not None and payer_id(message, conversation) == user.pk


def redact_sources(refs, items: list[dict], user) -> list[dict]:
    """Dokumentquellen aus Sammlungen, die ``user`` nicht lesen darf: nur Titel
    und Seite, ohne Link und ohne Literaturangaben (``restricted``)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return items
    chunk_ids = {r.chunk_id for r in refs if r.kind == SourceRef.Kind.DOCUMENT and r.chunk_id}
    if not chunk_ids:
        return items
    from .models import Chunk
    from .rag.search import readable_collections

    hidden = set(
        Chunk.objects.filter(pk__in=chunk_ids)
        .exclude(document__collection__in=readable_collections(user))
        .values_list("pk", flat=True)
    )
    if not hidden:
        return items
    for ref, item in zip(refs, items, strict=True):
        if ref.chunk_id not in hidden:
            continue
        where = citations.Locator(ref.page, ref.page_end)
        bib = citations.Reference.from_dict({"title": ref.title})
        style = item.get("style") or citations.DEFAULT_STYLE
        for key in ("paragraph", "paragraph_end", "section", "section_end"):
            item.pop(key, None)
        item.update(
            {
                "url": "",
                "restricted": True,
                "location": where.label(),
                "entry": citations.entry(bib, style),
                "short": citations.short(bib, style, where),
                "formats": citations.all_formats(bib, where),
                "label": citations.entry(bib, style),
            }
        )
    return items


# --- Seitenleiste und Seitenkontext ------------------------------------------------


def shared_with(user, query: str = "", archived: bool = False, limit: int = 100):
    """Chats anderer Konten, die mit ``user`` geteilt sind („Mit mir geteilt“)."""
    shares = (
        Share.objects.filter(conversation__isnull=False)
        .filter(Q(user=user) | Q(group__in=user.groups.values("pk")))
        .exclude(left_by=user)
    )
    qs = (
        Conversation.objects.filter(pk__in=shares.values("conversation_id"), archived=archived)
        .exclude(user=user)
        .select_related("user")
    )
    if query:
        qs = qs.filter(title__icontains=query)
    return qs.order_by("-updated", "-pk")[:limit]


def with_shared_flag(queryset):
    """Eigene Chats mit ``is_shared`` (Symbol in der Seitenleiste)."""
    return queryset.annotate(is_shared=Exists(Share.objects.filter(conversation_id=OuterRef("pk"))))


def page_context(user, conversation: Conversation) -> dict:
    """Rechte und Anzeige für die Chatansicht (Kopf, Knöpfe, Verfasser)."""
    access = access_for(user, conversation)
    shared = is_shared(conversation)
    return {
        "can_write": access.write,
        "can_update": access.update,
        "can_delete": access.delete,
        "is_owner": access.owner,
        "share_access": access,
        "chat_is_shared": shared,
        "can_manage_shares": access.owner and can(user, Action.SHARE),
        "can_copy_chat": access.shared and can(user, Action.CHAT),
        # Versionen umschalten: Besitzer (Hauptpfad) und Empfänger (eigene Ansicht).
        "can_switch_versions": access.owner or access.shared,
        "chat_owner_id": conversation.user_id,
        "chat_owner_name": display_name(conversation.user),
        "show_authors": shared or has_other_authors(conversation),
        "share_stamp": state_stamp(conversation) if shared else "",
    }


def author_label(msg: Message, viewer, owner_id: int, owner_name: str, show: bool) -> str:
    """Name an einer Nutzernachricht: „Du“ für eigene, sonst der Name (nur
    wenn mehrere Personen im Chat sind)."""
    author_id = msg.author_id or owner_id
    if not show or viewer is None or author_id == viewer.pk:
        return "Du"
    if msg.author_id:
        return display_name(msg.author)
    return owner_name


# --- Aktionen der Empfänger ------------------------------------------------------------


def leave(user, conversation: Conversation) -> bool:
    """„Aus meiner Liste entfernen“: Freigabe an das Konto löschen, bei
    Gruppenfreigaben austragen. False, wenn es keine Freigabe gab."""
    shares = list(applicable_shares(user, conversation))
    if not shares:
        return False
    with transaction.atomic():
        for share in shares:
            if share.user_id == user.pk:
                share.delete()
            else:
                share.left_by.add(user)
        ConversationView.objects.filter(conversation=conversation, user=user).delete()
    return True


def copy_conversation(user, conversation: Conversation) -> Conversation:
    """„Als eigene Kopie fortsetzen“: angezeigten Pfad in einen neuen eigenen Chat.

    Übernommen werden Nachrichten (ohne Kosten, ohne Werkzeugrunden), Quellen
    und Anhänge. Anhänge verweisen auf dieselbe Datei (``attachments.delete_files``
    löscht eine Datei erst, wenn kein Anhang sie mehr nutzt); Besitzer der Kopie
    ist das neue Konto. Werkzeugaufrufe selbst werden nicht kopiert.
    """
    from . import services

    path = services.visible_messages(bind_viewer(conversation, user))
    default_model = conversation.default_model
    if default_model is not None and not can(user, Action.USE_MODEL, default_model):
        default_model = None
    title = (conversation.title or "Neuer Chat").strip()
    suffix = " (Kopie)"
    max_len = Conversation._meta.get_field("title").max_length
    title = title[: max_len - len(suffix)] + suffix
    with transaction.atomic():
        copy = Conversation.objects.create(
            user=user,
            title=title,
            default_model=default_model,
            system_prompt=conversation.system_prompt,
            temperature=conversation.temperature,
            reasoning_effort=conversation.reasoning_effort,
        )
        parent = None
        for msg in path:
            status = msg.status
            if status == Message.Status.AWAITING_CONFIRMATION:
                status = Message.Status.ABORTED
            state = msg.tool_state if isinstance(msg.tool_state, dict) else {}
            new = Message.objects.create(
                conversation=copy,
                parent=parent,
                role=msg.role,
                content=msg.content,
                model=msg.model,
                author_id=msg.author_id or conversation.user_id,
                status=status,
                error=msg.error,
                tool_state={"notices": state["notices"]} if state.get("notices") else {},
            )
            for att in Attachment.objects.filter(message=msg).order_by("created", "id"):
                clone = chat_attachments.copy_for_message(att, new)
                Attachment.objects.filter(pk=clone.pk).update(owner=user)
            for ref in SourceRef.objects.filter(message=msg).order_by("id"):
                ref.pk = None
                ref.id = None
                ref._state.adding = True
                ref.message = new
                ref.save()
            parent = new
        if parent is not None:
            Conversation.objects.filter(pk=copy.pk).update(current_leaf=parent)
            copy.current_leaf = parent
    return copy


# --- Freigaben verwalten (Besitzer) ------------------------------------------------


def serialize_share(share: Share) -> dict:
    is_group = share.group_id is not None
    return {
        "id": share.pk,
        "kind": "group" if is_group else "user",
        "target_id": share.group_id if is_group else share.user_id,
        "name": share.group.name if is_group else display_name(share.user),
        "can_write": share.can_write,
        "can_update": share.can_update,
        "can_delete": share.can_delete,
        "label": share.rights_label,
        "text": rights_text(share.can_write, share.can_update, share.can_delete),
    }


def conversation_shares(conversation: Conversation) -> list[dict]:
    shares = conversation.shares.select_related("group", "user")
    rows = [serialize_share(s) for s in shares]
    return sorted(rows, key=lambda r: (r["kind"] != "user", r["name"].casefold()))


def recipients(owner) -> dict:
    """Auswahl im Dialog: aktive Konten (ohne den Besitzer) und alle Gruppen."""
    from django.contrib.auth import get_user_model

    from multigpt.accounts.models import UserGroup

    users = get_user_model().objects.filter(is_active=True).exclude(pk=owner.pk)
    groups = UserGroup.objects.order_by("name")
    user_rows = sorted(
        ({"id": u.pk, "name": display_name(u)} for u in users),
        key=lambda r: r["name"].casefold(),
    )
    return {
        "users": user_rows,
        "groups": [{"id": g.pk, "name": g.name} for g in groups],
    }


def share_url(conversation: Conversation) -> str:
    return reverse("chat:api_conversation_shares", args=[conversation.pk])
