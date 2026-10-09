"""Kopfzeilenhinweis zur Einsicht und Seitenleisten-Link „Familie“ (M6-04, M6-05)."""

from django import template

from multigpt.accounts.permissions import Action, can, supervision_active

register = template.Library()


@register.filter
def can_manage_family(user) -> bool:
    return can(user, Action.MANAGE_FAMILY)


@register.filter
def can_view_usage_all(user) -> bool:
    return can(user, Action.VIEW_USAGE_ALL)


@register.inclusion_tag("accounts/family/_supervision_notice.html", takes_context=True)
def supervision_notice(context):
    """Dauerhafter Hinweis für das Mitglied, solange die Einsicht wirkt."""
    request = context.get("request")
    user = getattr(request, "user", None)
    active = bool(user is not None and user.is_authenticated and supervision_active(user))
    return {"supervision_active": active}
