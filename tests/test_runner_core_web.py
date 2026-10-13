"""Runner, Teil core: Seiten „Runner“ (Kopplung) und „Arbeitsstände“, git-Bundle,
Sperre, Admin nur mit Metadaten."""

import subprocess

import pytest
from django.urls import reverse

from multigpt.accounts.models import Role
from multigpt.runner import control, tokens, workspaces
from multigpt.runner.models import RunnerSession, RunnerToken, RunnerWorkspace
from tests.node_support import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _no_notify(monkeypatch):
    monkeypatch.setattr(control, "notify_ctl", lambda kind, **data: None)


def login(client, user):
    client.force_login(user)
    return client


# --- Kopplungsseite ----------------------------------------------------------------------


def test_runner_page_requires_login(client):
    response = client.get(reverse("runner:index"))
    assert response.status_code == 302


def test_create_token_shown_once_with_quadlet(client, settings):
    settings.RUNNER_AGENT_IMAGE = "ghcr.io/test/agent@sha256:" + "a" * 64
    settings.RUNNER_CA_SHA256 = "AB:" * 31 + "AB"
    anna = make_user("anna")
    login(client, anna)
    response = client.post(reverse("runner:index"), {"name": "Laptop", "expires": "90"})
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.content.decode()
    token = RunnerToken.objects.get(owner=anna)
    secret = response.context["created"].secret
    assert tokens.authenticate(secret) == token
    assert secret in body
    quadlet = response.context["quadlet"]
    assert "ghcr.io/test/agent@sha256:" in quadlet
    assert "MGPT_URL=http://testserver" in quadlet
    assert "MGPT_CA_SHA256=" + "ab" * 32 in quadlet
    assert secret not in quadlet  # Token nur als Podman-Secret
    assert "podman secret create mgpt_runner_token -" in body
    # Danach nie wieder sichtbar.
    again = client.get(reverse("runner:index"))
    assert again["Cache-Control"] == "no-store"
    assert secret not in again.content.decode()
    assert token.display_prefix in again.content.decode()
    # MCP-Server „Runner“ für das Konto angelegt.
    assert anna.runner_mcp_servers.count() == 1


def test_quadlet_without_pin(client, settings):
    settings.RUNNER_CA_SHA256 = ""
    settings.RUNNER_TLS_CERT = ""
    login(client, make_user("anna"))
    response = client.post(reverse("runner:index"), {"name": "x", "expires": "never"})
    assert "MGPT_CA_SHA256" not in response.context["quadlet"]
    assert RunnerToken.objects.get().expires_at is None


def test_teen_cannot_create_token(client):
    tim = make_user("tim", "teen")
    login(client, tim)
    page = client.get(reverse("runner:index"))
    assert "erlaubt keinen Runner" in page.content.decode()
    client.post(reverse("runner:index"), {"name": "x"})
    assert not RunnerToken.objects.exists()


def test_revoke_and_delete_only_own(client):
    anna, ben = make_user("anna"), make_user("ben", "admin")
    token = tokens.create_token(anna, "x").token
    login(client, ben)
    assert client.post(reverse("runner:token_revoke", args=[token.pk])).status_code == 404
    login(client, anna)
    # Aktiver Token: Löschen erst nach Widerruf.
    client.post(reverse("runner:token_delete", args=[token.pk]))
    assert RunnerToken.objects.filter(pk=token.pk).exists()
    client.post(reverse("runner:token_revoke", args=[token.pk]))
    token.refresh_from_db()
    assert token.revoked
    client.post(reverse("runner:token_delete", args=[token.pk]))
    assert not RunnerToken.objects.filter(pk=token.pk).exists()


# --- Arbeitsstände -------------------------------------------------------------------------


