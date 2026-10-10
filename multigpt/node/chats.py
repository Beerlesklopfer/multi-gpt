"""Chats „über API“ (M15): Was ein Orchestrator über einen Key auslöst, bleibt
in MultiGPT sichtbar.

- ``ask`` legt je Aufruf einen neuen Chat an (Titel „API: …“), sofern kein
  bestehender Chat des Kontos angegeben ist. Ein gemeinsamer Chat je Key würde
  den Verlauf unabhängiger Aufgaben bei jeder Frage erneut an das Modell
  schicken (Kosten, vermischter Kontext).
- Direkte Werkzeugaufrufe (``tools.run``) landen in einem Chat je Key
  („API: <Key> – Werkzeuge“), damit erzeugte Dateien als Anhänge eines Chats
  gespeichert, im Verlauf sichtbar und mit den üblichen Rechten abrufbar sind.
"""

from __future__ import annotations

from django.db import transaction

from multigpt.chat.models import Conversation
from multigpt.chat.titles import title_from

from .models import ApiKey

API_PREFIX = "API: "
TITLE_LENGTH = 60


def ask_title(key: ApiKey, prompt: str) -> str:
    return f"{API_PREFIX}{title_from(prompt, TITLE_LENGTH - len(API_PREFIX))}"[:200]


def new_ask_chat(key: ApiKey, prompt: str) -> Conversation:
    return Conversation.objects.create(user=key.owner, title=ask_title(key, prompt))


def tool_chat(key: ApiKey) -> Conversation:
    """Chat für Werkzeugaufrufe dieses Keys; nach Löschen durch das Mitglied neu."""
    with transaction.atomic():
        locked = ApiKey.objects.select_for_update().get(pk=key.pk)
        conversation = None
        if locked.tool_conversation_id:
            conversation = Conversation.objects.filter(
                pk=locked.tool_conversation_id, user=key.owner
            ).first()
        if conversation is None:
            conversation = Conversation.objects.create(
                user=key.owner, title=f"{API_PREFIX}{key.name} – Werkzeuge"[:200]
            )
            ApiKey.objects.filter(pk=key.pk).update(tool_conversation=conversation)
        key.tool_conversation = conversation
    return conversation
