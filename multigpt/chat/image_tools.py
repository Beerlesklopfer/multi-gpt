"""Klassische Bildbearbeitung (M9-03): Werkzeug ``transform_image`` mit Pillow.

Zuschneiden, Skalieren, Drehen, Spiegeln, Rand bzw. Leinwand, Hintergrund füllen,
Graustufen, Text einsetzen, Collage und Format umwandeln – ohne Bildmodell, ohne Kosten,
für jedes werkzeugfähige Modell („mach das Bild quadratisch und 1024 Pixel breit“) und über
den MCP-Knoten (``node/tool_builtin.py``).

Abweichung vom ersten Plan (eigener stdio-MCP-Server ``mcp_imagetools`` mit Arbeitsordner je
Nutzer): umgesetzt als eingebautes Werkzeug wie ``run_python`` und ``create_pdf``. Es arbeitet
direkt auf Anhängen mit den Leserechten des Kontos; ein Arbeitsordner, das Kopieren hinein und
heraus und ein zusätzlicher Prozess entfallen.

Regeln:
- Eingaben sind Bild-Anhänge, die das Konto lesen darf (Chat mit Leserecht); ohne Angabe die
  Bilder der letzten Nutzerfrage, sonst das letzte erzeugte Bild im Zweig (wie ``edit_image``).
- Jede Bearbeitung erzeugt einen **neuen** Anhang mit Verweis auf das Ausgangsbild
  (``source_image``); das Original bleibt unverändert.
- Grenzen: höchstens ``MAX_STEPS`` Schritte, ``MAX_SOURCES`` Eingaben, Kanten bis
  ``images.MAX_SIDE``; Pixelbomben fängt ``attachments._open_image`` ab. Ergebnis ohne
  Metadaten, mit Vorschau (``attachments.process_image``).
"""

from __future__ import annotations

import io
import logging
import math
from pathlib import Path

from django.core.files.base import ContentFile
from django.db import transaction
from PIL import Image, ImageColor, ImageDraw, ImageFont, ImageOps

from multigpt.accounts.permissions import Action, can

from . import attachments as chat_attachments
from . import images, tooling
from .models import Attachment, Message
from .providers.base import ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "transform_image"
TOOL_LABEL = "Bildwerkzeuge"
MAX_STEPS = 12
MAX_SOURCES = 9
MAX_TEXT = 300
MAX_OUTPUTS_PER_ANSWER = 10
FORMATS = {"png": "PNG", "jpeg": "JPEG", "jpg": "JPEG", "webp": "WEBP"}
ANCHORS = ("center", "top", "bottom", "left", "right")
POSITIONS = (
    "top-left",
    "top",
    "top-right",
    "left",
    "center",
    "right",
    "bottom-left",
    "bottom",
    "bottom-right",
)
FONT_PATHS = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",  # Paket fonts-dejavu-core
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

MSG_NO_SOURCE = (
    "Kein Bild gefunden: Nenne die Nummer des Bildes (Anhang #…) in „attachment_ids“ oder "
    "hänge ein Bild an."
)
MSG_FOREIGN = "Auf Anhang #{pk} besteht kein Zugriff, oder es ist kein Bild."
MSG_SVG = "Anhang #{pk} ist eine Vektorgrafik (SVG); die Bildwerkzeuge bearbeiten nur Rasterbilder."
MSG_NO_STEPS = 'Keine Schritte angegeben („steps“, z. B. [{"op": "resize", "width": 1024}]).'
MSG_TOO_MANY_STEPS = f"Höchstens {MAX_STEPS} Schritte je Aufruf."
MSG_TOO_MANY_OUTPUTS = f"Höchstens {MAX_OUTPUTS_PER_ANSWER} bearbeitete Bilder je Antwort."
MSG_TOO_LARGE = f"Das Ergebnis wäre größer als {images.MAX_SIDE} Pixel an einer Kante."


class StepError(Exception):
    """Ungültiger Schritt; Text geht an das Modell."""


# --- Eingaben -----------------------------------------------------------------------------


def _readable(user, attachment: Attachment) -> bool:
    conversation = attachment.conversation or (
        attachment.message.conversation if attachment.message_id else None
    )
    return conversation is not None and can(user, Action.READ, conversation)


