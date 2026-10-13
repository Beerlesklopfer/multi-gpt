"""Runner, Teil core (M14-01/02): Rollenrechte, Key-Recht, Tokens, mgpt-ctl, Audit,
Hosts, MCP-Server je Konto."""

import io
from datetime import timedelta

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import IntegrityError, transaction
from django.utils import timezone

from multigpt.accounts.models import Role
from multigpt.accounts.permissions import Action, can
from multigpt.chat.models import McpServer
from multigpt.node import keys
from multigpt.node import scopes as api_scopes
from multigpt.runner import audit, control, hosts, limits, tokens
from multigpt.runner.mcp_server import ensure_mcp_server
from multigpt.runner.models import Runner, RunnerAudit, RunnerHost, RunnerToken
from tests.node_support import make_user

pytestmark = pytest.mark.django_db


@pytest.fixture
def notified(monkeypatch):
    """NOTIFY auf mgpt_runner_ctl abfangen (Zustellung prüft gateway)."""
    calls = []
    monkeypatch.setattr(control, "notify_ctl", lambda kind, **data: calls.append((kind, data)))
    return calls


# --- Rollen und Rechte -------------------------------------------------------------


def test_start_roles_runner_rights():
    expected = {"admin": True, "adult": True, "teen": False, "guest": False}
    for key, allowed in expected.items():
        role = Role.objects.get(key=key)
        assert role.can_use_runner is allowed
        assert role.can_use_runner_hosts is allowed
        assert role.runner_network is False
        assert role.runner_max_workspaces == 10
        assert role.runner_max_workspace_mb == 50
    admin, adult = Role.objects.get(key="admin"), Role.objects.get(key="adult")
    assert (admin.runner_max_cpus, admin.runner_max_memory_mb) == (4, 4096)
    assert (adult.runner_max_cpus, adult.runner_max_memory_mb) == (2, 2048)
    teen = Role.objects.get(key="teen")
    assert (teen.runner_max_cpus, teen.runner_max_memory_mb) == (1, 1024)


def test_new_role_fail_closed():
    role = Role.objects.create(name="Neu", key="neu")
    assert not role.can_use_runner and not role.can_use_runner_hosts and not role.runner_network


@pytest.mark.parametrize(
    "role_key,allowed", [("admin", True), ("adult", True), ("teen", False), ("guest", False)]
)
def test_can_use_runner_per_role(role_key, allowed):
    user = make_user(f"u-{role_key}", role_key)
    assert can(user, Action.USE_RUNNER) is allowed
    assert can(user, Action.USE_RUNNER_HOSTS) is allowed


def test_inactive_account_cannot_use_runner():
    user = make_user("anna", is_active=False)
    assert not can(user, Action.USE_RUNNER)


def test_runner_use_scope_in_roles_and_keys():
    assert api_scopes.RUNNER_USE in api_scopes.ALL_SCOPES
    assert "runner.use" in Role.objects.get(key="adult").api_scopes
    anna = make_user("anna")
    assert api_scopes.RUNNER_USE in keys.role_scopes(anna)
    # Rollenrecht weg -> Key-Recht weg, auch wenn die Höchstmenge es noch enthält.
    Role.objects.filter(key="adult").update(can_use_runner=False)
    anna.refresh_from_db()
    assert api_scopes.RUNNER_USE not in keys.role_scopes(anna)


def test_session_limits_from_role_and_api_key():
    anna = make_user("anna")
    Role.objects.filter(key="adult").update(runner_network=True)
    anna.refresh_from_db()
    assert limits.session_limits(anna) == {
        "cpus": 2,
        "memory_mb": 2048,
        "pids": 256,
        "tmpfs_mb": 1024,
        "network": True,
        "packages": False,  # freies Netz geht vor
        "scratch_mb": 1024,
        "scratch_mode": "container",
    }
    assert limits.session_limits(anna, via_api_key=True)["network"] is False


def test_session_limits_scratch_from_workspace_and_role():
    from multigpt.runner import workspaces

    anna = make_user("anna")
    Role.objects.filter(key="adult").update(runner_scratch_mb=2048)
    anna.refresh_from_db()
    ws = workspaces.create_workspace(anna, "Projekt", "local")
    result = limits.session_limits(anna, workspace=ws)
    assert (result["scratch_mode"], result["scratch_mb"]) == ("local", 2048)
    assert "Scratch" not in str(result)  # kein Pfad, nur die Kennung des Arbeitsstands
    assert Role.objects.get(key="teen").runner_scratch_mb == 1024


