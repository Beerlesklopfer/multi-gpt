"""Werkzeug create_pdf: Blätter zum Ausdrucken (documents_pdf, documents_sheets).

Gerendert wird echt mit WeasyPrint im Kindprozess (``pdf_render``); ohne
WeasyPrint bzw. Pango werden diese Tests übersprungen. Geometrie (Lineaturen)
wird über ein mit pdftoppm gerendertes PNG nachgemessen (10 Pixel je mm).
"""

import http.server
import io
import json
import re
import shutil
import subprocess
import threading

import httpx
import pytest
import respx
from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.urls import reverse
from PIL import Image
from pypdf import PdfReader

from multigpt.accounts.models import Role
from multigpt.accounts.permissions import Action, can
from multigpt.chat import (
    documents_pdf,
    documents_sheets,
    images,
    sandbox,
    tooling,
    tools_python,
)
from multigpt.chat.models import AIModel, Attachment, Conversation, Message, Provider, ToolCall
from multigpt.chat.services import append_message

PASSWORD = "Geheim-Test-1234"
BASE = "https://llm.example.invalid/v1"
PX_PER_MM = 10  # pdftoppm -r 254
MM_PT = 72 / 25.4

WEASY = documents_pdf.weasyprint_installed()
needs_weasy = pytest.mark.skipif(not WEASY, reason="WeasyPrint bzw. Pango fehlt")
needs_poppler = pytest.mark.skipif(not shutil.which("pdftoppm"), reason="pdftoppm fehlt")


def _sandbox_works() -> bool:
    try:
        return sandbox.bwrap_path() is not None and sandbox.status(refresh=True).available
    except Exception:  # noqa: BLE001
        return False


SANDBOX = _sandbox_works()


@pytest.fixture(autouse=True)
def _no_pdf_tool():
    """Überschreibt die Fixture aus conftest: Hier wird wirklich gerendert."""


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path / "media"


def make_user(role_key, username):
    return get_user_model().objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key)
    )


class Collector:
    def __init__(self, message):
        self.message = message
        self.changed = False


def pdf(args, user=None, message=None):
    job = documents_pdf.build(user, message, args)
    data, pages = documents_pdf.render(job.html)
    return job, data, pages


def text_of(data: bytes) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(data)).pages]


def size_mm(data: bytes, page: int = 0) -> tuple[float, float]:
    box = PdfReader(io.BytesIO(data)).pages[page].mediabox
    return round(float(box.width) / MM_PT, 1), round(float(box.height) / MM_PT, 1)


def png(data: bytes, tmp_path, page: int = 1) -> Image.Image:
    source = tmp_path / "blatt.pdf"
    source.write_bytes(data)
    subprocess.run(
        [
            "pdftoppm",
            "-r",
            "254",
            "-png",
            "-f",
            str(page),
            "-l",
            str(page),
            str(source),
            str(tmp_path / "seite"),
        ],
        check=True,
    )
    return Image.open(next(tmp_path.glob("seite*.png"))).convert("RGB")


def dark_lines(image: Image.Image, x_mm: float) -> list[float]:
    """Mitten dunkler Linien (in mm) in der Pixelspalte ``x_mm``."""
    gray = image.convert("L")
    x = int(x_mm * PX_PER_MM)
    runs, start = [], None
    for y in range(gray.height):
        dark = gray.getpixel((x, y)) < 160
        if dark and start is None:
            start = y
        elif not dark and start is not None:
            runs.append((start + y - 1) / 2 / PX_PER_MM)
            start = None
    return runs


# --- Markdown -> HTML (ohne Rendern) ----------------------------------------------------


def md(content, **kwargs):
    opts = documents_pdf.MarkdownOptions(content_width=174, **kwargs)
    return documents_pdf.markdown_html(content, opts)


def test_gap_becomes_writing_line():
    html = md("Die ___ nimmt Wasser auf, die ________ trägt Blätter.")
    assert html.count('class="gap"') == 2 and "___" not in html


def test_checkboxes():
    html = md("- [ ] Blüte\n- [x] Wurzel\n\nJa [ ] Nein [ ]")
    assert html.count('<li class="task">') == 2
    assert html.count('class="check"') == 4 and "✗" in html


