"""Zerteilung in überlappende Abschnitte mit Seitenzahl (M7-03, Agent ingest).

Tokenzählung als Schätzung statt mit ``tiktoken``: tiktoken bringt eine
Rust-Erweiterung mit und lädt seine BPE-Tabellen beim ersten Gebrauch aus dem
Internet nach (im gehärteten Dienst ohne Schreibrecht/Cache unpraktisch). Für
die Zerteilung reicht eine Schätzung: ca. 4 Zeichen je Token (OpenAI nennt
diese Faustregel für englischen Text; deutscher Text liegt ähnlich, lange
Komposita werden durch die Zeichenzählung mit erfasst). Selbst ein Fehler um
den Faktor 2 bleibt bei 800 Tokens weit unter der Eingabegrenze der
Embedding-Modelle (8191 Tokens).
"""

import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass

from .extract import Page

CHARS_PER_TOKEN = 4
# Längere "Wörter" (Base64, URLs, Tabellenreste ohne Leerzeichen) werden
# zerlegt, damit ein Abschnitt seine Größe einhält.
MAX_WORD_CHARS = 100
# Ein Abschnitt endet bevorzugt an Absatz- oder Satzende, wenn das im letzten
# Teil des Fensters liegt (ab diesem Anteil der Zielgröße).
BOUNDARY_FROM = 0.6

_SEGMENT_RE = re.compile(r"\S+\s*")
_SENTENCE_END_RE = re.compile(r"[.!?:;…][\"'»«“”)\]]*\s*$")


@dataclass(frozen=True)
class TextChunk:
    position: int
    text: str
    page: int | None
    tokens: int


@dataclass(frozen=True)
class _Segment:
    text: str
    page: int | None
    tokens: int


def estimate_tokens(text: str) -> int:
    """Geschätzte Tokenzahl eines Textes."""
    return sum(_word_tokens(m.group().rstrip()) for m in _SEGMENT_RE.finditer(text))


def _word_tokens(word: str) -> int:
    return max(1, math.ceil(len(word) / CHARS_PER_TOKEN))


def _segments(pages: Iterable[Page]) -> list[_Segment]:
    segments: list[_Segment] = []
    for page in pages:
        if not page.text.strip():
            continue
        if segments:
            # Seitenwechsel wie ein Absatz behandeln.
            last = segments[-1]
            segments[-1] = _Segment(last.text.rstrip() + "\n\n", last.page, last.tokens)
        for match in _SEGMENT_RE.finditer(page.text):
            piece = match.group()
            word = piece.rstrip()
            space = piece[len(word) :]
            while len(word) > MAX_WORD_CHARS:
                head, word = word[:MAX_WORD_CHARS], word[MAX_WORD_CHARS:]
                segments.append(_Segment(head + " ", page.number, _word_tokens(head)))
            segments.append(_Segment(word + space, page.number, _word_tokens(word)))
    return segments


def _is_boundary(segment: _Segment) -> tuple[bool, bool]:
    """(Absatzende, Satzende)"""
    paragraph = "\n\n" in segment.text or segment.text.endswith("\n")
    return paragraph, bool(_SENTENCE_END_RE.search(segment.text))


def _choose_end(segments: list[_Segment], start: int, limit: int, chunk_tokens: int) -> int:
    """Exklusives Ende des Abschnitts ab ``start`` (mindestens ein Segment)."""
    total = 0
    end = start
    while end < len(segments) and (end == start or total + segments[end].tokens <= chunk_tokens):
        total += segments[end].tokens
        end += 1
    if end >= len(segments):
        return end
    # Rückwärts nach einer natürlichen Grenze suchen: erst Absatz, dann Satz.
    minimum = BOUNDARY_FROM * chunk_tokens
    best_sentence = None
    running = total
    for i in range(end - 1, start, -1):
        if running < minimum:
            break
        paragraph, sentence = _is_boundary(segments[i])
        if paragraph:
            return i + 1
        if sentence and best_sentence is None:
            best_sentence = i + 1
        running -= segments[i].tokens
    return best_sentence or end


def split_pages(
    pages: Iterable[Page], chunk_tokens: int = 800, overlap_tokens: int = 100
) -> list[TextChunk]:
    """Text aller Seiten in Abschnitte von ca. ``chunk_tokens`` Tokens teilen.

    Aufeinanderfolgende Abschnitte überlappen um ca. ``overlap_tokens``. Die
    Seite eines Abschnitts ist die, auf der die meisten seiner Tokens liegen
    (bei Gleichstand die erste).
    """
    if chunk_tokens < 1:
        raise ValueError("chunk_tokens muss positiv sein")
    overlap_tokens = max(0, min(overlap_tokens, chunk_tokens // 2))
    segments = _segments(pages)
    chunks: list[TextChunk] = []
    start = 0
    limit = len(segments)
    while start < limit:
        end = _choose_end(segments, start, limit, chunk_tokens)
        part = segments[start:end]
        text = "".join(s.text for s in part).strip()
        if text:
            weights: Counter = Counter()
            for s in part:
                weights[s.page] += s.tokens
            best = max(weights.values())
            page = next(s.page for s in part if weights[s.page] == best)
            chunks.append(TextChunk(len(chunks), text, page, sum(s.tokens for s in part)))
        if end >= limit:
            break
        # Überlappung: so viele Segmente vom Ende wiederholen, bis overlap erreicht.
        next_start = end
        carried = 0
        while (
            next_start > start + 1 and carried + segments[next_start - 1].tokens <= overlap_tokens
        ):
            next_start -= 1
            carried += segments[next_start].tokens
        start = next_start
    return chunks