def resolve_sources(user, message, requested: list[int]) -> list[Attachment]:
    """Bilder für die Bearbeitung; Fehlertext als ``StepError``."""
    if not requested:
        found = images._edit_sources(message, []) if message is not None else []
        if not found:
            raise StepError(MSG_NO_SOURCE)
        return found
    rows = {
        a.pk: a
        for a in Attachment.objects.select_related("message__conversation", "conversation").filter(
            pk__in=requested[:MAX_SOURCES]
        )
    }
    out = []
    for pk in requested[:MAX_SOURCES]:
        attachment = rows.get(pk)
        if (
            attachment is None
            or attachment.kind != Attachment.Kind.IMAGE
            or not _readable(user, attachment)
        ):
            raise StepError(MSG_FOREIGN.format(pk=pk))
        if attachment.mime_type == "image/svg+xml":
            raise StepError(MSG_SVG.format(pk=pk))
        out.append(attachment)
    return out


def _load(attachment: Attachment) -> Image.Image:
    try:
        with attachment.file.open("rb") as handle:
            data = handle.read()
        return chat_attachments._open_image(io.BytesIO(data))
    except (OSError, ValueError, chat_attachments.UploadError) as exc:
        raise StepError(f"Anhang #{attachment.pk} lässt sich nicht öffnen.") from exc


# --- Schritte -----------------------------------------------------------------------------


def _int(step: dict, key: str, default=None, *, low: int = 0, high: int = images.MAX_SIDE):
    value = step.get(key, default)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise StepError(f"„{key}“ muss eine Zahl sein.")
    value = int(round(value))
    if not low <= value <= high:
        raise StepError(f"„{key}“ muss zwischen {low} und {high} liegen.")
    return value


def _color(value, default="white") -> tuple:
    try:
        return ImageColor.getrgb(str(value or default))
    except ValueError as exc:
        raise StepError(f"Unbekannte Farbe „{value}“ (z. B. white, black, #ff8800).") from exc


