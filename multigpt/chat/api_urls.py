"""JSON-/SSE-Endpunkte unter /api/ (Agent stream). Namespace ``chat``."""

from django.urls import path

from . import (
    api,
    api_attachments,
    api_collections,
    api_documents,
    api_manage,
    api_projects,
    api_sharing,
)

urlpatterns = [
    path("conversations/", api.conversation_create, name="api_conversations"),
    path("conversations/<int:pk>/messages/", api.messages, name="api_messages"),
    path("conversations/<int:pk>/branch/", api.branch, name="api_branch"),
    # Chats teilen (RWUD, api_sharing.py)
    path(
        "conversations/<int:pk>/shares/",
        api_sharing.shares,
        name="api_conversation_shares",
    ),
    path(
        "conversations/<int:pk>/shares/<int:share_id>/",
        api_sharing.share_detail,
        name="api_conversation_share_detail",
    ),
    path("conversations/<int:pk>/leave/", api_sharing.leave, name="api_conversation_leave"),
    path("conversations/<int:pk>/copy/", api_sharing.copy, name="api_conversation_copy"),
    path("conversations/<int:pk>/state/", api_sharing.state, name="api_conversation_state"),
    # Projekte (api_projects.py)
    path(
        "conversations/<int:pk>/project/",
        api_projects.conversation_project,
        name="api_conversation_project",
    ),
    path("projects/", api_projects.project_list, name="api_projects"),
    path("projects/<int:pk>/", api_projects.project_detail, name="api_project_detail"),
    path("models/", api.models_list, name="api_models"),
    # Anhänge im Chat (Bilder einfügen, Dateien hochladen)
    path("attachments/", api_attachments.upload, name="api_attachments"),
    path("attachments/<int:pk>/", api_attachments.detail, name="api_attachment_detail"),
    path("mcp-servers/", api.mcp_servers_list, name="api_mcp_servers"),
    path(
        "conversations/<int:pk>/tool-calls/confirm/",
        api.tool_confirm,
        name="api_tool_confirm",
    ),
    path("providers/status/", api.provider_status, name="api_provider_status"),
    path(
        "conversations/<int:pk>/",
        api_manage.conversation_detail,
        name="api_conversation_detail",
    ),
    # Sammlungen (M7, ragui); Upload/Statusliste der Dokumente: api_documents (ingest)
    path("collections/", api_collections.collections, name="api_collections"),
    path(
        "collections/<int:pk>/",
        api_collections.collection_detail,
        name="api_collection_detail",
    ),
    path(
        "collections/<int:pk>/shares/",
        api_collections.collection_shares_view,
        name="api_collection_shares",
    ),
    path(
        "collections/<int:pk>/documents/",
        api_documents.collection_documents,
        name="api_collection_documents",
    ),
    path("documents/<int:pk>/", api_collections.document_detail, name="api_document_detail"),
    path(
        "documents/<int:pk>/cancel/",
        api_collections.document_cancel,
        name="api_document_cancel",
    ),
]
