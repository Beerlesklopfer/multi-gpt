"""Kindprozess für ``create_pdf``: HTML -> PDF mit WeasyPrint.

Wird von ``documents_pdf.render`` als eigener Prozess gestartet
(``python -I -m multigpt.chat.pdf_render``), ohne Django und ohne die Umgebung
des Servers (keine Schlüssel, keine DB-Adresse). Gründe für den eigenen
Prozess: echtes Zeitlimit (ein Thread lässt sich nicht abbrechen), Grenzen
für Speicher und CPU-Zeit (``resource``) und ein Absturz von Pango/Cairo
trifft nicht den Server.

Protokoll: stdin JSON ``{"html": str, "max_pages": int}``; stdout das PDF.
Exit 0 = gut, 3 = zu viele Seiten (stdout: Seitenzahl), sonst Fehler. stderr
bleibt leer bzw. nur Fehlerart (keine Inhalte, keine URLs).

**Kein Nachladen:** ``LocalOnlyFetcher`` lehnt jede Adresse ab – außer
``data:image/…;base64`` (eingebettete Anhänge, vom Server eingesetzt). Damit
lädt WeasyPrint nichts aus dem Netz und nichts vom Dateisystem (``file://``),
auch nicht aus CSS, SVG oder HTML.
"""

from __future__ import annotations

import json
import logging
import re
import resource
import sys

MEMORY_BYTES = 1536 * 1024 * 1024
CPU_SECONDS = 90
EXIT_TOO_MANY_PAGES = 3
_DATA_IMAGE = re.compile(r"data:image/(png|jpeg|webp|svg\+xml);base64,", re.IGNORECASE)


def _limit() -> None:
    for kind, value in ((resource.RLIMIT_AS, MEMORY_BYTES), (resource.RLIMIT_CPU, CPU_SECONDS)):
        try:
            resource.setrlimit(kind, (value, value))
        except (ValueError, OSError):
            pass


def main() -> int:
    _limit()
    # WeasyPrint meldet Warnungen mit URLs und Textstellen: nicht ausgeben.
    logging.getLogger("weasyprint").setLevel(logging.CRITICAL + 1)
    logging.getLogger("fontTools").setLevel(logging.CRITICAL + 1)
    try:
        job = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        html, max_pages = str(job["html"]), int(job["max_pages"])
    except (ValueError, KeyError, TypeError):
        sys.stderr.write("Ungültiger Auftrag\n")
        return 2

    from weasyprint import HTML
    from weasyprint.urls import URLFetcher

    class LocalOnlyFetcher(URLFetcher):
        """Nur eingebettete Bilder (data:image/…;base64); alles andere abgelehnt."""

        def fetch(self, url, headers=None):
            if not _DATA_IMAGE.match(url or ""):
                raise ValueError("Laden verboten")
            return super().fetch(url, headers)

    fetcher = LocalOnlyFetcher(allowed_protocols=("data",), allow_redirects=False)
    try:
        document = HTML(string=html, base_url=None, url_fetcher=fetcher).render()
        if len(document.pages) > max_pages:
            sys.stdout.write(str(len(document.pages)))
            return EXIT_TOO_MANY_PAGES
        sys.stdout.buffer.write(document.write_pdf())
    except MemoryError:
        sys.stderr.write("MemoryError\n")
        return 4
    except Exception as exc:  # noqa: BLE001 - nur die Fehlerart melden
        sys.stderr.write(type(exc).__name__ + "\n")
        return 5
    return 0


if __name__ == "__main__":
    sys.exit(main())