def test_widgets_lines_and_box():
    html = md("[linien:3 lineatur:1]\n\n[kasten:40 Zeichne eine Blume]\n\nText [linien:2] bleibt")
    assert html.count("<svg") == 1 and 'class="lines"' in html
    assert 'style="height:40mm"' in html and "Zeichne eine Blume" in html
    assert "[linien:2] bleibt" in html  # nur als eigener Absatz ein Baustein


def test_raw_html_and_links_are_text():
    html = md(
        '<script>alert(1)</script> <img src="http://x.invalid/a.png">\n\n[hier](https://example.org/a)'
    )
    assert "<script" not in html and "&lt;script&gt;" in html
    assert "<img" not in html and "href" not in html
    assert "(https://example.org/a)" in html


def test_images_only_from_answer_attachments():
    html = md(
        "![Foto](http://x.invalid/a.png) ![Plan](anhang:7) ![](file:///etc/passwd)",
        images={7: "data:image/png;base64,AAAA"},
    )
    assert html.count("<img") == 1 and 'src="data:image/png;base64,AAAA"' in html
    assert "[Bild: Foto]" in html and "http://x.invalid" not in html


def test_formulas_without_katex():
    assert documents_pdf.tex_to_text(r"a^2 + b^2 = c^2") == "a² + b² = c²"
    assert documents_pdf.tex_to_text(r"\frac{1}{2} \cdot 4 \le x_1") == "1/2 · 4 ≤ x₁"
    assert 'class="math"' in md("Es gilt $x^2$.")


# --- Rendern ----------------------------------------------------------------------------


@needs_weasy
def test_text_pdf_title_table_umlauts_page_numbers():
    rows = "\n".join(f"| Zeile {i} | Größe {i} cm |" for i in range(90))
    job, data, pages = pdf(
        {
            "layout": "text",
            "title": "Übersicht für Jürgen",
            "content": f"# Maße\n\n| Name | Größe |\n|---|---|\n{rows}",
        }
    )
    texts = text_of(data)
    assert pages == len(texts) >= 2
    assert "Übersicht für Jürgen" in texts[0] and "Größe 0 cm" in texts[0]
    assert f"Seite 1 von {pages}" in texts[0] and f"Seite 2 von {pages}" in texts[1]
    assert "Name" in texts[1]  # Tabellenkopf auf Seite 2 wiederholt
    assert job.filename == "ubersicht-fur-jurgen.pdf"
    assert PdfReader(io.BytesIO(data)).metadata.title == "Übersicht für Jürgen"
    assert size_mm(data) == (210.0, 297.0)


@needs_weasy
def test_a4_landscape():
    _, data, _ = pdf({"layout": "text", "content": "Quer", "orientation": "landscape"})
    assert size_mm(data) == (297.0, 210.0)


class _Listener(http.server.BaseHTTPRequestHandler):
    hits: list[str] = []

    def do_GET(self):  # noqa: N802
        _Listener.hits.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.end_headers()

    def log_message(self, *args):
        pass


@needs_weasy
def test_no_network_or_file_access(tmp_path):
    _Listener.hits = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Listener)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    secret = tmp_path / "geheim.txt"
    secret.write_text("STRENG-GEHEIM")
    try:
        content = (
            f'<img src="{url}/roh.png"> ![Bild]({url}/md.png) ![Datei](file://{secret}) '
            f"[Link]({url}/link) ![](file:///etc/passwd)"
        )
        _, data, _ = pdf({"layout": "text", "content": content})
        assert "STRENG-GEHEIM" not in "".join(text_of(data))
        # Auch HTML am Markdown vorbei: Der Fetcher im Kindprozess lädt nichts.
        html = (
            f'<html><head><link rel="stylesheet" href="{url}/a.css"><style>'
            f'@import url("{url}/b.css"); body {{ background: url("{url}/c.png") }}'
            f'@font-face {{ font-family: X; src: url("{url}/d.ttf") }}</style></head><body>'
            f'<img src="{url}/e.png"><img src="file://{secret}"><object data="file://{secret}">'
            f'</object><iframe src="{url}/f"></iframe><svg><image href="{url}/g.png"/></svg>'
            "<p>x</p></body></html>"
        )
        data, _ = documents_pdf.render(html)
        assert "STRENG-GEHEIM" not in "".join(text_of(data))
    finally:
        server.shutdown()
    assert _Listener.hits == []


