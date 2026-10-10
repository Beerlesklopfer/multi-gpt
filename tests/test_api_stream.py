"""Stream- und JSON-Endpunkte (M3-03, M3-04, M3-06) mit Fake-Adapter.

Kein echter Anbieteraufruf: ``registry.get_adapter`` wird gepatcht.
"""

import json
from decimal import Decimal

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import services
from multigpt.chat.models import (
    DEFAULT_BASE_INSTRUCTIONS,
    AIModel,
    Conversation,
    Message,
    Provider,
    Share,
)
from multigpt.chat.providers import registry
from multigpt.chat.providers.base import Delta, Done, Error, ProviderAdapter, Usage
from tests.billing_helpers import set_price

pytestmark = pytest.mark.django_db

PASSWORD = "Geheim-Test-1234"


# --- Fake-Adapter -----------------------------------------------------------------


class FakeAdapter(ProviderAdapter):
    """Liefert vorgegebene Events und merkt sich Aufrufe und Schließen."""

    def __init__(self, provider, events, raise_exc=None):
        super().__init__(provider)
        self.events = events
        self.raise_exc = raise_exc
        self.calls = []
        self.closed = False

    def stream(self, model_id, messages, system=None, tools=None, **params):
        self.calls.append({"model_id": model_id, "messages": list(messages), "system": system})
        return self._gen()

    def _gen(self):
        try:
            yield from self.events
            if self.raise_exc:
                raise self.raise_exc
        finally:
            self.closed = True


@pytest.fixture
def fake(monkeypatch):
    """fake(events) installiert einen Fake-Adapter und gibt ihn zurück."""

    def install(events, raise_exc=None):
        holder = {}

        def get_adapter(provider):
            holder["adapter"] = FakeAdapter(provider, events, raise_exc)
            return holder["adapter"]

        monkeypatch.setattr(registry, "get_adapter", get_adapter)
        return holder

    return install


# --- Daten ------------------------------------------------------------------------


def make_user(role_key, username):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


@pytest.fixture
def provider():
    return Provider.objects.create(name="Cloud", kind=Provider.Kind.OPENAI_COMPAT)


@pytest.fixture
def local_provider():
    return Provider.objects.create(
        name="LM Studio", kind=Provider.Kind.OPENAI_COMPAT, is_local=True
    )


@pytest.fixture
def ai_model(provider):
    model = AIModel.objects.create(provider=provider, model_id="gpt-test", display_name="GPT Test")
    set_price(model, "2.5", "10")  # EUR je 1 Mio. Tokens (billing)
    return model


@pytest.fixture
def local_model(local_provider):
    model = AIModel.objects.create(provider=local_provider, model_id="llama", display_name="Llama")
    set_price(model, "1", "1")  # zählt nicht: lokales Token-Konto
    return model


@pytest.fixture
def adult(client):
    user = make_user("adult", "erwachsen")
    client.force_login(user)
    return user


@pytest.fixture
def conversation(adult):
    return Conversation.objects.create(user=adult, system_prompt="Antworte knapp.")


def url(conversation):
    return reverse("chat:api_messages", args=[conversation.pk])


def post(client, conversation, **data):
    return client.post(url(conversation), json.dumps(data), content_type="application/json")


def parse_sse(response):
    raw = b"".join(response.streaming_content).decode()
    return parse_chunks(raw)


def parse_chunks(raw):
    events = []
    for block in raw.split("\n\n"):
        if not block.strip():
            continue
        lines = dict(line.split(": ", 1) for line in block.split("\n"))
        events.append((lines["event"], json.loads(lines["data"])))
    return events


OK_EVENTS = [Delta("Hal"), Delta("lo"), Usage(12, 3), Done("stop")]


# --- Ablauf -----------------------------------------------------------------------


