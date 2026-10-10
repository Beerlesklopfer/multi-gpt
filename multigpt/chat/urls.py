from django.urls import include, path

from . import api_attachments, views, views_collections, views_projects

app_name = "chat"

# Seiten (Agent ui) hier, JSON/SSE-Endpunkte (Agent stream) in api_urls.py.
urlpatterns = [
    path("", views.index, name="index"),
    path("c/<int:pk>/", views.conversation, name="conversation"),
    path("c/<int:pk>/messages/", views.conversation_messages, name="conversation_messages"),
    path("c/<int:pk>/export.md", views.export_markdown, name="conversation_export"),
    # Anhänge (geschützte Auslieferung)
    path("anhang/<int:pk>/", api_attachments.serve, name="attachment"),
    path("anhang/<int:pk>/vorschau/", api_attachments.serve_thumbnail, name="attachment_thumb"),
    # Projekte (M5-07)
    path("projekte/", views_projects.project_list, name="project_list"),
    path("projekte/<int:pk>/", views_projects.project_detail, name="project_detail"),
    path(
        "projekte/<int:pk>/archivieren/",
        views_projects.project_archive,
        name="project_archive",
    ),
    path("projekte/<int:pk>/loeschen/", views_projects.project_delete, name="project_delete"),
    # Sammlungen und Dokumente (M7, RAG)
    path("sammlungen/", views_collections.collection_list, name="collection_list"),
    path("sammlungen/<int:pk>/", views_collections.collection_detail, name="collection_detail"),
    path(
        "dokumente/<int:pk>/download/",
        views_collections.document_download,
        name="document_download",
    ),
    path(
        "dokumente/<int:pk>/ansehen/",
        views_collections.document_view,
        name="document_view",
    ),
    path(
        "dokumente/<int:pk>/angaben/",
        views_collections.document_citation,
        name="document_citation",
    ),
    path(
        "dokumente/abschnitt/<int:chunk_id>/",
        views_collections.document_chunk,
        name="document_chunk",
    ),
    path("api/", include("multigpt.chat.api_urls")),
]
