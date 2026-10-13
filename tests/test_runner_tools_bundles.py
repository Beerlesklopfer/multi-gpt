"""Annahme neuer Bundles (M14, Pflicht 2): Prüfsumme, git bundle verify, atomar."""

import hashlib
import os
import subprocess

import pytest

from multigpt.accounts.models import Role, User
from multigpt.runner import bundles
from multigpt.runner.models import RunnerSession, RunnerWorkspace

pytestmark = pytest.mark.django_db

GIT_ENV = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": os.devnull,
}


def _git(repo, *args):
    env = {**os.environ, **GIT_ENV}
    return subprocess.run(["git", "-C", str(repo), *args], env=env, check=True, capture_output=True)


def make_bundle(tmp_path, files: dict[str, str], name="b") -> tuple[bytes, str]:
    repo = tmp_path / f"repo-{name}"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    for path, text in files.items():
        (repo / path).write_text(text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "Stand")
    out = tmp_path / f"{name}.bundle"
    _git(repo, "bundle", "create", "-q", str(out), "--all")
    head = _git(repo, "rev-parse", "HEAD").stdout.decode().strip()
    return out.read_bytes(), head


@pytest.fixture
def owner():
    return User.objects.create_user("anna", password="x", role=Role.objects.get(key="adult"))


@pytest.fixture
def workspace(owner):
    return RunnerWorkspace.objects.create(owner=owner, name="spiel")


@pytest.fixture
def session(owner, workspace):
    session = RunnerSession.objects.create(owner=owner, workspace=workspace)
    RunnerWorkspace.objects.filter(pk=workspace.pk).update(active_session=session)
    return session


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def test_accepts_valid_bundle(tmp_path, workspace, session):
    data, head = make_bundle(tmp_path, {"index.html": "<h1>Hallo</h1>"})
    result = bundles.accept(workspace.pk, data, _sha(data), session.pk, head="egal")
    assert result.ok, result.message
    assert result.head == head
    stored = RunnerWorkspace.objects.with_bundle().get(pk=workspace.pk)
    assert bytes(stored.bundle) == data
    assert stored.head_commit == head
    assert stored.size == len(data)
    assert stored.sha256 == _sha(data)


def test_wrong_checksum_keeps_last_good_state(tmp_path, workspace, session):
    good, head = make_bundle(tmp_path, {"a.txt": "eins"}, "a")
    assert bundles.accept(workspace.pk, good, _sha(good), session.pk).ok
    newer, _ = make_bundle(tmp_path, {"a.txt": "zwei"}, "b")
    result = bundles.accept(workspace.pk, newer, "0" * 64, session.pk)
    assert not result.ok
    assert result.message == bundles.MSG_CHECKSUM
    stored = RunnerWorkspace.objects.with_bundle().get(pk=workspace.pk)
    assert bytes(stored.bundle) == good
    assert stored.head_commit == head


def test_corrupt_bundle_is_rejected(tmp_path, workspace, session):
    good, _ = make_bundle(tmp_path, {"a.txt": "eins"})
    broken = good[:-40] + b"\x00" * 40
    result = bundles.accept(workspace.pk, broken, _sha(broken), session.pk)
    assert not result.ok
    assert RunnerWorkspace.objects.with_bundle().get(pk=workspace.pk).bundle is None


def test_not_a_bundle(workspace, session):
    data = b"#!/bin/sh\nrm -rf /\n"
    result = bundles.accept(workspace.pk, data, _sha(data), session.pk)
    assert result.message == bundles.MSG_FORMAT


def test_size_limit_from_role(tmp_path, owner, workspace, session):
    owner.role.runner_max_workspace_mb = 1
    owner.role.save()
    data = b"# v2 git bundle\n" + b"x" * (1024 * 1024)
    result = bundles.accept(workspace.pk, data, _sha(data), session.pk)
    assert not result.ok
    assert "1 MB" in result.message


def test_session_must_hold_lock(tmp_path, owner, workspace):
    data, _ = make_bundle(tmp_path, {"a.txt": "eins"})
    other = RunnerSession.objects.create(owner=owner, workspace=workspace)
    result = bundles.accept(workspace.pk, data, _sha(data), other.pk)
    assert result.message == bundles.MSG_NOT_LOCKED


def test_verify_runs_without_hooks(tmp_path, monkeypatch):
    # Eine globale Konfiguration mit Hook-Pfad darf nicht wirken.
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(tmp_path / "evil.cfg"))
    (tmp_path / "evil.cfg").write_text(f"[core]\n\thooksPath = {hooks}\n")
    data, head = make_bundle(tmp_path, {"a.txt": "eins"})
    assert bundles.verify(data) == (head, "")
    assert not marker.exists()


def test_accept_writes_audit_with_session(tmp_path, workspace, session):
    from multigpt.runner.models import RunnerAudit

    data, _ = make_bundle(tmp_path, {"a.txt": "eins"})
    assert bundles.accept(workspace.pk, data, _sha(data), session.pk).ok
    entry = RunnerAudit.objects.get(kind="workspace")
    assert entry.session_id == session.pk and "angenommen" in entry.message