def test_event_order_and_persistence(client, conversation, ai_model, fake):
    holder = fake(OK_EVENTS)
    response = post(client, conversation, content="Hi?", model=ai_model.pk)
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/event-stream")
    assert response["Cache-Control"] == "no-cache"
    assert response["X-Accel-Buffering"] == "no"

    events = parse_sse(response)
    assert [name for name, _ in events] == ["start", "delta", "delta", "usage", "done"]
    start = events[0][1]
    assert "".join(d["text"] for n, d in events if n == "delta") == "Hallo"
    assert events[3][1] == {"tokens_in": 12, "tokens_out": 3, "cost": "0.000060"}
    assert events[4][1] == {"status": "complete"}

    user_msg = Message.objects.get(pk=start["user_message_id"])
    answer = Message.objects.get(pk=start["assistant_message_id"])
    assert (user_msg.role, user_msg.content) == ("user", "Hi?")
    assert answer.content == "Hallo"
    assert answer.status == Message.Status.COMPLETE
    assert answer.model == ai_model
    assert (answer.tokens_in, answer.tokens_out) == (12, 3)
    # 12 * 2.5 / 1e6 + 3 * 10 / 1e6
    assert answer.cost == Decimal("0.000060")
    assert holder["adapter"].calls[0]["model_id"] == "gpt-test"
    assert holder["adapter"].closed

    conversation.refresh_from_db()
    assert conversation.title == "Hi?"
    assert conversation.default_model == ai_model

    # Nach erneutem Laden
    data = client.get(url(conversation)).json()
    assert [(m["role"], m["content"], m["status"]) for m in data] == [
        ("user", "Hi?", "complete"),
        ("assistant", "Hallo", "complete"),
    ]
    assert data[1]["model"] == "GPT Test"
    assert data[1]["model_id"] == ai_model.pk


def test_error_keeps_partial_text(client, conversation, ai_model, fake):
    fake([Delta("Teil"), Error("Der Anbieter ist nicht erreichbar.")])
    events = parse_sse(post(client, conversation, content="Frage", model=ai_model.pk))
    assert [n for n, _ in events] == ["start", "delta", "error", "usage", "done"]
    assert events[2][1] == {"message": "Der Anbieter ist nicht erreichbar."}
    assert events[-1][1] == {"status": "error"}
    answer = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert answer.status == Message.Status.ERROR
    assert answer.content == "Teil"
    assert answer.error == "Der Anbieter ist nicht erreichbar."


def test_unexpected_exception_becomes_generic_error(client, conversation, ai_model, fake):
    fake([Delta("x")], raise_exc=RuntimeError("geheime Interna sk-123"))
    events = parse_sse(post(client, conversation, content="Frage", model=ai_model.pk))
    error = dict(events)["error"]["message"]
    assert error == services.GENERIC_ERROR
    assert "sk-123" not in error
    assert events[-1][1] == {"status": "error"}


def test_unknown_provider_kind_is_error_event(client, conversation, provider, ai_model):
    Provider.objects.filter(pk=provider.pk).update(kind=Provider.Kind.GOOGLE)
    events = parse_sse(post(client, conversation, content="Frage", model=ai_model.pk))
    assert [n for n, _ in events] == ["start", "error", "usage", "done"]
    assert events[-1][1] == {"status": "error"}


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_abort_saves_partial_text_and_closes_adapter(client, conversation, ai_model, fake):
    holder = fake([Delta("Erster "), Delta("Zweiter "), Delta("Dritter"), Done()])
    response = post(client, conversation, content="Frage", model=ai_model.pk)
    chunks = iter(response.streaming_content)
    start = parse_chunks(next(chunks).decode())[0][1]
    first = parse_chunks(next(chunks).decode())
    assert first == [("delta", {"text": "Erster "})]
    # Client bricht ab: Der WSGI-Server ruft response.close() auf, das schließt
    # den Generator. Dabei feuert request_finished und schließt die
    # DB-Verbindung – deshalb läuft dieser Test ohne umschließende Transaktion.
    response.close()

    answer = Message.objects.get(pk=start["assistant_message_id"])
    assert answer.status == Message.Status.ABORTED
    assert answer.content == "Erster "
    assert holder["adapter"].closed


def test_placeholder_status_while_streaming(client, conversation, ai_model, fake):
    fake(OK_EVENTS)
    response = post(client, conversation, content="Frage", model=ai_model.pk)
    start = parse_chunks(next(iter(response.streaming_content)).decode())[0][1]
    # Läuft noch: Platzhalter "aborted", falls der Prozess stirbt.
    assert Message.objects.get(pk=start["assistant_message_id"]).status == "aborted"
    b"".join(response.streaming_content)


