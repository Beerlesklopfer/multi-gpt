"""Seiten des Runners unter /runner/ (die WebSocket-Adresse /runner/ws bedient
der Runner-Dienst; nginx leitet nur genau diesen Pfad dorthin)."""

from django.urls import path

from . import api_chat, views, views_live

app_name = "runner"

urlpatterns = [
    path("", views.runner_page, name="index"),
    path("token/<int:pk>/widerrufen/", views.token_revoke, name="token_revoke"),
    path("token/<int:pk>/loeschen/", views.token_delete, name="token_delete"),
    # Runner-Programm (natives Programm) und Prüfsummen: Anmeldung oder Kopplungs-Token
    path("download/<str:arch>", views.download, name="download"),
    path("arbeitsstaende/", views.workspaces_page, name="workspaces"),
    path("arbeitsstaende/<int:pk>/", views.workspace_detail, name="workspace"),
    path("arbeitsstaende/<int:pk>/bundle/", views.workspace_download, name="workspace_download"),
    path("arbeitsstaende/<int:pk>/loeschen/", views.workspace_delete, name="workspace_delete"),
    # Auswahl Arbeitsstand und Modus je Chat (tools, 8j/8t)
    path("api/chat/<int:pk>/", api_chat.chat_state, name="api_chat"),
    # Live-Status, Audit, Terminal und Vorschau (M14-12, 8p/8x; views_live.py)
    path("status/", views_live.status_api, name="status"),
    path("sitzung/<uuid:session_uuid>/", views_live.session_page, name="session"),
    path("api/sitzungen/<uuid:session_uuid>/", views_live.session_api, name="session_api"),
    path("api/sitzungen/<uuid:session_uuid>/pty/", views_live.pty_stream, name="pty_stream"),
    path(
        "api/sitzungen/<uuid:session_uuid>/pty/<str:pty>/eingabe/",
        views_live.pty_input,
        name="pty_input",
    ),
    path(
        "api/sitzungen/<uuid:session_uuid>/pty/<str:pty>/groesse/",
        views_live.pty_resize,
        name="pty_resize",
    ),
    path(
        "api/sitzungen/<uuid:session_uuid>/pty/<str:pty>/schliessen/",
        views_live.pty_close,
        name="pty_close",
    ),
    path("vorschau/<uuid:session_uuid>/<str:token>/", views_live.preview, name="preview_root"),
    path(
        "vorschau/<uuid:session_uuid>/<str:token>/<path:path>",
        views_live.preview,
        name="preview",
    ),
]
