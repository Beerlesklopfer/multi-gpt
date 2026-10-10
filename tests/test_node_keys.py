"""API-Keys (M15): Anlegen (einmal sichtbar), Hash, Ablauf, Widerruf, Rechte der
Rolle, Seite „API-Keys“ und Admin ohne Hash."""

import datetime as dt
import hashlib

import pytest
from django.urls import reverse
from django.utils import timezone

from multigpt.accounts.models import Role
from multigpt.node import keys
from multigpt.node import scopes as S
from multigpt.node.models import ApiCall, ApiKey
from tests.node_support import make_key, make_user, rpc

pytestmark = pytest.mark.django_db


def test_key_is_stored_only_as_hash():
    user = make_user("anna")
    key, secret = make_key(user, [S.DOCS_READ])
    assert secret.startswith(f"mgpt_{key.prefix}_")
    assert len(secret) > 50
    assert key.key_hash == hashlib.sha256(secret.encode()).hexdigest()
    stored = ApiKey.objects.values_list("prefix", "key_hash", "name", "scopes").get()
    assert secret not in str(stored)
    assert keys.authenticate(secret) == key
    assert keys.authenticate(secret[:-1] + ("A" if secret[-1] != "A" else "B")) is None
    assert keys.authenticate("mgpt_xxxxxxxxxx_" + "a" * 43) is None
    assert keys.authenticate(None) is None
    assert keys.authenticate("Bearer x") is None


def test_bearer_header_parsing():
    assert keys.bearer_token("Bearer abc") == "abc"
    assert keys.bearer_token("bearer  abc ") == "abc"
    assert keys.bearer_token("Basic abc") is None
    assert keys.bearer_token("") is None


def test_expiry_and_revoke_take_effect_immediately(client):
    user = make_user("anna")
    key, secret = make_key(user, [S.USAGE_READ], expires_at=timezone.now() + dt.timedelta(hours=1))
    assert rpc(client, secret, "ping").status_code == 200
    ApiKey.objects.filter(pk=key.pk).update(expires_at=timezone.now() - dt.timedelta(seconds=1))
    assert rpc(client, secret, "ping").status_code == 401
    key2, secret2 = make_key(user, [S.USAGE_READ], name="zwei")
    assert rpc(client, secret2, "ping").status_code == 200
    assert keys.revoke(key2) is True
    assert keys.revoke(key2) is False
    assert rpc(client, secret2, "ping").status_code == 401
    # Gesperrtes Konto: Key wirkt nicht mehr.
    key3, secret3 = make_key(user, [S.USAGE_READ], name="drei")
    user.is_active = False
    user.save()
    assert rpc(client, secret3, "ping").status_code == 401


def test_create_respects_role():
    teen = make_user("tim", "teen")
    with pytest.raises(ValueError, match="keine API-Keys"):
        make_key(teen, [S.DOCS_READ])
    adult = make_user("anna")
    role = adult.role
    role.api_scopes = [S.DOCS_READ]
    role.save()
    with pytest.raises(ValueError, match="erlaubt die Rolle nicht"):
        make_key(adult, [S.DOCS_READ, S.CHAT_ASK])
    with pytest.raises(ValueError, match="mindestens ein Recht"):
        keys.create_key(adult, "x", [])
    with pytest.raises(ValueError, match="Vergangenheit"):
        make_key(adult, [S.DOCS_READ], expires_at=timezone.now() - dt.timedelta(days=1))


def test_effective_scopes_follow_role_at_every_call(client):
    user = make_user("anna")
    key, secret = make_key(user, [S.DOCS_READ, S.USAGE_READ])
    assert keys.effective_scopes(key) == {S.DOCS_READ, S.USAGE_READ}
    role = Role.objects.get(key="adult")
    role.api_scopes = [S.USAGE_READ, S.CHAT_ASK]
    role.save()
    key.owner.refresh_from_db()
    assert keys.effective_scopes(keys.authenticate(secret)) == {S.USAGE_READ}
    # Recht der Rolle weg -> keine Rechte, 403 ohne Details.
    role.can_use_api = False
    role.save()
    response = rpc(client, secret, "tools/list")
    assert response.status_code == 403
    assert response.content == b""


def test_superuser_without_role_gets_key_scopes():
    admin = make_user("root", "admin", is_superuser=True)
    admin.role = None
    admin.save()
    key, _ = make_key(admin, [S.INDEX_CONTROL])
    assert keys.effective_scopes(key) == {S.INDEX_CONTROL}


def test_scope_parsing():
    assert S.parse("all") == list(S.ALL_SCOPES)
    assert S.parse("docs.read, chat.ask") == [S.CHAT_ASK, S.DOCS_READ]
    with pytest.raises(ValueError, match="Unbekannte"):
        S.parse("admin")


# --- Seite „API-Keys“ ----------------------------------------------------------------


