"""Volumen /data je Runner – Server-Seite (Nutzer, 2026-10-14).

Rollengrenze ``runner_data_mb``, Feld ``data_mb`` in ``session_start`` (nur an
Agents, die die Fähigkeit ``data`` im signierten Katalog melden – ältere lehnen
unbekannte Felder ab), Belegung ``data_used_mb`` im Heartbeat. Container laufen
hier nicht (``FakeLink`` wie in test_runner_tools_chat.py).
"""

# ruff: noqa: F811

import json
from pathlib import Path

import pytest

from multigpt.accounts.models import Role
from multigpt.runner import limits, protocol, store, trust
from multigpt.runner.models import Runner, RunnerSession
from tests.test_runner_tools_chat import (  # noqa: F401 (Fixtures)
    IMAGE,
    adult,
    ai_model,
    conversation,
    link,
    runner,
    scripted,
    send,
    server,
    tname,
    workspace,
)
from tests.test_tool_loop import answer, call, events_of, tool_round

pytestmark = pytest.mark.django_db

ROOT = Path(__file__).resolve().parent.parent


def start_frame(**changes) -> dict:
    frame = {
        "type": "session_start",
        "session": "s1",
        "workspace": "1",
        "image": IMAGE,
        "cpus": 1,
        "memory_mb": 1024,
        "pids": 256,
        "tmpfs_mb": 512,
        "network": "none",
        "command_seconds": 600,
        "output_bytes": 65536,
        "idle_minutes": 15,
    }
    frame.update(changes)
    return frame


def test_protocol_data_fields():
    assert protocol.validate(start_frame(data_mb=2048), protocol.SERVER)["data_mb"] == 2048
    assert protocol.validate(start_frame(), protocol.SERVER)["data_mb"] is None
    for bad in (-1, 1024 * 1024 + 1, "2048", True):
        with pytest.raises(protocol.ProtocolError):
            protocol.validate(start_frame(data_mb=bad), protocol.SERVER)
    beat = protocol.validate(
        {
            "type": "heartbeat",
            "sessions": [{"session": "s1", "state": "running", "data_used_mb": 12}],
        },
        protocol.AGENT,
    )
    assert beat["sessions"][0]["data_used_mb"] == 12


def test_catalog_features():
    catalog = (ROOT / "runner-rs" / "catalog.json").read_text(encoding="utf-8")
    assert trust.parse_features(catalog) == ["data", "logs"]
    assert trust.parse_features(json.dumps({"features": ["data", "unbekannt"]})) == ["data"]
    assert trust.parse_features(json.dumps({"features": "data"})) == []
    assert trust.parse_features("kein json") == []
    # Der Katalog bleibt für den Server gültig (zusätzlicher Schlüssel wird ignoriert).
    release, services = trust.parse_catalog(catalog)
    assert release == "dev" and any(s["name"] == "bash" for s in services)


def test_role_limit_and_session_limits(adult):
    assert set(Role.objects.values_list("runner_data_mb", flat=True)) == {2048}
    assert limits.session_limits(adult)["data_mb"] == 2048
    adult.role.runner_data_mb = 0
    adult.role.save()
    assert limits.session_limits(adult)["data_mb"] == 0


def test_heartbeat_remembers_data_usage(adult, runner, workspace, monkeypatch):
    monkeypatch.setattr(store, "fresh", lambda: None)  # Testtransaktion offen lassen
    session = RunnerSession.objects.create(
        owner=adult,
        workspace=workspace,
        runner=runner,
        status=RunnerSession.Status.RUNNING,
        limits_requested={"network": False, "data_mb": 100},
    )
    item = {"session": str(session.uuid), "state": "running", "data_used_mb": 150}
    beat = store.heartbeat(runner.pk, runner.token_id, [item])
    # Die Grenze setzt der Agent durch (Schreiben nach /data gesperrt); kein Stopp.
    assert beat.code is None and beat.frames == []
    session.refresh_from_db()
    assert session.limits_actual["data_used_mb"] == 150
    assert session.status == RunnerSession.Status.RUNNING


def _start(client, conversation, server, scripted, link):
    scripted([tool_round(call(tname(server, "bash"), {"command": "ls /data"}, "c1")), answer()])
    events_of(send(client, conversation, mcp_servers=[server.pk]))
    frame = link.started[0]
    protocol.validate(frame, protocol.SERVER)
    return frame


def test_data_mb_only_for_runners_with_the_feature(
    client, conversation, server, runner, scripted, link
):
    frame = _start(client, conversation, server, scripted, link)
    assert "data_mb" not in frame  # Runner ohne Fähigkeit (älterer Agent)
    assert RunnerSession.objects.get().limits_requested["data_mb"] == 2048


def test_data_mb_sent_to_capable_runner(client, conversation, server, runner, scripted, link):
    Runner.objects.filter(pk=runner.pk).update(features=["data"])
    frame = _start(client, conversation, server, scripted, link)
    assert frame["data_mb"] == 2048


def test_role_without_data_sends_nothing(client, conversation, server, runner, scripted, link):
    Runner.objects.filter(pk=runner.pk).update(features=["data"])
    role = conversation.user.role
    role.runner_data_mb = 0
    role.save()
    frame = _start(client, conversation, server, scripted, link)
    assert "data_mb" not in frame


def test_features_stored_from_trusted_catalog_only(runner, monkeypatch):
    monkeypatch.setattr(store, "fresh", lambda: None)
    catalog = (ROOT / "runner-rs" / "catalog.json").read_text(encoding="utf-8")
    hello = {
        "hostname": "ws",
        "engine": {"name": "podman", "version": "5", "rootless": True},
        "agent_version": "0.4.0",
        "protocol": protocol.PROTOCOL_VERSION,
        "catalog": catalog,
        "sessions": [],
    }
    trusted = trust.CatalogCheck("valid", "dev", [])
    monkeypatch.setattr(trust, "check_catalog", lambda hello: trusted)
    store.connect(runner.token_id, hello, None)
    runner.refresh_from_db()
    assert runner.features == ["data", "logs"]
    monkeypatch.setattr(trust, "check_catalog", lambda hello: trust.CatalogCheck("invalid"))
    store.connect(runner.token_id, hello, None)
    runner.refresh_from_db()
    assert runner.features == []
