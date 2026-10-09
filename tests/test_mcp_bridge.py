"""Brücke sync ↔ async (M4a-01): Loop-Thread, Start-Lock, Fork, Timeout, Aufräumen."""

import asyncio
import os
import threading
import time

import pytest

from multigpt.chat.mcp import bridge
from multigpt.chat.mcp.errors import McpTimeout


@pytest.fixture(autouse=True)
def _fresh_bridge():
    bridge.shutdown()
    yield
    bridge.shutdown()


def _loop_threads():
    return [t for t in threading.enumerate() if t.name == "mcp-loop" and t.is_alive()]


def test_loop_starts_once_for_parallel_threads():
    barrier = threading.Barrier(8)
    loops = []

    def worker():
        barrier.wait()
        loops.append(bridge.get_loop())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(loops) == 8
    assert len({id(loop) for loop in loops}) == 1
    assert len(_loop_threads()) == 1


def test_loop_is_not_started_before_first_use():
    assert not bridge.is_running()
    assert _loop_threads() == []


def test_new_loop_after_fork(monkeypatch):
    first = bridge.get_loop()
    old_thread = bridge._thread
    # Fork simulieren: Der gespeicherte Loop gehört zu einer anderen PID.
    monkeypatch.setattr(bridge, "_pid", os.getpid() + 100_000)
    try:
        second = bridge.get_loop()
        assert second is not first
        assert bridge._pid == os.getpid()
        assert bridge.run(_answer(), timeout=5) == 42
    finally:
        first.call_soon_threadsafe(first.stop)
        old_thread.join(timeout=5)
    assert not old_thread.is_alive()


async def _answer():
    return 42


def test_run_returns_result_from_loop_thread():
    async def where():
        return threading.current_thread().name

    assert bridge.run(where(), timeout=5) == "mcp-loop"


def test_timeout_cancels_and_does_not_block():
    cancelled = threading.Event()

    async def slow():
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    start = time.monotonic()
    with pytest.raises(McpTimeout):
        bridge.run(slow(), timeout=0.2)
    assert time.monotonic() - start < 2
    assert cancelled.wait(2)
    # Der Loop arbeitet danach normal weiter.
    assert bridge.run(_answer(), timeout=5) == 42


def test_run_inside_loop_thread_is_refused():
    async def nested():
        coro = _answer()
        try:
            bridge.run(coro, timeout=1)
        except RuntimeError as exc:
            return str(exc)
        return "kein Fehler"

    assert "Loop-Thread" in bridge.run(nested(), timeout=5)


def test_shutdown_runs_hooks_and_stops_thread(monkeypatch):
    called = threading.Event()

    async def hook():
        called.set()

    monkeypatch.setattr(bridge, "_shutdown_hooks", [hook])
    bridge.get_loop()
    thread = bridge._thread
    bridge.shutdown()
    assert called.is_set()
    assert not thread.is_alive()
    assert not bridge.is_running()
    # Danach startet der nächste Aufruf einen frischen Loop.
    assert bridge.run(_answer(), timeout=5) == 42


def test_submit_without_running_loop_does_nothing():
    ran = []

    async def mark():
        ran.append(1)

    bridge.submit(mark())
    assert not bridge.is_running()
    assert ran == []


def test_gunicorn_worker_exit_hook_shuts_down():
    import runpy
    from pathlib import Path

    conf = runpy.run_path(str(Path(__file__).parent.parent / "deploy" / "gunicorn.conf.py"))
    bridge.get_loop()
    conf["worker_exit"](None, None)
    assert not bridge.is_running()
