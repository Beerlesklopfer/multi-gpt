"""Brücke sync ↔ async (Plan 8g, M4a-01).

Das MCP-SDK ist asyncio-basiert, Django läuft synchron in gunicorn-``gthread``-
Threads. Je Prozess gibt es genau einen Daemon-Thread mit einem dauerhaften
Event-Loop; alle MCP-Sitzungen leben ausschließlich dort. Request-Threads
übergeben Coroutinen mit :func:`asyncio.run_coroutine_threadsafe` und warten mit
``future.result(timeout=…)``; bei Zeitüberschreitung wird das Future abgebrochen.

Der Loop-Thread startet verzögert beim ersten Aufruf (Double-Checked Locking mit
einem ``threading.Lock``). Threads überleben einen Fork nicht: Stimmt die
gespeicherte PID nicht mit ``os.getpid()`` überein, startet der Prozess seinen
eigenen Loop. Aufräumen über :func:`shutdown` (gunicorn-Hook ``worker_exit`` und
``atexit``).

Im Loop darf kein Django-ORM-Zugriff stattfinden (eigene DB-Verbindung je
Thread, nie geschlossen). Die Sync-API liest alles Nötige vorher im
Request-Thread aus.
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import os
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from .errors import McpTimeout

logger = logging.getLogger(__name__)

# Zusätzliche Wartezeit des Request-Threads über das Zeitlimit hinaus: Die
# Coroutine bricht sich mit ``asyncio.timeout`` selbst ab und liefert dann eine
# saubere Meldung; ``future.result`` ist nur die Rückfallsicherung.
GRACE_SECONDS = 2.0
SHUTDOWN_TIMEOUT = 10.0

_start_lock = threading.Lock()
_loop: asyncio.AbstractEventLoop | None = None
_thread: threading.Thread | None = None
_pid: int | None = None
_atexit_registered = False

# Async-Aufräumfunktionen, die beim Beenden im Loop laufen (z. B. Sitzungen schließen).
_shutdown_hooks: list[Callable[[], Awaitable[None]]] = []


def register_shutdown_hook(hook: Callable[[], Awaitable[None]]) -> None:
    if hook not in _shutdown_hooks:
        _shutdown_hooks.append(hook)


def _running() -> bool:
    return (
        _loop is not None
        and _pid == os.getpid()
        and _thread is not None
        and _thread.is_alive()
        and not _loop.is_closed()
    )


def _run_loop(loop: asyncio.AbstractEventLoop, ready: threading.Event) -> None:
    asyncio.set_event_loop(loop)
    loop.call_soon(ready.set)
    try:
        loop.run_forever()
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
        finally:
            loop.close()


def get_loop() -> asyncio.AbstractEventLoop:
    """Liefert den Loop dieses Prozesses und startet ihn bei Bedarf (genau einmal)."""
    global _loop, _thread, _pid, _atexit_registered
    if _running():  # schneller Weg ohne Sperre
        return _loop  # type: ignore[return-value]
    with _start_lock:
        if _running():
            return _loop  # type: ignore[return-value]
        # Nach einem Fork (PID gewechselt) gehören Loop und Thread dem Elternprozess:
        # nicht anfassen, nur vergessen. Ihr Thread existiert hier ohnehin nicht.
        loop = asyncio.new_event_loop()
        ready = threading.Event()
        thread = threading.Thread(
            target=_run_loop, args=(loop, ready), name="mcp-loop", daemon=True
        )
        thread.start()
        ready.wait()
        _loop, _thread, _pid = loop, thread, os.getpid()
        if not _atexit_registered:
            atexit.register(shutdown)
            _atexit_registered = True
        logger.info("MCP-Loop-Thread gestartet (PID %s)", _pid)
        return loop


def is_running() -> bool:
    """Läuft der Loop dieses Prozesses schon (ohne ihn zu starten)?"""
    return _running()


def in_loop_thread() -> bool:
    return _thread is not None and threading.current_thread() is _thread


def run[T](coro: Coroutine[Any, Any, T], timeout: float) -> T:
    """Führt ``coro`` im Loop-Thread aus und wartet höchstens ``timeout`` Sekunden.

    Bei Zeitüberschreitung wird die Coroutine abgebrochen und :class:`McpTimeout`
    ausgelöst; der Request-Thread blockiert nicht länger.
    """
    if in_loop_thread():
        coro.close()
        raise RuntimeError("bridge.run() darf nicht im MCP-Loop-Thread aufgerufen werden")
    loop = get_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError:
        future.cancel()
        raise McpTimeout(f"Zeitüberschreitung nach {timeout:g} s.") from None
    except concurrent.futures.CancelledError:
        raise McpTimeout("Der Aufruf wurde abgebrochen.") from None


def submit(coro: Coroutine[Any, Any, Any]) -> None:
    """Startet ``coro`` im Loop, ohne zu warten – nur wenn der Loop schon läuft."""
    if not _running():
        coro.close()
        return
    asyncio.run_coroutine_threadsafe(coro, _loop)  # type: ignore[arg-type]


def shutdown(timeout: float = SHUTDOWN_TIMEOUT) -> None:
    """Schließt alle Sitzungen, beendet ``stdio``-Kindprozesse und stoppt den Loop.

    Mehrfacher Aufruf ist harmlos. In einem geforkten Kind ohne eigenen Loop
    passiert nichts.
    """
    global _loop, _thread, _pid
    with _start_lock:
        loop, thread = _loop, _thread
        if loop is None or thread is None or _pid != os.getpid() or not thread.is_alive():
            return
        _loop, _thread, _pid = None, None, None

    async def _cleanup() -> None:
        for hook in list(_shutdown_hooks):
            try:
                await hook()
            except Exception:
                logger.exception("Fehler beim Aufräumen der MCP-Sitzungen")

    try:
        asyncio.run_coroutine_threadsafe(_cleanup(), loop).result(timeout=timeout)
    except Exception:
        logger.warning("MCP-Aufräumen nicht vollständig abgeschlossen")
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=timeout)
    logger.info("MCP-Loop-Thread beendet")
