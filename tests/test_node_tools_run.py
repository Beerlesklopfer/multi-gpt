"""Eingebaute Werkzeuge über die API (M15, ``tools.run``): create_pdf,
generate_image, run_python und web_search mit derselben Logik wie im Chat –
Rollenrechte, Buchung, Anhänge im Chat „API: … – Werkzeuge“, Abholen per get_file."""

import base64
import io
import json

import pytest
import respx
from PIL import Image

from multigpt.billing.models import UsageEntry
from multigpt.chat import documents_pdf, sandbox, websearch
from multigpt.chat.models import AIModel, Attachment, Provider
from multigpt.chat.websearch import Material
from multigpt.chat.websearch.base import SearchHit
from multigpt.node import protocol
from multigpt.node import scopes as S
from multigpt.node.tools import FileItem, ToolOutput
from tests.billing_helpers import set_price
from tests.node_support import (
    blocks,
    call,
    make_key,
    make_user,
    rpc,
    structured,
    text,
    tool_names,
)

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
    # Das PDF kommt direkt im Ergebnis (eingebettete Ressource), ohne structuredContent.
    assert "structuredContent" not in result
    (pdf,) = blocks(result, "resource")
    assert pdf["resource"]["mimeType"] == "application/pdf"
    assert base64.b64decode(pdf["resource"]["blob"]).startswith(b"%PDF-")
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
    (image,) = blocks(result, "image")
    assert image["mimeType"] == attachment.mime_type
    assert base64.b64decode(image["data"]) == attachment.file.open("rb").read()
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


# --- Ergebnis als MCP-Inhalte (run_python mit echter Sandbox) ------------------------------

needs_sandbox = pytest.mark.skipif(not sandbox.available(), reason="bubblewrap nicht nutzbar")


def _python_on():
    from multigpt.chat.models import ChatSettings

    cfg = ChatSettings.objects.get_or_create(pk=ChatSettings.SINGLETON_PK)[0]
    cfg.python_enabled = True
    cfg.save()


PLOT_ALL = """
import matplotlib.pyplot as plt
plt.plot([1, 2, 3], [1, 4, 9])
plt.savefig('kurve.png')
plt.savefig('kurve.svg')
plt.savefig('kurve.pdf')
print(6 * 7)
"""


@needs_sandbox
def test_run_python_returns_output_png_svg_and_pdf(client, secret):
    """Befund: über MCP kam nur {chat_id, message_id, attachments} an. Jetzt:
    stdout im Text, PNG als Bild, PDF als Ressource; das SVG nur als Verweis, weil das
    PNG zum Ansehen reicht (sonst füllt es den Kontext des Clients)."""
    _python_on()
    _, result = call(client, secret, "run_python", {"code": PLOT_ALL})
    assert not result["isError"], text(result)
    head = text(result)
    assert head.startswith("Exit-Status 0, Laufzeit")
    assert "Ausgabe (stdout):\n42" in head
    # Clients wie Claude Code zeigen sonst nur structuredContent (ohne Text und Bilder).
    assert "structuredContent" not in result
    (image,) = blocks(result, "image")
    assert image["mimeType"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(image["data"]))).format == "PNG"
    resources = {r["resource"]["mimeType"]: r["resource"] for r in blocks(result, "resource")}
    assert "image/svg+xml" not in resources
    (svg,) = blocks(result, "resource_link")
    assert svg["mimeType"] == "image/svg+xml" and svg["uri"].startswith("multigpt://attachment/")
    assert "get_file" in text(result)
    assert base64.b64decode(resources["application/pdf"]["blob"]).startswith(b"%PDF-")
    data = structured(result)
    assert {a["name"] for a in data["attachments"]} == {"kurve.png", "kurve.svg", "kurve.pdf"}
    assert data["chat_id"] and data["message_id"]
    assert result["content"][0]["type"] == "text" and result["content"][-1]["type"] == "text"


