"""``{% unread_notice_count user as n %}``: ungelesene Mitteilungen (A15)."""

from django import template

from .. import notices

register = template.Library()


@register.simple_tag
def unread_notice_count(user) -> int:
    return notices.unread_count(user)
