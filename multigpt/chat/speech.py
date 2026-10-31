"""Sprache (M10): Spracherkennung per Mikrofon, Vorlesen von Antworten.

Zwei Wege, beide mit Recht ``Action.VOICE`` („Sprache (Aufnahme und Vorlesen)“ der Rolle),
Rollenfreigabe des Modells, Budgetprüfung und Abrechnung wie bei den Bildern (``images.py``):

- **Spracherkennung** (``transcribe``, M10-01): Der Browser nimmt mit ``MediaRecorder`` auf und
  schickt die Datei an ``POST /api/speech/transcribe/``. Der Server prüft Größe und Länge
  (``ChatSettings``, M10-03), reicht sie an ein ``stt``-Modell weiter (``/audio/transcriptions``,
  OpenAI und Eliezer) und liefert den Text; der landet im Eingabefeld zum Korrigieren. Die
  Aufnahme wird **nicht gespeichert** (Datenschutz), nur gebucht: ``audio_minute`` (Minuten,
  vom Anbieter gemeldet, sonst die Angabe des Browsers, begrenzt auf die Höchstlänge) bzw.
  Tokens, falls der Anbieter sie meldet (GPT-4o-Transcribe).
- **Vorlesen** (``speak``, M10-02): Der Text der Antwort wird von Markdown befreit (``plain_text``),
  an ein ``tts``-Modell geschickt (``/audio/speech``) und als ``Attachment`` (Art Audio,
  ``generated_by_model``, ``owner`` leer) an die Antwort gehängt. Wer die Antwort lesen darf, darf
  die Audiodatei abspielen (Auslieferung wie bei jedem Anhang, ``api_attachments``). Eine Antwort
  wird nur einmal vertont; erneutes Vorlesen spielt die gespeicherte Datei (keine zweite
  Buchung). Buchung: ``tts_characters`` (Preis je 1 Mio. Zeichen) über ``book_attachment``.

Auswahl (``pick_model``): das Modell aus den Einstellungen des Kontos (``SpeechPreference``),
sonst das Standardmodell der Chat-Einstellungen, sonst das erste freigegebene, nicht gesperrte,
erreichbare Modell. Stimmen: ``voices`` fragt den Anbieter (Eliezer ``GET /v1/audio/voices``);
OpenAI kennt keine Liste, dafür gibt es ``OPENAI_VOICES``.

Datenschutz: Logs enthalten nur IDs, Längen und Fehlerarten, nie Text oder Audio.
"""

from __future__ import annotations

import logging
import re

from django.core.cache import cache
from django.core.files.base import ContentFile
from django.db import transaction

from multigpt.accounts import usage
from multigpt.accounts.permissions import Action, can, model_permitted

from . import attachments as chat_attachments
from . import status as provider_status
from .models import AIModel, Attachment, ChatSettings, Message, Provider, SpeechPreference
from .providers import registry
from .providers.base import ProviderError

logger = logging.getLogger(__name__)

# Anbieterarten, deren Adapter ``transcribe``/``speak`` kann.
SUPPORTED_KINDS = (Provider.Kind.OPENAI_COMPAT, Provider.Kind.LMSTUDIO)
# Stimmen der OpenAI-API (es gibt keinen Listen-Endpunkt); Stand der Doku Oktober 2026.
OPENAI_VOICES = (
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
)
VOICE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
LANGUAGE_PATTERN = re.compile(r"^[a-z]{2,3}(-[a-z]{2,4})?$")
VOICES_CACHE_SECONDS = 300
# Inhaltstypen von MediaRecorder (Chrome/Firefox webm bzw. ogg, Safari mp4).
AUDIO_EXTENSIONS = {
    "audio/webm": ".webm",
    "video/webm": ".webm",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
    "audio/x-m4a": ".m4a",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/flac": ".flac",
}
# Eine Sekunde Spielraum für Abweichungen zwischen Uhr des Browsers und Dauer der Datei.
DURATION_SLACK = 2.0

