"""Seiten der Projekte (M5-07, chat/projects.py): Liste mit Anlegen und die
Projektseite mit Einstellungen, Chats, Archivieren und Löschen.

Alles funktioniert ohne JavaScript (Formulare mit CSRF); projects.js ergänzt
Dialoge. Fremde Projekte -> 404. Löschen braucht die Wahl „Chats behalten“
bzw. „Chats mitlöschen“ und eine ausdrückliche Bestätigung.
"""

from django import forms
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_http_methods, require_POST

from multigpt.accounts.permissions import Action, can

from . import projects, services
from .models import AIModel, Project
from .rag.search import readable_collections


class CollectionChoiceField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, obj):
        owner_id = getattr(self, "viewer_id", None)
        if owner_id is not None and obj.owner_id != owner_id:
            return f"{obj.name} (von {obj.owner})"
        return obj.name


class ProjectForm(forms.ModelForm):
    """Einstellungen eines Projekts. Auswahl nur aus eigenen Möglichkeiten:
    freigegebene Chat-Modelle und lesbare Sammlungen."""

    collections = CollectionChoiceField(
        queryset=None,
        required=False,
        widget=forms.CheckboxSelectMultiple,
        label="Sammlungen",
        help_text="Im Eingabefeld der Chats vorausgewählt, wenn du sie lesen darfst.",
    )

    class Meta:
        model = Project
        fields = [
            "name",
            "description",
            "instructions",
            "default_model",
            "collections",
            "color",
            "pinned",
        ]
        widgets = {
            "description": forms.Textarea(attrs={"rows": 3}),
            "instructions": forms.Textarea(attrs={"rows": 8}),
        }
        labels = {
            "pinned": "In der Seitenleiste oben anheften",
        }
        help_texts = {
            "instructions": (
                "Gelten in allen Chats dieses Projekts zusätzlich zum System-Prompt des "
                "Chats (davor). Zum Beispiel: „Antworte immer auf Deutsch und duze mich.“"
            ),
            "default_model": "Vorauswahl für neue Chats im Projekt, wenn erreichbar.",
        }

    def __init__(self, *args, user, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["name"].max_length = projects.NAME_MAX_LENGTH
        self.fields["description"].max_length = projects.DESCRIPTION_MAX_LENGTH
        self.fields["instructions"].max_length = projects.INSTRUCTIONS_MAX_LENGTH
        for name in ("description", "instructions"):
            self.fields[name].widget.attrs["maxlength"] = self.fields[name].max_length
        model_ids = [m.pk for m in services.chat_models_for(user)]
        current = self.instance.default_model_id if self.instance.pk else None
        if current:
            model_ids.append(current)
        default_model = self.fields["default_model"]
        default_model.queryset = (
            AIModel.objects.filter(pk__in=model_ids)
            .select_related("provider")
            .order_by("provider__name", "display_name")
        )
        default_model.empty_label = "kein Standardmodell"
        default_model.label_from_instance = lambda m: f"{m.display_name} ({m.provider.name})"
        collections = self.fields["collections"]
        collections.queryset = readable_collections(user).select_related("owner").order_by("name")
        collections.viewer_id = user.pk

    def clean_name(self):
        name, err = projects.clean_name(self.cleaned_data.get("name"))
        if err:
            raise forms.ValidationError(err)
        return name

    def clean_default_model(self):
        model = self.cleaned_data.get("default_model")
        if model is not None and model.pk != self.instance.default_model_id:
            _, err = projects.clean_default_model(self.user, model.pk)
            if err:
                raise forms.ValidationError(err)
        return model


class ProjectCreateForm(forms.ModelForm):
    class Meta:
        model = Project
        fields = ["name"]
        labels = {"name": "Neues Projekt"}

    def clean_name(self):
        name, err = projects.clean_name(self.cleaned_data.get("name"))
        if err:
            raise forms.ValidationError(err)
        return name


def _own_project(request, pk) -> Project:
    project = projects.get_own_project(request.user, pk)
    if project is None:
        raise Http404
    return project


@require_http_methods(["GET", "POST"])
@login_required
def project_list(request):
    user = request.user
    may_create = can(user, Action.CHAT)
    form = ProjectCreateForm()
    if request.method == "POST":
        if not may_create:
            raise PermissionDenied
        form = ProjectCreateForm(request.POST)
        if form.is_valid():
            project = form.save(commit=False)
            project.owner = user
            project.save()
            messages.success(request, f"Projekt „{project.name}“ angelegt.")
            return redirect("chat:project_detail", pk=project.pk)
    items = list(projects.with_chat_count(projects.own_projects(user, archived=None)))
    return render(
        request,
        "chat/projects/list.html",
        {
            "form": form,
            "can_create": may_create,
            "active_projects": [p for p in items if not p.archived],
            "archived_projects": [p for p in items if p.archived],
        },
    )


@require_http_methods(["GET", "POST"])
@login_required
def project_detail(request, pk):
    project = _own_project(request, pk)
    if request.method == "POST":
        form = ProjectForm(request.POST, instance=project, user=request.user)
        if form.is_valid():
            form.save()
            messages.success(request, "Projekt gespeichert.")
            return redirect("chat:project_detail", pk=project.pk)
    else:
        form = ProjectForm(instance=project, user=request.user)
    chats = list(projects.project_chats(project))
    readable = set(projects.readable_collection_ids(request.user, project))
    model = projects.usable_default_model(request.user, project)
    return render(
        request,
        "chat/projects/detail.html",
        {
            "project": project,
            "active_project": project,
            "form": form,
            "project_chats_list": chats,
            "chat_count": len(chats),
            "project_collections": [
                c for c in project.collections.order_by("name") if c.pk in readable
            ],
            "project_model": model,
            "new_chat_url": f"{reverse('chat:index')}?projekt={project.pk}",
        },
    )


@require_POST
@login_required
def project_archive(request, pk):
    project = _own_project(request, pk)
    archived = request.POST.get("archived") == "1"
    Project.objects.filter(pk=project.pk).update(archived=archived)
    messages.success(
        request,
        f"Projekt „{project.name}“ {'archiviert' if archived else 'wiederhergestellt'}.",
    )
    return redirect("chat:project_detail", pk=project.pk)


@require_POST
@login_required
def project_delete(request, pk):
    project = _own_project(request, pk)
    mode = request.POST.get("chats")
    if mode not in ("keep", "delete") or request.POST.get("confirm") != "1":
        messages.error(
            request,
            "Projekt nicht gelöscht: Bitte wählen, was mit den Chats geschieht, und das "
            "Löschen bestätigen.",
        )
        return redirect(f"{reverse('chat:project_detail', args=[project.pk])}#projekt-loeschen")
    name = project.name
    deleted = projects.delete_project(project, delete_chats=mode == "delete")
    if mode == "delete":
        messages.success(
            request,
            f"Projekt „{name}“ und {deleted} Chat{'' if deleted == 1 else 's'} gelöscht.",
        )
    else:
        messages.success(request, f"Projekt „{name}“ gelöscht. Die Chats sind jetzt ohne Projekt.")
    return redirect("chat:project_list")
