"""Modus „Bild“ im Eingabefeld (M9-01): Daten für chat/_image_mode.html."""

from django import template

from .. import images

register = template.Library()


@register.simple_tag(takes_context=True)
def image_mode_config(context) -> dict:
    """Ist ein Bildmodell für den Betrachter nutzbar? Dazu Formate, Qualitäten und
    die Muster für den Hinweis bei Bildwünschen (``images.REQUEST_PATTERNS``)."""
    request = context.get("request")
    user = getattr(request, "user", None)
    if user is None or not user.is_authenticated:
        return {"available": False}
    models = images.selectable_models(user)
    if not models:
        return {"available": False}
    return {
        "available": True,
        "model_name": models[0].display_name,
        # Mehrere Bildmodelle (z. B. OpenAI und ein lokales): im Chat wählbar, Standard zuerst.
        "models": [{"id": m.pk, "name": m.display_name} for m in models],
        "formats": list(images.FORMAT_LABELS.items()),
        "qualities": list(images.QUALITIES.items()),
        "patterns": {"request": images.REQUEST_PATTERNS, "code": images.CODE_PATTERN},
    }
