"""Bilderzeugung (M9-01): Bildmodell finden, Bild erzeugen, als Anhang speichern.

Drei Wege führen hierher, alle mit derselben Rechte- und Budgetprüfung:

- **Werkzeug** ``generate_image`` für Chatmodelle mit ``supports_tools``
  (``tooling.register_builtin``). Angeboten nur, wenn ein Bildmodell
  verfügbar ist; ein Satz im System-Prompt (``system_hint``) sagt dem Modell,
  dass es Bildwünsche damit erfüllt statt SVG/Code zu schreiben.
- **Modus „Bild“** im Eingabefeld (``api_images.stream``): Die Nachricht geht
  direkt an das Bildmodell, ohne Chatmodell.
- **Hinweis ohne Werkzeuge** (``image_mode.js``): Klingt die Nachricht wie ein
  Bildauftrag und kann das Chatmodell keine Werkzeuge, bietet die Oberfläche
  vor dem Senden den Modus „Bild“ an. Erkennung: ``REQUEST_PATTERNS``.

Entscheidungen:

- **Auswahl des Bildmodells** (``pick_image_model``): zuerst das Standard-
  Bildmodell aus ``ChatSettings``, sonst das erste (Reihenfolge, Name) aktive
  Bildmodell eines aktiven Anbieters, das die Rolle erlaubt, das Budget nicht
  sperrt und das nach dem gespeicherten Status online ist. Nur Anbieterarten
  mit ``generate_image`` (OpenAI-kompatibel, Google). Recht der Rolle:
  ``can_images`` („Bilder erzeugen und bearbeiten“).
- **Rückfrage beim Werkzeug:** Standard aus, ``ChatSettings.image_tool_confirm``
  schaltet sie ein. Begründung: Der Nutzer hat das Bild ausdrücklich gewünscht,
  eine Bestätigung je Bild wäre lästig; die Kosten begrenzen Budgetprüfung vor
  jedem Bild und ``MAX_IMAGES_PER_ANSWER``. Wer Kosten strenger steuern will,
  schaltet die Rückfrage im Admin ein.
- **Speichern:** Bilder werden wie Uploads neu kodiert (``attachments.process_image``:
  ohne EXIF/XMP/ICC, längste Kante 2048 px, Vorschaubild) und als ``Attachment``
  der Antwort gespeichert (``generated_by_model`` = Herkunft „erzeugt“,
  ``owner`` leer). So erscheinen sie in der Antwort mit Vorschau, Lightbox und
  Download, geschützt über die Leserechte des Chats. An das Chatmodell geht
  nur eine kurze Bestätigung (Anhang-Nr., Größe), nie das Bild.
- **Kosten:** je Bild eine Buchung (``billing.booking.book_attachment``) auf den
  Absender (geteilte Chats: wer die Antwort ausgelöst hat), Einheit
  ``image:<qualität>:<größe>`` plus – falls der Anbieter sie meldet – Tokens
  (GPT Image rechnet nach Tokens ab). ``Attachment.cost`` setzt die Buchung.
- **Datenschutz:** Die Beschreibung geht nur an den Anbieter des Bildmodells;
  Logs enthalten nur IDs und Fehlerarten.
- **Bearbeiten (M9-02, vorbereitet):** ``source_image`` am Anhang und
  ``ProviderAdapter.edit_image`` sind vorgesehen; ein Werkzeug ``edit_image``
  folgt, wenn ein Bild an der Nachricht hängt.
"""

from __future__ import annotations

import inspect
import io
import logging

from django.core.files.base import ContentFile
from django.db import transaction
from django.utils.text import slugify

from multigpt.accounts import usage
from multigpt.accounts.permissions import Action, can, model_permitted

from . import attachments as chat_attachments
from . import status as provider_status
from . import tooling
from .models import AIModel, Attachment, ChatSettings, Provider
from .providers import registry
from .providers.base import MSG_IMAGE_TOO_LARGE, ContentBlocked, ProviderError, ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "generate_image"
TOOL_LABEL = "Bilderzeugung"

