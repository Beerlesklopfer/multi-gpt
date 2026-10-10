"""MCP-Server ``/mcp/`` (M15): Protokoll (beide Ären), Werkzeuge je Scope, jedes
Werkzeug einmal, Fehlerfälle, Drosselung, Audit-Log ohne Inhalte."""

import base64
import json
import logging

import httpx
import pytest
import respx
from django.core.files.base import ContentFile
from mcp_types import (
    CLIENT_CAPABILITIES_META_KEY,
    CLIENT_INFO_META_KEY,
    PROTOCOL_VERSION_META_KEY,
)

from multigpt.accounts.models import UserGroup
from multigpt.billing.models import UsageEntry
from multigpt.chat.models import (
    AIModel,
    Attachment,
    Chunk,
    Collection,
    Conversation,
    Document,
    IndexRun,
    Job,
    Message,
    Provider,
    Share,
)
from multigpt.chat.rag.embeddings import fake_embedding
from multigpt.chat.websearch import fetch as web_fetch
from multigpt.node import scopes as S
from multigpt.node.models import ApiCall, ApiKey
from multigpt.rag.models import DirectorySource
from tests.billing_helpers import set_price
from tests.node_support import (
    call,
    make_key,
    make_user,
    post,
    rpc,
    structured,
    text,
    tool_names,
)

pytestmark = pytest.mark.django_db

BASE = "https://llm.example.invalid/v1"
SECRET_PROMPT = "GEHEIMER-PROMPT-INHALT"


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    settings.ALLOWED_HOSTS = ["testserver", "nas.example"]


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def key(anna):
    return make_key(anna)


@pytest.fixture
def secret(key):
    return key[1]


def modern_body(method, params=None, request_id=1, version="2026-07-28"):
    params = dict(params or {})
    params["_meta"] = {
        PROTOCOL_VERSION_META_KEY: version,
        CLIENT_CAPABILITIES_META_KEY: {},
        CLIENT_INFO_META_KEY: {"name": "test", "version": "1"},
    }
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def modern(client, secret, method, params=None, name=None, **headers):
    extra = {
        "HTTP_MCP_PROTOCOL_VERSION": "2026-07-28",
        "HTTP_MCP_METHOD": method,
    }
    if name:
        extra["HTTP_MCP_NAME"] = name
    extra.update(headers)
    return post(client, secret, modern_body(method, params), **extra)


# --- Transport und Authentifizierung --------------------------------------------------


def test_missing_or_bad_key_is_401_without_details(client):
    for secret in (None, "falsch", "mgpt_abcdefghij_" + "x" * 43):
        response = rpc(client, secret, "ping")
        assert response.status_code == 401
        assert response.content == b""
        assert response["WWW-Authenticate"].startswith("Bearer")
    assert ApiCall.objects.filter(status=ApiCall.Status.UNAUTHORIZED).count() == 3


def test_cookie_login_does_not_authenticate(client, anna):
    client.force_login(anna)
    assert rpc(client, None, "ping").status_code == 401


def test_get_and_delete_are_405(client, secret):
    assert client.get("/mcp/", HTTP_AUTHORIZATION=f"Bearer {secret}").status_code == 405
    assert client.delete("/mcp/", HTTP_AUTHORIZATION=f"Bearer {secret}").status_code == 405


def test_path_without_slash_works(client, secret):
    response = client.post(
        "/mcp",
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}),
        content_type="application/json",
        HTTP_AUTHORIZATION=f"Bearer {secret}",
    )
    assert response.status_code == 200
    assert response.json() == {"jsonrpc": "2.0", "id": 1, "result": {}}


def test_foreign_origin_is_forbidden(client, secret):
    assert rpc(client, secret, "ping", HTTP_ORIGIN="https://evil.example").status_code == 403
    assert rpc(client, secret, "ping", HTTP_ORIGIN="https://nas.example").status_code == 200


def test_failed_attempts_block_ip(client, settings, secret):
    settings.API_FAILURE_LIMIT = 3
    for _ in range(3):
        assert rpc(client, "mgpt_abcdefghij_" + "y" * 43, "ping").status_code == 401
    # Auch mit gültigem Key gesperrt (wie die Login-Drosselung).
    response = rpc(client, secret, "ping")
    assert response.status_code == 429
    assert "Retry-After" in response


