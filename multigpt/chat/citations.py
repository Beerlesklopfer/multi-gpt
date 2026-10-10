"""Zitierangaben: Fundstelle (Abschnitt, Seite, Absatz) und Literaturangaben.

- ``citation_label``: deutsche Fundstelle „S. 12, Abs. 3–5“, mit Gliederung
  „Abschn. 7.5.3, S. 12, Abs. 3“ (Kontextblock fürs Modell, Quellenliste,
  Werkzeug ``search_documents``).
- ``Reference``: bibliografische Angaben einer Quelle (aus ``Document.bib_*``
  bzw. einer Webquelle); als ``dict`` in ``SourceRef.biblio`` festgehalten,
  damit Zitate auch nach dem Löschen oder Ändern des Dokuments gleich bleiben.
- ``entry``/``short``: Literaturverzeichnis-Eintrag und Kurzbeleg (In-Text) je
  Stil; ``bibtex``: Export.

Stile: DIN ISO 690 (Voreinstellung), APA 7, Harvard, Chicago (Author-Date),
MLA 9 – jeweils in der deutschsprachigen Fassung, weil die Oberfläche deutsch
ist („S.“, „Abs.“, „Hrsg.“, „Aufl.“, „o. J.“, „abgerufen am“). Bewusst eigene
kleine Formatierfunktionen statt citeproc/CSL: citeproc-py braucht die
CSL-Stile und -Locales als Dateien (nicht in Debian 13) und bringt für fünf
Stile mehr Abhängigkeiten als Regeln; so bleibt jede Regel lesbar und getestet.

Arten: Buch, Sammelband (herausgegeben), Beitrag im Sammelwerk (Kapitel),
Zeitschriftenartikel, Konferenzbeitrag (Proceedings), Bericht, Norm/Standard,
Webseite, Sonstiges. Normen werden über Normnummer und Ausgabedatum zitiert
(„DIN EN ISO 9001:2015-11“), auch im Kurzbeleg.

Fehlende Angaben fallen weg (kein „o. A.“); nur ein fehlendes Jahr wird nach
Stilkonvention als „o. J.“ angegeben (MLA lässt es weg). Ohne Autor rückt der
Herausgeber, die herausgebende Stelle oder der Titel an seine Stelle. Die
Ausgabe ist reiner Text (Escapen übernehmen Templates bzw. ``textContent``)
und deterministisch.

Dieses Modul importiert keine Modelle (``models`` nutzt Auswahllisten und
Validatoren von hier).
"""

from __future__ import annotations

import datetime
import re
import unicodedata
from dataclasses import asdict, dataclass, field, fields

from django.core.exceptions import ValidationError

# --- Stile, Arten, Einstellungen -----------------------------------------------------

STYLE_DIN = "din"
STYLE_APA = "apa"
STYLE_HARVARD = "harvard"
STYLE_CHICAGO = "chicago"
STYLE_MLA = "mla"
STYLE_BIBTEX = "bibtex"  # nur Export, kein Anzeigestil

STYLE_CHOICES = [
    (STYLE_DIN, "DIN ISO 690"),
    (STYLE_APA, "APA 7"),
    (STYLE_HARVARD, "Harvard"),
    (STYLE_CHICAGO, "Chicago (Author-Date)"),
    (STYLE_MLA, "MLA 9"),
]
STYLES = [key for key, _ in STYLE_CHOICES]
STYLE_LABELS = dict(STYLE_CHOICES) | {STYLE_BIBTEX: "BibTeX"}
DEFAULT_STYLE = STYLE_DIN

TYPE_BOOK = "book"
TYPE_EDITED = "edited"
TYPE_CHAPTER = "chapter"
TYPE_ARTICLE = "article"
TYPE_CONFERENCE = "conference"
TYPE_REPORT = "report"
TYPE_STANDARD = "standard"
TYPE_WEB = "web"
TYPE_OTHER = "other"
TYPE_CHOICES = [
    (TYPE_BOOK, "Buch"),
    (TYPE_EDITED, "Sammelband (herausgegeben)"),
    (TYPE_CHAPTER, "Beitrag im Sammelwerk (Kapitel)"),
    (TYPE_ARTICLE, "Zeitschriftenartikel"),
    (TYPE_CONFERENCE, "Konferenzbeitrag (Proceedings)"),
    (TYPE_REPORT, "Bericht"),
    (TYPE_STANDARD, "Norm/Standard"),
    (TYPE_WEB, "Webseite"),
    (TYPE_OTHER, "Sonstiges"),
]
TYPES = [key for key, _ in TYPE_CHOICES]
# Beiträge, die in einem Sammelwerk bzw. Tagungsband erscheinen.
IN_CONTAINER = (TYPE_CHAPTER, TYPE_CONFERENCE)

STATUS_VALID = "valid"
STATUS_WITHDRAWN = "withdrawn"
STATUS_CHOICES = [(STATUS_VALID, "gültig"), (STATUS_WITHDRAWN, "zurückgezogen")]


@dataclass(frozen=True)
class Prefs:
    """Persönliche Einstellungen (``accounts.User.citation_*``)."""

    style: str = DEFAULT_STYLE
    short: bool = False  # Quellen im Antworttext als Kurzbeleg statt nur [n]
    locator: bool = True  # Abschnitt/Seite/Absatz anzeigen


