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
#   gpt-5 (auch mini/nano) denkt immer (kleinste Stufe ``minimal``) und lehnt
#   sie ab. Ab gpt-5.1 nur mit ``reasoning_effort: none`` erlaubt, nicht bei
#   allen Modellen (GPT-6 Astra, GPT-6.1 Sol kennen kein ``none``). Ohne
#   gewählte Denktiefe (``reasoning``) setzt MultiGPT keinen Aufwand, dann die
#   ganze Familie gpt-5 und neuer ohne Temperatur; mit Denktiefe „Aus“, die
#   auf ``none`` abgebildet wird (``reasoning_params``), mit. gpt-4o, gpt-4.1
#   usw. nehmen sie an.
# - Anthropic (platform.claude.com, Migrationsleitfäden Opus 5.5, Sonnet 5.5,
#   Haiku 5.5: „Setting temperature, top_p, or top_k to any non-default value on
#   Claude Opus 4.7 and later models … returns a 400 error“; ebenso Sonnet 5,
#   Sonnet 5.5, Haiku 5.5, Fable und Mythos): ab claude-opus-4-7 bzw. allen
#   Claude-5-Modellen nicht. Mit Thinking (``thinking`` adaptive/enabled, also
#   jede Denktiefe außer „Aus“) generell nicht (Doku „Extended thinking“: nicht
#   mit temperature/top_k kombinierbar). Bis Sonnet 4.6, Haiku 4.5, Opus 4.6
#   ohne Thinking erlaubt (nur nicht zusammen mit top_p, das MultiGPT nicht setzt).
# - Google (ai.google.dev/gemini-api/docs/gemini-3: „For all Gemini 3 models,
#   we strongly recommend keeping the temperature parameter at its default
#   value of 1.0“, sonst Schleifen oder schlechtere Ergebnisse): Gemini 3 und
#   neuer nicht; Gemini 1.5/2.x ja (auch mit thinkingBudget).
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


def accepts_temperature(
    model_id: str, provider: str = "", *, thinking: bool = False, reasoning: str | None = None
) -> bool:
    """Darf ``temperature`` an das Modell gehen? ``provider``: ``Provider.kind``
    (``openai_compat``, ``anthropic``, ``google``); ``thinking``: Anfrage mit
    Thinking (Anthropic); ``reasoning``: Denktiefe, die mitgeht (Stufe aus
    ``REASONING_LEVELS``, ``None`` = keine). Im Zweifel ja – Ablehnungen fängt
    der Wiederholversuch ohne Temperatur ab."""
    if thinking:
        return False
    rule = _reasoning_rule(model_id, provider) if reasoning else None
    level = reasoning_level(reasoning, rule.levels) if rule is not None else None
    if level is not None:
        if rule.style in (_CLAUDE_EFFORT, _CLAUDE_BUDGET) and level != OFF:
            return False  # Thinking an
        if rule.style == _OPENAI and level == OFF and rule.off == "none":
            return True  # gpt-5.1+: reasoning_effort none erlaubt temperature
    return not _NO_TEMPERATURE.search((model_id or "").lower())


# Denktiefe (Reasoning, chat/reasoning.py). Stufen von MultiGPT, aufsteigend;
# „Aus“ heißt in der Oberfläche „Aus/minimal“, weil manche Modelle nur eine
# kleinste Stufe kennen. ``xhigh`` und ``max`` gibt es, weil OpenAI (ab
# gpt-5.2 bzw. gpt-5.6/GPT-6) und Anthropic (ab Opus 4.6) sie anbieten.
OFF, LOW, MEDIUM, HIGH, XHIGH, MAX = "off", "low", "medium", "high", "xhigh", "max"
REASONING_LEVELS = (OFF, LOW, MEDIUM, HIGH, XHIGH, MAX)

