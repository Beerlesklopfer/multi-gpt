"""JSON-/SSE-Endpunkte unter /api/ (Agent stream). Namespace ``chat``."""

from django.urls import path

from . import api, api_manage

urlpatterns = [
    path("conversations/", api.conversation_create, name="api_conversations"),
    path("conversations/<int:pk>/messages/", api.messages, name="api_messages"),
    path("models/", api.models_list, name="api_models"),
    path("providers/status/", api.provider_status, name="api_provider_status"),
    path(
        "conversations/<int:pk>/",
        api_manage.conversation_detail,
        name="api_conversation_detail",
    ),
]
