"""JSON-/SSE-Endpunkte unter /api/ (Agent stream). Namespace ``chat``."""

from django.urls import path

from . import api

urlpatterns = [
    path("conversations/", api.conversation_create, name="api_conversations"),
    path("conversations/<int:pk>/messages/", api.messages, name="api_messages"),
    path("models/", api.models_list, name="api_models"),
]
