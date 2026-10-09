"""Seite „Familie“ (M6-04, M6-05), eingebunden unter /familie/."""

from django.urls import path

from . import views_family as views

app_name = "family"

urlpatterns = [
    path("", views.overview, name="overview"),
    path("konto/anlegen/", views.member_create, name="member_create"),
    path("konto/<int:pk>/", views.member_detail, name="member"),
    path("konto/<int:pk>/passwort/", views.member_password, name="member_password"),
    path("konto/<int:pk>/status/", views.member_active, name="member_active"),
    path("konto/<int:pk>/rolle/", views.member_role, name="member_role"),
    path("konto/<int:pk>/budget/", views.member_budget, name="member_budget"),
    path("konto/<int:pk>/einsicht/", views.member_supervision, name="member_supervision"),
    path("gruppen/", views.group_list, name="groups"),
    path("gruppen/anlegen/", views.group_create, name="group_create"),
    path("gruppen/<int:pk>/", views.group_detail, name="group"),
    path("gruppen/<int:pk>/umbenennen/", views.group_rename, name="group_rename"),
    path("gruppen/<int:pk>/mitglieder/", views.group_members, name="group_members"),
    path("gruppen/<int:pk>/loeschen/", views.group_delete, name="group_delete"),
    path("verbrauch/", views.usage_all, name="usage"),
]
