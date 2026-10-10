"""Seite „Einstellungen → API-Keys“ (M15): eigene Keys anlegen, widerrufen,
zuletzt benutzt und Audit-Log der eigenen Aufrufe.

Nur das angemeldete Konto, ohne Parameter für ein anderes. Der Key erscheint
genau einmal in der Antwort auf das Anlegen (``Cache-Control: no-store``) und
wird nirgends gespeichert. Anlegen nur mit Rollenrecht „API-Keys/MCP-Zugang“
und nur mit Rechten, die die Rolle erlaubt; vorhandene Keys lassen sich immer
widerrufen.
"""

from __future__ import annotations

import json
from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_http_methods, require_POST

from multigpt.chat.api_collections import readable_collections

from . import keys
from . import scopes as api_scopes
from .models import ApiCall, ApiKey

EXPIRY_CHOICES = [
    ("30", "30 Tage"),
    ("90", "90 Tage"),
    ("365", "1 Jahr"),
    ("never", "kein Ablauf"),
]
CALLS_SHOWN = 50


class ApiKeyForm(forms.Form):
    name = forms.CharField(
        label="Name",
        max_length=keys.MAX_NAME,
        required=False,
        help_text="Wofür der Key ist, z. B. „n8n NAS-Ordner“. Leer: „MCP-Client <Datum>“.",
    )
    scopes = forms.MultipleChoiceField(
        label="Rechte",
        widget=forms.CheckboxSelectMultiple,
        choices=(),
        required=False,
        help_text="Leer: alle Rechte deiner Rolle.",
    )
    expires = forms.ChoiceField(
        label="Gültig", choices=EXPIRY_CHOICES, initial="90", required=False
    )
    collections = forms.ModelMultipleChoiceField(
        label="Nur diese Sammlungen",
        queryset=None,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        help_text="Leer = alle Sammlungen, die das Konto lesen bzw. schreiben darf.",
    )

    def __init__(self, user, *args, **kwargs):
        super().__init__(*args, **kwargs)
        allowed = keys.role_scopes(user)
        self.fields["scopes"].choices = [
            (key, f"{label} – {text}")
            for key, (label, text) in api_scopes.SCOPES.items()
            if key in allowed
        ]
        self.fields["collections"].queryset = readable_collections(user).order_by("name", "pk")
        self.fields["scopes"].initial = [key for key, _ in self.fields["scopes"].choices]

    def create(self, user):
        """Key nach den Formularangaben, mit Standards für leere Felder."""
        data = self.cleaned_data
        return keys.create_key(
            user,
            data["name"] or f"MCP-Client {timezone.localtime():%d.%m.%Y %H:%M}",
            data["scopes"] or sorted(keys.role_scopes(user)),
            expires_at=self.expires_at(),
            collections=data["collections"],
        )

    def expires_at(self):
        value = self.cleaned_data["expires"] or str(QUICK_DAYS)
        if value == "never":
            return None
        return timezone.now() + timedelta(days=int(value))


PLACEHOLDER = "<DEIN_API_KEY>"
QUICK_DAYS = 90
SERVER_NAME = "multigpt"


def connect_configs(mcp_url: str, secret: str = PLACEHOLDER) -> dict[str, str]:
    """Kopierfertige Verbindungsdaten für MCP-Clients.

    ``json``: „mcpServers“-Format (Claude Desktop, Claude Code ``.mcp.json``,
    Cursor, n8n-Import); ``claude_code``: Befehl für ``claude mcp add``.
    """
    config = {
        "mcpServers": {
            SERVER_NAME: {
                "type": "http",
                "url": mcp_url,
                "headers": {"Authorization": f"Bearer {secret}"},
            }
        }
    }
    command = (
        f"claude mcp add --transport http {SERVER_NAME} {mcp_url} "
        f'--header "Authorization: Bearer {secret}"'
    )
    return {"json": json.dumps(config, indent=2, ensure_ascii=False), "claude_code": command}


def _context(request, form, created=None):
    user = request.user
    mcp_url = request.build_absolute_uri(reverse("mcp"))
    own = ApiKey.objects.filter(owner=user).prefetch_related("collections")
    calls = (
        ApiCall.objects.filter(key__owner=user)
        .select_related("key")
        .order_by("-created", "-pk")[:CALLS_SHOWN]
    )
    return {
        "form": form,
        "keys": own,
        "calls": calls,
        "created": created,
        "connect": connect_configs(mcp_url),
        "placeholder": PLACEHOLDER,
        "connect_created": connect_configs(mcp_url, created.secret) if created else None,
        "connect_shown": connect_configs(mcp_url, created.secret if created else PLACEHOLDER),
        "can_create": bool(keys.role_scopes(user)),
        "scope_labels": {key: label for key, (label, _) in api_scopes.SCOPES.items()},
    }


@require_http_methods(["GET", "POST"])
@login_required
def api_keys_page(request):
    user = request.user
    form = ApiKeyForm(user, request.POST if request.method == "POST" else None)
    created = None
    if request.method == "POST":
        if not keys.role_scopes(user):
            messages.error(request, "Dieses Konto darf keine API-Keys anlegen.")
            return redirect("api_keys")
        if form.is_valid():
            try:
                created = form.create(user)
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                form = ApiKeyForm(user)
    response = render(request, "node/api_keys.html", _context(request, form, created))
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@login_required
def api_key_quick(request):
    """Knopf „Erzeugen“ im Key-Eingabefeld (node_connect.js): Key als JSON, einmalig."""
    if not keys.role_scopes(request.user):
        return JsonResponse({"error": "Dieses Konto darf keine API-Keys anlegen."}, status=403)
    form = ApiKeyForm(request.user, request.POST)
    if not form.is_valid():
        return JsonResponse(
            {"error": "Bitte die Angaben prüfen.", "fields": form.errors}, status=400
        )
    try:
        created = form.create(request.user)
    except ValueError as exc:
        return JsonResponse({"error": str(exc)}, status=400)
    response = JsonResponse({"secret": created.secret, "name": created.key.name}, status=201)
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@login_required
def api_key_delete(request, pk: int):
    """Widerrufenen bzw. abgelaufenen Key entfernen; Aufrufe bleiben im Audit-Log."""
    key = get_object_or_404(ApiKey, pk=pk, owner=request.user)
    if key.active and not key.is_expired():
        messages.error(request, "Bitte den Key zuerst widerrufen.")
    else:
        name = key.name
        key.delete()
        messages.success(request, f"Key „{name}“ gelöscht.")
    return redirect("api_keys")


@require_POST
@login_required
def api_key_revoke(request, pk: int):
    key = get_object_or_404(ApiKey, pk=pk, owner=request.user)
    if keys.revoke(key):
        messages.success(request, f"Key „{key.name}“ widerrufen. Er gilt ab sofort nicht mehr.")
    return redirect("api_keys")
