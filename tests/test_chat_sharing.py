"""Chats teilen (RWUD, chat/sharing.py, api_sharing.py).

Matrix je Recht und Aktion über Seiten und API, Widerruf (auch über eine
Gruppe und im laufenden Stream), Budget auf den Absender, Datenschutz
(Prompt des Empfängers ohne Persönliches des Besitzers, mit respx geprüft),
Quellen aus fremden Sammlungen, Verwalter, Kopie, Verfasser und
Gleichzeitigkeit von current_leaf. Kein echter Anbieteraufruf.
"""

# Fixtures werden aus test_api_stream importiert und als Parameter wieder benutzt.
# ruff: noqa: F811

import io
import json
import threading
from decimal import Decimal

import httpx
import pytest
import respx
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.urls import reverse
from PIL import Image

from multigpt.accounts import usage
from multigpt.accounts.models import Role, User, UserGroup
from multigpt.chat import services, sharing
from multigpt.chat.models import (
    EMBEDDING_DIMENSIONS,
    AIModel,
    Attachment,
    Chunk,
    Collection,
    Conversation,
    ConversationView,
    Document,
    Message,
    Provider,
    Share,
    SourceRef,
    ToolCall,
)
from multigpt.chat.providers.base import ChatMessage, Delta, Done, Usage
from tests.test_api_stream import (  # noqa: F401
    OK_EVENTS,
    PASSWORD,
    ai_model,
    fake,
    make_user,
    parse_sse,
    provider,
)

pytestmark = pytest.mark.django_db


# --- Hilfen -------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"
    return settings.MEDIA_ROOT


@pytest.fixture
def anna():
    user = make_user("adult", "anna_owner")
    user.display_name = "Anna"
    user.save(update_fields=["display_name"])
    return user


@pytest.fixture
def ben():
    user = make_user("adult", "ben_empf")
    user.display_name = "Ben"
    user.save(update_fields=["display_name"])
    return user


@pytest.fixture
def chat(anna, ai_model):
    """Chat von Anna mit Frage und Antwort (Hauptpfad)."""
    conv = Conversation.objects.create(
        user=anna, title="Urlaub", default_model=ai_model, system_prompt="Chat-Regel: knapp."
    )
    q = services.append_message(conv, role="user", content="Wohin?", author=anna)
    a = services.append_message(
        conv, role="assistant", content="Nach Rom.", model=ai_model, author=anna
    )
    return {"conv": conv, "q": q, "a": a}


def share_to(conv, user, *, write=False, update=False, delete=False):
    return Share.objects.create(
        conversation=conv, user=user, can_write=write, can_update=update, can_delete=delete
    )


def jpost(client, url, data=None):
    return client.post(url, json.dumps(data or {}), content_type="application/json")


def jpatch(client, url, data):
    return client.patch(url, json.dumps(data), content_type="application/json")


def api(name, conv, *extra):
    return reverse(f"chat:{name}", args=[conv.pk, *extra])


def png_bytes():
    out = io.BytesIO()
    Image.new("RGB", (20, 10), (10, 120, 200)).save(out, "PNG")
    return out.getvalue()


def stream(client, conv, **data):
    return client.post(api("api_messages", conv), json.dumps(data), content_type="application/json")


# --- Matrix je Recht und Aktion -------------------------------------------------------

RIGHTS = {
    "R": {},
    "RW": {"write": True},
    "RU": {"update": True},
    "RWU": {"write": True, "update": True},
    "RD": {"delete": True},
    "RWUD": {"write": True, "update": True, "delete": True},
}


