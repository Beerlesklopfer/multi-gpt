"""Filter für die Anzeige der Werkzeugaufrufe im Chat (M4a-06).

Gleiche Symbole und Dauerformate wie chat.js (Live-Anzeige)."""

from django import template

register = template.Library()

# Status -> Symbol (nur Zierde, aria-hidden; der Status steht immer als Text daneben).
TOOL_STATUS_ICONS = {
    "awaiting_confirmation": "?",
    "running": "↻",  # ↻
    "ok": "✓",  # ✓
    "error": "✗",  # ✗
    "timeout": "⧗",  # ⧗
    "rejected": "⊘",  # ⊘
}

# So viele Zeichen des Ergebnisses zeigt die Oberfläche (wie das SSE-Event).
RESULT_DISPLAY_CHARS = 4000


@register.filter
def tool_status_icon(status) -> str:
    return TOOL_STATUS_ICONS.get(str(status), "•")


@register.filter
def tool_duration(ms) -> str:
    """Millisekunden lesbar: „350 ms“, „1,2 s“, „2 min 5 s“."""
    if ms is None or ms == "":
        return ""
    ms = max(0, int(ms))
    if ms < 1000:
        return f"{ms} ms"
    if ms < 60_000:
        return f"{ms / 1000:.1f} s".replace(".", ",")
    minutes, seconds = divmod(round(ms / 1000), 60)
    return f"{minutes} min {seconds} s"


@register.filter
def tool_result_display(text) -> str:
    text = text or ""
    if len(text) > RESULT_DISPLAY_CHARS:
        return text[:RESULT_DISPLAY_CHARS] + "…"
    return text


@register.filter
def tool_result_truncated(text) -> bool:
    return len(text or "") > RESULT_DISPLAY_CHARS