@needs_weasy
def test_limits(monkeypatch):
    with pytest.raises(documents_pdf.DocumentError, match="zu lang"):
        documents_pdf.build(None, None, {"layout": "text", "content": "x" * 210_000})
    with pytest.raises(documents_pdf.DocumentError, match="Inhalt"):
        documents_pdf.build(None, None, {"layout": "text", "content": "  "})
    long_html = documents_pdf.build(
        None, None, {"layout": "text", "content": "\n\n".join(["Absatz"] * 400)}
    ).html
    with pytest.raises(
        documents_pdf.DocumentError, match="hätte .* Seiten, erlaubt sind höchstens 2"
    ):
        documents_pdf.render(long_html, max_pages=2)
    monkeypatch.setattr(documents_pdf, "RENDER_TIMEOUT", 0.01)
    with pytest.raises(documents_pdf.DocumentError, match="Abgebrochen"):
        documents_pdf.render(long_html)


def test_busy():
    for _ in range(documents_pdf.MAX_PARALLEL):
        documents_pdf._SLOTS.acquire()
    try:
        with pytest.raises(documents_pdf.DocumentError, match="zu viele"):
            documents_pdf.render("<p>x</p>")
    finally:
        for _ in range(documents_pdf.MAX_PARALLEL):
            documents_pdf._SLOTS.release()


# --- Lineaturen ---------------------------------------------------------------------------


def _systems(lines: list[float], per_system: int) -> list[list[float]]:
    return [lines[i : i + per_system] for i in range(0, len(lines), per_system)]


@needs_weasy
@needs_poppler
def test_lineatur_0_geometry_and_houses(tmp_path):
    job, data, pages = pdf({"layout": "lineatur", "lineatur": "0", "contrast": False})
    assert pages == 1 and job.filename == "lineatur-0.pdf"
    assert size_mm(data) == (210.0, 148.0)  # A5 quer
    image = png(data, tmp_path)
    lines = dark_lines(image, 105)
    assert len(lines) == 20  # 5 Zeilen à 4 Linien
    for system in _systems(lines, 4):
        bands = [b - a for a, b in zip(system, system[1:], strict=False)]
        assert all(abs(band - 6.0) <= 0.15 for band in bands), bands
    gaps = [b[0] - a[-1] for a, b in zip(_systems(lines, 4), _systems(lines, 4)[1:], strict=False)]
    assert all(abs(gap - 8.0) <= 0.15 for gap in gaps), gaps
    # Häuschen links und rechts: Dach (rot) im Oberband, Keller (braun) im Unterband.
    top = lines[0]
    for x_mm in (8 + 3, 210 - 8 - 3):
        roof = image.getpixel((int(x_mm * PX_PER_MM), int((top + 5) * PX_PER_MM)))
        cellar = image.getpixel((int(x_mm * PX_PER_MM), int((top + 15) * PX_PER_MM)))
        assert roof == pytest.approx((0xE8, 0xA3, 0x9B), abs=12), roof
        assert cellar == pytest.approx((0xC9, 0xB4, 0x9A), abs=12), cellar


@needs_weasy
@needs_poppler
def test_lineatur_without_houses_and_baseline_bold(tmp_path):
    _, data, _ = pdf(
        {"layout": "lineatur", "lineatur": "0", "house_symbols": False, "contrast": False}
    )
    image = png(data, tmp_path)
    top = dark_lines(image, 105)[0]
    assert image.getpixel((110, int((top + 5) * PX_PER_MM))) == (255, 255, 255)
    # Grundlinie (3. Linie) dicker als die Hilfslinien.
    gray = image.convert("L")
    x = 1050

    def thickness(y_mm):
        y = int(y_mm * PX_PER_MM)
        return sum(1 for dy in range(-8, 9) if gray.getpixel((x, y + dy)) < 160)

    assert thickness(top + 12) > thickness(top + 6) + 2


@needs_weasy
@needs_poppler
def test_lineatur_1_a4_and_sample_words(tmp_path):
    job, data, _ = pdf(
        {
            "layout": "lineatur",
            "lineatur": "1",
            "sample_words": "Oma Uhu",
            "name_field": True,
            "background": "hellgruen",
        }
    )
    assert size_mm(data) == (210.0, 297.0)
    assert "Oma Uhu" in text_of(data)[0] and "Name:" in text_of(data)[0]
    assert "Drucken mit 100 %" in text_of(data)[0]
    image = png(data, tmp_path)
    assert image.getpixel((50, 50)) == pytest.approx((0xEE, 0xF6, 0xD0), abs=3)
    systems = _systems(dark_lines(image, 80), 4)
    assert len(systems) >= 12 and all(len(s) == 4 for s in systems)
    for system in systems:
        assert all(abs(b - a - 5.0) <= 0.15 for a, b in zip(system, system[1:], strict=False))