@pytest.mark.parametrize("label", list(RIGHTS))
def test_rights_matrix(client, chat, ben, ai_model, fake, label):
    flags = RIGHTS[label]
    conv, q, a = chat["conv"], chat["q"], chat["a"]
    s = share_to(conv, ben, **flags)
    assert s.rights_label == label
    client.force_login(ben)
    w, u, d = flags.get("write", False), flags.get("update", False), flags.get("delete", False)

    def code(ok, ok_status=200):
        return ok_status if ok else 403

    # R: Seite, Fragment, Verlauf, Export, Anhangsliste – immer erlaubt.
    page = client.get(reverse("chat:conversation", args=[conv.pk]))
    assert page.status_code == 200
    html = page.content.decode()
    assert "Geteilt von <strong>Anna</strong>" in html
    assert ('id="chat-form"' in html) is w
    assert ('data-chat-action="rename"' in html) is u
    assert ('data-chat-action="delete"' in html) is d
    assert 'id="share-open"' not in html  # Teilen nur der Besitzer
    assert "data-share-copy" in html and "data-share-leave" in html
    assert client.get(reverse("chat:conversation_messages", args=[conv.pk])).status_code == 200
    assert client.get(api("api_messages", conv)).status_code == 200
    export = client.get(reverse("chat:conversation_export", args=[conv.pk]))
    assert export.status_code == 200 and "Nach Rom." in export.content.decode()
    # Versionen umschalten: eigene Ansicht, Lesen genügt.
    assert jpost(client, api("api_branch", conv), {"message_id": q.pk}).status_code == 200
    # Teilen verwalten: nur der Besitzer.
    assert client.get(api("api_conversation_shares", conv)).status_code == 403
    assert jpost(client, api("api_conversation_shares", conv), {}).status_code == 403
    assert client.delete(api("api_conversation_share_detail", conv, s.pk)).status_code == 403

    # W: senden (Stream), neu erzeugen, Anhang hochladen.
    fake(OK_EVENTS)
    response = stream(client, conv, content="Und dann?", model=ai_model.pk)
    assert response.status_code == code(w)
    if w:
        assert parse_sse(response)[-1][0] == "done"
    fake(OK_EVENTS)
    response = stream(client, conv, regenerate=True, model=ai_model.pk, message_id=a.pk)
    assert response.status_code == code(w)
    if w:
        parse_sse(response)
    upload = client.post(
        reverse("chat:api_attachments"),
        {
            "file": SimpleUploadedFile("bild.png", png_bytes(), "image/png"),
            "conversation": str(conv.pk),
        },
    )
    assert upload.status_code == code(w, 201), upload.content

    # U: bearbeiten (braucht auch W, weil eine neue Antwort entsteht), Titel, System-Prompt.
    fake(OK_EVENTS)
    response = stream(client, conv, content="Wohin genau?", model=ai_model.pk, edit_of=q.pk)
    assert response.status_code == code(w and u)
    if w and u:
        parse_sse(response)
    detail = api("api_conversation_detail", conv)
    assert jpatch(client, detail, {"title": "Neu"}).status_code == code(u)
    assert jpatch(client, detail, {"system_prompt": "x"}).status_code == code(u)

    # D: archivieren und löschen – für alle.
    assert jpatch(client, detail, {"archived": True}).status_code == code(d)
    conv.refresh_from_db()
    assert conv.archived is d

    # Kopie: R genügt.
    copy = jpost(client, api("api_conversation_copy", conv))
    assert copy.status_code == 201

    assert client.delete(detail).status_code == code(d)
    assert Conversation.objects.filter(pk=conv.pk).exists() is not d


def test_foreign_chat_is_404_everywhere(client, chat, ben, ai_model):
    """Ohne Freigabe: alles 404, auch die neuen Endpunkte (keine Existenz-Lecks)."""
    conv = chat["conv"]
    client.force_login(ben)
    for url in (
        reverse("chat:conversation", args=[conv.pk]),
        reverse("chat:conversation_messages", args=[conv.pk]),
        reverse("chat:conversation_export", args=[conv.pk]),
        api("api_messages", conv),
        api("api_conversation_shares", conv),
        api("api_conversation_state", conv),
    ):
        assert client.get(url).status_code == 404, url
    for name in ("api_conversation_leave", "api_conversation_copy", "api_branch"):
        assert jpost(client, api(name, conv), {"message_id": chat["q"].pk}).status_code == 404
    assert stream(client, conv, content="x", model=ai_model.pk).status_code == 404
    other = Conversation.objects.create(user=ben)
    assert client.delete(api("api_conversation_share_detail", other, 999)).status_code == 404


# --- Freigaben verwalten (Besitzer) --------------------------------------------------


