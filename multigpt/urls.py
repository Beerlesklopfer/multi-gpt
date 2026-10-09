from django.contrib import admin
from django.urls import include, path

from multigpt.chat import views as chat_views

urlpatterns = [
    path("healthz/", chat_views.healthz, name="healthz"),
    path("konto/", include("django.contrib.auth.urls")),
    path("admin/", admin.site.urls),
    path("", include("multigpt.chat.urls")),
]
