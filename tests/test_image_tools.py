"""Klassische Bildbearbeitung ``transform_image`` (M9-03, Tests M9-05).

Jede Operation, Zugriff nur auf lesbare Bild-Anhänge, das Original bleibt erhalten, Grenzen,
Aufruf als eingebautes Werkzeug und über den MCP-Knoten.
"""

import io

import pytest
from django.core.files.base import ContentFile
from PIL import Image

from multigpt.chat import image_tools, tooling
from multigpt.chat.models import Attachment, Conversation
from multigpt.chat.services import append_message
from multigpt.node import scopes as S
from tests.node_support import call, make_key, make_user, structured, tool_names

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _media(settings, tmp_path):
    settings.MEDIA_ROOT = str(tmp_path / "media")


def png(size=(200, 100), color=(200, 30, 30), mode="RGB") -> bytes:
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, "PNG")
    return out.getvalue()


def attach(message, data: bytes, name="foto.png", mime="image/png") -> Attachment:
    image = Image.open(io.BytesIO(data))
    attachment = Attachment(
        message=message,
        conversation=message.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type=mime,
        size=len(data),
        width=image.width,
        height=image.height,
        original_name=name,
    )
    attachment.file.save(name, ContentFile(data), save=True)
    return attachment


@pytest.fixture
def chat():
    user = make_user("anna")
    conversation = Conversation.objects.create(user=user, title="Bilder")
    question = append_message(conversation, role="user", content="Bild", author=user)
    answer = append_message(conversation, role="assistant", author=user)
    return user, question, answer


class Sources:
    def __init__(self, message):
        self.message = message
        self.changed = False


def run(user, answer, arguments):
    return tooling.get_builtin("transform_image").run(user, arguments, Sources(answer))


def opened(attachment) -> Image.Image:
    with attachment.file.open("rb") as handle:
        return Image.open(io.BytesIO(handle.read())).copy()


def newest(answer) -> Attachment:
    return Attachment.objects.filter(message=answer).order_by("-pk").first()


# --- Operationen einzeln --------------------------------------------------------------------


def img(size=(200, 100), color=(200, 30, 30), mode="RGB"):
    return Image.new(mode, size, color)


def test_crop_by_box_and_aspect():
    assert image_tools.crop(img(), {"left": 10, "top": 5, "right": 60, "bottom": 55}).size == (
        50,
        50,
    )
    assert image_tools.crop(img(), {"aspect": "1:1"}).size == (100, 100)
    assert image_tools.crop(img(), {"aspect": "16:9", "anchor": "top"}).size == (178, 100)
    with pytest.raises(image_tools.StepError):
        image_tools.crop(img(), {"left": 50, "right": 40})
    with pytest.raises(image_tools.StepError):
        image_tools.crop(img(), {"aspect": "0:1"})


def test_resize_variants_and_limits():
    assert image_tools.resize(img(), {"width": 100}).size == (100, 50)
    assert image_tools.resize(img(), {"height": 25}).size == (50, 25)
    assert image_tools.resize(img(), {"width": 50, "height": 50}).size == (50, 25)  # contain
    assert image_tools.resize(img(), {"width": 50, "height": 50, "fit": "cover"}).size == (50, 50)
    assert image_tools.resize(img(), {"width": 30, "height": 70, "fit": "stretch"}).size == (30, 70)
    for bad in ({}, {"width": 0}, {"width": "100"}, {"width": True}, {"width": 99999}):
        with pytest.raises(image_tools.StepError):
            image_tools.resize(img(), bad)