def test_owner_manages_shares(client, chat, anna, ben):
    conv = chat["conv"]
    group = UserGroup.objects.create(name="Kinder")
    client.force_login(anna)
    page = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert 'id="share-open"' in page
    data = client.get(api("api_conversation_shares", conv)).json()
    assert data["shares"] == []
    assert {"id": ben.pk, "name": "Ben"} in data["users"]
    assert all(u["id"] != anna.pk for u in data["users"])
    assert {"id": group.pk, "name": "Kinder"} in data["groups"]

    url = api("api_conversation_shares", conv)
    created = jpost(client, url, {"kind": "user", "target": ben.pk, "can_write": True})
    assert created.status_code == 201
    row = created.json()["shares"][0]
    assert (row["name"], row["label"], row["kind"]) == ("Ben", "RW", "user")
    assert row["text"] == "Lesen, Schreiben"
    # Gleicher Empfänger: Rechte ändern statt doppelt anlegen.
    again = jpost(client, url, {"kind": "user", "target": ben.pk, "can_delete": True})
    assert again.status_code == 200 and again.json()["shares"][0]["label"] == "RD"
    assert jpost(client, url, {"kind": "group", "target": group.pk}).status_code == 201
    share = Share.objects.get(conversation=conv, user=ben)
    patched = jpatch(
        client, api("api_conversation_share_detail", conv, share.pk), {"can_update": True}
    )
    assert {r["label"] for r in patched.json()["shares"]} == {"RUD", "R"}
    # Ungültiges
    assert jpost(client, url, {"kind": "user", "target": anna.pk}).status_code == 400
    assert jpost(client, url, {"kind": "x", "target": 1}).status_code == 400
    assert (
        jpost(client, url, {"kind": "user", "target": ben.pk, "can_write": "ja"}).status_code == 400
    )
    # Widerruf
    deleted = client.delete(api("api_conversation_share_detail", conv, share.pk))
    assert deleted.status_code == 200 and len(deleted.json()["shares"]) == 1
    # Seitenleiste: Symbol am eigenen geteilten Chat
    html = client.get(reverse("chat:index")).content.decode()
    assert "chat-shared-icon" in html


def test_share_requires_role_right(client, chat, anna, ben):
    role = Role.objects.get(key="adult")
    role.can_share = False
    role.save()
    client.force_login(anna)
    assert client.get(api("api_conversation_shares", chat["conv"])).status_code == 403
    page = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert 'id="share-open"' not in page


def test_share_api_requires_csrf(chat, anna, ben):
    from django.test import Client

    csrf_client = Client(enforce_csrf_checks=True)
    csrf_client.force_login(anna)
    url = api("api_conversation_shares", chat["conv"])
    response = jpost(csrf_client, url, {"kind": "user", "target": ben.pk})
    assert response.status_code == 403
    assert not Share.objects.exists()


def test_collection_shares_stay_group_only(anna, ben):
    from django.db import IntegrityError, transaction

    coll = Collection.objects.create(owner=anna, name="Docs")
    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(collection=coll, user=ben)
    conv = Conversation.objects.create(user=anna)
    with pytest.raises(IntegrityError), transaction.atomic():
        Share.objects.create(conversation=conv)


# --- Seitenleiste und Austragen -------------------------------------------------------


def test_shared_with_me_and_leave(client, chat, ben):
    conv = chat["conv"]
    group = UserGroup.objects.create(name="Geschwister")
    ben.groups.add(group)
    Share.objects.create(conversation=conv, group=group)
    client.force_login(ben)
    html = client.get(reverse("chat:index")).content.decode()
    assert "Mit mir geteilt" in html and "Urlaub" in html and "von Ben" not in html
    assert "von Anna" in html
    # Austragen aus einer Gruppenfreigabe: gilt nur für Ben.
    response = jpost(client, api("api_conversation_leave", conv))
    assert response.status_code == 200
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 404
    assert "Mit mir geteilt" not in client.get(reverse("chat:index")).content.decode()
    other = make_user("adult", "carla")
    other.groups.add(group)
    client.force_login(other)
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 200
    # Erneutes Freigeben (Besitzer) hebt das Austragen auf.
    client.force_login(chat["conv"].user)
    jpost(client, api("api_conversation_shares", conv), {"kind": "group", "target": group.pk})
    client.force_login(ben)
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 200


