"""Bestehende Chats in Gesprächsbäume umwandeln (Nachrichten bearbeiten, Versionen).

Vorwärts: Je Chat werden die Nutzer- und Assistant-Nachrichten in der
Reihenfolge (created, id) verkettet. ``parent`` jeder Nachricht ist die letzte
*sichtbare* (nicht ``superseded``) Nachricht davor. Damit werden durch „Neu
erzeugen“ ersetzte Antworten Geschwister der Antwort, die sie ersetzt hat
(gleicher parent, die Frage davor), und bekommen den Status ``complete``
(Inhalt, Tokens und Kosten bleiben). ``current_leaf`` ist die letzte sichtbare
Nachricht. Andere Rollen (system/tool) bleiben ohne parent außerhalb des Baums.

Rückwärts: Alles, was nicht auf dem angezeigten Pfad liegt, wird wieder
``superseded`` (also unsichtbar); die Felder entfernt danach 0009 rückwärts.
"""

from django.db import migrations

CHAT_ROLES = ("user", "assistant")
SUPERSEDED = "superseded"
COMPLETE = "complete"


def link_messages(apps, schema_editor):
    Conversation = apps.get_model("chat", "Conversation")
    Message = apps.get_model("chat", "Message")
    # Nur noch nicht umgewandelte Chats: ein zweiter Lauf ändert nichts.
    pending = Conversation.objects.filter(current_leaf__isnull=True).only("pk")
    for conversation in pending.iterator():
        messages = list(
            Message.objects.filter(conversation_id=conversation.pk, role__in=CHAT_ROLES)
            .only("pk", "status", "parent_id")
            .order_by("created", "id")
        )
        if not messages:
            continue
        last_visible = None
        for message in messages:
            message.parent_id = last_visible.pk if last_visible else None
            if message.status == SUPERSEDED:
                message.status = COMPLETE
            else:
                last_visible = message
        Message.objects.bulk_update(messages, ["parent", "status"], batch_size=500)
        leaf = last_visible or messages[-1]
        Conversation.objects.filter(pk=conversation.pk).update(current_leaf_id=leaf.pk)


def unlink_messages(apps, schema_editor):
    Conversation = apps.get_model("chat", "Conversation")
    Message = apps.get_model("chat", "Message")
    for conversation in Conversation.objects.all().only("pk", "current_leaf_id").iterator():
        if conversation.current_leaf_id is None:
            continue  # ohne angezeigten Zweig nichts verstecken
        rows = Message.objects.filter(
            conversation_id=conversation.pk, role__in=CHAT_ROLES
        ).values_list("pk", "parent_id")
        parents = dict(rows)
        path = set()
        node = conversation.current_leaf_id
        while node is not None and node in parents and node not in path:
            path.add(node)
            node = parents[node]
        off_path = [pk for pk in parents if pk not in path]
        if off_path:
            Message.objects.filter(pk__in=off_path).update(status=SUPERSEDED)


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0009_message_branches"),
    ]

    operations = [
        migrations.RunPython(link_messages, unlink_messages),
    ]
