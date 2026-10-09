from django.urls import include, path

from . import views, views_collections

app_name = "chat"

# Seiten (Agent ui) hier, JSON/SSE-Endpunkte (Agent stream) in api_urls.py.
urlpatterns = [
    path("", views.index, name="index"),
    path("c/<int:pk>/", views.conversation, name="conversation"),
    path("c/<int:pk>/messages/", views.conversation_messages, name="conversation_messages"),
    path("c/<int:pk>/export.md", views.export_markdown, name="conversation_export"),
    # Sammlungen und Dokumente (M7, RAG)
    path("sammlungen/", views_collections.collection_list, name="collection_list"),
    path("sammlungen/<int:pk>/", views_collections.collection_detail, name="collection_detail"),
    path(
        "dokumente/<int:pk>/download/",
        views_collections.document_download,
        name="document_download",
    ),
    path(
        "dokumente/abschnitt/<int:chunk_id>/",
        views_collections.document_chunk,
        name="document_chunk",
    ),
    path("api/", include("multigpt.chat.api_urls")),
]
