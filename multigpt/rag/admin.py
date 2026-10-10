"""Admin-Abschnitt „Dokumente (RAG)“: Übersicht, Dokumente, Sammlungen,
Indexierungsaufträge und Einstellungen.

Privatsphäre (Plan 8f): Auch Verwalter sehen keine fremden Inhalte. Angezeigt
werden nur Metadaten (Titel, Namen, Besitzer, Status, Größen, Zahlen, IDs) –
nie Text von Abschnitten, Dateiinhalte oder Job-Daten jenseits von IDs.
Bearbeiten von Inhalt oder Datei gibt es nicht; Dokumente lassen sich nach
Bestätigung samt Datei löschen.

Alle Aktionen laufen per POST mit CSRF-Token über die ``FamilyAdminSite``
(Zugang nur mit ``can(user, Action.ADMIN)``).
"""

from django.contrib import admin, messages
from django.contrib.admin.utils import unquote
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.db.models import Count, Q
from django.http import Http404, HttpResponseNotAllowed, HttpResponseRedirect
from django.template.defaultfilters import filesizeformat
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html
from django.utils.text import Truncator

from multigpt.chat import status as provider_status
from multigpt.chat.api_collections import delete_document_files
from multigpt.chat.forms_citation import BIB_FIELDS, DocumentCitationForm
from multigpt.chat.models import Chunk, Document, IndexRun, Job, Provider, RagSettings
from multigpt.chat.rag import embeddings as rag_embeddings
from multigpt.chat.rag import jobs as rag_jobs
from multigpt.chat.rag import ocr as rag_ocr

from . import crawl, services
from .models import (
    CollectionProxy,
    DirectorySource,
    DocumentProxy,
    IndexRunProxy,
    JobProxy,
    RagOverview,
    RagSettingsProxy,
)
from .settings_form import RagSettingsForm, materialize
from .source_forms import DirectorySourceForm

ADMIN_CSS = "rag/admin_rag.css"


def _status_badge(value: str, label: str):
    return format_html('<span class="rag-status rag-status--{}">{}</span>', value, label)


def _short(text: str, length: int = 80):
    if not text:
        return "–"
    return format_html('<span title="{}">{}</span>', text, Truncator(text).chars(length))


def _owner_name(user) -> str:
    return user.get_full_name() or user.get_username()


def _documents(count: int) -> str:
    return f"{count} Dokument" if count == 1 else f"{count} Dokumente"


def _jobs(count: int) -> str:
    return f"{count} Auftrag" if count == 1 else f"{count} Aufträge"


def _overview_url() -> str:
    return reverse("admin:rag_ragoverview_changelist")


def _level(level: str) -> int:
    return {"ok": messages.SUCCESS, "warning": messages.WARNING}.get(level, messages.ERROR)


# Hinweis bei laufenden Aufträgen: HTTP-Anfragen (z. B. OCR einer Seite in LM
# Studio, bis zu 600 s) werden nicht hart unterbrochen.
MSG_AFTER_STEP = (
    "{what} wird nach dem aktuellen Schritt beendet (z. B. nach der gerade gelesenen "
    "Seite; eine laufende Anfrage an den Anbieter kann bis zu 10 Minuten dauern)."
)


def _run_link(run_id) -> str:
    if run_id is None:
        return "–"
    url = reverse("admin:rag_indexrunproxy_change", args=[run_id])
    return format_html('<a href="{}">#{}</a>', url, run_id)


def _cancel_messages(admin_obj, request, removed: int, marked: int) -> None:
    if removed:
        admin_obj.message_user(request, f"{_jobs(removed)} abgebrochen und entfernt.")
    if marked:
        admin_obj.message_user(
            request,
            MSG_AFTER_STEP.format(what=f"{_jobs(marked)} läuft gerade und"),
            messages.WARNING,
        )
    if not removed and not marked:
        admin_obj.message_user(request, "Nichts abzubrechen.", messages.INFO)


def _post_only(request):
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    return None


class RagAdminMixin:
    """Gemeinsames: Reihenfolge im Abschnitt und eigenes Stylesheet."""

    index_order = 100

    class Media:
        css = {"all": [ADMIN_CSS]}


# --- Übersicht ----------------------------------------------------------------