# Abbildung je Anbieter. Stand der Herstellerangaben, geprüft 2026-10-10:
#
# - OpenAI, Chat Completions ``reasoning_effort`` (developers.openai.com/api/
#   docs/guides/reasoning: „Supported values are model-dependent and can include
#   `none`, `minimal`, `low`, `medium`, `high`, `xhigh`, and `max`“; Modellseiten
#   …/api/docs/models/<id>): o1/o3/o4-mini low–high (o1-mini/-preview ohne);
#   gpt-5 (mini/nano) minimal–high; gpt-5.1 none–high; gpt-5.2 bis 5.5
#   none–xhigh; gpt-5.6 none–max; GPT-6 Luna none–max; GPT-6 Astra und
#   GPT-6.1 Sol low–max („does not support `none`“, Sol auch kein
#   ``minimal``). *-chat-latest sind keine Reasoning-Modelle. „Aus“ = ``none``
#   bzw. bei gpt-5 ``minimal``.
# - Anthropic (platform.claude.com/docs/en/build-with-claude/effort und
#   …/thinking-troubleshooting, Tabelle „Thinking support, defaults, and
#   rejected configurations by model“): ``output_config.effort`` (low, medium,
#   high, xhigh, max; xhigh ab Opus 4.7/Sonnet 5/Haiku 5.5, nicht Opus 4.6,
#   Sonnet 4.6, Mythos Preview). Thinking adaptiv: Fable, Mythos, Opus 5.5
#   immer an (kein „Aus“, ``disabled`` -> 400); Opus 5, Sonnet 5, Haiku 5.5
#   standardmäßig an, „Aus“ = ``thinking: {"type": "disabled"}``; Sonnet 5.5
#   „Aus“ = ``{"type": "between_tools"}`` (``disabled`` -> 400); Opus 4.6–4.8,
#   Sonnet 4.6 standardmäßig aus, Stufe = ``thinking: {"type": "adaptive"}`` plus
#   effort. ``budget_tokens`` („extended thinking“) ab Claude 4.7 abgelehnt
#   (400), auf 4.6 veraltet; nur noch für Opus/Sonnet/Haiku 4.5, Opus 4/4.1,
#   Sonnet 4 und Claude 3.7 Sonnet: ``thinking: {"type": "enabled",
#   "budget_tokens": n}`` (mindestens 1024, kleiner als ``max_tokens``).
#   Bei xhigh/max empfiehlt die Doku ein großes ``max_tokens`` (64k).
# - Google, ``generationConfig.thinkingConfig`` (ai.google.dev/gemini-api/docs/
#   gemini-3 und firebase.google.com/docs/ai-logic/thinking, beide „Last
#   updated 2026-10-09“): Gemini 3 ``thinkingLevel`` MINIMAL/LOW/MEDIUM/HIGH
#   („minimal does not guarantee that thinking is off“), 3 Pro nur low/high,
#   3.1 Pro ohne minimal, 3.7/3.8 Flash ohne minimal (400). Gemini 2.5
#   ``thinkingBudget`` (Pro 128–32768, nicht abschaltbar; Flash 0–24576, Flash-
#   Lite 512–24576, 0 = aus). Beides zusammen -> 400.
# - Lokal bzw. OpenAI-kompatibel: gpt-oss kennt ``reasoning_effort`` low/medium/
#   high über sein Chat-Template (Modellkarte developers.openai.com/api/docs/
#   models/gpt-oss-120b; LM Studio ab 0.4.8 auf v1/chat/completions,
#   lmstudio.ai/changelog/lmstudio-v0.4.8). Andere lokale Modelle: nicht senden.
#
# Nicht unterstützte Stufen werden je Modell abgebildet (``reasoning_level``):
# „Aus“ auf die kleinste Stufe, sonst auf die nächstkleinere, notfalls die
# nächstgrößere. Lehnt ein Anbieter den Parameter trotzdem ab, wiederholt
# services.py ohne und merkt sich das Modell (reasoning.py).
_OPENAI, _CLAUDE_EFFORT, _CLAUDE_BUDGET = "openai", "claude_effort", "claude_budget"
_GEMINI_LEVEL, _GEMINI_BUDGET = "gemini_level", "gemini_budget"


