"""Admin-Bausteine der Modellverwaltung für LM Studio (M7-09, erweitert 0.3.2).

- Auswahl „Kontextfenster“ (Stufen bis zur maximalen Kontextlänge laut LM
  Studio) samt „Erwarteter Speicher“, der per kleinem JS live nachgerechnet
  wird (``rag/vram_estimate.js``, JSON von ``vram_view``) – in den
  RAG-Einstellungen (Embedding, OCR, Abbildungen), in der Fähigkeiten-Matrix
  und im Formular eines Modells.
- Seite „LM Studio: geladene Modelle“ (``page_context`` / ``page_action``):
  wie ``lms ps`` mit Budgetbalken und Knöpfen laden, entladen, anheften.

Nur für Verwalter: Die Ansichten hängen an der ``FamilyAdminSite``
(``can(user, Action.ADMIN)``); verändernde Aktionen zusätzlich nur per POST mit
CSRF-Token und mit Änderungsrecht auf KI-Modelle.
"""

from __future__ import annotations

import logging

from django import forms
from django.core.exceptions import ValidationError
from django.http import JsonResponse
from django.urls import reverse_lazy
from django.utils import timezone
from django.utils.html import format_html

from . import lmstudio, model_manager, vram
from .models import AIModel, ModelLease, ModelRuntime, Provider

logger = logging.getLogger(__name__)

ADMIN_TIMEOUT = 2.0
MSG_NOT_MANAGED = "Für diesen Anbieter ist „Modelle verwalten“ aus bzw. kein Budget gesetzt."
MSG_CONTEXT_TOO_BIG = "Das Modell kann höchstens {max} Tokens (laut LM Studio)."