def test_rotate_flip_and_grayscale():
    assert image_tools.rotate(img(), {"degrees": 90}).size == (100, 200)
    assert image_tools.rotate(img(), {"degrees": -90}).size == (100, 200)
    tilted = image_tools.rotate(img(), {"degrees": 15})
    assert tilted.mode == "RGBA" and tilted.width > 200  # Ecken transparent
    filled = image_tools.rotate(img(), {"degrees": 15, "background": "white"})
    assert filled.mode == "RGB" and filled.getpixel((0, 0)) == (255, 255, 255)
    half = Image.new("RGB", (2, 1))
    half.putpixel((0, 0), (255, 0, 0))
    assert image_tools.flip(half, {"direction": "horizontal"}).getpixel((1, 0)) == (255, 0, 0)
    assert image_tools.grayscale(img(), {}).mode == "L"
    with pytest.raises(image_tools.StepError):
        image_tools.rotate(img(), {})
    with pytest.raises(image_tools.StepError):
        image_tools.flip(img(), {"direction": "diagonal"})


def test_pad_background_and_colors():
    padded = image_tools.pad(img(), {"aspect": "1:1", "color": "#00ff00"})
    assert padded.size == (200, 200) and padded.getpixel((0, 0)) == (0, 255, 0)
    assert image_tools.pad(img(), {"margin": 10}).size == (220, 120)
    assert image_tools.pad(img(), {"width": 400, "height": 400}).size == (400, 400)
    clear = img(mode="RGBA", color=(0, 0, 0, 0))
    flat = image_tools.background(clear, {"color": "white"})
    assert flat.mode == "RGB" and flat.getpixel((5, 5)) == (255, 255, 255)
    with pytest.raises(image_tools.StepError):
        image_tools.pad(img(), {"color": "keine-farbe", "margin": 2})
    with pytest.raises(image_tools.StepError):
        image_tools.pad(img(), {})


def test_text_changes_pixels_where_asked():
    base = img((300, 200), (0, 0, 0))
    out = image_tools.text(base, {"text": "Hallo Ü", "position": "top", "color": "white"})
    assert out.size == base.size and out.mode == "RGB"
    top = out.crop((0, 0, 300, 60)).convert("L").getextrema()[1]
    bottom = out.crop((0, 150, 300, 200)).convert("L").getextrema()[1]
    assert top > 200 and bottom == 0
    for bad in ({"text": ""}, {"text": "x" * 400}, {"text": "a", "position": "irgendwo"}):
        with pytest.raises(image_tools.StepError):
            image_tools.text(base, bad)


def test_collage_grid():
    out = image_tools.collage([img(), img((50, 100)), img((80, 80))], {"cell": 100, "gap": 10})
    assert out.size == (2 * 100 + 3 * 10, 2 * 100 + 3 * 10)
    with pytest.raises(image_tools.StepError):
        image_tools.collage([img()], {})


def test_apply_steps_numbering_and_limits():
    out = image_tools.apply_steps(
        [img()], [{"op": "crop", "aspect": "1:1"}, {"op": "resize", "width": 64}]
    )
    assert out.size == (64, 64)
    with pytest.raises(image_tools.StepError, match="Schritt 2"):
        image_tools.apply_steps([img()], [{"op": "grayscale"}, {"op": "zaubern"}])
    with pytest.raises(image_tools.StepError):
        image_tools.apply_steps([img()], [])
    with pytest.raises(image_tools.StepError):
        image_tools.apply_steps([img()], [{"op": "grayscale"}] * (image_tools.MAX_STEPS + 1))
    with pytest.raises(image_tools.StepError, match="Pixel"):
        image_tools.apply_steps(
            [img()],
            [
                {"op": "resize", "width": 4000, "height": 4000, "fit": "stretch"},
                {"op": "pad", "margin": 200},
            ],
        )


# --- Werkzeug im Chat -----------------------------------------------------------------------