def prefs_for(user) -> Prefs:
    """Einstellungen eines Kontos (Voreinstellung bei fehlendem Konto)."""
    if user is None or not getattr(user, "is_authenticated", False):
        return Prefs()
    style = getattr(user, "citation_style", "") or DEFAULT_STYLE
    return Prefs(
        style=style if style in STYLES else DEFAULT_STYLE,
        short=bool(getattr(user, "citation_short", False)),
        locator=bool(getattr(user, "citation_locator", True)),
    )


# --- Fundstelle ----------------------------------------------------------------------


def _span(start, end) -> str:
    return f"{start}–{end}" if end not in (None, "") and end != start else f"{start}"


def citation_label(
    page: int | None = None,
    page_end: int | None = None,
    paragraph: int | None = None,
    paragraph_end: int | None = None,
    *,
    section: str = "",
    section_end: str = "",
    page_prefix: str = "S. ",
) -> str:
    """Deutsche Fundstelle, leer wenn nichts bekannt ist.

    „S. 12, Abs. 3“, „S. 12, Abs. 3–5“, „S. 12, Abs. 4 – S. 13, Abs. 2“,
    „S. 12–13“ (ohne Absätze, Altbestand), „Abs. 17“ bzw. „Abs. 17–19“ (ohne
    Seiten). Mit Gliederung davor: „Abschn. 7.5.3, S. 12, Abs. 3“ bzw.
    „Abschn. 7.5.3–7.5.4, …“. ``page_prefix`` "" ergibt Chicago/MLA-Form
    („12, Abs. 3“).
    """
    where = ""
    if page is None:
        if paragraph is not None:
            where = f"Abs. {_span(paragraph, paragraph_end)}"
    else:
        last_page = page_end if page_end is not None else page
        if paragraph is None:
            where = f"{page_prefix}{_span(page, last_page)}"
        elif last_page == page:
            where = f"{page_prefix}{page}, Abs. {_span(paragraph, paragraph_end)}"
        else:
            end = f"{page_prefix}{last_page}"
            if paragraph_end is not None:
                end += f", Abs. {paragraph_end}"
            where = f"{page_prefix}{page}, Abs. {paragraph} – {end}"
    if section:
        return ", ".join(p for p in (f"Abschn. {_span(section, section_end)}", where) if p)
    return where


@dataclass(frozen=True)
class Locator:
    page: int | None = None
    page_end: int | None = None
    paragraph: int | None = None
    paragraph_end: int | None = None
    section: str = ""
    section_end: str = ""

    def label(self, page_prefix: str = "S. ") -> str:
        return citation_label(
            self.page,
            self.page_end,
            self.paragraph,
            self.paragraph_end,
            section=self.section,
            section_end=self.section_end,
            page_prefix=page_prefix,
        )


NO_LOCATOR = Locator()


# --- Angaben -------------------------------------------------------------------------


@dataclass(frozen=True)
class Reference:
    """Bibliografische Angaben einer Quelle (alle Felder optional).

    ``authors``/``editors``: je Eintrag „Nachname, Vorname“; ohne Komma gilt der
    Eintrag als Körperschaft („Statistisches Bundesamt“) und wird nicht
    umgestellt. ``date``: „JJJJ“, „JJJJ-MM“ oder „JJJJ-MM-TT“ (bei Normen das
    Ausgabedatum). ``container``: Sammelwerk bzw. Tagungsband. ``number``:
    Normnummer. ``institution``: herausgebende Stelle. ``accessed``: Abrufdatum
    (ISO-Datum) bei Webquellen.
    """

    title: str = ""
    type: str = TYPE_OTHER
    authors: tuple[str, ...] = field(default_factory=tuple)
    editors: tuple[str, ...] = field(default_factory=tuple)
    date: str = ""
    container: str = ""
    publisher: str = ""
    place: str = ""
    edition: str = ""
    series: str = ""
    series_number: str = ""
    url: str = ""
    isbn: str = ""
    isbn_e: str = ""
    doi: str = ""
    journal: str = ""
    volume: str = ""
    issue: str = ""
    pages: str = ""
    number: str = ""
    institution: str = ""
    status: str = ""
    replaces: str = ""
    accessed: str = ""

    def to_dict(self) -> dict:
        """Nur gesetzte Felder (kompakt für ``SourceRef.biblio``)."""
        data = asdict(self)
        data["authors"] = list(self.authors)
        data["editors"] = list(self.editors)
        return {k: v for k, v in data.items() if v}

    @classmethod
    def from_dict(cls, data: dict | None) -> Reference:
        data = data if isinstance(data, dict) else {}
        values = {}
        for f in fields(cls):
            raw = data.get(f.name)
            if f.name in ("authors", "editors"):
                items = raw if isinstance(raw, list | tuple) else []
                values[f.name] = tuple(_text(a) for a in items if _text(a))
            elif raw is not None:
                values[f.name] = _text(raw)
        if values.get("type") not in TYPES:
            values["type"] = TYPE_OTHER
        return cls(**values)


def _text(value) -> str:
    return " ".join(str(value or "").split())


def split_people(raw: str) -> tuple[str, ...]:
    """Personenfeld (eine Person je Zeile) -> Tupel ohne Leerzeilen."""
    return tuple(_text(line) for line in (raw or "").splitlines() if _text(line))


_DOCUMENT_FIELDS = (
    "date",
    "container",
    "publisher",
    "place",
    "edition",
    "series",
    "series_number",
    "url",
    "isbn",
    "isbn_e",
    "doi",
    "journal",
    "volume",
    "issue",
    "pages",
    "number",
    "institution",
    "status",
    "replaces",
)