def test_limit_mismatches():
    want = {"cpus": 2, "memory_mb": 2048, "pids": 256, "tmpfs_mb": 1024, "network": False}
    assert limits.mismatches(want, dict(want)) == []
    assert limits.mismatches(want, {**want, "cpus": 1}) == []  # strenger ist erlaubt
    assert limits.mismatches(want, {**want, "memory_mb": 4096, "network": True}) == [
        "memory_mb",
        "network",
    ]
    assert limits.mismatches(want, {"cpus": 2}) == ["memory_mb", "pids", "tmpfs_mb", "network"]


# --- Tokens --------------------------------------------------------------------------


def test_token_hash_only_and_authenticate():
    anna = make_user("anna")
    created = tokens.create_token(anna, "Laptop")
    token, secret = created.token, created.secret
    assert secret.startswith("mgrt_") and len(secret) > 50
    assert secret not in token.token_hash and token.token_hash == tokens.hash_token(secret)
    assert not RunnerToken.objects.filter(token_hash=secret).exists()
    assert tokens.authenticate(secret) == token
    assert tokens.authenticate(secret + "x") is None
    assert tokens.authenticate(secret[:-1] + ("A" if secret[-1] != "A" else "B")) is None
    assert tokens.authenticate("mgpt_" + secret[5:]) is None
    assert tokens.authenticate(None) is None


def test_token_default_expiry_90_days():
    anna = make_user("anna")
    token = tokens.create_token(anna).token
    delta = token.expires_at - timezone.now()
    assert timedelta(days=89, hours=23) < delta <= timedelta(days=90)
    assert token.name.startswith("Runner ")


def test_token_expiry_and_inactive_owner():
    anna = make_user("anna")
    created = tokens.create_token(anna, "x")
    later = timezone.now() + timedelta(days=91)
    assert tokens.authenticate(created.secret, now=later) is None
    anna.is_active = False
    anna.save()
    assert tokens.authenticate(created.secret) is None


def test_token_requires_role_right():
    teen = make_user("tim", "teen")
    with pytest.raises(ValueError, match="Rolle"):
        tokens.create_token(teen, "x")
    with pytest.raises(ValueError, match="Vergangenheit"):
        tokens.create_token(make_user("anna"), "x", expires_at=timezone.now() - timedelta(1))


def test_revoke_notifies_and_blocks(notified):
    anna = make_user("anna")
    created = tokens.create_token(anna, "x")
    Runner.objects.create(token=created.token, name="laptop")
    assert tokens.revoke(created.token) is True
    assert created.token.revoked and created.token.state_label == "widerrufen"
    assert notified == [("revoke", {"token": created.token.pk})]
    assert tokens.authenticate(created.secret) is None
    assert tokens.revoke(created.token) is False  # schon widerrufen: keine zweite Meldung
    assert len(notified) == 1


def test_revoke_sends_real_notify():
    """Ohne Attrappe: pg_notify läuft fehlerfrei in der Transaktion."""
    anna = make_user("anna")
    assert tokens.revoke(tokens.create_token(anna, "x").token)


def test_find_by_id_and_prefix():
    anna = make_user("anna")
    token = tokens.create_token(anna, "x").token
    assert tokens.find(str(token.pk)) == token
    assert tokens.find(token.display_prefix) == token
    assert tokens.find("mgrt_unbekannt") is None


def test_one_runner_per_token():
    anna = make_user("anna")
    token = tokens.create_token(anna, "x").token
    Runner.objects.create(token=token)
    with pytest.raises(IntegrityError), transaction.atomic():
        Runner.objects.create(token=token)
    assert Runner.objects.get().owner == anna


# --- mgpt-ctl runner token -------------------------------------------------------------