@dataclass(frozen=True)
class _Reasoning:
    levels: tuple[str, ...]
    style: str
    # Wert für „Aus“: OpenAI ``none``/``minimal``; Claude ``disabled``,
    # ``between_tools`` oder leer (Standard ist aus, nichts senden).
    off: str = ""
    # Claude: Thinking ist standardmäßig aus und wird für eine Stufe eingeschaltet.
    adaptive: bool = False


_E = (LOW, MEDIUM, HIGH)
_OPENAI_RULES = [
    (r"(^|/)gpt-[\d.]+-chat|(^|/)o1-(mini|preview)", None),
    (r"(^|/)o[1-9]([-_.]|$)", _Reasoning(_E, _OPENAI)),
    (r"(^|/)gpt-5([-_]|$)", _Reasoning((OFF, *_E), _OPENAI, "minimal")),
    (r"(^|/)gpt-5\.1([-_]|$)", _Reasoning((OFF, *_E), _OPENAI, "none")),
    (r"(^|/)gpt-5\.[2-5]([-_]|$)", _Reasoning((OFF, *_E, XHIGH), _OPENAI, "none")),
    (r"(^|/)gpt-5\.([6-9]|\d{2})([-_]|$)", _Reasoning(REASONING_LEVELS, _OPENAI, "none")),
    (r"(^|/)gpt-([6-9]|\d{2})(\.\d+)?-luna", _Reasoning(REASONING_LEVELS, _OPENAI, "none")),
    (r"(^|/)gpt-([6-9]|\d{2})([-_.]|$)", _Reasoning((*_E, XHIGH, MAX), _OPENAI)),
    (r"gpt-oss", _Reasoning(_E, _OPENAI)),
]
_CLAUDE_ALL = (*_E, XHIGH, MAX)
_V = r"([-_.@]|$)"  # Ende der Versionsangabe (auch Bedrock-IDs mit Datum bzw. @)
_ANTHROPIC_RULES = [
    (r"claude-(fable|mythos)-([5-9]|\d{2})", _Reasoning(_CLAUDE_ALL, _CLAUDE_EFFORT)),
    (r"claude-mythos-preview", _Reasoning((*_E, MAX), _CLAUDE_EFFORT)),
    (r"claude-opus-5-5" + _V, _Reasoning(_CLAUDE_ALL, _CLAUDE_EFFORT)),
    (r"claude-sonnet-5-5" + _V, _Reasoning((OFF, *_CLAUDE_ALL), _CLAUDE_EFFORT, "between_tools")),
    (r"claude-(opus|sonnet)-5" + _V, _Reasoning((OFF, *_CLAUDE_ALL), _CLAUDE_EFFORT, "disabled")),
    (r"claude-haiku-5-5" + _V, _Reasoning((OFF, *_CLAUDE_ALL), _CLAUDE_EFFORT, "disabled")),
    # Neuere Familien: wie Opus 5.5 (Thinking immer an), bis die Doku anderes sagt.
    (r"claude-(opus|sonnet|haiku)-([5-9]|\d{2})", _Reasoning(_CLAUDE_ALL, _CLAUDE_EFFORT)),
    (
        r"claude-opus-4-[7-9]" + _V,
        _Reasoning((OFF, *_CLAUDE_ALL), _CLAUDE_EFFORT, adaptive=True),
    ),
    (
        r"claude-(opus|sonnet)-4-6" + _V,
        _Reasoning((OFF, *_E, MAX), _CLAUDE_EFFORT, adaptive=True),
    ),
    (
        r"claude-(opus|sonnet|haiku)-4-5"
        + _V
        + r"|claude-(opus|sonnet)-4"
        + _V
        + r"|claude-3-7-sonnet",
        _Reasoning((OFF, *_E), _CLAUDE_BUDGET),
    ),
]
_GOOGLE_RULES = [
    (r"gemini-2\.5-pro", _Reasoning(_E, _GEMINI_BUDGET)),
    (r"gemini-2\.5-flash", _Reasoning((OFF, *_E), _GEMINI_BUDGET)),
    (r"gemini-3-pro", _Reasoning((LOW, HIGH), _GEMINI_LEVEL)),
    (r"gemini-3\.[7-9]-flash(?!-lite)", _Reasoning(_E, _GEMINI_LEVEL)),
    (r"gemini-3(\.\d+)?-flash", _Reasoning((OFF, *_E), _GEMINI_LEVEL)),
    (r"gemini-([3-9]|\d{2})([-_.]|$)", _Reasoning(_E, _GEMINI_LEVEL)),
]
_RULES = {
    "openai_compat": [(re.compile(p), r) for p, r in _OPENAI_RULES],
    "anthropic": [(re.compile(p), r) for p, r in _ANTHROPIC_RULES],
    "google": [(re.compile(p), r) for p, r in _GOOGLE_RULES],
}