def reference_from_document(document) -> Reference:
    """Angaben eines ``Document`` (``bib_*``); Titel ersatzweise der Dokumenttitel."""
    title = _text(getattr(document, "bib_title", "")) or _text(getattr(document, "title", ""))
    values = {name: _text(getattr(document, f"bib_{name}", "")) for name in _DOCUMENT_FIELDS}
    kind = getattr(document, "bib_type", "") or TYPE_OTHER
    return Reference(
        title=title,
        type=kind if kind in TYPES else TYPE_OTHER,
        authors=split_people(getattr(document, "bib_authors", "")),
        editors=split_people(getattr(document, "bib_editors", "")),
        **values,
    )


def web_reference(title: str, url: str, accessed: datetime.date | None) -> Reference:
    """Webquelle: Titel, URL, Abrufdatum (Zeitpunkt der Antwort)."""
    return Reference(
        title=_text(title),
        type=TYPE_WEB,
        url=_text(url),
        accessed=accessed.isoformat() if accessed else "",
    )


# --- Bausteine -----------------------------------------------------------------------

MONTHS = [
    "Januar",
    "Februar",
    "März",
    "April",
    "Mai",
    "Juni",
    "Juli",
    "August",
    "September",
    "Oktober",
    "November",
    "Dezember",
]
MONTHS_SHORT = [
    "Jan.",
    "Feb.",
    "März",
    "Apr.",
    "Mai",
    "Juni",
    "Juli",
    "Aug.",
    "Sept.",
    "Okt.",
    "Nov.",
    "Dez.",
]
NO_YEAR = "o. J."
SHORT_TITLE_WORDS = 4

_DATE_RE = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


@dataclass(frozen=True)
class _Name:
    family: str
    given: str = ""

    @property
    def corporate(self) -> bool:
        return not self.given


def _people(raw: tuple[str, ...]) -> list[_Name]:
    names = []
    for item in raw:
        family, sep, given = item.partition(",")
        names.append(_Name(family.strip(), given.strip()) if sep else _Name(item.strip()))
    return [n for n in names if n.family]


def _initials(given: str) -> str:
    """„Hans Peter“ -> „H. P.“, „Hans-Peter“ -> „H.-P.“."""
    parts = []
    for word in given.split():
        pieces = [p for p in word.split("-") if p]
        parts.append("-".join(p[0] + "." for p in pieces))
    return " ".join(parts)


def _date_parts(value: str) -> tuple[int | None, int | None, int | None]:
    match = _DATE_RE.match(value or "")
    if not match:
        return None, None, None
    year, month, day = (int(g) if g else None for g in match.groups())
    if month is not None and not 1 <= month <= 12:
        month = day = None
    return year, month, day


def _year(ref: Reference, missing: str = NO_YEAR) -> str:
    year, _, _ = _date_parts(ref.date)
    return str(year) if year else missing


def _iso_date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _date_numeric(value: str) -> str:
    day = _iso_date(value)
    return day.strftime("%d.%m.%Y") if day else ""


def _date_long(value: str, months=MONTHS) -> str:
    day = _iso_date(value)
    return f"{day.day}. {months[day.month - 1]} {day.year}" if day else ""


def _end(text: str, mark: str = ".") -> str:
    """Satzzeichen anhängen, wenn der Text nicht schon mit einem endet."""
    text = text.rstrip()
    if not text or text[-1] in ".!?…":
        return text
    return text + mark


def _join(*parts: str, sep: str = " ") -> str:
    return sep.join(p for p in parts if p)


def _edition(ref: Reference) -> str:
    """„3“ -> „3. Aufl.“; erste Auflage wird nicht angegeben, Freitext bleibt."""
    ed = ref.edition.strip().rstrip(".")
    if not ed or ed == "1":
        return ""
    return f"{ed}. Aufl." if ed.isdigit() else ref.edition.strip()


def _pages(ref: Reference) -> str:
    return re.sub(r"\s*-+\s*", "–", ref.pages.strip())


def _publication(ref: Reference) -> str:
    """„Ort: Verlag“, sonst was davon bekannt ist."""
    if ref.place and ref.publisher:
        return f"{ref.place}: {ref.publisher}"
    return ref.place or ref.publisher


def _series(ref: Reference, volume: str = "Bd. ") -> str:
    """„Lecture Notes in Computer Science, Bd. 1234“."""
    if not ref.series:
        return ""
    return f"{ref.series}, {volume}{ref.series_number}" if ref.series_number else ref.series


def _isbn(ref: Reference) -> str:
    """„ISBN 978-… (Print), 978-… (eBook)“ bzw. eine von beiden."""
    if ref.isbn and ref.isbn_e:
        return f"ISBN {ref.isbn} (Print), {ref.isbn_e} (eBook)"
    return f"ISBN {ref.isbn or ref.isbn_e}" if (ref.isbn or ref.isbn_e) else ""


def _doi_url(ref: Reference) -> str:
    return f"https://doi.org/{ref.doi}" if ref.doi else ""


def _link(ref: Reference) -> str:
    return _doi_url(ref) or ref.url


def _short_title(ref: Reference) -> str:
    words = ref.title.split()
    if len(words) <= SHORT_TITLE_WORDS:
        return ref.title
    return " ".join(words[:SHORT_TITLE_WORDS]) + " …"