def test_leave_direct_share_deletes_it(client, chat, ben):
    share_to(chat["conv"], ben)
    client.force_login(ben)
    assert jpost(client, api("api_conversation_leave", chat["conv"])).status_code == 200
    assert not Share.objects.exists()


# --- Widerruf -----------------------------------------------------------------------


def test_revoke_is_immediate_also_via_group(client, chat, ben, ai_model, fake):
    conv = chat["conv"]
    group = UserGroup.objects.create(name="Schreiber")
    ben.groups.add(group)
    share = Share.objects.create(conversation=conv, group=group, can_write=True)
    client.force_login(ben)
    fake(OK_EVENTS)
    assert parse_sse(stream(client, conv, content="Hi", model=ai_model.pk))[-1][0] == "done"
    # Aus der Gruppe entfernt -> sofort 404
    ben.groups.remove(group)
    assert client.get(api("api_messages", conv)).status_code == 404
    assert stream(client, conv, content="Hi", model=ai_model.pk).status_code == 404
    assert client.get(api("api_conversation_state", conv)).status_code == 404
    # Wieder drin, Freigabe gelöscht -> 404
    ben.groups.add(group)
    assert client.get(api("api_messages", conv)).status_code == 200
    share.delete()
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 404


def test_revoke_stops_open_stream(client, chat, ben, ai_model, fake, monkeypatch):
    conv = chat["conv"]
    share = share_to(conv, ben, write=True)
    monkeypatch.setattr(services, "SAVE_INTERVAL", 0)

    def events():
        yield Delta("Teil eins. ")
        Share.objects.filter(pk=share.pk).delete()  # Besitzer widerruft
        yield Delta("Teil zwei.")
        yield Delta("Teil drei.")
        yield Usage(5, 3)
        yield Done("stop")

    holder = fake(events())
    client.force_login(ben)
    result = parse_sse(stream(client, conv, content="Los", model=ai_model.pk))
    names = [n for n, _ in result]
    assert ("error", {"message": sharing.MSG_REVOKED}) in result
    assert result[-1] == ("done", {"status": "aborted"})
    assert "Teil drei." not in "".join(d["text"] for n, d in result if n == "delta")
    assert names.count("delta") == 2
    answer = Message.objects.filter(conversation=conv, role="assistant").latest("pk")
    assert answer.status == Message.Status.ABORTED and answer.error == sharing.MSG_REVOKED
    assert holder["adapter"].closed


def test_account_without_chat_right_gets_no_write(client, chat, ben, ai_model):
    share_to(chat["conv"], ben, write=True, update=True, delete=True)
    User.objects.filter(pk=ben.pk).update(role=None)
    client.force_login(User.objects.get(pk=ben.pk))
    assert client.get(api("api_messages", chat["conv"])).status_code == 200
    assert stream(client, chat["conv"], content="x", model=ai_model.pk).status_code == 403
    detail = api("api_conversation_detail", chat["conv"])
    assert jpatch(client, detail, {"title": "x"}).status_code == 403


# --- Budget und Modelle des Absenders ---------------------------------------------------


def test_cost_is_booked_on_sender(client, chat, anna, ben, ai_model, fake):
    share_to(chat["conv"], ben, write=True)
    client.force_login(ben)
    fake([Delta("Ok"), Usage(1_000_000, 0), Done("stop")])
    parse_sse(stream(client, chat["conv"], content="Teuer", model=ai_model.pk))
    answer = Message.objects.filter(conversation=chat["conv"], role="assistant").latest("pk")
    assert answer.author_id == ben.pk and answer.cost == Decimal("2.5")
    assert usage.spent(ben) == Decimal("2.5")
    assert usage.spent(anna) == Decimal("0")
    totals = usage.spent_by_user([anna.pk, ben.pk], *usage.month_bounds())
    assert totals[ben.pk] == Decimal("2.5") and totals.get(anna.pk, 0) == 0
    rows = usage.usage_by_model(None, *usage.month_bounds(), by_user=True)
    assert {r["user_id"] for r in rows if r["cost"]} == {ben.pk}


