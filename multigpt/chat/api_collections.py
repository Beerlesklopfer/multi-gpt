"""Sammlungen verwalten (M7, RAG): anlegen, umbenennen, löschen, teilen,
Dokumente löschen. Upload und Statusliste der Dokumente liegen in
api_documents.py (ingest), Suche und Chat-Einbindung bei retrieval.

Endpunkte (Namespace ``chat``), Antworten als JSON, Fehler ``{"error": "<Text>"}``:

- ``GET /api/collections/`` – lesbare Sammlungen (eigene und per Gruppe freigegebene),
- ``POST /api/collections/`` ``{name}`` – neue Sammlung (Recht UPLOAD_DOCUMENTS),
- ``PATCH /api/collections/<pk>/`` ``{name}`` – umbenennen (WRITE),
- ``DELETE /api/collections/<pk>/`` – löschen samt Dokumenten (nur Besitzer),
- ``POST /api/collections/<pk>/shares/`` ``{group, can_write}`` – freigeben/ändern,
- ``DELETE /api/collections/<pk>/shares/`` ``{group}`` – Freigabe entziehen,
- ``DELETE /api/documents/<pk>/`` – Dokument löschen (WRITE auf die Sammlung).

Rechte immer über ``can()``: kein READ -> 404 (fremde Sammlungen bleiben
unsichtbar), READ ohne WRITE -> 403. Teilen und Löschen der Sammlung nur durch
den Besitzer; Teilen zusätzlich nur mit dem Recht SHARE.
"""

from django.db import IntegrityError, transaction
from django.db.models import Count, Q
from django.http import JsonResponse
from django.urls import reverse
from django.views.decorators.http import require_http_methods

from multigpt.accounts.models import UserGroup
from multigpt.accounts.permissions import Action, can

from .api import _error, _json_body, api_login_required
from .models import Collection, Document, Share

NAME_MAX_LENGTH = Collection._meta.get_field("name").max_length

MSG_NOT_FOUND = "Sammlung nicht gefunden."
MSG_READ_ONLY = "Du darfst diese Sammlung nur lesen."
MSG_FROM_SOURCE = (
    "Das Dokument wird aus einem Serververzeichnis eingelesen. Es verschwindet, wenn die "
    "Datei dort entfernt wird oder ein Verwalter die Verzeichnisquelle löscht."
)
MSG_COLLECTION_FROM_SOURCE = (
    "Die Sammlung wird aus einem Serververzeichnis eingelesen. Bitte zuerst einen Verwalter "
    "bitten, die Verzeichnisquelle zu entfernen."
)


def readable_collections(user):
    """Sammlungen mit Lesezugriff: eigene oder per Freigabe an eine Gruppe des Kontos.

    Dieselbe Regel wie ``can(user, READ, collection)``, nur als Abfrage.
    """
    if user is None or not user.is_authenticated or not user.is_active:
        return Collection.objects.none()
    shared = Share.objects.filter(
        collection__isnull=False, group__in=user.groups.values("pk")
    ).values("collection_id")
    return Collection.objects.filter(Q(owner=user) | Q(pk__in=shared))


def writable_collection_ids(user) -> set[int]:
    """IDs der lesbaren Sammlungen, die das Konto auch ändern darf."""
    if user is None or not user.is_authenticated or not user.is_active:
        return set()
    own = set(Collection.objects.filter(owner=user).values_list("pk", flat=True))
    shared = set(
        Share.objects.filter(
            collection__isnull=False, can_write=True, group__in=user.groups.values("pk")
        ).values_list("collection_id", flat=True)
    )
    return own | shared


def with_counts(queryset):
    return queryset.select_related("owner").annotate(
        document_count=Count("documents", distinct=True),
        indexed_count=Count(
            "documents", filter=Q(documents__status=Document.Status.INDEXED), distinct=True
        ),
        pending_count=Count(
            "documents", filter=Q(documents__status=Document.Status.PENDING), distinct=True
        ),
        error_count=Count(
            "documents", filter=Q(documents__status=Document.Status.ERROR), distinct=True
        ),
    )


