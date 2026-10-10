"""Formular für die Literaturangaben eines Dokuments (Sammlungsansicht und Admin).

Validierung: Datum (JJJJ, JJJJ-MM, JJJJ-MM-TT; deutsche Schreibweise
„12.03.2024“ bzw. „03.2024“ wird umgewandelt), ISBN mit Prüfziffer, DOI (auch
als https://doi.org/… eingefügt), Seitenbereich, URL nur http(s). Wer speichert,
setzt ``bib_edited``: Danach überschreibt die Indexierung nichts mehr.
"""

import re

from django import forms
from django.core.validators import URLValidator

from . import citations
from .models import Document

BIB_FIELDS = [
    "bib_type",
    "bib_authors",
    "bib_editors",
    "bib_title",
    "bib_container",
    "bib_date",
    "bib_publisher",
    "bib_place",
    "bib_edition",
    "bib_series",
    "bib_series_number",
    "bib_journal",
    "bib_volume",
    "bib_issue",
    "bib_pages",
    "bib_number",
    "bib_institution",
    "bib_status",
    "bib_replaces",
    "bib_isbn",
    "bib_isbn_e",
    "bib_doi",
    "bib_url",
]

_GERMAN_DATE = re.compile(r"^(?:(\d{1,2})\.)?(\d{1,2})\.(\d{4})$")
MAX_PEOPLE = 50


class DocumentCitationForm(forms.ModelForm):
    bib_url = forms.URLField(
        label="URL",
        required=False,
        max_length=2000,
        assume_scheme="https",
        validators=[URLValidator(schemes=["http", "https"])],
    )

    class Meta:
        model = Document
        fields = BIB_FIELDS
        widgets = {
            "bib_authors": forms.Textarea(attrs={"rows": 3}),
            "bib_editors": forms.Textarea(attrs={"rows": 2}),
        }

    def _people(self, name: str) -> str:
        lines = citations.split_people(self.cleaned_data.get(name, ""))
        if len(lines) > MAX_PEOPLE:
            raise forms.ValidationError(f"Höchstens {MAX_PEOPLE} Personen.")
        if any(len(line) > 200 for line in lines):
            raise forms.ValidationError("Ein Name ist zu lang (höchstens 200 Zeichen).")
        return "\n".join(lines)

    def clean_bib_authors(self):
        return self._people("bib_authors")

    def clean_bib_editors(self):
        return self._people("bib_editors")

    def clean_bib_date(self):
        value = (self.cleaned_data.get("bib_date") or "").strip()
        match = _GERMAN_DATE.match(value)
        if match:
            day, month, year = match.groups()
            value = f"{year}-{int(month):02d}" + (f"-{int(day):02d}" if day else "")
        if value:
            citations.validate_bib_date(value)
        return value

    def clean_bib_doi(self):
        value = citations.normalize_doi(self.cleaned_data.get("bib_doi", ""))
        if value:
            citations.validate_doi(value)
        return value

    def clean_bib_isbn(self):
        return " ".join((self.cleaned_data.get("bib_isbn") or "").split())

    def clean_bib_isbn_e(self):
        return " ".join((self.cleaned_data.get("bib_isbn_e") or "").split())

    def save(self, commit=True):
        self.instance.bib_edited = True
        return super().save(commit=commit)
