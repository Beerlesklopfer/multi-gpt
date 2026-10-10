"""Kontext schützen (M4a-12): Prompt-Tokens vor dem Senden schätzen, Kontextlänge
des Modells kennen, notfalls den älteren Verlauf kürzen.

Entscheidungen:

- **Schätzung statt Tokenizer:** Je Anbieter und Modell zählt ein anderer
  Tokenizer; ein exakter Zähler bräuchte je Familie eine Bibliothek bzw. einen
  Anbieteraufruf. Grob nach Zeichenart: Buchstaben und Leerraum 4,5 Zeichen je
  Token, Ziffern 1,1, übrige Zeichen 1,8 (JSON-Ergebnisse mit IDs und Zeitstempeln
  sind viel dichter als Fließtext). Werkzeuge: Name und Beschreibung wie Text,
  das Parameter-Schema 8 Zeichen je Token (Anbieter setzen es kompakt um, z. B.
  gpt-oss als TypeScript-Typen), dazu ein Zuschlag je Werkzeug, Nachricht und
  Bild. Kalibriert 2026-10-10 an gpt-oss-20b über LM Studio: deutscher Text
  1 534 geschätzt / 1 320 echt, JSON-Ergebnis 3 564 / 3 480, Home Assistant (8
  Werkzeuge) 654 / 574, n8n (54) 11 884 / 8 011. Lieber etwas darüber:
  Es soll vor dem Fehler greifen, nicht danach.
- **Kontextlänge:** Meldet der Anbieter sie (LM Studio ``/api/v0/models``:
  ``loaded_context_length`` des geladenen Modells, gespeichert bei der
  Statusprüfung in ``Provider.reported_context``), gilt sie; sonst die
  Angabe am Modell (``AIModel.context_length``, Admin). Unbekannt = keine
  Prüfung vor dem Senden (Cloud-Modelle mit großem Kontext).
- **Reserve für die Antwort:** ein Viertel des Kontexts, mindestens 2048 und
  höchstens 8192 Tokens – Reasoning-Modelle (gpt-oss) denken vor der Antwort
  und brauchen Platz dafür.
- **Kürzen (``trim_history``):** nur Nachrichten *vor* der aktuellen Frage, von
  vorn, immer in ganzen Einheiten (Assistant-Nachricht mit Werkzeugaufrufen samt
  Ergebnissen), und der Verlauf beginnt danach wieder mit einer Nutzernachricht.
  Die Frage selbst und die Runden der laufenden Antwort bleiben.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable

from .providers.base import ChatMessage, ToolSpec

# Zeichen je Token nach Zeichenart (siehe Moduldoku).
WORD_CHARS_PER_TOKEN = 4.5
DIGIT_CHARS_PER_TOKEN = 1.1
OTHER_CHARS_PER_TOKEN = 1.8
SCHEMA_CHARS_PER_TOKEN = 8
MESSAGE_OVERHEAD = 4
TOOL_OVERHEAD = 10
IMAGE_TOKENS = 1000

RESERVE_SHARE = 4  # ein Viertel des Kontexts für die Antwort
MIN_RESERVE = 2048
MAX_RESERVE = 8192


_DIGITS = re.compile(r"\d")
_OTHER = re.compile(r"[^\w\s]|_")


def text_tokens(text: str | None) -> int:
    if not text:
        return 0
    digits = len(_DIGITS.findall(text))
    other = len(_OTHER.findall(text))
    words = len(text) - digits - other
    return math.ceil(
        words / WORD_CHARS_PER_TOKEN
        + digits / DIGIT_CHARS_PER_TOKEN
        + other / OTHER_CHARS_PER_TOKEN
    )


def spec_tokens(spec: ToolSpec) -> int:
    """Geschätzte Tokens eines Werkzeugs (Name, Beschreibung, Parameter-Schema)."""
    try:
        schema = json.dumps(spec.parameters or {}, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        schema = str(spec.parameters)
    return (
        TOOL_OVERHEAD
        + text_tokens(f"{spec.name or ''} {spec.description or ''}")
        + math.ceil(len(schema) / SCHEMA_CHARS_PER_TOKEN)
    )


def specs_tokens(specs: Iterable[ToolSpec]) -> int:
    return sum(spec_tokens(s) for s in specs)


def message_tokens(message: ChatMessage) -> int:
    total = MESSAGE_OVERHEAD + text_tokens(message.content)
    for call in message.tool_calls or []:
        try:
            args = json.dumps(call.arguments or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            args = str(call.arguments)
        total += MESSAGE_OVERHEAD + text_tokens(call.name) + text_tokens(args)
    total += IMAGE_TOKENS * len(message.images or [])
    return total


def estimate(system: str | None, history: list[ChatMessage], specs=()) -> int:
    """Geschätzte Prompt-Tokens einer Anfrage (System, Verlauf, Werkzeuge)."""
    return text_tokens(system) + sum(message_tokens(m) for m in history) + specs_tokens(specs)


def context_length(ai_model) -> int | None:
    """Kontextlänge in Tokens: Meldung des Anbieters (LM Studio), sonst Admin-Angabe."""
    provider = getattr(ai_model, "provider", None)
    reported = (getattr(provider, "reported_context", None) or {}).get(ai_model.model_id)
    if isinstance(reported, int) and reported > 0:
        return reported
    return ai_model.context_length or None


def reserve(context: int) -> int:
    """Tokens, die für die Antwort (samt Nachdenken) frei bleiben sollen."""
    return min(MAX_RESERVE, max(MIN_RESERVE, context // RESERVE_SHARE))


def prompt_budget(context: int | None) -> int | None:
    """Höchstens so viele Prompt-Tokens; ``None`` bei unbekannter Kontextlänge."""
    if not context:
        return None
    return max(context - reserve(context), context // 2)


def _question_index(history: list[ChatMessage]) -> int:
    for index in range(len(history) - 1, -1, -1):
        if history[index].role == "user":
            return index
    return 0


def trim_history(history: list[ChatMessage], fixed_tokens: int, budget: int) -> int:
    """Ältere Nachrichten vor der aktuellen Frage entfernen, bis
    ``fixed_tokens`` (System, Werkzeuge) plus Verlauf in ``budget`` passt.

    Ändert ``history`` an Ort und Stelle; liefert die Zahl entfernter Nachrichten.
    """
    removed = 0
    total = fixed_tokens + sum(message_tokens(m) for m in history)
    while total > budget:
        question = _question_index(history)
        if question == 0:
            break
        # Eine Einheit von vorn: erste Nachricht plus nachfolgende Tool-Ergebnisse,
        # danach bis zur nächsten Nutzernachricht (der Verlauf beginnt mit „user“).
        end = 1
        while end < question and history[end].role != "user":
            end += 1
        for message in history[:end]:
            total -= message_tokens(message)
        del history[:end]
        removed += end
    return removed


def format_tokens(value: int) -> str:
    """25743 -> „25 743“ (Tausender mit Leerzeichen)."""
    return f"{int(value):,}".replace(",", " ")