def test_rate_limit_per_key(client, settings, secret):
    settings.API_RATE_LIMIT_PER_MINUTE = 2
    assert rpc(client, secret, "ping").status_code == 200
    assert rpc(client, secret, "ping").status_code == 200
    assert rpc(client, secret, "ping").status_code == 429


def test_last_used_is_recorded(client, key):
    api_key, secret = key
    rpc(client, secret, "ping", REMOTE_ADDR="10.1.2.3")
    api_key.refresh_from_db()
    assert api_key.last_used_at is not None
    assert api_key.last_used_ip == "10.1.2.3"


def test_body_size_limit(client, settings, secret):
    settings.DOCUMENT_MAX_UPLOAD_MB = 1
    big = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping", "x": "a" * 3_000_000})
    assert post(client, secret, big).status_code == 413


# --- Protokoll ----------------------------------------------------------------------


def test_initialize_handshake_is_stateless(client, secret):
    response = rpc(
        client,
        secret,
        "initialize",
        {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "n8n"}},
    )
    assert response.status_code == 200
    assert "Mcp-Session-Id" not in response
    result = response.json()["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"] == {"listChanged": False}
    assert result["serverInfo"]["name"] == "multi-gpt"
    # Unbekannte Version -> neueste Handshake-Version
    result = rpc(client, secret, "initialize", {"protocolVersion": "1999-01-01"}).json()["result"]
    assert result["protocolVersion"] == "2025-11-25"
    # notifications/initialized -> 202 ohne Inhalt
    response = post(client, secret, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert response.status_code == 202 and response.content == b""


def test_legacy_results_have_no_modern_fields(client, secret):
    result = rpc(client, secret, "tools/list", HTTP_MCP_PROTOCOL_VERSION="2025-11-25").json()
    assert "resultType" not in result["result"] and "ttlMs" not in result["result"]
    bad = rpc(client, secret, "tools/list", HTTP_MCP_PROTOCOL_VERSION="2030-01-01")
    assert bad.status_code == 400
    assert bad.json()["error"]["data"]["requested"] == "2030-01-01"


def test_modern_envelope_discover_and_tools_list(client, secret):
    response = modern(client, secret, "server/discover")
    assert response.status_code == 200, response.content
    result = response.json()["result"]
    assert "2026-07-28" in result["supportedVersions"]
    assert result["resultType"] == "complete"
    assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "multi-gpt"
    listed = modern(client, secret, "tools/list").json()["result"]
    assert listed["resultType"] == "complete"
    assert {t["name"] for t in listed["tools"]} >= {"usage", "list_collections"}


def test_modern_header_mismatch_and_unknown_method(client, secret):
    response = post(
        client,
        secret,
        modern_body("tools/list"),
        HTTP_MCP_PROTOCOL_VERSION="2026-07-28",
        HTTP_MCP_METHOD="ping",
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020
    response = modern(client, secret, "prompts/list")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == -32601
    # Ältere Ära: Methode unbekannt mit HTTP 200
    assert rpc(client, secret, "prompts/list").json()["error"]["code"] == -32601


def test_malformed_requests(client, secret):
    assert post(client, secret, "{kaputt").json()["error"]["code"] == -32700
    assert post(client, secret, [{"jsonrpc": "2.0", "id": 1}]).status_code == 400
    assert post(client, secret, {"jsonrpc": "1.0", "id": 1, "method": "ping"}).status_code == 400
    response = rpc(client, secret, "tools/call", {"name": "gibtsnicht"})
    assert response.json()["error"]["code"] == -32602
    response = rpc(client, secret, "tools/call", {"name": "usage", "arguments": "x"})
    assert response.json()["error"]["code"] == -32602


def test_tools_list_follows_scopes(client, anna):
    _, docs = make_key(anna, [S.DOCS_READ], name="docs")
    names = tool_names(client, docs)
    assert names == {"list_collections"}  # weitere Dokumentwerkzeuge erst mit Dokumenten
    _, usage = make_key(anna, [S.USAGE_READ], name="usage")
    assert tool_names(client, usage) == {"usage"}
    _, write = make_key(anna, [S.DOCS_WRITE], name="write")
    assert tool_names(client, write) == {
        "upload_document",
        "delete_document",
        "reindex",
        "run_status",
        "list_runs",
    }
    _, index = make_key(anna, [S.INDEX_CONTROL], name="index")
    # start_scan und list_sources nur für Verwalter
    assert tool_names(client, index) == {"run_status", "list_runs", "cancel_run"}
    _, files = make_key(anna, [S.FILES_READ], name="files")
    assert tool_names(client, files) == {"get_file"}
    # Werkzeug ohne Scope -> wie unbekannt
    response, _ = call(client, usage, "list_collections")
    assert response.json()["error"]["code"] == -32602
    assert ApiCall.objects.filter(status=ApiCall.Status.DENIED).exists()


def test_admin_sees_scan_tools(client):
    admin = make_user("chef", "admin")
    _, secret = make_key(admin, [S.INDEX_CONTROL])
    assert {"start_scan", "list_sources"} <= tool_names(client, secret)


# --- Dokumente ----------------------------------------------------------------------


def _indexed(collection, title="Handbuch", text_body="Die Heizung wartet man im Herbst."):
    doc = Document.objects.create(
        collection=collection, title=title, status=Document.Status.INDEXED, file="documents/x.txt"
    )
    Chunk.objects.create(
        document=doc, position=0, text=text_body, page=1, embedding=fake_embedding(text_body)
    )
    return doc


def test_docs_read_tools(client, settings, anna, secret):
    settings.DEBUG = True
    settings.RAG_FAKE_EMBEDDINGS = True
    own = Collection.objects.create(owner=anna, name="Haus")
    doc = _indexed(own)
    foreign = Collection.objects.create(owner=make_user("bernd"), name="Privat")
    foreign_doc = _indexed(foreign, "Geheim")

    _, result = call(client, secret, "list_collections")
    names = [c["name"] for c in structured(result)["collections"]]
    assert names == ["Haus"]

    _, result = call(client, secret, "search_documents", {"query": "Heizung Herbst"})
    assert not result["isError"]
    assert "<quellmaterial" in text(result)
    assert structured(result)["sources"][0]["title"] == "Handbuch"
    assert not Message.objects.exists()  # lesend: keine Chatnachricht

    _, result = call(client, secret, "list_documents", {})
    assert "Handbuch" in text(result) and "Geheim" not in text(result)
    _, result = call(client, secret, "read_document", {"document_id": doc.pk})
    assert "Heizung" in text(result)
    _, result = call(client, secret, "read_document", {"document_id": foreign_doc.pk})
    assert result["isError"] and "nicht gefunden" in text(result)
    _, result = call(client, secret, "document_info", {"document_id": doc.pk})
    assert not result["isError"]


def test_key_limited_to_collections(client, settings, anna):
    settings.DEBUG = True
    settings.RAG_FAKE_EMBEDDINGS = True
    allowed = Collection.objects.create(owner=anna, name="Erlaubt")
    hidden = Collection.objects.create(owner=anna, name="Verborgen")
    _indexed(allowed, "Offen")
    secret_doc = _indexed(hidden, "Zu", "Heizung im Keller")
    _, secret = make_key(anna, [S.DOCS_READ, S.FILES_READ, S.DOCS_WRITE], collections=[allowed])
    _, result = call(client, secret, "list_collections")
    assert [c["name"] for c in structured(result)["collections"]] == ["Erlaubt"]
    _, result = call(client, secret, "search_documents", {"query": "Heizung Keller"})
    assert all(s["title"] == "Offen" for s in structured(result).get("sources", []))
    _, result = call(
        client, secret, "search_documents", {"query": "x", "collections": ["Verborgen"]}
    )
    assert result["isError"]
    _, result = call(client, secret, "list_documents", {"collection": "Verborgen"})
    assert result["isError"]
    _, result = call(client, secret, "read_document", {"document_id": secret_doc.pk})
    assert result["isError"]
    _, result = call(client, secret, "get_file", {"document_id": secret_doc.pk})
    assert result["isError"]
    _, result = call(client, secret, "reindex", {"collection": hidden.pk})
    assert result["isError"]


def test_upload_base64_starts_run_and_checks_type_and_size(client, settings, anna, secret):
    collection = Collection.objects.create(owner=anna, name="Haus")
    content = base64.b64encode(b"Hallo Welt, eine Notiz.").decode()
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": "Haus", "filename": "notiz.txt", "content_base64": content},
    )
    assert not result["isError"], text(result)
    data = structured(result)
    document = Document.objects.get(pk=data["document_id"])
    assert document.collection == collection and document.title == "notiz.txt"
    run = IndexRun.objects.get(pk=data["run_id"])
    assert run.kind == IndexRun.Kind.UPLOAD and run.started_by == anna
    assert Job.objects.filter(run=run).count() == 1
    # Typ am Inhalt: Binärmüll als .pdf
    bad = base64.b64encode(b"\x00\x01kein pdf").decode()
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": collection.pk, "filename": "x.pdf", "content_base64": bad},
    )
    assert result["isError"]
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": collection.pk, "filename": "x.exe", "content_base64": content},
    )
    assert result["isError"]
    # Größe
    settings.DOCUMENT_MAX_UPLOAD_MB = 1
    big = base64.b64encode(b"a" * (1024 * 1024 + 10)).decode()
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": collection.pk, "filename": "gross.txt", "content_base64": big},
    )
    assert result["isError"] and "zu groß" in text(result)
    # kein gültiges base64, beides bzw. nichts
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": collection.pk, "filename": "a.txt", "content_base64": "%%%"},
    )
    assert result["isError"]
    _, result = call(client, secret, "upload_document", {"collection": collection.pk})
    assert result["isError"]


