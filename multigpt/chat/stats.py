"""Statistik je Antwort: Token/s und Zeit bis zum ersten Token.

MultiGPT misst selbst (alle Anbieter gleich, auch LM Studio über ``/v1``): je
Anbieteraufruf die Zeit bis zum ersten Lebenszeichen des Modells (Text, Denken
oder Werkzeugaufruf, ``Delta`` auch leer) und die Erzeugungszeit danach. Gespeichert
in ``Message.tool_state["stats"]``:

- ``ttft_ms``: erstes Token im ersten Aufruf (Wartezeit inkl. Verarbeiten der Frage);
- ``gen_ms`` / ``tokens``: Summe über alle Aufrufe der Antwort (Werkzeugrunden),
  Ausgabe-Tokens laut Anbieter inkl. Reasoning.

Token/s = ``tokens`` / ``gen_ms``; unter ``MIN_GEN_MS`` keine Angabe (sonst
Zufallswerte, wenn ein Anbieter alles auf einmal schickt).
"""

from __future__ import annotations

MIN_GEN_MS = 200


def add_round(state: dict, started: float, first_at: float | None, ended: float, tokens: int):
    """Einen Anbieteraufruf (Zeiten aus ``time.monotonic``) in ``state`` aufnehmen."""
    if first_at is None:
        return
    stats = state.setdefault("stats", {})
    stats.setdefault("ttft_ms", round((first_at - started) * 1000))
    stats["gen_ms"] = stats.get("gen_ms", 0) + round((ended - first_at) * 1000)
    stats["tokens"] = stats.get("tokens", 0) + max(0, int(tokens or 0))


def _seconds(ms: int) -> str:
    return f"{ms / 1000:.1f}".replace(".", ",") + " s"


def summary(state: dict | None) -> dict | None:
    """Anzeige: ``text`` (kurz, Kopf der Antwort) und ``title`` (ausführlich) oder ``None``."""
    stats = (state or {}).get("stats")
    if not isinstance(stats, dict) or "ttft_ms" not in stats:
        return None
    parts, details = [], []
    tokens, gen_ms = stats.get("tokens", 0), stats.get("gen_ms", 0)
    if tokens and gen_ms >= MIN_GEN_MS:
        rate = tokens * 1000 / gen_ms
        shown = f"{rate:.0f}" if rate >= 10 else f"{rate:.1f}".replace(".", ",")
        parts.append(f"{shown} Token/s")
        details.append(f"{tokens} Ausgabe-Tokens in {_seconds(gen_ms)}")
    first = _seconds(stats["ttft_ms"])
    parts.append(f"erstes Token nach {first}")
    details.append(f"erstes Token nach {first} (inkl. Verarbeiten der Frage)")
    return {"text": " · ".join(parts), "title": "; ".join(details)}
