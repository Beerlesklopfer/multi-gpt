"""Nachrichten bearbeiten, Neu erzeugen und Umschalten (Versionen, Gesprächsbaum)."""

# Fixtures werden aus test_api_stream importiert und als Parameter wieder benutzt.
# ruff: noqa: F811

import json
from decimal import Decimal

import pytest
from django.urls import reverse

from multigpt.accounts.models import UserGroup
from multigpt.chat import services
from multigpt.chat.models import Conversation, Message, Share
from multigpt.chat.providers.base import Delta, Done, Usage
from tests.test_api_stream import (  # noqa: F401
    OK_EVENTS,
    adult,
    ai_model,
    conversation,
    fake,
    make_user,
    parse_chunks,
    parse_sse,
    post,
    provider,
    url,
)

pytestmark = pytest.mark.django_db


def branch_url(conversation):
    return reverse("chat:api_branch", args=[conversation.pk])


def switch(client, conversation, **data):
    return client.post(branch_url(conversation), json.dumps(data), content_type="application/json")


def ask(client, conversation, ai_model, fake, content, answer="Antwort", **extra):
    """Eine Runde senden; liefert (start-Daten, Adapter-Aufrufe)."""
    holder = fake([Delta(answer), Usage(12, 3), Done("stop")])
    events = parse_sse(post(client, conversation, content=content, model=ai_model.pk, **extra))
    assert events[-1] == ("done", {"status": "complete"}), events
    return events[0][1], holder["adapter"].calls


def contents(client, conversation):
    return [m["content"] for m in client.get(url(conversation)).json()]


# --- Bearbeiten ---------------------------------------------------------------------


def test_edit_creates_branch_and_keeps_old(client, conversation, ai_model, fake):
    first, _ = ask(client, conversation, ai_model, fake, "Frage 1", "A1")
    ask(client, conversation, ai_model, fake, "Frage 2", "A2")
    ask(client, conversation, ai_model, fake, "Frage 3", "A3")
    q2 = Message.objects.get(content="Frage 2")

    start, calls = ask(client, conversation, ai_model, fake, "Frage 2b", "A2b", edit_of=q2.pk)
    new_q = Message.objects.get(pk=start["user_message_id"])
    assert new_q.parent_id == q2.parent_id == first["assistant_message_id"]
    assert start["parent_id"] == q2.parent_id
    # Verlauf an das Modell: nur der neue Pfad bis zur bearbeiteten Frage.
    assert [m.content for m in calls[0]["messages"]] == ["Frage 1", "A1", "Frage 2b"]

    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Frage 1", "A1", "Frage 2b", "A2b"]
    edited = data[2]
    assert edited["sibling_ids"] == [q2.pk, new_q.pk]
    assert (edited["sibling_index"], edited["sibling_count"]) == (1, 2)
    assert edited["parent_id"] == q2.parent_id
    assert data[0]["sibling_count"] == 1 and data[0]["parent_id"] is None
    # Alter Zweig bleibt vollständig gespeichert.
    assert Message.objects.filter(content__in=["Frage 2", "A2", "Frage 3", "A3"]).count() == 4
    # Titel bleibt beim Bearbeiten unverändert.
    conversation.refresh_from_db()
    assert conversation.title == "Frage 1"