def test_tool_creates_new_attachment_and_keeps_original(chat):
    user, question, answer = chat
    original = attach(question, png())
    before = original.file.read()
    original.file.close()
    result = run(
        user,
        answer,
        {
            "steps": [{"op": "crop", "aspect": "1:1"}, {"op": "resize", "width": 64}],
            "format": "webp",
        },
    )
    assert not result.is_error, result.text
    made = newest(answer)
    assert made.pk != original.pk and made.source_image_id == original.pk
    assert (made.width, made.height) == (64, 64) and made.mime_type == "image/webp"
    assert made.thumbnail and made.original_name == "foto-bearbeitet.webp"
    assert f"#{made.pk}" in result.text and f"#{original.pk}" in result.text
    original.refresh_from_db()
    assert original.file.read() == before  # Original unverändert


def test_tool_uses_named_attachments_and_collage(chat):
    user, question, answer = chat
    first = attach(question, png((100, 100), (255, 0, 0)), "a.png")
    second = attach(question, png((100, 100), (0, 0, 255)), "b.png")
    result = run(
        user,
        answer,
        {"attachment_ids": [first.pk, second.pk], "steps": [{"op": "collage", "columns": 2}]},
    )
    assert not result.is_error, result.text
    made = opened(newest(answer))
    assert made.width > made.height


def test_tool_keeps_transparency_as_png(chat):
    user, question, answer = chat
    attach(question, png(mode="RGBA", color=(0, 0, 0, 0)))
    assert not run(user, answer, {"steps": [{"op": "flip"}]}).is_error
    assert newest(answer).mime_type == "image/png"


def test_tool_refuses_foreign_and_non_images(chat):
    user, question, answer = chat
    other = make_user("bert")
    foreign_chat = Conversation.objects.create(user=other)
    foreign_msg = append_message(foreign_chat, role="user", content="x", author=other)
    foreign = attach(foreign_msg, png())
    result = run(user, answer, {"attachment_ids": [foreign.pk], "steps": [{"op": "grayscale"}]})
    assert result.is_error and f"#{foreign.pk}" in result.text
    svg = Attachment(
        message=question,
        conversation=question.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type="image/svg+xml",
    )
    svg.file.save("x.svg", ContentFile(b"<svg xmlns='http://www.w3.org/2000/svg'/>"), save=True)
    result = run(user, answer, {"attachment_ids": [svg.pk], "steps": [{"op": "grayscale"}]})
    assert result.is_error and "SVG" in result.text
    assert not Attachment.objects.filter(message=answer).exists()


def test_tool_without_image_and_bad_steps(chat):
    user, _, answer = chat
    result = run(user, answer, {"steps": [{"op": "grayscale"}]})
    assert result.is_error and "Kein Bild" in result.text
    result = run(user, answer, {"steps": "drehen"})
    assert result.is_error


def test_tool_limits_outputs_per_answer(chat, monkeypatch):
    user, question, answer = chat
    attach(question, png())
    monkeypatch.setattr(image_tools, "MAX_OUTPUTS_PER_ANSWER", 1)
    assert not run(user, answer, {"steps": [{"op": "grayscale"}]}).is_error
    result = run(user, answer, {"steps": [{"op": "grayscale"}]})
    assert result.is_error


def test_tool_is_offered_to_tool_models(chat):
    user, _, _ = chat

    class Model:
        supports_tools = True

    assert "transform_image" in tooling.builtin_bindings(user, Model())


# --- Über den MCP-Knoten ----------------------------------------------------------------------


def test_node_transform_image_returns_the_image(client, chat):
    user, question, _ = chat
    original = attach(question, png((120, 60)))
    secret = make_key(user, [S.TOOLS_RUN, S.FILES_READ])[1]
    assert "transform_image" in tool_names(client, secret)
    response, result = call(
        client,
        secret,
        "transform_image",
        {"attachment_ids": [original.pk], "steps": [{"op": "rotate", "degrees": 90}]},
    )
    assert response.status_code == 200 and not result.get("isError"), result
    assert any(c.get("type") == "image" for c in result["content"])
    data = structured(result)
    made = Attachment.objects.get(pk=data["attachments"][0]["attachment_id"])
    assert (made.width, made.height) == (60, 120) and made.source_image_id == original.pk