def _rgba(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGBA" else image.convert("RGBA")


def _check_size(width: int, height: int) -> None:
    if width < 1 or height < 1:
        raise StepError("Das Ergebnis hätte keine Pixel.")
    if max(width, height) > images.MAX_SIDE:
        raise StepError(MSG_TOO_LARGE)


def _aspect(value) -> float:
    text = str(value or "").replace("/", ":").strip()
    try:
        if ":" in text:
            a, b = (float(x) for x in text.split(":", 1))
        else:
            a, b = float(text), 1.0
    except ValueError as exc:
        raise StepError(f"Ungültiges Seitenverhältnis „{value}“ (z. B. 1:1, 16:9).") from exc
    if a <= 0 or b <= 0 or not 0.05 <= a / b <= 20:
        raise StepError(f"Ungültiges Seitenverhältnis „{value}“.")
    return a / b


def crop(image: Image.Image, step: dict) -> Image.Image:
    if "aspect" in step:
        ratio = _aspect(step["aspect"])
        width, height = image.size
        if width / height > ratio:
            new_w, new_h = max(1, round(height * ratio)), height
        else:
            new_w, new_h = width, max(1, round(width / ratio))
        anchor = step.get("anchor", "center")
        if anchor not in ANCHORS:
            raise StepError(f"„anchor“: eines von {', '.join(ANCHORS)}.")
        left = {"left": 0, "right": width - new_w}.get(anchor, (width - new_w) // 2)
        top = {"top": 0, "bottom": height - new_h}.get(anchor, (height - new_h) // 2)
        return image.crop((left, top, left + new_w, top + new_h))
    left = _int(step, "left", 0)
    top = _int(step, "top", 0)
    right = _int(step, "right", image.width)
    bottom = _int(step, "bottom", image.height)
    right, bottom = min(right, image.width), min(bottom, image.height)
    if right <= left or bottom <= top:
        raise StepError("Zuschnitt leer: „right“ > „left“ und „bottom“ > „top“ nötig.")
    return image.crop((left, top, right, bottom))


def resize(image: Image.Image, step: dict) -> Image.Image:
    width = _int(step, "width", low=1)
    height = _int(step, "height", low=1)
    if width is None and height is None:
        raise StepError("„resize“ braucht „width“ und/oder „height“.")
    fit = step.get("fit", "contain")
    if fit not in ("contain", "cover", "stretch"):
        raise StepError("„fit“: contain (einpassen), cover (füllen, zuschneiden) oder stretch.")
    ratio = image.width / image.height
    if width is None:
        width = max(1, round(height * ratio))
    elif height is None:
        height = max(1, round(width / ratio))
    elif fit == "contain":
        scale = min(width / image.width, height / image.height)
        width, height = max(1, round(image.width * scale)), max(1, round(image.height * scale))
    elif fit == "cover":
        _check_size(width, height)
        return ImageOps.fit(image, (width, height), Image.Resampling.LANCZOS)
    _check_size(width, height)
    return image.resize((width, height), Image.Resampling.LANCZOS)


def rotate(image: Image.Image, step: dict) -> Image.Image:
    degrees = step.get("degrees")
    if isinstance(degrees, bool) or not isinstance(degrees, (int, float)):
        raise StepError("„rotate“ braucht „degrees“ (gegen den Uhrzeigersinn, z. B. 90 oder -15).")
    degrees = float(degrees) % 360
    exact = {90.0: Image.Transpose.ROTATE_90, 180.0: Image.Transpose.ROTATE_180}
    exact[270.0] = Image.Transpose.ROTATE_270
    if degrees == 0:
        return image
    if degrees in exact:
        return image.transpose(exact[degrees])  # verlustfrei
    fill = step.get("background")
    if fill is None:
        rotated = _rgba(image).rotate(degrees, Image.Resampling.BICUBIC, expand=True)
    else:
        rotated = image.convert("RGB").rotate(
            degrees, Image.Resampling.BICUBIC, expand=True, fillcolor=_color(fill)
        )
    _check_size(*rotated.size)
    return rotated


def flip(image: Image.Image, step: dict) -> Image.Image:
    direction = step.get("direction", "horizontal")
    if direction == "horizontal":
        return ImageOps.mirror(image)
    if direction == "vertical":
        return ImageOps.flip(image)
    raise StepError("„direction“: horizontal oder vertical.")


def pad(image: Image.Image, step: dict) -> Image.Image:
    """Leinwand: Bild eingepasst und mittig auf Zielgröße bzw. mit Rand."""
    color = _color(step.get("color"), "white")
    margin = _int(step, "margin", low=0, high=2000)
    width = _int(step, "width", low=1)
    height = _int(step, "height", low=1)
    if margin is not None and width is None and height is None:
        width, height = image.width + 2 * margin, image.height + 2 * margin
    elif "aspect" in step:
        ratio = _aspect(step["aspect"])
        if image.width / image.height > ratio:
            width, height = image.width, round(image.width / ratio)
        else:
            width, height = round(image.height * ratio), image.height
    if width is None or height is None:
        raise StepError("„pad“ braucht „width“ und „height“, „aspect“ oder „margin“.")
    _check_size(width, height)
    inner = image.copy()
    inner.thumbnail((width, height), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA" if inner.mode == "RGBA" else "RGB", (width, height), color)
    offset = ((width - inner.width) // 2, (height - inner.height) // 2)
    canvas.paste(inner, offset, inner if inner.mode == "RGBA" else None)
    return canvas


def background(image: Image.Image, step: dict) -> Image.Image:
    """Transparenz mit einer Farbe füllen."""
    color = _color(step.get("color"), "white")
    rgba = _rgba(image)
    base = Image.new("RGB", rgba.size, color)
    base.paste(rgba, mask=rgba.getchannel("A"))
    return base


def grayscale(image: Image.Image, step: dict) -> Image.Image:
    if image.mode == "RGBA":
        gray = image.convert("LA").convert("RGBA")
        return gray
    return image.convert("L")


def _font(size: int):
    for path in FONT_PATHS:
        if Path(path).is_file():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default(size)


def text(image: Image.Image, step: dict) -> Image.Image:
    content = str(step.get("text") or "").strip()
    if not content:
        raise StepError("„text“ braucht den Text im Feld „text“.")
    if len(content) > MAX_TEXT:
        raise StepError(f"Text höchstens {MAX_TEXT} Zeichen.")
    size = _int(step, "size", max(12, image.height // 14), low=6, high=1000)
    color = _color(step.get("color"), "white")
    outline = step.get("outline", "black")
    stroke = max(1, size // 15) if outline else 0
    font = _font(size)
    canvas = _rgba(image).copy()
    draw = ImageDraw.Draw(canvas)
    box = draw.multiline_textbbox((0, 0), content, font=font, stroke_width=stroke, align="center")
    tw, th = box[2] - box[0], box[3] - box[1]
    margin = max(4, size // 2)
    if "x" in step or "y" in step:
        x = _int(step, "x", 0, high=image.width)
        y = _int(step, "y", 0, high=image.height)
    else:
        position = step.get("position", "bottom")
        if position not in POSITIONS:
            raise StepError(f"„position“: eines von {', '.join(POSITIONS)}.")
        vertical, _, horizontal = position.partition("-")
        if vertical in ("left", "right", "center"):
            vertical, horizontal = "center", vertical
        horizontal = horizontal or "center"
        x = {"left": margin, "right": image.width - tw - margin}.get(
            horizontal, (image.width - tw) // 2
        )
        y = {"top": margin, "bottom": image.height - th - margin}.get(
            vertical, (image.height - th) // 2
        )
    draw.multiline_text(
        (x - box[0], y - box[1]),
        content,
        font=font,
        fill=color,
        stroke_width=stroke,
        stroke_fill=_color(outline, "black") if outline else None,
        align="center",
    )
    return canvas if image.mode == "RGBA" else canvas.convert("RGB")


def collage(images_in: list[Image.Image], step: dict) -> Image.Image:
    """Raster aus allen Eingaben, jede in ihre Zelle eingepasst."""
    if len(images_in) < 2:
        raise StepError("Für eine Collage braucht es mindestens zwei Bilder („attachment_ids“).")
    count = len(images_in)
    columns = _int(step, "columns", math.ceil(math.sqrt(count)), low=1, high=count)
    rows = math.ceil(count / columns)
    gap = _int(step, "gap", 8, low=0, high=200)
    cell = _int(step, "cell", min(1024, max(max(i.size) for i in images_in)), low=32)
    width = columns * cell + (columns + 1) * gap
    height = rows * cell + (rows + 1) * gap
    _check_size(width, height)
    canvas = Image.new("RGB", (width, height), _color(step.get("background"), "white"))
    for index, item in enumerate(images_in):
        tile = _rgba(item).copy()
        tile.thumbnail((cell, cell), Image.Resampling.LANCZOS)
        col, row = index % columns, index // columns
        x = gap + col * (cell + gap) + (cell - tile.width) // 2
        y = gap + row * (cell + gap) + (cell - tile.height) // 2
        canvas.paste(tile, (x, y), tile)
    return canvas


OPERATIONS = {
    "crop": crop,
    "resize": resize,
    "rotate": rotate,
    "flip": flip,
    "pad": pad,
    "background": background,
    "grayscale": grayscale,
    "text": text,
}


def apply_steps(images_in: list[Image.Image], steps: list) -> Image.Image:
    """Schritte nacheinander auf das erste Bild anwenden (``collage`` nutzt alle)."""
    if not isinstance(steps, list) or not steps:
        raise StepError(MSG_NO_STEPS)
    if len(steps) > MAX_STEPS:
        raise StepError(MSG_TOO_MANY_STEPS)
    current = images_in[0]
    for number, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            raise StepError(f"Schritt {number}: ein Objekt mit „op“ erwartet.")
        op = step.get("op")
        try:
            if op == "collage":
                current = collage([current, *images_in[1:]], step)
            elif op in OPERATIONS:
                current = OPERATIONS[op](current, step)
            else:
                names = ", ".join([*OPERATIONS, "collage"])
                raise StepError(f"Unbekannte Operation „{op}“ (möglich: {names}).")
        except StepError as exc:
            raise StepError(f"Schritt {number} ({op}): {exc}") from None
    return current


# --- Speichern ----------------------------------------------------------------------------


def _target_format(requested, source: Attachment, image: Image.Image) -> str:
    if requested:
        fmt = FORMATS.get(str(requested).lower())
        if fmt is None:
            raise StepError("„format“: png, jpeg oder webp.")
        return fmt
    if image.mode in ("RGBA", "LA", "PA"):
        return "WEBP" if source.mime_type == "image/webp" else "PNG"
    return {"image/jpeg": "JPEG", "image/webp": "WEBP"}.get(source.mime_type, "PNG")


def store(message: Message, image: Image.Image, fmt: str, source: Attachment) -> Attachment:
    clean = chat_attachments._without_metadata(image, fmt)
    data = chat_attachments._encode(clean, fmt)
    result = chat_attachments.process_image(io.BytesIO(data), max_edge=images.MAX_SIDE)
    stem = Path(source.display_name).stem[:60] or "bild"
    attachment = Attachment(
        message=message,
        owner=None,  # erzeugt, nicht hochgeladen
        conversation=message.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type=result["mime_type"],
        width=result["width"],
        height=result["height"],
        size=len(result["data"]),
        original_name=f"{stem}-bearbeitet{result['extension']}",
        source_image=source,
    )
    try:
        with transaction.atomic():
            attachment.file.save(
                "bearbeitet" + result["extension"], ContentFile(result["data"]), save=False
            )
            attachment.thumbnail.save("thumb.webp", ContentFile(result["thumbnail"]), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise
    return attachment


def _outputs_so_far(message: Message) -> int:
    return Attachment.objects.filter(
        message=message, owner__isnull=True, generated_by_model__isnull=True
    ).count()


def transform(user, message: Message, arguments: dict) -> Attachment:
    """Ganzer Ablauf; ``StepError`` mit Text für das Modell."""
    args = arguments if isinstance(arguments, dict) else {}
    raw = args.get("attachment_ids")
    requested = (
        [i for i in raw if isinstance(i, int) and not isinstance(i, bool)]
        if isinstance(raw, list)
        else []
    )
    if _outputs_so_far(message) >= MAX_OUTPUTS_PER_ANSWER:
        raise StepError(MSG_TOO_MANY_OUTPUTS)
    sources = resolve_sources(user, message, requested)
    loaded = [_load(a) for a in sources]
    result = apply_steps(loaded, args.get("steps"))
    fmt = _target_format(args.get("format"), sources[0], result)
    return store(message, result, fmt, sources[0])


def _tool_available(user, ai_model) -> bool:
    return True  # nur lokale Rechenarbeit; Leserechte prüft jeder Aufruf


def _tool_run(user, arguments: dict, sources) -> tooling.BuiltinResult:
    message = sources.message
    try:
        attachment = transform(user, message, arguments)
    except StepError as exc:
        return tooling.BuiltinResult(str(exc), True)
    except chat_attachments.UploadError as exc:
        return tooling.BuiltinResult(str(exc), True)
    steps = ", ".join(
        str(s.get("op")) for s in (arguments.get("steps") or []) if isinstance(s, dict)
    )
    source = attachment.source_image_id
    return tooling.BuiltinResult(
        f"Bild bearbeitet ({steps}) und dem Nutzer in der Antwort angezeigt: Anhang "
        f"#{attachment.pk}, {attachment.width}×{attachment.height} px. Das Original "
        f"(Anhang #{source}) bleibt erhalten. {tooling.NOTE_ATTACHMENT_SHOWN}"
    )


TOOL_SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Bearbeitet ein vorhandenes Bild ohne Bildmodell und ohne Kosten: zuschneiden "
        "(auch auf ein Seitenverhältnis wie 1:1), Größe ändern, drehen, spiegeln, Rand bzw. "
        "Leinwand, transparenten Hintergrund füllen, Graustufen, Text einsetzen, Collage aus "
        "mehreren Bildern, Format umwandeln (png, jpeg, webp). Schritte laufen nacheinander. "
        "Für inhaltliche Änderungen („mach den Himmel blau“) stattdessen edit_image nutzen. "
        "Das Ergebnis ist ein neues Bild, das Original bleibt."
    ),
    parameters={
        "type": "object",
        "properties": {
            "attachment_ids": {
                "type": "array",
                "items": {"type": "integer"},
                "description": "Nummern der Bilder (Anhang #…); das erste wird bearbeitet, "
                "bei collage alle. Ohne Angabe die Bilder der letzten Nutzerfrage, sonst das "
                "letzte erzeugte Bild.",
            },
            "steps": {
                "type": "array",
                "description": (
                    "Schritte der Reihe nach, je ein Objekt mit op. "
                    'crop: left, top, right, bottom (Pixel) oder aspect ("1:1", "16:9") '
                    "mit anchor (center, top, bottom, left, right). "
                    "resize: width und/oder height, fit (contain, cover, stretch). "
                    "rotate: degrees (gegen den Uhrzeigersinn), background (Farbe, sonst "
                    "transparent). flip: direction (horizontal, vertical). "
                    "pad: width und height, aspect oder margin, color. "
                    "background: color (füllt Transparenz). grayscale. "
                    "text: text, position (top, bottom, center, top-left …) oder x und y, "
                    "size (Pixel), color, outline (Farbe oder leer). "
                    "collage: columns, gap, cell (Zellgröße), background."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "op": {
                            "type": "string",
                            "enum": [*OPERATIONS, "collage"],
                        }
                    },
                    "required": ["op"],
                },
            },
            "format": {
                "type": "string",
                "enum": ["png", "jpeg", "webp"],
                "description": "Ausgabeformat; ohne Angabe wie das Original.",
            },
        },
        "required": ["steps"],
    },
)


def register() -> None:
    tooling.register_builtin(
        tooling.BuiltinTool(
            name=TOOL_NAME,
            label=TOOL_LABEL,
            spec=TOOL_SPEC,
            available=_tool_available,
            run=_tool_run,
        )
    )


register()
