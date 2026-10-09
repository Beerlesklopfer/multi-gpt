"""Template-Tags für Sammlungen (M7, RAG)."""

from django import template

register = template.Library()


@register.simple_tag
def rag_search_ready() -> bool:
    """Ist die Dokumentsuche eingerichtet (Embedding-Modell bzw. Schein-Embeddings)?

    Ohne sie lehnt der Server Anfragen mit Sammlungen ab (409); die Auswahl im
    Chat bleibt dann verborgen, die Sammlungsseiten zeigen einen Hinweis.
    """
    from ..rag.chat import search_ready

    return search_ready()