@needs_weasy
@needs_poppler
@pytest.mark.parametrize("key,spacing", [("karo5", 5.0), ("liniert", 9.0), ("4", 10.0)])
def test_lineatur_single_lines_and_grid(tmp_path, key, spacing):
    _, data, _ = pdf({"layout": "lineatur", "lineatur": key})
    lines = dark_lines(png(data, tmp_path), 101.3)
    steps = [b - a for a, b in zip(lines, lines[1:], strict=False)]
    assert len(steps) > 10 and all(abs(s - spacing) <= 0.15 for s in steps), steps


@needs_weasy
def test_lineatur_rows_clamped_with_note():
    job, _, _ = pdf({"layout": "lineatur", "lineatur": "0", "rows": 12})
    assert "Es passen nur 5 Zeilen" in job.note


def test_unknown_lineatur():
    with pytest.raises(documents_pdf.DocumentError, match="Unbekannte Lineatur"):
        documents_pdf.build(None, None, {"layout": "lineatur", "lineatur": "99"})


# --- Rechenaufgaben -----------------------------------------------------------------------


def test_tasks_deterministic_by_seed():
    one = documents_sheets.make_tasks(["+", "-"], 0, 20, 20, seed=42)
    two = documents_sheets.make_tasks(["+", "-"], 0, 20, 20, seed=42)
    other = documents_sheets.make_tasks(["+", "-"], 0, 20, 20, seed=43)
    assert one == two and one != other and len(one) == 20


@pytest.mark.parametrize(
    "ops,lo,hi",
    [
        (["+", "-"], 0, 20),
        (["+"], 10, 100),
        (["-"], 0, 1000),
        (["*", ":"], 0, 100),
        (["*"], 0, 50),
        ([":"], 20, 400),
    ],
)
@pytest.mark.parametrize("blank", ["result", "mixed"])
def test_tasks_correct_and_within_range(ops, lo, hi, blank):
    for seed in range(5):
        tasks = documents_sheets.make_tasks(ops, lo, hi, 30, seed=seed, blank=blank)
        assert len(tasks) == 30
        for task in tasks:
            assert task.check(), task
            assert task.op in ops
            if task.op in "+-":
                assert all(lo <= v <= hi for v in (task.a, task.b, task.result)), task
            elif task.op == "*":
                assert lo <= task.result <= hi
            else:
                assert lo <= task.a <= hi and task.b >= 1
            assert task.blank in ("result", "a", "b")
            if blank == "result":
                assert task.parts()[2] == "" and task.parts(solved=True)[2] == str(task.result)


def test_times_table_and_clock_times():
    tasks = documents_sheets.times_table_tasks(
        [3, 7], mixed=False, with_division=True, count=None, seed=1
    )
    assert len(tasks) == 40 and all(t.check() for t in tasks)
    assert {t.b for t in tasks} == {3, 7}
    times = documents_sheets.make_times("viertel", 12, seed=3)
    assert len(times) == 12 and all(m in (0, 15, 30, 45) and 1 <= h <= 12 for h, m in times)
    assert times == documents_sheets.make_times("viertel", 12, seed=3)


def test_invalid_range():
    with pytest.raises(documents_pdf.DocumentError, match="Zahlenraum"):
        documents_pdf.build(None, None, {"layout": "aufgaben", "range": [20, 5]})


@needs_weasy
def test_worksheet_solutions_match_tasks():
    args = {
        "layout": "aufgaben",
        "operations": ["+", "-"],
        "range": [0, 20],
        "count": 12,
        "seed": 9,
        "columns": 1,
    }
    job, data, pages = pdf(args)
    assert pages == 2 and "Startwert (seed) 9" in job.note
    tasks = documents_sheets.make_tasks(["+", "-"], 0, 20, 12, seed=9)
    # Eine Aufgabe je Zeile (Zeichen können als eigene Textstücke extrahiert werden).
    solution = re.sub(r"\n(?=[-+=])", "", text_of(data)[1].replace("−", "-"))
    for task in tasks:
        sign = "+" if task.op == "+" else "-"
        assert f"){task.a}{sign}{task.b}={task.result}\n" in solution.replace(" ", "") + "\n"