# Format -> Größe (OpenAI GPT Image; Google nimmt das Seitenverhältnis daraus).
FORMATS = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}
FORMAT_LABELS = {"square": "quadratisch", "portrait": "hoch", "landscape": "quer"}
QUALITIES = {"auto": "automatisch", "low": "niedrig", "medium": "mittel", "high": "hoch"}
DEFAULT_FORMAT = "square"
DEFAULT_QUALITY = "auto"
# Anbieterarten, deren Adapter ``generate_image`` kann.
SUPPORTED_KINDS = (Provider.Kind.OPENAI_COMPAT, Provider.Kind.GOOGLE)
# Beschreibung höchstens so lang (DALL·E 3: 4000 Zeichen; GPT Image mehr).
MAX_PROMPT = 4000
# Höchstens so viele Bilder je Antwort (Schutz vor Werkzeugschleifen, Kosten).
MAX_IMAGES_PER_ANSWER = 4

MSG_NO_IMAGE_MODEL = "Es ist kein Modell zur Bilderzeugung eingerichtet bzw. freigegeben."
MSG_NOT_ALLOWED = "Die Bilderzeugung ist für dieses Konto nicht freigegeben."
MSG_EMPTY_PROMPT = "Bitte beschreiben, welches Bild erzeugt werden soll."
MSG_PROMPT_TOO_LONG = f"Die Bildbeschreibung ist zu lang (höchstens {MAX_PROMPT} Zeichen)."
MSG_TOO_MANY = f"Höchstens {MAX_IMAGES_PER_ANSWER} Bilder je Antwort."
MSG_UNSUPPORTED = "Dieser Anbieter kann keine Bilder erzeugen."
MSG_FAILED = "Das Bild konnte nicht erzeugt werden. Bitte später erneut versuchen."
MSG_INVALID_IMAGE = "Der Anbieter hat ein unbrauchbares Bild geliefert."
MSG_OFFLINE = "{provider} ist offline. Die Bilderzeugung ist gerade nicht möglich."

HINT = (
    "Wünscht der Nutzer ein Bild, eine Zeichnung, ein Foto, ein Logo oder eine Illustration, "
    "erzeuge es mit dem Werkzeug generate_image. Zeichne Bilder nicht selbst als SVG, ASCII "
    "oder Code – außer der Nutzer verlangt das ausdrücklich. Das Bild sieht der Nutzer "
    "direkt in der Antwort; beschreibe es danach nur kurz."
)

# Erkennung eines Bildauftrags (Hinweis ohne Werkzeuge, image_mode.js). Vorsichtig:
# Imperativ mit Objekt („zeichne mir“, „male die“), „erzeuge … ein Bild“, englische
# Formen. Lieber einmal zu wenig: Es ist nur ein Hinweis, das Senden bleibt möglich.
# Muster müssen in Python und JavaScript gleich wirken (keine Lookbehinds).
REQUEST_PATTERNS = [
    r"\b(zeichne|male)\s+(mir|uns|bitte|ein|eine|einen|die|der|das|den)\b",
    r"\bmal\s+(mir|uns)\s+(bitte\s+)?(ein|eine|einen)\b",
    r"\b(erzeuge?|erstelle?|generiere?|mache?)\s+(mir\s+|uns\s+)?(bitte\s+)?"
    r"(ein|eine|einen)\s+([a-zäöüß-]+\s+){0,2}"
    r"(bild|foto|grafik|illustration|zeichnung|logo|gemälde|poster|icon)\b",
    r"\b(draw|paint|sketch)\s+(me|us|a picture|an image)\b",
    r"\b(generate|create|make|render)\s+(me\s+)?(an?\s+)?([a-z-]+\s+){0,2}"
    r"(image|picture|photo|illustration|drawing|logo|painting)\b",
]
# Ausdrücklich Code gewünscht: kein Hinweis.
CODE_PATTERN = r"\b(svg|ascii|tikz|mermaid|plantuml|graphviz|html|css|canvas|python|code)\b"