def test_sender_budget_and_models_apply(client, chat, anna, ben, ai_model, provider, fake):
    share_to(chat["conv"], ben, write=True)
    ben.monthly_budget_override = Decimal("0")
    ben.save(update_fields=["monthly_budget_override"])
    client.force_login(ben)
    response = stream(client, chat["conv"], content="x", model=ai_model.pk)
    assert response.status_code == 403
    assert response.json()["error"] == usage.BUDGET_EXHAUSTED_MESSAGE
    # Modell, das nur die Rolle der Besitzerin erlaubt: für Ben gesperrt.
    special = AIModel.objects.create(provider=provider, model_id="vip", display_name="VIP")
    teen_role = Role.objects.get(key="teen")
    teen_role.all_models = False
    teen_role.save()
    teen_role.allowed_models.set([])
    User.objects.filter(pk=ben.pk).update(role=teen_role, monthly_budget_override=None)
    client.force_login(User.objects.get(pk=ben.pk))
    response = stream(client, chat["conv"], content="x", model=special.pk)
    assert response.status_code == 403


# --- Datenschutz: Kontext des Absenders --------------------------------------------------

BASE = "http://anbieter.test/v1"


def _sse_body(text):
    chunks = [
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 2}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    return body.encode()


def test_recipient_prompt_has_no_personal_data_of_owner(client, anna, ben):
    provider = Provider.objects.create(
        name="Respx", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE, api_key="sk-test"
    )
    model = AIModel.objects.create(provider=provider, model_id="m-test", display_name="M")
    owner_role = Role.objects.create(
        name="Anna-Rolle", key="anna-rolle", all_models=True, fixed_system_prompt="BESITZER-GEHEIM"
    )
    ben_role = Role.objects.create(
        name="Ben-Rolle", key="ben-rolle", all_models=True, fixed_system_prompt="BEN-REGEL"
    )
    User.objects.filter(pk=anna.pk).update(role=owner_role, citation_style="apa")
    User.objects.filter(pk=ben.pk).update(role=ben_role)
    conv = Conversation.objects.create(user=anna, system_prompt="CHAT-REGEL")
    services.append_message(conv, role="user", content="Frage von Anna", author=anna)
    # Antwort mit Werkzeugrunde (z. B. privates MCP-Ergebnis der Besitzerin)
    services.append_message(
        conv,
        role="assistant",
        content="Antwort an Anna",
        model=model,
        author=anna,
        tool_state={
            "rounds": [
                {
                    "text": "",
                    "calls": [{"id": "c1", "name": "mail", "arguments": {}}],
                    "results": [
                        {"tool_call_id": "c1", "name": "mail", "content": "ANNAS-POSTFACH"}
                    ],
                }
            ],
            "text_offset": 0,
        },
    )
    share_to(conv, ben, write=True)
    client.force_login(User.objects.get(pk=ben.pk))
    with respx.mock(assert_all_called=True) as router:
        route = router.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200, content=_sse_body("Hallo Ben"), headers={"content-type": "text/event-stream"}
            )
        )
        events = parse_sse(stream(client, conv, content="Frage von Ben", model=model.pk))
    assert events[-1] == ("done", {"status": "complete"})
    body = route.calls.last.request.content.decode()
    assert "BESITZER-GEHEIM" not in body
    assert "ANNAS-POSTFACH" not in body
    assert "BEN-REGEL" in body and "CHAT-REGEL" in body
    assert "Frage von Anna" in body and "Antwort an Anna" in body  # Chat-Inhalt bleibt


def test_tool_rounds_only_for_own_answers(anna, ben, ai_model):
    conv = Conversation.objects.create(user=anna)
    services.append_message(conv, role="user", content="F", author=anna)
    services.append_message(
        conv,
        role="assistant",
        content="A",
        model=ai_model,
        author=anna,
        tool_state={
            "rounds": [
                {
                    "text": "",
                    "calls": [{"id": "c1", "name": "t", "arguments": {}}],
                    "results": [{"tool_call_id": "c1", "name": "t", "content": "ROH"}],
                }
            ]
        },
    )
    own = services.build_history(conv, with_tools=True, for_user=anna)
    other = services.build_history(conv, with_tools=True, for_user=ben)
    assert any(m.content == "ROH" for m in own)
    assert [m.content for m in other] == ["F", "A"]
    assert all(isinstance(m, ChatMessage) and m.role != "tool" for m in other)


