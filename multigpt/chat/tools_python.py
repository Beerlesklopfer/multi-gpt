"""Werkzeug ``run_python``: Berechnungen in der Sandbox (M4a-10).

Modelle sollen rechnen statt schätzen: Numerik (numpy), Symbolik (sympy),
beliebige Genauigkeit (mpmath), Statistik, Einheiten-Umrechnungen und
Diagramme (matplotlib). Ausgeführt wird nur in ``sandbox`` (bubblewrap, ohne
Netz, ohne Server-Dateien); ohne funktionierende Sandbox wird das Werkzeug gar
nicht angeboten.

Entscheidungen:

- **Angeboten** für Modelle mit ``supports_tools`` (``tooling.builtin_bindings``),
  wenn ``ChatSettings.python_enabled``, Recht ``COMPUTE`` („Berechnungen
  ausführen“) und Sandbox verfügbar. Die MCP-Freigabe je Modell (``mcp_access``)
  gilt nicht: Auch ein nicht vertrauenswürdiges lokales Modell darf rechnen, die
  Sandbox deckt das.
- **Ohne Rückfrage**, weil die Sandbox es hält; der Verwalter kann sie mit
  ``ChatSettings.python_confirm`` einschalten (wie bei der Bilderzeugung).
- **Ausführung blockierend** im Thread der Antwort, wie MCP-Aufrufe (Zeitlimit
  des Servers): Die Antwort wartet ohnehin auf das Ergebnis, und die Wanduhr
  (höchstens 120 s) liegt weit unter dem gunicorn-Timeout. Über den Worker
  ginge es nur mit Warteschlange und Polling – mehr Verzögerung, kein Gewinn
  an Sicherheit (die Grenze ist bwrap, nicht der Prozess). Gleichzeitige Läufe
  je Prozess begrenzt ``sandbox.MAX_PARALLEL``.
- **Ergebnis an das Modell:** Exit-Status, Laufzeit, stdout und stderr
  (gekürzt), bei Fehlern der Traceback nur mit Zeilen aus dem eigenen Code
  (keine Serverpfade; Bibliothekspfade liegen ohnehin unter /sandbox/lib).
- **Dateien:** PNG, JPEG, WebP und SVG aus dem Arbeitsordner werden Anhänge der
  Antwort (wie erzeugte Bilder: ``owner`` leer, ohne Verknüpfung zum
  Werkzeugaufruf, damit sie in der Antwort mit Vorschau erscheinen). Raster
  werden über ``attachments.process_image`` neu kodiert (ohne Metadaten), SVG
  über ``svg_clean`` bereinigt und nur als ``<img>``/Download gezeigt.
  PDF (z. B. ``plt.savefig('blatt.pdf')`` für Blätter mit freier Geometrie in
  mm) wird nach Prüfung (Kopf ``%PDF-``, lesbar mit pypdf, höchstens
  ``MAX_PDF_PAGES`` Seiten) als Datei-Anhang übernommen; der Inhalt bleibt
  nicht vertrauenswürdig (Auslieferung siehe ``api_attachments``).
  Höchstens ``MAX_FILES_PER_ANSWER`` je Antwort.
- **Logs:** nur IDs, Dauer, Exit-Status (``sandbox``) – nie Code oder Ausgabe.
"""

from __future__ import annotations

import io
import logging
import re

from django.core.files.base import ContentFile
from django.db import transaction

from multigpt.accounts.permissions import Action, can

from . import attachments as chat_attachments
from . import sandbox, svg_clean, tooling
from .models import Attachment, ChatSettings
from .providers.base import ToolSpec

logger = logging.getLogger(__name__)

TOOL_NAME = "run_python"
TOOL_LABEL = "Berechnungen"
MAX_CODE_CHARS = 50_000
MAX_FILES_PER_RUN = 6
MAX_FILES_PER_ANSWER = 12
MAX_RASTER_BYTES = 5 * 1024 * 1024
MAX_SVG_BYTES = svg_clean.MAX_BYTES
RASTER_SUFFIXES = (".png", ".jpg", ".jpeg", ".webp")
MAX_PDF_PAGES = 50