def _list(items: list[str], last: str) -> str:
    """„A, B und C“ (``last`` = Verbindung vor dem letzten Element, mit Leerzeichen)."""
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + last + items[-1]


def _quoted(text: str) -> str:
    return f"„{text}“" if text else ""


def _norm(ref: Reference) -> str:
    """Normnummer mit Ausgabedatum: „DIN EN ISO 9001:2015-11“."""
    if not ref.number:
        return ""
    year, month, _ = _date_parts(ref.date)
    if year and month:
        return f"{ref.number}:{year}-{month:02d}"
    return f"{ref.number}:{year}" if year else ref.number


def _norm_notes(ref: Reference) -> list[str]:
    notes = []
    if ref.status == STATUS_WITHDRAWN:
        notes.append("Zurückgezogen.")
    if ref.replaces:
        notes.append(_end(f"Ersatz für {ref.replaces}"))
    return notes


def _lead_names(ref: Reference) -> tuple[list[_Name], bool]:
    """Wer vorne steht: Autoren, sonst Herausgeber (zweites Element True), sonst
    die herausgebende Stelle als Körperschaft."""
    authors = _people(ref.authors)
    if authors:
        return authors, False
    editors = _people(ref.editors)
    if editors and ref.type not in IN_CONTAINER:
        return editors, True
    if ref.institution:
        return [_Name(ref.institution)], False
    return [], False


def _families(names: list[_Name], *, two: str, many: str, max_listed: int = 2) -> str:
    """Nachnamen für den Kurzbeleg: 1, 2 (``two``) bzw. erster + ``many``."""
    if not names:
        return ""
    if len(names) <= max_listed:
        return _list([n.family for n in names], two)
    return f"{names[0].family} {many}"


def _short_wrap(who: str, year: str, locator: str, *, comma_year: bool = False) -> str:
    head = _join(who + ("," if comma_year and who and year else ""), year)
    return f"({head}{', ' + locator if locator else ''})"


def _norm_short(ref: Reference, locator: str) -> str:
    """Normen in allen Stilen: „(DIN EN ISO 9001:2015-11, S. 12)“."""
    return f"({_norm(ref)}{', ' + locator if locator else ''})"


# --- DIN ISO 690 ---------------------------------------------------------------------


def _din_people(names: list[_Name]) -> str:
    def one(n: _Name) -> str:
        return n.family if n.corporate else f"{n.family.upper()}, {n.given}"

    if len(names) > 3:
        return f"{one(names[0])} u. a."
    return "; ".join(one(n) for n in names)


def _din_entry(ref: Reference) -> str:
    if ref.type == TYPE_STANDARD and ref.number:
        parts = [_end(_norm(ref)), _end(ref.title), _end(_publication(ref))]
        return _join(*parts, *_norm_notes(ref), _end(_link(ref)) if ref.doi else "")
    names, editors = _lead_names(ref)
    who = _din_people(names) + (" (Hrsg.)" if editors else "")
    year = _year(ref)
    title = ref.title + (" [online]" if ref.type == TYPE_WEB else "")
    parts = [f"{who}, {_end(year)} {_end(title)}" if who else f"{title}, {_end(year)}"]
    if ref.type in IN_CONTAINER:
        editors_in = _din_people(_people(ref.editors))
        container = _join(f"{editors_in} (Hrsg.):" if editors_in else "", ref.container)
        parts.append(f"In: {_end(container)}" if container else "")
        pub = _publication(ref)
        pages = f"S. {_pages(ref)}" if ref.pages else ""
        parts += [_end(_edition(ref)), _end(_series(ref))]
        parts.append(_end(_join(pub + "," if pub and pages else pub, pages)))
    elif ref.type == TYPE_ARTICLE:
        source = ref.journal
        if ref.volume:
            source = _join(_end(source), ref.volume + (f"({ref.issue})" if ref.issue else ""))
        elif ref.issue:
            source = _join(_end(source), f"Nr. {ref.issue}")
        if ref.pages:
            source = _join(source + ",", f"S. {_pages(ref)}") if source else f"S. {_pages(ref)}"
        parts.append(_end(source))
    else:
        parts += [_end(_edition(ref)), _end(_series(ref)), _end(_publication(ref))]
    if _isbn(ref):
        parts.append(_end(_isbn(ref)))
    if ref.doi:
        parts.append(f"DOI: {ref.doi}.")
    if ref.accessed:
        parts.append(f"[Zugriff am: {_date_numeric(ref.accessed)}].")
    if ref.url:
        parts.append(f"Verfügbar unter: {ref.url}")
    return _join(*parts)


def _din_short(ref: Reference, locator: str) -> str:
    names, _ = _lead_names(ref)
    who = _families(names, two=" und ", many="u. a.") or _short_title(ref)
    return _short_wrap(who, _year(ref), locator)


# --- APA 7 ---------------------------------------------------------------------------


def _apa_people(names: list[_Name]) -> str:
    def one(n: _Name) -> str:
        return n.family if n.corporate else _join(n.family + ",", _initials(n.given))

    items = [one(n) for n in names]
    if not items:
        return ""
    if len(items) > 20:
        return ", ".join(items[:19]) + ", … " + items[-1]
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + ", & " + items[-1]


def _apa_editors_in(names: list[_Name]) -> str:
    """Herausgeber im Kapitel: „H. Müller & E. Schmidt (Hrsg.)“."""
    items = [n.family if n.corporate else _join(_initials(n.given), n.family) for n in names]
    if not items:
        return ""
    joined = items[0] if len(items) == 1 else ", ".join(items[:-1]) + " & " + items[-1]
    return f"{joined} (Hrsg.)"