def test_upload_needs_write_and_role_right(client, anna, secret):
    bernd = make_user("bernd")
    shared = Collection.objects.create(owner=bernd, name="Geteilt")
    group = UserGroup.objects.get(pk=anna.groups.first().pk)
    Share.objects.create(collection=shared, group=group, can_write=False)
    content = base64.b64encode(b"Text").decode()
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": shared.pk, "filename": "a.txt", "content_base64": content},
    )
    assert result["isError"] and "nur lesen" in text(result)
    role = anna.role
    role.can_upload_documents = False
    role.save()
    own = Collection.objects.create(owner=anna, name="Eigen")
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": own.pk, "filename": "a.txt", "content_base64": content},
    )
    assert result["isError"] and "keine Dokumente hochladen" in text(result)


def test_upload_from_url_uses_ssrf_protection(client, anna, secret, monkeypatch):
    Collection.objects.create(owner=anna, name="Haus")
    _, result = call(
        client, secret, "upload_document", {"collection": "Haus", "url": "http://127.0.0.1/a.txt"}
    )
    assert result["isError"] and "lokalen Netz" in text(result)
    _, result = call(
        client, secret, "upload_document", {"collection": "Haus", "url": "file:///etc/passwd"}
    )
    assert result["isError"]
    seen = {}

    def fake_fetch(url, **kwargs):
        seen.update(kwargs, url=url)
        return web_fetch.RawPage(
            url="https://example.org/docs/anleitung.txt",
            mime="text/plain",
            content_type="text/plain",
            body=b"Anleitung",
        )

    monkeypatch.setattr(web_fetch, "fetch_raw", fake_fetch)
    _, result = call(
        client,
        secret,
        "upload_document",
        {"collection": "Haus", "url": "https://example.org/docs/anleitung.txt"},
    )
    assert not result["isError"], text(result)
    assert Document.objects.get(pk=structured(result)["document_id"]).title == "anleitung.txt"
    assert seen["max_bytes"] == 25 * 1024 * 1024