def test_fixed_role_prompt_comes_first(client, ai_model, fake):
    teen = make_user("teen", "jugend")
    teen.role.allowed_models.add(ai_model)
    client.force_login(teen)
    conv = Conversation.objects.create(user=teen, system_prompt="Chat-Prompt")
    holder = fake(OK_EVENTS)
    parse_sse(post(client, conv, content="Hallo", model=ai_model.pk))
    system = holder["adapter"].calls[0]["system"]
    fixed = teen.role.fixed_system_prompt.strip()
    assert fixed
    # Grundregeln (ChatSettings) stehen davor.
    assert system == f"{DEFAULT_BASE_INSTRUCTIONS}\n\n{fixed}\n\nChat-Prompt"
    # Für das Mitglied unsichtbar: nicht im Verlauf
    assert all(fixed not in m["content"] for m in client.get(url(conv)).json())


def test_history_sent_to_adapter(client, conversation, ai_model, fake):
    services.append_message(conversation, role="user", content="A")
    services.append_message(conversation, role="assistant", content="B")
    services.append_message(conversation, role="user", content="C")
    services.append_message(conversation, role="assistant", content="D-teil", status="aborted")
    services.append_message(conversation, role="user", content="E")
    services.append_message(conversation, role="assistant", content="kaputt", status="error")
    holder = fake(OK_EVENTS)
    parse_sse(post(client, conversation, content="F", model=ai_model.pk))
    msgs = holder["adapter"].calls[0]["messages"]
    assert [(m.role, m.content) for m in msgs] == [
        ("user", "A"),
        ("assistant", "B"),
        ("user", "C"),
        ("assistant", "D-teil"),
        ("user", "E"),
        ("user", "F"),
    ]


# Neu erzeugen und Bearbeiten (Versionen): tests/test_branches_api.py


def test_regenerate_without_question(client, conversation, ai_model, fake):
    fake(OK_EVENTS)
    response = post(client, conversation, regenerate=True, model=ai_model.pk)
    assert response.status_code == 400
    assert "error" in response.json()


def test_local_model_costs_nothing(client, conversation, local_model, fake):
    fake([Delta("x"), Usage(1000, 1000), Done()])
    events = parse_sse(post(client, conversation, content="Frage", model=local_model.pk))
    answer = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert (answer.tokens_in, answer.tokens_out) == (1000, 1000)
    assert answer.cost == Decimal(0)


def test_empty_content_rejected(client, conversation, ai_model, fake):
    fake(OK_EVENTS)
    response = post(client, conversation, content="   ", model=ai_model.pk)
    assert response.status_code == 400
    assert not conversation.messages.exists()


# --- Rechte -----------------------------------------------------------------------


def test_anonymous_gets_json_403(client, ai_model):
    other = make_user("adult", "fremd")
    conv = Conversation.objects.create(user=other)
    assert client.get(url(conv)).status_code == 403
    assert post(client, conv, content="x", model=ai_model.pk).status_code == 403
    assert client.get(reverse("chat:api_models")).status_code == 403


def test_foreign_conversation_not_found(client, adult, ai_model, fake):
    holder = fake(OK_EVENTS)
    other = make_user("adult", "fremd")
    conv = Conversation.objects.create(user=other)
    services.append_message(conv, role="user", content="privat")
    response = client.get(url(conv))
    assert response.status_code == 404
    assert response.json() == {"error": "Chat nicht gefunden."}
    assert post(client, conv, content="x", model=ai_model.pk).status_code == 404
    assert conv.messages.count() == 1
    assert "adapter" not in holder


def test_shared_read_only_cannot_write(client, adult, ai_model, fake):
    fake(OK_EVENTS)
    other = make_user("adult", "fremd")
    conv = Conversation.objects.create(user=other)
    group = UserGroup.objects.create(name="Lesegruppe")
    adult.groups.add(group)
    Share.objects.create(conversation=conv, group=group, can_write=False)
    assert client.get(url(conv)).status_code == 200
    assert post(client, conv, content="x", model=ai_model.pk).status_code == 403
    Share.objects.filter(conversation=conv).update(can_write=True)
    assert post(client, conv, content="x", model=ai_model.pk).status_code == 200


