"""Embeddings für Abschnitte und Suchanfragen (Plan 8b, M7, M7-09).

``embed_texts(texts)`` nutzt das in ``RagSettings`` gewählte Embedding-Modell
über dessen Anbieter-Adapter und liefert Vektoren fester Länge
(``EMBEDDING_DIMENSIONS``, siehe settings ``RAG_EMBEDDING_DIMENSIONS``).

**Dimension (Entscheidung M7-09):** 768 = ``nomic-embed-text-v1.5`` lokal über
LM Studio (Modellkarte: https://huggingface.co/nomic-ai/nomic-embed-text-v1.5).
``dimensions`` wird nur an OpenAI-Modelle geschickt, die es kennen
(``text-embedding-3-*`` und neuer, die würden auf 768 gekürzt); LM Studio
benennt seine Modelle ebenfalls ``text-embedding-…`` – dort wird nichts
geschickt, sondern die gelieferte Länge geprüft. Passt sie nicht, gibt es
eine klare Meldung („liefert 1536 statt 768 Dimensionen“).

**Präfixe:** nomic-embed erwartet laut Modellkarte ``search_document: `` vor
Dokumentabschnitten und ``search_query: `` vor Suchanfragen. Die Präfixe stehen
in ``RagSettings`` (``document_prefix``/``query_prefix``) und werden bei Auswahl
eines nomic-Modells vorbelegt (``suggested_prefixes``).

**Fehler:** ``EmbeddingError`` mit deutschem ``message`` (ohne Key),
``retryable`` und ``unreachable`` (Anbieter aus) – der Worker wiederholt nur
wiederholbare Fehler und wartet bei nicht erreichbarem Anbieter ohne Obergrenze.

**Schein-Embeddings:** Mit ``RAG_FAKE_EMBEDDINGS`` *und* ``DEBUG`` entsteht ein
deterministischer Vektor aus gehashten Wörtern (ähnliche Wörter → ähnliche
Vektoren), ganz ohne Anbieter – nur für Browsertests in der Entwicklung.
"""

from __future__ import annotations

import hashlib
import logging
import math
import re

from django.conf import settings

from ..models import EMBEDDING_DIMENSIONS, AIModel, RagSettings
from ..providers import registry
from ..providers.base import ProviderError

logger = logging.getLogger(__name__)

MSG_NOT_CONFIGURED = (
    "Es ist kein Embedding-Modell eingerichtet. Ein Verwalter muss es in den "
    "RAG-Einstellungen festlegen."
)
MSG_MODEL_INACTIVE = "Das Embedding-Modell oder sein Anbieter ist deaktiviert."
MSG_WRONG_CAPABILITY = "Das gewählte Modell ist kein Embedding-Modell."
MSG_UNSUPPORTED = "Dieser Anbietertyp unterstützt keine Embeddings."
MSG_EMPTY = "Leere Texte lassen sich nicht einbetten."
MSG_WRONG_DIMENSIONS = (
    "Das Embedding-Modell liefert {got} statt {want} Dimensionen – das passt nicht zur "
    "Datenbank. Bitte ein Modell mit {want} Dimensionen wählen "
    "(z. B. text-embedding-nomic-embed-text-v1.5 über LM Studio)."
)
MSG_FAILED = "Die Embeddings konnten nicht erzeugt werden."
MSG_OFFLINE = "Der Anbieter „{name}“ des Embedding-Modells ist nicht erreichbar."

# Präfixe laut Modellkarte von nomic-embed-text (v1 und v1.5).
NOMIC_PATTERN = re.compile(r"nomic-embed", re.IGNORECASE)
NOMIC_DOCUMENT_PREFIX = "search_document: "
NOMIC_QUERY_PREFIX = "search_query: "

KIND_DOCUMENT = "document"
KIND_QUERY = "query"

_WORD = re.compile(r"\w+", re.UNICODE)


class EmbeddingError(Exception):
    """Embeddings nicht möglich; ``message`` ist ein deutscher Text ohne Key."""

    def __init__(self, message: str, *, retryable: bool = False, unreachable: bool = False):
        super().__init__(message)
        self.message = message
        self.retryable = retryable or unreachable
        self.unreachable = unreachable