MSG_NOT_ALLOWED = "Sprache ist für dieses Konto nicht freigegeben."
MSG_NO_STT_MODEL = "Es ist kein Modell zur Spracherkennung eingerichtet bzw. freigegeben."
MSG_NO_TTS_MODEL = "Es ist kein Modell zur Sprachausgabe eingerichtet bzw. freigegeben."
MSG_OFFLINE = "{provider} ist offline. Die Sprachfunktion ist gerade nicht möglich."
MSG_NO_FILE = "Es wurde keine Aufnahme übermittelt."
MSG_BAD_TYPE = "Dieses Audioformat wird nicht angenommen."
MSG_TOO_BIG = "Die Aufnahme ist zu groß (höchstens {mb} MB)."
MSG_TOO_LONG = "Die Aufnahme ist zu lang (höchstens {seconds} Sekunden)."
MSG_BAD_DURATION = "Ungültige Angabe zur Länge der Aufnahme."
MSG_BAD_LANGUAGE = "Ungültiger Sprachcode."
MSG_NOTHING_HEARD = "Es wurde keine Sprache erkannt. Bitte noch einmal aufnehmen."
MSG_UNSUPPORTED = "Dieser Anbieter kann das nicht."
MSG_NOT_ANSWER = "Vorlesen gibt es nur für Antworten."
MSG_NO_TEXT = "Diese Antwort enthält keinen Text zum Vorlesen."
MSG_NOT_READY = "Die Antwort ist noch nicht fertig."
MSG_FAILED = "Die Sprachfunktion ist fehlgeschlagen. Bitte später erneut versuchen."
MSG_NOT_FOUND = "Modell nicht gefunden."