def _num(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _gb(value: int | float) -> str:
    return vram.gb_text(value)


def context_choices(max_context: int | None) -> list[tuple[str, str]]:
    return [("", "Vorschlag")] + [(str(s), _num(s)) for s in vram.stages(max_context)]


def cached_max_context(ai_model: AIModel | None) -> int | None:
    if ai_model is None or ai_model.pk is None:
        return None
    return (
        ModelRuntime.objects.filter(ai_model=ai_model)
        .values_list("max_context_length", flat=True)
        .first()
    )


def refresh_catalogs(timeout: float = ADMIN_TIMEOUT) -> None:
    """Eckdaten der verwalteten Modelle auffrischen (nur lesend, kurz).

    Nur Anbieter mit „Modelle verwalten“: ohne Verwaltung keine zusätzlichen
    Abrufe beim Öffnen der Einstellungen (dann gelten gemerkte Werte)."""
    for provider in model_manager.local_providers():
        if not model_manager.managed(provider):
            continue
        try:
            model_manager.remember_catalog(provider, lmstudio.list_models(provider, timeout))
        except lmstudio.LmsError:
            continue


def context_field(label: str, max_context: int | None, help_text: str = "") -> forms.Field:
    return forms.TypedChoiceField(
        label=label,
        required=False,
        coerce=int,
        empty_value=None,
        choices=context_choices(max_context),
        help_text=help_text,
        widget=forms.Select(
            attrs={
                "class": "vram-context",
                "data-vram-url": reverse_lazy("admin:rag_overview_vram"),
            }
        ),
    )


def check_context(value: int | None, max_context: int | None) -> int | None:
    if value and value not in vram.CONTEXT_STAGES:
        raise ValidationError("Bitte eine der angebotenen Stufen wählen.")
    if value and max_context and value > max_context:
        raise ValidationError(MSG_CONTEXT_TOO_BIG.format(max=_num(max_context)))
    return value


CONTEXT_HELP = (
    "Damit lädt MultiGPT das Modell in LM Studio („Vorschlag“: Chat 32 768, OCR 8 192, "
    "Embedding Modellstandard). Daneben der erwartete Speicher: Modelldatei plus Kontext-Cache."
)


class DefaultsWhenMissingMixin:
    """Felder mit Standardwert dürfen im POST fehlen (ältere Formulare, Tests, Skripte):
    nicht Pflicht, leer -> Standardwert des Modells bzw. bisheriger Wert."""

    defaulted_fields = (
        "vram_reserve_gb",
        "load_priority",
        "idle_unload_minutes",
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.defaulted_fields:
            if name in self.fields:
                self.fields[name].required = False

    def _post_clean(self):
        for name in self.defaulted_fields:
            if name in self.fields and self.cleaned_data.get(name) is None:
                if self.instance.pk is not None:
                    current = type(self.instance).objects.filter(pk=self.instance.pk)
                    value = current.values_list(name, flat=True).first()
                else:
                    value = None
                if value is None:
                    value = self.instance._meta.get_field(name).get_default()
                self.cleaned_data[name] = value
        super()._post_clean()


class LoadProfileFormMixin:
    """``load_context_length`` als Auswahl der Stufen (Formular und Matrix)."""

    def _setup_context(self):
        instance = self.instance
        local = instance.pk and instance.provider_id and instance.provider.is_local
        max_context = cached_max_context(instance) if local else None
        field = context_field(
            AIModel._meta.get_field("load_context_length").verbose_name.capitalize(),
            max_context,
            CONTEXT_HELP,
        )
        if local:
            field.widget.attrs["data-vram-model"] = f"m{instance.pk}"
        current = instance.load_context_length
        if current and str(current) not in dict(field.choices):
            field.choices = [*field.choices, (str(current), _num(current))]
        self.fields["load_context_length"] = field
        self._max_context = max_context

    def clean_load_context_length(self):
        value = self.cleaned_data.get("load_context_length")
        if value == self.instance.load_context_length:
            return value  # unverändert: auch alte Werte außerhalb der Stufen behalten
        return check_context(value, self._max_context)


class AIModelForm(DefaultsWhenMissingMixin, LoadProfileFormMixin, forms.ModelForm):
    class Meta:
        model = AIModel
        # Alle Felder wie das Standardformular des Admins (Feldliste über get_fields).
        fields = "__all__"  # noqa: DJ007

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "load_context_length" in self.fields:
            self._setup_context()


class MatrixForm(DefaultsWhenMissingMixin, LoadProfileFormMixin, forms.ModelForm):
    """Zeile der Fähigkeiten-Matrix (list_editable)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if "load_context_length" in self.fields:
            self._setup_context()


def matrix_hint(ai_model: AIModel) -> str:
    """Spalte der Fähigkeiten-Matrix: Schätzung aus den gemerkten Eckdaten (ohne Netz)."""
    provider = ai_model.provider
    if not provider.is_local:
        return "–"
    rt = ModelRuntime.objects.filter(ai_model=ai_model).first()
    if not (ai_model.vram_gb or (rt and rt.size_bytes)):
        return "?"
    context = model_manager.profile_context(ai_model, rt.max_context_length if rt else None)
    est = model_manager.estimate_model(
        ai_model, None, context, model_manager.load_parallel(ai_model), rt
    )
    text = f"≈ {_gb(est.total)}"
    if provider.vram_budget_gb:
        usable = float(provider.vram_budget_gb - provider.vram_reserve_gb) * vram.GB
        if est.total > usable:
            return format_html('{} <strong class="rag-bad">größer als das Budget</strong>', text)
    return text


# --- Schätzung als JSON ------------------------------------------------------------


def _resolve(choice: str) -> tuple[Provider | None, AIModel | None, str]:
    """``m<pk>`` (Modell) oder ``r<anbieter>:<modell-id>`` (gemeldet, noch nicht angelegt)."""
    if choice.startswith("m") and choice[1:].isdigit():
        ai_model = AIModel.objects.select_related("provider").filter(pk=int(choice[1:])).first()
        if ai_model is not None:
            return ai_model.provider, ai_model, ai_model.model_id
    if choice.startswith("r") and ":" in choice:
        provider_pk, _, model_id = choice[1:].partition(":")
        if provider_pk.isdigit():
            provider = Provider.objects.filter(pk=int(provider_pk)).first()
            if provider is not None and model_id in (provider.reported_models or []):
                return provider, provider.ai_models.filter(model_id=model_id).first(), model_id
    return None, None, ""


def _entry(catalog, model_id: str):
    for entry in catalog or []:
        if entry.key == model_id or any(i.id == model_id for i in entry.instances):
            return entry
    return None


def _eliezer_estimate(provider: Provider, model_id: str, context, parallel):
    """Eliezers Schätzung für das Laden, sonst ``None`` (ältere Version, nicht erreichbar)."""
    try:
        return lmstudio.estimate(
            provider, model_id, context_length=context, parallel=parallel, timeout=ADMIN_TIMEOUT
        )
    except lmstudio.LmsError:
        return None


def estimate_payload(
    provider: Provider,
    ai_model: AIModel | None,
    model_id: str,
    context: int | None,
    capability: str = "",
    catalog=None,
) -> dict:
    """Erwarteter Speicher, Budget und Warnung für Modell und Kontextfenster."""
    error = ""
    if catalog is None:
        try:
            catalog = lmstudio.list_models(provider, ADMIN_TIMEOUT)
        except lmstudio.LmsError as exc:
            catalog, error = None, exc.message
    entry = _entry(catalog, model_id)
    rt = ModelRuntime.objects.filter(ai_model=ai_model).first() if ai_model else None
    max_context = entry.max_context_length if entry else (rt.max_context_length if rt else None)
    capability = capability or (ai_model.capability if ai_model else AIModel.Capability.CHAT)
    if not context:
        if ai_model is not None and ai_model.load_context_length:
            context = ai_model.load_context_length
        else:
            context = vram.default_context(capability, max_context)
    parallel = model_manager.load_parallel(ai_model) if ai_model else None
    eliezer = None
    if entry is not None and model_manager.eliezer(provider):
        eliezer = _eliezer_estimate(provider, entry.key, context, parallel)
    pool = eliezer.pool if eliezer is not None else None
    if eliezer is not None:
        est = vram.Estimate(
            eliezer.weights + eliezer.vision,
            eliezer.kv_cache,
            eliezer.overhead,
            context,
            f"Eliezer: {eliezer.basis}",
            eliezer.basis == "measured",
        )
    else:
        est = model_manager.estimate_model(ai_model, entry, context, parallel, rt)
    known = bool(entry or (rt and rt.size_bytes) or est.measured)
    parts = [f"Datei {_gb(est.weights)}", f"Kontext-Cache {_gb(est.kv)}"]
    parts.append(f"Zuschlag {_gb(est.overhead)}")
    detail = "gemessen" if est.measured else " + ".join(parts) + f"; {est.source}"
    text = f"Erwartet {_gb(est.total)} ({detail})" if known else "Größe unbekannt"
    result = {
        "stages": vram.stages(max_context),
        "max_context": max_context,
        "context": context,
        "estimate": est.as_dict(),
        "known": known,
        "text": text,
        "budget": None,
        "budget_text": "",
        "warning": "",
        "fits": None,
        "error": error,
    }
    if pool is None and not provider.vram_budget_gb:
        if provider.is_local:
            result["budget_text"] = "Kein Speicherbudget beim Anbieter eingestellt."
        return result
    if catalog is not None:
        snap = model_manager.snapshot(provider, catalog, pool=pool)
        others = [
            s
            for s in snap.slots
            if s.instance_id != model_id
            and s.key != model_id
            and (ai_model is None or s.ai_model is None or s.ai_model.pk != ai_model.pk)
        ]
        usable = snap.usable
        reserve = snap.reserve
    else:
        others = []
        usable = int(float(provider.vram_budget_gb - provider.vram_reserve_gb) * vram.GB)
        reserve = int(float(provider.vram_reserve_gb) * vram.GB)
    used = sum(s.bytes for s in others)
    if pool is not None:
        # Eliezers freier Platz zählt alles Geladene; das eigene Modell wird nicht abgezogen.
        free = pool.free + sum(s.bytes for s in snap.slots if s not in others)
    else:
        free = usable - used
    by = ", ".join(f"{s.label} {_gb(s.bytes)}" for s in others) or "nichts"
    result["budget"] = {
        "budget_gb": vram.gb(usable + reserve),
        "reserve_gb": vram.gb(reserve),
        "usable_gb": vram.gb(usable),
        "used_gb": vram.gb(used),
        "free_gb": vram.gb(free),
    }
    result["budget_text"] = (
        f"Budget {_gb(usable + reserve)}, Reserve für Anzeige/System {_gb(reserve)}; "
        f"belegt durch andere: {by}; frei {_gb(max(0, free))}."
    )
    if known:
        result["fits"] = est.total <= free
        if est.total > usable:
            result["warning"] = (
                f"passt nie: {_gb(est.total - usable)} mehr als das ganze Budget – "
                "kleinere Stufe oder kleineres Modell wählen."
            )
        elif est.total > free:
            result["warning"] = (
                f"passt nicht: {_gb(est.total - free)} zu viel – kleinere Stufe wählen oder "
                "anderes Modell entladen (geschieht beim Laden automatisch, soweit erlaubt)."
            )
    return result


def vram_view(request):
    """GET ``?model=<m…|r…>&context=<Tokens>`` -> JSON (nur Verwalter, nur lesend)."""
    provider, ai_model, model_id = _resolve(request.GET.get("model", ""))
    if provider is None:
        return JsonResponse({"error": "Unbekanntes Modell."}, status=404)
    if not provider.is_local:
        return JsonResponse({"local": False, "text": "", "stages": list(vram.CONTEXT_STAGES)})
    raw = request.GET.get("context") or ""
    context = int(raw) if raw.isdigit() and int(raw) in vram.CONTEXT_STAGES else None
    capability = request.GET.get("capability", "")
    if capability not in AIModel.Capability.values:
        capability = ""
    payload = estimate_payload(provider, ai_model, model_id, context, capability)
    payload["local"] = True
    return JsonResponse(payload)


# --- Seite „LM Studio: geladene Modelle“ ---------------------------------------------


def _status(rt: ModelRuntime | None, loaded: bool, now) -> tuple[str, str]:
    if rt and rt.loading_since and rt.loading_since > now - model_manager.LEASE_TTL:
        return "running", "wird geladen …"
    if loaded:
        return "done", "geladen"
    if rt and rt.retry_after and rt.retry_after > now:
        when = timezone.localtime(rt.retry_after)
        return "error", f"Ladefehler, neuer Versuch ab {when:%H:%M}"
    return "", "nicht geladen"


def provider_context(provider: Provider) -> dict:
    """Zeilen, Budgetbalken und Meldungen für einen lokalen Anbieter."""
    now = timezone.now()
    data = {"provider": provider, "managed": model_manager.managed(provider), "rows": []}
    try:
        catalog = lmstudio.list_models(provider, ADMIN_TIMEOUT)
        model_manager.remember_catalog(provider, catalog)
        data["error"] = ""
    except lmstudio.LmsError as exc:
        catalog, data["error"] = None, exc.message
    snap = model_manager.snapshot(provider, catalog) if catalog is not None else None
    runtimes = {
        rt.ai_model_id: rt for rt in ModelRuntime.objects.filter(ai_model__provider=provider)
    }
    busy: dict[int, int] = {}
    for model_id in ModelLease.objects.filter(
        ai_model__provider=provider, expires__gt=now
    ).values_list("ai_model_id", flat=True):
        busy[model_id] = busy.get(model_id, 0) + 1
    indexing = model_manager.indexing_ids()
    slots = {s.ai_model.pk: s for s in snap.slots if s.ai_model} if snap else {}
    usable = snap.usable if snap and snap.budget else None
    for ai_model in provider.ai_models.order_by("capability", "model_id"):
        rt = runtimes.get(ai_model.pk)
        slot = slots.get(ai_model.pk)
        entry = _entry(catalog, ai_model.model_id)
        if catalog is not None and entry is None:
            continue  # in LM Studio nicht (mehr) vorhanden
        payload = (
            estimate_payload(provider, ai_model, ai_model.model_id, None, catalog=catalog)
            if catalog is not None
            else None
        )
        level, label = _status(rt, slot is not None, now)
        total = payload["estimate"]["total_gb"] if payload else None
        data["rows"].append(
            {
                "model": ai_model,
                "size": _gb(entry.size_bytes) if entry and entry.size_bytes else "–",
                "max_context": entry.max_context_length if entry else None,
                "loaded_context": slot.context if slot else None,
                "slot": slot,
                "level": level,
                "status": label,
                "error": rt.last_error if rt and rt.retry_after and rt.retry_after > now else "",
                "last_used": rt.last_used if rt else None,
                "busy": busy.get(ai_model.pk, 0),
                "indexing": ai_model.pk in indexing,
                "payload": payload,
                "too_big": bool(
                    usable is not None and total is not None and total * vram.GB > usable
                ),
                "choices": context_choices(entry.max_context_length if entry else None),
                "profile_context": ai_model.load_context_length,
            }
        )
    if snap is not None:
        data["foreign"] = [s for s in snap.slots if s.ai_model is None]
        if snap.budget:
            data["bar"] = _bar(snap)
        data["snap"] = snap
        data["used"] = _gb(snap.used)
        data["free"] = _gb(max(0, snap.free))
        data["budget"] = _gb(snap.budget)
        data["reserve"] = _gb(snap.reserve)
    return data


def _bar(snap: model_manager.Snapshot) -> list[dict]:
    """Segmente des Budgetbalkens in Prozent: Modelle, frei, Reserve."""
    total = max(snap.budget, snap.used + snap.reserve, 1)
    segments = [
        {
            "label": f"{s.label}: {_gb(s.bytes)}",
            "pct": round(100 * s.bytes / total, 2),
            "kind": "pinned" if s.pinned else ("foreign" if s.ai_model is None else "model"),
        }
        for s in snap.slots
    ]
    free = max(0, snap.free)
    segments.append(
        {"label": f"frei: {_gb(free)}", "pct": round(100 * free / total, 2), "kind": "free"}
    )
    segments.append(
        {
            "label": f"Reserve: {_gb(snap.reserve)}",
            "pct": round(100 * snap.reserve / total, 2),
            "kind": "reserve",
        }
    )
    return segments


def page_context() -> list[dict]:
    return [provider_context(p) for p in model_manager.local_providers() if p.active]


def page_action(request) -> tuple[str, str]:
    """POST-Aktion der Seite; Rückgabe (Stufe ``ok``/``error``, Meldung)."""
    action = request.POST.get("action", "")
    model_pk = request.POST.get("model", "")
    ai_model = (
        AIModel.objects.select_related("provider").filter(pk=int(model_pk)).first()
        if model_pk.isdigit()
        else None
    )
    if action in ("pin", "unpin", "load", "profile") and (
        ai_model is None or not ai_model.provider.is_local
    ):
        return "error", "Unbekanntes Modell."
    if action in ("pin", "unpin"):
        ai_model.pinned = action == "pin"
        ai_model.save(update_fields=["pinned"])
        return "ok", f"„{ai_model.model_id}“ {'angeheftet' if ai_model.pinned else 'gelöst'}."
    if action == "profile":
        raw = request.POST.get("context", "")
        value = int(raw) if raw.isdigit() else None
        try:
            value = check_context(value, cached_max_context(ai_model))
        except ValidationError as exc:
            return "error", " ".join(exc.messages)
        ai_model.load_context_length = value
        ai_model.save(update_fields=["load_context_length"])
        shown = _num(value) if value else "Vorschlag"
        return (
            "ok",
            f"Kontextfenster für „{ai_model.model_id}“: {shown} (gilt ab dem nächsten Laden).",
        )
    if action == "load":
        if not model_manager.managed(ai_model.provider):
            return (
                "error",
                "Für diesen Anbieter ist „Modelle verwalten“ aus bzw. kein Budget gesetzt.",
            )
        try:
            loaded = model_manager.load_now(ai_model)
        except model_manager.ModelUnavailable as exc:
            return "error", exc.message
        return "ok", (
            f"„{ai_model.model_id}“ geladen."
            if loaded
            else f"„{ai_model.model_id}“ war schon geladen."
        )
    if action == "unload":
        provider_pk = request.POST.get("provider", "")
        provider = (
            Provider.objects.filter(pk=int(provider_pk), is_local=True).first()
            if provider_pk.isdigit()
            else None
        )
        instance = request.POST.get("instance", "")
        if provider is None or not instance:
            return "error", "Unbekannte Instanz."
        try:
            model_manager.unload_now(provider, instance)
        except (model_manager.ModelUnavailable, lmstudio.LmsError) as exc:
            return "error", f"Entladen nicht möglich: {exc.message}"
        return "ok", f"„{instance}“ entladen."
    return "error", "Unbekannte Aktion."