def test_citation_style_of_sender(chat, ben, monkeypatch):
    seen = []
    original = sharing.citations.prefs_for

    def spy(user):
        seen.append(getattr(user, "pk", None))
        return original(user)

    monkeypatch.setattr(sharing.citations, "prefs_for", spy)
    turn = services.Turn(ben, chat["conv"], chat["a"].model, None, chat["a"])
    loop = services._Loop(turn)
    assert seen == [ben.pk]
    assert loop.sources.prefs.style == original(ben).style


# --- Quellen aus fremden Sammlungen ----------------------------------------------------


def test_sources_from_unreadable_collection_without_link(client, chat, anna, ben):
    coll = Collection.objects.create(owner=anna, name="Annas Ordner")
    doc = Document.objects.create(
        collection=coll,
        title="Geheimes Gutachten",
        file="documents/x.pdf",
        status=Document.Status.INDEXED,
        bib_authors="Muster, Max",
    )
    chunk = Chunk.objects.create(
        document=doc, position=0, text="Inhalt", page=7, embedding=[0.0] * EMBEDDING_DIMENSIONS
    )
    SourceRef.objects.create(
        message=chat["a"],
        kind=SourceRef.Kind.DOCUMENT,
        title="Geheimes Gutachten",
        chunk=chunk,
        page=7,
        paragraph=2,
        biblio={"title": "Geheimes Gutachten", "authors": ["Muster, Max"]},
    )
    share_to(chat["conv"], ben)
    chunk_url = reverse("chat:document_chunk", args=[chunk.pk])
    # Besitzerin: Link und Literaturangaben
    client.force_login(anna)
    assert (
        chunk_url
        in client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    )
    # Empfänger: nur Titel und Seite, kein Link, Abschnitt 404
    client.force_login(ben)
    html = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert "Geheimes Gutachten" in html and "S. 7" in html
    assert chunk_url not in html and "Muster" not in html
    src = client.get(api("api_messages", chat["conv"])).json()[1]["sources"][0]
    assert src["url"] == "" and src["restricted"] is True
    assert "Muster" not in json.dumps(src) and "paragraph" not in src
    assert client.get(chunk_url).status_code == 404
    # Sammlung an Bens Gruppe geteilt -> Link wieder da
    group = UserGroup.objects.create(name="Leser")
    ben.groups.add(group)
    Share.objects.create(collection=coll, group=group)
    html = client.get(reverse("chat:conversation", args=[chat["conv"].pk])).content.decode()
    assert chunk_url in html
    assert client.get(chunk_url).status_code == 200


def test_recipient_collections_follow_own_rights(client, chat, anna, ben, ai_model, fake):
    coll = Collection.objects.create(owner=anna, name="Privat")
    share_to(chat["conv"], ben, write=True)
    client.force_login(ben)
    response = stream(client, chat["conv"], content="x", model=ai_model.pk, collections=[coll.pk])
    assert response.status_code == 404


# --- Verwalter -----------------------------------------------------------------------------


def test_admin_sees_no_foreign_content(client, chat, ben):
    admin_user = make_user("admin", "verwalter")
    admin_user.is_staff = True
    admin_user.save()
    share_to(chat["conv"], ben, write=True)
    client.force_login(admin_user)
    assert client.get(reverse("chat:conversation", args=[chat["conv"].pk])).status_code == 404
    assert client.get(api("api_messages", chat["conv"])).status_code == 404
    response = client.get(reverse("admin:chat_share_changelist"))
    assert response.status_code == 200
    html = response.content.decode()
    assert "Urlaub" not in html and "Nach Rom" not in html
    assert "RW" in html and "Ben" in html
    assert client.get(reverse("admin:chat_share_add")).status_code == 403


# --- Rückfrage nur durch den Auslöser ----------------------------------------------------