class EmbeddingNotConfigured(EmbeddingError):
    """Kein (nutzbares) Embedding-Modell eingerichtet."""

    def __init__(self, message: str = MSG_NOT_CONFIGURED):
        super().__init__(message, retryable=False)


_OPENAI_DIMENSIONS = re.compile(r"^text-embedding-(\d+)(-|$)")


def supports_dimensions(model_id: str) -> bool:
    """Nur OpenAIs text-embedding-3 und neuer kennen ``dimensions`` (OpenAI-Doku).

    Nicht: ``text-embedding-ada-002`` und LM-Studio-Namen wie
    ``text-embedding-nomic-embed-text-v1.5``.
    """
    name = model_id.rsplit("/", 1)[-1].lower()  # z. B. OpenRouter "openai/…"
    match = _OPENAI_DIMENSIONS.match(name)
    return bool(match) and int(match.group(1)) >= 3


def suggested_prefixes(model_id: str) -> tuple[str, str]:
    """(Abschnitt, Anfrage)-Präfix für ein Modell: nomic-embed laut Modellkarte, sonst leer."""
    if NOMIC_PATTERN.search(model_id or ""):
        return NOMIC_DOCUMENT_PREFIX, NOMIC_QUERY_PREFIX
    return "", ""


def fake_enabled() -> bool:
    return bool(getattr(settings, "RAG_FAKE_EMBEDDINGS", False) and settings.DEBUG)


def fake_embedding(text: str, dimensions: int = EMBEDDING_DIMENSIONS) -> list[float]:
    """Deterministischer, normierter Wort-Hash-Vektor (nur für Tests/Entwicklung)."""
    vector = [0.0] * dimensions
    for word in _WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dimensions
        vector[index] += 1.0 if digest[4] & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in vector))
    if not norm:
        vector[0] = 1.0
        return vector
    return [x / norm for x in vector]


def embedding_model() -> AIModel:
    """Das eingestellte Embedding-Modell (mit Anbieter) oder EmbeddingNotConfigured."""
    model_id = RagSettings.load().embedding_model_id
    if model_id is None:
        raise EmbeddingNotConfigured()
    ai_model = AIModel.objects.select_related("provider").filter(pk=model_id).first()
    if ai_model is None:
        raise EmbeddingNotConfigured()
    if ai_model.capability != AIModel.Capability.EMBEDDING:
        raise EmbeddingNotConfigured(MSG_WRONG_CAPABILITY)
    if not (ai_model.active and ai_model.provider.active):
        raise EmbeddingNotConfigured(MSG_MODEL_INACTIVE)
    return ai_model


def _provider_error(exc: ProviderError) -> EmbeddingError:
    return EmbeddingError(
        str(exc) or MSG_FAILED,
        retryable=exc.retryable,
        unreachable=getattr(exc, "unreachable", False),
    )


def _call(ai_model: AIModel, texts: list[str]) -> list[list[float]]:
    """Adapter aufrufen; ``dimensions`` nur für Modelle, die es kennen."""
    dimensions = EMBEDDING_DIMENSIONS if supports_dimensions(ai_model.model_id) else None
    try:
        adapter = registry.get_adapter(ai_model.provider)
        return adapter.embed(ai_model.model_id, texts, dimensions=dimensions)
    except NotImplementedError:
        raise EmbeddingError(MSG_UNSUPPORTED) from None
    except ProviderError as exc:
        raise _provider_error(exc) from None
    except Exception as exc:
        logger.error("Embeddings mit Modell %s: %s", ai_model.pk, type(exc).__name__)
        raise EmbeddingError(MSG_FAILED, retryable=True) from None


def _check_dimensions(vectors: list[list[float]]) -> None:
    for vector in vectors:
        if len(vector) != EMBEDDING_DIMENSIONS:
            raise EmbeddingError(
                MSG_WRONG_DIMENSIONS.format(got=len(vector), want=EMBEDDING_DIMENSIONS)
            )