@admin.register(RagOverview)
class RagOverviewAdmin(RagAdminMixin, admin.ModelAdmin):
    """Übersichtsseite (eigene Admin-URL über die Liste des Proxys)."""

    index_order = 0

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        view = self.admin_site.admin_view
        return [
            path("", view(self.overview_view), name="rag_ragoverview_changelist"),
            path("reindex-all/", view(self.reindex_all_view), name="rag_overview_reindex_all"),
            path("retry-failed/", view(self.retry_failed_view), name="rag_overview_retry_failed"),
            path("reset-stale/", view(self.reset_stale_view), name="rag_overview_reset_stale"),
            path(
                "check-embedding/",
                view(self.check_embedding_view),
                name="rag_overview_check_embedding",
            ),
            path("check-ocr/", view(self.check_ocr_view), name="rag_overview_check_ocr"),
        ]

    def _require(self, request):
        if not self.has_view_permission(request):
            raise PermissionDenied

    def _context(self, request, title):
        return {
            **self.admin_site.each_context(request),
            "opts": self.opts,
            "title": title,
            "subtitle": None,
            "media": self.media,
        }

    def overview_view(self, request):
        self._require(request)
        cfg = RagSettings.load()
        figure_model = cfg.figure_model if cfg.describe_figures else None
        for model in (cfg.embedding_model, cfg.ocr_model, figure_model):
            if model is not None and model.provider.active and model.provider.check_status:
                provider_status.refresh(model.provider)  # Erreichbarkeit, höchstens alle 15 s
        data = services.overview()
        model = data.settings.embedding_model
        ocr_model = data.settings.ocr_model
        context = {
            **self._context(request, "RAG-Übersicht"),
            "data": data,
            "embedding_model": model,
            "provider": model.provider if model else None,
            "ocr": services.ocr_overview(data.settings),
            "ocr_model": ocr_model,
            "ocr_provider": ocr_model.provider if ocr_model else None,
            "figure_model": data.settings.figure_model,
            "figure_provider": (
                data.settings.figure_model.provider if data.settings.figure_model else None
            ),
            "dimensions": rag_embeddings.EMBEDDING_DIMENSIONS,
            "storage": filesizeformat(data.storage_bytes),
            "stale_minutes": int(services.jobs.STALE_AFTER.total_seconds() // 60),
            "silent_minutes": int(services.WORKER_SILENT_AFTER.total_seconds() // 60),
        }
        return TemplateResponse(request, "admin/rag/ragoverview/overview.html", context)

    def reindex_all_view(self, request):
        self._require(request)
        if request.method == "POST":
            count = services.reindex_all(request.user)
            self.message_user(
                request,
                f"{_documents(count)} zur Indexierung eingereiht. Der Worker arbeitet sie ab.",
                messages.SUCCESS,
            )
            return HttpResponseRedirect(_overview_url())
        context = {
            **self._context(request, "Alles neu indexieren"),
            "document_count": Document.objects.count(),
            "chunk_count": Chunk.objects.count(),
            "embedding_model": RagSettings.load().embedding_model,
        }
        return TemplateResponse(request, "admin/rag/ragoverview/reindex_all.html", context)

    def retry_failed_view(self, request):
        self._require(request)
        if denied := _post_only(request):
            return denied
        count = services.retry_failed()
        self.message_user(
            request,
            f"{_jobs(count)} erneut eingereiht." if count else "Nichts fehlgeschlagen.",
            messages.SUCCESS if count else messages.INFO,
        )
        return HttpResponseRedirect(_overview_url())

    def reset_stale_view(self, request):
        self._require(request)
        if denied := _post_only(request):
            return denied
        count = services.reset_stale()
        self.message_user(
            request,
            f"{_jobs(count)} zurückgesetzt." if count else "Keine hängenden Aufträge.",
            messages.SUCCESS if count else messages.INFO,
        )
        return HttpResponseRedirect(_overview_url())

    def check_embedding_view(self, request):
        self._require(request)
        if denied := _post_only(request):
            return denied
        level, text = rag_embeddings.check()
        self.message_user(request, text, messages.SUCCESS if level == "ok" else messages.ERROR)
        return HttpResponseRedirect(_overview_url())

    def check_ocr_view(self, request):
        self._require(request)
        if denied := _post_only(request):
            return denied
        level, text = rag_ocr.check()
        self.message_user(request, text, _level(level))
        return HttpResponseRedirect(_overview_url())


# --- Einstellungen ------------------------------------------------------------


def _settings_url() -> str:
    return reverse("admin:rag_ragsettingsproxy_change", args=[RagSettings.SINGLETON_PK])


@admin.register(RagSettingsProxy)
class RagSettingsAdmin(RagAdminMixin, admin.ModelAdmin):
    """Genau ein Datensatz.

    „Speichern und Embedding testen“ / „Speichern und OCR testen“ sind
    Absende-Knöpfe des Formulars: Sie speichern (mit Prüfung) und testen dann
    genau die angezeigten, jetzt gespeicherten Werte – kein Test alter Werte.
    """

    TEST_EMBEDDING = "_save_and_test_embedding"
    TEST_OCR = "_save_and_test_ocr"

    class Media:
        css = {"all": [ADMIN_CSS]}
        js = ["rag/settings_form.js"]

    index_order = 40
    form = RagSettingsForm
    fieldsets = [
        (
            None,
            {
                "fields": ["embedding_model", "document_prefix", "query_prefix"],
                "description": "Nach dem Speichern über „Embedding testen“ (oben rechts) "
                "prüfen. Ein Wechsel des Modells oder der Präfixe erfordert „Alles neu "
                "indexieren“ (oben rechts), sonst passen die gespeicherten Abschnitte nicht "
                "zur Frage. Die Datenbank speichert Vektoren mit "
                f"{rag_embeddings.EMBEDDING_DIMENSIONS} Dimensionen.",
            },
        ),
        (
            "Texterkennung (OCR)",
            {
                "fields": ["ocr_backend", "ocr_model", "ocr_fallback_tesseract"],
                "description": "Für gescannte PDF-Seiten ohne Textebene. olmOCR liest "
                "Tabellen, Spalten und Formeln besser als Tesseract, braucht aber ein "
                "geladenes Vision-Modell (z. B. allenai/olmocr-2-7b in LM Studio). Nach dem "
                "Speichern über „OCR testen“ (oben rechts) prüfen.",
            },
        ),
        (
            "Abbildungen",
            {
                "fields": [
                    "describe_figures",
                    "figure_model",
                    "figure_max_per_document",
                    "figure_max_per_page",
                    "figure_min_edge",
                    "figure_max_edge",
                ],
                "description": "Bilder und Diagramme in PDF- und Word-Dokumenten sowie "
                "Bilddateien (JPG, PNG, TIFF, WEBP) beschreibt ein allgemeines Vision-Modell, "
                "z. B. Qwen3-VL in LM Studio; die Beschreibung steht als „[Abbildung: …]“ im "
                "Text und wird mit durchsucht. Je Abbildung ein Modellaufruf: Das dauert "
                "(lokal einige Sekunden je Bild) bzw. kostet bei Cloud-Anbietern Gebühren. "
                "Mit einem lokalen Modell bleiben die Bilder im Haus, mit einem Cloud-Modell "
                "gehen sie an den Anbieter. Kleine Bilder, schmale Linien und Wiederholungen "
                "(Logos) werden übersprungen. Wirkt für vorhandene Dokumente erst nach „Alles "
                "neu indexieren“.",
            },
        ),
        (
            "Zerteilung",
            {
                "fields": ["chunk_tokens", "overlap_tokens"],
                "description": "Wirkt erst nach „Alles neu indexieren“.",
            },
        ),
        ("Suche", {"fields": ["top_k", "hybrid"]}),
        (
            "Literaturangaben",
            {
                "fields": ["crossref_enabled", "crossref_mailto"],
                "description": "Erkennt die Indexierung eine DOI (Verlags-PDFs, z. B. "
                "Springer), kann sie fehlende Angaben bei Crossref nachschlagen. Aus "
                "Datenschutzgründen standardmäßig aus: Crossref erfährt dabei, welche "
                "Dokumente hier liegen.",
            },
        ),
    ]

    def has_add_permission(self, request):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def change_view(self, request, object_id, form_url="", extra_context=None):
        if request.method == "GET":
            # Gemeldete Modelle auffrischen (Statusprüfung, höchstens alle 15 s je
            # Anbieter), damit gerade in LM Studio geladene Modelle erscheinen.
            for provider in Provider.objects.filter(active=True, check_status=True):
                provider_status.refresh(provider)
        return super().change_view(request, object_id, form_url, extra_context)

    def changelist_view(self, request, extra_context=None):
        if not self.has_view_or_change_permission(request):
            raise PermissionDenied
        RagSettings.load()
        return HttpResponseRedirect(_settings_url())

    def get_object(self, request, object_id, from_field=None):
        # Fehlt der Datensatz noch (frische Installation), wird er angelegt.
        if str(object_id) == str(RagSettings.SINGLETON_PK):
            RagSettings.load()
        return super().get_object(request, object_id, from_field)

    def save_model(self, request, obj, form, change):
        # Neu gewählte, gemeldete Modelle anlegen (``RagSettingsForm``).
        for field in ("embedding_model", "ocr_model", "figure_model"):
            model = getattr(obj, field)
            if model is not None and model.pk is None:
                setattr(obj, field, materialize(model))
                self.message_user(
                    request,
                    f"Das Modell „{getattr(obj, field).model_id}“ wurde angelegt.",
                    messages.SUCCESS,
                )
        previous = (
            RagSettings.objects.filter(pk=obj.pk).values_list("ocr_model_id", flat=True).first()
        )
        super().save_model(request, obj, form, change)
        if (
            obj.ocr_model_id
            and obj.ocr_backend == RagSettings.OcrBackend.TESSERACT
            and ("ocr_model" in form.changed_data or previous != obj.ocr_model_id)
        ):
            self.message_user(
                request,
                "Ein OCR-Modell ist gewählt, das OCR-Verfahren steht aber auf „Tesseract“. "
                "Das Modell wird erst mit dem Verfahren „olmOCR“ genutzt.",
                messages.WARNING,
            )
        changed = set(form.changed_data) & {
            "embedding_model",
            "document_prefix",
            "query_prefix",
            "chunk_tokens",
            "overlap_tokens",
            "describe_figures",
            "figure_model",
        }
        if change and changed and Document.objects.exists():
            self.message_user(
                request,
                format_html(
                    'Modell, Zerteilung oder Abbildungen wurden geändert. Bitte <a href="{}">'
                    "alles neu indexieren</a>, damit alle Dokumente neu eingebettet werden.",
                    reverse("admin:rag_overview_reindex_all"),
                ),
                messages.WARNING,
            )

    def response_change(self, request, obj):
        tests = {self.TEST_EMBEDDING: rag_embeddings.check, self.TEST_OCR: rag_ocr.check}
        for key, check in tests.items():
            if key in request.POST:
                level, text = check()
                self.message_user(request, text, _level(level))
                return HttpResponseRedirect(_settings_url())
        return super().response_change(request, obj)

    def get_urls(self):
        return [
            path(
                "<path:object_id>/check/",
                self.admin_site.admin_view(self.check_view),
                name="rag_ragsettingsproxy_check",
            ),
            path(
                "<path:object_id>/check-ocr/",
                self.admin_site.admin_view(self.check_ocr_view),
                name="rag_ragsettingsproxy_check_ocr",
            ),
            *super().get_urls(),
        ]

    def check_view(self, request, object_id):
        if denied := _post_only(request):
            return denied
        if not self.has_change_permission(request):
            raise PermissionDenied
        level, text = rag_embeddings.check()
        self.message_user(request, text, messages.SUCCESS if level == "ok" else messages.ERROR)
        return HttpResponseRedirect(_settings_url())

    def check_ocr_view(self, request, object_id):
        if denied := _post_only(request):
            return denied
        if not self.has_change_permission(request):
            raise PermissionDenied
        level, text = rag_ocr.check()
        self.message_user(request, text, _level(level))
        return HttpResponseRedirect(_settings_url())


# --- Dokumente ----------------------------------------------------------------


class ReindexObjectMixin:
    """Knopf „Neu indexieren“ auf der Detailseite (POST ``<pk>/reindex/``)."""

    def _documents_for(self, obj):
        raise NotImplementedError

    def _reindex(self, request, obj) -> int:
        return services.reindex_documents(self._documents_for(obj))

    def get_urls(self):
        info = self.opts.app_label, self.opts.model_name
        return [
            path(
                "<path:object_id>/reindex/",
                self.admin_site.admin_view(self.reindex_view),
                name="{}_{}_reindex".format(*info),
            ),
            *super().get_urls(),
        ]

    def reindex_view(self, request, object_id):
        if denied := _post_only(request):
            return denied
        if not self.has_view_permission(request):
            raise PermissionDenied
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404("Nicht gefunden.")
        count = self._reindex(request, obj)
        self.message_user(request, f"{_documents(count)} zur Indexierung eingereiht.")
        info = self.opts.app_label, self.opts.model_name
        return HttpResponseRedirect(reverse("admin:{}_{}_change".format(*info), args=[obj.pk]))


@admin.register(DocumentProxy)
class DocumentAdmin(RagAdminMixin, ReindexObjectMixin, admin.ModelAdmin):
    index_order = 10
    list_display = [
        "title",
        "collection_name",
        "owner",
        "status_badge",
        "error_short",
        "size",
        "chunk_count",
        "created",
    ]
    list_display_links = ["title"]
    list_filter = [
        "status",
        ("collection", admin.RelatedOnlyFieldListFilter),
        ("collection__owner", admin.RelatedOnlyFieldListFilter),
    ]
    search_fields = ["title", "collection__name"]
    actions = ["reindex_action", "retry_action", "delete_selected"]
    readonly_fields = [
        "id",
        "title",
        "collection_name",
        "owner",
        "status_badge",
        "error_text",
        "size",
        "chunk_count",
        "figures_described",
        "created",
    ]
    # Literaturangaben fürs Zitieren sind bearbeitbar (ragcite), alles andere nicht.
    form = DocumentCitationForm
    fieldsets = [
        (None, {"fields": readonly_fields}),
        (
            "Literaturangaben",
            {
                "fields": BIB_FIELDS,
                "description": "Für Quellenangaben im Chat (Zitierstil je Konto). Beim "
                "Indexieren aus Datei-Metadaten, DOI und Normnummer vorbelegt; nach dem "
                "Speichern hier überschreibt die Indexierung nichts mehr.",
            },
        ),
    ]

    def _documents_for(self, obj):
        return [obj]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("collection__owner")
            .annotate(chunk_total=Count("chunks"))
        )

    def has_add_permission(self, request):
        return False

    @admin.display(description="Sammlung", ordering="collection__name")
    def collection_name(self, obj):
        return obj.collection.name

    @admin.display(description="Besitzer", ordering="collection__owner__username")
    def owner(self, obj):
        return _owner_name(obj.collection.owner)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        return _status_badge(obj.status, obj.get_status_display())

    @admin.display(description="Fehler")
    def error_short(self, obj):
        return _short("" if obj.status == Document.Status.INDEXED else obj.error_text)

    @admin.display(description="Größe")
    def size(self, obj):
        value = services.file_size(obj)
        return "Datei fehlt" if value is None else filesizeformat(value)

    @admin.display(description="Abschnitte", ordering="chunk_total")
    def chunk_count(self, obj):
        return getattr(obj, "chunk_total", None) or obj.chunks.count()

    @admin.action(description="Neu indexieren")
    def reindex_action(self, request, queryset):
        count = services.reindex_documents(list(queryset))
        self.message_user(request, f"{_documents(count)} zur Indexierung eingereiht.")

    @admin.action(description="Erneut versuchen (nur mit Fehler)")
    def retry_action(self, request, queryset):
        count = services.retry_errors(list(queryset))
        if count:
            self.message_user(request, f"{_documents(count)} erneut eingereiht.")
        else:
            self.message_user(request, "Keines der Dokumente hat einen Fehler.", messages.INFO)

    # Löschen: nur nach Djangos Bestätigungsseite, Datei nach dem Commit entfernen.

    def delete_model(self, request, obj):
        rag_jobs.cancel_document_jobs([obj.pk])  # laufende Indexierung abbrechen
        with transaction.atomic():
            delete_document_files([obj])
            obj.delete()

    def delete_queryset(self, request, queryset):
        rag_jobs.cancel_document_jobs(list(queryset.values_list("pk", flat=True)))
        with transaction.atomic():
            documents = list(queryset)
            delete_document_files(documents)
            queryset.model.objects.filter(pk__in=[d.pk for d in documents]).delete()

    def get_deleted_objects(self, objs, request):
        # Nur Titel und Zahlen – keine Liste der Abschnitte (Inhalt, Seitenlänge).
        objs = list(objs)
        chunks = Chunk.objects.filter(document__in=[o.pk for o in objs]).count()
        to_delete = [f"Dokument: {o.title} (mit Datei)" for o in objs]
        model_count = {"Dokumente": len(objs)}
        if chunks:
            model_count["Abschnitte"] = chunks
        return to_delete, model_count, set(), []


# --- Sammlungen -----------------------------------------------------------------


@admin.register(CollectionProxy)
class CollectionAdmin(RagAdminMixin, ReindexObjectMixin, admin.ModelAdmin):
    index_order = 20
    list_display = [
        "name",
        "owner_name",
        "document_count",
        "error_count",
        "chunk_count",
        "shares_display",
        "created",
    ]
    list_filter = [("owner", admin.RelatedOnlyFieldListFilter)]
    search_fields = ["name"]
    actions = ["reindex_action"]
    fields = [
        "id",
        "name",
        "owner_name",
        "document_count",
        "status_counts",
        "chunk_count",
        "shares_display",
        "created",
    ]
    readonly_fields = fields

    def _documents_for(self, obj):
        return list(obj.documents.order_by("pk"))

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("owner")
            .prefetch_related("shares__group")
            .annotate(
                document_total=Count("documents", distinct=True),
                error_total=Count(
                    "documents",
                    filter=Q(documents__status=Document.Status.ERROR),
                    distinct=True,
                ),
                chunk_total=Count("documents__chunks", distinct=True),
            )
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    @admin.display(description="Besitzer", ordering="owner__username")
    def owner_name(self, obj):
        return _owner_name(obj.owner)

    @admin.display(description="Dokumente", ordering="document_total")
    def document_count(self, obj):
        return obj.document_total

    @admin.display(description="mit Fehler", ordering="error_total")
    def error_count(self, obj):
        return obj.error_total or "–"

    @admin.display(description="Abschnitte", ordering="chunk_total")
    def chunk_count(self, obj):
        return obj.chunk_total

    @admin.display(description="Dokumente nach Status")
    def status_counts(self, obj):
        counts = dict(
            obj.documents.order_by()
            .values_list("status")
            .annotate(n=Count("pk"))
            .values_list("status", "n")
        )
        return ", ".join(
            f"{label}: {counts.get(value, 0)}" for value, label in Document.Status.choices
        )

    @admin.display(description="Freigaben")
    def shares_display(self, obj):
        shares = sorted(obj.shares.all(), key=lambda s: s.group.name)
        if not shares:
            return "privat"
        return ", ".join(
            f"{s.group.name} ({'schreibend' if s.can_write else 'lesend'})" for s in shares
        )

    def _reindex(self, request, obj) -> int:
        return services.reindex_collection(obj, request.user)

    @admin.action(description="Neu indexieren")
    def reindex_action(self, request, queryset):
        count = sum(services.reindex_collection(c, request.user) for c in queryset)
        self.message_user(request, f"{_documents(count)} zur Indexierung eingereiht.")


# --- Indexierungsaufträge -------------------------------------------------------


@admin.register(JobProxy)
class JobAdmin(RagAdminMixin, admin.ModelAdmin):
    index_order = 30
    list_display = [
        "id",
        "kind",
        "status_badge",
        "attempts",
        "run_after",
        "locked_at",
        "error_short",
        "document_ref",
        "run_ref",
        "created",
    ]
    list_filter = ["status", "kind", "cancel_requested"]
    actions = ["retry_action", "cancel_action"]
    fields = [
        "id",
        "kind",
        "status_badge",
        "attempts",
        "run_after",
        "locked_at",
        "last_error",
        "document_ref",
        "run_ref",
        "created",
    ]
    readonly_fields = fields

    def lookup_allowed(self, lookup, value, request=None):
        # Link „offene Aufträge“ eines Laufs (?run__id__exact=…).
        return lookup in ("run__id__exact", "run__id") or super().lookup_allowed(
            lookup, value, request
        )

    def get_queryset(self, request):
        # Neueste zuerst; die Liste ist vor allem zur Fehlersuche da.
        return super().get_queryset(request).order_by("-pk")

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        # Löschen nur über „Abbrechen“ (laufende werden nach dem Schritt beendet).
        return False

    def get_urls(self):
        return [
            path(
                "<path:object_id>/cancel/",
                self.admin_site.admin_view(self.cancel_view),
                name="rag_jobproxy_cancel",
            ),
            *super().get_urls(),
        ]

    def cancel_view(self, request, object_id):
        if denied := _post_only(request):
            return denied
        if not self.has_view_permission(request):
            raise PermissionDenied
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404("Nicht gefunden.")
        removed, marked = services.cancel_jobs(Job.objects.filter(pk=obj.pk))
        _cancel_messages(self, request, removed, marked)
        if marked:
            return HttpResponseRedirect(reverse("admin:rag_jobproxy_change", args=[obj.pk]))
        return HttpResponseRedirect(reverse("admin:rag_jobproxy_changelist"))

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        label = obj.get_status_display()
        if obj.cancel_requested and obj.status == Job.Status.RUNNING:
            label = "wird abgebrochen"
        if services.is_stale(obj):
            label += " (hängt?)"
        return _status_badge(obj.status, label)

    @admin.display(description="Lauf", ordering="run")
    def run_ref(self, obj):
        return _run_link(obj.run_id)

    @admin.display(description="letzter Fehler")
    def error_short(self, obj):
        return _short(obj.last_error, 60)

    @admin.display(description="Dokument-Nr.")
    def document_ref(self, obj):
        doc_id = services.document_id_of(obj)
        if doc_id is None:
            return "–"
        url = reverse("admin:rag_documentproxy_change", args=[doc_id])
        return format_html('<a href="{}">{}</a>', url, doc_id)

    @admin.action(description="Erneut versuchen")
    def retry_action(self, request, queryset):
        selected = list(queryset)
        count = sum(services.retry_job(job) for job in selected)
        skipped = len(selected) - count
        if count:
            self.message_user(request, f"{_jobs(count)} erneut eingereiht.")
        if skipped > 0:
            self.message_user(
                request,
                "Laufende und erledigte Aufträge werden nicht neu gestartet.",
                messages.INFO,
            )

    @admin.action(description="Abbrechen (laufende nach dem aktuellen Schritt)")
    def cancel_action(self, request, queryset):
        removed, marked = services.cancel_jobs(queryset)
        _cancel_messages(self, request, removed, marked)


# --- Läufe ------------------------------------------------------------------------


@admin.register(IndexRunProxy)
class IndexRunAdmin(RagAdminMixin, admin.ModelAdmin):
    """Läufe (Verzeichnis einlesen, Neuindexierung, Upload) mit Fortschritt."""

    index_order = 28
    list_display = [
        "id",
        "kind",
        "status_badge",
        "collection_name",
        "progress",
        "started_by_name",
        "started",
        "duration_display",
    ]
    list_filter = ["status", "kind"]
    actions = ["cancel_action"]
    fields = [
        "id",
        "kind",
        "status_badge",
        "collection_name",
        "source_ref",
        "started_by_name",
        "started",
        "finished",
        "duration_display",
        "progress",
        "files_found",
        "files_checked",
        "files_new",
        "files_changed",
        "files_deleted",
        "files_skipped",
        "docs_queued",
        "docs_done",
        "docs_failed",
        "docs_cancelled",
        "open_jobs",
        "error_text",
    ]
    readonly_fields = fields

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("collection", "source", "started_by")
            .order_by("-pk")
        )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False

    def get_urls(self):
        return [
            path(
                "<path:object_id>/cancel/",
                self.admin_site.admin_view(self.cancel_view),
                name="rag_indexrunproxy_cancel",
            ),
            *super().get_urls(),
        ]

    def cancel_view(self, request, object_id):
        if denied := _post_only(request):
            return denied
        if not self.has_view_permission(request):
            raise PermissionDenied
        obj = self.get_object(request, unquote(object_id))
        if obj is None:
            raise Http404("Nicht gefunden.")
        self._cancel(request, IndexRun.objects.filter(pk=obj.pk))
        return HttpResponseRedirect(reverse("admin:rag_indexrunproxy_change", args=[obj.pk]))

    def _cancel(self, request, queryset):
        runs, removed, marked = services.cancel_runs(queryset)
        if not runs:
            self.message_user(request, "Kein offener Lauf ausgewählt.", messages.INFO)
            return
        text = f"{runs} Lauf/Läufe abgebrochen, {_jobs(removed)} entfernt."
        self.message_user(request, text)
        if marked:
            self.message_user(
                request,
                MSG_AFTER_STEP.format(what=f"{_jobs(marked)} läuft gerade und"),
                messages.WARNING,
            )

    @admin.action(description="Lauf abbrechen")
    def cancel_action(self, request, queryset):
        self._cancel(request, queryset)

    @admin.display(description="Status", ordering="status")
    def status_badge(self, obj):
        css = {
            IndexRun.Status.RUNNING: "running",
            IndexRun.Status.CANCELLING: "pending",
            IndexRun.Status.FINISHED: "done",
            IndexRun.Status.FAILED: "failed",
        }.get(obj.status, "")
        return _status_badge(css, obj.get_status_display())

    @admin.display(description="Sammlung", ordering="collection__name")
    def collection_name(self, obj):
        return obj.collection.name if obj.collection else "–"

    @admin.display(description="Verzeichnisquelle")
    def source_ref(self, obj):
        if obj.source_id is None:
            return "–"
        url = reverse("admin:rag_directorysource_change", args=[obj.source_id])
        return format_html('<a href="{}">#{}</a>', url, obj.source_id)

    @admin.display(description="gestartet von")
    def started_by_name(self, obj):
        return _owner_name(obj.started_by) if obj.started_by else "automatisch"

    @admin.display(description="Laufzeit")
    def duration_display(self, obj):
        return obj.duration_text()

    @admin.display(description="Fortschritt")
    def progress(self, obj):
        return obj.progress_text()

    @admin.display(description="offene Aufträge")
    def open_jobs(self, obj):
        count = obj.jobs.filter(status__in=rag_jobs.OPEN).count()
        if not count:
            return "0"
        url = reverse("admin:rag_jobproxy_changelist")
        return format_html('<a href="{}?run__id__exact={}">{}</a>', url, obj.pk, count)


