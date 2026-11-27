"""Hilfen für die Verwaltungsbefehle (``mgpt-ctl``): Tabellen, Ein/Aus, Zahlen."""

from decimal import Decimal, InvalidOperation

from django.core.management.base import CommandError

ON = ("an", "ein", "on", "ja", "yes", "true", "1")
OFF = ("aus", "off", "nein", "no", "false", "0")


def table(stdout, headers, rows, empty="Keine Einträge.") -> None:
    """Spalten nach Breite ausgerichtet; leere Liste -> ``empty``."""
    rows = [tuple("" if c is None else str(c) for c in row) for row in rows]
    if not rows:
        stdout.write(empty)
        return
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]

    def line(cells) -> str:
        return "  ".join(f"{c:<{w}}" for c, w in zip(cells, widths, strict=True)).rstrip()

    stdout.write(line(headers))
    stdout.write(line("-" * w for w in widths))
    for row in rows:
        stdout.write(line(row))


def switch(value: str, name: str) -> bool:
    """„an“/„aus“ (auch on/off, ja/nein, 1/0) -> bool."""
    text = str(value).strip().lower()
    if text in ON:
        return True
    if text in OFF:
        return False
    raise CommandError(f"{name}: „an“ oder „aus“ erwartet, nicht „{value}“.")


def decimal(value: str, name: str, *, low: Decimal = Decimal(0)) -> Decimal:
    try:
        number = Decimal(str(value).replace(",", "."))
    except InvalidOperation as exc:
        raise CommandError(f"{name}: Zahl erwartet, nicht „{value}“.") from exc
    if number < low:
        raise CommandError(f"{name}: mindestens {low}.")
    return number


def yesno(flag: bool) -> str:
    return "an" if flag else "aus"