@needs_weasy
@pytest.mark.parametrize(
    "args,size,pages",
    [
        ({"layout": "text", "content": "# Brief\n\nHallo"}, (210.0, 297.0), 1),
        (
            {
                "layout": "arbeitsblatt",
                "grade": 1,
                "content": "## Aufgabe\n\n[linien:2]",
                "page_size": "A5",
            },
            (148.0, 210.0),
            1,
        ),
        ({"layout": "lineatur", "lineatur": "2", "pages": 3}, (210.0, 297.0), 3),
        ({"layout": "aufgaben", "grade": 1, "count": 20}, (210.0, 297.0), 2),
        ({"layout": "rechenkaestchen", "count": 20, "with_solutions": False}, (210.0, 297.0), 1),
        ({"layout": "einmaleins", "reihen": [2, 5]}, (210.0, 297.0), 2),
        (
            {
                "layout": "zahlenstrahl",
                "range": [0, 100],
                "step": 10,
                "page_size": "A4",
                "orientation": "landscape",
            },
            (297.0, 210.0),
            2,
        ),
        ({"layout": "uhr", "precision": "viertel", "mode": "einzeichnen"}, (210.0, 297.0), 2),
        ({"layout": "lineatur", "lineatur": "karo10", "page_size": "Letter"}, (215.9, 279.4), 1),
    ],
)
def test_every_layout_renders(args, size, pages):
    _, data, count = pdf(args)
    assert data.startswith(b"%PDF-") and count == pages
    assert all(size_mm(data, i) == size for i in range(count))


# --- Rechte, Angebot, Hinweis -------------------------------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize(
    "key,expected", [("admin", True), ("adult", True), ("teen", True), ("guest", False)]
)
def test_role_defaults_from_migration(key, expected):
    assert Role.objects.get(key=key).can_create_documents is expected
    user = make_user(key, f"u-{key}")
    assert can(user, Action.CREATE_DOCUMENTS) is expected


@pytest.fixture
def tool_model(db):
    provider = Provider.objects.create(name="LLM", kind=Provider.Kind.OPENAI_COMPAT, base_url=BASE)
    return AIModel.objects.create(
        provider=provider, model_id="gpt-test", display_name="G", supports_tools=True
    )


@pytest.mark.django_db
def test_offered_only_with_right_tools_and_weasyprint(tool_model, monkeypatch):
    adult, guest = make_user("adult", "erwachsen"), make_user("guest", "gast")
    monkeypatch.setattr(documents_pdf, "weasyprint_installed", lambda: True)
    assert "create_pdf" in tooling.builtin_bindings(adult, tool_model)
    assert "create_pdf" not in tooling.builtin_bindings(guest, tool_model)
    tool_model.supports_tools = False
    assert "create_pdf" not in tooling.builtin_bindings(adult, tool_model)
    tool_model.supports_tools = True
    monkeypatch.setattr(documents_pdf, "weasyprint_installed", lambda: False)
    assert "create_pdf" not in tooling.builtin_bindings(adult, tool_model)
    assert documents_pdf.system_hint({}) == ""
    assert "create_pdf" in documents_pdf.system_hint({"create_pdf": object()})


def test_descriptions_separate_pdf_and_image():
    assert "generate_image" in documents_pdf.DESCRIPTION
    assert "lineatur" in documents_pdf.DESCRIPTION and "Schreibheft" in documents_pdf.DESCRIPTION
    assert "blatt.pdf" in tools_python.DESCRIPTION and "create_pdf" not in tools_python.DESCRIPTION
    assert "create_pdf" not in images.TOOL_SPEC.description


@pytest.mark.django_db
def test_tool_run_rights_and_limit_per_answer(monkeypatch):
    guest = make_user("guest", "gast")
    conversation = Conversation.objects.create(user=guest)
    answer = append_message(conversation, role="assistant", author=guest)
    result = documents_pdf._tool_run(guest, {"layout": "text", "content": "x"}, Collector(answer))
    assert result.is_error and result.text == tooling.MSG_NOT_ALLOWED
    adult = make_user("adult", "erwachsen")
    conversation = Conversation.objects.create(user=adult)
    answer = append_message(conversation, role="assistant", author=adult)
    for _ in range(documents_pdf.MAX_PDFS_PER_ANSWER):
        documents_pdf.store_pdf(answer, b"%PDF-1.4\n", "x.pdf")
    result = documents_pdf._tool_run(adult, {"layout": "text", "content": "x"}, Collector(answer))
    assert result.is_error and "Höchstens" in result.text


