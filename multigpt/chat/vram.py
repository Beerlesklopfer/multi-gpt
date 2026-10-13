"""Speicherschätzung für lokale Modelle (Modellverwaltung LM Studio, M7-09 erweitert 0.3.2).

Belegung eines geladenen Modells im Grafikspeicher, bewusst konservativ:

    gesamt = Modelldatei + Kontext-Cache (KV-Cache) + Zuschlag

**Modelldatei:** ``size_bytes`` aus ``GET /api/v1/models`` (bei Vision-Modellen
samt Bild-Projektor, z. B. olmocr-2-7b 6,04 GB = Sprachmodell Q4_K_M plus mmproj).

**KV-Cache:** Für jedes Token im Kontext speichert jede Schicht einen Key- und
einen Value-Vektor je KV-Kopf:

    KV = 2 × Schichten × KV-Köpfe × Kopfdimension × Bytes je Wert × Kontext

(Quelle: NVIDIA, „Mastering LLM Techniques: Inference Optimization“,
https://developer.nvidia.com/blog/mastering-llm-techniques-inference-optimization/
– „Size of KV cache per token in bytes = 2 * (num_layers) * (num_heads *
dim_head) * precision_in_bytes“; bei Grouped-Query-Attention zählen die
KV-Köpfe). Bytes je Wert: 2 (f16, Standard von llama.cpp und LM Studio).
Die Parallelität (``parallel``) vervielfacht den Cache nicht: llama.cpp teilt
das eine Kontextfenster auf die gleichzeitigen Anfragen auf.

**Architekturdaten:** LM Studio meldet nur ``architecture`` (z. B. ``gpt-oss``,
``qwen2vl``), ``params_string`` und die Quantisierung, nicht Schichten und
Köpfe. Für bekannte Modelle stehen die Werte aus deren ``config.json`` in
``KNOWN`` (Hugging Face: openai/gpt-oss-20b, allenai/olmOCR-2-7B-1025,
Qwen/Qwen3-30B-A3B, nomic-ai/nomic-embed-text-v1.5 u. a.). Sonst gilt eine
konservative Näherung nach Parameterzahl: 8 KV-Köpfe × 128 Dimensionen und
reichlich Schichten (``_fallback``). Embedding-Modelle ohne Eintrag: wie
BERT-large (24 Schichten, 16 Köpfe, 64 Dimensionen).

**Sliding-Window-Attention (gpt-oss):** Jede zweite Schicht sieht nur die
letzten 128 Tokens. llama.cpp hält für diese Schichten nur ein Fenster vor
(Fenster × Parallelität plus Batch); hier gerechnet mit Fenster × Parallelität
+ 2048 Tokens, höchstens der Kontext.

**Zuschlag:** Rechenpuffer 0,5 GB je Sprachmodell, bei Bild-Eingabe 0,3 GB
mehr (Bild-Encoder), bei Embedding-Modellen 0,25 GB.

Ein gemessener Wert am Modell (``AIModel.vram_gb``) hat immer Vorrang.
Einheit GB = 10⁹ Byte wie die Dateigrößen in LM Studio.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

GB = 1_000_000_000
BYTES_PER_VALUE = 2  # f16
OVERHEAD_LLM = int(0.5 * GB)
OVERHEAD_VISION = int(0.3 * GB)
OVERHEAD_EMBEDDING = int(0.25 * GB)
SWA_EXTRA_TOKENS = 2048
DEFAULT_PARALLEL = 4  # LM Studio ohne Angabe

# Auswahl „Kontextfenster“ im Admin; angeboten bis zur maximalen Länge des Modells.
CONTEXT_STAGES = (2048, 4096, 8192, 16384, 32768, 65536, 131072)
DEFAULT_CHAT_CONTEXT = 32768
DEFAULT_OCR_CONTEXT = 8192


@dataclass(frozen=True)
class Arch:
    """Schichten, KV-Köpfe, Kopfdimension; ``swa_layers`` mit ``window`` Tokens.

    ``full_kv_heads``/``full_head_dim``: abweichende Werte der Schichten mit voller
    Attention (Gemma 4); ``linear_layers``: Schichten mit linearer Attention ohne
    wachsenden Cache, stattdessen ``state_bytes`` je Sequenz (Qwen3.5/3.8).
    """

    layers: int
    kv_heads: int
    head_dim: int
    swa_layers: int = 0
    window: int = 0
    label: str = ""
    full_kv_heads: int = 0
    full_head_dim: int = 0
    linear_layers: int = 0
    state_bytes: int = 0


# (Architektur laut LM Studio, Parameter klein geschrieben) -> Daten aus config.json.
KNOWN: dict[tuple[str, str], Arch] = {
    ("gpt-oss", "20b"): Arch(24, 8, 64, swa_layers=12, window=128, label="gpt-oss-20b"),
    ("gpt-oss", "120b"): Arch(36, 8, 64, swa_layers=18, window=128, label="gpt-oss-120b"),
    # Qwen2/2.5-VL (olmOCR-2-7B-1025: hidden 3584 / 28 Köpfe = 128).
    ("qwen2vl", "7b"): Arch(28, 4, 128, label="Qwen2.5-VL 7B"),
    ("qwen2vl", "3b"): Arch(36, 2, 128, label="Qwen2.5-VL 3B"),
    ("qwen2vl", "2b"): Arch(28, 2, 128, label="Qwen2-VL 2B"),
    ("qwen3moe", "30b-a3b"): Arch(48, 4, 128, label="Qwen3 30B-A3B"),
    ("qwen3", "4b"): Arch(36, 8, 128, label="Qwen3 4B"),
    ("qwen3", "8b"): Arch(36, 8, 128, label="Qwen3 8B"),
    ("qwen3", "14b"): Arch(40, 8, 128, label="Qwen3 14B"),
    ("qwen3", "32b"): Arch(64, 8, 128, label="Qwen3 32B"),
    ("qwen3vl", "8b"): Arch(36, 8, 128, label="Qwen3-VL 8B"),
    ("llama", "8b"): Arch(32, 8, 128, label="Llama 3 8B"),
    # google/gemma-4-26B-A4B: 30 Schichten, 25 mit Fenster 1024 (8 KV-Köpfe × 256),
    # 5 volle mit 2 KV-Köpfen × 512 (num_global_key_value_heads, global_head_dim).
    ("gemma4", "26b-a4b"): Arch(
        30,
        8,
        256,
        swa_layers=25,
        window=1024,
        full_kv_heads=2,
        full_head_dim=512,
        label="Gemma 4 26B-A4B",
    ),
    # Qwen/Qwen3.8-27B (Architektur qwen35): 64 Schichten, nur jede vierte mit voller
    # Attention (4 KV-Köpfe × 256); 48 lineare (Gated DeltaNet) mit festem Zustand je
    # Sequenz: 48 Value-Köpfe × 128 × 128 × 4 Byte (float32) ≈ 3,1 MB je Schicht.
    ("qwen35", "27b"): Arch(
        64,
        4,
        256,
        linear_layers=48,
        state_bytes=48 * (48 * 128 * 128 * 4 + 256 * 1024),
        label="Qwen3.8 27B",
    ),
}
# Embedding-Modelle (LM Studio meldet keine Architektur): Muster in der Modell-ID.
KNOWN_EMBEDDING = [
    (re.compile(r"nomic-embed-text", re.I), Arch(12, 12, 64, label="nomic-embed-text")),
]
EMBEDDING_FALLBACK = Arch(24, 16, 64, label="Näherung (BERT-large)")

_PARAMS = re.compile(r"(\d+(?:\.\d+)?)\s*b", re.I)


@dataclass
class Estimate:
    weights: int
    kv: int
    overhead: int
    context: int | None
    source: str  # z. B. „gpt-oss-20b“, „Näherung nach Größe“, „gemessen“
    measured: bool = False

    @property
    def total(self) -> int:
        return self.weights + self.kv + self.overhead

    def as_dict(self) -> dict:
        return {
            "weights_gb": gb(self.weights),
            "kv_gb": gb(self.kv),
            "overhead_gb": gb(self.overhead),
            "total_gb": gb(self.total),
            "context": self.context,
            "source": self.source,
            "measured": self.measured,
        }


def gb(value: int | float) -> float:
    return round(value / GB, 2)


def gb_text(value: int | float) -> str:
    """„12,9 GB“ (eine Nachkommastelle, deutsches Komma)."""
    return f"{value / GB:.1f}".replace(".", ",") + " GB"


def total_params_b(params: str, size_bytes: int = 0, bits: float | None = None) -> float:
    """Parameterzahl in Milliarden: ``30B-A3B`` -> 30; sonst aus Größe und Bits."""
    match = _PARAMS.search(params or "")
    if match:
        return float(match.group(1))
    if size_bytes:
        return size_bytes * 8 / (bits or 4.5) / 1e9
    return 8.0


def _fallback(params_b: float) -> Arch:
    """Konservativ: GQA mit 8 KV-Köpfen à 128, eher viele Schichten."""
    for limit, layers in ((4, 36), (9, 36), (16, 48), (40, 64), (80, 80)):
        if params_b <= limit:
            return Arch(layers, 8, 128, label="Näherung nach Größe")
    return Arch(96, 8, 128, label="Näherung nach Größe")


def arch_for(
    architecture: str, params: str, model_type: str, key: str, size_bytes=0, bits=None
) -> Arch:
    if model_type == "embedding":
        for pattern, arch in KNOWN_EMBEDDING:
            if pattern.search(key or ""):
                return arch
        return EMBEDDING_FALLBACK
    known = KNOWN.get(((architecture or "").lower(), (params or "").lower()))
    if known is not None:
        return known
    return _fallback(total_params_b(params, size_bytes, bits))


def kv_bytes(arch: Arch, context: int, parallel: int | None = None) -> int:
    per_token_layer = 2 * arch.kv_heads * arch.head_dim * BYTES_PER_VALUE
    per_token_full = (
        2
        * (arch.full_kv_heads or arch.kv_heads)
        * (arch.full_head_dim or arch.head_dim)
        * BYTES_PER_VALUE
    )
    full = arch.layers - arch.swa_layers - arch.linear_layers
    total = full * per_token_full * context
    if arch.swa_layers:
        window = min(context, arch.window * max(1, parallel or 1) + SWA_EXTRA_TOKENS)
        total += arch.swa_layers * per_token_layer * window
    if arch.state_bytes:
        # Fester Zustand je Sequenz; ohne Angabe die Parallelität von LM Studio (4).
        total += arch.state_bytes * max(1, parallel or DEFAULT_PARALLEL)
    return total


def estimate(
    *,
    size_bytes: int,
    context: int | None,
    architecture: str = "",
    params: str = "",
    model_type: str = "llm",
    key: str = "",
    vision: bool = False,
    bits: float | None = None,
    parallel: int | None = None,
    measured_gb=None,
) -> Estimate:
    """Erwarteter Speicher eines Modells bei ``context`` Tokens."""
    if measured_gb:
        total = int(float(measured_gb) * GB)
        return Estimate(total, 0, 0, context, "gemessen", measured=True)
    arch = arch_for(architecture, params, model_type, key, size_bytes, bits)
    kv = kv_bytes(arch, context or 0, parallel) if context else 0
    if model_type == "embedding":
        overhead = OVERHEAD_EMBEDDING
    else:
        overhead = OVERHEAD_LLM + (OVERHEAD_VISION if vision else 0)
    return Estimate(int(size_bytes or 0), kv, overhead, context, arch.label)


def default_context(capability: str, max_context: int | None) -> int | None:
    """Vorschlag nach Art: Chat 32 768, OCR 8 192, Embedding Modellstandard (Maximum)."""
    from .models import AIModel

    if capability == AIModel.Capability.EMBEDDING:
        value = max_context
    elif capability == AIModel.Capability.OCR:
        value = DEFAULT_OCR_CONTEXT
    else:
        value = DEFAULT_CHAT_CONTEXT
    if value and max_context:
        value = min(value, max_context)
    return value


def stages(max_context: int | None) -> list[int]:
    """Stufen bis zur maximalen Kontextlänge (unbekannt: alle)."""
    if not max_context:
        return list(CONTEXT_STAGES)
    allowed = [s for s in CONTEXT_STAGES if s <= max_context]
    return allowed or [CONTEXT_STAGES[0]]