class SpeechError(Exception):
    """Abgelehnt oder fehlgeschlagen; ``message`` ist ein deutscher Text für Nutzer."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


# --- Grenzen (M10-03) --------------------------------------------------------------


def limits() -> dict:
    """Grenzen aus den Chat-Einstellungen: Aufnahmelänge (s), Dateigröße (Bytes), Zeichen."""
    row = ChatSettings.load()
    return {
        "max_seconds": int(row.speech_max_seconds),
        "max_upload_bytes": int(row.speech_max_upload_mb) * 1024 * 1024,
        "max_chars": int(row.speech_max_chars),
    }


# --- Auswahl der Modelle -----------------------------------------------------------


def preference(user) -> SpeechPreference | None:
    return (
        SpeechPreference.objects.filter(user=user).select_related("stt_model", "tts_model").first()
    )


def candidate_models(user, capability: str) -> list[AIModel]:
    """Aktive Sprachmodelle unterstützter, aktiver Anbieter, die die Rolle erlaubt."""
    qs = AIModel.objects.filter(
        capability=capability,
        active=True,
        provider__active=True,
        provider__kind__in=SUPPORTED_KINDS,
    ).select_related("provider")
    return [m for m in qs if model_permitted(user, m)]


def ordered_models(user, capability: str) -> list[AIModel]:
    """Nutzbare Modelle: die eigene Wahl zuerst, dann das Standardmodell, dann der Rest."""
    models = candidate_models(user, capability)
    pref = preference(user)
    row = ChatSettings.load()
    if capability == AIModel.Capability.STT:
        wanted = [pref.stt_model_id if pref else None, row.default_stt_model_id]
    else:
        wanted = [pref.tts_model_id if pref else None, row.default_tts_model_id]
    rank = {pk: i for i, pk in enumerate(p for p in wanted if p)}
    models.sort(key=lambda m: rank.get(m.pk, len(rank)))  # stabil
    return models


def blocked_reason(user, ai_model: AIModel) -> str:
    """Warum ``ai_model`` gerade nicht nutzbar ist (Budget, offline); leer = frei. Nur der
    gespeicherte Status, kein Netzaufruf."""
    if not can(user, Action.USE_MODEL, ai_model):
        return usage.blocked_reason(user, ai_model) or usage.BUDGET_EXHAUSTED_MESSAGE
    online, available = provider_status.model_state(ai_model)
    if not (online and available):
        return MSG_OFFLINE.format(provider=ai_model.provider.name)
    return ""


def pick_model(user, capability: str) -> tuple[AIModel | None, str]:
    """(Modell, "") oder (None, Grund für den Nutzer)."""
    missing = MSG_NO_STT_MODEL if capability == AIModel.Capability.STT else MSG_NO_TTS_MODEL
    if not can(user, Action.VOICE):
        return None, MSG_NOT_ALLOWED
    first_reason = ""
    for ai_model in ordered_models(user, capability):
        reason = blocked_reason(user, ai_model)
        if not reason:
            return ai_model, ""
        first_reason = first_reason or reason
    return None, first_reason or missing


def reason_status(reason: str) -> int:
    """HTTP-Status zum Grund aus ``pick_model``: kein Modell 409, offline 503, sonst 403."""
    if reason in (MSG_NO_STT_MODEL, MSG_NO_TTS_MODEL):
        return 409
    if reason.endswith(MSG_OFFLINE.split("}", 1)[1]):
        return 503
    return 403


def availability(user) -> dict:
    """Was kann das Konto im Chat? ``{"stt": bool, "tts": bool}`` (Oberfläche: Mikrofon bzw.
    Lautsprecherknopf). Rolle und mindestens ein freigegebenes Modell; kein Netzaufruf."""
    if not can(user, Action.VOICE):
        return {"stt": False, "tts": False}
    return {
        "stt": bool(candidate_models(user, AIModel.Capability.STT)),
        "tts": bool(candidate_models(user, AIModel.Capability.TTS)),
    }


# --- Stimmen -----------------------------------------------------------------------


def voices(ai_model: AIModel, *, refresh: bool = False) -> list[str]:
    """Stimmen des Modells: vom Anbieter (kurz gemerkt), bei OpenAI die feste Liste."""
    key = f"speech-voices:{ai_model.provider_id}"
    if not refresh:
        cached = cache.get(key)
        if cached is not None:
            return cached
    names = registry.get_adapter(ai_model.provider).list_voices()
    if (
        not names
        and ai_model.provider.kind == Provider.Kind.OPENAI_COMPAT
        and not ai_model.provider.is_local
    ):
        names = list(OPENAI_VOICES)
    if names:  # eine leere Antwort (Anbieter gerade nicht erreichbar) nicht merken
        cache.set(key, names, VOICES_CACHE_SECONDS)
    return names


# --- Spracherkennung (M10-01) ------------------------------------------------------


def clean_language(raw) -> str | None:
    """Sprachcode aus der Anfrage; leer oder ``auto`` = automatisch (``None``)."""
    value = str(raw or "").strip()
    if not value or value.lower() == "auto":
        return None
    value = value.lower()
    if not LANGUAGE_PATTERN.match(value):
        raise SpeechError(MSG_BAD_LANGUAGE)
    return value.split("-")[0]


def _content_type(upload) -> str:
    return (upload.content_type or "").split(";")[0].strip().lower()


def transcribe(user, upload, duration, language=None) -> dict:
    """Aufnahme ``upload`` (Django-Upload) in Text umwandeln.

    ``duration``: vom Browser gemessene Länge in Sekunden. Gibt ``{"text", "language",
    "seconds", "model"}`` zurück oder wirft ``SpeechError``."""
    if not can(user, Action.VOICE):
        raise SpeechError(MSG_NOT_ALLOWED, 403)
    if upload is None:
        raise SpeechError(MSG_NO_FILE)
    limit = limits()
    try:
        claimed = float(duration)
    except (TypeError, ValueError):
        raise SpeechError(MSG_BAD_DURATION) from None
    if not (0 <= claimed < 86400):
        raise SpeechError(MSG_BAD_DURATION)
    if claimed > limit["max_seconds"] + DURATION_SLACK:
        raise SpeechError(MSG_TOO_LONG.format(seconds=limit["max_seconds"]), 413)
    if upload.size <= 0:
        raise SpeechError(MSG_NO_FILE)
    if upload.size > limit["max_upload_bytes"]:
        raise SpeechError(MSG_TOO_BIG.format(mb=limit["max_upload_bytes"] // (1024 * 1024)), 413)
    mime_type = _content_type(upload)
    extension = AUDIO_EXTENSIONS.get(mime_type)
    if extension is None:
        raise SpeechError(MSG_BAD_TYPE, 415)
    code = clean_language(language)
    ai_model, reason = pick_model(user, AIModel.Capability.STT)
    if ai_model is None:
        raise SpeechError(reason, reason_status(reason))
    adapter = registry.get_adapter(ai_model.provider)
    data = upload.read()
    try:
        result = adapter.transcribe(
            ai_model.model_id, ("aufnahme" + extension, mime_type, data), language=code
        )
    except NotImplementedError:
        raise SpeechError(MSG_UNSUPPORTED, 409) from None
    except ProviderError as exc:
        logger.info(
            "Spracherkennung fehlgeschlagen (Modell %s, %s)", ai_model.pk, type(exc).__name__
        )
        if getattr(exc, "retryable", False):
            provider_status.invalidate(ai_model.provider_id)
        raise SpeechError(str(exc) or MSG_FAILED, 502) from None
    seconds = result.duration or min(claimed, float(limit["max_seconds"]))
    _book(None, user, ai_model, {"audio_minute": round(seconds / 60, 4)}, result.usage)
    logger.info(
        "Spracherkennung (Modell %s, %d Bytes, %.1f s, %d Zeichen)",
        ai_model.pk,
        len(data),
        seconds,
        len(result.text),
    )
    if result.duration and result.duration > limit["max_seconds"] + DURATION_SLACK:
        raise SpeechError(MSG_TOO_LONG.format(seconds=limit["max_seconds"]), 413)
    if not result.text:
        raise SpeechError(MSG_NOTHING_HEARD, 422)
    return {
        "text": result.text,
        "language": result.language,
        "seconds": round(seconds, 1),
        "model": ai_model.display_name,
    }


def _book(attachment, user, ai_model: AIModel, units: dict, result_usage) -> None:
    """Buchung (billing). Ein Fehler verhindert das Ergebnis nicht (nur Log)."""
    try:
        from multigpt.billing import booking

        if attachment is not None:
            booking.book_attachment(attachment, ai_model, units, user=user, usage=result_usage)
        else:
            booking.book_service(user, ai_model, units, usage=result_usage)
    except Exception as exc:  # noqa: BLE001 - Buchung darf das Ergebnis nicht verlieren
        logger.error(
            "Buchung für Sprache (Modell %s) fehlgeschlagen: %s", ai_model.pk, type(exc).__name__
        )


# --- Vorlesen (M10-02) -------------------------------------------------------------

_FENCE = re.compile(r"(```|~~~).*?(\1|\Z)", re.DOTALL)
_LATEX_BLOCK = re.compile(r"\$\$.*?\$\$|\\\[.*?\\\]|\\\(.*?\\\)", re.DOTALL)
_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_CITATION = re.compile(r"\s?\[(?:\d+(?:\s*[,–-]\s*\d+)*)\]")
_HTML = re.compile(r"</?[A-Za-z][^>]*>")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+", re.MULTILINE)
_RULE = re.compile(r"^\s*(?:[-*_]\s*){3,}$", re.MULTILINE)
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$", re.MULTILINE)
_EMPHASIS = re.compile(r"(\*\*|__|\*|_|~~|`)")


def plain_text(markdown: str) -> str:
    """Antworttext fürs Vorlesen: ohne Code, Formeln, Links, Quellenzahlen und Markdown-Zeichen."""
    text = markdown or ""
    text = _FENCE.sub("\n", text)
    text = _LATEX_BLOCK.sub(" ", text)
    text = _IMAGE.sub(lambda m: m.group(1), text)
    text = _LINK.sub(lambda m: m.group(1), text)
    text = _BARE_URL.sub("", text)
    text = _CITATION.sub("", text)
    text = _HTML.sub("", text)
    text = _TABLE_SEP.sub("", text)
    text = _RULE.sub("", text)
    text = _HEADING.sub("", text)
    text = _QUOTE.sub("", text)
    text = _BULLET.sub("", text)
    text = text.replace("|", ", ")
    text = _EMPHASIS.sub("", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def shorten(text: str, max_chars: int) -> tuple[str, bool]:
    """Auf höchstens ``max_chars`` kürzen, möglichst am Satz- oder Absatzende."""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    for pattern in (r"\n\n", r"[.!?](?=\s)", r"\s"):
        matches = list(re.finditer(pattern, cut))
        if matches and matches[-1].end() > max_chars // 2:
            end = matches[-1].end()
            return cut[:end].strip(), True
    return cut.strip(), True


def audio_of(message: Message) -> Attachment | None:
    """Die erzeugte Audiodatei der Antwort, falls vorgelesen."""
    return (
        Attachment.objects.filter(message=message, kind=Attachment.Kind.AUDIO, owner__isnull=True)
        .order_by("-created", "-id")
        .first()
    )


def voice_for(user, ai_model: AIModel) -> str:
    """Stimme des Kontos; sie gehört zum Modell der Einstellungen und gilt nicht für ein anderes."""
    pref = preference(user)
    if pref is None or not pref.voice:
        return ""
    if pref.tts_model_id and pref.tts_model_id != ai_model.pk:
        return ""
    return pref.voice


def speak(user, message: Message, *, force: bool = False) -> tuple[Attachment, bool, bool]:
    """Antwort ``message`` vorlesen. Liefert (Audio-Anhang, neu erzeugt, gekürzt).

    Leserecht auf den Chat prüft der Aufrufer. Gibt es schon eine Audiodatei, wird sie
    zurückgegeben (``force`` erzeugt neu, z. B. mit anderer Stimme)."""
    if not can(user, Action.VOICE):
        raise SpeechError(MSG_NOT_ALLOWED, 403)
    if message.role != Message.Role.ASSISTANT:
        raise SpeechError(MSG_NOT_ANSWER)
    if message.status not in (Message.Status.COMPLETE, Message.Status.ABORTED):
        raise SpeechError(MSG_NOT_READY, 409)
    if not force:
        existing = audio_of(message)
        if existing is not None:
            return existing, False, False
    limit = limits()
    text, truncated = shorten(plain_text(message.content), limit["max_chars"])
    if not text:
        raise SpeechError(MSG_NO_TEXT, 422)
    ai_model, reason = pick_model(user, AIModel.Capability.TTS)
    if ai_model is None:
        raise SpeechError(reason, reason_status(reason))
    adapter = registry.get_adapter(ai_model.provider)
    try:
        result = adapter.speak(ai_model.model_id, text, voice_for(user, ai_model) or None)
    except NotImplementedError:
        raise SpeechError(MSG_UNSUPPORTED, 409) from None
    except ProviderError as exc:
        logger.info(
            "Sprachausgabe für Nachricht %s fehlgeschlagen (Modell %s, %s)",
            message.pk,
            ai_model.pk,
            type(exc).__name__,
        )
        if getattr(exc, "retryable", False):
            provider_status.invalidate(ai_model.provider_id)
        raise SpeechError(str(exc) or MSG_FAILED, 502) from None
    attachment = Attachment(
        message=message,
        owner=None,  # erzeugt, nicht hochgeladen
        conversation=message.conversation,
        kind=Attachment.Kind.AUDIO,
        mime_type=result.mime_type,
        size=len(result.data),
        original_name=f"vorlesen-{message.pk}{result.extension}",
        generated_by_model=ai_model,
    )
    try:
        with transaction.atomic():
            attachment.file.save("audio" + result.extension, ContentFile(result.data), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise
    _book(attachment, user, ai_model, {"tts_characters": len(text)}, result.usage)
    attachment.refresh_from_db(fields=["cost"])
    logger.info(
        "Audio %s erzeugt (Nachricht %s, Modell %s, %d Zeichen, %d Bytes)",
        attachment.pk,
        message.pk,
        ai_model.pk,
        len(text),
        attachment.size,
    )
    return attachment, True, truncated