def test_tool_confirmation_only_by_trigger(client, chat, anna, ben, ai_model):
    conv = chat["conv"]
    share_to(conv, ben, write=True)
    pending = services.append_message(
        conv,
        role="assistant",
        content="",
        model=ai_model,
        author=anna,
        status=Message.Status.AWAITING_CONFIRMATION,
        parent=chat["q"],
    )
    tc = ToolCall.objects.create(
        message=pending, tool="x", status=ToolCall.Status.AWAITING_CONFIRMATION
    )
    client.force_login(ben)
    url = api("api_tool_confirm", conv)
    response = jpost(client, url, {"decisions": {str(tc.pk): "approve"}})
    assert response.status_code == 403
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "data-tool-confirm-all" not in html
    assert "Nur wer diese Antwort ausgelöst hat" in html
    client.force_login(anna)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert "data-tool-confirm-all" in html


# --- Kopie fortsetzen ----------------------------------------------------------------------


def test_copy_continues_as_own_chat(client, chat, anna, ben, ai_model, fake):
    conv = chat["conv"]
    att = Attachment.objects.create(
        message=chat["q"],
        owner=anna,
        conversation=conv,
        kind=Attachment.Kind.FILE,
        file="attachments/1/x.txt",
        original_name="notiz.txt",
        size=3,
    )
    chat["a"].cost = Decimal("1")
    chat["a"].save()
    SourceRef.objects.create(
        message=chat["a"], kind=SourceRef.Kind.WEB, title="W", url="https://x.de"
    )
    share_to(conv, ben)
    client.force_login(ben)
    response = jpost(client, api("api_conversation_copy", conv))
    assert response.status_code == 201
    copy = Conversation.objects.get(pk=response.json()["id"])
    assert copy.user_id == ben.pk and copy.title == "Urlaub (Kopie)"
    assert copy.system_prompt == "Chat-Regel: knapp."
    msgs = list(copy.messages.order_by("pk"))
    assert [m.content for m in msgs] == ["Wohin?", "Nach Rom."]
    assert msgs[1].parent_id == msgs[0].pk and copy.current_leaf_id == msgs[1].pk
    assert msgs[1].cost is None  # kein doppeltes Budget
    copied = Attachment.objects.get(message=msgs[0])
    assert copied.file.name == att.file.name and copied.owner_id == ben.pk
    assert SourceRef.objects.filter(message=msgs[1]).count() == 1
    # Ben schreibt in seiner Kopie weiter (Original bleibt unberührt).
    fake(OK_EVENTS)
    parse_sse(stream(client, copy, content="Weiter", model=ai_model.pk))
    assert conv.messages.count() == 2
    assert copy.messages.count() == 4


def test_copy_not_for_supervision(client, ai_model):
    teen = make_user("teen", "teenie")
    teen.allow_supervision = True
    teen.save()
    conv = Conversation.objects.create(user=teen)
    admin_user = make_user("admin", "aufsicht")
    client.force_login(admin_user)
    assert client.get(reverse("chat:conversation", args=[conv.pk])).status_code == 200
    assert jpost(client, api("api_conversation_copy", conv)).status_code == 403
    assert jpost(client, api("api_branch", conv), {"message_id": 1}).status_code == 403


# --- Verfasser an Nachrichten --------------------------------------------------------------


def test_author_names_when_several_people(client, chat, anna, ben, ai_model, fake):
    conv = chat["conv"]
    client.force_login(anna)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert '<h2 class="chat-message-author">Du</h2>' in html
    share_to(conv, ben, write=True)
    client.force_login(ben)
    fake(OK_EVENTS)
    start = parse_sse(stream(client, conv, content="Von Ben", model=ai_model.pk))[0][1]
    assert Message.objects.get(pk=start["user_message_id"]).author_id == ben.pk
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert '<h2 class="chat-message-author">Anna</h2>' in html
    assert '<h2 class="chat-message-author">Du</h2>' in html
    client.force_login(anna)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    assert '<h2 class="chat-message-author">Ben</h2>' in html
    export = client.get(reverse("chat:conversation_export", args=[conv.pk])).content.decode()
    assert "## Ben" in export and "## Du" in export


# --- Gleichzeitigkeit: current_leaf ------------------------------------------------------------


def _ids(client, conv):
    return [m["id"] for m in client.get(api("api_messages", conv)).json()]


