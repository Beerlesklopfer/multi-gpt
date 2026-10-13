"""Seiten „Runner“ (Kopplung, M14-02) und „Arbeitsstände“ (M14-10/12, Teil core).

Nur das angemeldete Konto, ohne Parameter für ein anderes: Auch Verwalter
sehen hier keine fremden Tokens und keine fremden Arbeitsstände. Ein neuer
Token erscheint genau einmal in der Antwort auf das Anlegen
(``Cache-Control: no-store``), zusammen mit der fertigen Quadlet-Datei.
Anlegen nur mit Rollenrecht „Runner nutzen“; vorhandene Tokens lassen sich
immer widerrufen, eigene Arbeitsstände immer ansehen, herunterladen und löschen.
"""

from __future__ import annotations

from datetime import timedelta

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import FileResponse, Http404, HttpResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from multigpt.accounts.permissions import Action, can

from . import audit, control, quadlet, tokens, trust, workspaces
from .mcp_server import ensure_mcp_server
from .models import Runner, RunnerAudit, RunnerToken, RunnerWorkspace

EXPIRY_CHOICES = [
    ("30", "30 Tage"),
    ("90", "90 Tage"),
    ("365", "1 Jahr"),
    ("never", "kein Ablauf"),
]


def _no_store(response):
    response["Cache-Control"] = "no-store"
    return response


class TokenForm(forms.Form):
    name = forms.CharField(
        label="Name",
        max_length=tokens.MAX_NAME,
        required=False,
        help_text="Wofür der Token ist, z. B. „Laptop“. Leer: „Runner <Datum>“.",
    )
    expires = forms.ChoiceField(
        label="Gültig", choices=EXPIRY_CHOICES, initial="90", required=False
    )

    def expires_at(self):
        value = self.cleaned_data.get("expires") or "90"
        if value == "never":
            return None
        return timezone.now() + timedelta(days=int(value))


class WorkspaceForm(forms.Form):
    name = forms.CharField(
        label="Name",
        max_length=workspaces.MAX_NAME,
        help_text="z. B. „Bildschirmschoner“ oder „Hausaufgaben Informatik“.",
    )
    scratch_mode = forms.ChoiceField(
        label="Scratchpad",
        choices=RunnerWorkspace.ScratchMode.choices,
        initial=RunnerWorkspace.ScratchMode.CONTAINER,
        required=False,
        widget=forms.RadioSelect,
        help_text="Ablage /scratch für Ergebnisse und Zwischenstände, die nicht ins "
        "Repository gehören. „Lokal“ ist der Ordner ~/MultiGPT/Scratch/<Kennung>/ auf dem "
        "Rechner, auf dem der Runner läuft – er bleibt nach der Sitzung erhalten. Ins Chat "
        "kommen Dateien daraus nur auf Anfrage.",
    )


# --- Runner (Kopplung) ------------------------------------------------------------


def _runner_context(request, form, created=None):
    user = request.user
    own = RunnerToken.objects.filter(owner=user).select_related("runner")
    context = {
        "form": form,
        "tokens": own,
        "created": created,
        "can_pair": can(user, Action.USE_RUNNER),
        "commands": quadlet.setup_commands(),
        "quadlet_name": quadlet.FILE_NAME,
        "secret_name": quadlet.SECRET_NAME,
        "unit_name": quadlet.UNIT_NAME,
        "server_key": trust.public_key_text(),
        "outdated": Runner.objects.filter(token__owner=user, token__revoked_at__isnull=True)
        .exclude(catalog_state__in=("valid", "dev"))
        .exists(),
    }
    if created is not None:
        url = quadlet.server_url(request)
        info = trust.dist_info()
        context.update(
            {
                "quadlet": quadlet.render(url),
                "unit": quadlet.render_unit(url),
                "dist": info,
                "downloads": [
                    {"arch": arch, **item} for arch, item in sorted(info.binaries.items())
                ],
                "native_commands": quadlet.native_commands(url, info.binaries),
                # Eingabe für "multigpt-runner pair": Token und Schlüssel des Servers
                "pair_input": f"{created.secret}\n{trust.public_key_text()}",
            }
        )
    return context


