"""Eingebaute Werkzeuge über die API (M15, ``tools.run``): create_pdf,
generate_image, run_python und web_search mit derselben Logik wie im Chat –
Rollenrechte, Buchung, Anhänge im Chat „API: … – Werkzeuge“, Abholen per get_file."""

import base64
import io

import pytest
import respx
from PIL import Image

from multigpt.billing.models import UsageEntry
from multigpt.chat import documents_pdf, sandbox, websearch
from multigpt.chat.models import AIModel, Attachment, Provider
from multigpt.chat.websearch import Material
from multigpt.chat.websearch.base import SearchHit
from multigpt.node import scopes as S
from tests.billing_helpers import set_price
from tests.node_support import call, make_key, make_user, structured, text, tool_names

pytestmark = pytest.mark.django_db

WEASY = documents_pdf.weasyprint_installed()


@pytest.fixture(autouse=True)
def _no_pdf_tool():
    """Überschreibt conftest: create_pdf rendert hier wirklich (falls WeasyPrint da)."""


@pytest.fixture(autouse=True)
def _no_sandbox():
    """Überschreibt conftest: echter Sandbox-Status (run_python)."""


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")
    sandbox._status_cache = None
    yield
    sandbox._status_cache = None


@pytest.fixture
def anna():
    return make_user("anna")


@pytest.fixture
def secret(anna):
    return make_key(anna, [S.TOOLS_RUN, S.FILES_READ])[1]


@pytest.mark.skipif(not WEASY, reason="WeasyPrint bzw. Pango fehlt")
def test_create_pdf_and_get_file(client, secret):
    _, result = call(
        client, secret, "create_pdf", {"layout": "text", "content": "# Einkauf\n\n- Brot"}
    )
    assert not result["isError"], text(result)
    attachment = structured(result)["attachments"][0]
    assert attachment["mime_type"] == "application/pdf"
    _, file_result = call(
        client, secret, "get_file", {"attachment_id": attachment["attachment_id"]}
    )
    blob = base64.b64decode(file_result["content"][1]["resource"]["blob"])
    assert blob.startswith(b"%PDF-")


def test_create_pdf_needs_role_right(client, anna, secret):
    role = anna.role
    role.can_create_documents = False
    role.save()
    assert "create_pdf" not in tool_names(client, secret)


def _png() -> str:
    out = io.BytesIO()
    Image.new("RGB", (32, 32), (10, 120, 200)).save(out, "PNG")
    return base64.b64encode(out.getvalue()).decode()


def test_generate_image_books_on_account(client, anna, secret):
    provider = Provider.objects.create(name="OpenAI", kind=Provider.Kind.OPENAI_COMPAT, api_key="k")
    model = AIModel.objects.create(
        provider=provider,
        model_id="gpt-image-1",
        display_name="GPT Image",
        capability=AIModel.Capability.IMAGE,
    )
    set_price(model, "5", "40", unit_prices={"image:auto:square": "0.04"})
    payload = {
        "created": 1,
        "data": [{"b64_json": _png()}],
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }
    with respx.mock() as router:
        router.post("https://api.openai.com/v1/images/generations").respond(200, json=payload)
        _, result = call(client, secret, "generate_image", {"prompt": "Ein blaues Quadrat"})
    assert not result["isError"], text(result)
    attachment = Attachment.objects.get(pk=structured(result)["attachments"][0]["attachment_id"])
    assert attachment.kind == Attachment.Kind.IMAGE
    entry = UsageEntry.objects.get(attachment=attachment)
    assert entry.user == anna


@pytest.mark.skipif(not sandbox.available(), reason="bubblewrap nicht nutzbar")
def test_run_python_in_sandbox(client, secret):
    from multigpt.chat.models import ChatSettings

    cfg = ChatSettings.objects.get_or_create(pk=ChatSettings.SINGLETON_PK)[0]
    cfg.python_enabled = True
    cfg.save()
    _, result = call(client, secret, "run_python", {"code": "print(6*7)"})
    assert not result["isError"], text(result)
    assert "42" in text(result)


def test_web_search(client, anna, secret, monkeypatch):
    monkeypatch.setattr(websearch, "web_search_available", lambda user, cfg=None: True)
    hit = SearchHit(title="Beispiel", url="https://example.org/", snippet="Ein Treffer")
    monkeypatch.setattr(
        websearch, "gather", lambda query, cfg=None, **kw: [Material(hit=hit, text="Inhalt")]
    )
    _, result = call(client, secret, "web_search", {"query": "Beispiel"})
    assert not result["isError"], text(result)
    assert "<quellmaterial" in text(result)
    assert structured(result)["sources"][0]["url"] == "https://example.org/"
    role = anna.role
    role.can_web_search = False
    role.save()
    monkeypatch.undo()
    assert "web_search" not in tool_names(client, secret)