@needs_sandbox
def test_run_python_svg_only_gets_png_preview(client, secret):
    _python_on()
    code = "import matplotlib.pyplot as plt\nplt.bar(['a', 'b'], [3, 5])\n"
    code += "plt.savefig('balken.svg')\n"
    _, result = call(client, secret, "run_python", {"code": code})
    assert not result["isError"], text(result)
    names = {a["name"] for a in structured(result)["attachments"]}
    assert names == {"balken.svg", "balken.png"}
    assert [b["mimeType"] for b in blocks(result, "image")] == ["image/png"]
    assert blocks(result, "resource_link")[0]["mimeType"] == "image/svg+xml"
    assert not blocks(result, "resource")


@needs_sandbox
def test_matplotlib_svg_keeps_text():
    """svg.fonttype none: Beschriftungen bleiben <text> statt Glyphen-Pfaden."""
    code = "import matplotlib.pyplot as plt\nplt.title('Umsatz')\nplt.savefig('t.svg')\n"
    (svg,) = sandbox.run(code, sandbox.Limits()).files
    content = svg.data.decode()
    assert "<text" in content and "Umsatz" in content
    assert '<use xlink:href="#DejaVuSans' not in content


@needs_sandbox
def test_png_preview_only_when_requested():
    """Im Chat (ohne png_preview) bleibt es beim SVG; mit Vorschau kommt das PNG dazu,
    nicht aber, wenn der Code die Figur schon geschlossen hat."""
    code = "import matplotlib.pyplot as plt\nplt.plot([1, 2])\nplt.savefig('linie.svg')\n"
    plain = sandbox.run(code, sandbox.Limits())
    assert [f.name for f in plain.files] == ["linie.svg"]
    preview = sandbox.run(code, sandbox.Limits(png_preview=True))
    assert sorted(f.name for f in preview.files) == ["linie.png", "linie.svg"]
    closed = sandbox.run(code + "plt.close('all')\n", sandbox.Limits(png_preview=True))
    assert [f.name for f in closed.files] == ["linie.svg"]


def test_run_python_description_prefers_svg(client, secret):
    from multigpt.chat import tools_python

    assert "bevorzugt als SVG" in tools_python.DESCRIPTION
    assert "plt.savefig('diagramm.svg')" in tools_python.DESCRIPTION
    from multigpt.node import tools as node_tools

    mcp = node_tools.get("run_python").description
    assert mcp.startswith(tools_python.DESCRIPTION)
    assert "stdout" in mcp and "als Bild" in mcp and "image/svg+xml" in mcp
    assert "application/pdf" in node_tools.get("create_pdf").description


NOISE = """
import numpy as np
import matplotlib.pyplot as plt
rng = np.random.default_rng(1)
plt.imsave('rauschen.png', rng.integers(0, 255, (900, 900, 3), dtype=np.uint8))
print('ok')
"""


@needs_sandbox
def test_run_python_large_image_over_sse_is_shrunk(client, secret):
    """Streamable HTTP mit SSE-Antwort (progressToken): Das Event bleibt unter
    1 MB (SDK-Grenze, LOP C18), das Bild kommt verkleinert, das Original über get_file."""
    _python_on()
    response = rpc(
        client,
        secret,
        "tools/call",
        {"name": "run_python", "arguments": {"code": NOISE}, "_meta": {"progressToken": "p"}},
    )
    assert response["Content-Type"] == "text/event-stream"
    raw = b"".join(response.streaming_content)
    lines = raw.decode().splitlines()
    events = [json.loads(line[6:]) for line in lines if line.startswith("data: ")]
    result = events[-1]["result"]
    assert not result["isError"], text(result)
    assert max(len(line) for line in raw.splitlines()) < 1024 * 1024
    original = Attachment.objects.get(original_name="rauschen.png")
    assert original.size > protocol.SSE_INLINE_BYTES
    (image,) = blocks(result, "image")
    assert len(image["data"]) <= protocol.SSE_INLINE_BYTES
    assert "verkleinert" in text(result) and f"attachment_id {original.pk}" in text(result)
    # Dasselbe als JSON-Antwort: genug Platz, das Bild kommt unverändert.
    _, plain = call(client, secret, "run_python", {"code": NOISE})
    (full,) = blocks(plain, "image")
    assert full["mimeType"] == "image/png" and "verkleinert" not in text(plain)