def test_concurrency_and_views(client, chat, anna, ben, ai_model, fake):
    conv, q, a = chat["conv"], chat["q"], chat["a"]
    share_to(conv, ben, write=True, update=True)
    # Ben hängt an den Hauptpfad -> Hauptpfad spult vor, Anna sieht es.
    client.force_login(ben)
    fake(OK_EVENTS)
    start = parse_sse(stream(client, conv, content="Ben fragt", model=ai_model.pk, leaf=a.pk))[0][1]
    ben_answer = start["assistant_message_id"]
    conv.refresh_from_db()
    assert conv.current_leaf_id == ben_answer
    assert not ConversationView.objects.exists()
    # Anna sendet mit veraltetem Stand -> 409, nichts angelegt.
    client.force_login(anna)
    before = Message.objects.count()
    response = stream(client, conv, content="Anna (alt)", model=ai_model.pk, leaf=a.pk)
    assert response.status_code == 409 and response.json()["error"] == sharing.STALE_MESSAGE
    assert Message.objects.count() == before
    assert _ids(client, conv)[-1] == ben_answer
    # Ben schaltet auf eine ältere Version um -> nur seine Ansicht.
    client.force_login(ben)
    fake(OK_EVENTS)
    regen = parse_sse(stream(client, conv, regenerate=True, model=ai_model.pk, message_id=a.pk))
    new_a = regen[0][1]["assistant_message_id"]
    conv.refresh_from_db()
    assert conv.current_leaf_id == ben_answer  # Neu erzeugen ändert Annas Pfad nicht
    assert _ids(client, conv) == [q.pk, new_a]
    client.force_login(anna)
    assert _ids(client, conv)[-1] == ben_answer
    client.force_login(ben)
    assert jpost(client, api("api_branch", conv), {"message_id": a.pk}).status_code == 200
    assert _ids(client, conv)[-1] == ben_answer
    assert not ConversationView.objects.filter(user=ben).exists()  # folgt wieder dem Hauptpfad
    # Bearbeiten (U) durch Ben: eigener Zweig, Annas Ansicht bleibt.
    fake(OK_EVENTS)
    edit = parse_sse(stream(client, conv, content="Wohin 2?", model=ai_model.pk, edit_of=q.pk))
    edited = edit[0][1]["user_message_id"]
    assert _ids(client, conv)[0] == edited
    client.force_login(anna)
    assert _ids(client, conv)[0] == q.pk
    # Stand für den Hinweis „Neue Nachrichten“
    state = client.get(api("api_conversation_state", conv)).json()
    assert state["stamp"] == sharing.state_stamp(conv) and state["rights"] == "RWUD"


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_parallel_sends_keep_tree_consistent(ai_model):
    """Besitzerin und Empfänger senden gleichzeitig (eigene Threads/Verbindungen)."""
    anna = make_user("adult", "parallel_anna")
    ben = make_user("adult", "parallel_ben")
    conv = Conversation.objects.create(user=anna)
    first = services.append_message(conv, role="user", content="Start", author=anna)
    leaf = services.append_message(conv, role="assistant", content="A", model=ai_model, author=anna)
    share_to(conv, ben, write=True)
    barrier = threading.Barrier(2)
    errors = []

    def worker(user, text):
        try:
            barrier.wait(timeout=10)
            fresh = sharing.bind_viewer(Conversation.objects.get(pk=conv.pk), user)
            services.prepare_turn(user, fresh, ai_model, content=text)
        except Exception as exc:  # pragma: no cover - nur zur Diagnose
            errors.append(exc)
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(anna, "Anna parallel")),
        threading.Thread(target=worker, args=(ben, "Ben parallel")),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    tree = services.Tree(conv)
    msgs = {m.pk: m for m in conv.messages.all()}
    assert len(msgs) == 6
    for m in msgs.values():
        assert m.parent_id is None or m.parent_id in msgs
    conv.refresh_from_db()
    assert conv.current_leaf_id in msgs
    path = tree.path_to(conv.current_leaf_id)
    # Die Sperre reiht die Züge: der zweite hängt hinter dem ersten, nichts geht verloren.
    assert path[:2] == [first.pk, leaf.pk] and len(path) == 6
    users = [m for m in msgs.values() if m.role == "user" and m.pk != first.pk]
    assert {m.author_id for m in users} == {anna.pk, ben.pk}
