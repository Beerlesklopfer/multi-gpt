"""Formular der RAG-Einstellungen mit Auswahl aus den gemeldeten Modellen (M7-09).

Die Auswahl „Embedding-Modell“, „OCR-Modell“ und „Modell für Abbildungen“ zeigt je
aktivem Anbieter die
vorhandenen ``AIModel``-Einträge und zusätzlich die vom Anbieter bei der
letzten Prüfung **gemeldeten** Modelle (``Provider.reported_models``), die noch
nicht angelegt sind („(neu)“). Wird ein neues gewählt, legt ``materialize``
beim Speichern das ``AIModel`` an – ohne Umweg über „KI-Modell ändern“.

Werte der Auswahl: ``m<pk>`` (vorhanden) oder ``r<anbieter-pk>:<modell-id>``
(gemeldet, neu). Neue Werte werden gegen die gespeicherte Modellliste des
Anbieters geprüft, beliebige IDs aus dem Formular werden nicht angelegt.
"""

from __future__ import annotations

import re

from django import forms
from django.core.exceptions import ValidationError
from django.utils.text import capfirst

from multigpt.chat.management.commands.sync_models import guess_capability
from multigpt.chat.models import AIModel, Provider, RagSettings
from multigpt.chat.rag.embeddings import suggested_prefixes

EMBEDDING = "embedding"
OCR = "ocr"
FIGURE = "figure"
MODEL_ID_MAX_LENGTH = AIModel._meta.get_field("model_id").max_length
NOT_FOR_OCR = {
    AIModel.Capability.EMBEDDING,
    AIModel.Capability.TTS,
    AIModel.Capability.STT,
    AIModel.Capability.IMAGE,
    AIModel.Capability.MUSIC,
}
MSG_INVALID = "Dieses Modell steht nicht (mehr) zur Auswahl. Bitte die Seite neu laden."


# OCR-Kandidaten bei Cloud-Anbietern nur mit eindeutigem Vision-/OCR-Muster in
# der ID (sonst stünden rund 100 Chatmodelle von OpenAI in der Liste).
OLMOCR_PATTERN = re.compile(r"olmocr", re.IGNORECASE)
VISION_PATTERN = re.compile(
    r"ocr|vision|llava|pixtral|(^|[-_./])vl([-_./]|$)|-vl\d|qwen\d?(\.\d)?-?vl", re.IGNORECASE
)
RECOMMENDED = "Empfohlen"


def _fits(purpose: str, capability: str) -> bool:
    if purpose == EMBEDDING:
        return capability == AIModel.Capability.EMBEDDING
    return capability not in NOT_FOR_OCR


def _figure_candidate(provider: Provider, model: AIModel | None, model_id: str) -> bool:
    """Abbildungen: nur OpenAI-kompatible Anbieter (``describe_image``); bei
    Cloud-Anbietern nur Modelle mit Bild-Eingabe bzw. Vision-Muster in der ID."""
    if provider.kind != Provider.Kind.OPENAI_COMPAT:
        return False
    if provider.is_local:
        return True
    return bool((model is not None and model.supports_vision) or VISION_PATTERN.search(model_id))


def is_figure_recommended(model: AIModel | None, model_id: str) -> bool:
    """Allgemeines Vision-Modell (nicht olmOCR, das nur Text liest)."""
    if is_olmocr(model_id):
        return False
    return bool((model is not None and model.supports_vision) or VISION_PATTERN.search(model_id))


def is_olmocr(model_id: str) -> bool:
    return bool(OLMOCR_PATTERN.search(model_id or ""))


def _offer_new(purpose: str, provider: Provider, model_id: str) -> bool:
    """Gemeldetes, noch nicht angelegtes Modell anbieten?"""
    if not _fits(purpose, guess_capability(model_id)):
        return False
    if purpose == OCR and not provider.is_local:
        return bool(VISION_PATTERN.search(model_id))
    if purpose == FIGURE:
        return _figure_candidate(provider, None, model_id)
    return True


def _label(provider: Provider, model: AIModel | None, model_id: str) -> str:
    if model is None:
        return f"{provider.name} · {model_id} (neu)"
    text = f"{provider.name} · {model.display_name}"
    if model.display_name != model.model_id:
        text += f" ({model.model_id})"
    if not model.active:
        text += " – für den Chat inaktiv" if model.capability == "chat" else " – inaktiv"
    return text