def run(*args):
    out, err = io.StringIO(), io.StringIO()
    call_command(*args, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


def test_cli_create_list_revoke(notified):
    make_user("anna")
    out, err = run("runner", "token", "create", "anna", "--name", "Laptop")
    secret = out.strip()
    token = RunnerToken.objects.get()
    assert tokens.authenticate(secret) == token
    assert "nur dieses eine Mal" in err and secret not in err
    assert token.expires_at is not None
    listing, _ = run("runner", "token", "list")
    assert token.display_prefix in listing and "Laptop" in listing and secret not in listing
    out, _ = run("runner", "token", "revoke", token.display_prefix)
    assert "widerrufen" in out
    assert notified and notified[0][0] == "revoke"
    out, _ = run("runner", "token", "revoke", str(token.pk))
    assert "schon widerrufen" in out
    assert RunnerAudit.objects.filter(kind=RunnerAudit.Kind.TOKEN).count() == 2


def test_cli_errors():
    make_user("tim", "teen")
    with pytest.raises(CommandError, match="nicht gefunden"):
        run("runner", "token", "create", "niemand")
    with pytest.raises(CommandError, match="Rolle"):
        run("runner", "token", "create", "tim")
    with pytest.raises(CommandError, match="nicht gefunden"):
        run("runner", "token", "revoke", "999")


def test_cli_never_expires():
    make_user("anna")
    run("runner", "token", "create", "anna", "--expires", "never")
    assert RunnerToken.objects.get().expires_at is None


# --- Audit ------------------------------------------------------------------------------


def test_audit_redacts_secrets_and_truncates():
    anna = make_user("anna")
    entry = audit.record(
        owner=anna,
        tool="bash",
        arguments={
            "command": "echo " + "x" * 2000,
            "password": "hunter2",
            "env": {"API_TOKEN": "abc"},
            "note": "Bearer abcdefghijkl und mgrt_abcdefghij_" + "A" * 43,
        },
        exit_code=0,
        duration_ms=12,
        message="mit Bearer abcdefghijklmnop",
    )
    assert "hunter2" not in entry.arguments and 'abc"' not in entry.arguments
    assert "abcdefghijkl" not in entry.arguments and "AAAA" not in entry.arguments
    assert "[entfernt]" in entry.arguments
    assert len(entry.arguments) <= audit.MAX_ARGUMENTS
    assert "abcdefghijklmnop" not in entry.message


def test_audit_private_key_removed():
    text = audit.redact(
        {"x": "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAA\n-----END OPENSSH PRIVATE KEY-----"}
    )
    assert "AAAA" not in text and "[entfernt]" in text
    assert audit.redact(None) == ""


# --- Hosts (ssh/telnet) ------------------------------------------------------------------


def test_hosts_visible_only_when_released_and_confirmed():
    anna, tim = make_user("anna"), make_user("tim", "teen")
    nas = RunnerHost.objects.create(
        name="nas",
        address="192.0.2.10",
        username="admin",
        secret="geheim",
        host_key="ssh-ed25519 AAAA",
    )
    router = RunnerHost.objects.create(
        name="router",
        address="192.0.2.1",
        port=23,
        protocol="telnet",
        auth_method="password",
        secret="pw",
    )
    nas.allowed_roles.add(anna.role, tim.role)
    router.allowed_users.add(anna)
    # SSH ohne bestätigten Host-Schlüssel: nicht nutzbar.
    assert [h.name for h in hosts.hosts_for(anna)] == ["router"]
    nas.host_key_confirmed_at = timezone.now()
    nas.save()
    assert [h.name for h in hosts.hosts_for(anna)] == ["nas", "router"]
    # Jugendliche: kein Fernzugriff, trotz Freigabe der Rolle.
    assert list(hosts.hosts_for(tim)) == []
    visible = hosts.visible_hosts(anna)
    assert visible[0] == {"name": "nas", "protocol": "ssh", "description": ""}
    assert "192.0.2" not in str(visible) and "geheim" not in str(visible)
    conn = hosts.connection_for(hosts.get_host(anna, "nas"))
    assert conn["secret"] == "geheim" and conn["address"] == "192.0.2.10"
    nas.active = False
    nas.save()
    assert hosts.get_host(anna, "nas") is None


def test_host_secret_encrypted_in_db():
    from django.db import connection

    host = RunnerHost.objects.create(name="nas", address="nas.local", secret="sehr-geheim")
    with connection.cursor() as cursor:
        cursor.execute("SELECT secret FROM runner_runnerhost WHERE id = %s", [host.pk])
        raw = cursor.fetchone()[0]
    assert "sehr-geheim" not in raw
    assert RunnerHost.objects.get().secret == "sehr-geheim"


# --- MCP-Server je Konto -------------------------------------------------------------------


def test_mcp_server_per_account_only_owner():
    anna, ben = make_user("anna"), make_user("ben", "admin")
    server = ensure_mcp_server(anna)
    assert ensure_mcp_server(anna) == server
    assert server.transport == "runner" and server.owner == anna and server.name == "Runner (anna)"
    assert can(anna, Action.USE_MCP_SERVER, server)
    assert not can(ben, Action.USE_MCP_SERVER, server)  # Verwalter: kein fremder Runner
    ben.is_superuser = True
    ben.save()
    assert not can(ben, Action.USE_MCP_SERVER, server)
    Role.objects.filter(key="adult").update(can_use_runner=False, all_mcp_servers=True)
    anna.refresh_from_db()
    assert not can(anna, Action.USE_MCP_SERVER, server)


def test_mcp_server_constraint_runner_needs_owner():
    with pytest.raises(IntegrityError), transaction.atomic():
        McpServer.objects.create(name="x", transport="runner")
    anna = make_user("anna")
    with pytest.raises(IntegrityError), transaction.atomic():
        McpServer.objects.create(name="y", transport="http", url="https://x", owner=anna)
