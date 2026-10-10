"""Dokumentsuche über Sammlungen (Plan 8b, M7-05).

``search(user, query, collection_ids, top_k)`` liefert die besten Abschnitte
als ``Hit``.

**Zugriff (Plan 8b, 9):** Der Filter steht *in der SQL-Abfrage selbst*:
Abschnitte nur aus Sammlungen, die dem Nutzer gehören oder per ``Share`` an
eine seiner Gruppen freigegeben sind (``EXISTS``-Unterabfrage, ausgewertet bei
jeder Abfrage – ein Entzug wirkt sofort). Die übergebenen ``collection_ids``
schränken nur weiter ein; fremde IDs fallen einfach heraus. Gesperrte oder
anonyme Konten bekommen nichts.

**Ranking:** Vektorsuche (Kosinus-Abstand, HNSW) und – wenn
``RagSettings.hybrid`` – Volltextsuche (``to_tsvector('german')``, ODER-Suche
über die Wörter der Frage, ``ts_rank_cd``). Beide Listen werden per
*Reciprocal Rank Fusion* zusammengeführt: score = Σ 1/(60 + Rang). RRF
braucht keine Normierung der unvergleichbaren Werte (Abstand vs. Rang) und
ist robust; k=60 ist der übliche Wert (Cormack et al. 2009). Ohne Hybrid ist
der score die Kosinus-Ähnlichkeit (1 − Abstand).

**HNSW mit Filter:** Der Index liefert zuerst Kandidaten, der WHERE-Filter
greift danach. Damit gefilterte Abfragen nicht zu wenige Treffer liefern,
wird ``hnsw.iterative_scan`` (pgvector ≥ 0.8) für die Abfrage eingeschaltet.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from django.contrib.postgres.search import SearchQuery, SearchRank
from django.db import DatabaseError, connection, transaction
from django.db.models import Exists, F, OuterRef, Q
from pgvector.django import CosineDistance

from .. import citations
from ..models import SEARCH_CONFIG, Chunk, Collection, RagSettings, Share
from .embeddings import embed_query

logger = logging.getLogger(__name__)

RRF_K = 60
# Kandidaten je Liste vor der Zusammenführung.
CANDIDATE_FACTOR = 4
MIN_CANDIDATES = 20
MAX_QUERY_WORDS = 32
MAX_QUERY_CHARS = 2000
_WORD = re.compile(r"\w+", re.UNICODE)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: int
    document_title: str
    collection_id: int
    page: int | None
    text: str
    score: float
    # Fundstelle von–bis und Literaturangaben des Dokuments (citations.Reference).
    page_end: int | None = None
    paragraph: int | None = None
    paragraph_end: int | None = None
    section: str = ""
    section_title: str = ""
    section_end: str = ""
    biblio: dict = field(default_factory=dict)


def _usable(user) -> bool:
    return user is not None and user.is_authenticated and user.is_active


def readable_collections(user):
    """Sammlungen, die ``user`` lesen darf (eigene oder per Gruppe freigegeben).

    Gleiche Regel wie ``can(user, READ, collection)``, aber als QuerySet.
    """
    if not _usable(user):
        return Collection.objects.none()
    shared = Share.objects.filter(
        collection_id=OuterRef("pk"), group_id__in=user.groups.values("pk")
    )
    return Collection.objects.filter(Q(owner_id=user.pk) | Exists(shared))


def accessible_chunks(user, collection_ids=None):
    """Abschnitte mit Zugriffsfilter in SQL; ``collection_ids`` schränkt weiter ein."""
    if not _usable(user):
        return Chunk.objects.none()
    shared = Share.objects.filter(
        collection_id=OuterRef("document__collection_id"),
        group_id__in=user.groups.values("pk"),
    )
    qs = Chunk.objects.filter(Q(document__collection__owner_id=user.pk) | Exists(shared))
    if collection_ids is not None:
        ids = [int(x) for x in collection_ids]
        qs = qs.filter(document__collection_id__in=ids)
    return qs


def text_query(query: str) -> SearchQuery | None:
    """ODER-Verknüpfung der Wörter (deutsche Stammformen, Stoppwörter fallen weg).

    Eine natürliche Frage enthält viele Wörter, die nicht alle im Abschnitt
    stehen; UND (``websearch``/``plain``) fände dann fast nie etwas.
    """
    words = []
    for word in _WORD.findall(query.lower()):
        if word not in words and len(word) > 1:
            words.append(word)
        if len(words) >= MAX_QUERY_WORDS:
            break
    if not words:
        return None
    raw = " | ".join("'" + w.replace("'", "") + "'" for w in words)
    return SearchQuery(raw, config=SEARCH_CONFIG, search_type="raw")


def _enable_iterative_scan():
    """``SET LOCAL hnsw.iterative_scan`` (pgvector ≥ 0.8); ältere Versionen: ignorieren."""
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute("SET LOCAL hnsw.iterative_scan = strict_order")
    except DatabaseError:
        logger.debug("hnsw.iterative_scan nicht verfügbar")


def _vector_ranking(qs, vector, limit: int) -> list[tuple[int, float]]:
    with transaction.atomic():
        if connection.vendor == "postgresql":
            _enable_iterative_scan()
        rows = list(
            qs.annotate(distance=CosineDistance("embedding", vector))
            .order_by("distance", "pk")
            .values_list("pk", "distance")[:limit]
        )
    return [(pk, float(distance)) for pk, distance in rows]


def _text_ranking(qs, query: str, limit: int) -> list[int]:
    tsquery = text_query(query)
    if tsquery is None:
        return []
    return list(
        qs.filter(search_vector=tsquery)
        .annotate(rank=SearchRank(F("search_vector"), tsquery, cover_density=True))
        .order_by("-rank", "pk")
        .values_list("pk", flat=True)[:limit]
    )


def rrf(*rankings: list[int], k: int = RRF_K) -> dict[int, float]:
    """Reciprocal Rank Fusion mehrerer Ranglisten (beste zuerst)."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for position, pk in enumerate(ranking, start=1):
            scores[pk] = scores.get(pk, 0.0) + 1.0 / (k + position)
    return scores


