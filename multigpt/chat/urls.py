from django.urls import include, path

from . import views

app_name = "chat"

# Seiten (Agent ui) hier, JSON/SSE-Endpunkte (Agent stream) in api_urls.py.
urlpatterns = [
    path("", views.index, name="index"),
    path("c/<int:pk>/", views.conversation, name="conversation"),
    path("c/<int:pk>/export.md", views.export_markdown, name="conversation_export"),
    path("api/", include("multigpt.chat.api_urls")),
]
