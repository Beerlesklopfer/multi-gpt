"""Zerteilung in überlappende Abschnitte mit Fundstelle (Seite, Absatz) (M7-03, Agent ingest).

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
from collections.abc import Iterable
from dataclasses import dataclass, replace
from pathlib import PurePosixPath

from .extract import ALLOWED_EXTENSIONS, Page

CHARS_PER_TOKEN = 4
# Längere "Wörter" (Base64, URLs, Tabellenreste ohne Leerzeichen) werden
# zerlegt, damit ein Abschnitt seine Größe einhält.
MAX_WORD_CHARS = 100
# Ein Abschnitt endet bevorzugt an Absatz- oder Satzende, wenn das im letzten
# Teil des Fensters liegt (ab diesem Anteil der Zielgröße).
BOUNDARY_FROM = 0.6

_SEGMENT_RE = re.compile(r"\S+\s*")
_SENTENCE_END_RE = re.compile(r"[.!?:;…][\"'»«“”)\]]*\s*$")
# Absatzgrenze: Leerzeile (auch mit Leerraum darin).
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n\s*")
# Längste Zeile des Kontextkopfs (Titel bzw. Pfad), damit der Kopf den
# Abschnitt beim Einbetten nicht verdrängt.
MAX_HEADING_LINE = 300
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class TextChunk:
    """Ein Abschnitt mit Fundstelle von–bis.

    ``page``/``page_end``: erste und letzte Seite (None bei Formaten ohne
    Seiten). ``paragraph``/``paragraph_end``: Absatz des ersten bzw. letzten
    Segments (ab 1, je Seite neu gezählt; ohne Seiten durchgehend). Wegen der
    Überlappung kann der Abschnitt mitten im Startabsatz beginnen.
    ``section``/``section_title``: Nummer und Titel der Gliederungsüberschrift,
    die am Abschnittsbeginn gilt („7.5.3“, „Lenkung dokumentierter
    Information“); ``section_end``: Nummer am Ende, nur wenn sie abweicht.
    """

    position: int
    text: str
    page: int | None
    tokens: int
    page_end: int | None = None
    paragraph: int | None = None
    paragraph_end: int | None = None
    section: str = ""
    section_title: str = ""
    section_end: str = ""


@dataclass(frozen=True)
class _Segment:
    text: str
    page: int | None
    tokens: int
    paragraph: int | None = None
    section: str = ""
    section_title: str = ""


def estimate_tokens(text: str) -> int:
    """Geschätzte Tokenzahl eines Textes."""
    return sum(_word_tokens(m.group().rstrip()) for m in _SEGMENT_RE.finditer(text))


def _word_tokens(word: str) -> int:
    return max(1, math.ceil(len(word) / CHARS_PER_TOKEN))


def _paragraphs(text: str) -> list[str]:
    """Absätze einer Seite: durch Leerzeile getrennte Blöcke, leere fallen weg."""
    return [block for block in _PARAGRAPH_RE.split(text) if block.strip()]


# --- Gliederung (Abschnittsnummern) ---------------------------------------------------
#
# Normen, Berichte und Fachbücher sind nummeriert gegliedert („7.5.3 Lenkung
# dokumentierter Information“, „Anhang A“, „A.2.1 …“). Eine Zeile gilt als
# Überschrift, wenn sie
#
# - am Absatzanfang steht oder auf ein Satzende folgt (PDF-Text ohne Leerzeilen),
# - mit einer Nummer ohne Schlusspunkt beginnt („1. Punkt“ ist eine Aufzählung)
#   und danach ein Großbuchstabe folgt,
# - kurz ist und weder mit Satzzeichen noch mit einer Zahl (Seitenzahl im
#   Inhaltsverzeichnis) oder Punktreihe endet,
# - und plausibel auf die vorige Überschrift folgt: nächste Nummer derselben
#   Ebene (eine darf fehlen), erste Nummer einer tieferen Ebene oder Rückkehr
#   zu einer höheren Ebene. So gelten Tabellenzahlen und Aufzählungen
#   („2 Mitarbeiter …“ mitten in 7.5) nicht als Abschnitt.
#
# Inhaltsverzeichnisse werden übersprungen: SECTION_TOC_RUN oder mehr
# nummerierte Zeilen direkt hintereinander (ohne Text dazwischen).

SECTION_MAX_CHARS = 120
SECTION_MAX_WORDS = 15
SECTION_TOC_RUN = 4
SECTION_TITLE_CHARS = 100
ANNEX_BASE = 100  # Anhang A = 100, B = 101 … (nach allen Hauptabschnitten)

_SECTION_NUMBER = re.compile(r"^(\d{1,2}(?:\.\d{1,3}){0,5})\s+(\S.*)$")
_SECTION_ANNEX_NUMBER = re.compile(r"^([A-Z](?:\.\d{1,3}){1,5})\s+(\S.*)$")
_SECTION_ANNEX = re.compile(r"^(?:Anhang|Annex)\s+([A-Z])\b\s*(.*)$")
_SECTION_LOOSE = re.compile(
    r"^(?:\d{1,2}(?:\.\d{1,3}){0,5}|[A-Z](?:\.\d{1,3}){1,5}|(?:Anhang|Annex)\s+[A-Z])\s"
)
_SECTION_BAD_END = re.compile(r"(?:[.,;:!?…]|\d|\.{2,}\s*\S*)$")
_TITLE_START = re.compile(r"^[A-ZÄÖÜ(„\"]")


def _section_candidate(line: str) -> tuple[tuple[int, ...], str, str] | None:
    """(Schlüssel, Nummer, Titel) einer möglichen Überschriftszeile, sonst None."""
    line = " ".join(line.split())
    if not line or len(line) > SECTION_MAX_CHARS or len(line.split()) > SECTION_MAX_WORDS:
        return None
    match = _SECTION_ANNEX.match(line)
    if match:
        letter, title = match.groups()
        if title and _SECTION_BAD_END.search(title):
            return None
        return (ANNEX_BASE + ord(letter) - ord("A"),), f"Anhang {letter}", title
    for pattern in (_SECTION_NUMBER, _SECTION_ANNEX_NUMBER):
        match = pattern.match(line)
        if not match:
            continue
        number, title = match.groups()
        if not _TITLE_START.match(title) or _SECTION_BAD_END.search(title):
            return None
        parts = number.split(".")
        if parts[0].isdigit():
            key = tuple(int(x) for x in parts)
        else:
            key = (ANNEX_BASE + ord(parts[0]) - ord("A"), *(int(x) for x in parts[1:]))
        return key, number, title
    return None


def _plausible_next(current: tuple[int, ...] | None, key: tuple[int, ...]) -> bool:
    """Folgt ``key`` plausibel auf die vorige Überschrift ``current``?"""
    if current is None:
        # Erste Überschrift: 0/1 auf jeder Ebene („0 Einleitung“, „1.1 …“) oder Anhang A.
        return key[0] == ANNEX_BASE or all(x <= 1 for x in key)
    if key[0] >= ANNEX_BASE and current[0] < ANNEX_BASE:
        return key[0] == ANNEX_BASE and all(x <= 1 for x in key[1:])
    if len(key) > len(current) and key[: len(current)] == current:
        return all(x <= 1 for x in key[len(current) :])  # tiefer: 7 -> 7.1
    level = len(key) - 1
    if len(current) > level and key[:level] == current[:level]:
        return current[level] < key[level] <= current[level] + 2  # nächste (eine darf fehlen)
    return False


def find_sections(blocks: list[list[str]]) -> dict[tuple[int, int], tuple[str, str]]:
    """Überschriften in Absätzen (je Absatz die Liste seiner Zeilen).

    Rückgabe: {(Absatzindex, Zeilenindex): (Nummer, Titel)}.
    """
    flat = [
        (bi, li, line.strip()) for bi, lines in enumerate(blocks) for li, line in enumerate(lines)
    ]
    # Inhaltsverzeichnis: lange Folgen nummerierter Zeilen ohne Text dazwischen.
    toc: set[int] = set()
    run: list[int] = []
    for index, (_, _, line) in enumerate([*flat, (-1, -1, "")]):
        if line and _SECTION_LOOSE.match(line):
            run.append(index)
            continue
        if len(run) >= SECTION_TOC_RUN:
            toc.update(run)
        run = []
    found: dict[tuple[int, int], tuple[str, str]] = {}
    current = None
    # Passt eine Überschrift nicht (z. B. weil davor eine nicht erkannt wurde),
    # merkt sie sich als „wartend“; folgt die nächste plausibel auf sie, gelten
    # beide (Wiederaufsetzen). Einzelne Ausreißer bleiben so wirkungslos.
    pending = None
    for index, (bi, li, line) in enumerate(flat):
        if index in toc:
            continue
        if li > 0 and not _SENTENCE_END_RE.search(flat[index - 1][2]):
            continue
        candidate = _section_candidate(line)
        if candidate is None:
            continue
        # Geht der Satz in der nächsten Zeile klein weiter, ist es keine Überschrift.
        following = blocks[bi][li + 1].strip() if li + 1 < len(blocks[bi]) else ""
        if following[:1].islower():
            continue
        key, number, title = candidate
        entry = (number, title[:SECTION_TITLE_CHARS].rstrip())
        if _plausible_next(current, key):
            current, pending = key, None
            found[(bi, li)] = entry
        elif pending is not None and _plausible_next(pending[0], key):
            found[pending[1]] = pending[2]
            current, pending = key, None
            found[(bi, li)] = entry
        else:
            pending = (key, (bi, li), entry)
    return found


def _segments(pages: Iterable[Page]) -> list[_Segment]:
    """Wörter mit Seite, Absatznummer und Gliederungsabschnitt.

    Absätze werden ab 1 gezählt, bei Formaten mit Seiten je Seite neu, sonst
    durchgehend. Das letzte Segment eines Absatzes (und einer Seite) endet
    auf "\n\n" – daran erkennt ``_is_boundary`` die Absatzgrenze.
    """
    blocks: list[tuple[int | None, int, list[str]]] = []
    paragraph = 0
    for page in pages:
        page_blocks = _paragraphs(page.text)
        if not page_blocks:
            continue
        if page.number is not None:
            paragraph = 0
        for block in page_blocks:
            paragraph += 1
            blocks.append((page.number, paragraph, block.strip().split("\n")))
    headings = find_sections([lines for _, _, lines in blocks])

    segments: list[_Segment] = []
    section, section_title = "", ""
    for bi, (number, paragraph, lines) in enumerate(blocks):
        if segments:
            # Absatz- und Seitenwechsel beenden den vorigen Absatz.
            segments[-1] = replace(segments[-1], text=segments[-1].text.rstrip() + "\n\n")
        for li, line in enumerate(lines):
            if (bi, li) in headings:
                section, section_title = headings[(bi, li)]
            text = line + ("\n" if li < len(lines) - 1 else "")
            for match in _SEGMENT_RE.finditer(text):
                piece = match.group()
                word = piece.rstrip()
                space = piece[len(word) :]
                while len(word) > MAX_WORD_CHARS:
                    head, word = word[:MAX_WORD_CHARS], word[MAX_WORD_CHARS:]
                    segments.append(
                        _Segment(
                            head + " ",
                            number,
                            _word_tokens(head),
                            paragraph,
                            section,
                            section_title,
                        )
                    )
                segments.append(
                    _Segment(
                        word + space,
                        number,
                        _word_tokens(word),
                        paragraph,
                        section,
                        section_title,
                    )
                )
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

    Aufeinanderfolgende Abschnitte überlappen um ca. ``overlap_tokens``. Jeder
    Abschnitt trägt seine Fundstelle von–bis: ``page`` ist die erste Seite,
    ``page_end`` die letzte, ``paragraph``/``paragraph_end`` die Absätze des
    ersten und letzten Wortes (siehe ``TextChunk``).
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
            first, last = part[0], part[-1]
            chunks.append(
                TextChunk(
                    len(chunks),
                    text,
                    first.page,
                    sum(s.tokens for s in part),
                    page_end=last.page,
                    paragraph=first.paragraph,
                    paragraph_end=last.paragraph,
                    section=first.section,
                    section_title=first.section_title,
                    section_end=last.section if last.section != first.section else "",
                )
            )
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


# --- Kontextkopf ------------------------------------------------------------------


def _clean(value: str) -> str:
    return " ".join(_CONTROL_RE.sub(" ", value or "").split())


def _without_extension(name: str) -> str:
    """Dateiendung abschneiden, wenn es eine unterstützte ist.

    Wichtig für die Volltextsuche: PostgreSQL liest „Jahresbericht.pdf“ als
    *ein* Token (Hostname), „Jahresbericht“ allein findet es dann nicht.
    """
    suffix = PurePosixPath(name).suffix
    if suffix and suffix.lower() in ALLOWED_EXTENSIONS and len(name) > len(suffix):
        return name[: -len(suffix)]
    return name


def heading(
    title: str,
    source_path: str = "",
    page: int | None = None,
    page_end: int | None = None,
    section: str = "",
    section_title: str = "",
) -> str:
    """Kontextkopf eines Abschnitts: Dokumentname, Ordnerpfad, Seite(n), Gliederung.

    Beispiel::

        Dokument: Jahresbericht 2024
        Pfad: Steuern / 2024 / Jahresbericht 2024
        Seite: 3
        Abschnitt: 7.5.3 Lenkung dokumentierter Information

    Über mehrere Seiten: „Seiten: 12–13“. Die Abschnittsnummer hilft bei
    Fragen wie „Was steht in Abschnitt 7.5?“. Absätze stehen bewusst nicht im
    Kopf (die Nummern würden die Vektoren nur verrauschen).

    Er wird beim Einbetten vor den Abschnittstext gestellt und mit höherem
    Gewicht in die Volltextsuche aufgenommen, damit Fragen nach dem
    Dokumentnamen oder Ordner die Abschnitte finden.
    """
    lines = []
    name = _clean(_without_extension(_clean(title)))[:MAX_HEADING_LINE]
    if name:
        lines.append(f"Dokument: {name}")
    parts = [_clean(p) for p in re.split(r"[\\/]+", source_path or "")]
    parts = [p for p in parts if p and p != "."]
    if parts:
        parts[-1] = _without_extension(parts[-1])
        path = " / ".join(parts)
        if len(path) > MAX_HEADING_LINE:
            # Gekürzt wird vorne: Dateiname und nahe Ordner sind aussagekräftiger.
            path = "… " + path[-MAX_HEADING_LINE:].lstrip()
        lines.append(f"Pfad: {path}")
    if page is not None:
        if page_end is not None and page_end != page:
            lines.append(f"Seiten: {page}–{page_end}")
        else:
            lines.append(f"Seite: {page}")
    if section:
        lines.append(_clean(f"Abschnitt: {section} {section_title}")[:MAX_HEADING_LINE])
    return "\n".join(lines)