def test_delete_document(client, anna, secret):
    collection = Collection.objects.create(owner=anna, name="Haus")
    doc = Document.objects.create(collection=collection, title="Alt", file="documents/a.txt")
    _, result = call(client, secret, "delete_document", {"document_id": doc.pk})
    assert not result["isError"]
    assert not Document.objects.filter(pk=doc.pk).exists()
    foreign = Document.objects.create(
        collection=Collection.objects.create(owner=make_user("bernd"), name="B"), title="B"
    )
    _, result = call(client, secret, "delete_document", {"document_id": foreign.pk})
    assert result["isError"]
    assert Document.objects.filter(pk=foreign.pk).exists()


# --- Läufe --------------------------------------------------------------------------


def test_reindex_status_and_cancel(client, anna, secret):
    collection = Collection.objects.create(owner=anna, name="Haus")
    docs = [Document.objects.create(collection=collection, title=f"D{i}") for i in range(3)]
    _, result = call(client, secret, "reindex", {"collection": collection.pk})
    run_id = structured(result)["id"]
    run = IndexRun.objects.get(pk=run_id)
    assert run.kind == IndexRun.Kind.REINDEX_COLLECTION and run.docs_queued == 3
    _, result = call(client, secret, "run_status", {"run_id": run_id})
    assert structured(result)["open"] is True
    assert "0 von 3" in text(result)
    _, result = call(client, secret, "list_runs", {"open_only": True})
    assert [r["id"] for r in structured(result)["runs"]] == [run_id]
    _, result = call(client, secret, "cancel_run", {"run_id": run_id})
    assert not result["isError"]
    run.refresh_from_db()
    assert run.status == IndexRun.Status.CANCELLED
    _, result = call(client, secret, "cancel_run", {"run_id": run_id})
    assert result["isError"] and "beendet" in text(result)
    # Einzelne Dokumente
    _, result = call(client, secret, "reindex", {"documents": [docs[0].pk, docs[1].pk]})
    assert structured(result)["kind"] == IndexRun.Kind.REINDEX_DOCUMENTS
    assert structured(result)["docs_queued"] == 2


