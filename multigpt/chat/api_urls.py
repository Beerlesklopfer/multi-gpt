"""JSON-/SSE-Endpunkte unter /api/ (Agent stream). Namespace ``chat``."""

from django.urls import path

from . import api, api_collections, api_documents, api_manage

urlpatterns = [
    path("conversations/", api.conversation_create, name="api_conversations"),
    path("conversations/<int:pk>/messages/", api.messages, name="api_messages"),
    path("conversations/<int:pk>/branch/", api.branch, name="api_branch"),
    path("models/", api.models_list, name="api_models"),
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
]
