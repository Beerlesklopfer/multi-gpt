from django.urls import path

from . import views

app_name = "scratchpad"
urlpatterns = [
    path("", views.index, name="index"),
    path("dateien/<int:pk>/", views.file, name="file"),
    path("dateien/<int:pk>/loeschen/", views.delete, name="delete"),
]