def owner_name(collection: Collection) -> str:
    owner = collection.owner
    return owner.get_full_name() or owner.get_username()


def access_label(collection: Collection, user, can_write: bool) -> str:
    if collection.owner_id == user.pk:
        return "eigene"
    return "schreibend" if can_write else "lesend"


def serialize_collection(collection: Collection, user, can_write: bool | None = None) -> dict:
    if can_write is None:
        can_write = can(user, Action.WRITE, collection)
    is_owner = collection.owner_id == user.pk
    data = {
        "id": collection.pk,
        "name": collection.name,
        "is_owner": is_owner,
        "owner_name": owner_name(collection),
        "can_write": can_write,
        "access": access_label(collection, user, can_write),
        "url": reverse("chat:collection_detail", args=[collection.pk]),
    }
    for key in ("document_count", "indexed_count", "pending_count", "error_count"):
        if hasattr(collection, key):
            data[key] = getattr(collection, key)
    return data


def document_size(document: Document) -> int | None:
    if document.source_id is not None:
        return document.source_size
    try:
        return document.file.size if document.file else None
    except (OSError, ValueError):
        return None


def serialize_document(document: Document) -> dict:
    """Ein Dokument für die Statusliste (auch vom Upload in api_documents genutzt)."""
    return {
        "id": document.pk,
        "title": document.title,
        "status": document.status,
        "status_label": document.get_status_display(),
        # Bei "error" die Ursache, bei "pending" ggf. der Hinweis auf einen neuen Versuch.
        "error_text": "" if document.status == Document.Status.INDEXED else document.error_text,
        "created": document.created.isoformat() if document.created else None,
        "size": document_size(document),
        "download_url": reverse("chat:document_download", args=[document.pk]),
        # Aus einer Verzeichnisquelle: nicht einzeln löschbar (Datei bzw. Quelle entfernen).
        "from_source": document.source_id is not None,
    }


def serialize_share(share: Share) -> dict:
    return {"group": share.group_id, "group_name": share.group.name, "can_write": share.can_write}


def collection_shares(collection: Collection) -> list[dict]:
    shares = collection.shares.select_related("group").order_by("group__name")
    return [serialize_share(s) for s in shares]


def share_groups():
    """Gruppen, mit denen geteilt werden kann (alle Gruppen der Familie)."""
    return UserGroup.objects.order_by("name")


def _clean_name(raw) -> tuple[str | None, str | None]:
    if not isinstance(raw, str):
        return None, "Ungültiger Name."
    name = " ".join(raw.replace("\x00", " ").split())
    if not name:
        return None, "Der Name darf nicht leer sein."
    if len(name) > NAME_MAX_LENGTH:
        return None, f"Der Name darf höchstens {NAME_MAX_LENGTH} Zeichen lang sein."
    return name, None


MSG_DUPLICATE = "Du hast schon eine Sammlung mit diesem Namen."


def _readable(request, pk) -> Collection | None:
    collection = Collection.objects.select_related("owner").filter(pk=pk).first()
    if collection is None or not can(request.user, Action.READ, collection):
        return None
    return collection


def delete_document_files(documents) -> None:
    """Dateien nach erfolgreichem Commit aus dem Speicher entfernen."""
    files = [(d.file.storage, d.file.name) for d in documents if d.file]

    def _remove():
        for storage, name in files:
            try:
                storage.delete(name)
            except OSError:
                pass  # Bereits weg oder nicht löschbar: der Datensatz ist entfernt.

    transaction.on_commit(_remove)


@require_http_methods(["GET", "POST"])
@api_login_required
def collections(request):
    user = request.user
    if request.method == "GET":
        writable = writable_collection_ids(user)
        items = with_counts(readable_collections(user)).order_by("name", "pk")
        return JsonResponse(
            [serialize_collection(c, user, c.pk in writable) for c in items], safe=False
        )

    if not can(user, Action.UPLOAD_DOCUMENTS):
        return _error("Du darfst keine Sammlungen anlegen.", 403)
    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    name, err = _clean_name(data.get("name"))
    if err:
        return _error(err, 400)
    try:
        with transaction.atomic():
            collection = Collection.objects.create(owner=user, name=name)
    except IntegrityError:
        return _error(MSG_DUPLICATE, 400)
    collection = with_counts(Collection.objects.filter(pk=collection.pk)).get()
    return JsonResponse(serialize_collection(collection, user, True), status=201)