MSG_NO_CODE = "Bitte Python-Code im Argument „code“ angeben."
MSG_TOO_LONG = f"Der Code ist zu lang (höchstens {MAX_CODE_CHARS} Zeichen)."
MSG_UNAVAILABLE = "Berechnungen sind gerade nicht möglich (Sandbox nicht verfügbar)."
MSG_BUSY = "Gerade laufen zu viele Berechnungen. Bitte gleich noch einmal versuchen."
ABORT_TEXTS = {
    "timeout": "Abgebrochen: Zeitlimit von {wall} s überschritten.",
    "cpu": "Abgebrochen: CPU-Zeit-Grenze von {cpu} s erreicht.",
    "file_size": "Abgebrochen: Datei größer als {file_mb} MB.",
    "output": "Abgebrochen: viel zu viel Ausgabe (Grenze {output_kb} KB).",
    "signal": "Abgebrochen durch Signal {signal} (z. B. Speicher des Rechners knapp).",
}

DESCRIPTION = (
    "Führt Python-Code für Berechnungen aus, mit numpy, sympy und mpmath sowie "
    "matplotlib für Diagramme. Kein Netz, keine Dateien außerhalb eines leeren "
    "Arbeitsordners. Ergebnis per print ausgeben. Nutze es für exakte bzw. numerische "
    "Rechnungen (Gleichungen, Integrale, Statistik, Einheiten, große Zahlen) statt "
    "Kopfrechnen. Für Diagramme matplotlib nutzen und mit plt.savefig('diagramm.png') "
    "bzw. plt.savefig('diagramm.svg') speichern; die Dateien erscheinen in der Antwort. "
    "Ein Blatt mit eigener, exakter Geometrie (Maße in mm) als PDF speichern, z. B. "
    "plt.figure(figsize=(210/25.4, 297/25.4)) und plt.savefig('blatt.pdf'). "
    "Jeder Aufruf startet frisch (keine Variablen aus früheren Aufrufen), CPU-Zeit und "
    "Speicher sind begrenzt."
)


def settings_obj() -> ChatSettings:
    """Gespeicherte Einstellungen, ohne Datensatz die Vorgaben (ohne zu schreiben)."""
    return ChatSettings.objects.filter(pk=ChatSettings.SINGLETON_PK).first() or ChatSettings()


def limits_from(cfg: ChatSettings) -> sandbox.Limits:
    return sandbox.Limits(
        cpu_seconds=cfg.python_cpu_seconds,
        wall_seconds=cfg.python_wall_seconds,
        memory_mb=cfg.python_memory_mb,
        processes=cfg.python_processes,
        file_mb=cfg.python_file_mb,
        output_kb=cfg.python_output_kb,
        max_files=MAX_FILES_PER_RUN,
        max_file_bytes=MAX_RASTER_BYTES,
    ).clamped()


def _tool_available(user, ai_model) -> bool:
    cfg = settings_obj()
    return bool(cfg.python_enabled and can(user, Action.COMPUTE) and sandbox.available())


def tool_needs_confirmation() -> bool:
    return bool(settings_obj().python_confirm)


# --- Dateien als Anhänge -------------------------------------------------------------


def _clean_stem(name: str) -> str:
    stem = re.sub(r"[^\w.-]+", "-", name.rsplit(".", 1)[0], flags=re.UNICODE).strip("-.")
    return stem[:60] or "diagramm"


def _store_raster(message, item: sandbox.OutputFile) -> Attachment:
    result = chat_attachments.process_image(io.BytesIO(item.data))
    attachment = Attachment(
        message=message,
        owner=None,  # erzeugt, nicht hochgeladen
        conversation=message.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type=result["mime_type"],
        width=result["width"],
        height=result["height"],
        size=len(result["data"]),
        original_name=_clean_stem(item.name) + result["extension"],
    )
    _save(attachment, "rechnung" + result["extension"], result["data"], result["thumbnail"])
    return attachment


