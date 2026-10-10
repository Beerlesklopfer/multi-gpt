"""Formeln in Antworten (LaTeX mit KaTeX): eingebundene Dateien, Reihenfolge
der Skripte, sichere KaTeX-Optionen, Export mit rohen Formeln.

Das Erkennen und Setzen selbst (math.js) prüft der Browserlauf; Node gibt es
in der Entwicklungsumgebung nicht."""

import re
from pathlib import Path

import pytest
from django.conf import settings
from django.urls import reverse

from multigpt.accounts.models import Role, User
from multigpt.chat import services
from multigpt.chat.models import AIModel, Conversation, Provider

pytestmark = pytest.mark.django_db

STATIC = Path(settings.BASE_DIR) / "multigpt" / "chat" / "static" / "chat"
KATEX = STATIC / "vendor" / "katex"
FORMULA = "Impuls:\n\n\\[\n\\boxed{ \\mathbf p = m\\,\\mathbf v }\n\\]\n\nmit \\(m\\) und $x_1$."


@pytest.fixture
def ai_model():
    provider = Provider.objects.create(name="Testanbieter", kind=Provider.Kind.OPENAI_COMPAT)
    return AIModel.objects.create(provider=provider, model_id="m-1", display_name="Modell Eins")


@pytest.fixture
def anna_client(client):
    anna = User.objects.create_user(
        "anna", password="Geheim-Test-1234", role=Role.objects.get(key="adult")
    )
    client.force_login(anna)
    return client, anna


@pytest.mark.parametrize(
    "name",
    [
        "katex.min.js",
        "katex.min.css",
        "contrib/mhchem.min.js",
        "LICENSE",
        "fonts/KaTeX_Main-Regular.woff2",
    ],
)
def test_katex_files_present(name):
    path = KATEX / name
    assert path.is_file() and path.stat().st_size > 0


def test_katex_css_references_only_existing_woff2_fonts():
    css = (KATEX / "katex.min.css").read_text()
    urls = re.findall(r"url\(([^)]+)\)", css)
    assert urls
    for url in urls:
        assert url.endswith(".woff2"), url
        assert (KATEX / url).is_file(), url
    # Nur woff2 ausgeliefert (Größe).
    assert {p.suffix for p in (KATEX / "fonts").iterdir()} == {".woff2"}
    assert "| KaTeX" in (KATEX.parent / "README.md").read_text()


def test_math_js_uses_safe_katex_options():
    js = (STATIC / "math.js").read_text()
    for option in (
        'output: "htmlAndMathml"',
        "throwOnError: false",
        "trust: false",
        "maxSize:",
        "maxExpand:",
    ):
        assert option in js
    assert "trust: true" not in js
    assert "innerHTML" not in js


def test_markdown_js_protects_before_marked_and_typesets_after_sanitize():
    js = (STATIC / "markdown.js").read_text()
    assert js.index("math.protect(") < js.index("marked.parse(")
    assert js.index("DOMPurify.sanitize(html") < js.index("math?.restore(")
    assert js.index("math?.restore(") < js.index("math?.typeset(")


def test_chat_page_includes_katex_and_math_js(anna_client, ai_model):
    client, anna = anna_client
    conv = Conversation.objects.create(user=anna, title="Physik")
    services.append_message(conv, role="assistant", model=ai_model, content=FORMULA)
    html = client.get(reverse("chat:conversation", args=[conv.pk])).content.decode()
    for name in (
        "vendor/katex/katex.min.css",
        "vendor/katex/katex.min.js",
        "vendor/katex/contrib/mhchem.min.js",
        "math.js",
    ):
        assert f'"{settings.STATIC_URL}chat/{name}"' in html
    assert (
        html.index("katex.min.js")
        < html.index("mhchem.min.js")
        < html.index("chat/math.js")
        < html.index("chat/markdown.js")
    )
    # Rohtext der Antwort steht unverändert (escaped) im HTML, markdown.js setzt ihn.
    assert "\\boxed{ \\mathbf p = m\\,\\mathbf v }" in html


def test_export_keeps_formulas_raw(anna_client, ai_model):
    client, anna = anna_client
    conv = Conversation.objects.create(user=anna, title="Physik")
    services.append_message(conv, role="assistant", model=ai_model, content=FORMULA)
    body = client.get(reverse("chat:conversation_export", args=[conv.pk])).content.decode()
    assert FORMULA in body
