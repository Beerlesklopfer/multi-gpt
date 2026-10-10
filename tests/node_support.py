"""Gemeinsame Hilfen für die Tests des Knotens (M15): Konten, Keys, JSON-RPC."""

import json

from multigpt.accounts.models import Role, User
from multigpt.node import keys
from multigpt.node import scopes as api_scopes

PASSWORD = "Geheim-Test-1234"
ACCEPT = "application/json, text/event-stream"


def make_user(username, role_key="adult", **extra):
    return User.objects.create_user(
        username, password=PASSWORD, role=Role.objects.get(key=role_key), **extra
    )


def make_key(user, scopes=None, **extra):
    """Key mit allen (bzw. den angegebenen) Rechten; Rückgabe ``(ApiKey, Klartext)``."""
    created = keys.create_key(
        user, extra.pop("name", "Test"), scopes or list(api_scopes.ALL_SCOPES), **extra
    )
    return created.key, created.secret


def post(client, secret, body, **headers):
    data = body if isinstance(body, str | bytes) else json.dumps(body)
    extra = {"HTTP_ACCEPT": ACCEPT}
    if secret is not None:
        extra["HTTP_AUTHORIZATION"] = f"Bearer {secret}"
    extra.update(headers)
    return client.post("/mcp/", data=data, content_type="application/json", **extra)


def rpc(client, secret, method, params=None, request_id=1, **headers):
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return post(client, secret, body, **headers)


def call(client, secret, name, arguments=None, **headers):
    """``tools/call`` -> (HTTP-Antwort, result-dict bzw. None)."""
    response = rpc(
        client, secret, "tools/call", {"name": name, "arguments": arguments or {}}, **headers
    )
    payload = (
        response.json() if response.get("Content-Type", "").startswith("application/json") else {}
    )
    return response, payload.get("result")


def tool_names(client, secret) -> set[str]:
    response = rpc(client, secret, "tools/list")
    assert response.status_code == 200, response.content
    return {t["name"] for t in response.json()["result"]["tools"]}


def structured(result) -> dict:
    return result.get("structuredContent") or {}


def text(result) -> str:
    return result["content"][0]["text"]
