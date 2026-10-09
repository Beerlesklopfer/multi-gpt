"""Formular für Verzeichnisquellen im Admin (Agent crawler).

Anlegen: vorhandene Sammlung wählen ODER neue Sammlung mit Besitzer anlegen.
Der Pfad wird gegen ``RAG_SOURCE_ROOTS`` (realpath) und auf Lesbarkeit für den
laufenden Dienstnutzer geprüft und als echter Pfad gespeichert. Nach der
Anlage sind Pfad und Sammlung fest (die Dokumente verweisen relativ darauf).
"""

from django import forms
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError

from multigpt.chat.models import Collection

from . import paths
from .crawl import parse_patterns
from .models import MIN_INTERVAL_MINUTES, DirectorySource

NAME_MAX = Collection._meta.get_field("name").max_length


class DirectorySourceForm(forms.ModelForm):
    new_collection_name = forms.CharField(
        label="Neue Sammlung: Name",
        required=False,
        max_length=NAME_MAX,
        help_text="Statt einer vorhandenen Sammlung eine neue anlegen.",
    )
    new_collection_owner = forms.ModelChoiceField(
        label="Neue Sammlung: Besitzer",
        queryset=get_user_model().objects.filter(is_active=True).order_by("username"),
        required=False,
        help_text="Der Besitzer kann die Sammlung im Chat nutzen und mit Gruppen teilen.",
    )

    class Meta:
        model = DirectorySource
        fields = [
            "collection",
            "path",
            "recursive",
            "include_patterns",
            "exclude_patterns",
            "interval_minutes",
            "active",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "collection" in self.fields:
            self.fields["collection"].required = False
            self.fields["collection"].queryset = Collection.objects.select_related(
                "owner"
            ).order_by("name", "pk")
            self.fields["collection"].label_from_instance = lambda c: (
                f"{c.name} ({c.owner.get_full_name() or c.owner.get_username()})"
            )
        if "path" in self.fields:
            roots = paths.allowed_roots()
            self.fields["path"].help_text = (
                "Absoluter Pfad auf dem Server. Erlaubte Wurzeln: "
                + (", ".join(roots) if roots else "keine (RAG_SOURCE_ROOTS ist leer)")
                + ". Der Dienstnutzer „multi-gpt“ braucht Leserechte."
            )

    def clean_path(self):
        raw = (self.cleaned_data.get("path") or "").strip()
        try:
            return paths.check_directory(raw)
        except paths.SourcePathError as exc:
            raise ValidationError(exc.message) from exc

    def clean_include_patterns(self):
        value = self.cleaned_data.get("include_patterns") or ""
        if not parse_patterns(value):
            raise ValidationError("Bitte mindestens ein Dateimuster angeben, z. B. „*.pdf“.")
        return ", ".join(parse_patterns(value))

    def clean_exclude_patterns(self):
        return ", ".join(parse_patterns(self.cleaned_data.get("exclude_patterns") or ""))

    def clean_interval_minutes(self):
        value = self.cleaned_data.get("interval_minutes")
        if value is None or value < MIN_INTERVAL_MINUTES:
            raise ValidationError(f"Mindestens {MIN_INTERVAL_MINUTES} Minuten.")
        return value

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk:
            return cleaned
        collection = cleaned.get("collection")
        name = " ".join((cleaned.get("new_collection_name") or "").split())
        owner = cleaned.get("new_collection_owner")
        if collection and (name or owner):
            raise ValidationError(
                "Bitte entweder eine vorhandene Sammlung wählen oder eine neue anlegen, "
                "nicht beides."
            )
        if not collection:
            if not name or not owner:
                raise ValidationError(
                    "Bitte eine Sammlung wählen oder Name und Besitzer der neuen Sammlung angeben."
                )
            if Collection.objects.filter(owner=owner, name=name).exists():
                self.add_error(
                    "new_collection_name", "Dieser Besitzer hat schon eine Sammlung mit dem Namen."
                )
            cleaned["new_collection_name"] = name
        path = cleaned.get("path")
        if (
            collection
            and path
            and DirectorySource.objects.filter(collection=collection, path=path).exists()
        ):
            self.add_error("path", "Diese Sammlung liest das Verzeichnis bereits ein.")
        return cleaned

    def save(self, commit=True):
        if not self.instance.pk and not self.cleaned_data.get("collection"):
            self.instance.collection = Collection.objects.create(
                owner=self.cleaned_data["new_collection_owner"],
                name=self.cleaned_data["new_collection_name"],
            )
        return super().save(commit=commit)
