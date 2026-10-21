"""Vorlagen für ``create_pdf``: Lineaturen und Mathe-Arbeitsblätter als Vektorgrafik.

Jede Seite ist ein SVG mit Maßen in Millimetern (``viewBox`` = Seitengröße in
mm); ``documents_pdf`` setzt sie in ein HTML-Gerüst mit ``@page``-Größe und
Rand 0, WeasyPrint übernimmt sie als Vektoren ins PDF. So sind Linien exakt
und druckfertig – Drucken mit 100 % („tatsächliche Größe“), nicht „an Seite
anpassen“.

**Lineaturen** (Maße in mm). Die Ausgangsnorm DIN 16552-1 (2005) ist nicht frei
zugänglich; die Werte folgen übereinstimmenden Hersteller- und Lehrmittelangaben:

- **0** (Klasse 1, Schreiblernheft): 4 Linien, Bänder 6/6/6 (Ober-, Mittel-,
  Unterband), Kontrastlineatur, A5 quer mit 5 Zeilen – Brunnen Schreiblernheft
  1045940 („vier Linien pro Zeile, Abstand jeweils 6 mm, 5 extragroße
  Liniensysteme“), code-knacker.de/lineaturen.htm; Wikipedia „Lineatur“
  (Tabelle Handschrift, A5 quer).
- **1** (Klasse 1): 4 Linien, Bänder 5/5/5, Zeilenabstand 5 (Raster 20) –
  vorlagenplatz.de/vorlage/liniertes-papier, diktatheld.de/lineatur, Wikipedia.
- **2** (Klasse 2): 4 Linien, Bänder 4/4/4, Zeilenabstand 5 (Raster 17) – dieselben.
- **3** (Klasse 3): 2 Linien (Mittelband 3,5), Abstand 8 – diktatheld.de, Wikipedia.
- **4** (Klasse 4): eine Grundlinie, Abstand 10 – Wikipedia, vorlagenplatz.de.
- **liniert**: Lineatur 25 (A4, 9 mm, Korrekturrand 20 mm rechts) – vorlagenplatz.de.
- **karo5** (Lineatur 22/5, 5 × 5 mm), **karo10** (10 × 10 mm, freie Größe),
  **blanko_rand** (leer mit Korrekturrand 20 mm).

Den Abstand zwischen den Liniensystemen von Lineatur 0 nennen die Hersteller
nicht; 8 mm ergeben die 5 Systeme auf A5 quer. Werden weniger Zeilen als
möglich gewünscht, verteilt sich der Rest gleichmäßig auf die Abstände (die
Bänder bleiben exakt). Linienstärke 0,25 mm, verstärkte Grundlinie 0,6 mm.

**Vorlageschrift:** DejaVu Sans (freie Lizenz, Debian ``fonts-dejavu-core``),
keine Schulausgangsschrift (deren Lizenzen sind meist nicht frei). Die x-Höhe
von DejaVu Sans (1120/2048 em) wird auf das Mittelband gesetzt.

**Rechenaufgaben** erzeugt der Server (``make_tasks``) deterministisch aus
``seed`` – nie das Modell –, damit die Lösungen stimmen.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from xml.sax.saxutils import escape

PAGE_SIZES = {"A4": (210.0, 297.0), "A5": (148.0, 210.0), "Letter": (215.9, 279.4)}
FONT = "DejaVu Sans"
X_HEIGHT = 1120 / 2048  # DejaVu Sans: x-Höhe je em
CAP_HEIGHT = 1493 / 2048
MARGIN = 8.0  # Seitenrand (Drucker drucken meist ab ca. 4–5 mm)
HEADER = 10.0
LINE = 0.25
BOLD_LINE = 0.6
INK = "#222222"
HELPER = "#555555"
GRID = "#8c9aa6"
CONTRAST_FILL = "#e4e4e4"  # Mittelband hinterlegt, auch schwarz-weiß gut sichtbar
PRINT_HINT = "Drucken mit 100 % (tatsächliche Größe)"
BACKGROUNDS = {"weiss": "#ffffff", "weiß": "#ffffff", "hellgruen": "#eef6d0", "hellgrün": "#eef6d0"}


class SheetError(ValueError):
    """Ungültige Angabe; ``str(exc)`` ist ein deutscher Text für das Modell."""


def page_dims(page_size: str, orientation: str) -> tuple[float, float]:
    w, h = PAGE_SIZES[page_size]
    return (h, w) if orientation == "landscape" else (w, h)


def _n(value: float) -> str:
    text = f"{value:.3f}".rstrip("0").rstrip(".")
    return text if text not in ("", "-0") else "0"


# --- SVG-Leinwand in Millimetern ------------------------------------------------------


class Canvas:
    def __init__(self, width: float, height: float, background: str = "#ffffff"):
        self.width, self.height = width, height
        self.items: list[str] = []
        if background.lower() != "#ffffff":
            self.rect(0, 0, width, height, fill=background, stroke="none")

    def line(self, x1, y1, x2, y2, width=LINE, color=HELPER, dash: str = "") -> None:
        extra = f' stroke-dasharray="{dash}"' if dash else ""
        self.items.append(
            f'<line x1="{_n(x1)}" y1="{_n(y1)}" x2="{_n(x2)}" y2="{_n(y2)}" '
            f'stroke="{color}" stroke-width="{_n(width)}"{extra}/>'
        )

    def rect(self, x, y, w, h, *, fill="none", stroke=INK, width=LINE, radius=0.0) -> None:
        r = f' rx="{_n(radius)}"' if radius else ""
        self.items.append(
            f'<rect x="{_n(x)}" y="{_n(y)}" width="{_n(w)}" height="{_n(h)}"{r} '
            f'fill="{fill}" stroke="{stroke}" stroke-width="{_n(width)}"/>'
        )

    def polygon(self, points, *, fill="none", stroke=INK, width=LINE) -> None:
        pts = " ".join(f"{_n(x)},{_n(y)}" for x, y in points)
        self.items.append(
            f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="{_n(width)}"/>'
        )

    def circle(self, cx, cy, r, *, fill="none", stroke=INK, width=LINE) -> None:
        self.items.append(
            f'<circle cx="{_n(cx)}" cy="{_n(cy)}" r="{_n(r)}" fill="{fill}" '
            f'stroke="{stroke}" stroke-width="{_n(width)}"/>'
        )

    def text(self, x, y, text, size, *, anchor="start", bold=False, color=INK) -> None:
        weight = ' font-weight="bold"' if bold else ""
        self.items.append(
            f'<text x="{_n(x)}" y="{_n(y)}" font-family="{FONT}" font-size="{_n(size)}" '
            f'text-anchor="{anchor}" fill="{color}"{weight}>{escape(str(text))}</text>'
        )

    def svg(self) -> str:
        return (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{_n(self.width)}mm" '
            f'height="{_n(self.height)}mm" viewBox="0 0 {_n(self.width)} {_n(self.height)}">'
            + "".join(self.items)
            + "</svg>"
        )


@dataclass
class Frame:
    """Gemeinsame Angaben jeder Vorlage (Seite, Kopf, Hintergrund)."""

    width: float
    height: float
    title: str = ""
    name_field: bool = False
    background: str = "#ffffff"
    label: str = ""
    font: float = 5.0  # Schriftgröße der Aufgaben in mm (aus ``grade``)
    instruction: str = ""  # Arbeitsanweisung unter dem Kopf (nur erste Seite der Aufgaben)

    def canvas(self, subtitle: str = "") -> tuple[Canvas, float, float]:
        """Neue Seite mit Kopf und Fuß; liefert (Leinwand, oben, unten) des Inhalts."""
        c = Canvas(self.width, self.height, self.background)
        top = MARGIN
        if self.title or self.name_field or subtitle:
            base = MARGIN + 6
            title = " – ".join(t for t in (self.title, subtitle) if t)
            if title:
                c.text(MARGIN, base, title, 4.6, bold=True)
            if self.name_field:
                # „Name: ______  Datum: ______“ rechtsbündig (schmale Seiten kürzer).
                right = self.width - MARGIN
                name_w, date_w = (45, 30) if self.width >= 180 else (32, 20)
                c.line(right - date_w, base + 0.6, right, base + 0.6, 0.3, INK)
                c.text(right - date_w - 2, base, "Datum:", 3.6, anchor="end")
                name_end = right - date_w - 17
                c.line(name_end - name_w, base + 0.6, name_end, base + 0.6, 0.3, INK)
                c.text(name_end - name_w - 2, base, "Name:", 3.6, anchor="end")
            top += HEADER
        if self.instruction and not subtitle:
            size = max(3.6, self.font * 0.75)
            c.text(MARGIN, top + size, self.instruction, size)
            top += size * 1.8
        bottom = self.height - MARGIN
        hint_y = self.height - 3.2
        c.text(self.width - MARGIN, hint_y, PRINT_HINT, 2.2, anchor="end", color="#888888")
        if self.label:
            c.text(MARGIN, hint_y, self.label, 2.2, color="#888888")
        return c, top, bottom


# --- Lineaturen -------------------------------------------------------------------------


@dataclass(frozen=True)
class Lineatur:
    label: str
    offsets: tuple[float, ...] = ()  # Linien eines Systems ab Oberkante (mm)
    baseline: int = 0  # Index der Grundlinie in ``offsets``
    band: tuple[int, int] | None = None  # Mittelband (Index oben, unten)
    gap: float = 0.0  # Abstand zwischen zwei Systemen
    grid: float = 0.0  # Karo: Kantenlänge
    margin_right: float = 0.0  # Korrekturrand
    contrast: bool = False
    houses: bool = False
    default_page: tuple[str, str] = ("A4", "portrait")

    @property
    def system_height(self) -> float:
        return self.offsets[-1] if self.offsets else 0.0

    @property
    def middle(self) -> float:
        if self.band:
            return self.offsets[self.band[1]] - self.offsets[self.band[0]]
        return self.gap * 0.4


LINEATUREN = {
    "0": Lineatur(
        "Lineatur 0",
        (0, 6, 12, 18),
        2,
        (1, 2),
        8,
        contrast=True,
        houses=True,
        default_page=("A5", "landscape"),
    ),
    "1": Lineatur("Lineatur 1", (0, 5, 10, 15), 2, (1, 2), 5, contrast=True, houses=True),
    "2": Lineatur("Lineatur 2", (0, 4, 8, 12), 2, (1, 2), 5, houses=True),
    "3": Lineatur("Lineatur 3", (0, 3.5), 1, (0, 1), 8),
    "4": Lineatur("Lineatur 4", (0,), 0, None, 10),
    "liniert": Lineatur("Lineatur 25 (9 mm, Rand)", (0,), 0, None, 9, margin_right=20),
    "karo5": Lineatur("Karo 5 mm", grid=5),
    "karo10": Lineatur("Karo 10 mm", grid=10),
    "blanko_rand": Lineatur("Blanko mit Rand", margin_right=20),
}
HOUSE_ROOF, HOUSE_BODY, HOUSE_CELLAR = "#e8a39b", "#fbe7a1", "#c9b49a"


def _house(c: Canvas, x: float, top: float, lin: Lineatur) -> None:
    """Häuschen: Dach im Oberband, Haus im Mittelband, Keller im Unterband."""
    o0, o1, o2, o3 = (top + v for v in lin.offsets)
    w = o2 - o1
    c.polygon([(x, o1), (x + w, o1), (x + w / 2, o0)], fill=HOUSE_ROOF, stroke=INK, width=0.3)
    c.rect(x, o1, w, o2 - o1, fill=HOUSE_BODY, stroke=INK, width=0.3)
    door = w * 0.3
    c.rect(x + (w - door) / 2, o2 - w * 0.5, door, w * 0.5, fill="#ffffff", stroke=INK, width=0.2)
    c.rect(x, o2, w, o3 - o2, fill=HOUSE_CELLAR, stroke=INK, width=0.3)


def draw_system(
    c: Canvas,
    lin: Lineatur,
    x0: float,
    x1: float,
    top: float,
    *,
    baseline_bold: bool | None = None,
    contrast: bool | None = None,
) -> None:
    """Ein Liniensystem von ``x0`` bis ``x1``, Oberkante ``top``. Grundlinie
    verstärkt: Standard nur bei Lineaturen mit Bändern."""
    if baseline_bold is None:
        baseline_bold = lin.band is not None
    if (lin.contrast if contrast is None else contrast) and lin.band:
        a, b = (top + lin.offsets[i] for i in lin.band)
        c.rect(x0, a, x1 - x0, b - a, fill=CONTRAST_FILL, stroke="none")
    for i, offset in enumerate(lin.offsets):
        bold = baseline_bold and i == lin.baseline
        c.line(
            x0, top + offset, x1, top + offset, BOLD_LINE if bold else LINE, INK if bold else HELPER
        )


def system_positions(lin: Lineatur, top: float, bottom: float, rows: int | None):
    """Oberkanten der Systeme und tatsächliche Zeilenzahl (zentriert im Bereich)."""
    avail = bottom - top
    height, gap = lin.system_height, lin.gap
    pitch = height + gap
    fit = max(1, int((avail + gap + 1e-6) // pitch))
    count = fit if rows is None else max(1, min(int(rows), fit))
    if rows is not None and count < fit and count > 1 and height > 0:
        # Weniger Zeilen: Rest gleichmäßig verteilen, höchstens eine Systemhöhe Abstand.
        gap = min((avail - count * height) / (count - 1), max(gap, height))
    used = count * height + (count - 1) * gap
    start = top + (avail - used) / 2
    return [start + i * (height + gap) for i in range(count)], fit


def lineatur_page(frame: Frame, lin: Lineatur, opts: dict) -> tuple[str, dict]:
    c, top, bottom = frame.canvas()
    left, right = MARGIN, frame.width - MARGIN
    info = {"rows": 0, "fit": 0}
    if lin.grid:
        nx = int((right - left) // lin.grid)
        ny = int((bottom - top) // lin.grid)
        gx = left + ((right - left) - nx * lin.grid) / 2
        gy = top + ((bottom - top) - ny * lin.grid) / 2
        for i in range(nx + 1):
            c.line(gx + i * lin.grid, gy, gx + i * lin.grid, gy + ny * lin.grid, 0.2, GRID)
        for j in range(ny + 1):
            c.line(gx, gy + j * lin.grid, gx + nx * lin.grid, gy + j * lin.grid, 0.2, GRID)
        info.update(rows=ny, fit=ny)
        return c.svg(), info
    if lin.margin_right:
        rand = right - lin.margin_right
        c.line(rand, top, rand, bottom, 0.3, "#c0392b")
        right_lines = right
    else:
        right_lines = right
    if not lin.offsets:
        return c.svg(), info
    houses = opts.get("house_symbols", lin.houses) and len(lin.offsets) == 4
    x0, x1 = left, right_lines
    if houses:
        w = lin.middle
        x0, x1 = left + w + 2, right_lines - w - 2
    positions, fit = system_positions(lin, top, bottom, opts.get("rows"))
    info.update(rows=len(positions), fit=fit)
    for y in positions:
        draw_system(
            c,
            lin,
            x0,
            x1,
            y,
            baseline_bold=opts.get("baseline_bold"),
            contrast=opts.get("contrast"),
        )
        if houses:
            _house(c, left, y, lin)
            _house(c, right_lines - lin.middle, y, lin)
    words = (opts.get("sample_words") or "").strip()
    if words and positions:
        size = lin.middle / X_HEIGHT
        base = positions[0] + lin.offsets[lin.baseline]
        c.text(x0 + 3, base, words, size, color=INK)
    return c.svg(), info


# --- Rechenaufgaben -----------------------------------------------------------------------

OPERATIONS = ("+", "-", "*", ":")
OP_SIGNS = {"+": "+", "-": "−", "*": "·", ":": ":"}
DIGIT = 1303 / 2048  # Breite einer Ziffer in DejaVu Sans je em
SOLUTION = "#1a5e1a"


@dataclass(frozen=True)
class Task:
    a: int
    op: str
    b: int
    result: int
    blank: str = "result"  # Lücke: result, a oder b

    def check(self) -> bool:
        if self.op == "+":
            return self.a + self.b == self.result
        if self.op == "-":
            return self.a - self.b == self.result
        if self.op == "*":
            return self.a * self.b == self.result
        return self.b != 0 and self.a == self.b * self.result

    def parts(self, solved: bool = False) -> tuple[str, str, str]:
        """(a, b, Ergebnis) als Text; die Lücke leer, außer ``solved``."""
        values = {"a": str(self.a), "b": str(self.b), "result": str(self.result)}
        if not solved:
            values[self.blank] = ""
        return values["a"], values["b"], values["result"]

    def text(self, solved: bool = False, gap: str = "___") -> str:
        a, b, r = self.parts(solved)
        return f"{a or gap} {OP_SIGNS[self.op]} {b or gap} = {r or gap}"


def _one(rng: random.Random, op: str, lo: int, hi: int) -> tuple[int, int, int] | None:
    low = max(lo, 1)  # keine Null als Zahl in der Aufgabe (zu leicht)
    if op == "+":
        if hi < 2 * low:
            return None
        r = rng.randint(2 * low, hi)
        a = rng.randint(low, r - low)
        return a, r - a, r
    if op == "-":
        if hi < low + lo:
            return None
        a = rng.randint(low + lo, hi)
        b = rng.randint(low, a - lo)
        return a, b, a - b
    top = 10 if hi <= 100 else 20  # Faktoren bzw. Teiler: kleines bzw. großes Einmaleins
    if op == "*":
        a, b = rng.randint(1, top), rng.randint(1, top)
        return (a, b, a * b) if lo <= a * b <= hi else None
    b, q = rng.randint(1, top), rng.randint(1, top)
    if lo <= b * q <= hi:
        return b * q, b, q
    return None


def make_tasks(
    operations, lo: int, hi: int, count: int, seed: int, blank: str = "result"
) -> list[Task]:
    """``count`` Aufgaben ohne Wiederholung (solange möglich), deterministisch für
    dieselben Angaben und ``seed``. Plus/Minus: alle Zahlen in [lo, hi];
    Mal/Geteilt: Produkt bzw. Dividend in [lo, hi], Faktoren bzw. Teiler bis 10
    (Zahlenraum bis 100) bzw. 20."""
    ops = [op for op in OPERATIONS if op in list(operations or ())] or ["+"]
    if not 0 <= lo < hi <= 1_000_000:
        raise SheetError("Zahlenraum ungültig: 0 ≤ von < bis ≤ 1 000 000.")
    rng = random.Random(f"{seed}:{','.join(ops)}:{lo}:{hi}:{blank}")
    tasks: list[Task] = []
    seen: set[tuple] = set()
    attempts = 0
    while len(tasks) < count and attempts < count * 200:
        attempts += 1
        op = rng.choice(ops)
        made = _one(rng, op, lo, hi)
        if made is None:
            continue
        a, b, r = made
        key = (a, op, b)
        if key in seen and attempts < count * 100:
            continue
        seen.add(key)
        gap = blank if blank in ("result", "a", "b") else rng.choice(("result", "a", "b"))
        tasks.append(Task(a, op, b, r, gap))
    if not tasks:
        raise SheetError("Mit diesen Rechenarten passen keine Aufgaben in den Zahlenraum.")
    return tasks


def times_table_tasks(rows, mixed: bool, with_division: bool, count: int | None, seed: int):
    """Einmaleins-Reihen (z. B. [2, 5]): k · 1 … k · 10, optional mit Geteilt."""
    rows = [r for r in rows if isinstance(r, int) and 1 <= r <= 20][:10] or [2]
    rng = random.Random(f"1x1:{seed}:{rows}:{mixed}:{with_division}")
    tasks = []
    for k in rows:
        for n in range(1, 11):
            tasks.append(Task(n, "*", k, n * k))
            if with_division:
                tasks.append(Task(n * k, ":", k, n))
    if mixed:
        rng.shuffle(tasks)
    if count:
        tasks = tasks[:count] if not mixed else (tasks * 3)[:count]
    return tasks


def task_pages(frame: Frame, tasks: list[Task], columns: int, solved: bool) -> list[str]:
    """Aufgabenliste in Spalten; Lücken als Schreiblinie, Lösungen fett."""
    pages, index = [], 0
    while index < len(tasks):
        c, top, bottom = frame.canvas("Lösungen" if solved else "")
        top += frame.font
        pitch = frame.font * 2.6
        per_col = max(1, int((bottom - top) // pitch))
        per_page = per_col * columns
        col_w = (frame.width - 2 * MARGIN) / columns
        size = frame.font
        chunk = tasks[index : index + per_page]
        rows = math.ceil(len(chunk) / columns)  # Spalten gleichmäßig füllen
        for slot, task in enumerate(chunk):
            col, row = divmod(slot, rows)
            x = MARGIN + col * col_w
            y = top + row * pitch + size
            c.text(x, y, f"{index + slot + 1})", size * 0.6, color="#777777")
            x += size * 1.7
            for kind, part in zip(("a", "b", "result"), task.parts(solved), strict=True):
                if part:
                    bold = solved and kind == task.blank
                    c.text(x, y, part, size, bold=bold, color=SOLUTION if bold else INK)
                    x += DIGIT * size * len(part) + size * 0.4
                else:
                    width = DIGIT * size * max(2, len(str(getattr(task, kind)))) + size * 0.8
                    c.line(x, y + 0.8, x + width, y + 0.8, 0.35, INK)
                    x += width + size * 0.4
                if kind != "result":
                    sign = OP_SIGNS[task.op] if kind == "a" else "="
                    c.text(x + size * 0.45, y, sign, size, anchor="middle")
                    x += size * 1.1
        index += per_page
        pages.append(c.svg())
    return pages


def grid_task_pages(frame: Frame, tasks: list[Task], columns: int, solved: bool) -> list[str]:
    """Rechenkästchen: 5-mm-Karo, eine Ziffer bzw. ein Zeichen je Kästchen."""
    cell = 5.0
    pages, index = [], 0
    while index < len(tasks):
        c, top, bottom = frame.canvas("Lösungen" if solved else "")
        left, right = MARGIN, frame.width - MARGIN
        nx, ny = int((right - left) // cell), int((bottom - top) // cell)
        gx = left + ((right - left) - nx * cell) / 2
        gy = top + ((bottom - top) - ny * cell) / 2
        for i in range(nx + 1):
            c.line(gx + i * cell, gy, gx + i * cell, gy + ny * cell, 0.2, GRID)
        for j in range(ny + 1):
            c.line(gx, gy + j * cell, gx + nx * cell, gy + j * cell, 0.2, GRID)
        col_cells = nx // columns
        per_col = (ny - 1) // 2
        per_page = per_col * columns
        chunk = tasks[index : index + per_page]
        rows = math.ceil(len(chunk) / columns)
        for slot, task in enumerate(chunk):
            col, row = divmod(slot, rows)
            cx = col * col_cells + 1
            cy = 1 + row * 2
            a, b, r = task.parts(solved)
            chars = []
            for value, width in (
                (a, len(str(task.a))),
                (OP_SIGNS[task.op], 1),
                (b, len(str(task.b))),
                ("=", 1),
                (r, len(str(task.result))),
            ):
                chars.extend(list(value) if value else [""] * width)
            if cx + len(chars) > (col + 1) * col_cells:
                continue
            for k, ch in enumerate(chars):
                if ch:
                    x = gx + (cx + k) * cell + cell / 2
                    y = gy + cy * cell + cell * 0.78
                    c.text(x, y, ch, 3.8, anchor="middle")
        index += per_page
        pages.append(c.svg())
    return pages


# --- Zahlenstrahl -------------------------------------------------------------------------


def number_line_pages(
    frame: Frame, lo: int, hi: int, step: int, count: int, gaps: int, seed: int, solved: bool
) -> list[str]:
    ticks = (hi - lo) // step
    if step < 1 or ticks < 2 or ticks > 100:
        raise SheetError("Zahlenstrahl: 2 bis 100 Striche (Bereich und Schritt anpassen).")
    rng = random.Random(f"zs:{seed}:{lo}:{hi}:{step}:{gaps}")
    label_every = 1 if ticks <= 20 else (5 if ticks <= 50 else 10)
    labelled = [i for i in range(ticks + 1) if i % label_every == 0]
    c, top, bottom = frame.canvas("Lösungen" if solved else "")
    pitch = (bottom - top) / max(1, count)
    left, right = MARGIN + 4, frame.width - MARGIN - 8
    unit = (right - left) / ticks
    for n in range(count):
        y = top + n * pitch + pitch * 0.45
        c.line(left - 3, y, right + 5, y, 0.5, INK)
        c.polygon(
            [(right + 7, y), (right + 4, y - 1.4), (right + 4, y + 1.4)],
            fill=INK,
            stroke=INK,
            width=0.2,
        )
        hidden = set(rng.sample(labelled[1:-1] or labelled, min(gaps, max(0, len(labelled) - 2))))
        for i in range(ticks + 1):
            x = left + i * unit
            major = i % label_every == 0
            five = label_every == 1 and i % 5 == 0
            length = 3.2 if major and label_every > 1 else (2.6 if five else 1.8)
            c.line(x, y - length, x, y + length, 0.4 if major else 0.25, INK)
            if major:
                value = lo + i * step
                size = min(
                    frame.font * 0.8, unit * label_every * 0.8 / max(1, len(str(value)) * 0.62)
                )
                if i in hidden and not solved:
                    box = max(size * 0.62 * len(str(value)) + 2, 6)
                    c.rect(x - box / 2, y + 4, box, size + 2, stroke=INK, width=0.3)
                else:
                    bold = solved and i in hidden
                    c.text(
                        x,
                        y + 4 + size,
                        str(value),
                        size,
                        anchor="middle",
                        bold=bold,
                        color=SOLUTION if bold else INK,
                    )
    return [c.svg()]


# --- Uhr ---------------------------------------------------------------------------------

PRECISIONS = {
    "voll": (0,),
    "halb": (0, 30),
    "viertel": (0, 15, 30, 45),
    "5min": tuple(range(0, 60, 5)),
    "1min": tuple(range(60)),
}


def make_times(precision: str, count: int, seed: int) -> list[tuple[int, int]]:
    minutes = PRECISIONS.get(precision, PRECISIONS["halb"])
    rng = random.Random(f"uhr:{seed}:{precision}")
    pool = [(h, m) for h in range(1, 13) for m in minutes]
    rng.shuffle(pool)
    return (pool * (count // len(pool) + 1))[:count]


def _clock(c: Canvas, cx: float, cy: float, r: float, time: tuple[int, int] | None) -> None:
    c.circle(cx, cy, r, stroke=INK, width=0.6)
    for i in range(60):
        angle = math.radians(i * 6)
        hour = i % 5 == 0
        inner = r - (3.0 if hour else 1.4)
        c.line(
            cx + inner * math.sin(angle),
            cy - inner * math.cos(angle),
            cx + r * math.sin(angle),
            cy - r * math.cos(angle),
            0.7 if hour else 0.2,
            INK,
        )
    size = r * 0.17
    for h in range(1, 13):
        angle = math.radians(h * 30)
        rr = r - 4.2 - size * 0.8
        c.text(
            cx + rr * math.sin(angle),
            cy - rr * math.cos(angle) + size * CAP_HEIGHT / 2,
            str(h),
            size,
            anchor="middle",
        )
    if time is not None:
        h, m = time
        for length, width, angle in (
            (r * 0.5, 1.6, (h % 12 + m / 60) * 30),
            (r * 0.78, 0.9, m * 6),
        ):
            a = math.radians(angle)
            c.line(cx, cy, cx + length * math.sin(a), cy - length * math.cos(a), width, INK)
    c.circle(cx, cy, 0.9, fill=INK, stroke="none")


def time_text(t: tuple[int, int]) -> str:
    return f"{t[0]}:{t[1]:02d} Uhr"


def clock_pages(frame: Frame, times, mode: str, solved: bool) -> list[str]:
    """``ablesen``: Zeiger gezeichnet, Zeit aufschreiben; ``einzeichnen``: Zeit
    vorgegeben, Ziffernblatt ohne Zeiger. Lösungsseite jeweils ausgefüllt."""
    cols = 3 if frame.width > frame.height else 2
    c, top, bottom = frame.canvas("Lösungen" if solved else "")
    rows = max(1, math.ceil(len(times) / cols))
    cell_w = (frame.width - 2 * MARGIN) / cols
    cell_h = (bottom - top) / rows
    r = max(8.0, min(cell_w * 0.38, (cell_h - 12) / 2))
    for i, t in enumerate(times):
        col, row = i % cols, i // cols
        cx = MARGIN + col * cell_w + cell_w / 2
        cy = top + row * cell_h + r + 2
        show_hands = mode == "ablesen" or solved
        _clock(c, cx, cy, r, t if show_hands else None)
        y = cy + r + 7
        if mode == "einzeichnen" or solved:
            c.text(
                cx,
                y,
                time_text(t),
                frame.font * 0.9,
                anchor="middle",
                bold=solved and mode == "ablesen",
            )
        else:
            c.line(cx - 16, y, cx + 4, y, 0.35, INK)
            c.text(cx + 6, y, "Uhr", frame.font * 0.8)
    return [c.svg()]


# --- Hundertertafel, Zwanzigerfeld, Stellenwerttafel, Geobrett ---------------------------


def hundred_chart_pages(frame: Frame, gaps: int, seed: int, solved: bool) -> list[str]:
    """Hundertertafel 1–100; ``gaps`` Felder bleiben leer (Lösung: grün fett)."""
    rng = random.Random(f"ht:{seed}:{gaps}")
    hidden = set(rng.sample(range(1, 101), min(gaps, 100)))
    c, top, bottom = frame.canvas("Lösungen" if solved else "")
    cell = min((frame.width - 2 * MARGIN) / 10, (bottom - top) / 10, 20.0)
    x0 = (frame.width - 10 * cell) / 2
    for i in range(11):
        width = BOLD_LINE if i in (0, 5, 10) else LINE
        c.line(x0 + i * cell, top, x0 + i * cell, top + 10 * cell, width, INK)
        c.line(x0, top + i * cell, x0 + 10 * cell, top + i * cell, width, INK)
    size = cell * 0.5
    for n in range(1, 101):
        x = x0 + ((n - 1) % 10 + 0.5) * cell
        y = top + ((n - 1) // 10) * cell + cell / 2 + size * CAP_HEIGHT / 2
        if n in hidden and not solved:
            continue
        bold = solved and n in hidden
        c.text(x, y, n, size, anchor="middle", bold=bold, color=SOLUTION if bold else INK)
    return [c.svg()]


def twenty_frame_pages(frame: Frame, count: int, mode: str, seed: int, solved: bool) -> list[str]:
    """Zwanzigerfelder (2 × 10, Fünferstruktur). ``ablesen``: Plättchen gelegt, Zahl
    schreiben; ``einzeichnen``: Zahl gegeben, Plättchen einzeichnen."""
    rng = random.Random(f"zf:{seed}:{count}:{mode}")
    numbers = [rng.randint(1, 20) for _ in range(count)]
    c, top, bottom = frame.canvas("Lösungen" if solved else "")
    cell = min((frame.width - 2 * MARGIN) / 10, 16.0)
    x0 = (frame.width - 10 * cell) / 2
    pitch = (bottom - top) / max(1, count)
    size = frame.font * 0.9
    for n, number in enumerate(numbers):
        y0 = top + n * pitch + 2
        for i in range(11):
            width = BOLD_LINE if i in (0, 5, 10) else LINE
            c.line(x0 + i * cell, y0, x0 + i * cell, y0 + 2 * cell, width, INK)
        for j in range(3):
            c.line(
                x0, y0 + j * cell, x0 + 10 * cell, y0 + j * cell, BOLD_LINE if j != 1 else LINE, INK
            )
        show = mode == "ablesen" or solved
        if show:
            for k in range(number):
                cx = x0 + (k % 10 + 0.5) * cell
                cy = y0 + (k // 10 + 0.5) * cell
                color = SOLUTION if solved and mode == "einzeichnen" else INK
                c.circle(cx, cy, cell * 0.35, fill=color, stroke=color, width=0.2)
        label_y = y0 + 2 * cell + size + 2
        if mode == "ablesen":
            if solved:
                c.text(x0, label_y, f"Zahl: {number}", size, bold=True, color=SOLUTION)
            else:
                c.text(x0, label_y, "Zahl:", size)
                c.line(x0 + size * 3, label_y + 0.6, x0 + size * 3 + 25, label_y + 0.6, 0.3, INK)
        else:
            c.text(x0, label_y, f"Zahl: {number}", size, bold=True)
    return [c.svg()]


PLACES = ("E", "Z", "H", "T", "ZT", "HT", "M")  # Einer … Millionen
PLACE_NAMES = {
    "E": "Einer",
    "Z": "Zehner",
    "H": "Hunderter",
    "T": "Tausender",
    "ZT": "Zehntausender",
    "HT": "Hunderttausender",
    "M": "Million",
}


def place_value_pages(frame: Frame, places: int, rows: int) -> list[str]:
    """Stellenwerttafel mit ``places`` Stellen (3–7) und ``rows`` leeren Zeilen."""
    if not 3 <= places <= 7:
        raise SheetError("Stellenwerttafel: 3 bis 7 Stellen (Zahlenraum anpassen).")
    c, top, bottom = frame.canvas()
    columns = list(reversed(PLACES[:places]))
    number_w = 40.0
    cell = (frame.width - 2 * MARGIN - number_w) / places
    head = 14.0
    row_h = min(18.0, (bottom - top - head) / max(1, rows))
    x0 = MARGIN
    size = frame.font * 0.9
    c.rect(x0, top, places * cell + number_w, head + rows * row_h, stroke=INK, width=BOLD_LINE)
    c.line(x0, top + head, x0 + places * cell + number_w, top + head, BOLD_LINE, INK)
    for i, key in enumerate(columns):
        x = x0 + i * cell
        if i:
            # Klassengrenze (nach je drei Stellen von rechts) verstärkt.
            heavy = (places - i) % 3 == 0
            c.line(x, top, x, top + head + rows * row_h, BOLD_LINE if heavy else LINE, INK)
        c.text(x + cell / 2, top + 6, key, size, anchor="middle", bold=True)
        c.text(x + cell / 2, top + 11, PLACE_NAMES[key], 2.6, anchor="middle", color=HELPER)
    xz = x0 + places * cell
    c.line(xz, top, xz, top + head + rows * row_h, BOLD_LINE, INK)
    c.text(xz + number_w / 2, top + 8, "Zahl", size, anchor="middle", bold=True)
    for j in range(1, rows):
        y = top + head + j * row_h
        c.line(x0, y, x0 + places * cell + number_w, y, LINE, INK)
    return [c.svg()]


def geoboard_pages(frame: Frame, count: int, size: int, columns: int) -> list[str]:
    """Geobrett-Vorlagen: ``size`` × ``size`` Nägel, ``count`` Bretter je Seite."""
    if not 3 <= size <= 11:
        raise SheetError("Geobrett: 3 bis 11 Nägel je Seite.")
    c, top, bottom = frame.canvas()
    columns = max(1, min(columns, count))
    rows = math.ceil(count / columns)
    slot_w = (frame.width - 2 * MARGIN) / columns
    slot_h = (bottom - top) / rows
    side = min(slot_w, slot_h) - 6
    spacing = side / size
    for n in range(count):
        col, row = n % columns, n // columns
        cx = MARGIN + (col + 0.5) * slot_w
        cy = top + (row + 0.5) * slot_h
        left, upper = cx - side / 2, cy - side / 2
        c.rect(left, upper, side, side, stroke=INK, width=BOLD_LINE, radius=2)
        for i in range(size):
            for j in range(size):
                px = left + (i + 0.5) * spacing
                py = upper + (j + 0.5) * spacing
                c.circle(px, py, max(0.6, spacing * 0.08), fill=INK, stroke=INK, width=0.2)
    return [c.svg()]


# --- Bausteine für Arbeitsblätter (Markdown-Layout) -------------------------------------------


def writing_lines_svg(width: float, rows: int, key: str = "1") -> str:
    """Schreiblinien als Antwortfeld (inline im Markdown-Layout), Breite in mm."""
    lin = LINEATUREN.get(key) or LINEATUREN["1"]
    if not lin.offsets:
        lin = LINEATUREN["4"]
    rows = max(1, min(int(rows), 20))
    pitch = lin.system_height + max(lin.gap, 4)
    height = rows * pitch - (pitch - lin.system_height) + 2
    c = Canvas(width, height)
    for i in range(rows):
        draw_system(c, lin, 0, width, 1 + i * pitch, baseline_bold=False, contrast=False)
    return c.svg()