# --- Verzeichnisquellen (Agent crawler) -------------------------------------------


def _source_result(result: dict) -> str:
    if not result:
        return "–"
    text = (
        f"{result.get('new', 0)} neu, {result.get('changed', 0)} geändert, "
        f"{result.get('unchanged', 0)} unverändert, {result.get('deleted', 0)} entfernt"
    )
    if result.get("skipped"):
        text += f", {result['skipped']} übersprungen"
    if result.get("errors"):
        text += f", {result['errors']} Fehler"
    return text


@admin.register(DirectorySource)
class DirectorySourceAdmin(RagAdminMixin, admin.ModelAdmin):
    """Verzeichnisse auf dem Server/NAS, die periodisch in eine Sammlung
    eingelesen werden. Nur Verwalter (FamilyAdminSite); Pfade müssen unter
    ``RAG_SOURCE_ROOTS`` liegen."""

    index_order = 25
    form = DirectorySourceForm
    list_display = [
        "collection_name",
        "owner",
        "path",
        "state_badge",
        "interval_minutes",
        "last_scan_finished",
        "result_display",
        "error_short",
        "document_count",
    ]
    list_display_links = ["collection_name"]
    list_filter = ["active"]
    search_fields = ["collection__name", "path"]
    actions = [
        "scan_now_action",
        "cancel_scan_action",
        "pause_action",
        "activate_action",
        "delete_selected",
    ]
    readonly_fields = [
        "collection_name",
        "owner",
        "path",
        "state_badge",
        "run_progress",
        "last_scan_started",
        "last_scan_finished",
        "result_display",
        "last_error",
        "document_count",
    ]

    def get_queryset(self, request):
        return (
            super()
            .get_queryset(request)
            .select_related("collection__owner")
            .annotate(document_total=Count("documents"))
        )

    def get_fieldsets(self, request, obj=None):
        settings_fields = [
            "recursive",
            "include_patterns",
            "exclude_patterns",
            "interval_minutes",
            "active",
        ]
        if obj is None:
            return [
                (
                    "Sammlung",
                    {
                        "fields": ["collection", "new_collection_name", "new_collection_owner"],
                        "description": "Eine vorhandene Sammlung wählen oder eine neue mit "
                        "Besitzer anlegen.",
                    },
                ),
                ("Verzeichnis", {"fields": ["path", *settings_fields]}),
            ]
        return [
            (None, {"fields": ["collection_name", "owner", "path"]}),
            ("Einstellungen", {"fields": settings_fields}),
            (
                "Letzter Lauf",
                {
                    "fields": [
                        "state_badge",
                        "run_progress",
                        "last_scan_started",
                        "last_scan_finished",
                        "result_display",
                        "last_error",
                        "document_count",
                    ]
                },
            ),
        ]

    def get_readonly_fields(self, request, obj=None):
        return self.readonly_fields if obj is not None else []

    def get_form(self, request, obj=None, change=False, **kwargs):
        if obj is not None:
            # Pfad und Sammlung sind nach der Anlage fest.
            kwargs["fields"] = [
                "recursive",
                "include_patterns",
                "exclude_patterns",
                "interval_minutes",
                "active",
            ]
        return super().get_form(request, obj, change=change, **kwargs)

    def _roots_warning(self, request):
        if not services.source_paths.enabled():
            self.message_user(
                request,
                "Verzeichnisquellen sind ausgeschaltet: RAG_SOURCE_ROOTS ist leer. Bitte in "
                "/etc/multi-gpt/.env die erlaubten Wurzeln eintragen (Komma-Liste absoluter "
                "Pfade) und Dienst sowie Worker neu starten.",
                messages.WARNING,
            )

    def changelist_view(self, request, extra_context=None):
        if request.method == "GET":
            self._roots_warning(request)
        return super().changelist_view(request, extra_context)

    def add_view(self, request, form_url="", extra_context=None):
        if request.method == "GET":
            self._roots_warning(request)
        return super().add_view(request, form_url, extra_context)

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        if not change and obj.active:
            crawl.enqueue_scan(obj, request.user)
            self.message_user(request, "Das Verzeichnis wird jetzt im Hintergrund eingelesen.")

    # Anzeige

    @admin.display(description="Sammlung", ordering="collection__name")
    def collection_name(self, obj):
        return obj.collection.name

    @admin.display(description="Besitzer", ordering="collection__owner__username")
    def owner(self, obj):
        return _owner_name(obj.collection.owner)

    @admin.display(description="Status")
    def state_badge(self, obj):
        run = crawl.open_run(obj)
        if run is not None:
            if run.status == IndexRun.Status.CANCELLING:
                return _status_badge("pending", "wird abgebrochen")
            return _status_badge("running", "wird eingelesen")
        if crawl.open_scan_job(obj) is not None:
            return _status_badge("running", "wird eingelesen")
        if obj.last_error:
            return _status_badge("error", "Fehler" if obj.active else "pausiert, Fehler")
        if not obj.active:
            return _status_badge("pending", "pausiert")
        return _status_badge("done", "aktiv")

    @admin.display(description="Aktueller Lauf")
    def run_progress(self, obj):
        run = crawl.open_run(obj) or obj.runs.order_by("-pk").first()
        if run is None:
            return "–"
        url = reverse("admin:rag_indexrunproxy_change", args=[run.pk])
        return format_html(
            '<a href="{}">{}</a> – {}', url, run.progress_text(), run.get_status_display()
        )

    @admin.display(description="Ergebnis")
    def result_display(self, obj):
        return _source_result(obj.last_result or {})

    @admin.display(description="Fehler")
    def error_short(self, obj):
        return _short(obj.last_error, 60)

    @admin.display(description="Dokumente", ordering="document_total")
    def document_count(self, obj):
        total = getattr(obj, "document_total", None)
        return total if total is not None else obj.documents.count()

    # Aktionen

    @admin.action(description="Jetzt einlesen")
    def scan_now_action(self, request, queryset):
        if not services.source_paths.enabled():
            self._roots_warning(request)
            return
        created = sum(crawl.enqueue_scan(source, request.user) is not None for source in queryset)
        skipped = queryset.count() - created
        if created:
            self.message_user(request, f"{created} Verzeichnis(se) zum Einlesen eingereiht.")
        if skipped:
            self.message_user(
                request, f"{skipped} Verzeichnis(se) werden bereits eingelesen.", messages.INFO
            )

    @admin.action(description="Einlesen abbrechen")
    def cancel_scan_action(self, request, queryset):
        runs = removed = marked = 0
        for source in queryset:
            r, rem, mark = crawl.cancel_scans(source)
            runs, removed, marked = runs + r, removed + rem, marked + mark
        if not (runs or removed or marked):
            self.message_user(request, "Es wird gerade nichts eingelesen.", messages.INFO)
            return
        self.message_user(
            request,
            f"Einlesen abgebrochen ({runs} Lauf/Läufe, {_jobs(removed)} entfernt). Bereits "
            "indexierte Dokumente bleiben; nichts wird als verschwunden gelöscht.",
        )
        if marked:
            self.message_user(
                request,
                MSG_AFTER_STEP.format(what=f"{_jobs(marked)} läuft gerade und"),
                messages.WARNING,
            )

    @admin.action(description="Pausieren")
    def pause_action(self, request, queryset):
        count = queryset.update(active=False)
        self.message_user(request, f"{count} Verzeichnisquelle(n) pausiert.")

    @admin.action(description="Aktivieren")
    def activate_action(self, request, queryset):
        count = queryset.update(active=True)
        self.message_user(request, f"{count} Verzeichnisquelle(n) aktiviert.")

    def get_deleted_objects(self, objs, request):
        # Nur Zahlen: Die Dokumente der Quelle (samt Abschnitten) werden entfernt,
        # die Dateien auf dem Server bleiben unberührt.
        objs = list(objs)
        documents = Document.objects.filter(source__in=[o.pk for o in objs]).count()
        to_delete = [
            f"Verzeichnisquelle der Sammlung „{o.collection.name}“ (Dateien auf dem Server "
            "bleiben erhalten)"
            for o in objs
        ]
        model_count = {"Verzeichnisquellen": len(objs)}
        if documents:
            model_count["Dokumente aus der Quelle"] = documents
        return to_delete, model_count, set(), []