def test_edit_first_message_creates_second_root(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Erste", "A")
    q = Message.objects.get(content="Erste")
    start, calls = ask(client, conversation, ai_model, fake, "Erste neu", "B", edit_of=q.pk)
    assert start["parent_id"] is None
    assert [m.content for m in calls[0]["messages"]] == ["Erste neu"]
    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Erste neu", "B"]
    assert data[0]["sibling_ids"] == [q.pk, start["user_message_id"]]


def test_switch_branch_and_reload(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Frage", "A")
    ask(client, conversation, ai_model, fake, "Weiter", "B")
    q = Message.objects.get(content="Frage")
    ask(client, conversation, ai_model, fake, "Frage neu", "C", edit_of=q.pk)
    conversation.refresh_from_db()
    updated = conversation.updated

    response = switch(client, conversation, message_id=q.pk)
    assert response.status_code == 200
    # Neuestes Blatt im alten Zweig: die Antwort auf "Weiter".
    assert [m["content"] for m in response.json()] == ["Frage", "A", "Weiter", "B"]
    assert response.json()[0]["sibling_index"] == 0
    # Nach dem Neuladen (GET, Seite) bleibt der gewählte Zweig.
    assert contents(client, conversation) == ["Frage", "A", "Weiter", "B"]
    page = client.get(reverse("chat:conversation", args=[conversation.pk]))
    assert [m.content for m in page.context["chat_messages"]] == ["Frage", "A", "Weiter", "B"]
    conversation.refresh_from_db()
    assert conversation.updated == updated  # Umschalten sortiert die Seitenleiste nicht um

    # Neue Nachricht hängt an den angezeigten Zweig.
    _, calls = ask(client, conversation, ai_model, fake, "Noch was", "D")
    assert [m.content for m in calls[0]["messages"]] == ["Frage", "A", "Weiter", "B", "Noch was"]
    # Export folgt ebenfalls dem angezeigten Zweig.
    body = client.get(reverse("chat:conversation_export", args=[conversation.pk])).content
    assert b"Weiter" in body and b"Frage neu" not in body


def test_branch_validation(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Frage", "A")
    for payload in ({}, {"message_id": "1"}, {"message_id": True}, {"message_id": 10**9}):
        response = switch(client, conversation, **payload)
        assert response.status_code == 400, payload
        assert "error" in response.json()
    response = client.post(branch_url(conversation), "kein json", content_type="application/json")
    assert response.status_code == 400
    assert client.get(branch_url(conversation)).status_code == 405


def test_edit_of_validation(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Frage", "A")
    answer = Message.objects.get(role="assistant")
    other = Conversation.objects.create(user=make_user("adult", "fremd"))
    foreign = services.append_message(other, role="user", content="privat")
    fake(OK_EVENTS)
    for edit_of in (answer.pk, foreign.pk, 10**9, "1", True):
        response = post(client, conversation, content="x", model=ai_model.pk, edit_of=edit_of)
        assert response.status_code == 400, edit_of
    # Bearbeiten und Neu erzeugen zugleich ist widersprüchlich.
    q = Message.objects.get(content="Frage")
    response = post(client, conversation, regenerate=True, model=ai_model.pk, edit_of=q.pk)
    assert response.status_code == 400
    assert Message.objects.filter(conversation=conversation).count() == 2
    assert Message.objects.get(pk=foreign.pk).content == "privat"


def test_permissions(client, adult, ai_model, fake):
    owner = make_user("adult", "besitzer")
    conv = Conversation.objects.create(user=owner)
    q = services.append_message(conv, role="user", content="Frage")
    services.append_message(conv, role="assistant", content="A")
    fake(OK_EVENTS)
    # Fremder Chat: 404 – auch für Umschalten und Bearbeiten.
    assert switch(client, conv, message_id=q.pk).status_code == 404
    response = post(client, conv, content="x", model=ai_model.pk, edit_of=q.pk)
    assert response.status_code == 404
    # Nur lesend geteilt: sehen ja, umschalten/bearbeiten nein.
    group = UserGroup.objects.create(name="Lesegruppe")
    adult.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=False)
    assert client.get(url(conv)).status_code == 200
    assert switch(client, conv, message_id=q.pk).status_code == 403
    response = post(client, conv, content="x", model=ai_model.pk, edit_of=q.pk)
    assert response.status_code == 403
    # Mit Schreibrecht erlaubt.
    Share.objects.filter(conversation=conv).update(can_write=True)
    assert switch(client, conv, message_id=q.pk).status_code == 200
    events = parse_sse(post(client, conv, content="x", model=ai_model.pk, edit_of=q.pk))
    assert events[-1][0] == "done"
    assert switch(client, conv, message_id=q.pk).status_code == 200


def test_branch_requires_login(client, conversation):
    client.logout()
    response = switch(client, conversation, message_id=1)
    assert response.status_code == 403


# --- Neu erzeugen -------------------------------------------------------------------


def test_regenerate_creates_versions(client, conversation, ai_model, fake):
    first, _ = ask(client, conversation, ai_model, fake, "Frage", "Hallo")
    old_id = first["assistant_message_id"]

    holder = fake([Delta("Neu"), Done()])
    events = parse_sse(
        post(client, conversation, regenerate=True, model=ai_model.pk, message_id=old_id)
    )
    start = events[0][1]
    assert start["user_message_id"] is None
    assert start["parent_id"] == first["user_message_id"]
    new_id = start["assistant_message_id"]
    # Alte Antwort bleibt als Version mit Tokens und Kosten (Verbrauch/Budget, M6).
    old = Message.objects.get(pk=old_id)
    assert old.status == Message.Status.COMPLETE
    assert (old.content, old.tokens_in, old.tokens_out) == ("Hallo", 12, 3)
    assert old.cost == Decimal("0.000060")
    # ... aber nicht im Verlauf an das Modell.
    assert [m.content for m in holder["adapter"].calls[0]["messages"]] == ["Frage"]
    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Frage", "Neu"]
    assert data[1]["sibling_ids"] == [old_id, new_id]
    assert (data[1]["sibling_index"], data[1]["sibling_count"]) == (1, 2)

    # Ohne message_id: letzte Antwort des Pfads -> dritte Version.
    fake([Delta("Noch neuer"), Done()])
    parse_sse(post(client, conversation, regenerate=True, model=ai_model.pk))
    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Frage", "Noch neuer"]
    assert (data[1]["sibling_index"], data[1]["sibling_count"]) == (2, 3)
    assert not Message.objects.filter(status=Message.Status.SUPERSEDED).exists()

    # Zurück auf Version 1, dann Folgefrage: Verlauf enthält diese Version.
    assert switch(client, conversation, message_id=old_id).status_code == 200
    _, calls = ask(client, conversation, ai_model, fake, "Weiter")
    assert [m.content for m in calls[0]["messages"]] == ["Frage", "Hallo", "Weiter"]
    page = client.get(reverse("chat:conversation", args=[conversation.pk]))
    assert [m.pk for m in page.context["chat_messages"]][1] == old_id


def test_regenerate_older_answer_in_path(client, conversation, ai_model, fake):
    first, _ = ask(client, conversation, ai_model, fake, "Eins", "A1")
    ask(client, conversation, ai_model, fake, "Zwei", "A2")
    holder = fake([Delta("A1 neu"), Done()])
    events = parse_sse(
        post(
            client,
            conversation,
            regenerate=True,
            model=ai_model.pk,
            message_id=first["assistant_message_id"],
        )
    )
    assert events[-1] == ("done", {"status": "complete"})
    assert [m.content for m in holder["adapter"].calls[0]["messages"]] == ["Eins"]
    assert contents(client, conversation) == ["Eins", "A1 neu"]


def test_regenerate_validation(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Frage", "A")
    q = Message.objects.get(role="user")
    other = Conversation.objects.create(user=make_user("adult", "fremd"))
    services.append_message(other, role="user", content="privat")
    foreign = services.append_message(other, role="assistant", content="geheim")
    fake(OK_EVENTS)
    for message_id in (q.pk, foreign.pk, 10**9, "x"):
        response = post(
            client, conversation, regenerate=True, model=ai_model.pk, message_id=message_id
        )
        assert response.status_code == 400, message_id
    assert Message.objects.filter(conversation=conversation).count() == 2


# --- Abbruch während des Bearbeitens ----------------------------------------------


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_abort_during_edit_stream(client, conversation, ai_model, fake):
    ask(client, conversation, ai_model, fake, "Frage", "Alt")
    q = Message.objects.get(content="Frage")
    holder = fake([Delta("Erster "), Delta("Zweiter"), Done()])
    response = post(client, conversation, content="Frage neu", model=ai_model.pk, edit_of=q.pk)
    chunks = iter(response.streaming_content)
    start = parse_chunks(next(chunks).decode())[0][1]
    assert parse_chunks(next(chunks).decode()) == [("delta", {"text": "Erster "})]
    response.close()

    answer = Message.objects.get(pk=start["assistant_message_id"])
    assert answer.status == Message.Status.ABORTED and answer.content == "Erster "
    assert holder["adapter"].closed
    # Der neue Zweig bleibt angezeigt, der alte als Version erhalten.
    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Frage neu", "Erster "]
    assert data[0]["sibling_count"] == 2
    assert switch(client, conversation, message_id=q.pk).json()[1]["content"] == "Alt"
