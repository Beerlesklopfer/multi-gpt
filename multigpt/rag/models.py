"""Proxy-Modelle für den Admin-Abschnitt „Dokumente (RAG)“.

Keine eigenen Tabellen: Die Proxys bündeln die RAG-Modelle der App ``chat``
unter einem eigenen Abschnitt im Admin. ``RagOverview`` trägt die Übersichtsseite
(ein Eintrag im Abschnitt, keine eigenen Daten).

Einzige eigene Tabelle: ``DirectorySource`` (Verzeichnisquellen, Agent crawler).
"""

from django.core.validators import MinValueValidator
from django.db import models

from multigpt.chat.models import Collection, Document, Job, RagSettings


class RagOverview(RagSettings):
    class Meta:
        proxy = True
        verbose_name = "RAG-Übersicht"
        verbose_name_plural = "RAG-Übersicht"


class RagSettingsProxy(RagSettings):
    class Meta:
        proxy = True
        verbose_name = "Einstellungen"
        verbose_name_plural = "Einstellungen"


class CollectionProxy(Collection):
    class Meta:
        proxy = True
        verbose_name = "Sammlung"
        verbose_name_plural = "Sammlungen"


class DocumentProxy(Document):
    class Meta:
        proxy = True
        verbose_name = "Dokument"
        verbose_name_plural = "Dokumente"


class JobProxy(Job):
    class Meta:
        proxy = True
        verbose_name = "Indexierungsauftrag"
        verbose_name_plural = "Indexierungsaufträge"


# --- Verzeichnisquellen (Agent crawler) -----------------------------------------

DEFAULT_INCLUDE_PATTERNS = "*.pdf, *.docx, *.txt, *.md"
DEFAULT_EXCLUDE_PATTERNS = ".*, ~$*, *.tmp, *.part"
MIN_INTERVAL_MINUTES = 5


class DirectorySource(models.Model):
    """Ein Verzeichnis auf dem Server/NAS, aus dem eine Sammlung periodisch
    eingelesen wird. Die Dateien bleiben am Ort; Dokumente der Quelle verweisen
    über ``Document.source``/``source_path`` darauf."""

    collection = models.ForeignKey(
        Collection,
        on_delete=models.CASCADE,
        related_name="directory_sources",
        verbose_name="Sammlung",
    )
    # Echter Pfad (realpath) zum Zeitpunkt der Anlage; wird bei jedem Einlesen
    # erneut gegen RAG_SOURCE_ROOTS geprüft. Nach der Anlage nicht änderbar.
    path = models.CharField("Verzeichnis", max_length=1000)
    recursive = models.BooleanField("Unterordner einbeziehen", default=True)
    include_patterns = models.CharField(
        "Dateimuster",
        max_length=500,
        default=DEFAULT_INCLUDE_PATTERNS,
        help_text="Kommagetrennt, z. B. „*.pdf, *.docx“. Nur PDF, DOCX, TXT und MD werden "
        "verarbeitet.",
    )
    exclude_patterns = models.CharField(
        "Ausschlussmuster",
        max_length=500,
        default=DEFAULT_EXCLUDE_PATTERNS,
        blank=True,
        help_text="Kommagetrennt; gilt für Datei- und Ordnernamen sowie relative Pfade "
        "(z. B. „Entwürfe/*“). Versteckte Dateien und Ordner (Name beginnt mit „.“) werden "
        "immer übersprungen.",
    )
    interval_minutes = models.PositiveIntegerField(
        "Intervall (Minuten)",
        default=60,
        validators=[MinValueValidator(MIN_INTERVAL_MINUTES)],
        help_text=f"Wie oft das Verzeichnis eingelesen wird (mindestens {MIN_INTERVAL_MINUTES}).",
    )
    active = models.BooleanField("aktiv", default=True)
    last_scan_started = models.DateTimeField("letzter Lauf begonnen", null=True, blank=True)
    last_scan_finished = models.DateTimeField("letzter Lauf beendet", null=True, blank=True)
    # Zahlen des letzten Laufs: new, changed, unchanged, deleted, skipped, errors, truncated.
    last_result = models.JSONField("Ergebnis des letzten Laufs", default=dict, blank=True)
    last_error = models.TextField("letzter Fehler", blank=True)
    created = models.DateTimeField("angelegt", auto_now_add=True)

    class Meta:
        ordering = ["collection__name", "pk"]
        verbose_name = "Verzeichnisquelle"
        verbose_name_plural = "Verzeichnisquellen"

    def __str__(self):
        return f"Verzeichnisquelle {self.pk} ({self.collection})"