def _apa_date(ref: Reference) -> str:
    year, month, day = _date_parts(ref.date)
    if not year:
        return f"({NO_YEAR})"
    if ref.type in (TYPE_WEB, TYPE_ARTICLE) and month and not ref.volume:
        detail = f"{day}. {MONTHS[month - 1]}" if day else MONTHS[month - 1]
        return f"({year}, {detail})"
    return f"({year})"


def _apa_entry(ref: Reference) -> str:
    names, editors = _lead_names(ref)
    who = _apa_people(names) + (" (Hrsg.)" if editors else "")
    date = _apa_date(ref)
    title = ref.title
    if who:
        parts = [_end(who), _end(date)]
    else:
        parts, title = [_end(ref.title), _end(date)], ""
    publisher = ref.publisher if ref.publisher and ref.publisher not in who else ""
    if ref.type == TYPE_ARTICLE:
        parts.append(_end(title))
        source = ref.journal
        if ref.volume:
            source = _join(source + ",", ref.volume + (f"({ref.issue})" if ref.issue else ""))
        if ref.pages:
            source = _join(source + ",", _pages(ref)) if source else _pages(ref)
        parts += [_end(source), _link(ref)]
    elif ref.type in IN_CONTAINER:
        parts.append(_end(title))
        pages = f"S. {_pages(ref)}" if ref.pages else ""
        detail = ", ".join(p for p in (_edition(ref), pages) if p)
        book = _join(ref.container, f"({detail})" if detail else "")
        editors_in = _apa_editors_in(_people(ref.editors))
        parts.append(_end(_join("In", _join(editors_in + "," if editors_in else "", book))))
        parts += [_end(publisher), _link(ref)]
    elif ref.type == TYPE_STANDARD:
        detail = f"({_norm(ref)})" if ref.number else ""
        parts += [_end(_join(title, detail)), _end(publisher), _link(ref)]
    else:
        edition = f"({_edition(ref)})" if _edition(ref) else ""
        parts.append(_end(_join(title, edition)))
        parts.append(_end(publisher))
        if ref.type == TYPE_WEB and ref.accessed and ref.url:
            parts.append(f"Abgerufen am {_date_long(ref.accessed)}, von {ref.url}")
        else:
            parts.append(_link(ref))
    return _join(*parts)


def _apa_short(ref: Reference, locator: str) -> str:
    names, _ = _lead_names(ref)
    who = _families(names, two=" & ", many="et al.") or _short_title(ref)
    return _short_wrap(who, _year(ref), locator, comma_year=True)


# --- Harvard -------------------------------------------------------------------------


def _harvard_people(names: list[_Name]) -> str:
    def one(n: _Name) -> str:
        return n.family if n.corporate else _join(n.family + ",", _initials(n.given))

    if len(names) >= 4:
        return f"{one(names[0])} et al."
    return _list([one(n) for n in names], " und ")


def _harvard_entry(ref: Reference) -> str:
    names, editors = _lead_names(ref)
    who = _harvard_people(names) + (" (Hrsg.)" if editors else "")
    year = f"({_year(ref)})"
    if who:
        parts, title = [_join(who, year)], ref.title
    else:
        parts, title = [_end(_join(ref.title, year))], ""
    if ref.type == TYPE_ARTICLE:
        source = ref.journal
        if ref.volume:
            source = _join(source + ",", ref.volume + (f"({ref.issue})" if ref.issue else ""))
        if ref.pages:
            source = _join(source + ",", f"S. {_pages(ref)}") if source else f"S. {_pages(ref)}"
        quoted = f"‚{title}‘" if title else ""
        parts.append(_end(_join(quoted + "," if quoted and source else quoted, source)))
    elif ref.type in IN_CONTAINER:
        quoted = f"‚{title}‘," if title else ""
        editors_in = _harvard_people(_people(ref.editors))
        book = _join(f"{editors_in} (Hrsg.)" if editors_in else "", _end(ref.container))
        pub = _publication(ref)
        pages = f"S. {_pages(ref)}" if ref.pages else ""
        series = f"({_series(ref)})" if ref.series else ""
        parts += [quoted, f"in {book}" if book else "", _end(_edition(ref)), series]
        parts.append(_end(_join(pub + "," if pub and pages else pub, pages)))
    elif ref.type == TYPE_STANDARD:
        number = _norm(ref)
        parts.append(_end(_join(number + ":" if number and title else number, title)))
        parts += [_end(_publication(ref)), *_norm_notes(ref)]
    else:
        series = f"({_series(ref)})" if ref.series else ""
        parts += [_end(title), _end(_edition(ref)), series, _end(_publication(ref))]
    if ref.doi:
        parts.append(f"doi: {ref.doi}.")
    if ref.url:
        accessed = _date_numeric(ref.accessed)
        suffix = f" (Zugriff: {accessed})." if accessed else ""
        parts.append(f"Verfügbar unter: {ref.url}{suffix}")
    return _join(*parts)


def _harvard_short(ref: Reference, locator: str) -> str:
    names, _ = _lead_names(ref)
    if len(names) == 3:
        who = _list([n.family for n in names], " und ")
    else:
        who = _families(names, two=" und ", many="et al.")
    return _short_wrap(who or _short_title(ref), _year(ref), locator)


