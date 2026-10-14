"""Modellverwaltung für lokale Anbieter (LM Studio): laden vor Gebrauch, Speicherbudget.

Weiterentwicklung von M7-09 („Modelle dauerhaft laden“, 0.3.2): Statt dass LM
Studio Modelle bei Bedarf selbst lädt (JIT) und dabei andere verdrängt oder den
Grafikspeicher überfüllt, bestimmt MultiGPT, was geladen ist. Aktiv nur bei
Anbietern mit „Modelle verwalten“ (``Provider.manage_models``); sonst ändert
sich nichts.

**Vor jedem Gebrauch** (Chat, Vergleich, ask, Embeddings, OCR, Abbildungen)
prüft ``use`` bzw. ``chat_ready``, ob das Modell geladen ist, und lädt es sonst
mit seinem Lade-Profil (Kontextfenster, Parallelität, keine TTL;
``chat/lmstudio.py``). Ein frisch bestätigter Zustand gilt 15 s je Prozess, damit
OCR nicht vor jeder Seite die Liste abruft.

**Budget, nie darüber:** nutzbar = Budget − Reserve (Monitor, System,
Schätzfehler). Belegung je geladenem Modell = Schätzung aus ``chat/vram.py``
(Datei + KV-Cache + Zuschlag) bzw. der gemessene Wert. Passt das neue Modell
nicht in den freien Rest, wird Platz geschaffen – aber nur, wenn das Entladen
genug frei macht; sonst wird nichts entladen und nichts geladen.

**Entladeregeln** (``_evictable``), Reihenfolge Priorität, dann zuletzt genutzt:

- nie angeheftete Modelle;
- nie Modelle mit laufender Anfrage (``ModelLease``, prozessübergreifend, mit Ablauf);
- nie Modelle, die MultiGPT nicht kennt (von Hand in LM Studio geladen);
- Indexierungsmodelle (Embedding, OCR, Abbildungen laut RAG-Einstellungen)
  zählen weniger als ein Chatmodell, auf das ein Mensch wartet – solange kein
  Lauf offen ist; während eines Laufs sind sie vor dem Chat geschützt;
- im Chat dürfen Modelle gleicher oder niedrigerer Priorität weichen, die
  Indexierung verdrängt nur niedrigere Priorität (und andere Indexierungs-
  modelle) – das Chatmodell bleibt also ungefragt geladen.

**Gleichzeitigkeit:** Laden und Entladen je Anbieter unter einer PostgreSQL-
Advisory-Lock (Sitzungsebene); wer wartet, sieht danach das geladene Modell.

**Austausch auf Nachfrage:** Blockieren im Chat nur angeheftete Modelle oder
solche höherer Priorität (nicht belegte, von MultiGPT geladene), wirft ``plan``
``SwapPossible``: Der Chat zeigt die Meldung mit dem Angebot, diese Modelle zu
entladen; bestätigt der Nutzer, läuft die Antwort mit ``swap=True`` erneut
(``ensure_loaded``/``chat_ready``) und darf genau diese Modelle verdrängen. Das
Anheften bleibt eingestellt – braucht jemand das Modell wieder, wird es geladen.

**Fehler:** ``ModelUnavailable`` mit deutscher Meldung. Ein fehlgeschlagenes
Laden sperrt weitere Versuche mit Backoff (1, 2, 4 … höchstens 30 min). Ist LM
Studio selbst nicht erreichbar, greift die Verwaltung nicht ein – der
eigentliche Aufruf meldet dann wie bisher „nicht erreichbar“.

**Leerlauf:** ``unload_idle`` (Worker, jede Minute) entlädt Modelle nach
``AIModel.idle_unload_minutes`` ohne Gebrauch – nicht angeheftete, nicht
belegte, keine Indexierungsmodelle während eines Laufs.

Logs nur mit IDs, keine Inhalte.
"""

from __future__ import annotations

import contextvars
import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from django.db import connection
from django.utils import timezone

from . import lmstudio, vram
from .models import AIModel, IndexRun, ModelLease, ModelRuntime, Provider, RagSettings

logger = logging.getLogger(__name__)