# Claude: Budgets für „extended thinking“ und ``max_tokens`` (Pflichtfeld,
# Standard des Adapters 16000; das Budget kommt obendrauf, höchstens 32000 =
# Ausgabegrenze von Opus 4/4.1). Gemini 2.5: ``thinkingBudget`` je Stufe.
_CLAUDE_MAX_TOKENS = 16000
_CLAUDE_MAX_TOKENS_LARGE = 64000  # xhigh/max laut Doku
_CLAUDE_BUDGETS = {LOW: 2048, MEDIUM: 8000, HIGH: 16000}
_GEMINI_BUDGETS = {LOW: 1024, MEDIUM: 8192, HIGH: 24576}


def _reasoning_rule(model_id: str, provider: str = "") -> _Reasoning | None:
    lowered = (model_id or "").lower()
    if not provider:
        if "claude" in lowered:
            provider = "anthropic"
        elif "gemini" in lowered:
            provider = "google"
        else:
            provider = "openai_compat"
    for pattern, rule in _RULES.get(provider, []):
        if pattern.search(lowered):
            return rule
    return None


def reasoning_support(model_id: str, provider: str = "") -> tuple[str, ...] | None:
    """Stufen der Denktiefe, die das Modell kennt (Teilmenge von
    ``REASONING_LEVELS``), oder ``None`` (kein einstellbares Reasoning)."""
    rule = _reasoning_rule(model_id, provider)
    return rule.levels if rule is not None else None


def reasoning_level(level: str | None, supported) -> str | None:
    """Gewählte Stufe auf die unterstützten abbilden (siehe oben); ``None``,
    wenn nichts gewählt ist oder das Modell keine Stufen kennt."""
    if not level or not supported or level not in REASONING_LEVELS:
        return None
    if level in supported:
        return level
    order = REASONING_LEVELS.index
    lowest = min(supported, key=order)
    if level == OFF:
        return lowest
    below = [s for s in supported if s != OFF and order(s) < order(level)]
    return max(below, key=order) if below else lowest


def reasoning_params(model_id: str, provider: str, level: str | None) -> dict:
    """Parameter für ``adapter.stream`` (anbieterspezifisch, siehe oben) oder ``{}``."""
    rule = _reasoning_rule(model_id, provider)
    if rule is None:
        return {}
    level = reasoning_level(level, rule.levels)
    if level is None:
        return {}
    if rule.style == _OPENAI:
        return {"reasoning_effort": rule.off if level == OFF else level}
    if rule.style == _CLAUDE_EFFORT:
        if level == OFF:
            return {"thinking": {"type": rule.off}} if rule.off else {}
        params: dict = {"output_config": {"effort": level}}
        if rule.adaptive:
            params["thinking"] = {"type": "adaptive"}
        if level in (XHIGH, MAX):
            params["max_tokens"] = _CLAUDE_MAX_TOKENS_LARGE
        return params
    if rule.style == _CLAUDE_BUDGET:
        if level == OFF:
            return {}
        budget = _CLAUDE_BUDGETS[level]
        return {
            "thinking": {"type": "enabled", "budget_tokens": budget},
            "max_tokens": _CLAUDE_MAX_TOKENS + budget,
        }
    if rule.style == _GEMINI_LEVEL:
        return {"thinkingConfig": {"thinkingLevel": "MINIMAL" if level == OFF else level.upper()}}
    # _GEMINI_BUDGET
    return {"thinkingConfig": {"thinkingBudget": 0 if level == OFF else _GEMINI_BUDGETS[level]}}
