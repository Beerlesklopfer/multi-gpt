"""Vergleichsmodus (M6, Plan 8 Punkt 7): mehrere Antworten als Geschwister.

Ablauf der Oberfläche: erste Spalte per POST messages (content, compare),
weitere Spalten per ``regenerate`` mit ``message_id`` der ersten Antwort,
Wahl per POST branch.
"""

# Fixtures werden aus test_api_stream importiert und als Parameter wieder benutzt.
# ruff: noqa: F811

import re
import threading
from decimal import Decimal

import pytest
from django.db import connection
from django.urls import reverse

from multigpt.accounts.models import UserGroup
from multigpt.chat import services, tooling
from multigpt.chat.models import AIModel, Conversation, Message, Share
from multigpt.chat.providers.base import Delta, Done, Usage
from tests.billing_helpers import set_price
from tests.test_api_stream import (  # noqa: F401
    OK_EVENTS,
    adult,
    ai_model,
    conversation,
    fake,
    make_user,
    parse_sse,
    post,
    provider,
    url,
)
from tests.test_branches_api import switch

pytestmark = pytest.mark.django_db


@pytest.fixture
def models(provider, ai_model):
    second = AIModel.objects.create(provider=provider, model_id="m2", display_name="Modell Zwei")
    set_price(second, "1", "4")
    third = AIModel.objects.create(provider=provider, model_id="m3", display_name="Modell Drei")
    return [ai_model, second, third]


def column(client, conversation, fake, ai_model, text, **data):
    """Eine Spalte streamen; liefert die Events."""
    fake([Delta(text), Usage(100, 20), Done("stop")])
    return parse_sse(post(client, conversation, model=ai_model.pk, compare=True, **data))


def run_compare(client, conversation, fake, models, question="Frage"):
    first = column(client, conversation, fake, models[0], "Antwort 1", content=question)
    anchor = first[0][1]["assistant_message_id"]
    events = [first]
    for n, m in enumerate(models[1:], start=2):
        events.append(
            column(
                client,
                conversation,
                fake,
                m,
                f"Antwort {n}",
                regenerate=True,
                message_id=anchor,
            )
        )
    return events


def test_three_columns_become_siblings_and_choice_sets_leaf(client, conversation, models, fake):
    events = run_compare(client, conversation, fake, models)
    for evs in events:
        assert [n for n, _ in evs] == ["start", "delta", "usage", "done"]
        assert evs[-1] == ("done", {"status": "complete"})
    ids = [evs[0][1]["assistant_message_id"] for evs in events]
    question = Message.objects.get(role="user")
    # Reihenfolge = Reihenfolge der Spalten, alle mit derselben Frage.
    answers = list(Message.objects.filter(role="assistant").order_by("created", "id"))
    assert [a.pk for a in answers] == ids
    assert {a.parent_id for a in answers} == {question.pk}
    assert [a.model_id for a in answers] == [m.pk for m in models]
    assert events[1][0][1]["parent_id"] == question.pk

    # Bis zur Wahl bleibt die erste Spalte angezeigt, Standardmodell unverändert.
    conversation.refresh_from_db()
    assert conversation.current_leaf_id == ids[0]
    assert conversation.default_model_id == models[0].pk
    data = client.get(url(conversation)).json()
    assert [m["content"] for m in data] == ["Frage", "Antwort 1"]
    assert data[1]["sibling_ids"] == ids
    assert (data[1]["sibling_index"], data[1]["sibling_count"]) == (0, 3)

    # „Mit dieser Antwort weiter“ (Spalte 2), Modell der Spalte wird Standard.
    assert switch(client, conversation, message_id=ids[1], adopt_model="ja").status_code == 400
    response = switch(client, conversation, message_id=ids[1], adopt_model=True)
    assert response.status_code == 200
    assert [m["content"] for m in response.json()] == ["Frage", "Antwort 2"]
    conversation.refresh_from_db()
    assert conversation.current_leaf_id == ids[1]
    assert conversation.default_model_id == models[1].pk
    # Neuladen: normale Ansicht mit „‹ 2/3 ›“.
    page = client.get(reverse("chat:conversation", args=[conversation.pk]))
    shown = page.context["chat_messages"]
    assert [m.content for m in shown] == ["Frage", "Antwort 2"]
    assert (shown[1].sibling_index, shown[1].sibling_count) == (1, 3)
    assert "Version 2 von 3" in page.content.decode()

    # Weiter im gewählten Zweig: Verlauf an das Modell enthält Antwort 2.
    holder = fake(OK_EVENTS)
    parse_sse(post(client, conversation, content="Weiter", model=models[0].pk))
    sent = [m.content for m in holder["adapter"].calls[0]["messages"]]
    assert sent == ["Frage", "Antwort 2", "Weiter"]


def test_costs_of_all_columns_count(client, conversation, models, fake):
    events = run_compare(client, conversation, fake, models)
    usages = [dict(evs)["usage"] for evs in events]
    # 100 in / 20 out: Modell 1 2,5/10 € je 1 Mio., Modell 2 1/4 €, Modell 3 ohne Preise.
    assert usages[0] == {"tokens_in": 100, "tokens_out": 20, "cost": "0.000450"}
    assert usages[1] == {"tokens_in": 100, "tokens_out": 20, "cost": "0.000180"}
    assert usages[2]["cost"] is None
    costs = Message.objects.filter(role="assistant").values_list("cost", flat=True)
    assert sum(c or 0 for c in costs) == Decimal("0.000630")
    # Nach der Wahl zählen die anderen Spalten weiter.
    ids = [evs[0][1]["assistant_message_id"] for evs in events]
    switch(client, conversation, message_id=ids[2])
    # Normales Umschalten ändert das Standardmodell nicht.
    conversation.refresh_from_db()
    assert conversation.default_model_id == models[0].pk
    assert Message.objects.filter(conversation=conversation, cost__isnull=False).count() == 2