@require_http_methods(["PATCH", "DELETE"])
@api_login_required
def collection_detail(request, pk: int):
    user = request.user
    collection = _readable(request, pk)
    if collection is None:
        return _error(MSG_NOT_FOUND, 404)
    if not can(user, Action.WRITE, collection):
        return _error(MSG_READ_ONLY, 403)

    if request.method == "DELETE":
        if collection.owner_id != user.pk:
            return _error("Nur wer die Sammlung angelegt hat, kann sie löschen.", 403)
        if collection.directory_sources.exists():
            return _error(MSG_COLLECTION_FROM_SOURCE, 403)
        with transaction.atomic():
            delete_document_files(list(collection.documents.all()))
            collection.delete()
        return JsonResponse({"deleted": True, "id": pk})

    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    if "name" not in data:
        return _error("Keine Änderung angegeben.", 400)
    name, err = _clean_name(data["name"])
    if err:
        return _error(err, 400)
    if (
        Collection.objects.filter(owner_id=collection.owner_id, name=name)
        .exclude(pk=collection.pk)
        .exists()
    ):
        return _error("Es gibt schon eine Sammlung mit diesem Namen.", 400)
    try:
        with transaction.atomic():
            Collection.objects.filter(pk=collection.pk).update(name=name)
    except IntegrityError:
        return _error("Es gibt schon eine Sammlung mit diesem Namen.", 400)
    collection = with_counts(Collection.objects.filter(pk=collection.pk)).get()
    return JsonResponse(serialize_collection(collection, user, True))


@require_http_methods(["GET", "POST", "DELETE"])
@api_login_required
def collection_shares_view(request, pk: int):
    user = request.user
    collection = _readable(request, pk)
    if collection is None:
        return _error(MSG_NOT_FOUND, 404)
    if collection.owner_id != user.pk:
        return _error("Nur wer die Sammlung angelegt hat, kann sie teilen.", 403)
    if not can(user, Action.SHARE, collection):
        return _error("Du darfst keine Sammlungen teilen.", 403)
    if request.method == "GET":
        return JsonResponse({"shares": collection_shares(collection)})

    data = _json_body(request)
    if data is None:
        return _error("Ungültige Anfrage.", 400)
    group_id = data.get("group")
    if isinstance(group_id, bool) or not isinstance(group_id, int):
        return _error("Bitte eine Gruppe wählen.", 400)
    group = UserGroup.objects.filter(pk=group_id).first()
    if group is None:
        return _error("Gruppe nicht gefunden.", 400)

    if request.method == "DELETE":
        Share.objects.filter(collection=collection, group=group).delete()
        return JsonResponse({"shares": collection_shares(collection)})

    can_write = data.get("can_write", False)
    if not isinstance(can_write, bool):
        return _error("Ungültiger Wert für „Schreibrecht“.", 400)
    with transaction.atomic():
        Share.objects.update_or_create(
            collection=collection, group=group, defaults={"can_write": can_write}
        )
    return JsonResponse({"shares": collection_shares(collection)})


@require_http_methods(["DELETE"])
@api_login_required
def document_detail(request, pk: int):
    document = Document.objects.select_related("collection").filter(pk=pk).first()
    if document is None or not can(request.user, Action.READ, document.collection):
        return _error("Dokument nicht gefunden.", 404)
    if not can(request.user, Action.WRITE, document.collection):
        return _error(MSG_READ_ONLY, 403)
    if document.source_id is not None:
        return _error(MSG_FROM_SOURCE, 403)
    with transaction.atomic():
        delete_document_files([document])
        document.delete()
    return JsonResponse({"deleted": True, "id": pk})