# --- Chicago (Author-Date) -----------------------------------------------------------


def _chicago_people(names: list[_Name]) -> str:
    def first(n: _Name) -> str:
        return n.family if n.corporate else f"{n.family}, {n.given}"

    def other(n: _Name) -> str:
        return n.family if n.corporate else f"{n.given} {n.family}"

    if not names:
        return ""
    if len(names) > 10:
        return ", ".join([first(names[0])] + [other(n) for n in names[1:7]]) + " et al."
    items = [first(names[0])] + [other(n) for n in names[1:]]
    if len(items) == 2:
        return f"{items[0]}, und {items[1]}"
    return _list(items, ", und ")


def _natural(names: list[_Name]) -> str:
    """Namen in natürlicher Reihenfolge: „Hans Müller und Eva Schmidt“."""
    return _list([n.family if n.corporate else f"{n.given} {n.family}" for n in names], " und ")


def _chicago_entry(ref: Reference) -> str:
    names, editors = _lead_names(ref)
    who = _chicago_people(names) + (", Hrsg" if editors else "")
    year = _year(ref)
    if who:
        parts, title = [_end(who), _end(year)], ref.title
    else:
        parts, title = [_end(ref.title), _end(year)], ""
    link = _end(_link(ref)) if _link(ref) else ""
    if ref.type == TYPE_ARTICLE:
        source = ref.journal
        if ref.volume:
            source = _join(source, ref.volume + (f" ({ref.issue})" if ref.issue else ""))
        if ref.pages:
            source = f"{source}: {_pages(ref)}" if source else _pages(ref)
        parts += [_quoted(_end(title)), _end(source), link]
    elif ref.type in IN_CONTAINER:
        editors_in = _natural(_people(ref.editors))
        book = _join(
            f"In {ref.container}" if ref.container else "",
            f"herausgegeben von {editors_in}" if editors_in else "",
            _pages(ref),
            sep=", ",
        )
        parts += [_quoted(_end(title)), _end(book), _end(_edition(ref)), _end(_series(ref, ""))]
        parts += [_end(_publication(ref)), link]
    elif ref.type == TYPE_WEB:
        parts += [_quoted(_end(title)), _end(ref.publisher)]
        if ref.accessed:
            parts.append(f"Zugegriffen {_date_long(ref.accessed)}.")
        parts.append(_end(ref.url) if ref.url else "")
    elif ref.type == TYPE_STANDARD:
        parts += [_end(title), _end(_norm(ref)), _end(_publication(ref)), *_norm_notes(ref)]
    else:
        parts += [_end(title), _end(_edition(ref)), _end(_series(ref, ""))]
        parts += [_end(_publication(ref)), link]
    return _join(*parts)


def _chicago_short(ref: Reference, locator: str) -> str:
    names, _ = _lead_names(ref)
    if len(names) <= 3:
        who = _list([n.family for n in names], " und ")
    else:
        who = f"{names[0].family} et al."
    return _short_wrap(who or _short_title(ref), _year(ref), locator)


# --- MLA 9 ---------------------------------------------------------------------------


def _mla_people(names: list[_Name]) -> str:
    def first(n: _Name) -> str:
        return n.family if n.corporate else f"{n.family}, {n.given}"

    if not names:
        return ""
    if len(names) == 1:
        return first(names[0])
    if len(names) == 2:
        second = names[1]
        other = second.family if second.corporate else f"{second.given} {second.family}"
        return f"{first(names[0])}, und {other}"
    return f"{first(names[0])}, et al."


def _mla_date(ref: Reference) -> str:
    year, month, day = _date_parts(ref.date)
    if not year:
        return ""
    if month and ref.type in (TYPE_WEB, TYPE_ARTICLE):
        return _join(f"{day}." if day else "", MONTHS_SHORT[month - 1], str(year))
    return str(year)


def _mla_entry(ref: Reference) -> str:
    names, editors = _lead_names(ref)
    who = _mla_people(names) + (", Hrsg" if editors else "")
    parts = [_end(who)] if who else []
    container: list[str] = []
    if ref.type == TYPE_ARTICLE:
        parts.append(_quoted(_end(ref.title)))
        container = [
            ref.journal,
            f"Bd. {ref.volume}" if ref.volume else "",
            f"Nr. {ref.issue}" if ref.issue else "",
            _mla_date(ref),
            f"S. {_pages(ref)}" if ref.pages else "",
        ]
    elif ref.type in IN_CONTAINER:
        editors_in = _natural(_people(ref.editors))
        parts.append(_quoted(_end(ref.title)))
        container = [
            ref.container,
            f"herausgegeben von {editors_in}" if editors_in else "",
            _edition(ref),
            ref.publisher,
            _mla_date(ref),
            f"S. {_pages(ref)}" if ref.pages else "",
        ]
    elif ref.type == TYPE_WEB:
        parts.append(_quoted(_end(ref.title)))
        container = [ref.publisher, _mla_date(ref), ref.url]
    elif ref.type == TYPE_STANDARD:
        parts.append(_end(ref.title))
        container = [_norm(ref), ref.publisher, _mla_date(ref)]
    else:
        parts.append(_end(ref.title))
        container = [_edition(ref), ref.publisher, _mla_date(ref)]
    tail = ", ".join(c for c in container if c)
    if ref.type != TYPE_WEB and _link(ref):
        tail = ", ".join(c for c in (tail, _link(ref)) if c)
    parts.append(_end(tail))
    if ref.series and ref.type not in (TYPE_ARTICLE, TYPE_WEB):
        parts.append(_end(_series(ref)))
    if ref.type == TYPE_WEB and ref.accessed:
        parts.append(f"Abgerufen am {_date_long(ref.accessed, MONTHS_SHORT)}.")
    return _join(*parts)


