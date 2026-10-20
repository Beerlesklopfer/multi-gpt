"""Mitteilungen innerhalb von MultiGPT (A15), keine E-Mail.

Der Besitzer eines geteilten Chats erfährt, wenn ein Empfänger mit Recht D den
Chat löscht, archiviert bzw. aus dem Archiv holt oder eine Antwort samt Frage
und Folgeverlauf löscht. Handelt der Besitzer selbst, entsteht nichts.

Der Text nennt nur den Anzeigenamen des Handelnden (``sharing.display_name``)
und den Titel des Chats – keine Inhalte. Beim Löschen des ganzen Chats verweist
die Mitteilung nicht auf ihn (er ist weg), der Titel steht dann nur im Text.

Angezeigt im persönlichen Menü („Mitteilungen“ mit Zahl der ungelesenen) und
auf der Seite ``/mitteilungen/`` (``views_notices.py``). Gelesene Mitteilungen
löscht der Worker nach ``RETENTION_DAYS`` Tagen (``cleanup``).
"""

from __future__ import annotations

from datetime import timedelta

from django.utils import timezone

from . import sharing
from .models import Conversation, Notice

RETENTION_DAYS = 90

DELETED = "deleted"
ARCHIVED = "archived"
UNARCHIVED = "unarchived"
EXCHANGE_DELETED = "exchange_deleted"
VERSION_DELETED = "version_deleted"

_TEXTS = {
    DELETED: "{actor} hat deinen geteilten Chat „{title}“ gelöscht.",
    ARCHIVED: "{actor} hat deinen geteilten Chat „{title}“ archiviert.",
    UNARCHIVED: "{actor} hat deinen geteilten Chat „{title}“ aus dem Archiv geholt.",
    EXCHANGE_DELETED: (
        "{actor} hat in deinem geteilten Chat „{title}“ eine Antwort samt Frage "
        "und Folgeverlauf gelöscht."
    ),
    VERSION_DELETED: (
        "{actor} hat in deinem geteilten Chat „{title}“ eine Version einer Antwort gelöscht."
    ),
}


def chat_title(conversation: Conversation) -> str:
    return " ".join((conversation.title or "Neuer Chat").split())


def notify_owner(conversation: Conversation, actor, action: str) -> Notice | None:
    """Mitteilung an den Besitzer, wenn ``actor`` nicht der Besitzer ist."""
    if actor is None or conversation.user_id == actor.pk:
        return None
    text = _TEXTS[action].format(actor=sharing.display_name(actor), title=chat_title(conversation))
    return Notice.objects.create(
        recipient_id=conversation.user_id,
        text=text,
        conversation=None if action == DELETED else conversation,
    )


def unread_count(user) -> int:
    if not getattr(user, "is_authenticated", False):
        return 0
    return Notice.objects.filter(recipient=user, read__isnull=True).count()


def mark_read(user, pk: int | None = None) -> int:
    """Eine (``pk``) oder alle ungelesenen Mitteilungen von ``user`` als gelesen."""
    qs = Notice.objects.filter(recipient=user, read__isnull=True)
    if pk is not None:
        qs = qs.filter(pk=pk)
    return qs.update(read=timezone.now())


def cleanup(now=None) -> int:
    """Gelesene Mitteilungen älter als ``RETENTION_DAYS`` löschen (Worker)."""
    cutoff = (now or timezone.now()) - timedelta(days=RETENTION_DAYS)
    deleted, _ = Notice.objects.filter(read__isnull=False, read__lt=cutoff).delete()
    return deleted