def _store_svg(message, item: sandbox.OutputFile) -> Attachment:
    data, width, height = svg_clean.clean(item.data)
    attachment = Attachment(
        message=message,
        owner=None,
        conversation=message.conversation,
        kind=Attachment.Kind.IMAGE,
        mime_type="image/svg+xml",
        width=width,
        height=height,
        size=len(data),
        original_name=_clean_stem(item.name) + ".svg",
    )
    _save(attachment, "rechnung.svg", data, None)
    return attachment


def _store_pdf(message, item: sandbox.OutputFile) -> Attachment:
    """PDF aus der Sandbox prüfen (Kopf, lesbar, Seitenzahl) und als Datei speichern."""
    from pypdf import PdfReader
    from pypdf.errors import PyPdfError

    if not item.data.startswith(b"%PDF-"):
        raise chat_attachments.UploadError("Kein PDF.")
    try:
        pages = len(PdfReader(io.BytesIO(item.data)).pages)
    except (PyPdfError, ValueError, KeyError, TypeError, OSError) as exc:
        raise chat_attachments.UploadError("PDF nicht lesbar.") from exc
    if not 1 <= pages <= MAX_PDF_PAGES:
        raise chat_attachments.UploadError("Zu viele Seiten.")
    attachment = Attachment(
        message=message,
        owner=None,
        conversation=message.conversation,
        kind=Attachment.Kind.FILE,
        mime_type="application/pdf",
        size=len(item.data),
        original_name=_clean_stem(item.name) + ".pdf",
    )
    _save(attachment, "rechnung.pdf", item.data, None)
    return attachment


def _save(attachment: Attachment, name: str, data: bytes, thumbnail: bytes | None) -> None:
    try:
        with transaction.atomic():
            attachment.file.save(name, ContentFile(data), save=False)
            if thumbnail is not None:
                attachment.thumbnail.save("thumb.webp", ContentFile(thumbnail), save=False)
            attachment.save()
    except Exception:
        chat_attachments.delete_files([attachment], immediately=True)
        raise


def store_files(message, files: list[sandbox.OutputFile]) -> tuple[list[Attachment], list[str]]:
    """Dateien prüfen und als Anhänge der Antwort speichern; (Anhänge, verworfen)."""
    stored, rejected = [], []
    already = Attachment.objects.filter(
        message=message, owner__isnull=True, generated_by_model__isnull=True, tool_call__isnull=True
    ).count()
    for item in files:
        lower = item.name.lower()
        if already + len(stored) >= MAX_FILES_PER_ANSWER:
            rejected.append(f"{item.name} (höchstens {MAX_FILES_PER_ANSWER} Dateien je Antwort)")
            continue
        try:
            if lower.endswith(".svg") and len(item.data) <= MAX_SVG_BYTES:
                stored.append(_store_svg(message, item))
            elif lower.endswith(RASTER_SUFFIXES) and len(item.data) <= MAX_RASTER_BYTES:
                stored.append(_store_raster(message, item))
            elif lower.endswith(".pdf") and len(item.data) <= MAX_RASTER_BYTES:
                stored.append(_store_pdf(message, item))
            else:
                rejected.append(f"{item.name} (Format oder Größe)")
        except (svg_clean.SvgError, chat_attachments.UploadError):
            kind = "PDF" if lower.endswith(".pdf") else "Bild"
            rejected.append(f"{item.name} (kein gültiges {kind})")
        except Exception as exc:  # noqa: BLE001 - ein Anhang darf das Ergebnis nicht verlieren
            logger.error("Anhang aus Berechnung (Nachricht %s): %s", message.pk, type(exc).__name__)
            rejected.append(f"{item.name} (Speichern fehlgeschlagen)")
    return stored, rejected


# --- Werkzeug ----------------------------------------------------------------------------


