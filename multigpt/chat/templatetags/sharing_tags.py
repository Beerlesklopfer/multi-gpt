"""Template-Tags für geteilte Chats (sharing.py): Verfasser an Nachrichten und
wer eine Rückfrage bestätigen darf. Kontext aus ``sharing.page_context``;
ohne ihn (eigener Chat) bleibt alles wie bisher („Du“)."""

from django import template

from .. import sharing

register = template.Library()


@register.simple_tag(takes_context=True)
def message_author(context, msg) -> str:
    """„Du“ oder der Name der Person, die die Nachricht geschrieben hat."""
    owner_id = context.get("chat_owner_id")
    if owner_id is None:
        return "Du"
    return sharing.author_label(
        msg,
        context.get("user"),
        owner_id,
        context.get("chat_owner_name", ""),
        bool(context.get("show_authors")),
    )


@register.simple_tag(takes_context=True)
def may_confirm(context, msg) -> bool:
    """Darf der Betrachter die Rückfrage dieser Antwort beantworten?"""
    owner_id = context.get("chat_owner_id")
    user = context.get("user")
    if owner_id is None or user is None:
        return True
    return (msg.author_id or owner_id) == user.pk