def test_foreign_runs_are_invisible_and_scan_admin_only(client, anna, secret, settings, tmp_path):
    bernd = make_user("bernd")
    other = Collection.objects.create(owner=bernd, name="B")
    run = IndexRun.objects.create(kind=IndexRun.Kind.UPLOAD, collection=other, started_by=bernd)
    _, result = call(client, secret, "run_status", {"run_id": run.pk})
    assert result["isError"] and "nicht gefunden" in text(result)
    _, result = call(client, secret, "cancel_run", {"run_id": run.pk})
    assert result["isError"]
    settings.RAG_SOURCE_ROOTS = [str(tmp_path)]
    source = DirectorySource.objects.create(collection=other, path=str(tmp_path))
    response, _ = call(client, secret, "start_scan", {"source": source.pk})
    assert response.json()["error"]["code"] == -32602  # nicht angeboten

    admin = make_user("chef", "admin")
    _, admin_secret = make_key(admin, [S.INDEX_CONTROL], name="admin", sources=[source])
    _, result = call(client, admin_secret, "run_status", {"run_id": run.pk})
    assert not result["isError"]
    _, result = call(client, admin_secret, "list_sources")
    assert [s["id"] for s in structured(result)["sources"]] == [source.pk]
    assert str(tmp_path) not in text(result)  # kein Serverpfad
    _, result = call(client, admin_secret, "start_scan", {"source": source.pk})
    assert structured(result)["new"] is True
    scan_run = IndexRun.objects.get(pk=structured(result)["id"])
    assert scan_run.kind == IndexRun.Kind.DIRECTORY_SCAN and scan_run.started_by == admin
    _, result = call(client, admin_secret, "start_scan", {"source": source.pk})
    assert structured(result)["new"] is False and structured(result)["id"] == scan_run.pk
    # Quelle außerhalb der Einschränkung des Keys
    second = DirectorySource.objects.create(collection=other, path=str(tmp_path))
    _, result = call(client, admin_secret, "start_scan", {"source": second.pk})
    assert result["isError"]


# --- Dateien und Verbrauch -----------------------------------------------------------


def _attachment(user, data=b"%PDF-1.4 test"):
    conversation = Conversation.objects.create(user=user)
    message = Message.objects.create(conversation=conversation, role=Message.Role.ASSISTANT)
    attachment = Attachment(
        message=message,
        conversation=conversation,
        kind=Attachment.Kind.FILE,
        mime_type="application/pdf",
        original_name="blatt.pdf",
        size=len(data),
    )
    attachment.file.save("blatt.pdf", ContentFile(data), save=True)
    return attachment