LOCK_NAMESPACE = 7319  # erster Schlüssel der Advisory-Lock (zweiter: Anbieter-ID)
LOCK_WAIT = 240.0  # Sekunden: so lange auf ein laufendes Laden warten
LOCK_POLL = 0.5
LEASE_TTL = timedelta(minutes=20)  # Absturz: Belegung läuft spätestens dann ab
LEASE_RENEW = 60.0  # Sekunden: lange Chat-Antworten verlängern ihre Belegung
VERIFIED_FOR = 15.0  # Sekunden: „ist geladen“ je Prozess so lange glauben
BACKOFF_BASE = timedelta(minutes=1)
BACKOFF_MAX = timedelta(minutes=30)
MIN_FIT_CONTEXT = 8192  # kleiner wählt ``fit_context`` nicht von selbst

MSG_NO_AUTOLOAD = (
    "„{name}“ ist in LM Studio nicht geladen und darf nicht automatisch geladen werden."
)
MSG_TOO_BIG = (
    "„{name}“ braucht etwa {need} – mehr als das Budget für Modelle ({usable} nach Abzug der "
    "Reserve). Kleineres Kontextfenster oder Modell wählen bzw. Budget prüfen."
)
MSG_NO_ROOM = (
    "Zu wenig Grafikspeicher für „{name}“ (braucht etwa {need}, frei {free}): belegt durch {by}."
)
MSG_LOAD_FAILED = "„{name}“ konnte nicht geladen werden: {error}"
MSG_BACKOFF = "{error} Neuer Ladeversuch ab {when} Uhr."
MSG_BUSY = "LM Studio lädt gerade ein anderes Modell; bitte gleich noch einmal versuchen."
MSG_LOADING = "Lade {name} …"

# Lauf, für den der Worker gerade arbeitet (Hinweis „Lade …“ am Lauf).
current_run: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "model_manager_run", default=None
)
_verified: dict[tuple[int, str], float] = {}