def test_create_workspace_with_empty_repo(client):
    anna = make_user("anna")
    login(client, anna)
    response = client.post(reverse("runner:workspaces"), {"name": "Bildschirmschoner"})
    assert response.status_code == 302
    ws = RunnerWorkspace.objects.with_bundle().get(owner=anna)
    assert ws.bundle and ws.size == len(ws.bundle) and len(ws.sha256) == 64
    assert len(ws.head_commit) == 40
    commits = workspaces.history(ws)
    assert [c.subject for c in commits] == ["Arbeitsstand angelegt"]
    assert commits[0].sha == ws.head_commit
    # Listen laden das Bundle nicht.
    listed = RunnerWorkspace.objects.get(pk=ws.pk)
    assert "bundle" in listed.get_deferred_fields()


def test_workspace_limits_per_role(client):
    anna = make_user("anna")
    Role.objects.filter(key="adult").update(runner_max_workspaces=1)
    anna.refresh_from_db()
    workspaces.create_workspace(anna, "eins")
    with pytest.raises(workspaces.WorkspaceError, match="Höchstens 1"):
        workspaces.create_workspace(anna, "zwei")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.create_workspace(make_user("tim", "teen"), "x")
    assert workspaces.limits_for(anna).max_bytes == 50 * 1024 * 1024


def test_workspace_duplicate_and_bad_names():
    anna = make_user("anna")
    workspaces.create_workspace(anna, "Projekt")
    with pytest.raises(workspaces.WorkspaceError, match="schon"):
        workspaces.create_workspace(anna, "Projekt")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.create_workspace(anna, "../etc;rm")
    with pytest.raises(workspaces.WorkspaceError):
        workspaces.create_workspace(anna, "   ")


def test_history_page_download_and_delete(client, tmp_path):
    anna = make_user("anna")
    ws = workspaces.create_workspace(anna, "Projekt")
    login(client, anna)
    page = client.get(reverse("runner:workspace", args=[ws.pk]))
    assert "Arbeitsstand angelegt" in page.content.decode()
    download = client.get(reverse("runner:workspace_download", args=[ws.pk]))
    assert download["Content-Disposition"].startswith("attachment")
    assert download["Cache-Control"] == "no-store"
    bundle = tmp_path / "ws.bundle"
    bundle.write_bytes(download.content)
    # Das Bundle lässt sich mit git klonen (Wiederherstellung beim Mitglied).
    subprocess.run(
        ["git", "clone", "-q", str(bundle), str(tmp_path / "clone")],
        check=True,
        capture_output=True,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1"},
    )
    assert (tmp_path / "clone" / ".git").is_dir()
    confirm = client.get(reverse("runner:workspace_delete", args=[ws.pk]))
    assert "endgültig" in confirm.content.decode().lower()
    assert RunnerWorkspace.objects.filter(pk=ws.pk).exists()  # GET löscht nicht
    client.post(reverse("runner:workspace_delete", args=[ws.pk]))
    assert not RunnerWorkspace.objects.filter(pk=ws.pk).exists()


def test_foreign_workspace_invisible_even_for_admin(client):
    anna, ben = make_user("anna"), make_user("ben", "admin")
    ws = workspaces.create_workspace(anna, "Geheim")
    login(client, ben)
    for name in ("runner:workspace", "runner:workspace_download", "runner:workspace_delete"):
        assert client.get(reverse(name, args=[ws.pk])).status_code == 404
    assert client.post(reverse("runner:workspace_delete", args=[ws.pk])).status_code == 404
    assert "Geheim" not in client.get(reverse("runner:workspaces")).content.decode()
    assert RunnerWorkspace.objects.filter(pk=ws.pk).exists()


def test_lock_one_session_and_delete_blocked():
    anna = make_user("anna")
    ws = workspaces.create_workspace(anna, "Projekt")
    first = RunnerSession.objects.create(owner=anna, workspace=ws)
    second = RunnerSession.objects.create(owner=anna, workspace=ws)
    assert workspaces.acquire(ws, first)
    assert workspaces.acquire(ws, first)  # dieselbe Sitzung: bleibt gesperrt
    assert not workspaces.acquire(ws, second)
    with pytest.raises(workspaces.WorkspaceError, match="Benutzung"):
        workspaces.delete_workspace(ws)
    assert not workspaces.release(ws, second)  # fremde Sitzung löst nicht
    # Beendete Sitzung gilt als frei.
    RunnerSession.objects.filter(pk=first.pk).update(status=RunnerSession.Status.STOPPED)
    assert workspaces.acquire(ws, second)
    assert workspaces.release(ws, second)
    workspaces.delete_workspace(ws)
    assert not RunnerWorkspace.objects.filter(pk=ws.pk).exists()
    first.refresh_from_db()
    assert first.workspace is None  # Sitzung (Audit, Laufzeit) bleibt


