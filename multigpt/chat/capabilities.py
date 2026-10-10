"""Fähigkeiten von Modellen aus ihrer ID schätzen (Vorbelegung, im Admin änderbar).

Reine Funktionen ohne Django-Importe, damit auch Migrationen sie nutzen können.
"""

import re
from dataclasses import dataclass

# Bild-Eingabe (Vision): bekannte Familien und typische Kennzeichen lokaler
# Vision-Modelle (olmOCR, Qwen-VL, LLaVA, Pixtral, „vision“).
_VISION = re.compile(
    r"(^|/)(gpt-4o|chatgpt-4o|gpt-4\.1|gpt-5|claude-|gemini-)"
    r"|olmocr|llava|pixtral|vision|(^|[-_./])vl([-_./]|$)|-vl\d|qwen\d(\.\d)?-?vl"
)
# Varianten ohne Bild-Eingabe trotz passender Familie.
_NOT_VISION = re.compile(r"embed|audio|realtime|transcribe|tts|whisper|dall-e|gpt-image|imagen")


def guess_vision(model_id: str) -> bool:
    """Versteht das Modell vermutlich Bilder? Im Zweifel ``False``."""
    lowered = (model_id or "").lower()
    if _NOT_VISION.search(lowered):
        return False
    return bool(_VISION.search(lowered))


# Werkzeuge (Function Calling). Stand der Herstellerangaben, geprüft 2026-10-10:
#
# - OpenAI (developers.openai.com/api/docs/models): gpt-4o, gpt-4.1, gpt-4-turbo,
#   gpt-5.x (auch *-chat-latest laut Modellseite), o1, o3, o4-mini. Ohne
#   Werkzeuge: chatgpt-4o-latest, *-search-preview, Audio/Realtime/Transcribe,
#   o1-mini/-preview, *-deep-research.
# - Anthropic (docs.claude.com, Models overview): alle claude-3-Modelle und
#   neuer (claude-3-5-…, claude-sonnet-4…, claude-opus-4…, claude-haiku-4-5,
#   claude-*-5); claude-2 und claude-instant nicht.
# - Google (ai.google.dev/gemini-api/docs/models): gemini-1.5, 2.x, 3.x; ohne
#   Werkzeuge die Bild-, TTS- und Embedding-Varianten. Gemma über die
#   Gemini-API: kein natives Function Calling -> nein.
# - Lokal (lmstudio.ai/docs, „Tool Use“: native Unterstützung u. a. Qwen,
#   Llama 3.1+, Mistral; andere nur per Standard-Prompt, oft unzuverlässig):
#   gpt-oss, qwen2.5/qwen3 (auch -coder), qwq, llama-3.1/3.2/3.3/4,
#   Mistral-Familie (ab v0.3, Nemo, Small, Large, Ministral, Magistral,
#   Devstral, Codestral), command-r/command-a, Hermes 2 Pro/3/4, Granite 3+,
#   Functionary, GLM 4.5+, deepseek-v3 bzw. deepseek-chat und deepseek-r1-0528.
#   Nein: gemma (nur per Prompt), die übrigen deepseek-r1 (auch -distill),
#   reine Vision-/OCR-Modelle (llava, olmOCR, *-vl, *-vision).
#
# LM Studio meldet die Fähigkeit selbst (``/api/v0/models``, ``tool_use``,
# siehe ``detect.py``); diese Liste ist nur die Rückfallebene. Im Zweifel nein.
_TOOLS = re.compile(
    # Cloud
    r"(^|/)(gpt-4o|gpt-4\.1|gpt-4-turbo|gpt-5|o1|o3|o4-mini)([-_.]|$)"
    r"|(^|/)claude-(3|opus|sonnet|haiku)"
    r"|(^|/)(models/)?gemini-(1\.5|2|3)"
    # Lokal
    r"|gpt-oss"
    r"|qwen-?2\.5|qwen-?3|qwq"
    r"|llama-?3\.[123]|llama-?4"
    r"|mistral|mixtral|ministral|magistral|devstral|codestral"
    r"|command-r|command-a"
    r"|hermes-?(2-pro|3|4)"
    r"|granite-?(3|4)"
    r"|functionary"
    r"|glm-?4\.[5-9]"
    r"|deepseek-(v3|chat)|deepseek-r1-0528"
)
# Varianten ohne Werkzeuge trotz passender Familie.
_NOT_TOOLS = re.compile(
    r"embed|olmocr|ocr|audio|realtime|transcribe|tts|whisper|dall-e|gpt-image|imagen"
    r"|-image|search-preview|deep-research|chatgpt-4o|o1-(mini|preview)"
    r"|llava|vision|(^|[-_./])vl([-_./]|$)|-vl\d|qwen\d(\.\d)?-?vl"
    r"|mistral-7b-instruct-v0\.[12]|moderation|-base([-_.]|$)|distill"
)