def test_get_file_with_rights_and_limit(client, settings, anna, secret):
    own = _attachment(anna)
    _, result = call(client, secret, "get_file", {"attachment_id": own.pk})
    assert not result["isError"], text(result)
    resource = result["content"][1]["resource"]
    assert resource["mimeType"] == "application/pdf"
    assert base64.b64decode(resource["blob"]) == b"%PDF-1.4 test"
    assert resource["uri"] == f"multigpt://attachment/{own.pk}"
    foreign = _attachment(make_user("bernd"))
    _, result = call(client, secret, "get_file", {"attachment_id": foreign.pk})
    assert result["isError"] and "nicht gefunden" in text(result)
    collection = Collection.objects.create(owner=anna, name="Haus")
    doc = Document(collection=collection, title="Notiz")
    doc.file.save("n.txt", ContentFile(b"Inhalt"), save=True)
    _, result = call(client, secret, "get_file", {"document_id": doc.pk})
    assert base64.b64decode(result["content"][1]["resource"]["blob"]) == b"Inhalt"
    settings.API_FILE_MAX_MB = 1
    big = _attachment(anna, b"x" * (1024 * 1024 + 1))
    _, result = call(client, secret, "get_file", {"attachment_id": big.pk})
    assert result["isError"] and "größer" in text(result)
    _, result = call(client, secret, "get_file", {})
    assert result["isError"]


def test_usage_tool(client, secret):
    _, result = call(client, secret, "usage")
    assert not result["isError"]
    assert "month" in structured(result)


# --- Werkzeuge (tools.run) ------------------------------------------------------------


def test_builtin_tool_runs_in_api_chat(client, anna, secret, monkeypatch):
    from multigpt.chat import tooling
    from multigpt.chat.websearch import pages

    monkeypatch.setattr(pages, "available", lambda user, flag, cfg=None: True)
    monkeypatch.setattr(
        pages, "run_fetch", lambda url, sources, cfg, **kw: (f"Seite {url} gelesen", False)
    )
    assert "fetch_url" in tool_names(client, secret)
    _, result = call(client, secret, "fetch_url", {"url": "https://example.org/"})
    assert not result["isError"], text(result)
    assert "gelesen" in text(result)
    data = structured(result)
    conversation = Conversation.objects.get(pk=data["chat_id"])
    assert conversation.user == anna and conversation.title.startswith("API: Test")
    message = Message.objects.get(pk=data["message_id"])
    assert message.tool_calls.get().tool == "fetch_url"
    # Zweiter Aufruf: gleicher Chat
    _, again = call(client, secret, "fetch_url", {"url": "https://example.org/b"})
    assert structured(again)["chat_id"] == conversation.pk
    # Ohne Rollenrecht (Websuche) nicht angeboten
    monkeypatch.setattr(pages, "available", lambda user, flag, cfg=None: False)
    assert "fetch_url" not in tool_names(client, secret)
    assert tooling.get_builtin("fetch_url") is not None


# --- ask -----------------------------------------------------------------------------


def _sse_body(*texts, prompt_tokens=100, completion_tokens=20):
    lines = []
    for piece in texts:
        chunk = {"choices": [{"index": 0, "delta": {"content": piece}, "finish_reason": None}]}
        lines.append(f"data: {json.dumps(chunk)}\n\n")
    usage = {
        "choices": [],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }
    lines.append(f"data: {json.dumps(usage)}\n\n")
    lines.append("data: [DONE]\n\n")
    return "".join(lines).encode()


@pytest.fixture
def chat_model():
    provider = Provider.objects.create(
        name="Cloud", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE, api_key="sk-test"
    )
    model = AIModel.objects.create(provider=provider, model_id="gpt-test", display_name="GPT Test")
    set_price(model, "2", "10")
    return model


def test_ask_books_on_account_and_is_visible(client, anna, secret, chat_model, caplog):
    caplog.set_level(logging.INFO)
    with respx.mock(assert_all_called=True) as mock:
        route = mock.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200,
                content=_sse_body("Hallo ", "Welt."),
                headers={"content-type": "text/event-stream"},
            )
        )
        _, result = call(client, secret, "ask", {"prompt": SECRET_PROMPT, "model": "gpt-test"})
    assert not result["isError"], result
    assert text(result) == "Hallo Welt."
    data = structured(result)
    request = json.loads(route.calls[0].request.content)
    assert "tools" not in request  # ohne „tools“ keine Werkzeuge
    conversation = Conversation.objects.get(pk=data["chat_id"])
    assert conversation.user == anna and conversation.title.startswith("API: ")
    message = Message.objects.get(pk=data["message_id"])
    assert message.status == Message.Status.COMPLETE and message.author == anna
    entry = UsageEntry.objects.get(message=message)
    assert entry.user == anna
    assert entry.amount_eur > 0
    assert data["cost_eur"] == str(message.cost)
    # Datenschutz: kein Inhalt im Audit-Log und in den Logs
    call_row = ApiCall.objects.get(tool="ask")
    assert SECRET_PROMPT not in json.dumps(list(ApiCall.objects.values()), default=str)
    assert call_row.status == ApiCall.Status.OK and call_row.response_bytes > 0
    assert SECRET_PROMPT not in caplog.text
    assert secret not in caplog.text