def model_choices(purpose: str, current: AIModel | None = None) -> list:
    """Gruppierte Auswahl für ``embedding``, ``ocr`` oder ``figure``.

    Reihenfolge: bei OCR zuerst die Gruppe „Empfohlen“ mit allen olmOCR-Modellen
    (vorhanden und gemeldet, über alle Anbieter), dann je Anbieter – lokale
    (LM Studio) vor Cloud-Anbietern. Neue OCR-Kandidaten von Cloud-Anbietern
    nur mit Vision-/OCR-Muster in der ID; angelegte Modelle bleiben wählbar.
    Das aktuell gesetzte Modell steht immer in der Liste, auch wenn sein
    Anbieter deaktiviert ist. Bei Abbildungen stehen allgemeine Vision-Modelle
    unter „Empfohlen“; angeboten werden nur OpenAI-kompatible Anbieter, bei
    Cloud-Anbietern nur Modelle mit Bild-Eingabe bzw. Vision-Muster in der ID.
    """
    provider_ids = set(Provider.objects.filter(active=True).values_list("pk", flat=True))
    if current is not None:
        provider_ids.add(current.provider_id)
    providers = (
        Provider.objects.filter(pk__in=provider_ids)
        .prefetch_related("ai_models")
        .order_by("-is_local", "name", "pk")
    )
    recommended: list = []
    groups: list = []
    for provider in providers:
        existing = {m.model_id: m for m in provider.ai_models.all()}
        options = []
        for model in existing.values():
            selected = current is not None and model.pk == current.pk
            fits = _fits(purpose, model.capability) and (
                purpose != FIGURE or _figure_candidate(provider, model, model.model_id)
            )
            if selected or (provider.active and fits):
                options.append(
                    (model.model_id, f"m{model.pk}", _label(provider, model, model.model_id))
                )
        if provider.active:
            for model_id in dict.fromkeys(provider.reported_models or []):
                if not isinstance(model_id, str) or model_id in existing:
                    continue
                if not 0 < len(model_id) <= MODEL_ID_MAX_LENGTH:
                    continue
                if _offer_new(purpose, provider, model_id):
                    options.append(
                        (model_id, f"r{provider.pk}:{model_id}", _label(provider, None, model_id))
                    )
        rest = []
        for model_id, value, label in options:
            if purpose == OCR and is_olmocr(model_id):
                recommended.append((value, label))
            elif purpose == FIGURE and is_figure_recommended(existing.get(model_id), model_id):
                recommended.append((value, label))
            else:
                rest.append((value, label))
        if rest:
            rest.sort(key=lambda option: option[1].lower())
            groups.append((provider.name, rest))
    choices: list = [("", "---------")]
    if recommended:
        choices.append((RECOMMENDED, recommended))
    return choices + groups


def first_recommended(choices: list) -> str:
    """Wert des ersten empfohlenen Modells (olmOCR) oder ``""``."""
    for group, options in choices:
        if group == RECOMMENDED and options:
            return options[0][0]
    return ""


def resolve_choice(purpose: str, value: str) -> AIModel | None:
    """Auswahlwert -> ``AIModel`` (bei „neu“ ungespeichert) oder ``ValidationError``."""
    if not value:
        return None
    if value.startswith("m") and value[1:].isdigit():
        model = AIModel.objects.select_related("provider").filter(pk=int(value[1:])).first()
        if model is None:
            raise ValidationError(MSG_INVALID)
        return model
    if value.startswith("r") and ":" in value:
        provider_pk, _, model_id = value[1:].partition(":")
        provider = (
            Provider.objects.filter(pk=int(provider_pk), active=True).first()
            if provider_pk.isdigit()
            else None
        )
        if (
            provider is None
            or model_id not in (provider.reported_models or [])
            or not 0 < len(model_id) <= MODEL_ID_MAX_LENGTH
        ):
            raise ValidationError(MSG_INVALID)
        model = provider.ai_models.filter(model_id=model_id).first()
        if model is not None:  # inzwischen angelegt (z. B. Statusprüfung)
            return model
        capability = (
            AIModel.Capability.EMBEDDING if purpose == EMBEDDING else AIModel.Capability.CHAT
        )
        # Embedding-Modelle aktiv; OCR-Modelle und Modelle für Abbildungen nur
        # dafür (für den Chat inaktiv).
        return AIModel(
            provider=provider,
            model_id=model_id,
            display_name=model_id,
            capability=capability,
            active=purpose == EMBEDDING,
            supports_vision=purpose == FIGURE,
        )
    raise ValidationError(MSG_INVALID)