# --- Größengrenzen und Inhaltstypen (protocol.call_tool_result) ----------------------------


def _item(mime, data, *, preview=True, pk=7, name="datei"):
    return FileItem(
        uri=f"multigpt://attachment/{pk}",
        mime_type=mime,
        data=data,
        name=name,
        preview=preview,
        attachment_id=pk if preview else None,
    )


def test_large_pdf_only_as_resource_link():
    big = b"%PDF-1.4\n" + b"0" * (protocol.RESOURCE_INLINE_BYTES + 10)
    result = protocol.call_tool_result(
        ToolOutput("PDF erzeugt.", {"chat_id": 1}, files=[_item("application/pdf", big)])
    )
    (link,) = blocks(result, "resource_link")
    assert link["uri"] == "multigpt://attachment/7" and link["mimeType"] == "application/pdf"
    assert link["size"] == len(big)
    assert "get_file (attachment_id 7)" in text(result)
    assert not blocks(result, "resource")


def test_get_file_over_sse_budget_points_to_streamable_http():
    data = b"x" * protocol.SSE_INLINE_BYTES
    output = ToolOutput(
        "Datei", {"kind": "document"}, files=[_item("text/plain", data, preview=False)]
    )
    over_sse = protocol.call_tool_result(output, protocol.SSE_INLINE_BYTES)
    assert blocks(over_sse, "resource_link") and not blocks(over_sse, "resource")
    assert "Streamable HTTP" in text(over_sse)
    over_json = protocol.call_tool_result(output)
    assert base64.b64decode(blocks(over_json, "resource")[0]["resource"]["blob"]) == data


def test_svg_resource_and_budget_shared_between_files():
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"/>'
    png = base64.b64decode(_png())
    alone = protocol.call_tool_result(ToolOutput("ok", files=[_item("image/svg+xml", svg, pk=1)]))
    (resource,) = blocks(alone, "resource")
    assert resource["resource"] == {
        "uri": "multigpt://attachment/1",
        "mimeType": "image/svg+xml",
        "text": svg.decode(),
    }
    # Mit PNG daneben reicht das Bild, das SVG kommt nur als Verweis.
    files = [_item("image/svg+xml", svg, pk=1), _item("image/png", png, pk=2)]
    result = protocol.call_tool_result(ToolOutput("ok", {"a": 1}, files=files))
    assert blocks(result, "image")[0]["data"] == _png()
    (link,) = blocks(result, "resource_link")
    assert link["uri"] == "multigpt://attachment/1" and not blocks(result, "resource")
    # Ohne Platz: beide nur als Verweis.
    tiny = protocol.call_tool_result(ToolOutput("ok", files=files), 40)
    assert len(blocks(tiny, "resource_link")) == 2


def test_text_goes_into_structured_content_without_files():
    """Ohne Dateien bleibt structuredContent – mit dem Text, damit Clients, die nur
    structuredContent zeigen, die Ausgabe sehen (ask, run_python ohne Bild)."""
    result = protocol.call_tool_result(ToolOutput("Exit-Status 0\n42", {"chat_id": 3}))
    assert result["structuredContent"] == {"chat_id": 3, "text": "Exit-Status 0\n42"}
    from multigpt.node.tools import json_output

    plain = protocol.call_tool_result(json_output({"n": 1}, "Eins."))
    assert plain["structuredContent"] == {"n": 1}