def test_page_shows_key_once_and_revokes(client):
    user = make_user("anna")
    client.force_login(user)
    url = reverse("api_keys")
    response = client.post(
        url, {"name": "n8n", "scopes": [S.DOCS_READ, S.USAGE_READ], "expires": "30"}
    )
    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    key = ApiKey.objects.get()
    secret = response.context["created"].secret
    assert secret.encode() in response.content
    assert key.scopes == [S.DOCS_READ, S.USAGE_READ]
    assert key.expires_at > timezone.now() + dt.timedelta(days=29)
    # Danach nie wieder sichtbar.
    again = client.get(url)
    assert secret.encode() not in again.content
    assert key.display_prefix.encode() in again.content
    # Widerrufen
    response = client.post(reverse("api_key_revoke", args=[key.pk]))
    assert response.status_code == 302
    key.refresh_from_db()
    assert not key.active and key.revoked_at is not None


def test_page_rejects_scopes_beyond_role_and_foreign_keys(client):
    user = make_user("anna")
    other = make_user("bernd")
    other_key, _ = make_key(other, [S.DOCS_READ])
    role = user.role
    role.api_scopes = [S.DOCS_READ]
    role.save()
    client.force_login(user)
    response = client.post(
        reverse("api_keys"), {"name": "x", "scopes": [S.CHAT_ASK], "expires": "30"}
    )
    assert response.status_code == 200
    assert not ApiKey.objects.filter(owner=user).exists()
    assert client.post(reverse("api_key_revoke", args=[other_key.pk])).status_code == 404
    other_key.refresh_from_db()
    assert other_key.active


def test_page_without_role_right_cannot_create(client):
    teen = make_user("tim", "teen")
    client.force_login(teen)
    response = client.get(reverse("api_keys"))
    assert b"erlaubt keine API-Keys" in response.content
    client.post(reverse("api_keys"), {"name": "x", "scopes": [S.DOCS_READ], "expires": "30"})
    assert not ApiKey.objects.exists()


def test_admin_shows_metadata_only(client):
    admin = make_user("chef", "admin")
    user = make_user("anna")
    key, secret = make_key(user, [S.DOCS_READ])
    ApiCall.objects.create(key=key, method="tools/call", tool="read_document", status="ok")
    client.force_login(admin)
    for url in (
        reverse("admin:node_apikey_changelist"),
        reverse("admin:node_apikey_change", args=[key.pk]),
        reverse("admin:node_apicall_changelist"),
        reverse("admin:node_integrationoverview_changelist"),
    ):
        response = client.get(url)
        assert response.status_code == 200, url
        assert key.key_hash.encode() not in response.content
        assert secret.encode() not in response.content
    assert (
        b"Kein n8n-MCP-Server eingerichtet"
        in client.get(reverse("admin:node_integrationoverview_changelist")).content
    )
    # Widerrufen per Aktion
    client.post(
        reverse("admin:node_apikey_changelist"),
        {"action": "revoke_keys", "_selected_action": [key.pk]},
    )
    key.refresh_from_db()
    assert not key.active
    # Nicht-Verwalter: kein Zugang
    client.force_login(user)
    assert client.get(reverse("admin:node_apikey_changelist")).status_code == 403


def test_page_offers_copyable_mcp_config(client):
    import json

    user = make_user("anna")
    client.force_login(user)
    url = reverse("api_keys")
    response = client.post(url, {"name": "claude", "scopes": [S.DOCS_READ], "expires": "30"})
    secret = response.context["created"].secret
    cfg = json.loads(response.context["connect_created"]["json"])
    server = cfg["mcpServers"]["multigpt"]
    assert server["type"] == "http"
    assert server["url"] == "http://testserver/mcp/"
    assert server["headers"]["Authorization"] == f"Bearer {secret}"
    assert (
        f'--header "Authorization: Bearer {secret}"'
        in (response.context["connect_created"]["claude_code"])
    )
    assert b'data-copy-target="mcp-json-new"' in response.content
    # Ohne frisch angelegten Key nur der Platzhalter, nie ein Key.
    again = client.get(url)
    assert b"&lt;DEIN_API_KEY&gt;" in again.content
    assert secret.encode() not in again.content
    assert again.context["connect_created"] is None


def test_quick_button_creates_key_with_role_scopes(client):
    import json

    user = make_user("anna")
    client.force_login(user)
    url = reverse("api_keys")
    assert b'name="quick"' in client.get(url).content
    response = client.post(url, {"quick": "1"})
    key = ApiKey.objects.get()
    assert key.name.startswith("MCP-Client ")
    assert set(key.scopes) == set(keys.role_scopes(user))
    assert key.expires_at > timezone.now() + dt.timedelta(days=89)
    secret = response.context["created"].secret
    cfg = json.loads(response.context["connect_created"]["json"])
    assert cfg["mcpServers"]["multigpt"]["headers"]["Authorization"] == f"Bearer {secret}"


def test_quick_button_needs_role_right(client):
    user = make_user("gast", role_key="guest")
    client.force_login(user)
    response = client.post(reverse("api_keys"), {"quick": "1"})
    assert response.status_code == 302
    assert not ApiKey.objects.exists()