def _mla_short(ref: Reference, locator: str) -> str:
    names, _ = _lead_names(ref)
    who = _families(names, two=" und ", many="et al.") or _short_title(ref)
    if not locator:
        return f"({who})"
    # MLA: Seite ohne Komma direkt hinter dem Namen, Abschnitt/Absatz mit Komma.
    sep = ", " if locator.startswith(("Abs.", "Abschn.")) else " "
    return f"({who}{sep}{locator})"


# --- BibTeX --------------------------------------------------------------------------

_BIBTEX_TYPES = {
    TYPE_BOOK: "book",
    TYPE_EDITED: "book",
    TYPE_CHAPTER: "incollection",
    TYPE_ARTICLE: "article",
    TYPE_CONFERENCE: "inproceedings",
    TYPE_REPORT: "techreport",
    TYPE_STANDARD: "techreport",
    TYPE_WEB: "online",
    TYPE_OTHER: "misc",
}
_BIBTEX_SPECIAL = re.compile(r"([{}\\%&$#_^~])")


def _bib_escape(value: str) -> str:
    def repl(match: re.Match) -> str:
        ch = match.group(1)
        if ch == "\\":
            return r"\textbackslash{}"
        if ch == "^":
            return r"\^{}"
        if ch == "~":
            return r"\~{}"
        return "\\" + ch

    return _BIBTEX_SPECIAL.sub(repl, value)


def _bib_people(names: list[_Name]) -> str:
    # Körperschaften in Klammern, damit BibTeX sie nicht als Vor-/Nachname zerlegt.
    return " and ".join(
        f"{{{_bib_escape(n.family)}}}"
        if n.corporate
        else f"{_bib_escape(n.family)}, {_bib_escape(n.given)}"
        for n in names
    )


def _ascii(value: str) -> str:
    value = value.replace("ß", "ss").replace("ä", "ae").replace("ö", "oe").replace("ü", "ue")
    value = value.replace("Ä", "Ae").replace("Ö", "Oe").replace("Ü", "Ue")
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^A-Za-z0-9]", "", value)


def bibtex_key(ref: Reference) -> str:
    year, _, _ = _date_parts(ref.date)
    if ref.type == TYPE_STANDARD and ref.number:
        return _ascii(ref.number).lower() + (str(year) if year else "")
    names, _ = _lead_names(ref)
    who = _ascii(names[0].family.split()[-1] if names else "").lower()
    word = next((_ascii(w).lower() for w in ref.title.split() if len(_ascii(w)) > 3), "")
    return (who + (str(year) if year else "") + word) or "quelle"


def bibtex(ref: Reference) -> str:
    """BibTeX-Eintrag (biblatex-kompatibel: ``@online``, ``urldate``). Normen als
    ``@techreport`` mit ``type = {Norm}`` und Normnummer in ``number``."""
    kind = _BIBTEX_TYPES.get(ref.type, "misc")
    year, month, _ = _date_parts(ref.date)
    standard = ref.type == TYPE_STANDARD
    techreport = kind == "techreport"
    pairs = [
        ("author", _bib_people(_people(ref.authors))),
        ("editor", _bib_people(_people(ref.editors))),
        ("title", ref.title),
        ("booktitle", ref.container if ref.type in IN_CONTAINER else ""),
        ("journal", ref.journal if kind == "article" else ""),
        ("series", ref.series),
        ("volume", ref.volume or ref.series_number),
        ("number", _norm(ref) if standard else ref.issue),
        ("type", "Norm" if standard else ""),
        ("pages", re.sub(r"\s*[-–]+\s*", "--", ref.pages.strip())),
        ("edition", ref.edition.strip().rstrip(".") if _edition(ref) else ""),
        ("institution", (ref.institution or ref.publisher) if techreport else ""),
        ("publisher", ref.publisher if kind not in ("article", "techreport") else ""),
        ("address", ref.place if kind != "article" else ""),
        ("year", str(year) if year else ""),
        ("month", str(month) if year and month else ""),
        ("isbn", ref.isbn or ref.isbn_e),
        ("doi", ref.doi),
        ("url", ref.url),
        ("urldate", ref.accessed),
        ("note", " ".join(_norm_notes(ref)) if standard else ""),
    ]
    lines = [f"@{kind}{{{bibtex_key(ref)},"]
    for key, value in pairs:
        if value:
            escaped = value if key in ("url", "doi", "author", "editor") else _bib_escape(value)
            lines.append(f"  {key} = {{{escaped}}},")
    lines[-1] = lines[-1].rstrip(",")
    lines.append("}")
    return "\n".join(lines)


# --- Öffentliche Schnittstelle -------------------------------------------------------

_ENTRY = {
    STYLE_DIN: _din_entry,
    STYLE_APA: _apa_entry,
    STYLE_HARVARD: _harvard_entry,
    STYLE_CHICAGO: _chicago_entry,
    STYLE_MLA: _mla_entry,
}
_SHORT = {
    STYLE_DIN: _din_short,
    STYLE_APA: _apa_short,
    STYLE_HARVARD: _harvard_short,
    STYLE_CHICAGO: _chicago_short,
    STYLE_MLA: _mla_short,
}
# Chicago und MLA geben Seiten ohne „S.“ an („Müller 2024, 12“, „Müller 12“).
_PAGE_PREFIX = {STYLE_CHICAGO: "", STYLE_MLA: ""}