def test_compare_disables_mcp_tools(client, conversation, models, fake, monkeypatch):
    models[0].supports_tools = True
    models[0].save()
    monkeypatch.setattr(tooling, "enabled_server_ids", lambda user, ids, ai_model=None: [42])
    column(client, conversation, fake, models[0], "A", content="Frage", mcp_servers=[42])
    answer = Message.objects.get(role="assistant")
    assert "servers" not in (answer.tool_state or {})
    # Ohne Vergleich gilt die Auswahl wie bisher.
    fake(OK_EVENTS)
    turn = services.prepare_turn(
        conversation.user, conversation, models[0], content="Noch eine", mcp_servers=[42]
    )
    assert turn.assistant_message.tool_state["servers"] == [42]


def test_regenerate_without_compare_still_moves_leaf(client, conversation, models, fake):
    first = column(client, conversation, fake, models[0], "A", content="Frage")
    anchor = first[0][1]["assistant_message_id"]
    fake(OK_EVENTS)
    events = parse_sse(
        post(client, conversation, regenerate=True, model=models[1].pk, message_id=anchor)
    )
    conversation.refresh_from_db()
    assert conversation.current_leaf_id == events[0][1]["assistant_message_id"]
    assert conversation.default_model_id == models[1].pk


@pytest.mark.parametrize("value", ["ja", 1, [], {}])
def test_compare_must_be_bool(client, conversation, ai_model, fake, value):
    fake(OK_EVENTS)
    response = post(client, conversation, content="x", model=ai_model.pk, compare=value)
    assert response.status_code == 400
    assert response.json() == {"error": "Ungültige Anfrage."}
    assert not Message.objects.exists()


def test_column_error_is_json_and_other_columns_continue(client, conversation, models, fake):
    first = column(client, conversation, fake, models[0], "A", content="Frage")
    anchor = first[0][1]["assistant_message_id"]
    # Spalte 2: Modell nicht (mehr) erlaubt -> JSON-Fehler nur für diese Spalte.
    models[1].active = False
    models[1].save()
    fake(OK_EVENTS)
    response = post(
        client, conversation, model=models[1].pk, compare=True, regenerate=True, message_id=anchor
    )
    assert response.status_code == 403
    assert "error" in response.json()
    third = column(client, conversation, fake, models[2], "C", regenerate=True, message_id=anchor)
    assert third[-1] == ("done", {"status": "complete"})
    assert Message.objects.filter(role="assistant").count() == 2


def test_permissions(client, adult, models, fake):
    owner = make_user("adult", "besitzer")
    conv = Conversation.objects.create(user=owner)
    services.append_message(conv, role="user", content="Frage")
    answer = services.append_message(conv, role="assistant", content="A", model=models[0])
    fake(OK_EVENTS)
    data = {"model": models[1].pk, "compare": True, "regenerate": True, "message_id": answer.pk}
    assert post(client, conv, **data).status_code == 404
    group = UserGroup.objects.create(name="Lesegruppe")
    adult.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=False)
    assert post(client, conv, **data).status_code == 403
    assert post(client, conv, content="x", model=models[0].pk, compare=True).status_code == 403
    assert Message.objects.filter(conversation=conv).count() == 2
    Share.objects.filter(conversation=conv).update(can_write=True)
    events = parse_sse(post(client, conv, **data))
    assert events[-1][0] == "done"
    conv.refresh_from_db()
    assert conv.current_leaf_id == answer.pk


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_parallel_columns_are_serialized(models):
    """Weitere Spalten starten gleichzeitig (eigene Threads/Verbindungen):
    Die Sperre in prepare_turn hält den Baum konsistent."""
    user = make_user("adult", "parallel")
    conv = Conversation.objects.create(user=user)
    services.append_message(conv, role="user", content="Frage")
    anchor = services.append_message(conv, role="assistant", content="A", model=models[0])
    barrier = threading.Barrier(4)
    errors = []

    def worker(ai_model):
        try:
            barrier.wait(timeout=10)
            services.prepare_turn(
                user,
                Conversation.objects.get(pk=conv.pk),
                ai_model,
                regenerate=True,
                regenerate_of=anchor.pk,
                compare=True,
            )
        except Exception as exc:  # pragma: no cover - nur zur Diagnose
            errors.append(exc)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(m,)) for m in models + [models[1]]]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    answers = Message.objects.filter(conversation=conv, role="assistant")
    assert answers.count() == 5
    assert {a.parent_id for a in answers} == {anchor.parent_id}
    conv.refresh_from_db()
    assert conv.current_leaf_id == anchor.pk


def test_page_has_compare_ui_without_inline_js(client, conversation, ai_model):
    html = client.get(reverse("chat:conversation", args=[conversation.pk])).content.decode()
    assert 'id="compare-toggle"' in html
    assert 'id="compare-panel"' in html
    assert "chat/compare.js" in html
    assert "<script>" not in html
    assert not re.search(r"\son[a-z]+=", html)
    assert "javascript:" not in html
    assert "{#" not in html


def test_read_only_page_has_no_compare_switch(client, adult, ai_model):
    owner = make_user("adult", "besitzer2")
    conv = Conversation.objects.create(user=owner)
    group = UserGroup.objects.create(name="Lesen")
    adult.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=False)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="compare-toggle"' not in html