def test_broken_bundle_reported_not_executed():
    anna = make_user("anna")
    ws = workspaces.create_workspace(anna, "kaputt")
    RunnerWorkspace.objects.filter(pk=ws.pk).update(bundle=b"# v2 git bundle\nkaputt\n")
    ws = RunnerWorkspace.objects.with_bundle().get(pk=ws.pk)
    with pytest.raises(workspaces.WorkspaceError, match="nicht lesbar"):
        workspaces.history(ws)


# --- Admin ------------------------------------------------------------------------------


def test_admin_shows_only_metadata(client):
    anna, ben = make_user("anna"), make_user("ben", "admin", is_staff=True)
    ws = workspaces.create_workspace(anna, "Projekt")
    created = tokens.create_token(anna, "Laptop")
    login(client, ben)
    page = client.get(reverse("admin:runner_runnerworkspace_change", args=[ws.pk]))
    assert page.status_code == 200
    body = page.content.decode()
    assert "Projekt" in body and "bundle" not in body.lower().replace("git-bundle", "")
    page = client.get(reverse("admin:runner_runnertoken_changelist"))
    assert created.token.display_prefix in page.content.decode()
    assert created.token.token_hash not in page.content.decode()
    assert client.get(reverse("admin:runner_runnerworkspace_add")).status_code == 403


def test_user_menu_link(client):
    login(client, make_user("anna"))
    assert reverse("runner:index") in client.get(reverse("settings")).content.decode()
    client.logout()
    login(client, make_user("tim", "teen"))
    assert reverse("runner:index") not in client.get(reverse("settings")).content.decode()


# --- Scratchpad ----------------------------------------------------------------------------


def test_scratch_mode_on_create_and_edit(client):
    anna = make_user("anna")
    login(client, anna)
    client.post(reverse("runner:workspaces"), {"name": "Lokal", "scratch_mode": "local"})
    ws = RunnerWorkspace.objects.get(owner=anna)
    assert ws.scratch_mode == "local" and ws.uuid
    page = client.get(reverse("runner:workspaces")).content.decode()
    assert f"~/MultiGPT/Scratch/{ws.uuid}/" in page
    client.post(
        reverse("runner:workspace", args=[ws.pk]), {"name": "Neu", "scratch_mode": "container"}
    )
    ws.refresh_from_db()
    assert (ws.name, ws.scratch_mode) == ("Neu", "container")
    # Standard ohne Angabe: im Container; unbekannter Wert wird abgewiesen.
    client.post(reverse("runner:workspaces"), {"name": "Zwei"})
    assert RunnerWorkspace.objects.get(name="Zwei").scratch_mode == "container"
    client.post(reverse("runner:workspaces"), {"name": "Drei", "scratch_mode": "/etc"})
    assert not RunnerWorkspace.objects.filter(name="Drei").exists()


def test_scratch_mode_locked_during_session(client):
    anna, ben = make_user("anna"), make_user("ben", "admin")
    ws = workspaces.create_workspace(anna, "Projekt")
    session = RunnerSession.objects.create(owner=anna, workspace=ws)
    assert workspaces.acquire(ws, session)
    with pytest.raises(workspaces.WorkspaceError, match="Sitzung"):
        workspaces.update_workspace(ws, "Projekt", "local")
    workspaces.update_workspace(ws, "Umbenannt", "container")  # Name geht weiter
    login(client, ben)
    response = client.post(reverse("runner:workspace", args=[ws.pk]), {"name": "x"})
    assert response.status_code == 404  # fremder Arbeitsstand, auch für Verwalter