def search(
    user,
    query: str,
    collection_ids=None,
    top_k: int | None = None,
    *,
    hybrid: bool | None = None,
) -> list[Hit]:
    """Beste Abschnitte zu ``query`` aus lesbaren Sammlungen (siehe Moduldoku).

    ``collection_ids``: None = alle lesbaren, sonst nur diese (soweit lesbar).
    ``top_k``/``hybrid``: None = aus ``RagSettings``. Embedding-Fehler gehen als
    ``EmbeddingError`` an den Aufrufer.
    """
    query = (query or "").strip()[:MAX_QUERY_CHARS]
    if not query or not _usable(user):
        return []
    if collection_ids is not None and not list(collection_ids):
        return []
    rag = RagSettings.load()
    top_k = max(1, int(top_k or rag.top_k))
    hybrid = rag.hybrid if hybrid is None else hybrid
    qs = accessible_chunks(user, collection_ids)
    candidates = max(MIN_CANDIDATES, top_k * CANDIDATE_FACTOR)

    vector = embed_query(query)
    vector_hits = _vector_ranking(qs, vector, candidates)
    if hybrid:
        scores = rrf([pk for pk, _ in vector_hits], _text_ranking(qs, query, candidates))
    else:
        scores = {pk: 1.0 - distance for pk, distance in vector_hits}
    best = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
    if not best:
        return []
    # Erneut über den Zugriffsfilter laden (nicht nur per pk).
    chunks = qs.filter(pk__in=[pk for pk, _ in best]).select_related("document")
    by_id = {c.pk: c for c in chunks}
    hits = []
    for pk, score in best:
        chunk = by_id.get(pk)
        if chunk is None:
            continue
        hits.append(
            Hit(
                chunk_id=chunk.pk,
                document_id=chunk.document_id,
                document_title=chunk.document.title,
                collection_id=chunk.document.collection_id,
                page=chunk.page,
                text=chunk.text,
                score=round(score, 6),
                page_end=chunk.page_end,
                paragraph=chunk.paragraph,
                paragraph_end=chunk.paragraph_end,
                section=chunk.section,
                section_title=chunk.section_title,
                section_end=chunk.section_end,
                biblio=citations.reference_from_document(chunk.document).to_dict(),
            )
        )
    return hits