@require_http_methods(["GET", "POST"])
@login_required
def runner_page(request):
    user = request.user
    form = TokenForm(request.POST if request.method == "POST" else None)
    created = None
    if request.method == "POST":
        if not can(user, Action.USE_RUNNER):
            messages.error(request, "Deine Rolle erlaubt keinen Runner.")
            return redirect("runner:index")
        if form.is_valid():
            try:
                created = tokens.create_token(
                    user, form.cleaned_data["name"], expires_at=form.expires_at()
                )
            except ValueError as exc:
                form.add_error(None, str(exc))
            else:
                ensure_mcp_server(user)
                audit.record(
                    RunnerAudit.Kind.TOKEN,
                    owner=user,
                    message=f"Token {created.token.display_prefix} angelegt",
                )
                form = TokenForm()
    return _no_store(render(request, "runner/runner.html", _runner_context(request, form, created)))


@require_POST
@login_required
def token_revoke(request, pk: int):
    token = get_object_or_404(RunnerToken, pk=pk, owner=request.user)
    if tokens.revoke(token):
        audit.record(
            RunnerAudit.Kind.TOKEN,
            owner=request.user,
            message=f"Token {token.display_prefix} widerrufen",
        )
        messages.success(
            request, f"Token „{token.name}“ widerrufen. Ein verbundener Runner wird getrennt."
        )
    return redirect("runner:index")


@require_POST
@login_required
def token_delete(request, pk: int):
    """Widerrufenen bzw. abgelaufenen Token entfernen (samt Runner-Eintrag)."""
    token = get_object_or_404(RunnerToken, pk=pk, owner=request.user)
    if token.is_usable():
        messages.error(request, "Bitte den Token zuerst widerrufen.")
    else:
        name, token_id = token.name, token.pk
        token.delete()
        control.revoke_token(token_id)
        messages.success(request, f"Token „{name}“ gelöscht.")
    return redirect("runner:index")


# --- Arbeitsstände ------------------------------------------------------------------


@require_http_methods(["GET", "POST"])
@login_required
def workspaces_page(request):
    user = request.user
    form = WorkspaceForm(request.POST if request.method == "POST" else None)
    if request.method == "POST":
        if form.is_valid():
            try:
                workspace = workspaces.create_workspace(
                    user, form.cleaned_data["name"], form.cleaned_data["scratch_mode"]
                )
            except workspaces.WorkspaceError as exc:
                form.add_error(None, str(exc))
            else:
                audit.record(
                    RunnerAudit.Kind.WORKSPACE,
                    owner=user,
                    message=f"Arbeitsstand {workspace.pk} angelegt",
                )
                messages.success(request, f"Arbeitsstand „{workspace.name}“ angelegt.")
                return redirect("runner:workspaces")
    own = list(workspaces.own_workspaces(user))
    limits = workspaces.limits_for(user)
    return render(
        request,
        "runner/workspaces.html",
        {
            "form": form,
            "workspaces": own,
            "limits": limits,
            "can_create": can(user, Action.USE_RUNNER) and len(own) < limits.max_count,
            "can_runner": can(user, Action.USE_RUNNER),
        },
    )


def _own_workspace(request, pk: int, *, with_bundle: bool = False) -> RunnerWorkspace:
    workspace = workspaces.get_own(request.user, pk, with_bundle=with_bundle)
    if workspace is None:
        raise Http404("Arbeitsstand nicht gefunden.")
    return workspace


