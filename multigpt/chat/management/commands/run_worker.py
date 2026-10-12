"""Hintergrund-Worker für die Job-Tabelle (M7-02): ``make worker``.

Läuft im Vordergrund (systemd-Unit ``multi-gpt-worker.service``), holt fällige
Jobs mit ``SKIP LOCKED`` und beendet sich bei SIGTERM/SIGINT sauber: Ein
laufender Job wird an der nächsten Prüfstelle (zwischen PDF-Seiten bzw.
Embedding-Paketen) unterbrochen und wieder eingereiht.

Einmal je Minute reiht er außerdem fällige Verzeichnisquellen zum Einlesen ein
(je Quelle höchstens ein offener Scan-Job), prüft fällige MCP-Server
(``chat/mcp/status.py``) und löscht alte Einträge im Audit-Log der API
(``node/audit.py``) und entlädt lokale Modelle nach Leerlauf
(``chat/model_manager.py``) und holt einmal täglich den EZB-Kurs USD
(``billing/ecb.py``) – ein eigener Timer ist nicht nötig.

Spuren über ``index_workers`` hinaus übernehmen nur eilige Aufträge (Hochladen,
gezielt neu indexieren), damit ein einzelnes Dokument nicht hinter langen
Dokumenten großer Läufe wartet.

Parallele Indexierung (C31): Neben dem Hauptprozess holen bis zu drei weitere
Prozesse (``run_worker --lane N``) Jobs ab, so viele wie
``RagSettings.index_workers`` minus eins (ohne Neustart änderbar). Prozesse statt
Threads, weil die Indexierung in Python rechnet (PDF-Text, Zerteilung): Threads
teilten sich wegen des GIL einen Kern und wurden nicht schneller (gemessen
2026-10-12). ``SKIP LOCKED`` verteilt die Jobs, Laden und Entladen der Modelle
läuft unter der Sperre je Anbieter (``model_manager``). Der Hauptprozess startet
die Spuren, startet abgestürzte neu und beendet sie mit sich; eine Spur endet
auch, wenn ihr Hauptprozess verschwindet. Mit ``--once`` (Tests, Fehlersuche)
gibt es keine Spuren.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections, connection

from multigpt.billing import ecb as billing_ecb
from multigpt.chat import attachments as chat_attachments
from multigpt.chat import model_manager, notices
from multigpt.chat.mcp import status as mcp_status
from multigpt.chat.models import RagSettings
from multigpt.chat.rag import jobs
from multigpt.node import audit as api_audit
from multigpt.node import sse as api_sse
from multigpt.rag import crawl

logger = logging.getLogger("multigpt.worker")

MAX_LANES = 4  # wie MaxValueValidator von RagSettings.index_workers
SETTINGS_TTL = 30.0  # Sekunden, so oft liest eine Spur die Einstellung neu
LANE_RESTART_DELAY = 60.0  # Sekunden zwischen Neustarts einer abgestürzten Spur


def wanted_lanes() -> int:
    try:
        value = RagSettings.load().index_workers
    except DatabaseError:
        return 1
    return max(1, min(MAX_LANES, value or 1))


def run_lane(number: int, stop: threading.Event, poll: float, parent: int | None = None) -> None:
    """Zusätzliche Spur: holt Jobs, solange ``index_workers`` sie vorsieht.
    ``parent``: PID des Hauptprozesses; ist er weg, endet die Spur."""
    active_until = 0.0
    active = False
    try:
        while not stop.is_set():
            if parent is not None and os.getppid() != parent:
                logger.warning("Spur %d: Hauptprozess beendet, Spur endet", number)
                break
            if not connection.in_atomic_block:
                close_old_connections()
            if time.monotonic() > active_until:
                active = wanted_lanes() > number
                active_until = time.monotonic() + SETTINGS_TTL
            try:
                # Über „Dokumente gleichzeitig“ hinaus nur Eiliges (Hochgeladenes):
                # Ein einzelnes Dokument wartet so nicht, bis große Läufe einen
                # regulären Platz freigeben.
                job = jobs.work_once(stop.is_set, urgent_only=not active)
            except DatabaseError as exc:
                logger.warning("Datenbankfehler in Spur %d: %s", number, type(exc).__name__)
                if connection.in_atomic_block:
                    raise
                connection.close()
                stop.wait(max(poll, 5.0))
                continue
            except Exception:
                # Ein Fehler im Job darf die Spur nicht beenden (run_job fängt das
                # normalerweise selbst ab).
                logger.exception("Unerwarteter Fehler in Spur %d", number)
                stop.wait(max(poll, 5.0))
                continue
            if job is None:
                stop.wait(poll)
    finally:
        if not connection.in_atomic_block:
            connection.close()


def lane_command(number: int, poll: float) -> list[str]:
    """Aufruf einer Spur: derselbe Interpreter mit denselben Schaltern (z. B. ``-P``
    im Paket) und demselben Einstieg (``manage.py`` bzw. ``-m django``)."""
    argv = list(getattr(sys, "orig_argv", []) or [sys.executable, *sys.argv])
    if "run_worker" in argv:
        argv = argv[: argv.index("run_worker") + 1]
    else:  # z. B. call_command aus einem anderen Einstieg
        argv = [sys.executable, "-m", "django", "run_worker"]
    return [*argv, "--lane", str(number), "--poll", str(poll)]


def start_lane(number: int, poll: float) -> subprocess.Popen:
    return subprocess.Popen(lane_command(number, poll), close_fds=True)


def stop_lanes(procs: dict[int, subprocess.Popen], timeout: float = 60.0) -> None:
    """SIGTERM an alle Spuren; laufende Jobs halten an der nächsten Prüfstelle."""
    for proc in procs.values():
        if proc.poll() is None:
            proc.terminate()
    deadline = time.monotonic() + timeout
    for proc in procs.values():
        try:
            proc.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            proc.kill()


class Command(BaseCommand):
    help = "Hintergrundjobs (Indexierung von Dokumenten) abarbeiten."

    def add_arguments(self, parser):
        parser.add_argument(
            "--once",
            action="store_true",
            help="Alle fälligen Jobs abarbeiten und dann beenden.",
        )
        parser.add_argument(
            "--poll",
            type=float,
            default=3.0,
            help="Wartezeit in Sekunden, wenn kein Job fällig ist (Standard 3).",
        )
        parser.add_argument(
            "--lane",
            type=int,
            default=0,
            help="Intern: als zusätzliche Spur N laufen (startet der Worker selbst).",
        )

    def handle(self, *args, once=False, poll=3.0, lane=0, **options):
        stop = threading.Event()

        def on_signal(signum, frame):
            if not stop.is_set():
                logger.info("Signal %s empfangen, Worker wird beendet", signum)
            stop.set()

        previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
        if lane:
            logger.info("Spur %d gestartet", lane)
            try:
                run_lane(lane, stop, poll, parent=os.getppid())
            finally:
                for sig, handler in previous.items():
                    signal.signal(sig, handler)
                logger.info("Spur %d beendet", lane)
            return
        logger.info("Worker gestartet%s", " (einmalig)" if once else "")
        last_stale_check = 0.0
        lanes: dict[int, subprocess.Popen] = {}
        started: dict[int, float] = {}
        # Nicht in einer Transaktion (Tests) und nicht mit --once.
        with_lanes = not once and not connection.in_atomic_block
        try:
            while not stop.is_set():
                if with_lanes:
                    now = time.monotonic()
                    for number in range(1, MAX_LANES):
                        proc = lanes.get(number)
                        if proc is not None and proc.poll() is None:
                            continue
                        # Abgestürzte Spur höchstens einmal je Minute neu starten.
                        if now - started.get(number, -LANE_RESTART_DELAY) < LANE_RESTART_DELAY:
                            continue
                        if proc is not None:
                            logger.warning(
                                "Spur %d beendet (Code %s), wird neu gestartet",
                                number,
                                proc.returncode,
                            )
                        lanes[number] = start_lane(number, poll)
                        started[number] = now
                # Wie zwischen zwei Requests: veraltete/kaputte Verbindungen verwerfen
                # (nicht innerhalb einer Transaktion, z. B. im Test).
                if not connection.in_atomic_block:
                    close_old_connections()
                try:
                    if time.monotonic() - last_stale_check > 60:
                        jobs.requeue_stale()
                        # Verzeichnisquellen (crawler): fällige Scans einreihen.
                        crawl.enqueue_due_scans()
                        # Anhänge im Chat: Entwürfe älter als 24 h löschen.
                        chat_attachments.cleanup_drafts()
                        # MCP-Server: Status und Werkzeugliste (online alle 5 Min.,
                        # offline jede Minute; Anspruch gegen Doppelprüfung).
                        mcp_status.check_due()
                        # Audit-Log der API-Aufrufe (M15): alte Einträge löschen.
                        api_audit.cleanup()
                        # Legacy-SSE (M15): Sitzungen ohne lebenden Stream löschen.
                        api_sse.cleanup()
                        # Modellverwaltung (LM Studio): nach Leerlauf entladen.
                        model_manager.unload_idle()
                        # Mitteilungen (A15): gelesene nach 90 Tagen löschen.
                        notices.cleanup()
                        # EZB-Kurs USD (A11): einmal täglich, Fehler nur als Warnung.
                        billing_ecb.auto_fetch()
                        last_stale_check = time.monotonic()
                    job = jobs.work_once(stop.is_set)
                except DatabaseError as exc:
                    # Datenbank kurz weg (Neustart, Netz): Verbindung verwerfen, warten.
                    logger.warning("Datenbankfehler im Worker: %s", type(exc).__name__)
                    if once or connection.in_atomic_block:
                        raise
                    connection.close()
                    stop.wait(max(poll, 5.0))
                    continue
                if job is None:
                    if once:
                        break
                    stop.wait(poll)
        finally:
            stop.set()
            stop_lanes(lanes)
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            logger.info("Worker beendet")