def test_guest_without_model_forbidden(client, ai_model, fake):
    holder = fake(OK_EVENTS)
    guest = make_user("guest", "gast")
    client.force_login(guest)
    conv = Conversation.objects.create(user=guest)
    response = post(client, conv, content="x", model=ai_model.pk)
    assert response.status_code == 403
    assert "error" in response.json()
    assert not conv.messages.exists()
    assert "adapter" not in holder
    assert client.get(reverse("chat:api_models")).json() == []


def test_inactive_model_forbidden(client, conversation, ai_model, provider, fake):
    holder = fake(OK_EVENTS)
    ai_model.active = False
    ai_model.save()
    assert post(client, conversation, content="x", model=ai_model.pk).status_code == 403
    ai_model.active = True
    ai_model.save()
    provider.active = False
    provider.save()
    assert post(client, conversation, content="x", model=ai_model.pk).status_code == 403
    assert "adapter" not in holder


def test_unknown_or_non_chat_model(client, conversation, provider, fake):
    fake(OK_EVENTS)
    image = AIModel.objects.create(
        provider=provider, model_id="img", display_name="Bild", capability="image"
    )
    assert post(client, conversation, content="x", model=image.pk).status_code == 400
    assert post(client, conversation, content="x", model=999999).status_code == 400
    assert post(client, conversation, content="x").status_code == 400


# --- JSON-Endpunkte ------------------------------------------------------------------


def test_models_list(client, adult, ai_model, local_model, provider):
    AIModel.objects.create(provider=provider, model_id="off", display_name="Aus", active=False)
    AIModel.objects.create(
        provider=provider, model_id="emb", display_name="Emb", capability="embedding"
    )
    data = client.get(reverse("chat:api_models")).json()
    assert sorted(m["display_name"] for m in data) == ["GPT Test", "Llama"]
    llama = next(m for m in data if m["display_name"] == "Llama")
    assert llama == {
        "id": local_model.pk,
        "display_name": "Llama",
        "provider": "LM Studio",
        "provider_id": local_model.provider_id,
        "is_local": True,
        "supports_tools": False,
        "supports_vision": False,
        "mcp_access": "none",
        "mcp_server_ids": [],
        "online": True,
        "available": True,
        "blocked_by_budget": False,
        "budget_reason": "",
        "billing_title": "Konto Lokale Modelle · nur Tokens gezählt",
    }


def test_models_list_respects_role(client, ai_model, local_model):
    teen = make_user("teen", "jugend")
    teen.role.allowed_models.add(local_model)
    client.force_login(teen)
    data = client.get(reverse("chat:api_models")).json()
    assert [m["id"] for m in data] == [local_model.pk]


def test_create_conversation(client, adult, ai_model):
    response = client.post(
        reverse("chat:api_conversations"),
        json.dumps({"default_model": ai_model.pk}),
        content_type="application/json",
    )
    assert response.status_code == 201
    data = response.json()
    conv = Conversation.objects.get(pk=data["id"])
    assert conv.user == adult
    assert conv.default_model == ai_model
    assert data["url"] == reverse("chat:conversation", args=[conv.pk])
    assert data["title"] == ""


def test_create_conversation_disallowed_model(client, ai_model):
    guest = make_user("guest", "gast")
    client.force_login(guest)
    response = client.post(
        reverse("chat:api_conversations"),
        json.dumps({"default_model": ai_model.pk}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert not Conversation.objects.exists()


def test_create_conversation_requires_login(client):
    assert client.post(reverse("chat:api_conversations")).status_code == 403


# --- Speichern ohne Test-Transaktion (Verbindungsprüfung aktiv) ----------------------


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_saving_outside_transaction(client, monkeypatch, fake):
    monkeypatch.setattr(services, "SAVE_INTERVAL", 0)
    user = make_user("adult", "tx")
    client.force_login(user)
    prov = Provider.objects.create(name="P", kind=Provider.Kind.OPENAI_COMPAT)
    model = AIModel.objects.create(provider=prov, model_id="m", display_name="M")
    conv = Conversation.objects.create(user=user)
    fake([Delta("a"), Delta("b"), Usage(1, 2), Done()])
    events = parse_sse(post(client, conv, content="x", model=model.pk))
    answer = Message.objects.get(pk=events[0][1]["assistant_message_id"])
    assert (answer.content, answer.status) == ("ab", "complete")
    assert answer.cost is None  # keine Preise hinterlegt
