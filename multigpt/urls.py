from django.contrib import admin
from django.urls import include, path

from multigpt.accounts import views_usage

urlpatterns = [
    path("konto/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    path("familie/", include("multigpt.accounts.urls_family")),
    # Eigener Verbrauch (M6, budget); vor dem chat-include, der api/ belegt.
    path("verbrauch/", views_usage.usage_page, name="usage"),
    path("api/usage/", views_usage.usage_api, name="api_usage"),
    path("", include("multigpt.chat.urls")),
]