class ImageError(Exception):
    """Abgelehnt oder fehlgeschlagen; ``message`` ist ein deutscher Text für Nutzer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Auswahl des Bildmodells ---------------------------------------------------------


def candidate_models(user) -> list[AIModel]:
    """Aktive Bildmodelle unterstützter, aktiver Anbieter, die die Rolle erlaubt
    (ohne Budget- und Statusprüfung). Reihenfolge wie in der Modellauswahl."""
    qs = AIModel.objects.filter(
        capability=AIModel.Capability.IMAGE,
        active=True,
        provider__active=True,
        provider__kind__in=SUPPORTED_KINDS,
    ).select_related("provider")
    return [m for m in qs if model_permitted(user, m)]


def blocked_reason(user, ai_model: AIModel) -> str:
    """Warum ``ai_model`` gerade nicht nutzbar ist (Budget, offline); leer = frei.

    Nur gespeicherter Status (kein Netzaufruf): Das Werkzeug wird bei jeder
    Antwort geprüft und darf sie nicht verzögern.
    """
    if not can(user, Action.USE_MODEL, ai_model):
        return usage.blocked_reason(user, ai_model) or usage.BUDGET_EXHAUSTED_MESSAGE
    online, available = provider_status.model_state(ai_model)
    if not (online and available):
        return MSG_OFFLINE.format(provider=ai_model.provider.name)
    return ""


def pick_image_model(user) -> tuple[AIModel | None, str]:
    """(Bildmodell, "") oder (None, Grund für Nutzer bzw. Modell)."""
    if not can(user, Action.IMAGES):
        return None, MSG_NOT_ALLOWED
    models = candidate_models(user)
    default_id = (
        ChatSettings.objects.filter(pk=ChatSettings.SINGLETON_PK)
        .values_list("default_image_model_id", flat=True)
        .first()
    )
    models.sort(key=lambda m: m.pk != default_id)  # stabil: Standard zuerst
    first_reason = ""
    for ai_model in models:
        reason = blocked_reason(user, ai_model)
        if not reason:
            return ai_model, ""
        first_reason = first_reason or reason
    return None, first_reason or MSG_NO_IMAGE_MODEL


def reason_status(reason: str) -> int:
    """HTTP-Status zum Grund aus ``pick_image_model``: kein Modell 409, offline
    503, sonst (Recht, Budget) 403."""
    if reason == MSG_NO_IMAGE_MODEL:
        return 409
    if reason.endswith(MSG_OFFLINE.split("}", 1)[1]):
        return 503
    return 403


def tool_needs_confirmation() -> bool:
    """Rückfrage vor jedem Bild über das Werkzeug (Einstellung im Admin)."""
    return bool(
        ChatSettings.objects.filter(pk=ChatSettings.SINGLETON_PK)
        .values_list("image_tool_confirm", flat=True)
        .first()
    )


# --- Erzeugen ----------------------------------------------------------------------


def clean_options(fmt=None, quality=None, transparent=None) -> tuple[str, str, bool]:
    """Format, Qualität und Transparenz prüfen; ungültig -> ``ImageError``."""
    fmt = fmt or DEFAULT_FORMAT
    quality = quality or DEFAULT_QUALITY
    if fmt not in FORMATS or quality not in QUALITIES:
        raise ImageError("Ungültiges Format oder ungültige Qualität.")
    if transparent is not None and not isinstance(transparent, bool):
        raise ImageError("Ungültige Angabe zum transparenten Hintergrund.")
    return fmt, quality, bool(transparent)


def _file_name(prompt: str) -> str:
    stem = slugify(" ".join(prompt.split()[:8]))[:50].strip("-") or "bild"
    return f"bild-{stem}.png"


def _book(attachment: Attachment, ai_model: AIModel, units: dict, image_usage, user) -> None:
    """Buchung je Bild (billing). Fehler verhindern das Bild nicht (nur Log)."""
    try:
        from multigpt.billing import booking

        kwargs = {"user": user}
        if (
            image_usage is not None
            and "usage" in inspect.signature(booking.book_attachment).parameters
        ):
            kwargs["usage"] = image_usage
        booking.book_attachment(attachment, ai_model, units, **kwargs)
    except Exception as exc:  # noqa: BLE001 - Buchung darf das Ergebnis nicht verlieren
        logger.error("Buchung für Bild %s fehlgeschlagen: %s", attachment.pk, type(exc).__name__)


def _store(message, ai_model: AIModel, image, prompt: str) -> Attachment:
    """Bild prüfen, ohne Metadaten neu kodieren, Vorschau, als Anhang speichern."""
    if len(image.data) > chat_attachments.max_image_bytes():
        raise ImageError(MSG_IMAGE_TOO_LARGE)
    try:
        result = chat_attachments.process_image(io.BytesIO(image.data))
    except chat_attachments.UploadError as exc:
        raise ImageError(MSG_INVALID_IMAGE) from exc
    attachment = Attachment(
        message=message,
        owner=None,  # erzeugt, nicht hochgeladen
        conversation=message.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type=result["mime_type"],
        width=result["width"],
        height=result["height"],
        size=len(result["data"]),
        original_name=_file_name(prompt).removesuffix(".png") + result["extension"],
        generated_by_model=ai_model,
    )
    try:
        with transaction.atomic():
            attachment.file.save(
                "image" + result["extension"], ContentFile(result["data"]), save=False
            )
            attachment.thumbnail.save("thumb.webp", ContentFile(result["thumbnail"]), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise
    return attachment


def generate(
    user,
    message,
    prompt: str,
    *,
    ai_model: AIModel,
    fmt: str = DEFAULT_FORMAT,
    quality: str = DEFAULT_QUALITY,
    transparent: bool = False,
) -> tuple[Attachment, str]:
    """Ein Bild zu ``prompt`` mit ``ai_model`` erzeugen und an ``message`` hängen.

    Rechte und Modellwahl prüft der Aufrufer (``pick_image_model``); hier
    wird nur das Budget für genau dieses Modell erneut geprüft. Liefert
    (Anhang, vom Anbieter umgeschriebene Beschreibung oder ""). Fehler als
    ``ImageError`` mit deutschem Text (ohne Key, ohne Rohtext des Anbieters).
    """
    prompt = (prompt or "").strip()
    if not prompt:
        raise ImageError(MSG_EMPTY_PROMPT)
    if len(prompt) > MAX_PROMPT:
        raise ImageError(MSG_PROMPT_TOO_LONG)
    if not can(user, Action.USE_MODEL, ai_model):
        raise ImageError(usage.blocked_reason(user, ai_model) or MSG_NO_IMAGE_MODEL, 403)
    size = FORMATS[fmt]
    adapter = registry.get_adapter(ai_model.provider)
    try:
        result = adapter.generate_image(
            ai_model.model_id,
            prompt,
            size=size,
            quality=quality,
            background="transparent" if transparent else None,
            n=1,
        )
    except NotImplementedError:
        raise ImageError(MSG_UNSUPPORTED, 409) from None
    except ContentBlocked as exc:
        logger.info("Bild für Nachricht %s: Inhaltsfilter (Modell %s)", message.pk, ai_model.pk)
        raise ImageError(str(exc), 422) from None
    except ProviderError as exc:
        logger.info(
            "Bild für Nachricht %s fehlgeschlagen (Modell %s, %s)",
            message.pk,
            ai_model.pk,
            type(exc).__name__,
        )
        if getattr(exc, "retryable", False):
            provider_status.invalidate(ai_model.provider_id)
        raise ImageError(str(exc) or MSG_FAILED, 502) from None
    image = result.images[0]
    attachment = _store(message, ai_model, image, prompt)
    _book(attachment, ai_model, {f"image:{quality}:{size}": 1}, result.usage, user)
    attachment.refresh_from_db(fields=["cost"])
    logger.info(
        "Bild %s erzeugt (Nachricht %s, Modell %s, %d Bytes)",
        attachment.pk,
        message.pk,
        ai_model.pk,
        attachment.size,
    )
    return attachment, image.revised_prompt


def generated_count(message) -> int:
    return Attachment.objects.filter(message=message, generated_by_model__isnull=False).count()


# --- Werkzeug generate_image ---------------------------------------------------------


def system_hint(bindings) -> str:
    """Satz für den System-Prompt, wenn ``generate_image`` angeboten wird."""
    return HINT if TOOL_NAME in (bindings or {}) else ""


def _tool_available(user, ai_model) -> bool:
    model, _ = pick_image_model(user)
    return model is not None


def _tool_run(user, arguments: dict, sources) -> tooling.BuiltinResult:
    """Ergebnis an das Modell: kurze Bestätigung mit Anhang-Nummer und Größe."""
    args = arguments if isinstance(arguments, dict) else {}
    message = sources.message
    image_model, reason = pick_image_model(user)
    if image_model is None:
        return tooling.BuiltinResult(reason, True)
    if generated_count(message) >= MAX_IMAGES_PER_ANSWER:
        return tooling.BuiltinResult(MSG_TOO_MANY, True)
    fmt = args.get("size") if args.get("size") in FORMATS else DEFAULT_FORMAT
    quality = args.get("quality") if args.get("quality") in QUALITIES else DEFAULT_QUALITY
    transparent = args.get("transparent_background") is True
    try:
        attachment, _ = generate(
            user,
            message,
            str(args.get("prompt") or ""),
            ai_model=image_model,
            fmt=fmt,
            quality=quality,
            transparent=transparent,
        )
    except ImageError as exc:
        return tooling.BuiltinResult(exc.message, True)
    return tooling.BuiltinResult(
        f"Bild erzeugt und dem Nutzer in der Antwort angezeigt (Anhang #{attachment.pk}, "
        f"{attachment.width}×{attachment.height} px, Modell {image_model.display_name}). "
        "Das Bild nicht erneut als Code oder Text darstellen."
    )


TOOL_SPEC = ToolSpec(
    name=TOOL_NAME,
    description=(
        "Erzeugt ein Bild mit einem Bildmodell und zeigt es dem Nutzer direkt in der Antwort. "
        "Nutze dieses Werkzeug immer, wenn der Nutzer ein Bild, eine Zeichnung, ein Foto, ein "
        "Logo oder eine Illustration möchte – zeichne Bilder nicht selbst als SVG, ASCII oder "
        "Code, außer der Nutzer verlangt ausdrücklich Code. Beschreibe im Argument „prompt“ "
        "das gewünschte Bild genau (Motiv, Stil, Farben, Text im Bild). Kostet Geld: je "
        "Wunsch nur ein Bild, außer der Nutzer will mehrere."
    ),
    parameters={
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "Ausführliche Beschreibung des Bildes.",
            },
            "size": {
                "type": "string",
                "enum": list(FORMATS),
                "description": "Format: square (quadratisch), portrait (hoch), "
                "landscape (quer). Standard square.",
            },
            "quality": {
                "type": "string",
                "enum": list(QUALITIES),
                "description": "Qualität; höher kostet mehr. Standard auto.",
            },
            "transparent_background": {
                "type": "boolean",
                "description": "Transparenter Hintergrund (z. B. für Logos), sofern das "
                "Modell es kann.",
            },
        },
        "required": ["prompt"],
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
            confirm=tool_needs_confirmation,
        )
    )


register()