def test_ask_continue_chat_and_rights(client, anna, secret, chat_model):
    body = _sse_body("Ok.")
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200, content=body, headers={"content-type": "text/event-stream"}
            )
        )
        _, first = call(client, secret, "ask", {"prompt": "Eins"})
        chat_id = structured(first)["chat_id"]
        _, second = call(client, secret, "ask", {"prompt": "Zwei", "chat": chat_id})
    assert structured(second)["chat_id"] == chat_id
    assert Message.objects.filter(conversation_id=chat_id).count() == 4
    foreign = Conversation.objects.create(user=make_user("bernd"))
    _, result = call(client, secret, "ask", {"prompt": "x", "chat": foreign.pk})
    assert result["isError"] and "nicht gefunden" in text(result)
    # Werkzeuge nur mit tools.run, Sammlungen nur mit docs.read
    _, only_ask = make_key(anna, [S.CHAT_ASK], name="nur-ask")
    _, result = call(client, only_ask, "ask", {"prompt": "x", "tools": True})
    assert result["isError"] and "tools.run" in text(result)
    _, result = call(client, only_ask, "ask", {"prompt": "x", "collections": [1]})
    assert result["isError"] and "docs.read" in text(result)
    # Modell außerhalb der Rolle
    role = anna.role
    role.all_models = False
    role.save()
    _, result = call(client, secret, "ask", {"prompt": "x", "model": chat_model.pk})
    assert result["isError"]
    assert not Conversation.objects.filter(title="API: x").exists()  # leerer Chat entfernt


def test_ask_streams_progress_as_sse(client, secret, chat_model):
    with respx.mock() as mock:
        mock.post(f"{BASE}/chat/completions").mock(
            return_value=httpx.Response(
                200, content=_sse_body("A", "B"), headers={"content-type": "text/event-stream"}
            )
        )
        response = rpc(
            client,
            secret,
            "tools/call",
            {"name": "ask", "arguments": {"prompt": "Hi"}, "_meta": {"progressToken": "p1"}},
        )
        assert response["Content-Type"] == "text/event-stream"
        assert response["X-Accel-Buffering"] == "no"
        raw = b"".join(response.streaming_content).decode()
    events = [json.loads(line[6:]) for line in raw.splitlines() if line.startswith("data: ")]
    progress = [e for e in events if e.get("method") == "notifications/progress"]
    assert progress and progress[0]["params"]["progressToken"] == "p1"
    final = events[-1]
    assert final["id"] == 1 and final["result"]["content"][0]["text"] == "AB"
    assert ApiCall.objects.get(tool="ask").status == ApiCall.Status.OK


def test_list_models(client, secret, chat_model):
    _, result = call(client, secret, "list_models")
    assert [m["model_id"] for m in structured(result)["models"]] == ["gpt-test"]


def test_audit_log_has_no_contents(client, anna, secret):
    collection = Collection.objects.create(owner=anna, name="Haus")
    payload = base64.b64encode(b"STRENG-GEHEIMER-TEXT").decode()
    call(
        client,
        secret,
        "upload_document",
        {"collection": collection.pk, "filename": "geheim.txt", "content_base64": payload},
    )
    dump = json.dumps(list(ApiCall.objects.values()), default=str)
    assert "geheim.txt" not in dump and payload not in dump
    assert secret not in dump
    row = ApiCall.objects.get(tool="upload_document")
    assert row.request_bytes > len(payload) and row.method == "tools/call"
    assert ApiKey.objects.get().last_used_at is not None