def guess_tools(model_id: str) -> bool:
    """Kann das Modell vermutlich Werkzeuge aufrufen? Im Zweifel ``False``."""
    lowered = (model_id or "").lower()
    if _NOT_TOOLS.search(lowered):
        return False
    return bool(_TOOLS.search(lowered))


# Hauptart (``AIModel.Capability``, hier als Text ohne Django-Import) aus
# eindeutigen ID-Mustern; Reihenfolge zählt, erstes passendes Muster gewinnt,
# sonst ``chat``. OpenAI: gpt-image-*/dall-e (Bilderzeugung, M9),
# whisper/*-transcribe (Spracherkennung, M10), tts-*/*-tts (Sprachausgabe,
# M10); Google: Gemini-Bildmodelle (gemini-*-image, „Nano Banana“) und das
# abgekündigte imagen (Bild), Lyria (Musik, M11).
CHAT, IMAGE, EMBEDDING, STT, TTS, MUSIC = "chat", "image", "embedding", "stt", "tts", "music"
# Reine Texterkennung (olmOCR, *-ocr): nur für die OCR der Dokumentsuche, nicht im Chat.
OCR = "ocr"
_CAPABILITY_PATTERNS = [
    (re.compile(r"embed"), EMBEDDING),
    (re.compile(r"olmocr|(^|[-_./])ocr([-_./\d]|$)"), OCR),
    (re.compile(r"(^|[-_./])tts([-_.]|$)"), TTS),
    (re.compile(r"whisper|transcribe"), STT),
    (
        re.compile(r"dall-e|gpt-image|chatgpt-image|(^|/)imagen|gemini-[\w.-]*-image|nano-banana"),
        IMAGE,
    ),
    (re.compile(r"lyria|musicgen|(^|[-_./])music([-_.]|$)"), MUSIC),
]


def guess_capability(model_id: str) -> str:
    """Hauptart aus eindeutigen ID-Mustern, sonst ``chat``."""
    lowered = (model_id or "").lower()
    for pattern, capability in _CAPABILITY_PATTERNS:
        if pattern.search(lowered):
            return capability
    return CHAT


@dataclass(frozen=True)
class Detected:
    """Erkannte Fähigkeiten eines Modells; ``source`` ``lmstudio`` oder ``heuristik``."""

    capability: str
    tools: bool
    vision: bool
    source: str


def guess(model_id: str) -> Detected:
    """Fähigkeiten aus der Modell-ID (Rückfallebene)."""
    capability = guess_capability(model_id)
    chat = capability == CHAT
    return Detected(
        capability=capability,
        tools=chat and guess_tools(model_id),
        vision=(chat and guess_vision(model_id)) or capability == OCR,
        source="heuristik",
    )


def from_lmstudio(item: dict) -> Detected | None:
    """Ein Eintrag aus LM Studios ``GET /api/v0/models``.

    Felder (lmstudio.ai/docs/developer/rest/endpoints, Changelog 0.3.16):
    ``type`` ``llm``/``vlm``/``embeddings`` und ``capabilities`` als Liste,
    z. B. ``["tool_use"]``. Ohne ``type`` ``None`` (dann gilt die Heuristik);
    ältere Versionen ohne ``capabilities``: Werkzeuge nach Heuristik.
    """
    if not isinstance(item, dict):
        return None
    kind = item.get("type")
    if kind not in ("llm", "vlm", "embeddings"):
        return None
    if kind == "embeddings":
        return Detected(capability=EMBEDDING, tools=False, vision=False, source="lmstudio")
    if guess_capability(str(item.get("id") or "")) == OCR:
        # LM Studio leitet ``tool_use`` aus dem Chat-Template ab; olmOCR (auf Basis
        # von Qwen2.5-VL) kann so als werkzeugfähig gelten. Ein OCR-Modell ruft
        # Werkzeuge aber nicht zuverlässig auf und gehört nicht in die Chat-Auswahl.
        return Detected(capability=OCR, tools=False, vision=True, source="lmstudio")
    caps = item.get("capabilities")
    if isinstance(caps, list):
        tools = "tool_use" in caps
    else:
        tools = guess_tools(str(item.get("id") or ""))
    return Detected(capability=CHAT, tools=tools, vision=kind == "vlm", source="lmstudio")