def format_result(result: sandbox.RunResult, limits: sandbox.Limits, stored=(), rejected=()) -> str:
    """Text an das Modell: Status, Laufzeit, Ausgabe, Dateien."""
    seconds = f"{result.duration:.2f}".replace(".", ",")
    if result.aborted:
        head = ABORT_TEXTS[result.aborted].format(
            wall=limits.wall_seconds,
            cpu=limits.cpu_seconds,
            file_mb=limits.file_mb,
            output_kb=limits.output_kb,
            signal=result.signal,
        )
        lines = [f"{head} Laufzeit {seconds} s."]
    else:
        lines = [f"Exit-Status {result.exit_code}, Laufzeit {seconds} s."]
    if result.stdout:
        lines += ["", "Ausgabe (stdout):", result.stdout.rstrip("\n")]
    if result.stderr:
        lines += ["", "Fehlerausgabe (stderr):", result.stderr.rstrip("\n")]
    if not result.stdout and not result.stderr:
        lines += ["", "(Keine Ausgabe – Ergebnisse mit print ausgeben.)"]
    if result.truncated:
        lines += ["", f"[Ausgabe gekürzt, höchstens {limits.output_kb} KB.]"]
    if stored:
        names = ", ".join(f"{a.original_name} (Anhang #{a.pk})" for a in stored)
        lines += [
            "",
            f"Dateien als Anhang gespeichert und dem Nutzer in der Antwort angezeigt: {names}. "
            "Nicht erneut als Code, Text oder Bild ausgeben.",
        ]
    skipped = list(rejected) + [f"{n} (Grenze für Anzahl oder Größe)" for n in result.skipped_files]
    if skipped:
        lines += ["", "Nicht übernommen: " + ", ".join(skipped) + "."]
    return "\n".join(lines)


def _tool_run(user, arguments: dict, sources) -> tooling.BuiltinResult:
    args = arguments if isinstance(arguments, dict) else {}
    code = args.get("code")
    if not isinstance(code, str) or not code.strip():
        return tooling.BuiltinResult(MSG_NO_CODE, True)
    if len(code) > MAX_CODE_CHARS:
        return tooling.BuiltinResult(MSG_TOO_LONG, True)
    cfg = settings_obj()
    if not (cfg.python_enabled and can(user, Action.COMPUTE)):
        return tooling.BuiltinResult(tooling.MSG_NOT_ALLOWED, True)
    limits = limits_from(cfg)
    try:
        result = sandbox.run(code, limits)
    except sandbox.SandboxBusy:
        return tooling.BuiltinResult(MSG_BUSY, True)
    except sandbox.SandboxUnavailable:
        return tooling.BuiltinResult(MSG_UNAVAILABLE, True)
    message = sources.message
    stored, rejected = store_files(message, result.files) if result.files else ([], [])
    logger.info(
        "Berechnung für Nachricht %s (Konto %s): Exit %s, Abbruch %s, %d ms, %d Anhang/Anhänge",
        message.pk,
        user.pk,
        result.exit_code,
        result.aborted or "-",
        int(result.duration * 1000),
        len(stored),
    )
    return tooling.BuiltinResult(format_result(result, limits, stored, rejected), not result.ok)


def tool_spec() -> ToolSpec:
    return ToolSpec(
        name=TOOL_NAME,
        description=DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Vollständiges Python-3-Programm; Ergebnisse mit print "
                    "ausgeben. Verfügbar: numpy, sympy, mpmath, matplotlib (Agg) und die "
                    "Standardbibliothek (math, fractions, decimal, statistics, …).",
                }
            },
            "required": ["code"],
        },
    )


def register() -> None:
    tooling.register_builtin(
        tooling.BuiltinTool(
            name=TOOL_NAME,
            label=TOOL_LABEL,
            spec=tool_spec(),
            available=_tool_available,
            run=_tool_run,
            confirm=tool_needs_confirmation,
        )
    )


register()
