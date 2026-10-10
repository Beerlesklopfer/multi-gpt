"""Hintergrund-Worker für die Job-Tabelle (M7-02): ``make worker``.

Läuft im Vordergrund (systemd-Unit ``multi-gpt-worker.service``), holt fällige
Jobs mit ``SKIP LOCKED`` und beendet sich bei SIGTERM/SIGINT sauber: Ein
laufender Job wird an der nächsten Prüfstelle (zwischen PDF-Seiten bzw.
Embedding-Paketen) unterbrochen und wieder eingereiht.

Einmal je Minute reiht er außerdem fällige Verzeichnisquellen zum Einlesen ein
(je Quelle höchstens ein offener Scan-Job) und prüft fällige MCP-Server
(``chat/mcp/status.py``) – ein eigener Timer ist nicht nötig.
"""

import logging
import signal
import threading
import time

from django.core.management.base import BaseCommand
from django.db import DatabaseError, close_old_connections, connection

from multigpt.chat import attachments as chat_attachments
from multigpt.chat.mcp import status as mcp_status
from multigpt.chat.rag import jobs
from multigpt.rag import crawl

logger = logging.getLogger("multigpt.worker")


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

    def handle(self, *args, once=False, poll=3.0, **options):
        stop = threading.Event()

        def on_signal(signum, frame):
            if not stop.is_set():
                logger.info("Signal %s empfangen, Worker wird beendet", signum)
            stop.set()

        previous = {sig: signal.signal(sig, on_signal) for sig in (signal.SIGTERM, signal.SIGINT)}
        logger.info("Worker gestartet%s", " (einmalig)" if once else "")
        last_stale_check = 0.0
        try:
            while not stop.is_set():
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
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            logger.info("Worker beendet")