def _with_prefix(texts: list[str], prefix: str) -> list[str]:
    return [prefix + t for t in texts] if prefix else texts


def embed_with(ai_model: AIModel, texts: list[str], *, prefix: str = "") -> list[list[float]]:
    """Wie ``embed_texts``, aber mit einem bestimmten Modell und Präfix."""
    texts = list(texts)
    if not texts:
        return []
    if any(not isinstance(t, str) or not t.strip() for t in texts):
        raise EmbeddingError(MSG_EMPTY)
    if fake_enabled():
        return [fake_embedding(t) for t in texts]
    vectors = _call(ai_model, _with_prefix(texts, prefix))
    if len(vectors) != len(texts):
        raise EmbeddingError(MSG_FAILED)
    _check_dimensions(vectors)
    return vectors


def _ensure_reachable(ai_model: AIModel) -> None:
    """Schnelle Absage, wenn die Statusprüfung den (lokalen) Anbieter offline sieht.

    Nur für Anbieter mit „Online-Status prüfen“; die Prüfung ist höchstens
    15 s alt bzw. dauert höchstens ca. 2 s – statt bis zu 10 s Verbindungsaufbau.
    """
    provider = ai_model.provider
    if not provider.check_status:
        return
    from .. import status

    status.refresh(provider)
    if not provider.online:
        raise EmbeddingError(MSG_OFFLINE.format(name=provider.name), unreachable=True)


def embed_texts(texts: list[str], *, kind: str = KIND_DOCUMENT) -> list[list[float]]:
    """Ein Vektor (Länge ``EMBEDDING_DIMENSIONS``) je Text, gleiche Reihenfolge.

    ``kind``: ``document`` (Abschnitte) oder ``query`` (Suchanfrage) – bestimmt
    das Präfix aus ``RagSettings``. Leere Liste -> ``[]``. Leere Texte, fehlende
    Einrichtung und Anbieterfehler -> ``EmbeddingError`` (bzw.
    ``EmbeddingNotConfigured``).
    """
    texts = list(texts)
    if not texts:
        return []
    if fake_enabled():
        if any(not isinstance(t, str) or not t.strip() for t in texts):
            raise EmbeddingError(MSG_EMPTY)
        return [fake_embedding(t) for t in texts]
    ai_model = embedding_model()
    cfg = RagSettings.load()
    prefix = cfg.query_prefix if kind == KIND_QUERY else cfg.document_prefix
    if kind == KIND_QUERY:
        _ensure_reachable(ai_model)
    return embed_with(ai_model, texts, prefix=prefix)


def embed_query(text: str) -> list[float]:
    """Vektor für eine Suchanfrage (mit Anfrage-Präfix)."""
    return embed_texts([text], kind=KIND_QUERY)[0]


CHECK_TEXT = "Test der Einbettung für MultiGPT."


def check() -> tuple[str, str]:
    """(Stufe ``ok``/``error``, Meldung) für „Embedding testen“ im Admin.

    Nutzt das gespeicherte Modell; Schein-Embeddings werden dabei nicht
    verwendet, damit der Test den echten Anbieter prüft.
    """
    try:
        ai_model = embedding_model()
    except EmbeddingNotConfigured as exc:
        return "error", exc.message
    prefix = RagSettings.load().query_prefix
    try:
        vectors = _call(ai_model, _with_prefix([CHECK_TEXT], prefix))
    except EmbeddingError as exc:
        return "error", f"Embedding fehlgeschlagen: {exc.message}"
    got = len(vectors[0]) if vectors else 0
    if got != EMBEDDING_DIMENSIONS:
        return "error", "Embedding fehlgeschlagen: " + MSG_WRONG_DIMENSIONS.format(
            got=got, want=EMBEDDING_DIMENSIONS
        )
    return (
        "ok",
        f"Embedding erfolgreich: „{ai_model.display_name}“ liefert Vektoren mit {got} "
        "Dimensionen (passt zur Datenbank).",
    )