@require_http_methods(["GET", "POST"])
@login_required
def workspace_detail(request, pk: int):
    """Verlauf (git log aus dem Bundle, nur lesend); POST ändert Name und Scratchpad."""
    workspace = _own_workspace(request, pk)
    if request.method == "POST":
        form = WorkspaceForm(request.POST)
        if form.is_valid():
            try:
                workspaces.update_workspace(
                    workspace, form.cleaned_data["name"], form.cleaned_data["scratch_mode"]
                )
            except workspaces.WorkspaceError as exc:
                form.add_error(None, str(exc))
            else:
                messages.success(request, f"Arbeitsstand „{workspace.name}“ gespeichert.")
                return redirect("runner:workspace", pk=workspace.pk)
    else:
        form = WorkspaceForm(
            initial={"name": workspace.name, "scratch_mode": workspace.scratch_mode}
        )
    error = ""
    try:
        commits = workspaces.history(workspace)
    except workspaces.WorkspaceError as exc:
        commits, error = [], str(exc)
    return render(
        request,
        "runner/workspace_detail.html",
        {"workspace": workspace, "commits": commits, "error": error, "form": form},
    )


@login_required
def workspace_download(request, pk: int):
    """Bundle herunterladen (``git clone arbeitsstand.bundle`` stellt alles wieder her)."""
    workspace = _own_workspace(request, pk, with_bundle=True)
    if not workspace.bundle:
        raise Http404("Der Arbeitsstand hat noch kein Bundle.")
    filename = f"arbeitsstand-{workspace.pk}.bundle"
    response = HttpResponse(bytes(workspace.bundle), content_type="application/octet-stream")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return _no_store(response)


@require_http_methods(["GET", "POST"])
@login_required
def workspace_delete(request, pk: int):
    workspace = _own_workspace(request, pk)
    if request.method == "POST":
        try:
            workspaces.delete_workspace(workspace)
        except workspaces.WorkspaceError as exc:
            messages.error(request, str(exc))
            return redirect("runner:workspace", pk=workspace.pk)
        audit.record(
            RunnerAudit.Kind.WORKSPACE,
            owner=request.user,
            message=f"Arbeitsstand {pk} gelöscht",
        )
        messages.success(request, f"Arbeitsstand „{workspace.name}“ gelöscht.")
        return redirect("runner:workspaces")
    return render(request, "runner/workspace_delete.html", {"workspace": workspace})


# --- Runner-Programme (natives Programm, Rust-Agent) --------------------------------------


def _download_allowed(request) -> bool:
    """Angemeldet mit Recht „Runner nutzen“ oder ``Authorization: Bearer <Kopplungs-Token>``
    (Zielsysteme beim Einrichten, ``curl``)."""
    if request.user.is_authenticated:
        return can(request.user, Action.USE_RUNNER)
    header = request.headers.get("Authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return False
    token = tokens.authenticate(value.strip())
    return token is not None and can(token.owner, Action.USE_RUNNER)


@require_GET
def download(request, arch: str):
    """Runner-Programm (``amd64``/``arm64``) bzw. ``SHA256SUMS`` und ``SHA256SUMS.sig``
    aus ``RUNNER_DIST_DIR`` (vom Paket befüllt, signiert mit dem Release-Schlüssel)."""
    if not _download_allowed(request):
        if request.user.is_authenticated:
            return HttpResponse("Deine Rolle erlaubt keinen Runner.", status=403)
        response = HttpResponse("Anmeldung bzw. Kopplungs-Token nötig.", status=401)
        response["WWW-Authenticate"] = 'Bearer realm="multigpt-runner"'
        return response
    base = trust.dist_dir()
    if arch in (trust.SUMS_NAME, f"{trust.SUMS_NAME}.sig"):
        path = base / arch
        if not path.is_file():
            raise Http404("Keine Prüfsummen vorhanden.")
        response = FileResponse(path.open("rb"), content_type="text/plain; charset=utf-8")
        response["X-Content-Type-Options"] = "nosniff"
        return _no_store(response)
    norm = trust.normalize_arch(arch)
    info = trust.dist_info()
    item = info.binaries.get(norm or "")
    if item is None:
        raise Http404("Für diese Architektur gibt es kein Runner-Programm.")
    response = FileResponse(
        trust.binary_path(norm).open("rb"),
        as_attachment=True,
        filename=item["name"],
        content_type="application/octet-stream",
    )
    response["X-Checksum-SHA256"] = item["sha256"]
    response["X-Content-Type-Options"] = "nosniff"
    return _no_store(response)