class ModelUnavailable(Exception):
    """Modell nicht nutzbar; ``message`` deutsch für Chat bzw. Lauf."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class SwapPossible(ModelUnavailable):
    """Kein Platz, aber Entladen von ``unload`` (Modellnamen) auf Nachfrage reicht."""

    def __init__(self, message: str, unload: list[str]):
        super().__init__(message)
        self.unload = unload


# Gründe, die ein Nutzer im Chat per Austausch übergehen darf (nicht: in Benutzung,
# fremd geladen, Indexierung läuft).
SWAPPABLE = frozenset({"angeheftet", "höhere Priorität"})


# --- Grundlagen -------------------------------------------------------------------


def managed(provider: Provider) -> bool:
    return bool(
        provider.manage_models
        and provider.active
        and provider.is_lmstudio
        and provider.vram_budget_gb
    )


def model_name(ai_model: AIModel) -> str:
    return ai_model.model_id


def indexing_ids() -> set[int]:
    """Modelle, die die Indexierung nutzt (laut RAG-Einstellungen)."""
    cfg = RagSettings.load()
    ids = {cfg.embedding_model_id}
    if cfg.ocr_backend == RagSettings.OcrBackend.OLMOCR:
        ids.add(cfg.ocr_model_id)
    if cfg.describe_figures:
        ids.add(cfg.figure_model_id)
    ids.discard(None)
    return ids


# Indexierungsmodelle nach dem Lauf: kurze Schonfrist, damit ein Folgelauf oder eine
# Suche direkt danach nicht ent- und neu lädt.
AFTER_RUN_GRACE_MINUTES = 2


def run_open() -> bool:
    return IndexRun.objects.filter(status__in=IndexRun.OPEN).exists()


def busy_ids(now=None) -> set[int]:
    now = now or timezone.now()
    return set(
        ModelLease.objects.filter(expires__gt=now).values_list("ai_model_id", flat=True).distinct()
    )


def runtime(ai_model: AIModel) -> ModelRuntime:
    obj, _ = ModelRuntime.objects.get_or_create(ai_model=ai_model)
    return obj


def touch(ai_model: AIModel, now=None) -> None:
    now = now or timezone.now()
    if not ModelRuntime.objects.filter(ai_model=ai_model).update(last_used=now):
        ModelRuntime.objects.get_or_create(ai_model=ai_model, defaults={"last_used": now})


def _note_run(text: str) -> None:
    run_id = current_run.get()
    if run_id is not None:
        IndexRun.objects.filter(pk=run_id).update(note=text[:300])


# --- Belegung (laufende Anfragen) -----------------------------------------------------


def acquire_lease(ai_model: AIModel) -> int:
    now = timezone.now()
    return ModelLease.objects.create(ai_model=ai_model, created=now, expires=now + LEASE_TTL).pk


def renew_lease(lease_id: int) -> None:
    ModelLease.objects.filter(pk=lease_id).update(expires=timezone.now() + LEASE_TTL)


def release_lease(lease_id: int) -> None:
    ModelLease.objects.filter(pk=lease_id).delete()


# --- Sperre --------------------------------------------------------------------------


@contextmanager
def provider_lock(provider: Provider, wait: float | None = None):
    """Laden/Entladen je Anbieter serialisieren (Advisory-Lock auf Sitzungsebene)."""
    deadline = time.monotonic() + (LOCK_WAIT if wait is None else wait)
    with connection.cursor() as cursor:
        while True:
            cursor.execute("SELECT pg_try_advisory_lock(%s, %s)", [LOCK_NAMESPACE, provider.pk])
            if cursor.fetchone()[0]:
                break
            if time.monotonic() >= deadline:
                raise ModelUnavailable(MSG_BUSY)
            time.sleep(LOCK_POLL)
    try:
        yield
    finally:
        with connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_unlock(%s, %s)", [LOCK_NAMESPACE, provider.pk])


# --- Abgleich mit der Liste von LM Studio ------------------------------------------------


def match(ai_model: AIModel, catalog: list[lmstudio.CatalogModel]):
    """(Eintrag, geladene Instanz oder None) für ein Modell; (None, None) unbekannt."""
    entry_for_key = None
    for entry in catalog:
        for inst in entry.instances:
            if inst.id == ai_model.model_id:
                return entry, inst
        if entry.key == ai_model.model_id:
            entry_for_key = entry
    if entry_for_key is not None:
        inst = entry_for_key.instances[0] if entry_for_key.instances else None
        return entry_for_key, inst
    return None, None


def remember_catalog(provider: Provider, catalog: list[lmstudio.CatalogModel]) -> None:
    """Eckdaten je bekanntem Modell in ``ModelRuntime`` merken (für Admin offline)."""
    now = timezone.now()
    for ai_model in provider.ai_models.all():
        entry, _inst = match(ai_model, catalog)
        if entry is None:
            continue
        values = {
            "size_bytes": entry.size_bytes or None,
            "max_context_length": entry.max_context_length,
            "architecture": entry.architecture[:100],
            "params": entry.params[:50],
            "model_type": entry.type[:20],
            "vision": entry.vision,
            "reasoning_options": list(entry.reasoning_options),
            "reasoning_default": entry.reasoning_default,
        }
        rt = runtime(ai_model)
        changed = [k for k, v in values.items() if getattr(rt, k) != v]
        for key in changed:
            setattr(rt, key, values[key])
        rt.catalog_seen = now
        rt.save(update_fields=[*changed, "catalog_seen"])


def load_parallel(ai_model: AIModel) -> int | None:
    """``parallel`` fürs Laden: Embedding-Modelle lehnen es ab (LM Studio 0.4, am Gerät
    geprüft 2026-10-12: HTTP 400 „not supported for embedding models: parallel“).

    Ohne Angabe **1**: LM Studio teilt den geladenen Kontext durch die Parallelität
    (Standard 4), bei 16 384 Tokens bliebe je Anfrage 4 096 – am Gerät am 2026-10-14
    gesehen als „Verlauf passt nicht in den Kontext“ mit Werkzeugbeschreibungen. Mehr
    gleichzeitige Anfragen stellt der Verwalter am Modell ein und vergrößert dazu den
    Kontext entsprechend."""
    if ai_model.capability == AIModel.Capability.EMBEDDING:
        return None
    return ai_model.load_parallel or 1


def profile_context(ai_model: AIModel, max_context: int | None) -> int | None:
    value = ai_model.load_context_length or vram.default_context(ai_model.capability, max_context)
    if value and max_context:
        value = min(value, max_context)
    return value


def fit_context(ai_model: AIModel, entry, context: int | None, usable: int) -> int | None:
    """Ohne eigenes Kontextfenster (Standard 32 768): die größte Stufe, die ins Budget
    passt, statt „mehr als das Budget“ zu melden. Ein eingestelltes Fenster bleibt."""
    if ai_model.load_context_length or not context:
        return context
    stages = [s for s in vram.CONTEXT_STAGES if s <= context and s >= MIN_FIT_CONTEXT]
    for stage in sorted(stages, reverse=True):
        if estimate_model(ai_model, entry, stage, load_parallel(ai_model)).total <= usable:
            if stage < context:
                logger.info(
                    "LM Studio: Modell %s mit %d statt %d Tokens Kontext (Budget)",
                    ai_model.pk,
                    stage,
                    context,
                )
            return stage
    return context


def estimate_model(
    ai_model: AIModel | None,
    entry: lmstudio.CatalogModel | None,
    context: int | None,
    parallel: int | None = None,
    rt: ModelRuntime | None = None,
) -> vram.Estimate:
    """Schätzung aus dem Listeneintrag bzw. den gemerkten Eckdaten."""
    measured = ai_model.vram_gb if ai_model is not None else None
    if entry is not None:
        return vram.estimate(
            size_bytes=entry.size_bytes,
            context=context,
            architecture=entry.architecture,
            params=entry.params,
            model_type=entry.type,
            key=entry.key,
            vision=entry.vision,
            bits=entry.bits_per_weight,
            parallel=parallel,
            measured_gb=measured,
        )
    rt = rt or (ModelRuntime.objects.filter(ai_model=ai_model).first() if ai_model else None)
    is_embedding = ai_model is not None and ai_model.capability == AIModel.Capability.EMBEDDING
    return vram.estimate(
        size_bytes=(rt.size_bytes if rt else 0) or 0,
        context=context,
        architecture=rt.architecture if rt else "",
        params=rt.params if rt else "",
        model_type=(rt.model_type if rt and rt.model_type else "")
        or ("embedding" if is_embedding else "llm"),
        key=ai_model.model_id if ai_model else "",
        vision=bool(rt and rt.vision),
        parallel=parallel,
        measured_gb=measured,
    )


@dataclass
class Slot:
    """Eine geladene Instanz in LM Studio."""

    instance_id: str
    key: str
    ai_model: AIModel | None
    context: int | None
    bytes: int
    source: str
    pinned: bool = False
    busy: bool = False
    indexing: bool = False
    priority: int = 0
    last_used: datetime | None = None

    @property
    def label(self) -> str:
        return self.ai_model.model_id if self.ai_model else self.instance_id

    @property
    def size_text(self) -> str:
        return vram.gb_text(self.bytes)


@dataclass
class Snapshot:
    provider: Provider
    budget: int
    reserve: int
    slots: list[Slot] = field(default_factory=list)
    catalog: list[lmstudio.CatalogModel] = field(default_factory=list)
    run_open: bool = False

    @property
    def usable(self) -> int:
        return max(0, self.budget - self.reserve)

    @property
    def used(self) -> int:
        return sum(s.bytes for s in self.slots)

    @property
    def free(self) -> int:
        return self.usable - self.used


def snapshot(provider: Provider, catalog: list[lmstudio.CatalogModel] | None = None) -> Snapshot:
    """Belegung laut LM Studio (ruft die Liste ab, wenn ``catalog`` fehlt)."""
    if catalog is None:
        catalog = lmstudio.list_models(provider)
    models = list(provider.ai_models.all())
    by_instance = {m.model_id: m for m in models}
    by_key = {m.model_id: m for m in models}
    runtimes = {
        rt.ai_model_id: rt for rt in ModelRuntime.objects.filter(ai_model__provider=provider)
    }
    busy = busy_ids()
    indexing = indexing_ids()
    snap = Snapshot(
        provider,
        budget=int(float(provider.vram_budget_gb or 0) * vram.GB),
        reserve=int(float(provider.vram_reserve_gb or 0) * vram.GB),
        catalog=catalog,
        run_open=run_open(),
    )
    for entry in catalog:
        for inst in entry.instances:
            ai_model = by_instance.get(inst.id) or by_key.get(entry.key)
            est = estimate_model(ai_model, entry, inst.context_length, inst.parallel)
            rt = runtimes.get(ai_model.pk) if ai_model else None
            snap.slots.append(
                Slot(
                    instance_id=inst.id,
                    key=entry.key,
                    ai_model=ai_model,
                    context=inst.context_length,
                    bytes=est.total,
                    source=est.source,
                    pinned=bool(ai_model and ai_model.pinned),
                    busy=bool(ai_model and ai_model.pk in busy),
                    indexing=bool(ai_model and ai_model.pk in indexing),
                    priority=ai_model.load_priority if ai_model else 100,
                    last_used=rt.last_used if rt else None,
                )
            )
    return snap


# --- Planen ------------------------------------------------------------------------------


def _evictable(slot: Slot, requester: AIModel, interactive: bool, snap: Snapshot) -> str:
    """ "" = darf entladen werden, sonst der Grund (für die Meldung)."""
    if slot.ai_model is None:
        return "nicht von MultiGPT geladen"
    if slot.pinned:
        return "angeheftet"
    if slot.busy:
        return "in Benutzung"
    if slot.indexing:
        if interactive and snap.run_open:
            return "Indexierung läuft"
        return ""
    if interactive and slot.priority <= requester.load_priority:
        return ""
    if not interactive and slot.priority < requester.load_priority:
        return ""
    return "höhere Priorität"


def plan(
    snap: Snapshot, ai_model: AIModel, need: int, interactive: bool, swap: bool = False
) -> list[Slot]:
    """Zu entladende Instanzen, damit ``need`` Byte frei werden; sonst ``ModelUnavailable``
    bzw. ``SwapPossible`` (Chat: mit ``swap=True`` dürfen auch angeheftete Modelle und
    solche höherer Priorität weichen)."""
    name = model_name(ai_model)
    if need > snap.usable:
        raise ModelUnavailable(
            MSG_TOO_BIG.format(name=name, need=vram.gb_text(need), usable=vram.gb_text(snap.usable))
        )
    free = snap.free
    if free >= need:
        return []
    candidates = []
    blocked = []
    for slot in snap.slots:
        reason = _evictable(slot, ai_model, interactive, snap)
        if reason and not (swap and interactive and reason in SWAPPABLE):
            blocked.append((slot, reason))
        else:
            candidates.append(slot)
    oldest = timezone.make_aware(datetime(2000, 1, 1))
    candidates.sort(key=lambda s: (s.priority, s.last_used or oldest))
    chosen: list[Slot] = []
    for slot in candidates:
        chosen.append(slot)
        free += slot.bytes
        if free >= need:
            return chosen
    by = ", ".join(
        f"{slot.label} ({vram.gb_text(slot.bytes)}, {reason})" for slot, reason in blocked
    ) or ", ".join(f"{s.label} ({vram.gb_text(s.bytes)})" for s in snap.slots)
    message = MSG_NO_ROOM.format(
        name=name,
        need=vram.gb_text(need),
        free=vram.gb_text(max(0, snap.free)),
        by=by or "–",
    )
    swappable = [slot for slot, reason in blocked if reason in SWAPPABLE]
    if interactive and not swap and swappable:
        # Reicht es, wenn diese weichen? Dann den Austausch anbieten (nur die nötigen).
        swappable.sort(key=lambda s: (s.priority, s.last_used or oldest))
        unload = []
        for slot in swappable:
            unload.append(slot.label)
            free += slot.bytes
            if free >= need:
                raise SwapPossible(message, unload)
    raise ModelUnavailable(message)


# --- Laden ---------------------------------------------------------------------------------


def _record_failure(ai_model: AIModel, message: str) -> datetime:
    rt = runtime(ai_model)
    rt.load_failures = min(rt.load_failures + 1, 30)
    delay = min(BACKOFF_BASE * (2 ** (rt.load_failures - 1)), BACKOFF_MAX)
    rt.retry_after = timezone.now() + delay
    rt.last_error = message[:500]
    rt.loading_since = None
    rt.save(update_fields=["load_failures", "retry_after", "last_error", "loading_since"])
    return rt.retry_after


def _record_success(ai_model: AIModel) -> None:
    now = timezone.now()
    ModelRuntime.objects.filter(ai_model=ai_model).update(
        load_failures=0,
        retry_after=None,
        last_error="",
        loading_since=None,
        loaded_at=now,
        last_used=now,
    )


def _check_backoff(ai_model: AIModel) -> None:
    rt = ModelRuntime.objects.filter(ai_model=ai_model).first()
    if rt and rt.retry_after and rt.retry_after > timezone.now():
        when = timezone.localtime(rt.retry_after)
        raise ModelUnavailable(MSG_BACKOFF.format(error=rt.last_error, when=f"{when:%H:%M}"))


def _verified_recently(ai_model: AIModel) -> bool:
    seen = _verified.get((ai_model.provider_id, ai_model.model_id))
    return seen is not None and time.monotonic() - seen < VERIFIED_FOR


def _mark_verified(ai_model: AIModel) -> None:
    _verified[(ai_model.provider_id, ai_model.model_id)] = time.monotonic()


def forget(provider_id: int, model_id: str | None = None) -> None:
    """Zwischengespeichertes „ist geladen“ verwerfen (nach Entladen)."""
    for key in list(_verified):
        if key[0] == provider_id and (model_id is None or key[1] == model_id):
            _verified.pop(key, None)


def is_loaded(ai_model: AIModel) -> bool:
    """Geladen laut LM Studio? Nicht erreichbar bzw. nicht verwaltet -> True (nicht eingreifen)."""
    if not managed(ai_model.provider) or _verified_recently(ai_model):
        return True
    try:
        catalog = lmstudio.list_models(ai_model.provider)
    except lmstudio.LmsError:
        return True
    _entry, inst = match(ai_model, catalog)
    if inst is not None:
        _mark_verified(ai_model)
    return inst is not None


def _do_unload(provider: Provider, slot: Slot, reason: str) -> None:
    lmstudio.unload(provider, slot.instance_id)
    _disown(provider, [slot.instance_id])
    forget(provider.pk, slot.ai_model.model_id if slot.ai_model else slot.instance_id)
    logger.info(
        "LM Studio (Anbieter %s): Modell %s entladen (%s)",
        provider.pk,
        slot.ai_model.pk if slot.ai_model else "fremd",
        reason,
    )


def ensure_loaded(
    ai_model: AIModel,
    *,
    interactive: bool,
    on_status: Callable[[str], None] | None = None,
    force: bool = False,
    swap: bool = False,
) -> bool:
    """Modell geladen halten; ggf. Platz schaffen und laden. True, wenn geladen wurde.

    ``interactive``: ein Mensch wartet (Chat, Suche). ``force``: von Hand
    (Verwalter) – ohne „automatisch laden“ und ohne Backoff-Sperre. ``swap``: der
    Nutzer hat den Austausch bestätigt (``SwapPossible``).
    Wirft ``ModelUnavailable``.
    """
    provider = ai_model.provider
    if not managed(provider) or _verified_recently(ai_model):
        return False
    try:
        catalog = lmstudio.list_models(provider)
    except lmstudio.LmsError as exc:
        # Nicht erreichbar bzw. zu alt: nicht eingreifen; der Aufruf meldet den Fehler.
        logger.info("LM Studio (Anbieter %s): Liste nicht abrufbar (%s)", provider.pk, exc.status)
        return False
    remember_catalog(provider, catalog)
    if match(ai_model, catalog)[1] is not None:
        _mark_verified(ai_model)
        return False
    name = model_name(ai_model)
    if not (ai_model.auto_load or force):
        raise ModelUnavailable(MSG_NO_AUTOLOAD.format(name=name))
    if not force:
        _check_backoff(ai_model)
    if on_status:
        on_status(MSG_LOADING.format(name=name))
    _note_run(MSG_LOADING.format(name=name))
    try:
        with provider_lock(provider):
            catalog = lmstudio.list_models(provider)
            entry, inst = match(ai_model, catalog)
            if inst is not None:  # inzwischen von einem anderen Prozess geladen
                _mark_verified(ai_model)
                return False
            context = profile_context(ai_model, entry.max_context_length if entry else None)
            snap = snapshot(provider, catalog)
            context = fit_context(ai_model, entry, context, snap.usable)
            est = estimate_model(ai_model, entry, context, load_parallel(ai_model))
            for slot in plan(snap, ai_model, est.total, interactive, swap=swap):
                why = "Austausch" if swap and slot.pinned else "Platz"
                _do_unload(provider, slot, f"{why} für Modell {ai_model.pk}")
            ModelRuntime.objects.update_or_create(
                ai_model=ai_model, defaults={"loading_since": timezone.now()}
            )
            started = time.monotonic()
            try:
                answer = lmstudio.load(
                    provider,
                    entry.key if entry else ai_model.model_id,
                    context_length=context,
                    parallel=load_parallel(ai_model),
                )
            except lmstudio.LmsError as exc:
                message = MSG_LOAD_FAILED.format(name=name, error=exc.message)
                _record_failure(ai_model, message)
                logger.warning("LM Studio: Modell %s nicht geladen (%s)", ai_model.pk, exc.status)
                raise ModelUnavailable(message) from None
            _record_success(ai_model)
            instance = str(answer.get("instance_id") or (entry.key if entry else ai_model.model_id))
            ModelRuntime.objects.filter(ai_model=ai_model).update(loaded_instance=instance)
            _mark_verified(ai_model)
            logger.info(
                "LM Studio: Modell %s geladen (%d Tokens, %.1f s)",
                ai_model.pk,
                context or 0,
                time.monotonic() - started,
            )
            return True
    except lmstudio.LmsError as exc:  # Liste bzw. Entladen unter der Sperre
        raise ModelUnavailable(MSG_LOAD_FAILED.format(name=name, error=exc.message)) from None
    finally:
        _note_run("")


@contextmanager
def use(ai_model: AIModel, *, interactive: bool = False, on_status=None):
    """Modell für eine Anfrage belegen und vorher laden (Embeddings, OCR, Abbildungen)."""
    if not managed(ai_model.provider):
        yield
        return
    lease = acquire_lease(ai_model)
    try:
        ensure_loaded(ai_model, interactive=interactive, on_status=on_status)
        yield
    finally:
        release_lease(lease)
        touch(ai_model)


def chat_ready(ai_model: AIModel, swap: bool = False) -> Iterator[tuple[str, dict]]:
    """Chat: Modell laden (mit Statusmeldung). Rückgabe (``yield from``): ``None`` oder
    die Ausnahme (``ModelUnavailable``, ggf. ``SwapPossible`` mit Angebot)."""
    if not managed(ai_model.provider):
        return None
    touch(ai_model)
    if is_loaded(ai_model):
        return None
    yield "status", {"text": MSG_LOADING.format(name=model_name(ai_model)), "level": "info"}
    try:
        ensure_loaded(ai_model, interactive=True, swap=swap)
    except ModelUnavailable as exc:
        return exc
    return None


def hold(ai_model: AIModel, events: Iterator):
    """Chat-Ereignisse durchreichen und das Modell solange als belegt markieren.

    Wird der äußere Generator geschlossen (Client bricht ab), wird der innere
    ausdrücklich geschlossen, damit dessen Abbruch-Behandlung sofort läuft.
    """
    if not managed(ai_model.provider):
        yield from events
        return
    lease = acquire_lease(ai_model)
    renewed = time.monotonic()
    try:
        for event in events:
            if time.monotonic() - renewed > LEASE_RENEW:
                renew_lease(lease)
                renewed = time.monotonic()
            yield event
    finally:
        close = getattr(events, "close", None)
        if close is not None:
            close()
        release_lease(lease)
        touch(ai_model)


# --- Leerlauf und Hand-Steuerung -------------------------------------------------------------


def _disown(provider: Provider, instance_ids) -> None:
    """Merker „selbst geladen“ löschen (entladen bzw. nicht mehr geladen): Lädt eine
    andere Installation dasselbe Modell, hat die Instanz dieselbe ID (den Modellnamen)."""
    ModelRuntime.objects.filter(
        ai_model__provider=provider, loaded_instance__in=list(instance_ids)
    ).update(loaded_instance="")


def own_instance(ai_model: AIModel, instance_id: str) -> bool:
    loaded = (
        ModelRuntime.objects.filter(ai_model=ai_model)
        .values_list("loaded_instance", flat=True)
        .first()
    )
    return bool(loaded) and loaded == instance_id


def unload_idle(now=None) -> int:
    """Worker: Modelle nach Leerlauf entladen; abgelaufene Belegungen löschen."""
    now = now or timezone.now()
    ModelLease.objects.filter(expires__lte=now).delete()
    count = 0
    providers = Provider.objects.filter(
        manage_models=True, active=True, is_local=True, vram_budget_gb__isnull=False
    )
    for provider in providers:
        if not managed(provider):
            continue
        try:
            catalog = lmstudio.list_models(provider, timeout=3)
        except lmstudio.LmsError:
            continue
        remember_catalog(provider, catalog)
        snap = snapshot(provider, catalog)
        # Nicht mehr geladene Instanzen gelten nicht mehr als eigene.
        loaded = {slot.instance_id for slot in snap.slots}
        ModelRuntime.objects.filter(ai_model__provider=provider).exclude(
            loaded_instance=""
        ).exclude(loaded_instance__in=loaded).update(loaded_instance="")
        for slot in snap.slots:
            model = slot.ai_model
            if model is None or slot.pinned or slot.busy:
                continue
            # Nur selbst Geladenes: Eine andere MultiGPT-Installation am selben LM Studio
            # (oder ein Mensch) hat das Modell vielleicht gerade in Gebrauch (C33).
            if not own_instance(model, slot.instance_id):
                continue
            if slot.indexing and snap.run_open:
                continue
            minutes = model.idle_unload_minutes
            if slot.indexing and model.unload_after_indexing:
                minutes = min(minutes or AFTER_RUN_GRACE_MINUTES, AFTER_RUN_GRACE_MINUTES)
            if not minutes:
                continue
            if slot.last_used is None:
                touch(model, now)  # erstmals gesehen: ab jetzt zählt der Leerlauf
                continue
            if slot.last_used > now - timedelta(minutes=minutes):
                continue
            try:
                with provider_lock(provider, wait=0):
                    _do_unload(provider, slot, "Leerlauf")
                count += 1
            except (ModelUnavailable, lmstudio.LmsError):
                continue
    return count


def load_now(ai_model: AIModel) -> bool:
    """Von Hand laden (Verwalter): wie im Chat, ohne Backoff und „automatisch laden“."""
    forget(ai_model.provider_id, ai_model.model_id)
    return ensure_loaded(ai_model, interactive=True, force=True)


def unload_now(provider: Provider, instance_id: str) -> None:
    """Von Hand entladen (Verwalter); auch angeheftete und fremde Modelle."""
    with provider_lock(provider, wait=30):
        lmstudio.unload(provider, instance_id)
    _disown(provider, [instance_id])
    forget(provider.pk)
    logger.info("LM Studio (Anbieter %s): Instanz von Hand entladen", provider.pk)


def local_providers():
    """Anbieter mit der Schnittstelle „LM Studio“ (Kandidaten für die Modellverwaltung)."""
    return list(Provider.objects.filter(kind=Provider.Kind.LMSTUDIO).order_by("name"))


def budget_info(provider: Provider, *, others_than: AIModel | None = None) -> dict | None:
    """Budget, Reserve, belegt (ohne ``others_than``), frei – für Anzeige und Schätzung."""
    if not provider.vram_budget_gb:
        return None
    try:
        snap = snapshot(provider)
        error = ""
    except lmstudio.LmsError as exc:
        snap = Snapshot(
            provider,
            budget=int(float(provider.vram_budget_gb) * vram.GB),
            reserve=int(float(provider.vram_reserve_gb or 0) * vram.GB),
        )
        error = exc.message
    slots = [
        s
        for s in snap.slots
        if others_than is None or s.ai_model is None or s.ai_model.pk != others_than.pk
    ]
    used = sum(s.bytes for s in slots)
    return {
        "budget": snap.budget,
        "reserve": snap.reserve,
        "usable": snap.usable,
        "used": used,
        "free": snap.usable - used,
        "others": [(s.label, s.bytes) for s in slots],
        "error": error,
    }