def materialize(model: AIModel | None) -> AIModel | None:
    """Ungespeichertes ``AIModel`` aus ``resolve_choice`` anlegen (oder vorhandenes nehmen)."""
    if model is None or model.pk is not None:
        return model
    saved, _ = AIModel.objects.get_or_create(
        provider=model.provider,
        model_id=model.model_id,
        defaults={
            "display_name": model.display_name,
            "capability": model.capability,
            "active": model.active,
            "supports_vision": model.supports_vision,
        },
    )
    return saved


def _field(name: str):
    return RagSettings._meta.get_field(name)


class RagSettingsForm(forms.ModelForm):
    embedding_model = forms.ChoiceField(
        label="Embedding-Modell",
        required=False,
        help_text=_field("embedding_model").help_text
        + " Zur Auswahl stehen auch die von den Anbietern gemeldeten, noch nicht "
        "angelegten Modelle („(neu)“); sie werden beim Speichern angelegt.",
    )
    ocr_model = forms.ChoiceField(
        label="OCR-Modell",
        required=False,
        help_text=_field("ocr_model").help_text
        + " Neu gewählte Modelle werden nur für OCR angelegt (für den Chat inaktiv).",
    )
    figure_model = forms.ChoiceField(
        label="Modell für Abbildungen",
        required=False,
        help_text=_field("figure_model").help_text
        + " Neu gewählte Modelle werden nur dafür angelegt (für den Chat inaktiv).",
    )
    # strip=False: Die Präfixe enden auf ein Leerzeichen („search_query: “).
    document_prefix = forms.CharField(
        label=capfirst(_field("document_prefix").verbose_name),
        help_text=_field("document_prefix").help_text,
        required=False,
        strip=False,
        max_length=100,
    )
    query_prefix = forms.CharField(
        label=capfirst(_field("query_prefix").verbose_name),
        help_text=_field("query_prefix").help_text,
        required=False,
        strip=False,
        max_length=100,
    )

    class Meta:
        model = RagSettings
        fields = [
            "embedding_model",
            "document_prefix",
            "query_prefix",
            "ocr_backend",
            "ocr_model",
            "ocr_fallback_tesseract",
            "describe_figures",
            "figure_model",
            "figure_max_per_document",
            "figure_max_per_page",
            "figure_min_edge",
            "figure_max_edge",
            "chunk_tokens",
            "overlap_tokens",
            "top_k",
            "hybrid",
            "crossref_enabled",
            "crossref_mailto",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        instance = self.instance
        embedding = instance.embedding_model if instance.embedding_model_id else None
        ocr = instance.ocr_model if instance.ocr_model_id else None
        figure = instance.figure_model if instance.figure_model_id else None
        self.fields["embedding_model"].choices = model_choices(EMBEDDING, embedding)
        self.fields["ocr_model"].choices = model_choices(OCR, ocr)
        self.fields["figure_model"].choices = model_choices(FIGURE, figure)
        self.initial["figure_model"] = f"m{figure.pk}" if figure else ""
        self.initial["embedding_model"] = f"m{embedding.pk}" if embedding else ""
        # Noch kein OCR-Modell gesetzt: olmOCR (falls vorhanden) vorauswählen.
        self.initial["ocr_model"] = (
            f"m{ocr.pk}" if ocr else first_recommended(self.fields["ocr_model"].choices)
        )

    def clean_embedding_model(self):
        return resolve_choice(EMBEDDING, self.cleaned_data.get("embedding_model") or "")

    def clean_ocr_model(self):
        return resolve_choice(OCR, self.cleaned_data.get("ocr_model") or "")

    def clean_figure_model(self):
        return resolve_choice(FIGURE, self.cleaned_data.get("figure_model") or "")

    def clean(self):
        cleaned = super().clean()
        model = cleaned.get("embedding_model")
        # Bei neuem Embedding-Modell die Präfixe vorbelegen, sofern sie nicht
        # gleichzeitig von Hand geändert wurden (nomic: search_document/search_query).
        if (
            model is not None
            and "embedding_model" in self.changed_data
            and not {"document_prefix", "query_prefix"} & set(self.changed_data)
        ):
            cleaned["document_prefix"], cleaned["query_prefix"] = suggested_prefixes(model.model_id)
        return cleaned