# Temperatur (Kreativität, chat/creativity.py). Stand der Herstellerangaben,
# geprüft 2026-10-10. Nicht senden, wenn der Anbieter sie ablehnt (HTTP 400),
# ignoriert oder ausdrücklich davon abrät:
#
# - OpenAI (developers.openai.com/api/docs/guides/reasoning und
#   …/guides/latest-model: „When reasoning effort is not `none`, remove
#   `temperature`, `top_p`, and `top_logprobs`“): o1/o3/o4 nur mit Standard;
#   gpt-5 (auch mini/nano) denkt standardmäßig (medium) und lehnt sie ab. Ab
#   gpt-5.1 nur mit ``reasoning_effort: none`` erlaubt, nicht bei allen
#   Modellen (GPT-6 Astra, GPT-6.1 Sol kennen kein ``none``). MultiGPT setzt
#   keinen Reasoning-Aufwand, daher die ganze Familie gpt-5 und neuer ohne
#   Temperatur. gpt-4o, gpt-4.1 usw. nehmen sie an.
# - Anthropic (platform.claude.com, Migrationsleitfäden Opus 5.5, Sonnet 5.5,
#   Haiku 5.5: „Setting temperature, top_p, or top_k to any non-default value on
#   Claude Opus 4.7 and later models … returns a 400 error“; ebenso Sonnet 5,
#   Sonnet 5.5, Haiku 5.5, Fable und Mythos): ab claude-opus-4-7 bzw. allen
#   Claude-5-Modellen nicht. Mit Extended Thinking (``thinking``) generell nicht
#   (Doku „Extended thinking“: nicht mit temperature/top_k kombinierbar). Bis
#   Sonnet 4.6, Haiku 4.5, Opus 4.6 erlaubt (nur nicht zusammen mit top_p, das
#   MultiGPT nicht setzt).
# - Google (ai.google.dev/gemini-api/docs/gemini-3: „For all Gemini 3 models,
#   we strongly recommend keeping the temperature parameter at its default
#   value of 1.0“, sonst Schleifen oder schlechtere Ergebnisse): Gemini 3 und
#   neuer nicht; Gemini 1.5/2.x ja.
# - Lokal (LM Studio/llama.cpp, Ollama): immer erlaubt.
#
# Die Regel wirkt nach Modell-ID (auch über OpenRouter u. Ä. mit Präfix
# ``openai/``, ``anthropic/`` usw.). Lehnt ein Anbieter die Temperatur trotzdem
# ab, wiederholt services.py ohne und merkt sich das Modell (creativity.py).
_NO_TEMPERATURE = re.compile(
    # OpenAI: o-Serie, GPT-5 und neuer
    r"(^|/)o[1-9]([-_.]|$)"
    r"|(^|/)gpt-([5-9]|\d{2})([-_.]|$)"
    # Anthropic: Opus 4.7+ und alle Claude-5-Familien (auch Bedrock-IDs)
    r"|claude-opus-4-[7-9]([-_.@]|$)"
    r"|claude-(opus|sonnet|haiku)-([5-9]|\d{2})([-_.@]|$)"
    r"|claude-(fable|mythos)"
    # Google: Gemini 3 und neuer
    r"|(^|/)(models/)?gemini-([3-9]|\d{2})([-_.]|$)"
)


def accepts_temperature(model_id: str, provider: str = "", *, thinking: bool = False) -> bool:
    """Darf ``temperature`` an das Modell gehen? ``provider``: ``Provider.kind``
    (``openai_compat``, ``anthropic``, ``google``); ``thinking``: Anfrage mit
    Extended Thinking (Anthropic). Im Zweifel ja – Ablehnungen fängt der
    Wiederholversuch ohne Temperatur ab."""
    if thinking:
        return False
    return not _NO_TEMPERATURE.search((model_id or "").lower())