# --- Werkzeugschleife und Auslieferung ----------------------------------------------------


def sse(*chunks):
    lines = [f"data: {json.dumps(chunk)}\n\n" for chunk in chunks]
    return "".join([*lines, "data: [DONE]\n\n"]).encode()


def tool_call_body(call_id, name, arguments):
    part = {
        "index": 0,
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": json.dumps(arguments)},
    }
    return sse(
        {"choices": [{"index": 0, "delta": {"tool_calls": [part]}, "finish_reason": None}]},
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
        {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 2}},
    )


def text_body(text):
    return sse(
        {"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 5}},
    )


def events_of(response):
    assert response.status_code == 200, response.content
    raw = b"".join(response.streaming_content).decode()
    events = []
    for block in raw.split("\n\n"):
        if block.strip():
            lines = dict(line.split(": ", 1) for line in block.split("\n"))
            events.append((lines["event"], json.loads(lines["data"])))
    return events


@needs_weasy
@pytest.mark.django_db
def test_tool_loop_creates_pdf_attachment(client, tool_model):
    user = make_user("adult", "erwachsen")
    arguments = {"layout": "lineatur", "lineatur": "0", "title": "Schreibblatt"}
    bodies = [tool_call_body("c1", "create_pdf", arguments), text_body("Hier ist dein Blatt.")]
    requests = []

    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200, content=bodies[len(requests) - 1], headers={"content-type": "text/event-stream"}
        )

    client.force_login(user)
    conversation = Conversation.objects.create(user=user)
    with respx.mock(assert_all_called=False) as mock:
        mock.post(f"{BASE}/chat/completions").mock(side_effect=reply)
        response = client.post(
            reverse("chat:api_messages", args=[conversation.pk]),
            json.dumps({"content": "Schreibblatt Lineatur 0 bitte", "model": tool_model.pk}),
            content_type="application/json",
        )
        events = events_of(response)
    offered = {t["function"]["name"] for t in requests[0]["tools"]}
    assert "create_pdf" in offered
    system = requests[0]["messages"][0]
    assert system["role"] == "system" and "create_pdf" in system["content"]
    result = [d for n, d in events if n == "tool_result"][0]
    assert result["status"] == "ok", result
    attachment = Attachment.objects.get()
    answer = Message.objects.get(role="assistant")
    assert attachment.message == answer and attachment.owner is None
    assert attachment.mime_type == "application/pdf" and attachment.tool_call is None
    expected = f"PDF erzeugt: schreibblatt.pdf, 1 Seite (Anhang #{attachment.pk})."
    tool_messages = [m for m in requests[1]["messages"] if m["role"] == "tool"]
    assert tool_messages[0]["content"] == expected
    assert ToolCall.objects.get().tool == "create_pdf"
    # Anzeige: Datei-Chip mit Ansehen und Download.
    page = client.get(reverse("chat:conversation_messages", args=[conversation.pk]))
    html = page.content.decode()
    assert "schreibblatt.pdf" in html and "Ansehen" in html and " download" in html
    # Auslieferung inline, ohne sandbox-CSP, nur mit Leserecht.
    served = client.get(reverse("chat:attachment", args=[attachment.pk]))
    assert served.status_code == 200 and served["Content-Type"] == "application/pdf"
    assert served["Content-Disposition"].startswith("inline")
    assert "Content-Security-Policy" not in served and served["X-Content-Type-Options"] == "nosniff"
    assert b"".join(served.streaming_content).startswith(b"%PDF-")
    client.force_login(make_user("adult", "fremd"))
    assert client.get(reverse("chat:attachment", args=[attachment.pk])).status_code == 404


@pytest.mark.django_db
def test_uploaded_pdf_stays_download(client):
    user = make_user("adult", "erwachsen")
    conversation = Conversation.objects.create(user=user)
    message = Message.objects.create(conversation=conversation, role="user", content="x")
    attachment = Attachment(
        message=message,
        owner=user,
        kind=Attachment.Kind.FILE,
        mime_type="application/pdf",
        original_name="upload.pdf",
    )
    attachment.file.save("u.pdf", ContentFile(b"%PDF-1.4\n"), save=True)
    client.force_login(user)
    served = client.get(reverse("chat:attachment", args=[attachment.pk]))
    assert served["Content-Disposition"].startswith("attachment")
    assert "sandbox" in served["Content-Security-Policy"]