def entry(ref: Reference, style: str = DEFAULT_STYLE) -> str:
    """Eintrag fürs Literaturverzeichnis (ohne Fundstelle)."""
    if style == STYLE_BIBTEX:
        return bibtex(ref)
    return " ".join(_ENTRY.get(style, _din_entry)(ref).split())


def short(ref: Reference, style: str = DEFAULT_STYLE, locator: Locator = NO_LOCATOR) -> str:
    """Kurzbeleg im Text, z. B. APA „(Müller, 2024, S. 12, Abs. 3)“; Normen in
    jedem Stil „(DIN EN ISO 9001:2015-11, Abschn. 7.5.3, S. 12)“."""
    if style not in _SHORT:
        style = DEFAULT_STYLE
    if ref.type == TYPE_STANDARD and ref.number:
        return _norm_short(ref, locator.label())
    return _SHORT[style](ref, locator.label(_PAGE_PREFIX.get(style, "S. ")))


def all_formats(ref: Reference, locator: Locator = NO_LOCATOR) -> dict:
    """Alle Stile für das Kopiermenü: {stil: {"entry", "short"}, "bibtex": text}."""
    data: dict = {s: {"entry": entry(ref, s), "short": short(ref, s, locator)} for s in STYLES}
    data[STYLE_BIBTEX] = bibtex(ref)
    return data


# --- Erkennung (Vorbelegung beim Indexieren) -----------------------------------------

_DOI_FIND = re.compile(r"\b(10\.\d{4,9}/[^\s\"'<>]+)", re.IGNORECASE)
# Normnummer mit Ausgabedatum: „DIN EN ISO 9001:2015-11“, „DIN 1450 Ausgabe 2013-04“,
# „ISO/IEC 27001:2022-10“; im Dateinamen auch mit Unterstrichen.
_NORM_FIND = re.compile(
    r"\b((?:DIN|ISO|IEC|EN|VDE|VDI)(?:[ /](?:EN|ISO|IEC|VDE|SPEC|TS|TR))*"
    r" ?\d{1,6}(?:-\d{1,3}){0,3})"
    r"(?:\s*:\s*|,?\s*Ausgabe\s*:?\s*|\s+)((?:19|20)\d{2})-(0[1-9]|1[0-2])(?!\d)"
)


def find_doi(text: str) -> str:
    """Erste DOI im Text (ohne angehängte Satzzeichen), sonst ""."""
    match = _DOI_FIND.search(text or "")
    if not match:
        return ""
    return match.group(1).rstrip(".,;:)]}")


@dataclass(frozen=True)
class NormMatch:
    number: str  # „DIN EN ISO 9001“
    date: str  # „2015-11“
    institution: str  # „DIN“


def find_norm(text: str) -> NormMatch | None:
    """Normnummer samt Ausgabedatum im Text (Dateiname, Titelseite), sonst None.

    Ohne erkennbares Ausgabedatum gibt es keinen Treffer – lieber nichts setzen
    als eine falsche Ausgabe.
    """
    cleaned = " ".join(re.sub(r"_+", " ", text or "").split())
    match = _NORM_FIND.search(cleaned)
    if not match:
        return None
    number = match.group(1).strip()
    issuer = number.split()[0].split("/")[0]
    return NormMatch(number, f"{match.group(2)}-{match.group(3)}", issuer)


# --- Validierung (Document.bib_*) ----------------------------------------------------

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$")
_PAGES_RE = re.compile(r"^[\w.]+(?:\s*[-–]\s*[\w.]+)?$")


def validate_bib_date(value: str) -> None:
    year, month, day = _date_parts(value)
    if not year or (_DATE_RE.match(value).group(2) and not month):
        raise ValidationError("Bitte als Jahr (JJJJ) oder Datum (JJJJ-MM bzw. JJJJ-MM-TT).")
    if day:
        try:
            datetime.date(year, month, day)
        except ValueError:
            raise ValidationError("Dieses Datum gibt es nicht.") from None


def normalize_isbn(value: str) -> str:
    return re.sub(r"[\s-]", "", value or "").upper()


def validate_isbn(value: str) -> None:
    digits = normalize_isbn(value)
    ok = False
    if re.fullmatch(r"\d{9}[\dX]", digits):
        total = sum((10 - i) * (10 if c == "X" else int(c)) for i, c in enumerate(digits))
        ok = total % 11 == 0
    elif re.fullmatch(r"\d{13}", digits):
        total = sum((1 if i % 2 == 0 else 3) * int(c) for i, c in enumerate(digits))
        ok = total % 10 == 0
    if not ok:
        raise ValidationError("Keine gültige ISBN (10 oder 13 Ziffern mit Prüfziffer).")


def normalize_doi(value: str) -> str:
    value = (value or "").strip()
    return re.sub(r"^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", "", value, flags=re.IGNORECASE)


def validate_doi(value: str) -> None:
    if not _DOI_RE.match(normalize_doi(value)):
        raise ValidationError("Keine gültige DOI (Form 10.xxxx/…).")


def validate_pages(value: str) -> None:
    if not _PAGES_RE.match((value or "").strip()):
        raise ValidationError("Bitte als Seite oder Bereich angeben, z. B. 45–67.")
