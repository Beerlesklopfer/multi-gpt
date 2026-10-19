"""REST-API von LM Studio für die Modellverwaltung (M7-09, erweitert 0.3.2).

Grundlage (gelesen 2026-10-10, LM Studio 0.4.x):

- Übersicht: https://lmstudio.ai/docs/developer/rest – die native API v1 unter
  ``/api/v1/*`` gibt es seit LM Studio 0.4.0 (Load/Unload/Download neu in 0.4.0,
  https://lmstudio.ai/docs/developer/api-changelog).
- ``GET /api/v1/models`` (https://lmstudio.ai/docs/developer/rest/list):
  ``{"models": [{type, key, display_name, architecture, quantization{name,
  bits_per_weight}, size_bytes, params_string, max_context_length,
  capabilities{vision, …}, loaded_instances: [{id, config: {context_length,
  parallel, …}}]}]}``. Geprüft lesend am LM Studio des Nutzers.
- ``POST /api/v1/models/load`` (https://lmstudio.ai/docs/developer/rest/load):
  ``model`` (Pflicht), optional ``context_length``, ``eval_batch_size``,
  ``flash_attention``, ``num_experts``, ``offload_kv_cache_to_gpu``,
  ``echo_load_config``; Antwort ``{type, instance_id, load_time_seconds,
  status: "loaded", load_config?}``. Fehlerformate sind nicht dokumentiert.
- ``POST /api/v1/models/unload`` (https://lmstudio.ai/docs/developer/rest/unload):
  ``{"instance_id": …}`` -> ``{"instance_id": …}``.
- TTL (https://lmstudio.ai/docs/developer/core/ttl-and-auto-evict): Per JIT
  geladene Modelle haben standardmäßig 60 Minuten TTL, mit ``lms load``
  geladene keine. Der Load-Endpunkt dokumentiert kein ``ttl`` – MultiGPT
  schickt deshalb keines; das Leerlauf-Entladen übernimmt der Worker.
- ``parallel`` steht in der Liste (``config.parallel``), beim Load-Endpunkt aber
  nicht in der Doku. Es wird nur geschickt, wenn am Modell ausdrücklich gesetzt.
- Grafikspeicher (frei/gesamt) meldet die API nicht; das Budget stellt der
  Verwalter ein (``Provider.vram_budget_gb``).

Authentifizierung wie bei ``/v1``: Bearer-Token, falls am Anbieter ein Key
gespeichert ist. Fehlertexte von LM Studio (z. B. „nicht genug Speicher“)
werden gekürzt übernommen – der lokale Server kennt keinen Key, der darin
stehen könnte; Logs enthalten nur IDs und HTTP-Status.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

LIST_TIMEOUT = 5.0
LOAD_TIMEOUT = 600.0  # große Modelle laden von der Platte (bis Minuten)
UNLOAD_TIMEOUT = 60.0
MAX_ERROR_CHARS = 200

MSG_UNREACHABLE = "LM Studio ist nicht erreichbar."
MSG_OLD_VERSION = "LM Studio kennt die Modellverwaltung nicht (REST-API v1 ab Version 0.4 nötig)."
MSG_INVALID = "LM Studio lieferte eine unerwartete Antwort."
MSG_HTTP = "LM Studio meldet HTTP {status}"


class LmsError(Exception):
    """Fehler der LM-Studio-API; ``message`` deutsch, ``unreachable`` bei Netzfehlern."""

    def __init__(self, message: str, *, status: int | None = None, unreachable: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.unreachable = unreachable


@dataclass
class Instance:
    id: str
    context_length: int | None = None
    parallel: int | None = None


@dataclass
class CatalogModel:
    """Ein Eintrag aus ``GET /api/v1/models``."""

    key: str
    type: str = "llm"  # llm, embedding, decision
    display_name: str = ""
    architecture: str = ""
    params: str = ""
    size_bytes: int = 0
    max_context_length: int | None = None
    bits_per_weight: float | None = None
    vision: bool = False
    # ``capabilities.reasoning``: erlaubte Stufen (off, low, medium, high, xhigh, on) und Vorgabe.
    reasoning_options: tuple[str, ...] = ()
    reasoning_default: str = ""
    instances: list[Instance] = field(default_factory=list)

    @property
    def loaded(self) -> bool:
        return bool(self.instances)


DEFAULT_ROOT = "http://localhost:1234"


def api_root(provider) -> str:
    """Server-Wurzel (eine Angabe je Anbieter): ``/v1`` für den Chat und ``/api/v1``
    für die Verwaltung werden daraus abgeleitet; ein ``/v1`` am Ende wird ignoriert."""
    base = (provider.base_url or DEFAULT_ROOT).rstrip("/")
    return base.removesuffix("/v1")


def _headers(provider) -> dict:
    headers = {"Accept": "application/json"}
    if provider.api_key:
        headers["Authorization"] = f"Bearer {provider.api_key}"
    return headers


def _int(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _error_text(response: httpx.Response) -> str:
    """Kurzer Fehlertext aus der Antwort (``error.message`` bzw. ``error``)."""
    try:
        data = response.json()
    except (ValueError, json.JSONDecodeError):
        return ""
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        error = error.get("message")
    if not isinstance(error, str):
        return ""
    text = " ".join(error.split())
    return text if len(text) <= MAX_ERROR_CHARS else text[: MAX_ERROR_CHARS - 1] + "…"


def parse_models(data) -> list[CatalogModel]:
    """Antwort von ``GET /api/v1/models`` -> Liste; unbrauchbare Einträge fallen weg."""
    items = data.get("models") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise LmsError(MSG_INVALID)
    result = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str):
            continue
        quant = item.get("quantization") if isinstance(item.get("quantization"), dict) else {}
        caps = item.get("capabilities") if isinstance(item.get("capabilities"), dict) else {}
        instances = []
        for inst in item.get("loaded_instances") or []:
            if isinstance(inst, dict) and isinstance(inst.get("id"), str):
                config = inst.get("config") if isinstance(inst.get("config"), dict) else {}
                instances.append(
                    Instance(
                        inst["id"],
                        _int(config.get("context_length")),
                        _int(config.get("parallel")),
                    )
                )
        bits = quant.get("bits_per_weight")
        reasoning = caps.get("reasoning") if isinstance(caps.get("reasoning"), dict) else {}
        options = reasoning.get("allowed_options")
        if not isinstance(options, list):
            options = []
        options = tuple(o for o in options if isinstance(o, str))[:10]
        default = reasoning.get("default") if isinstance(reasoning.get("default"), str) else ""
        result.append(
            CatalogModel(
                key=item["key"],
                type=str(item.get("type") or "llm"),
                display_name=str(item.get("display_name") or item["key"]),
                architecture=str(item.get("architecture") or ""),
                params=str(item.get("params_string") or ""),
                size_bytes=_int(item.get("size_bytes")) or 0,
                max_context_length=_int(item.get("max_context_length")),
                bits_per_weight=float(bits) if isinstance(bits, int | float) and bits > 0 else None,
                vision=bool(caps.get("vision")),
                reasoning_options=options,
                reasoning_default=default[:20],
                instances=instances,
            )
        )
    return result


# Eliezer (Modellverwalter, Ersatz für LM Studio) spricht dieselbe Schnittstelle und meldet sich
# im Kopf ``Server: eliezer/<Version>``. Die Art wird aus der letzten Antwort gemerkt.
_servers: dict[int, str] = {}


def server_kind(provider) -> str:
    """``"eliezer"``, wenn die letzte Antwort dieses Anbieters von Eliezer kam, sonst ``""``."""
    return _servers.get(provider.pk, "")


def _request(provider, method: str, path: str, timeout: float, body=None) -> httpx.Response:
    url = api_root(provider) + path
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.request(method, url, headers=_headers(provider), json=body)
        header = response.headers.get("server", "")
        _servers[provider.pk] = "eliezer" if header.lower().startswith("eliezer/") else ""
        return response
    except httpx.TimeoutException as exc:
        raise LmsError(f"{MSG_UNREACHABLE} (Zeitüberschreitung)", unreachable=True) from exc
    except httpx.HTTPError as exc:
        raise LmsError(MSG_UNREACHABLE, unreachable=True) from exc


def _check(response: httpx.Response, what: str, provider) -> None:
    if response.status_code == 200:
        return
    detail = _error_text(response)
    logger.warning("LM Studio %s (Anbieter %s): HTTP %s", what, provider.pk, response.status_code)
    if response.status_code == 404 and what == "Modellliste":
        raise LmsError(MSG_OLD_VERSION, status=404)
    message = MSG_HTTP.format(status=response.status_code)
    raise LmsError(f"{message}: {detail}" if detail else f"{message}.", status=response.status_code)


def list_models(provider, timeout: float = LIST_TIMEOUT) -> list[CatalogModel]:
    """Alle Modelle samt geladener Instanzen (``GET /api/v1/models``)."""
    response = _request(provider, "GET", "/api/v1/models", timeout)
    _check(response, "Modellliste", provider)
    try:
        return parse_models(response.json())
    except ValueError as exc:
        raise LmsError(MSG_INVALID) from exc


def load(
    provider,
    model_key: str,
    *,
    context_length: int | None = None,
    parallel: int | None = None,
    timeout: float = LOAD_TIMEOUT,
) -> dict:
    """Modell laden (``POST /api/v1/models/load``), ohne TTL. Rückgabe: Antwort-JSON."""
    body: dict = {"model": model_key, "echo_load_config": True}
    if context_length:
        body["context_length"] = int(context_length)
    if parallel:
        body["parallel"] = int(parallel)
    response = _request(provider, "POST", "/api/v1/models/load", timeout, body)
    _check(response, "Laden", provider)
    try:
        data = response.json()
    except ValueError as exc:
        raise LmsError(MSG_INVALID) from exc
    return data if isinstance(data, dict) else {}


def unload(provider, instance_id: str, timeout: float = UNLOAD_TIMEOUT) -> None:
    """Instanz entladen (``POST /api/v1/models/unload``)."""
    response = _request(
        provider, "POST", "/api/v1/models/unload", timeout, {"instance_id": instance_id}
    )
    _check(response, "Entladen", provider)


@dataclass
class Pool:
    """Speicher eines Pools (GPU oder CPU) laut Eliezer, bezogen auf eine Ladeanfrage."""

    budget: int
    free: int
    free_after_eviction: int
    fits_budget: bool = True
    fits_now: bool = True
    fits_after_eviction: bool = True
    max_context_length: int | None = None


@dataclass
class Estimate:
    """Antwort von ``POST /api/v1/models/estimate``: was Eliezer für das Laden rechnet."""

    model: str
    context_length: int | None
    parallel: int | None
    device: str
    total: int
    basis: str = ""
    weights: int = 0
    kv_cache: int = 0
    overhead: int = 0
    vision: int = 0
    pools: dict[str, Pool] = field(default_factory=dict)

    @property
    def pool(self) -> Pool | None:
        """Pool, in den das Modell käme (``device``), sonst der GPU-Pool."""
        return self.pools.get(self.device) or self.pools.get("gpu")


def _count(value) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def parse_estimate(data) -> Estimate:
    """Antwort von ``/api/v1/models/estimate`` -> ``Estimate``; Unbrauchbares ist ``LmsError``."""
    est = data.get("estimate") if isinstance(data, dict) else None
    pools = data.get("pools") if isinstance(data, dict) else None
    if not isinstance(est, dict) or not isinstance(pools, dict) or "total_bytes" not in est:
        raise LmsError(MSG_INVALID)
    result: dict[str, Pool] = {}
    for name, item in pools.items():
        if not isinstance(item, dict):
            continue
        result[str(name)] = Pool(
            budget=_count(item.get("budget_bytes")),
            free=_count(item.get("free_bytes")),
            free_after_eviction=_count(item.get("free_after_eviction_bytes")),
            fits_budget=bool(item.get("fits_budget", True)),
            fits_now=bool(item.get("fits_now", True)),
            fits_after_eviction=bool(item.get("fits_after_eviction", True)),
            max_context_length=_int(item.get("max_context_length")),
        )
    return Estimate(
        model=str(data.get("model") or ""),
        context_length=_int(data.get("context_length")),
        parallel=_int(data.get("parallel")),
        device=str(data.get("device") or "gpu"),
        total=_count(est.get("total_bytes")),
        basis=str(est.get("basis") or ""),
        weights=_count(est.get("weights_bytes")),
        kv_cache=_count(est.get("kv_cache_bytes")),
        overhead=_count(est.get("overhead_bytes")),
        vision=_count(est.get("vision_bytes")),
        pools=result,
    )


def estimate(
    provider,
    model_key: str,
    *,
    context_length: int | None = None,
    parallel: int | None = None,
    timeout: float = LIST_TIMEOUT,
) -> Estimate:
    """Speicherbedarf und Platz, wie Eliezer ihn rechnet (``POST /api/v1/models/estimate``)."""
    body: dict = {"model": model_key}
    if context_length:
        body["context_length"] = int(context_length)
    if parallel:
        body["parallel"] = int(parallel)
    response = _request(provider, "POST", "/api/v1/models/estimate", timeout, body)
    _check(response, "Schätzung", provider)
    try:
        return parse_estimate(response.json())
    except ValueError as exc:
        raise LmsError(MSG_INVALID) from exc


def instance_bytes(provider, timeout: float = LIST_TIMEOUT) -> dict[str, int]:
    """Belegter Speicher je geladenem Modell laut Eliezer (``/api/v1/status``, ``vram_bytes``)."""
    response = _request(provider, "GET", "/api/v1/status", timeout)
    _check(response, "Status", provider)
    try:
        data = response.json()
    except ValueError as exc:
        raise LmsError(MSG_INVALID) from exc
    models = data.get("models") if isinstance(data, dict) else None
    if not isinstance(models, list):
        raise LmsError(MSG_INVALID)
    return {
        item["key"]: _count(item.get("vram_bytes"))
        for item in models
        if isinstance(item, dict)
        and isinstance(item.get("key"), str)
        and item.get("state") in ("loaded", "loading", "unloading")
    }
