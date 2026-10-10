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
        help_text="Wofür der Key ist, z. B. „n8n NAS-Ordner“.",
    )
    scopes = forms.MultipleChoiceField(
        label="Rechte", widget=forms.CheckboxSelectMultiple, choices=()
    )
    expires = forms.ChoiceField(label="Gültig", choices=EXPIRY_CHOICES, initial="90")
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

    def expires_at(self):
        value = self.cleaned_data["expires"]
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
        "connect_created": connect_configs(mcp_url, created.secret) if created else None,
        "can_create": bool(keys.role_scopes(user)),
        "scope_labels": {key: label for key, (label, _) in api_scopes.SCOPES.items()},
    }


@require_http_methods(["GET", "POST"])
@login_required
def api_keys_page(request):
    user = request.user
    form = ApiKeyForm(user, request.POST or None)
    created = None
    if request.method == "POST":
        if not keys.role_scopes(user):
            messages.error(request, "Dieses Konto darf keine API-Keys anlegen.")
            return redirect("api_keys")
        if request.POST.get("quick"):
            # Knopf „Key erzeugen“ in der Verbindungsvorlage: alle Rechte der Rolle,
            # 90 Tage gültig, Name mit Datum – danach ohne Formular einsatzbereit.
            form = ApiKeyForm(user)
            created = keys.create_key(
                user,
                f"MCP-Client {timezone.localtime():%d.%m.%Y %H:%M}",
                sorted(keys.role_scopes(user)),
                expires_at=timezone.now() + timedelta(days=QUICK_DAYS),
            )
        elif form.is_valid():
            try:
                created = keys.create_key(
                    user,
                    form.cleaned_data["name"],
                    form.cleaned_data["scopes"],
                    expires_at=form.expires_at(),
                    collections=form.cleaned_data["collections"],
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                form = ApiKeyForm(user)
    response = render(request, "node/api_keys.html", _context(request, form, created))
    response["Cache-Control"] = "no-store"
    return response


@require_POST
@login_required
def api_key_revoke(request, pk: int):
    key = get_object_or_404(ApiKey, pk=pk, owner=request.user)
    if keys.revoke(key):
        messages.success(request, f"Key „{key.name}“ widerrufen. Er gilt ab sofort nicht mehr.")
    return redirect("api_keys")
