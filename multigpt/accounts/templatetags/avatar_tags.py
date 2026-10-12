"""``{% avatar user size %}``: Profilbild bzw. Initialen auf fester Farbe."""

from django import template
from django.urls import reverse

from .. import avatar as avatar_lib

register = template.Library()


@register.filter
def can_use_api_keys(user) -> bool:
    """Rolle mit Recht „API-Keys/MCP-Zugang“ (sonst kein Menüeintrag)."""
    from multigpt.node import keys

    return bool(keys.role_scopes(user))


@register.inclusion_tag("accounts/_avatar.html")
def avatar(user, size: int = 32):
    url = ""
    if getattr(user, "avatar_updated", None):
        url = reverse("avatar_image", args=[user.pk])
        url += f"?v={int(user.avatar_updated.timestamp())}"
    return {
        "url": url,
        "initials": avatar_lib.initials(user),
        "color": avatar_lib.color(user),
        "size": int(size),
        "label": str(user),
    }