@needs_weasy
@pytest.mark.django_db
def test_embeds_image_attachment_of_same_chat():
    user = make_user("adult", "erwachsen")
    conversation = Conversation.objects.create(user=user)
    answer = append_message(conversation, role="assistant", author=user)
    buffer = io.BytesIO()
    Image.new("RGB", (40, 30), (200, 30, 30)).save(buffer, "PNG")
    image = Attachment(
        message=answer,
        kind=Attachment.Kind.IMAGE,
        mime_type="image/png",
        size=len(buffer.getvalue()),
    )
    image.file.save("b.png", ContentFile(buffer.getvalue()), save=True)
    other = make_user("adult", "andere")
    foreign_answer = append_message(
        Conversation.objects.create(user=other), role="assistant", author=other
    )
    foreign = Attachment(
        message=foreign_answer,
        kind=Attachment.Kind.IMAGE,
        mime_type="image/png",
        size=len(buffer.getvalue()),
    )
    foreign.file.save("f.png", ContentFile(buffer.getvalue()), save=True)
    content = f"![Pflanze](anhang:{image.pk})\n\n![Fremd](anhang:{foreign.pk})"
    job = documents_pdf.build(user, answer, {"layout": "arbeitsblatt", "content": content})
    assert job.html.count("data:image/png;base64,") == 1 and "[Bild: Fremd]" in job.html
    data, _ = documents_pdf.render(job.html)
    assert PdfReader(io.BytesIO(data)).pages[0].images  # Bild wirklich eingebettet


# --- PDF aus run_python ---------------------------------------------------------------------


@pytest.mark.django_db
def test_pdf_from_python_becomes_attachment():
    user = make_user("adult", "erwachsen")
    conversation = Conversation.objects.create(user=user)
    answer = append_message(conversation, role="assistant", author=user)
    from pypdf import PdfWriter

    buffer = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(buffer)
    stored, rejected = tools_python.store_files(
        answer,
        [
            sandbox.OutputFile("blatt.pdf", buffer.getvalue()),
            sandbox.OutputFile("kaputt.pdf", b"<html>kein pdf</html>"),
        ],
    )
    assert len(stored) == 1 and stored[0].mime_type == "application/pdf"
    assert stored[0].original_name == "blatt.pdf" and stored[0].owner is None
    assert rejected == ["kaputt.pdf (kein gültiges PDF)"]
    assert '".pdf")' in sandbox.RUNNER  # Runner übernimmt PDF aus dem Arbeitsordner


@pytest.mark.skipif(not SANDBOX, reason="bubblewrap nicht nutzbar")
def test_real_sandbox_returns_pdf():
    code = (
        "import matplotlib.pyplot as plt\n"
        "fig = plt.figure(figsize=(148/25.4, 210/25.4))\n"
        "fig.add_artist(plt.Line2D([0.1, 0.9], [0.5, 0.5]))\n"
        "fig.savefig('blatt.pdf')\n"
    )
    result = sandbox.run(code, sandbox.Limits(cpu_seconds=30, wall_seconds=60))
    assert result.ok, result.stderr
    names = [f.name for f in result.files]
    assert names == ["blatt.pdf"] and result.files[0].data.startswith(b"%PDF-")


@pytest.mark.parametrize("layout", documents_pdf.LAYOUTS)
def test_garbage_arguments_do_not_crash(layout):
    args = {
        "layout": layout,
        "content": "x",
        "page_size": [1],
        "orientation": {},
        "operations": [[1], "+"],
        "reihen": ["x", 3],
        "range": "x",
        "precision": [1],
        "mode": [2],
        "rows": "viele",
        "count": "y",
        "grade": "eins",
        "background": "<script>",
        "lineatur": ["0"],
    }
    try:
        job = documents_pdf.build(None, None, args)
    except documents_pdf.DocumentError:
        return
    assert "<script>" not in job.html


def test_lineatur_0_on_a4_stays_landscape():
    job = documents_pdf.build(
        None, None, {"layout": "lineatur", "lineatur": "0", "page_size": "A4"}
    )
    assert "size: 297mm 210mm" in job.html
